from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from omarchy_imagine.ffmpeg_util import (
    FfmpegNotFound,
    concat_manifest,
    extract_last_frame,
    stitch_clips,
)

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")


def _color_clip(path: Path, color: str) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c={color}:s=320x180:d=1",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-t",
            "1",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


def test_concat_manifest_quotes_paths(tmp_path: Path) -> None:
    clip = tmp_path / "clip's.mp4"
    text = concat_manifest([clip])
    assert "file '" in text
    assert r"clip'\''s.mp4" in text


def test_stitch_and_last_frame(tmp_path: Path) -> None:
    first = tmp_path / "a.mp4"
    second = tmp_path / "b.mp4"
    _color_clip(first, "black")
    _color_clip(second, "white")
    episode = tmp_path / "episode.mp4"
    stitch_clips([first, second], episode)
    assert episode.is_file() and episode.stat().st_size > 0

    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "csv=p=0",
            str(episode),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert float(probe.stdout.strip()) > 1.5

    frame = tmp_path / "last_frame.png"
    extract_last_frame(second, frame)
    assert frame.is_file() and frame.stat().st_size > 0
    assert frame.read_bytes().startswith(b"\x89PNG")


def test_missing_ffmpeg_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("omarchy_imagine.ffmpeg_util.ffmpeg_path", lambda: None)
    with pytest.raises(FfmpegNotFound):
        stitch_clips([tmp_path / "missing.mp4"], tmp_path / "episode.mp4")
