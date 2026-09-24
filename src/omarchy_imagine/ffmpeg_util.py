"""ffmpeg helpers. Imagine is never used to concatenate clips."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


class FfmpegNotFound(RuntimeError):
    pass


class FfmpegError(RuntimeError):
    pass


def ffmpeg_path() -> str | None:
    return shutil.which("ffmpeg")


def require_ffmpeg() -> str:
    binary = ffmpeg_path()
    if not binary:
        raise FfmpegNotFound("ffmpeg is not on PATH")
    return binary


def concat_manifest(clip_paths: list[Path]) -> str:
    lines: list[str] = []
    for path in clip_paths:
        escaped = str(path.resolve()).replace("'", r"'\''")
        lines.append(f"file '{escaped}'")
    return "\n".join(lines) + "\n"


def extract_last_frame(clip_path: Path, dest_path: Path) -> None:
    binary = require_ffmpeg()
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    _run(
        [
            binary,
            "-y",
            "-sseof",
            "-0.08",
            "-i",
            str(clip_path),
            "-frames:v",
            "1",
            str(dest_path),
        ],
        dest_path,
    )


def transcode_to_png(src: Path, dest: Path) -> None:
    binary = require_ffmpeg()
    dest.parent.mkdir(parents=True, exist_ok=True)
    _run([binary, "-y", "-i", str(src), str(dest)], dest)


def stitch_clips(clip_paths: list[Path], episode_path: Path) -> None:
    """Concat demuxer, hard cuts. Stream copy first, then libx264 if copy fails."""
    if not clip_paths:
        raise FfmpegError("no clips to stitch")
    binary = require_ffmpeg()
    episode_path.parent.mkdir(parents=True, exist_ok=True)
    list_path = episode_path.with_name("concat.txt")
    list_path.write_text(concat_manifest(clip_paths), encoding="utf-8")
    copy_cmd = [
        binary,
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(list_path),
        "-c",
        "copy",
        str(episode_path),
    ]
    copied = subprocess.run(copy_cmd, capture_output=True, text=True)
    if copied.returncode != 0 or not _nonempty(episode_path):
        if episode_path.exists():
            episode_path.unlink()
        encode_cmd = [
            binary,
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(list_path),
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(episode_path),
        ]
        encoded = subprocess.run(encode_cmd, capture_output=True, text=True)
        if encoded.returncode != 0 or not _nonempty(episode_path):
            detail = (encoded.stderr or copied.stderr or "ffmpeg produced no episode").strip()
            raise FfmpegError(detail[-2000:])


def _run(cmd: list[str], output: Path) -> None:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0 or not _nonempty(output):
        detail = (result.stderr or "ffmpeg produced no output").strip()
        raise FfmpegError(detail[-2000:])


def _nonempty(path: Path) -> bool:
    return path.is_file() and path.stat().st_size > 0
