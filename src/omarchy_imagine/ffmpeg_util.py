"""ffmpeg helpers. Imagine is never used to concatenate clips.

Grade match, when the build has ``signalstats`` and ``eq``, soft-matches
later clips toward clip 1 before concat. A missing filter or a failed
measurement skips the pass and leaves the original clips. It does not raise.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


class FfmpegNotFound(RuntimeError):
    pass


class FfmpegError(RuntimeError):
    pass


_STATS_KEYS = ("YAVG", "YMIN", "YMAX", "UAVG", "VAVG", "SATAVG")
_STATS_LINE = re.compile(
    r"lavfi\.signalstats\.(" + "|".join(_STATS_KEYS) + r")=(-?\d+(?:\.\d+)?)"
)
_GRADE_STRENGTH = 0.65


@dataclass(frozen=True)
class GradeMatch:
    """Clips to concatenate, whether a graded file was written, and a job note."""

    clips: list[Path]
    ran: bool
    note: str


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


def grade_match_enabled() -> bool:
    raw = os.environ.get("OMARCHY_GRADE_MATCH", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def ffmpeg_filter_names() -> set[str] | None:
    """Names reported by ``ffmpeg -filters``. ``None`` when ffmpeg is missing."""
    binary = ffmpeg_path()
    if not binary:
        return None
    result = subprocess.run(
        [binary, "-hide_banner", "-filters"],
        capture_output=True,
        text=True,
    )
    return _parse_filter_names(result.stdout)


def match_grade(clip_paths: list[Path]) -> GradeMatch:
    """Soft-match every clip after the first toward clip 1.

    Uses ``signalstats`` averages and a gentle ``eq`` (brightness, contrast,
    saturation, gamma). This is a colorlevels-style nudge that stock ffmpeg
    can run. ``lut3d`` is not required. The first clip is left unchanged so
    the episode keeps that grade as the reference.

    Skip, with a note, when the flag is off, ffmpeg or the filters are
    missing, there is only one clip, or a measurement fails. Never raises
    for those cases. A caller that needs a hard failure still gets the
    original paths back.
    """
    if len(clip_paths) < 2:
        return GradeMatch(
            list(clip_paths),
            False,
            "Grade match skipped: only one clip. Nothing to match.",
        )
    if not grade_match_enabled():
        return GradeMatch(
            list(clip_paths),
            False,
            "Grade match skipped: OMARCHY_GRADE_MATCH is off. Clips were concatenated unchanged.",
        )
    names = ffmpeg_filter_names()
    if names is None:
        return GradeMatch(
            list(clip_paths),
            False,
            "Grade match skipped: ffmpeg is not on PATH.",
        )
    if "signalstats" not in names or "eq" not in names:
        return GradeMatch(
            list(clip_paths),
            False,
            "Grade match skipped: ffmpeg has no eq or signalstats filter. "
            "Clips were concatenated unchanged.",
        )
    try:
        reference = _signal_stats(clip_paths[0])
    except (FfmpegError, FfmpegNotFound, OSError) as exc:
        return GradeMatch(
            list(clip_paths),
            False,
            f"Grade match skipped: could not read clip 1 ({exc}).",
        )

    graded: list[Path] = [clip_paths[0]]
    wrote = False
    problems: list[str] = []
    for path in clip_paths[1:]:
        dest = path.with_name(f"{path.stem}.graded.mp4")
        try:
            stats = _signal_stats(path)
            _apply_eq(path, dest, _eq_filter(reference, stats))
        except (FfmpegError, FfmpegNotFound, OSError) as exc:
            problems.append(f"{path.name}: {exc}")
            graded.append(path)
            continue
        graded.append(dest)
        wrote = True
    if not wrote:
        detail = "; ".join(problems) or "no clip was rewritten"
        return GradeMatch(
            list(clip_paths),
            False,
            f"Grade match skipped: {detail}. Clips were concatenated unchanged.",
        )
    note = "Grade match ran: later clips were soft-matched toward clip 1 with ffmpeg eq."
    if problems:
        note = f"{note} Skipped {'; '.join(problems)}."
    return GradeMatch(graded, True, note)


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


def _parse_filter_names(text: str) -> set[str]:
    names: set[str] = set()
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        _flags, name, arrow = parts[0], parts[1], parts[2]
        if "->" in arrow:
            names.add(name)
    return names


def _signal_stats(path: Path) -> dict[str, float]:
    binary = require_ffmpeg()
    result = subprocess.run(
        [
            binary,
            "-hide_banner",
            "-i",
            str(path),
            "-t",
            "2",
            "-vf",
            "signalstats,metadata=print",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
    )
    buckets: dict[str, list[float]] = {key: [] for key in _STATS_KEYS}
    for match in _STATS_LINE.finditer(result.stderr + "\n" + result.stdout):
        buckets[match.group(1)].append(float(match.group(2)))
    if not buckets["YAVG"]:
        detail = (result.stderr or "signalstats produced no YAVG").strip()
        raise FfmpegError(detail[-500:])
    return {key: sum(values) / len(values) for key, values in buckets.items() if values}


def _eq_filter(reference: dict[str, float], target: dict[str, float]) -> str:
    """Gentle eq. Identity is brightness 0, contrast 1, saturation 1, gamma 1."""
    delta = (reference["YAVG"] - target["YAVG"]) / 255.0 * _GRADE_STRENGTH
    brightness = _clamp(delta, -0.25, 0.25)
    ref_span = max(
        reference.get("YMAX", reference["YAVG"]) - reference.get("YMIN", reference["YAVG"]),
        1.0,
    )
    tgt_span = max(
        target.get("YMAX", target["YAVG"]) - target.get("YMIN", target["YAVG"]),
        1.0,
    )
    contrast = _clamp(1 + ((ref_span - tgt_span) / 255.0) * _GRADE_STRENGTH, 0.75, 1.35)
    gamma = 1.0
    if abs(delta) > 0.02:
        gamma = _clamp(1 + delta * 0.35, 0.85, 1.15)
    saturation = _saturation(reference, target)
    return (
        f"eq=brightness={brightness:.4f}:contrast={contrast:.4f}:"
        f"saturation={saturation:.4f}:gamma={gamma:.4f}"
    )


def _saturation(reference: dict[str, float], target: dict[str, float]) -> float:
    ref_sat = reference.get("SATAVG")
    tgt_sat = target.get("SATAVG")
    if ref_sat is not None and tgt_sat is not None and tgt_sat > 1:
        raw = 1 + ((ref_sat - tgt_sat) / tgt_sat) * _GRADE_STRENGTH * 0.35
        return _clamp(raw, 0.7, 1.4)

    def chroma(stats: dict[str, float]) -> float:
        return abs(stats.get("UAVG", 128.0) - 128.0) + abs(stats.get("VAVG", 128.0) - 128.0)

    raw = 1 + ((chroma(reference) - chroma(target)) / 128.0) * _GRADE_STRENGTH
    return _clamp(raw, 0.7, 1.4)


def _apply_eq(src: Path, dest: Path, eq: str) -> None:
    binary = require_ffmpeg()
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    _run(
        [
            binary,
            "-y",
            "-i",
            str(src),
            "-vf",
            eq,
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            "-an",
            str(dest),
        ],
        dest,
    )


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _run(cmd: list[str], output: Path) -> None:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0 or not _nonempty(output):
        detail = (result.stderr or "ffmpeg produced no output").strip()
        raise FfmpegError(detail[-2000:])


def _nonempty(path: Path) -> bool:
    return path.is_file() and path.stat().st_size > 0
