"""Pack schema. Continuity: shot N start_state must match shot N-1 end_state."""

from __future__ import annotations

import re

from pydantic import BaseModel, Field, field_validator, model_validator

from omarchy_imagine.config import (
    ASPECT_RATIOS,
    DEFAULT_DURATION_SEC,
    MAX_DURATION_SEC,
    MIN_DURATION_SEC,
    VIDEO_RESOLUTIONS,
)

_SHOT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

BIBLE_HEADER = "Look bible (locked across every shot):"
_BIBLE_LABELS = ("Cast", "Wardrobe", "Palette", "Lighting", "Camera")


class LookBible(BaseModel):
    """Pack-wide lock for identity, clothes, color, light, and camera.

    Empty strings are allowed so a hand-written pack can omit the block.
    Plan and fill always write all five lines.
    """

    cast: str = ""
    wardrobe: str = ""
    palette: str = ""
    lighting: str = ""
    camera: str = ""

    @field_validator("cast", "wardrobe", "palette", "lighting", "camera")
    @classmethod
    def bible_text(cls, value: str) -> str:
        return value.strip()

    def prompt_block(self) -> str:
        lines = [
            f"{label}: {getattr(self, label.lower())}"
            for label in _BIBLE_LABELS
            if getattr(self, label.lower())
        ]
        if not lines:
            return ""
        return BIBLE_HEADER + "\n" + "\n".join(lines)


def render_look_bible(value: object) -> str:
    """Turn a stored bible (model, dict, or already-rendered block) into prompt text."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    try:
        bible = value if isinstance(value, LookBible) else LookBible.model_validate(value)
    except ValueError:
        return ""
    return bible.prompt_block()


class Shot(BaseModel):
    id: str
    prompt_still: str
    prompt_motion: str
    duration_sec: int = DEFAULT_DURATION_SEC
    end_state: str = ""
    start_state: str = ""

    @field_validator("id")
    @classmethod
    def shot_id(cls, value: str) -> str:
        cleaned = value.strip()
        if not _SHOT_ID.fullmatch(cleaned):
            raise ValueError("id must be a filename-safe token (letters, numbers, _, -)")
        return cleaned

    @field_validator("prompt_still", "prompt_motion")
    @classmethod
    def required_prompt(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("must not be empty")
        return cleaned

    @field_validator("end_state", "start_state")
    @classmethod
    def state_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("duration_sec")
    @classmethod
    def duration(cls, value: int) -> int:
        if value < MIN_DURATION_SEC or value > MAX_DURATION_SEC:
            raise ValueError(
                f"duration_sec must be between {MIN_DURATION_SEC} and {MAX_DURATION_SEC}"
            )
        return value


class PackIn(BaseModel):
    title: str
    logline: str = ""
    aspect_ratio: str = "16:9"
    resolution: str = "720p"
    look_bible: LookBible = Field(default_factory=LookBible)
    shots: list[Shot] = Field(min_length=1)

    @field_validator("title")
    @classmethod
    def title_text(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("title must not be empty")
        return cleaned

    @field_validator("logline")
    @classmethod
    def logline_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("aspect_ratio")
    @classmethod
    def aspect(cls, value: str) -> str:
        cleaned = value.strip()
        if cleaned not in ASPECT_RATIOS:
            allowed = ", ".join(ASPECT_RATIOS)
            raise ValueError(f"aspect_ratio must be one of: {allowed}")
        return cleaned

    @field_validator("resolution")
    @classmethod
    def resolution_value(cls, value: str) -> str:
        cleaned = value.strip()
        if cleaned not in VIDEO_RESOLUTIONS:
            allowed = ", ".join(VIDEO_RESOLUTIONS)
            raise ValueError(f"resolution must be one of: {allowed}")
        return cleaned

    @model_validator(mode="after")
    def continuity(self) -> PackIn:
        ids = [shot.id for shot in self.shots]
        if len(ids) != len(set(ids)):
            raise ValueError("shot ids must be unique")
        for index in range(1, len(self.shots)):
            previous = self.shots[index - 1]
            current = self.shots[index]
            both_present = bool(previous.end_state and current.start_state)
            mismatched = current.start_state != previous.end_state
            if both_present and mismatched:
                raise ValueError(
                    f"shots[{index}].start_state must equal shots[{index - 1}].end_state"
                )
        return self


class PackOut(PackIn):
    id: str
    created_at: str


class ModerationNote(BaseModel):
    """What a moderation retry changed on one shot. Gates are not part of this note."""

    retry_count: int = 0
    original_prompt_still: str = ""
    original_prompt_motion: str = ""
    softened_prompt_still: str = ""
    softened_prompt_motion: str = ""


class ShotStatus(BaseModel):
    id: str
    called_imagine_still: bool
    produced_still: bool
    called_imagine_video: bool
    produced_mp4: bool
    still_path: str | None = None
    clip_path: str | None = None
    last_frame_path: str | None = None
    still_mode: str | None = None
    video_request_id: str | None = None
    error: str | None = None
    moderation: ModerationNote | None = None


class JobOut(BaseModel):
    id: str
    pack_id: str
    status: str
    message: str | None = None
    error: str | None = None
    continuity_mode: str | None = None
    grade_match: bool = False
    called_imagine_still: bool
    produced_still: bool
    called_imagine_video: bool
    produced_mp4: bool
    stitched_episode: bool
    episode_path: str | None = None
    shots: list[ShotStatus]
    created_at: str
    updated_at: str


class JobsOut(BaseModel):
    jobs: list[JobOut]


class RunOut(BaseModel):
    job_id: str
    pack_id: str
    status: str
