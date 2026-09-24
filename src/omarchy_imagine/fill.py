"""Fill blank shot fields from a title and logline.

Phase 1.5 uses a deterministic heuristic. It does not call xAI, so it works
without ``XAI_API_KEY`` and never invents a media URL. Non-empty user text is
left as written. When both sides of a continuity boundary are set, they must
already match; this module will not overwrite either side to force a match.
"""

from __future__ import annotations

from pydantic import BaseModel, ValidationError, field_validator

from omarchy_imagine.config import (
    ASPECT_RATIOS,
    DEFAULT_DURATION_SEC,
    MAX_DURATION_SEC,
    MIN_DURATION_SEC,
    VIDEO_RESOLUTIONS,
)
from omarchy_imagine.schema import PackIn

MAX_SHOTS = 12
DEFAULT_SHOT_COUNT = 2


class FillError(ValueError):
    """The partial pack cannot be filled into a valid draft."""


class ShotFill(BaseModel):
    id: str = ""
    prompt_still: str = ""
    prompt_motion: str = ""
    duration_sec: int | None = None
    end_state: str = ""
    start_state: str = ""

    @field_validator("id", "prompt_still", "prompt_motion", "end_state", "start_state")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("duration_sec")
    @classmethod
    def duration(cls, value: int | None) -> int | None:
        if value is None:
            return None
        if value < MIN_DURATION_SEC or value > MAX_DURATION_SEC:
            raise ValueError(
                f"duration_sec must be between {MIN_DURATION_SEC} and {MAX_DURATION_SEC}"
            )
        return value


class FillIn(BaseModel):
    title: str = ""
    logline: str = ""
    aspect_ratio: str | None = None
    resolution: str | None = None
    shot_count: int | None = None
    shots: list[ShotFill] | None = None

    @field_validator("title", "logline")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("aspect_ratio")
    @classmethod
    def aspect(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            return None
        if cleaned not in ASPECT_RATIOS:
            allowed = ", ".join(ASPECT_RATIOS)
            raise ValueError(f"aspect_ratio must be one of: {allowed}")
        return cleaned

    @field_validator("resolution")
    @classmethod
    def resolution_value(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            return None
        if cleaned not in VIDEO_RESOLUTIONS:
            allowed = ", ".join(VIDEO_RESOLUTIONS)
            raise ValueError(f"resolution must be one of: {allowed}")
        return cleaned

    @field_validator("shot_count")
    @classmethod
    def count(cls, value: int | None) -> int | None:
        if value is None:
            return None
        if value < 1 or value > MAX_SHOTS:
            raise ValueError(f"shot_count must be between 1 and {MAX_SHOTS}")
        return value


def fill_pack(body: FillIn) -> PackIn:
    """Return a pack draft with blank prompts and continuity states filled."""
    title = body.title
    logline = body.logline
    if not title and not logline:
        raise FillError("Add a title or a logline before filling blanks.")
    if not title:
        title = logline

    basis = logline or title
    shots = [_shot_dict(shot) for shot in _expand_shots(body)]
    _assign_ids(shots)
    _fill_states(shots, basis)
    _fill_prompts(shots, basis, title)

    draft = {
        "title": title,
        "logline": logline,
        "aspect_ratio": body.aspect_ratio or "16:9",
        "resolution": body.resolution or "720p",
        "shots": shots,
    }
    try:
        return PackIn.model_validate(draft)
    except ValidationError as exc:
        raise FillError(_validation_message(exc)) from exc


def _expand_shots(body: FillIn) -> list[ShotFill]:
    shots = list(body.shots or [])
    if len(shots) > MAX_SHOTS:
        raise FillError(f"A pack can include at most {MAX_SHOTS} shots.")
    target = body.shot_count if body.shot_count is not None else (len(shots) or DEFAULT_SHOT_COUNT)
    # A smaller shot_count does not drop shots the user already wrote.
    target = max(target, len(shots))
    while len(shots) < target:
        shots.append(ShotFill())
    return shots


def _shot_dict(shot: ShotFill) -> dict[str, object]:
    return {
        "id": shot.id,
        "prompt_still": shot.prompt_still,
        "prompt_motion": shot.prompt_motion,
        "duration_sec": shot.duration_sec
        if shot.duration_sec is not None
        else DEFAULT_DURATION_SEC,
        "end_state": shot.end_state,
        "start_state": shot.start_state,
    }


def _assign_ids(shots: list[dict[str, object]]) -> None:
    used = {str(shot["id"]) for shot in shots if shot["id"]}
    for index, shot in enumerate(shots):
        if shot["id"]:
            continue
        number = index + 1
        candidate = f"s{number:02d}"
        while candidate in used:
            number += 1
            candidate = f"s{number:02d}"
        shot["id"] = candidate
        used.add(candidate)


def _fill_states(shots: list[dict[str, object]], basis: str) -> None:
    count = len(shots)
    generated = [_boundary(basis, boundary, count) for boundary in range(count + 1)]
    locked: list[str | None] = [None] * (count + 1)

    first_start = str(shots[0]["start_state"])
    last_end = str(shots[-1]["end_state"])
    if first_start:
        locked[0] = first_start
    if last_end:
        locked[count] = last_end

    for index in range(1, count):
        previous_end = str(shots[index - 1]["end_state"])
        current_start = str(shots[index]["start_state"])
        if previous_end and current_start and previous_end != current_start:
            raise FillError(
                f"shots[{index}].start_state must equal shots[{index - 1}].end_state. "
                "Non-empty text was left unchanged."
            )
        if previous_end:
            locked[index] = previous_end
        elif current_start:
            locked[index] = current_start

    for index, shot in enumerate(shots):
        if not shot["start_state"]:
            shot["start_state"] = locked[index] or generated[index]
        if not shot["end_state"]:
            shot["end_state"] = locked[index + 1] or generated[index + 1]


def _fill_prompts(shots: list[dict[str, object]], basis: str, title: str) -> None:
    count = len(shots)
    for index, shot in enumerate(shots):
        start = str(shot["start_state"])
        end = str(shot["end_state"])
        if not shot["prompt_still"]:
            shot["prompt_still"] = _still(basis, title, index, count, start, end)
        if not shot["prompt_motion"]:
            shot["prompt_motion"] = _motion(basis, index, count, start, end)


def _role(index: int, count: int) -> str:
    if count == 1:
        return "A single shot holds the whole story."
    if index == 0:
        return f"Establishing shot, 1 of {count}."
    if index == count - 1:
        return f"Closing shot, {index + 1} of {count}."
    return f"Continuing shot, {index + 1} of {count}."


def _still(basis: str, title: str, index: int, count: int, start: str, end: str) -> str:
    title_clause = f" Title: {title}." if title and title != basis else ""
    role = _role(index, count)
    return (
        f"{basis}{title_clause} {role} "
        f"The frame opens on: {start} "
        f"The frame is headed toward: {end}"
    )


def _motion(basis: str, index: int, count: int, start: str, end: str) -> str:
    if count == 1:
        move = "Play the story in one continuous move."
    elif index == 0:
        move = "Begin the story and carry it into the next beat."
    elif index == count - 1:
        move = "Bring the story to its close."
    else:
        move = "Carry the story forward."
    return f"{move} {basis} Start locked to: {start} End locked to: {end}"


def _boundary(basis: str, boundary: int, count: int) -> str:
    if boundary == 0:
        return f"At the start, before anything moves: {basis}"
    if boundary == count:
        return f"At the end, the story has landed: {basis}"
    if boundary == 1 and count > 2:
        return f"After the opening, the story is in motion: {basis}"
    if boundary == count - 1:
        return f"One beat from the end: {basis}"
    return f"After beat {boundary} of {count}, the story has advanced: {basis}"


def _validation_message(exc: ValidationError) -> str:
    parts: list[str] = []
    for err in exc.errors():
        loc = ".".join(str(item) for item in err["loc"])
        msg = str(err["msg"])
        parts.append(f"{loc}: {msg}" if loc else msg)
    return "; ".join(parts) or "Pack could not be filled."
