"""Turn one story prompt and a target length into a pack draft.

The draft is a ``PackIn``. Nothing is saved, and Imagine is not called.
Shot count and per-shot durations are computed here. With ``XAI_API_KEY`` set,
prose comes from the text model (chat completions, strict JSON schema) and is
validated into the pack model. Without a key, the same math drives the
fill-blanks heuristic. Either path writes a beat map and a camera card per
shot, softens violent wording, and does not add media URLs.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Protocol

import httpx
from pydantic import BaseModel, Field, ValidationError, field_validator

from omarchy_imagine.config import (
    ASPECT_RATIOS,
    DEFAULT_TEXT_TIMEOUT_SEC,
    MAX_DURATION_SEC,
    MAX_PLAN_SHOTS,
    MAX_PLAN_TARGET_SEC,
    MIN_DURATION_SEC,
    MIN_PLAN_SHOTS,
    MIN_PLAN_TARGET_SEC,
    PREFERRED_SHOT_SEC,
    TEXT_MODEL,
    VIDEO_RESOLUTIONS,
    XAI_API_BASE,
)
from omarchy_imagine.craft import (
    ShotGrammar,
    apply_heuristic_craft,
    beat_map_lines,
    grammar_for,
    infer_style,
    lens_line,
    lock_model_craft,
)
from omarchy_imagine.fill import FillError, FillIn, build_look_bible, fill_pack
from omarchy_imagine.moderate import soften_wording
from omarchy_imagine.schema import (
    BEAT_ROLES,
    CAMERA_ANGLES,
    CAMERA_MOVES,
    CAMERA_SCALES,
    STYLE_PRESETS,
    LookBible,
    PackIn,
    coerce_token,
)

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


class PlanError(ValueError):
    """The brief cannot be turned into a valid pack draft."""


class PlanUpstreamError(PlanError):
    """The text model call failed. The brief itself may still be valid."""


class PlanIn(BaseModel):
    prompt: str
    target_duration_sec: int
    aspect_ratio: str | None = None
    resolution: str | None = None
    title: str | None = None
    style_preset: str | None = None

    @field_validator("prompt")
    @classmethod
    def prompt_text(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Add a story prompt before planning.")
        return cleaned

    @field_validator("title")
    @classmethod
    def title_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None

    @field_validator("target_duration_sec")
    @classmethod
    def target(cls, value: int) -> int:
        if value < MIN_PLAN_TARGET_SEC or value > MAX_PLAN_TARGET_SEC:
            raise ValueError(
                "target_duration_sec must be an integer from "
                f"{MIN_PLAN_TARGET_SEC} to {MAX_PLAN_TARGET_SEC} seconds"
            )
        return value

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

    @field_validator("style_preset")
    @classmethod
    def style_value(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            return None
        return coerce_token(cleaned, STYLE_PRESETS, {}, "style_preset", allow_empty=False)


@dataclass(frozen=True)
class PlanBrief:
    prompt: str
    title: str
    aspect_ratio: str
    resolution: str
    durations: tuple[int, ...]
    style_preset: str
    grammar: tuple[ShotGrammar, ...]


class StoryCamera(BaseModel):
    """Loose camera card from the text model. The server replaces invalid enums."""

    scale: str = ""
    angle: str = ""
    move: str = ""
    exit_frame: str = ""

    @field_validator("scale", "angle", "move", "exit_frame")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()


class StoryBeatDraft(BaseModel):
    role: str = ""
    summary: str = ""

    @field_validator("role", "summary")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()


class StoryShot(BaseModel):
    prompt_still: str
    prompt_motion: str
    end_state: str
    beat: str = ""
    camera: StoryCamera = Field(default_factory=StoryCamera)

    @field_validator("prompt_still", "prompt_motion", "end_state", "beat")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()


class StoryDraft(BaseModel):
    title: str = ""
    logline: str = ""
    opening_state: str = ""
    style_preset: str = ""
    beat_map: list[StoryBeatDraft] = Field(default_factory=list)
    look_bible: LookBible = Field(default_factory=LookBible)
    shots: list[StoryShot]

    @field_validator("title", "logline", "opening_state", "style_preset")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()


class TextPlanner(Protocol):
    def plan_story(self, brief: PlanBrief) -> StoryDraft:
        """Return stills, motion, and end states. Durations are ignored."""


def shot_count_for(target_sec: int) -> int:
    """Nearest shot count to ``target_sec / 8``. Halves round up. Clamped to 2–8.

    ``20`` is halfway between 2 and 3 shots of 8 seconds, so it becomes 3.
    ``12`` becomes 2. Counts above 8 are capped so a long brief stays affordable.
    """
    quotient, remainder = divmod(target_sec, PREFERRED_SHOT_SEC)
    if remainder * 2 >= PREFERRED_SHOT_SEC:
        quotient += 1
    if quotient < MIN_PLAN_SHOTS:
        return MIN_PLAN_SHOTS
    if quotient > MAX_PLAN_SHOTS:
        return MAX_PLAN_SHOTS
    return quotient


def split_durations(target_sec: int, shot_count: int) -> list[int]:
    """Split ``target_sec`` across ``shot_count`` clips, each from 1 to 15 seconds.

    The split is as even as possible. Earlier shots receive the leftover seconds.
    When the target cannot be met inside the clamp, the result is the closest
    feasible sum (every clip pushed to the near boundary).
    """
    if shot_count < 1:
        raise ValueError("shot_count must be at least 1")
    low = MIN_DURATION_SEC * shot_count
    high = MAX_DURATION_SEC * shot_count
    goal = min(max(target_sec, low), high)
    base, extra = divmod(goal, shot_count)
    return [base + (1 if index < extra else 0) for index in range(shot_count)]


def plan_pack(body: PlanIn, planner: TextPlanner | None = None) -> PackIn:
    """Return a full pack draft for ``POST /api/packs``. Does not save or render."""
    aspect = body.aspect_ratio or "16:9"
    resolution = body.resolution or "720p"
    durations = split_durations(body.target_duration_sec, shot_count_for(body.target_duration_sec))
    style = body.style_preset or infer_style(body.prompt)
    grammar = tuple(grammar_for(len(durations), style))
    if planner is None:
        return _heuristic_pack(body, durations, aspect, resolution, style)
    brief = PlanBrief(
        prompt=body.prompt,
        title=body.title or "",
        aspect_ratio=aspect,
        resolution=resolution,
        durations=tuple(durations),
        style_preset=style,
        grammar=grammar,
    )
    story = planner.plan_story(brief)
    return _pack_from_story(body, durations, story, aspect, resolution, style)


class XAITextPlanner:
    """One-shot chat completion against the pinned text model.

    Structured output follows the current xAI docs: ``POST /v1/chat/completions``
    with ``response_format.type`` of ``json_schema`` and ``strict: true``.
    Grok 4.6 accepts that API. The call is stateless. The key is the
    ``Authorization`` header only.
    """

    def __init__(
        self,
        api_key: str,
        *,
        http_client: httpx.Client | None = None,
        base_url: str | None = None,
        model: str | None = None,
    ) -> None:
        cleaned = api_key.strip()
        if not cleaned:
            raise ValueError("XAITextPlanner requires an API key")
        self.api_key = cleaned
        chosen = (model or os.environ.get("XAI_TEXT_MODEL", TEXT_MODEL)).strip()
        self.model = chosen or TEXT_MODEL
        if base_url is not None:
            configured = base_url
        else:
            configured = os.environ.get("XAI_API_BASE", XAI_API_BASE)
        self.base_url = configured.rstrip("/")
        self._owns_http = http_client is None
        self._http = http_client or httpx.Client(
            timeout=httpx.Timeout(DEFAULT_TEXT_TIMEOUT_SEC, connect=30.0),
        )

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def plan_story(self, brief: PlanBrief) -> StoryDraft:
        schema = _story_schema(len(brief.durations))
        body = {
            "model": self.model,
            "messages": _messages(brief),
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "director_pack",
                    "strict": True,
                    "schema": schema,
                },
            },
        }
        try:
            response = self._http.post(
                f"{self.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=body,
            )
        except httpx.HTTPError as exc:
            raise PlanUpstreamError(
                "The text model request failed before a response: "
                f"{exc.__class__.__name__}"
            ) from exc
        if response.status_code >= 400:
            raise PlanUpstreamError(
                f"The text model returned HTTP {response.status_code}: {_error_text(response)}"
            )
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise PlanUpstreamError("The text model response was not JSON.") from exc
        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise PlanUpstreamError("The text model response had no message content.") from exc
        return _parse_story(content)


def _heuristic_pack(
    body: PlanIn,
    durations: list[int],
    aspect: str,
    resolution: str,
    style: str,
) -> PackIn:
    prompt = soften_wording(body.prompt)
    supplied = soften_wording(body.title) if body.title else ""
    # Fill compares title to the logline with a raw string check. A title taken
    # from the first sentence, or a logline that still has its period, would be
    # copied into every still as a parenthetical. Strip that punctuation for the
    # fill, then put the real title and the original prompt back on the draft.
    basis = prompt.strip().rstrip(".!?").strip() or prompt
    fill_title = supplied or basis
    try:
        filled = fill_pack(
            FillIn(
                title=fill_title,
                logline=basis,
                aspect_ratio=aspect,
                resolution=resolution,
                shot_count=len(durations),
            )
        )
    except FillError as exc:
        raise PlanError(str(exc)) from exc
    draft = filled.model_dump()
    draft["logline"] = prompt
    if not supplied:
        draft["title"] = _title_from_prompt(prompt)
    for shot, duration in zip(draft["shots"], durations, strict=True):
        shot["duration_sec"] = duration
    apply_heuristic_craft(draft, style, prompt)
    _soften_and_lock(draft)
    return _validate_pack(draft)


def _pack_from_story(
    body: PlanIn,
    durations: list[int],
    story: StoryDraft,
    aspect: str,
    resolution: str,
    style: str,
) -> PackIn:
    expected = len(durations)
    if len(story.shots) != expected:
        raise PlanError(
            f"The text model returned {len(story.shots)} shots; the plan needs {expected}."
        )
    prompt = soften_wording(body.prompt)
    title_source = body.title or story.title or _title_from_prompt(prompt)
    title = soften_wording(title_source) or _title_from_prompt(prompt)
    logline = soften_wording(story.logline or body.prompt)
    opening = soften_wording(story.opening_state)
    shots: list[dict[str, object]] = []
    for index, (item, duration) in enumerate(zip(story.shots, durations, strict=True)):
        still = soften_wording(item.prompt_still)
        motion = soften_wording(item.prompt_motion)
        end_state = soften_wording(item.end_state)
        if not still or not motion or not end_state:
            raise PlanError(
                f"Shot {index + 1} is missing a still prompt, a motion prompt, or an end state."
            )
        shots.append(
            {
                "id": f"s{index + 1:02d}",
                "prompt_still": still,
                "prompt_motion": motion,
                "duration_sec": duration,
                "start_state": "",
                "end_state": end_state,
            }
        )
    if not opening:
        opening = f"The image begins as {_lower_first(str(shots[0]['prompt_still']).rstrip('.'))}."
    shots[0]["start_state"] = opening
    for index in range(1, len(shots)):
        shots[index]["start_state"] = shots[index - 1]["end_state"]
    bible = complete_look_bible(story.look_bible, title, logline, style)
    model_beats = [
        (item.role.lower().replace(" ", "_").replace("-", "_"), soften_wording(item.summary))
        for item in story.beat_map
    ]
    model_exits = [soften_wording(item.camera.exit_frame) for item in story.shots]
    beat_map = lock_model_craft(
        shots,
        style=style,
        prompt=prompt,
        model_beat_map=model_beats,
        model_exits=model_exits,
    )
    for item in beat_map:
        item["summary"] = soften_wording(item["summary"])
    return _validate_pack(
        {
            "title": title,
            "logline": logline,
            "aspect_ratio": aspect,
            "resolution": resolution,
            "style_preset": style,
            "beat_map": beat_map,
            "look_bible": bible.model_dump(),
            "shots": shots,
        }
    )


def complete_look_bible(
    bible: LookBible,
    title: str,
    logline: str,
    style: str = "generic",
) -> LookBible:
    """Keep model lines that are set. Fill blanks from the heuristic, then soften."""
    fallback = build_look_bible(title, logline).model_dump()
    fallback["camera"] = lens_line(style)
    merged: dict[str, str] = {}
    for key, value in bible.model_dump().items():
        chosen = str(value).strip() or str(fallback[key])
        merged[key] = soften_wording(chosen)
    return LookBible.model_validate(merged)


def _soften_and_lock(draft: dict[str, object]) -> None:
    draft["title"] = soften_wording(str(draft["title"]))
    draft["logline"] = soften_wording(str(draft["logline"]))
    bible = draft.get("look_bible")
    if isinstance(bible, dict):
        for key in ("cast", "wardrobe", "palette", "lighting", "camera"):
            bible[key] = soften_wording(str(bible.get(key, "")))
    beat_map = draft.get("beat_map")
    if isinstance(beat_map, list):
        for item in beat_map:
            if isinstance(item, dict):
                item["summary"] = soften_wording(str(item.get("summary", "")))
    shots = draft["shots"]
    if not isinstance(shots, list):
        return
    for shot in shots:
        shot["prompt_still"] = soften_wording(str(shot["prompt_still"]))
        shot["prompt_motion"] = soften_wording(str(shot["prompt_motion"]))
        shot["start_state"] = soften_wording(str(shot["start_state"]))
        shot["end_state"] = soften_wording(str(shot["end_state"]))
        camera = shot.get("camera")
        if isinstance(camera, dict):
            camera["exit_frame"] = soften_wording(str(camera.get("exit_frame", "")))
            if camera["exit_frame"] != shot["end_state"] and shot["end_state"]:
                # Heuristic packs set these equal. Keep them equal after softening.
                camera["exit_frame"] = shot["end_state"]
    for index in range(1, len(shots)):
        shots[index]["start_state"] = shots[index - 1]["end_state"]


def _validate_pack(draft: dict[str, object]) -> PackIn:
    try:
        return PackIn.model_validate(draft)
    except ValidationError as exc:
        raise PlanError(_validation_message(exc)) from exc


def _title_from_prompt(prompt: str) -> str:
    text = prompt.strip()
    match = re.search(r"[.!?]", text)
    if match and match.start() > 0:
        text = text[: match.start()]
    text = re.sub(r"\s+", " ", text).strip(" \"'")
    if len(text) > 80:
        text = text[:77].rstrip() + "..."
    return text or "Untitled"


def _lower_first(text: str) -> str:
    stripped = text.strip()
    if not stripped:
        return stripped
    return stripped[0].lower() + stripped[1:]


def _messages(brief: PlanBrief) -> list[dict[str, str]]:
    durations = ", ".join(str(item) for item in brief.durations)
    title_line = (
        f"Use this title: {brief.title}"
        if brief.title
        else "Invent a short title from the story."
    )
    card_lines = [
        (
            f"Shot {index} ({duration}s): beat={card.beat}, scale={card.scale}, "
            f"angle={card.angle}, move={card.move}."
        )
        for index, (duration, card) in enumerate(
            zip(brief.durations, brief.grammar, strict=True),
            start=1,
        )
    ]
    seen: list[str] = []
    for card in brief.grammar:
        if card.beat not in seen:
            seen.append(card.beat)
    beat_lines = [
        f"- {role}: {summary}"
        for role, summary in beat_map_lines(brief.prompt, seen, brief.style_preset)
    ]
    user = "\n".join(
        [
            f"Story: {brief.prompt}",
            title_line,
            f"Style preset: {brief.style_preset}",
            f"Shot count: {len(brief.durations)}",
            f"Clip durations in seconds, in order: {durations}",
            f"Aspect ratio: {brief.aspect_ratio}",
            f"Resolution: {brief.resolution}",
            "Beat map:",
            *beat_lines,
            "Camera cards. Write each still and motion to match its card.",
            *card_lines,
            "Write one continuous chain. The end of each shot is the start of the next.",
            "Put the exit frame in camera.exit_frame and match it with end_state.",
        ]
    )
    return [
        {
            "role": "system",
            "content": (
                "You plan a short film for Grok Imagine. Expand the story into "
                "exactly the requested number of shots. Follow the beat map and the "
                "camera card for each shot. Do not change the shot count or the durations. "
                "Story shape is setup, then turn, then climax, then button. Not equal filler. "
                "Each shot has one want and one obstacle. Escalate or reverse the emotion. "
                "The last shot is a readable button. "
                "prompt_still is one locked frame: who, wardrobe, pose, space, and light. "
                "No camera move in the still. "
                "prompt_motion is only what changes, led by a verb, plus the one camera move "
                "on the card. Do not write a mood essay. "
                "look_bible locks the whole film: cast is the same face and body, "
                "wardrobe is the same clothes, palette is the colors, lighting is the "
                "key light, and camera is the film stock and lens for the style preset. "
                "Repeat those anchors lightly in each still and each motion line. "
                "Use the scale, angle, and single move given for that shot. "
                "Alternate scale across the pack. Use a Dutch angle only when the card says dutch. "
                "exit_frame names the picture the next shot must open on, so a last-frame edit "
                "can start clean. end_state matches that exit frame. "
                "opening_state is the picture at the start of the first shot. "
                "Keep the chain continuous. Write family-safe prose: no blood, gore, "
                "death, or injury. A clash is bloodless choreography that ends in "
                "exhaustion and victory. Do not include URLs."
            ),
        },
        {"role": "user", "content": user},
    ]


def _story_schema(shot_count: int) -> dict[str, object]:
    camera = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "scale": {"type": "string", "enum": list(CAMERA_SCALES)},
            "angle": {"type": "string", "enum": list(CAMERA_ANGLES)},
            "move": {"type": "string", "enum": list(CAMERA_MOVES)},
            "exit_frame": {"type": "string"},
        },
        "required": ["scale", "angle", "move", "exit_frame"],
    }
    shot = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "prompt_still": {"type": "string"},
            "prompt_motion": {"type": "string"},
            "end_state": {"type": "string"},
            "beat": {"type": "string", "enum": list(BEAT_ROLES)},
            "camera": camera,
        },
        "required": ["prompt_still", "prompt_motion", "end_state", "beat", "camera"],
    }
    beat = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "role": {"type": "string", "enum": list(BEAT_ROLES)},
            "summary": {"type": "string"},
        },
        "required": ["role", "summary"],
    }
    bible = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "cast": {"type": "string"},
            "wardrobe": {"type": "string"},
            "palette": {"type": "string"},
            "lighting": {"type": "string"},
            "camera": {"type": "string"},
        },
        "required": ["cast", "wardrobe", "palette", "lighting", "camera"],
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "title": {"type": "string"},
            "logline": {"type": "string"},
            "opening_state": {"type": "string"},
            "style_preset": {"type": "string", "enum": list(STYLE_PRESETS)},
            "beat_map": {
                "type": "array",
                "minItems": 1,
                "maxItems": 4,
                "items": beat,
            },
            "look_bible": bible,
            "shots": {
                "type": "array",
                "minItems": shot_count,
                "maxItems": shot_count,
                "items": shot,
            },
        },
        "required": [
            "title",
            "logline",
            "opening_state",
            "style_preset",
            "beat_map",
            "look_bible",
            "shots",
        ],
    }


def _parse_story(content: object) -> StoryDraft:
    text = _content_text(content)
    if not text:
        raise PlanError("The text model returned an empty pack.")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PlanError("The text model did not return JSON.") from exc
    try:
        return StoryDraft.model_validate(payload)
    except ValidationError as exc:
        raise PlanError(
            "The text model JSON did not match the pack schema. "
            + _validation_message(exc)
        ) from exc


def _content_text(content: object) -> str:
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                value = item.get("text") or item.get("content") or ""
                parts.append(str(value))
        text = "".join(parts)
    else:
        return ""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = _FENCE.sub("", stripped).strip()
    return stripped


def _error_text(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except json.JSONDecodeError:
        return response.text[:500]
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])[:500]
        if isinstance(err, str):
            return err[:500]
        if payload.get("message"):
            return str(payload["message"])[:500]
    return response.text[:500]


def _validation_message(exc: ValidationError) -> str:
    parts: list[str] = []
    for err in exc.errors():
        loc = ".".join(str(item) for item in err["loc"])
        msg = str(err["msg"])
        parts.append(f"{loc}: {msg}" if loc else msg)
    return "; ".join(parts) or "Pack could not be planned."
