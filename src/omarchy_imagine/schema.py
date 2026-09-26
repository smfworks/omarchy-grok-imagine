"""Pack schema. Continuity: shot N start_state must match shot N-1 end_state."""

from __future__ import annotations

import re

from pydantic import BaseModel, Field, field_validator, model_validator

from omarchy_imagine.config import (
    ASPECT_RATIOS,
    CAST_ROLES,
    DEFAULT_DURATION_SEC,
    EXTEND_MAX_SEC,
    EXTEND_MIN_SEC,
    MAX_CAST_ENTRIES,
    MAX_DURATION_SEC,
    MIN_DURATION_SEC,
    VIDEO_MODES,
    VIDEO_RESOLUTIONS,
)

_SHOT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

BIBLE_HEADER = "Look bible (locked across every shot):"
_BIBLE_LABELS = ("Cast", "Wardrobe", "Palette", "Lighting", "Camera")

BEAT_ROLES = ("setup", "turn", "climax", "button")
STYLE_PRESETS = ("generic", "action_duel", "quiet_drama", "trek", "chase")
CAMERA_SCALES = ("wide", "medium", "close", "extreme_close")
CAMERA_ANGLES = ("eye", "low", "high", "ots", "dutch")
CAMERA_MOVES = (
    "static",
    "dolly_in",
    "dolly_out",
    "orbit",
    "pan",
    "tilt",
    "whip_pan",
    "handheld",
)

_SCALE_ALIASES = {
    "wide_shot": "wide",
    "medium_shot": "medium",
    "close_up": "close",
    "closeup": "close",
    "extreme_close_up": "extreme_close",
    "ecu": "extreme_close",
}
_ANGLE_ALIASES = {
    "eye_level": "eye",
    "eyelevel": "eye",
    "high_angle": "high",
    "low_angle": "low",
    "dutch_angle": "dutch",
    "over_the_shoulder": "ots",
    "over_shoulder": "ots",
}
_MOVE_ALIASES = {
    "locked": "static",
    "lock_off": "static",
    "push_in": "dolly_in",
    "push_out": "dolly_out",
    "whip": "whip_pan",
    "hand_held": "handheld",
}

# Screen position, left to right. Depth is near to far. Travel may be static
# while facing and look point somewhere else (a torso twist, not a reversal).
SCREEN_X = (
    "offscreen_left",
    "left_edge",
    "left_third",
    "center",
    "right_third",
    "right_edge",
    "offscreen_right",
)
DEPTHS = ("foreground", "mid", "background", "far")
FACINGS = ("screen_left", "screen_right", "toward_camera", "away_from_camera")
TRAVELS = (*FACINGS, "static")
CAMERA_SIDES = ("same", "on_axis", "cross")
ENTITY_KINDS = ("character", "group", "prop", "location")
STAGE_RELATIONS = ("behind", "ahead", "beside", "facing", "surrounding")
STAGE_GAPS = ("touching", "near", "mid", "far")

_X_ALIASES = {
    "left": "left_third",
    "right": "right_third",
    "middle": "center",
    "centre": "center",
}
_DEPTH_ALIASES = {
    "midground": "mid",
    "mid_ground": "mid",
    "near": "foreground",
    "close": "foreground",
}
_KIND_ALIASES = {"people": "group", "person": "character", "crowd": "group"}
_SIDE_ALIASES = {"axis": "on_axis", "onaxis": "on_axis"}
_REL_ALIASES = {"back": "behind", "trailing": "behind", "front": "ahead"}


def coerce_token(
    value: str,
    allowed: tuple[str, ...],
    aliases: dict[str, str],
    name: str,
    *,
    allow_empty: bool,
) -> str:
    """Normalize an optional enum token. Empty is valid when ``allow_empty`` is set."""
    cleaned = value.strip().lower().replace("-", "_").replace(" ", "_")
    if not cleaned:
        if allow_empty:
            return ""
        raise ValueError(f"{name} must be one of: {', '.join(allowed)}")
    cleaned = aliases.get(cleaned, cleaned)
    if cleaned not in allowed:
        raise ValueError(f"{name} must be one of: {', '.join(allowed)}")
    return cleaned


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


class CameraCard(BaseModel):
    """One shot's camera grammar. Empty strings mean a hand-written pack skipped the card.

    ``scale``, ``angle``, and ``move`` are a single framing choice. ``exit_frame``
    names the picture the next shot should open on.
    """

    scale: str = ""
    angle: str = ""
    move: str = ""
    exit_frame: str = ""

    @field_validator("scale")
    @classmethod
    def scale_value(cls, value: str) -> str:
        return coerce_token(value, CAMERA_SCALES, _SCALE_ALIASES, "scale", allow_empty=True)

    @field_validator("angle")
    @classmethod
    def angle_value(cls, value: str) -> str:
        return coerce_token(value, CAMERA_ANGLES, _ANGLE_ALIASES, "angle", allow_empty=True)

    @field_validator("move")
    @classmethod
    def move_value(cls, value: str) -> str:
        return coerce_token(value, CAMERA_MOVES, _MOVE_ALIASES, "move", allow_empty=True)

    @field_validator("exit_frame")
    @classmethod
    def exit_text(cls, value: str) -> str:
        return value.strip()


class StoryBeat(BaseModel):
    """One line in the pack beat map. ``role`` is setup, turn, climax, or button."""

    role: str
    summary: str = ""

    @field_validator("role")
    @classmethod
    def role_value(cls, value: str) -> str:
        return coerce_token(value, BEAT_ROLES, {}, "beat", allow_empty=False)

    @field_validator("summary")
    @classmethod
    def summary_text(cls, value: str) -> str:
        return value.strip()


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


class CastRef(BaseModel):
    """One named reference image. ``image_path`` is relative to the data directory.

    An empty ``image_path`` is allowed on a plan or fill draft so names can be
    written into prompts before a file is uploaded. Saving a pack requires the file.
    """

    id: str
    name: str
    role: str
    markers: str = ""
    image_path: str = ""

    @field_validator("id")
    @classmethod
    def cast_id(cls, value: str) -> str:
        cleaned = value.strip()
        if not _SHOT_ID.fullmatch(cleaned):
            raise ValueError("id must be a filename-safe token (letters, numbers, _, -)")
        return cleaned

    @field_validator("name")
    @classmethod
    def cast_name(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("name must not be empty")
        if len(cleaned) > 80:
            raise ValueError("name must be at most 80 characters")
        return cleaned

    @field_validator("role")
    @classmethod
    def cast_role(cls, value: str) -> str:
        return coerce_token(value, CAST_ROLES, {}, "role", allow_empty=False)

    @field_validator("markers")
    @classmethod
    def cast_markers(cls, value: str) -> str:
        cleaned = value.strip()
        if len(cleaned) > 400:
            raise ValueError("markers must be at most 400 characters")
        return cleaned

    @field_validator("image_path")
    @classmethod
    def cast_image_path(cls, value: str) -> str:
        return _relative_data_path(value, "image_path")


def _relative_data_path(value: str, name: str) -> str:
    cleaned = value.strip().replace("\\", "/")
    if not cleaned:
        return ""
    if cleaned.startswith("/") or ".." in cleaned.split("/"):
        raise ValueError(f"{name} must be a relative path inside the data directory")
    return cleaned


class StageBlock(BaseModel):
    """Where one entity is at a shot boundary. ``look`` is the eyeline, not travel."""

    id: str
    x: str
    depth: str
    facing: str
    travel: str
    visible: bool = True
    look: str = ""

    @field_validator("id")
    @classmethod
    def block_id(cls, value: str) -> str:
        cleaned = value.strip()
        if not _SHOT_ID.fullmatch(cleaned):
            raise ValueError("id must be a filename-safe token (letters, numbers, _, -)")
        return cleaned

    @field_validator("x")
    @classmethod
    def x_value(cls, value: str) -> str:
        return coerce_token(value, SCREEN_X, _X_ALIASES, "x", allow_empty=False)

    @field_validator("depth")
    @classmethod
    def depth_value(cls, value: str) -> str:
        return coerce_token(value, DEPTHS, _DEPTH_ALIASES, "depth", allow_empty=False)

    @field_validator("facing")
    @classmethod
    def facing_value(cls, value: str) -> str:
        return coerce_token(value, FACINGS, {}, "facing", allow_empty=False)

    @field_validator("look")
    @classmethod
    def look_value(cls, value: str) -> str:
        return coerce_token(value, FACINGS, {}, "look", allow_empty=True)

    @field_validator("travel")
    @classmethod
    def travel_value(cls, value: str) -> str:
        return coerce_token(value, TRAVELS, {}, "travel", allow_empty=False)


class StageRelation(BaseModel):
    """A spatial relation. Scene relations are the default; a shot may override them."""

    a: str
    rel: str
    b: str
    gap: str = ""

    @field_validator("a", "b")
    @classmethod
    def party_id(cls, value: str) -> str:
        cleaned = value.strip()
        if not _SHOT_ID.fullmatch(cleaned):
            raise ValueError("relation ids must be filename-safe tokens")
        return cleaned

    @field_validator("rel")
    @classmethod
    def rel_value(cls, value: str) -> str:
        return coerce_token(value, STAGE_RELATIONS, _REL_ALIASES, "rel", allow_empty=False)

    @field_validator("gap")
    @classmethod
    def gap_value(cls, value: str) -> str:
        return coerce_token(value, STAGE_GAPS, {}, "gap", allow_empty=True)


class StageEntity(BaseModel):
    """One figure or group on the scene stage map. ``cast_id`` may be empty."""

    id: str
    label: str
    kind: str = "character"
    cast_id: str = ""
    count: int = 1

    @field_validator("id")
    @classmethod
    def entity_id(cls, value: str) -> str:
        cleaned = value.strip()
        if not _SHOT_ID.fullmatch(cleaned):
            raise ValueError("id must be a filename-safe token (letters, numbers, _, -)")
        return cleaned

    @field_validator("label")
    @classmethod
    def entity_label(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("label must not be empty")
        if len(cleaned) > 160:
            raise ValueError("label must be at most 160 characters")
        return cleaned

    @field_validator("kind")
    @classmethod
    def kind_value(cls, value: str) -> str:
        return coerce_token(value, ENTITY_KINDS, _KIND_ALIASES, "kind", allow_empty=False)

    @field_validator("cast_id")
    @classmethod
    def cast_link(cls, value: str) -> str:
        cleaned = value.strip()
        if cleaned and not _SHOT_ID.fullmatch(cleaned):
            raise ValueError("cast_id must be a filename-safe token")
        return cleaned

    @field_validator("count")
    @classmethod
    def count_value(cls, value: int) -> int:
        if value < 1 or value > 24:
            raise ValueError("count must be between 1 and 24")
        return value


class StagingScene(BaseModel):
    """Axis, travel, and default relations for one run of shots."""

    id: str
    shot_ids: list[str] = Field(default_factory=list)
    axis: str = ""
    travel: str = ""
    entities: list[StageEntity] = Field(default_factory=list)
    relations: list[StageRelation] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def scene_id(cls, value: str) -> str:
        cleaned = value.strip()
        if not _SHOT_ID.fullmatch(cleaned):
            raise ValueError("id must be a filename-safe token (letters, numbers, _, -)")
        return cleaned

    @field_validator("shot_ids")
    @classmethod
    def shot_id_list(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value if item.strip()]
        for item in cleaned:
            if not _SHOT_ID.fullmatch(item):
                raise ValueError("shot_ids must be filename-safe tokens")
        return cleaned

    @field_validator("axis")
    @classmethod
    def axis_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("travel")
    @classmethod
    def scene_travel(cls, value: str) -> str:
        return coerce_token(value, TRAVELS, {}, "travel", allow_empty=True)


class StagingMap(BaseModel):
    """Pack-level stage map. Empty when a pack has no blocking."""

    scenes: list[StagingScene] = Field(default_factory=list)


class ShotStage(BaseModel):
    """Blocking at the first and last frame. ``cross_reason`` is required to change sides.

    ``cross_motivation`` is accepted as an alias for ``cross_reason``.
    """

    scene_id: str = ""
    camera_side: str = ""
    cross_reason: str = ""
    start: list[StageBlock] = Field(default_factory=list)
    end: list[StageBlock] = Field(default_factory=list)
    relations: list[StageRelation] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def motivation_alias(cls, value: object) -> object:
        if isinstance(value, dict) and "cross_reason" not in value and "cross_motivation" in value:
            merged = dict(value)
            merged["cross_reason"] = merged.pop("cross_motivation")
            return merged
        return value

    @field_validator("scene_id")
    @classmethod
    def scene_link(cls, value: str) -> str:
        cleaned = value.strip()
        if cleaned and not _SHOT_ID.fullmatch(cleaned):
            raise ValueError("scene_id must be a filename-safe token")
        return cleaned

    @field_validator("camera_side")
    @classmethod
    def side_value(cls, value: str) -> str:
        return coerce_token(value, CAMERA_SIDES, _SIDE_ALIASES, "camera_side", allow_empty=True)

    @field_validator("cross_reason")
    @classmethod
    def reason_text(cls, value: str) -> str:
        return value.strip()


class Shot(BaseModel):
    id: str
    prompt_still: str
    prompt_motion: str
    duration_sec: int = DEFAULT_DURATION_SEC
    end_state: str = ""
    start_state: str = ""
    beat: str = ""
    camera: CameraCard = Field(default_factory=CameraCard)
    stage: ShotStage | None = None
    video_mode: str = "image_to_video"
    dialogue: str = ""
    voice_id: str = ""

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

    @field_validator("beat")
    @classmethod
    def beat_role(cls, value: str) -> str:
        return coerce_token(value, BEAT_ROLES, {}, "beat", allow_empty=True)

    @field_validator("duration_sec")
    @classmethod
    def duration(cls, value: int) -> int:
        if value < MIN_DURATION_SEC or value > MAX_DURATION_SEC:
            raise ValueError(
                f"duration_sec must be between {MIN_DURATION_SEC} and {MAX_DURATION_SEC}"
            )
        return value

    @field_validator("video_mode")
    @classmethod
    def video_mode_value(cls, value: str) -> str:
        return coerce_token(value, VIDEO_MODES, {}, "video_mode", allow_empty=False)

    @field_validator("dialogue")
    @classmethod
    def dialogue_text(cls, value: str) -> str:
        cleaned = value.strip()
        if len(cleaned) > 400:
            raise ValueError("dialogue must be at most 400 characters")
        return cleaned

    @field_validator("voice_id")
    @classmethod
    def voice_value(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            return ""
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,31}", cleaned):
            raise ValueError("voice_id must be a short voice token such as eve")
        return cleaned


class PackIn(BaseModel):
    title: str
    logline: str = ""
    aspect_ratio: str = "16:9"
    resolution: str = "720p"
    look_bible: LookBible = Field(default_factory=LookBible)
    style_preset: str = ""
    beat_map: list[StoryBeat] = Field(default_factory=list)
    cast: list[CastRef] = Field(default_factory=list)
    staging: StagingMap | None = None
    lock_staging: bool = True
    music_path: str = ""
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

    @field_validator("style_preset")
    @classmethod
    def style_value(cls, value: str) -> str:
        return coerce_token(value, STYLE_PRESETS, {}, "style_preset", allow_empty=True)

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

    @field_validator("music_path")
    @classmethod
    def music_value(cls, value: str) -> str:
        return _relative_data_path(value, "music_path")

    @model_validator(mode="after")
    def continuity(self) -> PackIn:
        if len(self.cast) > MAX_CAST_ENTRIES:
            raise ValueError(f"A pack can include at most {MAX_CAST_ENTRIES} cast references")
        cast_ids = [item.id for item in self.cast]
        if len(cast_ids) != len(set(cast_ids)):
            raise ValueError("cast ids must be unique")
        ids = [shot.id for shot in self.shots]
        if len(ids) != len(set(ids)):
            raise ValueError("shot ids must be unique")
        for shot in self.shots:
            if shot.voice_id and shot.video_mode != "reference_to_video":
                raise ValueError(
                    f"shots[{shot.id}].voice_id is only supported on reference_to_video"
                )
            if shot.video_mode == "reference_to_video" and not self.cast and not shot.voice_id:
                raise ValueError(
                    f"shots[{shot.id}].video_mode reference_to_video "
                    "needs cast images or a voice_id"
                )
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


class ShotRevision(BaseModel):
    """One kept clip. ``clip_path`` is null once that file is gone from disk."""

    version: int
    action: str
    clip_path: str | None = None
    prompt: str = ""
    created_at: str = ""


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
    video_mode: str | None = None
    video_request_id: str | None = None
    note: str | None = None
    error: str | None = None
    moderation: ModerationNote | None = None
    revisions: list[ShotRevision] = Field(default_factory=list)


class JobOut(BaseModel):
    id: str
    pack_id: str
    status: str
    message: str | None = None
    error: str | None = None
    continuity_mode: str | None = None
    grade_match: bool = False
    has_audio: bool = False
    music_bed_applied: bool = False
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


class RegenerateIn(BaseModel):
    prompt_still: str = ""
    prompt_motion: str = ""

    @field_validator("prompt_still", "prompt_motion")
    @classmethod
    def optional_prompt(cls, value: str) -> str:
        return value.strip()


class EditClipIn(BaseModel):
    prompt: str

    @field_validator("prompt")
    @classmethod
    def required_prompt(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("prompt must not be empty")
        return cleaned


class ExtendClipIn(BaseModel):
    prompt: str
    duration_sec: int

    @field_validator("prompt")
    @classmethod
    def required_prompt(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("prompt must not be empty")
        return cleaned

    @field_validator("duration_sec")
    @classmethod
    def extend_duration(cls, value: int) -> int:
        if value < EXTEND_MIN_SEC or value > EXTEND_MAX_SEC:
            raise ValueError(
                f"duration_sec must be between {EXTEND_MIN_SEC} and {EXTEND_MAX_SEC}"
            )
        return value


class MusicOut(BaseModel):
    music_path: str
    music_bed_applied: bool = False
    has_audio: bool = False
