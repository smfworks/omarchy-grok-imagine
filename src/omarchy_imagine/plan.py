"""Turn one story prompt and a target length into a pack draft.

The draft is a ``PackIn``. Nothing is saved, and Imagine is not called.
Shot count and per-shot durations are computed here. With ``XAI_API_KEY`` set,
prose comes from the text model (chat completions, strict JSON schema) and is
validated into the pack model. Without a key, the same math drives the
fill-blanks heuristic. Either path softens violent wording before the draft
is returned.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Protocol

import httpx
from pydantic import BaseModel, ValidationError, field_validator

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
from omarchy_imagine.fill import FillError, FillIn, fill_pack
from omarchy_imagine.moderate import soften_wording
from omarchy_imagine.schema import PackIn

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


@dataclass(frozen=True)
class PlanBrief:
    prompt: str
    title: str
    aspect_ratio: str
    resolution: str
    durations: tuple[int, ...]


class StoryShot(BaseModel):
    prompt_still: str
    prompt_motion: str
    end_state: str

    @field_validator("prompt_still", "prompt_motion", "end_state")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()


class StoryDraft(BaseModel):
    title: str = ""
    logline: str = ""
    opening_state: str = ""
    shots: list[StoryShot]

    @field_validator("title", "logline", "opening_state")
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
    if planner is None:
        return _heuristic_pack(body, durations, aspect, resolution)
    brief = PlanBrief(
        prompt=body.prompt,
        title=body.title or "",
        aspect_ratio=aspect,
        resolution=resolution,
        durations=tuple(durations),
    )
    story = planner.plan_story(brief)
    return _pack_from_story(body, durations, story, aspect, resolution)


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
) -> PackIn:
    prompt = soften_wording(body.prompt)
    title = soften_wording(body.title) if body.title else _title_from_prompt(prompt)
    if not title:
        title = _title_from_prompt(prompt)
    try:
        filled = fill_pack(
            FillIn(
                title=title,
                logline=prompt,
                aspect_ratio=aspect,
                resolution=resolution,
                shot_count=len(durations),
            )
        )
    except FillError as exc:
        raise PlanError(str(exc)) from exc
    draft = filled.model_dump()
    for shot, duration in zip(draft["shots"], durations, strict=True):
        shot["duration_sec"] = duration
    _soften_and_lock(draft)
    return _validate_pack(draft)


def _pack_from_story(
    body: PlanIn,
    durations: list[int],
    story: StoryDraft,
    aspect: str,
    resolution: str,
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
    return _validate_pack(
        {
            "title": title,
            "logline": logline,
            "aspect_ratio": aspect,
            "resolution": resolution,
            "shots": shots,
        }
    )


def _soften_and_lock(draft: dict[str, object]) -> None:
    draft["title"] = soften_wording(str(draft["title"]))
    draft["logline"] = soften_wording(str(draft["logline"]))
    shots = draft["shots"]
    if not isinstance(shots, list):
        return
    for shot in shots:
        shot["prompt_still"] = soften_wording(str(shot["prompt_still"]))
        shot["prompt_motion"] = soften_wording(str(shot["prompt_motion"]))
        shot["start_state"] = soften_wording(str(shot["start_state"]))
        shot["end_state"] = soften_wording(str(shot["end_state"]))
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
    user = "\n".join(
        [
            f"Story: {brief.prompt}",
            title_line,
            f"Shot count: {len(brief.durations)}",
            f"Clip durations in seconds, in order: {durations}",
            f"Aspect ratio: {brief.aspect_ratio}",
            f"Resolution: {brief.resolution}",
            "Write one continuous chain. The end of each shot is the start of the next.",
        ]
    )
    return [
        {
            "role": "system",
            "content": (
                "You plan a short film for Grok Imagine. Expand the story into "
                "exactly the requested number of shots. prompt_still is one image: "
                "subject, place, light, and wardrobe, with no camera move. "
                "prompt_motion is how the camera and the subject move during that clip. "
                "end_state is one sentence that describes the picture at the end of the clip. "
                "opening_state is the picture at the start of the first shot. "
                "Keep the chain continuous. Write family-safe prose: no blood, gore, "
                "death, or injury. A clash is a choreographed duel that ends in "
                "exhaustion and victory. Do not include URLs or durations."
            ),
        },
        {"role": "user", "content": user},
    ]


def _story_schema(shot_count: int) -> dict[str, object]:
    shot = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "prompt_still": {"type": "string"},
            "prompt_motion": {"type": "string"},
            "end_state": {"type": "string"},
        },
        "required": ["prompt_still", "prompt_motion", "end_state"],
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "title": {"type": "string"},
            "logline": {"type": "string"},
            "opening_state": {"type": "string"},
            "shots": {
                "type": "array",
                "minItems": shot_count,
                "maxItems": shot_count,
                "items": shot,
            },
        },
        "required": ["title", "logline", "opening_state", "shots"],
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
