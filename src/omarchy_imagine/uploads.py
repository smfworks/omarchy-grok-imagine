"""Local reference images and music beds. Files stay under the data directory.

Nothing here calls xAI or downloads a file. Callers pass bytes they already read.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from omarchy_imagine.config import MAX_UPLOAD_AUDIO_BYTES, MAX_UPLOAD_IMAGE_BYTES


class UploadError(ValueError):
    def __init__(self, message: str, *, status_code: int = 422) -> None:
        super().__init__(message)
        self.status_code = status_code


def read_limited(raw: bytes, limit: int, label: str) -> bytes:
    if len(raw) > limit:
        raise UploadError(f"{label} exceeds {limit} bytes.", status_code=413)
    if not raw:
        raise UploadError(f"{label} is empty.")
    return raw


def image_suffix(raw: bytes) -> str:
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if raw.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if raw.startswith(b"RIFF") and len(raw) >= 12 and raw[8:12] == b"WEBP":
        return ".webp"
    raise UploadError("Reference images must be PNG, JPEG, or WebP.")


def audio_suffix(raw: bytes) -> str:
    if raw.startswith(b"RIFF") and len(raw) >= 12 and raw[8:12] == b"WAVE":
        return ".wav"
    if raw.startswith(b"ID3") or (len(raw) >= 2 and raw[0] == 0xFF and raw[1] & 0xE0 == 0xE0):
        return ".mp3"
    if raw.startswith(b"OggS"):
        return ".ogg"
    if len(raw) >= 12 and raw[4:8] == b"ftyp":
        return ".m4a"
    raise UploadError("Music must be WAV, MP3, M4A, or Ogg.")


def store_reference(data_dir: Path, raw: bytes) -> tuple[str, str]:
    """Write an image. Returns ``(id, relative path)``."""
    payload = read_limited(raw, MAX_UPLOAD_IMAGE_BYTES, "Reference image")
    suffix = image_suffix(payload)
    ref_id = uuid.uuid4().hex
    folder = data_dir / "references"
    folder.mkdir(parents=True, exist_ok=True)
    relative = f"references/{ref_id}{suffix}"
    (data_dir / relative).write_bytes(payload)
    return ref_id, relative


def store_music(data_dir: Path, raw: bytes) -> str:
    payload = read_limited(raw, MAX_UPLOAD_AUDIO_BYTES, "Music file")
    suffix = audio_suffix(payload)
    music_id = uuid.uuid4().hex
    folder = data_dir / "music"
    folder.mkdir(parents=True, exist_ok=True)
    relative = f"music/{music_id}{suffix}"
    (data_dir / relative).write_bytes(payload)
    return relative


def content_type_for(path: Path) -> str:
    suffix = path.suffix.lower()
    return {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".wav": "audio/wav",
        ".mp3": "audio/mpeg",
        ".m4a": "audio/mp4",
        ".ogg": "audio/ogg",
    }.get(suffix, "application/octet-stream")


def find_reference(data_dir: Path, ref_id: str) -> Path | None:
    if not ref_id or "/" in ref_id or ".." in ref_id:
        return None
    folder = data_dir / "references"
    matches = sorted(folder.glob(f"{ref_id}.*"))
    for path in matches:
        if path.is_file() and path.stat().st_size > 0:
            return path
    return None
