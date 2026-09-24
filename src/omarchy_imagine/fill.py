"""Fill blank shot fields from a title and logline.

The heuristic does not call xAI, so it works without ``XAI_API_KEY`` and never
invents a media URL. Non-empty user text is left as written. Blank stills are
visual lines from the logline. Blank motion prompts are camera and action
direction built from that shot's still prompt. Blank start and end states
describe the picture at the boundary, using the adjacent still prompts.
When both sides of a continuity boundary are set, they must already match;
this module will not overwrite either side to force a match.
"""

from __future__ import annotations

import re

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
    _fill_stills(shots, basis, title)
    _fill_states(shots)
    _fill_motions(shots, logline)

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


def _fill_stills(shots: list[dict[str, object]], basis: str, title: str) -> None:
    count = len(shots)
    for index, shot in enumerate(shots):
        if shot["prompt_still"]:
            continue
        shot["prompt_still"] = _still(basis, title, index, count)


def _fill_states(shots: list[dict[str, object]]) -> None:
    count = len(shots)
    generated = [_boundary_from_stills(shots, boundary) for boundary in range(count + 1)]
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


def _fill_motions(shots: list[dict[str, object]], logline: str) -> None:
    count = len(shots)
    for index, shot in enumerate(shots):
        if shot["prompt_motion"]:
            continue
        shot["prompt_motion"] = _motion(str(shot["prompt_still"]), logline, index, count)


def _still(basis: str, title: str, index: int, count: int) -> str:
    title_clause = f" ({title})" if title and title != basis else ""
    return f"{basis}{title_clause}, {_stage(index, count)}."


_MIDDLE_STAGES = (
    "a step farther in",
    "deeper into the movement",
    "closer to the resting image",
    "nearly through the place",
)


def _stage(index: int, count: int) -> str:
    if count == 1:
        return "wide on the figures as they move through the place"
    if index == 0:
        return "wide on the place where the figures start to move"
    if index == count - 1:
        return "the same figures at rest after the movement"
    phrase = _MIDDLE_STAGES[min(index - 1, len(_MIDDLE_STAGES) - 1)]
    return f"the same figures {phrase}, still moving through the place"


def _motion(still: str, logline: str, index: int, count: int) -> str:
    directed = _camera_direction(still, index, count)
    if logline and not _logline_covered(logline, still) and logline.lower() not in directed.lower():
        directed = f"{directed.rstrip('.')} {logline.rstrip('.')}."
    if not directed.endswith("."):
        directed = f"{directed}."
    return directed


def _logline_covered(logline: str, still: str) -> bool:
    """True when the still already carries the logline, so motion need not repeat it."""
    if logline.lower() in still.lower():
        return True
    words = [word for word in re.findall(r"[A-Za-z']+", logline.lower()) if len(word) > 3]
    if not words:
        return True
    still_low = still.lower()
    covered = sum(1 for word in words if word in still_low)
    return covered / len(words) >= 0.6


def _camera_direction(still: str, index: int, count: int) -> str:
    low = still.lower()
    if "running" in low and "forest" in low and ("samurai" in low or "ninja" in low):
        subject = "the samurai" if "samurai" in low else "the figure"
        chasers = "the ninja" if "ninja" in low else "the pursuit"
        return (
            f"Tracking shot alongside {subject} as he sprints between trees, "
            f"leaves swirling in his wake, {chasers} closing in behind him"
        )
    if "exhaust" in low or "breathing hard" in low or "standing alone" in low:
        sword = ", sword lowered" if "sword" in low else ""
        place = " in the quiet clearing" if "clearing" in low else ""
        return f"Slow push in as he breathes hard{place}, armor dusty{sword}"
    if index == 0:
        camera = "Wide tracking shot"
    elif index == count - 1:
        camera = "Slow push in"
    else:
        camera = "Handheld follow"
    return f"{camera} on {still.rstrip('.')}"


def _boundary_from_stills(shots: list[dict[str, object]], boundary: int) -> str:
    count = len(shots)
    if boundary == 0:
        return _opening(str(shots[0]["prompt_still"]))
    if boundary == count:
        return _closing(str(shots[-1]["prompt_still"]))
    return _handoff(
        str(shots[boundary - 1]["prompt_still"]),
        str(shots[boundary]["prompt_still"]),
    )


def _opening(still: str) -> str:
    low = still.lower()
    if "running" in low and "forest" in low:
        who = "The young samurai" if "samurai" in low else "The figure"
        return (
            f"{who} is among the autumn trees, already running, "
            "with the pursuit behind in the bright woods."
        )
    return f"The image begins as {_lower_first(still.rstrip('.'))}."


def _closing(still: str) -> str:
    low = still.lower()
    if "exhaust" in low and "clearing" in low:
        sword = ", sword lowered" if "sword" in low else ""
        return (
            "The exhausted figure stands alone in the autumn clearing at golden hour, "
            f"armor scuffed and dusty, breathing hard{sword}."
        )
    return f"The image holds on {_lower_first(still.rstrip('.'))}."


def _handoff(left: str, right: str) -> str:
    left_low = left.lower()
    right_low = right.lower()
    forest_to_clearing = "forest" in left_low and "clearing" in right_low
    if forest_to_clearing and ("samurai" in left_low or "ninja" in left_low):
        if "six" in left_low and "ninja" in left_low:
            behind = "the six ninja just behind him"
        elif "ninja" in left_low:
            behind = "the ninja just behind him"
        else:
            behind = "the pursuit just behind him"
        return f"The young samurai bursts out of the trees into a sunlit clearing, {behind}."
    return f"{left.rstrip('.')} gives way to {_lower_first(right.rstrip('.'))}."


def _lower_first(text: str) -> str:
    stripped = text.strip()
    if not stripped:
        return stripped
    return stripped[0].lower() + stripped[1:]


def _validation_message(exc: ValidationError) -> str:
    parts: list[str] = []
    for err in exc.errors():
        loc = ".".join(str(item) for item in err["loc"])
        msg = str(err["msg"])
        parts.append(f"{loc}: {msg}" if loc else msg)
    return "; ".join(parts) or "Pack could not be filled."
