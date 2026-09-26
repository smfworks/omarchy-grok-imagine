"""Stage map, prompt clause, and spatial checks. No network and no media URLs.

The planner may omit ``staging``. A pack without it still loads. When a shot
has blocking and ``lock_staging`` is on, ``staging_clause`` is injected into
the still, the edit, and the motion prompt.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Any

from omarchy_imagine.schema import (
    CAMERA_SIDES,
    DEPTHS,
    ENTITY_KINDS,
    FACINGS,
    SCREEN_X,
    STAGE_GAPS,
    STAGE_RELATIONS,
    TRAVELS,
    PackIn,
    ShotStage,
    StagingMap,
    coerce_token,
)

STAGING_HEADER = "Staging (locked, 180° line):"

_X_INDEX = {name: index for index, name in enumerate(SCREEN_X)}
_DEPTH_INDEX = {name: index for index, name in enumerate(DEPTHS)}
_LEFT = {"offscreen_left", "left_edge", "left_third"}
_RIGHT = {"offscreen_right", "right_edge", "right_third"}
_OPPOSITE_TRAVEL = {
    "screen_left": "screen_right",
    "screen_right": "screen_left",
    "toward_camera": "away_from_camera",
    "away_from_camera": "toward_camera",
}

_X_PHRASE = {
    "offscreen_left": "off screen to the left",
    "left_edge": "left edge of frame",
    "left_third": "left third of frame",
    "center": "center of frame",
    "right_third": "right third of frame",
    "right_edge": "right edge of frame",
    "offscreen_right": "off screen to the right",
}
_DEPTH_PHRASE = {
    "foreground": "foreground",
    "mid": "mid-ground",
    "background": "background",
    "far": "far background",
}
_LOOK_PHRASE = {
    "screen_left": "screen-left",
    "screen_right": "screen-right",
    "toward_camera": "the camera",
    "away_from_camera": "away from the camera",
}
_TRAVEL_SENTENCE = {
    "screen_right": "Everything travels left-to-right on screen.",
    "screen_left": "Everything travels right-to-left on screen.",
    "toward_camera": "Everything travels toward the camera.",
    "away_from_camera": "Everything travels away from the camera.",
    "static": "The figures hold their places.",
}
_GAP_PHRASE = {
    "touching": "at touching distance",
    "near": "near",
    "mid": "at mid distance",
    "far": "far back",
}
_CONTACT = re.compile(
    r"\b(?:touch(?:es|ing)?|collid(?:e|es|ing)|grabs?|contact|tackl(?:e|es|ing))\b",
    re.IGNORECASE,
)
_VAGUE = re.compile(r"\b(?:near|next to|alongside|beside)\b", re.IGNORECASE)
_FALL_BACK = re.compile(
    r"\b(?:(?:have|has|had|having|been)\s+)?(?:fall|falls|falling|fell|fallen)\s+back\b",
    re.IGNORECASE,
)
# The story itself asked pursuers to stop or come off the horse. "Fall back"
# in a generated line is still rewritten unless one of these is in the brief,
# because the still model reads that phrase as bodies on the ground.
_STORY_ALLOWS_STOP = re.compile(
    r"\b(?:falls?\s+back|fallen\s+back|falling\s+back|dismounts?|unhorsed|"
    r"tumbles?|lying on the ground|halts?|stopped|stand(?:s|ing)? still|"
    r"shot off|knocked off)\b",
    re.IGNORECASE,
)
_MOUNT_WORD = re.compile(
    r"\b(?:horses?|mounts?|steeds?|mares?|stallions?|geldings?|ponies|mustangs?)\b",
    re.IGNORECASE,
)
_ASTRIDE = re.compile(r"\b(?:on|riding|rides|astride|atop)\b", re.IGNORECASE)
_HORSE_PARTY = re.compile(
    r"\b(?:horses?|saddles?|riders?|mounts?|gallops?|galloping)\b",
    re.IGNORECASE,
)
_SENTENCE_START = re.compile(r"(^|(?<=\.\s))([a-z])")
_AFTER_PERIOD = re.compile(r"(?<=\.\s)([a-z])")
_NAME_STOP = frozenset(
    {
        "the",
        "his",
        "her",
        "their",
        "and",
        "with",
        "horse",
        "horses",
        "mount",
        "rider",
        "riders",
    }
)
_PURSUIT = (
    "chase",
    "pursuit",
    "pursued",
    "posse",
    "bandits",
    "outlaws",
    "fleeing",
    "gallop",
)
_LINE_CROSS_MOVES = {"orbit", "whip_pan"}


@dataclass(frozen=True)
class StagingIssue:
    shot_index: int
    code: str
    severity: str
    message: str


def is_pursuit(prompt: str) -> bool:
    """True when the story is a chase. Used to stage a no-key plan."""
    low = prompt.lower()
    return any(re.search(rf"\b{re.escape(word)}\b", low) for word in _PURSUIT)


def staging_clause(pack: PackIn | dict[str, Any], shot: Any, phase: str) -> str:
    """Positional English plus negative locks. Empty when locking is off or unset."""
    parsed = _pack(pack)
    shot_model = _shot(parsed, shot)
    if parsed is None or shot_model is None:
        return ""
    if not getattr(parsed, "lock_staging", True):
        return ""
    stage = getattr(shot_model, "stage", None)
    if stage is None:
        return ""
    blocks = stage.end if phase == "motion" and stage.end else stage.start
    if phase != "motion":
        blocks = stage.start or stage.end
    if not blocks:
        return ""
    scene = _scene_for(parsed.staging, stage, shot_model.id)
    entities = {item.id: item for item in (scene.entities if scene else [])}
    raw_relations = list(stage.relations or (scene.relations if scene else []))
    mount_ids = _self_mount_ids(raw_relations, entities)
    rider_ids = _self_mount_riders(raw_relations, entities)
    relations = [item for item in raw_relations if not _is_self_mount_relation(item, entities)]
    travel = _scene_travel(scene, blocks)
    lines = [STAGING_HEADER, _header_sentence(scene, stage, travel)]
    rendered_twist = False
    for block in blocks:
        if block.id in mount_ids and rider_ids and any(item.id in rider_ids for item in blocks):
            continue
        if not block.visible and phase != "motion":
            label = _label(entities, block.id)
            lines.append(f"- {label}: not in frame.")
            continue
        line = _block_line(block, entities, relations, travel, parsed.style_preset)
        if "twists at the waist in the saddle" in line:
            rendered_twist = True
        lines.append(line)
    # The still is the picture the image model paints. If the look-back lands
    # on stage.end, the start blocks alone would only say "looks toward".
    if phase != "motion" and not rendered_twist:
        extra = _lookback_sentence(stage.end, entities, parsed.style_preset)
        if extra:
            lines.append(extra)
    if phase == "motion":
        change = _change_line(stage.start, stage.end, entities, parsed.style_preset)
        if change:
            lines.append(change)
        if not rendered_twist:
            extra = _lookback_sentence(blocks, entities, parsed.style_preset)
            if extra and extra not in lines:
                lines.append(extra)
    locks = _negative_locks(relations, entities, travel)
    if locks:
        lines.append(locks)
    return _polish_clause("\n".join(lines))


def apply_staging(
    draft: dict[str, Any],
    *,
    style: str,
    prompt: str,
    supplied: StagingMap | dict[str, Any] | None = None,
    model_staging: dict[str, Any] | None = None,
    rebuild: bool = False,
) -> None:
    """Write a scene map and per-shot blocking onto a pack draft.

    A supplied map wins. Otherwise a model map is normalized. A chase with
    neither gets a two-party default. ``rebuild`` refreshes that default after
    beats are known. Shot N ``stage.start`` copies shot N-1 ``stage.end``.
    Packs that are not chases and have no map are left alone.
    """
    shots = draft.get("shots")
    if not isinstance(shots, list):
        return
    supplied_map = _coerce_map(supplied)
    model_map = _coerce_map(model_staging)
    existing = _coerce_map(draft.get("staging"))
    if supplied_map is not None and supplied_map.scenes:
        staging = supplied_map
        if rebuild:
            for shot in shots:
                if isinstance(shot, dict):
                    shot["stage"] = None
    elif model_map is not None and model_map.scenes:
        staging = model_map
    elif (style == "chase" or is_pursuit(prompt)) and (rebuild or existing is None):
        staging = _default_map(shots, prompt)
        if rebuild:
            for shot in shots:
                if isinstance(shot, dict):
                    shot["stage"] = None
    elif existing is not None and existing.scenes:
        staging = existing
    else:
        staging = None
    if staging is None:
        draft["staging"] = None
        return
    _attach_shot_ids(staging, shots)
    _strip_scene_mounts(staging)
    draft["staging"] = staging.model_dump()
    _fill_shot_stages(shots, staging, style=style, prompt=prompt)
    _strip_shot_mounts(shots, staging)
    _keep_pursuers_riding(shots, staging, prompt)
    _copy_handoff(shots)
    _guard_cameras(shots, staging)
    _normalize_draft_prose(draft, staging, prompt)
    _ensure_lookback_motion(shots, staging, style)


def check_staging(
    pack: PackIn,
    *,
    still_prompts: list[str] | None = None,
    motion_prompts: list[str] | None = None,
) -> list[StagingIssue]:
    """Spatial issues for ``/api/packs/preflight``. Does not call the network."""
    issues: list[StagingIssue] = []
    staging = getattr(pack, "staging", None)
    lock_staging = getattr(pack, "lock_staging", True)
    for index, shot in enumerate(pack.shots):
        stage = getattr(shot, "stage", None)
        scene = _scene_for(staging, stage, shot.id)
        count = _entity_count(pack, scene)
        if count >= 2 and (stage is None or not stage.start):
            issues.append(
                StagingIssue(
                    index,
                    "stage_missing",
                    "warn",
                    f"Shot {shot.id} has {count} entities and no stage blocking.",
                )
            )
        if stage is None:
            continue
        if index > 0:
            previous = pack.shots[index - 1]
            issues.extend(_handoff_issues(previous, shot, index))
        previous = pack.shots[index - 1] if index else None
        issues.extend(_side_issues(shot, index, previous=previous))
        issues.extend(_travel_issues(shot, index, previous=previous))
        issues.extend(_relation_issues(shot, scene, index))
        if count >= 2 and _line_risk(shot):
            move = shot.camera.move or "unset"
            angle = shot.camera.angle or "unset"
            issues.append(
                StagingIssue(
                    index,
                    "line_risk_camera",
                    "warn",
                    (
                        f"Shot {shot.id} camera {move}/{angle} crosses the line. "
                        "Use pan or dolly, or set camera_side to cross with a cross_reason."
                    ),
                )
            )
        if count >= 2 and shot.video_mode == "reference_to_video":
            issues.append(
                StagingIssue(
                    index,
                    "r2v_no_anchor",
                    "warn",
                    (
                        f"Shot {shot.id} is reference_to_video, so the composed still "
                        "is not the first frame. The staging clause is the only position lock."
                    ),
                )
            )
        prose = f"{shot.prompt_still}\n{shot.prompt_motion}"
        relations = stage.relations or (scene.relations if scene else [])
        if any(item.rel == "behind" for item in relations) and _VAGUE.search(prose):
            issues.append(
                StagingIssue(
                    index,
                    "vague_position",
                    "info",
                    (
                        f"Shot {shot.id} uses a vague position (near, next to, alongside, "
                        "or beside) for a pursuit. Name the screen third instead."
                    ),
                )
            )
        if lock_staging and stage.start:
            still = still_prompts[index] if still_prompts and index < len(still_prompts) else ""
            motion = motion_prompts[index] if motion_prompts and index < len(motion_prompts) else ""
            if (still and STAGING_HEADER not in still) or (motion and STAGING_HEADER not in motion):
                issues.append(
                    StagingIssue(
                        index,
                        "clause_missing",
                        "warn",
                        f"Shot {shot.id} assembled prompt is missing the staging clause.",
                    )
                )
    return issues


def guard_line_cross(move: str, angle: str, camera_side: str) -> tuple[str, str]:
    """Drop orbit, whip-pan, and a reverse OTS unless the shot crosses on purpose."""
    if camera_side == "cross":
        return move, angle
    if move in _LINE_CROSS_MOVES:
        move = "pan"
    if angle == "ots" and camera_side != "on_axis":
        angle = "eye"
    return move, angle


def _pack(pack: PackIn | dict[str, Any]) -> PackIn | None:
    if isinstance(pack, PackIn):
        return pack
    if isinstance(pack, dict):
        try:
            return PackIn.model_validate(pack)
        except ValueError:
            return None
    return None


def _shot(pack: PackIn | None, shot: Any) -> Any:
    if pack is None:
        return None
    if hasattr(shot, "stage") and hasattr(shot, "id"):
        match = next((item for item in pack.shots if item.id == shot.id), None)
        return match or shot
    if isinstance(shot, dict):
        shot_id = str(shot.get("id") or "")
        match = next((item for item in pack.shots if item.id == shot_id), None)
        return match
    return None


def _scene_for(staging: StagingMap | None, stage: ShotStage | None, shot_id: str) -> Any:
    if staging is None:
        return None
    scene_id = stage.scene_id if stage else ""
    if scene_id:
        found = next((scene for scene in staging.scenes if scene.id == scene_id), None)
        if found is not None:
            return found
    for scene in staging.scenes:
        if shot_id in scene.shot_ids:
            return scene
    if len(staging.scenes) == 1:
        return staging.scenes[0]
    return None


def _scene_travel(scene: Any, blocks: list[Any]) -> str:
    if scene is not None and scene.travel:
        return str(scene.travel)
    for block in blocks:
        if block.travel and block.travel != "static":
            return str(block.travel)
    return "static"


def _header_sentence(scene: Any, stage: ShotStage, travel: str) -> str:
    parts = [_TRAVEL_SENTENCE.get(travel, _TRAVEL_SENTENCE["static"])]
    side = stage.camera_side or "same"
    if side == "cross":
        reason = stage.cross_reason or "none given"
        parts.append(f"Camera crosses the line. Reason: {reason}.")
    elif side == "on_axis":
        parts.append("Camera sits on the axis, looking along the line of action.")
    else:
        parts.append("Camera stays on the same side of the line.")
    if scene is not None and scene.axis:
        parts.append(scene.axis.rstrip(".") + ".")
    return _capitalize_sentences(" ".join(parts))


def _label(entities: dict[str, Any], entity_id: str) -> str:
    entity = entities.get(entity_id)
    if entity is not None and entity.label:
        return str(entity.label)
    return entity_id.replace("_", " ")


def _block_line(
    block: Any,
    entities: dict[str, Any],
    relations: list[Any],
    travel: str,
    style: str,
) -> str:
    label = _label(entities, block.id)
    relation = _relation_phrase(block.id, relations, entities)
    gap = _gap_for(block.id, relations)
    riding = style == "chase" and block.travel in {"screen_left", "screen_right"}
    look = ""
    profile = _pursuer_profile(block, entities, relations, style)
    if _looks_back(block.look, block.travel) and _is_mounted_rider(block, entities, style):
        motion = _twist_sentence(block.look, block.travel)
    elif profile:
        motion = profile
    else:
        motion = _motion_phrase(block.travel, riding=riding)
        if block.look and block.look not in {block.facing, block.travel}:
            look = f", looks toward {_LOOK_PHRASE.get(block.look, block.look)}"
    gap_bit = f", {gap}" if gap else ""
    rel_bit = f", {relation}" if relation else ""
    hidden = "" if block.visible else ", not in frame"
    return (
        f"- {label}: {_X_PHRASE.get(block.x, block.x)}, "
        f"{_DEPTH_PHRASE.get(block.depth, block.depth)}{rel_bit}{gap_bit}, "
        f"{motion}{look}{hidden}."
    )


def _relation_phrase(entity_id: str, relations: list[Any], entities: dict[str, Any]) -> str:
    for item in relations:
        if item.a == entity_id and item.rel == "behind":
            return f"BEHIND {_label(entities, item.b)}"
        if item.a == entity_id and item.rel:
            return f"{item.rel} {_label(entities, item.b)}"
    return ""


def _gap_for(entity_id: str, relations: list[Any]) -> str:
    for item in relations:
        if item.a == entity_id and item.gap:
            if item.gap == "far" and item.rel == "behind":
                return "about ten horse-lengths back"
            return _GAP_PHRASE.get(item.gap, item.gap)
    return ""


def _motion_phrase(travel: str, *, riding: bool) -> str:
    verb = "riding" if riding else "moving"
    if travel == "screen_right":
        return f"{verb} toward screen-right"
    if travel == "screen_left":
        return f"{verb} toward screen-left"
    if travel == "toward_camera":
        return "moving toward the camera"
    if travel == "away_from_camera":
        return "moving away from the camera"
    return "holding still"


def _change_line(
    start: list[Any],
    end: list[Any],
    entities: dict[str, Any],
    style: str,
) -> str:
    before = {block.id: block for block in start}
    bits: list[str] = []
    for block in end:
        prior = before.get(block.id)
        if prior is None:
            continue
        label = _label(entities, block.id)
        if prior.depth != block.depth:
            bits.append(
                _capitalize_sentences(
                    f"{label} moves from {_DEPTH_PHRASE.get(prior.depth, prior.depth)} "
                    f"to {_DEPTH_PHRASE.get(block.depth, block.depth)}"
                )
            )
        elif prior.x != block.x:
            bits.append(
                _capitalize_sentences(
                    f"{label} moves from {_X_PHRASE.get(prior.x, prior.x)} "
                    f"to {_X_PHRASE.get(block.x, block.x)}"
                )
            )
        looking_back = _looks_back(block.look, block.travel) and _is_mounted_rider(
            block, entities, style
        )
        if prior.look != block.look and block.look and not looking_back:
            bits.append(
                _capitalize_sentences(
                    f"{label} looks toward {_LOOK_PHRASE.get(block.look, block.look)}"
                )
            )
        if prior.travel != block.travel:
            bits.append(
                _capitalize_sentences(f"{label} travel becomes {block.travel.replace('_', ' ')}")
            )
    if not bits:
        return ""
    return "Change: " + "; ".join(bits) + "."


def _negative_locks(relations: list[Any], entities: dict[str, Any], travel: str) -> str:
    sentences: list[str] = []
    for item in relations:
        if item.rel != "behind":
            continue
        pursued = _label(entities, item.b)
        pursuers = _label(entities, item.a)
        ban = _banned_side(travel)
        sentences.append(f"Nobody rides beside {pursued}.")
        sentences.append(
            _capitalize_sentences(
                f"{pursuers} never pass {pursued} and never appear on {ban}."
            )
        )
    seen: list[str] = []
    for sentence in sentences:
        if sentence not in seen:
            seen.append(sentence)
    return " ".join(seen)


def _banned_side(travel: str) -> str:
    if travel == "screen_right":
        return "the right of frame"
    if travel == "screen_left":
        return "the left of frame"
    if travel == "toward_camera":
        return "the camera side ahead of them"
    if travel == "away_from_camera":
        return "the far side ahead of them"
    return "a side that puts them beside the lead"


def _coerce_map(value: StagingMap | dict[str, Any] | None) -> StagingMap | None:
    if value is None:
        return None
    if isinstance(value, StagingMap):
        return value
    if isinstance(value, dict) and not value.get("scenes"):
        return None
    try:
        return StagingMap.model_validate(_loose_map(value))
    except ValueError:
        return None


def _loose_map(value: dict[str, Any]) -> dict[str, Any]:
    scenes: list[dict[str, Any]] = []
    for scene in value.get("scenes") or []:
        if not isinstance(scene, dict):
            continue
        entities = []
        for entity in scene.get("entities") or []:
            if not isinstance(entity, dict) or not str(entity.get("id") or "").strip():
                continue
            entities.append(
                {
                    "id": str(entity.get("id")),
                    "label": str(entity.get("label") or entity.get("id")),
                    "kind": _token(
                        str(entity.get("kind") or "character"), ENTITY_KINDS, "character"
                    ),
                    "cast_id": str(entity.get("cast_id") or ""),
                    "count": int(entity.get("count") or 1),
                }
            )
        scenes.append(
            {
                "id": str(scene.get("id") or "sc1"),
                "shot_ids": [str(item) for item in scene.get("shot_ids") or []],
                "axis": str(scene.get("axis") or ""),
                "travel": _token(str(scene.get("travel") or ""), TRAVELS, ""),
                "entities": entities,
                "relations": [_loose_relation(item) for item in scene.get("relations") or []],
            }
        )
        scenes[-1]["relations"] = [item for item in scenes[-1]["relations"] if item]
    return {"scenes": scenes}


def _loose_relation(value: Any) -> dict[str, str] | None:
    if not isinstance(value, dict):
        return None
    left = str(value.get("a") or "").strip()
    right = str(value.get("b") or "").strip()
    if not left or not right:
        return None
    return {
        "a": left,
        "rel": _token(str(value.get("rel") or "behind"), STAGE_RELATIONS, "behind"),
        "b": right,
        "gap": _token(str(value.get("gap") or ""), STAGE_GAPS, ""),
    }


def _loose_blocks(value: Any) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    if not isinstance(value, list):
        return blocks
    for item in value:
        if not isinstance(item, dict) or not str(item.get("id") or "").strip():
            continue
        travel = _token(str(item.get("travel") or "screen_right"), TRAVELS, "screen_right")
        blocks.append(
            {
                "id": str(item.get("id")),
                "x": _token(str(item.get("x") or "center"), SCREEN_X, "center"),
                "depth": _token(str(item.get("depth") or "mid"), DEPTHS, "mid"),
                "facing": _token(str(item.get("facing") or travel), FACINGS, "screen_right"),
                "look": _token(str(item.get("look") or ""), FACINGS, ""),
                "travel": travel,
                "visible": bool(item.get("visible", True)),
            }
        )
    return blocks


def _token(value: str, allowed: tuple[str, ...], default: str) -> str:
    try:
        return coerce_token(value, allowed, {}, "token", allow_empty=not default)
    except ValueError:
        return default if default in allowed or default == "" else allowed[0]


def _default_map(shots: list[Any], prompt: str) -> StagingMap:
    pursued_label, pursuer_label = _party_labels(prompt)
    ids = [str(shot.get("id") or "") for shot in shots if isinstance(shot, dict)]
    return StagingMap.model_validate(
        {
            "scenes": [
                {
                    "id": "sc1",
                    "shot_ids": [item for item in ids if item],
                    "axis": (
                        "the line of the chase runs across the frame; "
                        "the camera stays on one side"
                    ),
                    "travel": "screen_right",
                    "entities": [
                        {
                            "id": "pursued",
                            "label": pursued_label,
                            "kind": "character",
                            "cast_id": "",
                            "count": 1,
                        },
                        {
                            "id": "pursuers",
                            "label": pursuer_label,
                            "kind": "group",
                            "cast_id": "",
                            "count": 3,
                        },
                    ],
                    "relations": [{"a": "pursuers", "rel": "behind", "b": "pursued", "gap": "far"}],
                }
            ]
        }
    )


def _party_labels(prompt: str) -> tuple[str, str]:
    low = prompt.lower()
    if "cowboy" in low:
        lead = "the cowboy"
    elif "samurai" in low:
        lead = "the samurai"
    else:
        lead = "the pursued rider"
    if "bandit" in low:
        chase = "the bandits"
    elif "posse" in low:
        chase = "the posse"
    elif "outlaw" in low:
        chase = "the outlaws"
    else:
        chase = "the pursuers"
    return lead, chase


def _attach_shot_ids(staging: StagingMap, shots: list[Any]) -> None:
    """Map scene shot_ids onto the pack's real ids when the model used another set.

    A text model often returns ``"1"``–``"4"`` while the pack shots are ``s01``–``s04``.
    Ids that already match are left alone. Otherwise shots that name the scene via
    ``stage.scene_id`` win, then a single scene takes every shot, then leftover
    scenes are filled in order by how many ids they listed.
    """
    real = [str(shot.get("id")) for shot in shots if isinstance(shot, dict) and shot.get("id")]
    if not real or not staging.scenes:
        return
    real_set = set(real)

    def linked(scene_id: str) -> list[str]:
        found: list[str] = []
        for shot in shots:
            if not isinstance(shot, dict):
                continue
            stage = shot.get("stage")
            sid = str(shot.get("id") or "")
            if not sid or not isinstance(stage, dict):
                continue
            if str(stage.get("scene_id") or "") == scene_id:
                found.append(sid)
        return found

    pending: list[Any] = []
    claimed: list[str] = []
    for scene in staging.scenes:
        current = [item for item in scene.shot_ids if item in real_set]
        if scene.shot_ids and len(current) == len(scene.shot_ids):
            claimed.extend(scene.shot_ids)
            continue
        by_scene = linked(scene.id)
        if by_scene:
            scene.shot_ids = by_scene
            claimed.extend(by_scene)
            continue
        pending.append(scene)
    if not pending:
        return
    if len(staging.scenes) == 1:
        staging.scenes[0].shot_ids = list(real)
        return
    if not claimed:
        cursor = 0
        for scene in staging.scenes:
            count = len(scene.shot_ids)
            if count <= 0:
                continue
            scene.shot_ids = real[cursor : cursor + count]
            cursor += count
        return
    remaining = [sid for sid in real if sid not in set(claimed)]
    cursor = 0
    for scene in pending:
        count = len(scene.shot_ids)
        if count <= 0:
            scene.shot_ids = []
            continue
        scene.shot_ids = remaining[cursor : cursor + count]
        cursor += count


def _fill_shot_stages(
    shots: list[Any],
    staging: StagingMap,
    *,
    style: str,
    prompt: str,
) -> None:
    scene = staging.scenes[0]
    beats = [str(shot.get("beat") or "") if isinstance(shot, dict) else "" for shot in shots]
    generated = _generated_stages(scene, beats, style=style, prompt=prompt)
    for index, shot in enumerate(shots):
        if not isinstance(shot, dict):
            continue
        existing = shot.get("stage")
        if isinstance(existing, dict) and (existing.get("start") or existing.get("end")):
            shot["stage"] = _clean_stage(existing, scene.id)
            continue
        shot["stage"] = generated[index] if index < len(generated) else generated[-1]


def _clean_stage(raw: dict[str, Any], scene_id: str) -> dict[str, Any]:
    side = _token(str(raw.get("camera_side") or "same"), CAMERA_SIDES, "same")
    start = _loose_blocks(raw.get("start"))
    end = _loose_blocks(raw.get("end")) or copy.deepcopy(start)
    relations = [
        item for item in (_loose_relation(item) for item in raw.get("relations") or []) if item
    ]
    return {
        "scene_id": str(raw.get("scene_id") or scene_id),
        "camera_side": side,
        "cross_reason": str(raw.get("cross_reason") or raw.get("cross_motivation") or "").strip(),
        "start": start,
        "end": end,
        "relations": relations,
    }


def _generated_stages(
    scene: Any,
    beats: list[str],
    *,
    style: str,
    prompt: str,
) -> list[dict[str, Any]]:
    if style == "chase" or is_pursuit(prompt):
        return _chase_stages(scene, beats)
    return [_static_stage(scene) for _beat in beats]


def _static_stage(scene: Any) -> dict[str, Any]:
    blocks = _layout_blocks(
        scene, pursued_x="right_third", pursuer_x="left_third", pursuer_depth="far"
    )
    return {
        "scene_id": scene.id,
        "camera_side": "same",
        "cross_reason": "",
        "start": blocks,
        "end": copy.deepcopy(blocks),
        "relations": [item.model_dump() for item in scene.relations],
    }


def _chase_stages(scene: Any, beats: list[str]) -> list[dict[str, Any]]:
    """Progress the gap. Each start is the previous end, except the opening."""
    ends: list[list[dict[str, Any]]] = []
    for beat in beats:
        if beat == "button":
            # The final frame keeps the pursuers mounted and riding. Stopping
            # them, or writing that they "fall back", reads as bodies on the ground.
            ends.append(
                _layout_blocks(
                    scene,
                    pursued_x="right_edge",
                    pursued_depth="background",
                    pursuer_x="left_edge",
                    pursuer_depth="far",
                )
            )
        elif beat == "climax":
            ends.append(
                _layout_blocks(
                    scene,
                    pursued_x="right_third",
                    pursued_depth="foreground",
                    pursued_look="screen_left",
                    pursuer_x="left_third",
                    pursuer_depth="background",
                )
            )
        elif beat == "turn":
            ends.append(
                _layout_blocks(
                    scene,
                    pursued_x="right_third",
                    pursuer_x="left_third",
                    pursuer_depth="background",
                )
            )
        else:
            ends.append(
                _layout_blocks(
                    scene,
                    pursued_x="right_third",
                    pursuer_x="left_third",
                    pursuer_depth="far",
                )
            )
    opening = _layout_blocks(
        scene,
        pursued_x="center",
        pursuer_x="left_edge",
        pursuer_depth="far",
    )
    starts = [opening, *copy.deepcopy(ends[:-1])]
    stages: list[dict[str, Any]] = []
    for index, beat in enumerate(beats):
        gap = "far" if beat in {"", "setup", "button"} else "mid"
        relations = [
            {**item.model_dump(), "gap": gap} if item.rel == "behind" else item.model_dump()
            for item in scene.relations
        ]
        stages.append(
            {
                "scene_id": scene.id,
                "camera_side": "same",
                "cross_reason": "",
                "start": starts[index],
                "end": ends[index],
                "relations": relations,
            }
        )
    return stages


def _layout_blocks(
    scene: Any,
    *,
    pursued_x: str,
    pursuer_x: str,
    pursued_depth: str = "mid",
    pursuer_depth: str = "far",
    pursued_look: str = "",
    pursuer_travel: str = "",
) -> list[dict[str, Any]]:
    travel = scene.travel or "screen_right"
    pursued_id, pursuer_id = _pair_ids(scene)
    blocks = [
        {
            "id": pursued_id,
            "x": pursued_x,
            "depth": pursued_depth,
            "facing": travel if travel in FACINGS else "screen_right",
            "look": pursued_look,
            "travel": travel if travel != "static" else "screen_right",
            "visible": True,
        }
    ]
    if pursuer_id:
        blocks.append(
            {
                "id": pursuer_id,
                "x": pursuer_x,
                "depth": pursuer_depth,
                "facing": travel if travel in FACINGS else "screen_right",
                "look": "",
                "travel": pursuer_travel or (travel if travel != "static" else "screen_right"),
                "visible": True,
            }
        )
    return blocks


def _pair_ids(scene: Any) -> tuple[str, str]:
    ids = [entity.id for entity in scene.entities]
    for relation in scene.relations:
        if relation.rel == "behind":
            return relation.b, relation.a
    if len(ids) >= 2:
        return ids[0], ids[1]
    if ids:
        return ids[0], ""
    return "pursued", "pursuers"


def _copy_handoff(shots: list[Any]) -> None:
    for index in range(1, len(shots)):
        previous = shots[index - 1]
        current = shots[index]
        if not isinstance(previous, dict) or not isinstance(current, dict):
            continue
        prev_stage = previous.get("stage")
        stage = current.get("stage")
        if not isinstance(prev_stage, dict) or not isinstance(stage, dict):
            continue
        if prev_stage.get("end"):
            stage["start"] = copy.deepcopy(prev_stage["end"])


def _guard_cameras(shots: list[Any], staging: StagingMap) -> None:
    if sum(len(scene.entities) for scene in staging.scenes) < 2 and not any(
        len(scene.entities) >= 2 for scene in staging.scenes
    ):
        return
    multi = any(len(scene.entities) >= 2 for scene in staging.scenes)
    if not multi:
        return
    for shot in shots:
        if not isinstance(shot, dict):
            continue
        stage = shot.get("stage") if isinstance(shot.get("stage"), dict) else {}
        side = str(stage.get("camera_side") or "same")
        camera = shot.get("camera")
        if not isinstance(camera, dict):
            continue
        move, angle = guard_line_cross(
            str(camera.get("move") or ""), str(camera.get("angle") or ""), side
        )
        camera["move"] = move
        camera["angle"] = angle


def _entity_count(pack: PackIn, scene: Any) -> int:
    if scene is not None and scene.entities:
        return len(scene.entities)
    staging = getattr(pack, "staging", None)
    if staging and staging.scenes:
        return max(len(item.entities) for item in staging.scenes)
    cast = getattr(pack, "cast", []) or []
    return sum(1 for ref in cast if getattr(ref, "role", "") == "character")


def _handoff_issues(previous: Any, shot: Any, index: int) -> list[StagingIssue]:
    prev_stage = previous.stage
    stage = shot.stage
    if prev_stage is None or stage is None or not prev_stage.end or not stage.start:
        return []
    if _signature(prev_stage.end) == _signature(stage.start):
        return []
    return [
        StagingIssue(
            index,
            "stage_handoff",
            "block",
            (
                f"Shot {shot.id} stage.start is not shot {previous.id} stage.end. "
                "A paraphrase of the blocking is a fail."
            ),
        )
    ]


def _side_issues(shot: Any, index: int, *, previous: Any) -> list[StagingIssue]:
    stage = shot.stage
    if stage is None:
        return []
    if stage.camera_side == "cross" and not stage.cross_reason.strip():
        return [
            StagingIssue(
                index,
                "side_flip",
                "block",
                f"Shot {shot.id} sets camera_side to cross without a cross_reason.",
            )
        ]
    if _side_change_allowed(stage, previous.stage if previous is not None else None):
        return []
    changed = _pair_flipped(stage.start, stage.end)
    if previous is not None and previous.stage is not None and not _resets_line(previous.stage):
        changed = changed or _pair_flipped(previous.stage.end, stage.start)
    if not changed:
        return []
    return [
        StagingIssue(
            index,
            "side_flip",
            "block",
            (
                f"Shot {shot.id} moves an entity from screen-left to screen-right "
                "without a cross_reason."
            ),
        )
    ]


def _travel_issues(shot: Any, index: int, *, previous: Any) -> list[StagingIssue]:
    stage = shot.stage
    previous_stage = previous.stage if previous is not None else None
    if stage is None or _side_change_allowed(stage, previous_stage):
        return []
    reversed_travel = _travel_reversed(stage.start, stage.end)
    # An on-axis shot resets the line, so the next opening may pick a new travel.
    if previous_stage is not None and not _resets_line(previous_stage):
        reversed_travel = reversed_travel or _travel_reversed(previous_stage.end, stage.start)
    if not reversed_travel:
        return []
    return [
        StagingIssue(
            index,
            "travel_flip",
            "block",
            (
                f"Shot {shot.id} reverses travel without a cross_reason. "
                "A turn is a torso twist; the travel direction stays put."
            ),
        )
    ]


def _relation_issues(shot: Any, scene: Any, index: int) -> list[StagingIssue]:
    stage = shot.stage
    if stage is None:
        return []
    relations = stage.relations or (scene.relations if scene else [])
    behind = [item for item in relations if item.rel == "behind"]
    if not behind:
        return []
    prose = f"{shot.prompt_still}\n{shot.prompt_motion}"
    issues: list[StagingIssue] = []
    for relation in behind:
        if relation.gap == "touching" and not _CONTACT.search(prose):
            issues.append(
                StagingIssue(
                    index,
                    "relation_violation",
                    "block",
                    (
                        f"Shot {shot.id} sets {relation.a} touching {relation.b} "
                        "without a contact beat."
                    ),
                )
            )
        for blocks, label in ((stage.start, "start"), (stage.end, "end")):
            placed = {block.id: block for block in blocks}
            trailer = placed.get(relation.a)
            lead = placed.get(relation.b)
            if trailer is None or lead is None or not trailer.visible or not lead.visible:
                continue
            travel = lead.travel if lead.travel != "static" else trailer.travel
            if _is_behind(trailer, lead, travel):
                continue
            issues.append(
                StagingIssue(
                    index,
                    "relation_violation",
                    "block",
                    (
                        f"Shot {shot.id} {label}: {relation.a} is behind {relation.b} "
                        f"with travel {travel}, but {relation.a}.x ({trailer.x}) is not "
                        f"upstream of {relation.b}.x ({lead.x})."
                    ),
                )
            )
    return issues


def _line_risk(shot: Any) -> bool:
    side = shot.stage.camera_side if shot.stage else ""
    if side not in {"", "same"}:
        return False
    move = shot.camera.move
    angle = shot.camera.angle
    if move in _LINE_CROSS_MOVES:
        return True
    return angle == "ots" and side != "on_axis"


def _side_change_allowed(stage: ShotStage, previous: ShotStage | None) -> bool:
    if stage.camera_side == "on_axis":
        return True
    if stage.camera_side == "cross" and stage.cross_reason.strip():
        return True
    if previous is not None and previous.camera_side == "on_axis":
        return False
    return False


def _resets_line(stage: ShotStage) -> bool:
    return stage.camera_side == "on_axis"


def _pair_flipped(left: list[Any], right: list[Any]) -> bool:
    after = {block.id: block for block in right}
    for block in left:
        other = after.get(block.id)
        if other is None or not block.visible or not other.visible:
            continue
        if _screen_side(block.x) == "center" or _screen_side(other.x) == "center":
            continue
        if _screen_side(block.x) != _screen_side(other.x):
            return True
    return False


def _travel_reversed(left: list[Any], right: list[Any]) -> bool:
    after = {block.id: block for block in right}
    for block in left:
        other = after.get(block.id)
        if other is None or not block.visible or not other.visible:
            continue
        opposite = _OPPOSITE_TRAVEL.get(block.travel)
        if opposite and other.travel == opposite:
            return True
    return False


def _screen_side(x: str) -> str:
    if x in _LEFT:
        return "left"
    if x in _RIGHT:
        return "right"
    return "center"


def _is_behind(trailer: Any, lead: Any, travel: str) -> bool:
    tx, lx = _X_INDEX.get(trailer.x, 3), _X_INDEX.get(lead.x, 3)
    td, ld = _DEPTH_INDEX.get(trailer.depth, 1), _DEPTH_INDEX.get(lead.depth, 1)
    if travel == "screen_right":
        return tx < lx or (tx == lx and td > ld)
    if travel == "screen_left":
        return tx > lx or (tx == lx and td > ld)
    if travel == "toward_camera":
        return td > ld
    if travel == "away_from_camera":
        return td < ld
    return td > ld


def normalize_pursuer_fall_back(
    text: str,
    pursuers: list[str],
    leads: list[str],
    *,
    story: str = "",
) -> str:
    """Rewrite pursuer 'fall back' / 'fallen back' so the still model keeps them riding.

    The phrase is replaced only when the nearer party in that sentence is a
    pursuer. The lead can still fall back. When ``story`` itself says the
    pursuers stop or come off the horse, the line is left alone.
    """
    if not text or not pursuers or not _FALL_BACK.search(text):
        return text
    if story and _STORY_ALLOWS_STOP.search(story):
        return text
    parts = re.split(r"([.!?])", text)
    out: list[str] = []
    index = 0
    while index < len(parts):
        chunk = parts[index]
        punct = parts[index + 1] if index + 1 < len(parts) else ""
        index += 2 if punct else 1
        out.append(_rewrite_fall_back_sentence(chunk, pursuers, leads) + punct)
    return "".join(out)


def _rewrite_fall_back_sentence(sentence: str, pursuers: list[str], leads: list[str]) -> str:
    def replacer(match: re.Match[str]) -> str:
        before = sentence[: match.start()]
        if _nearer_party(before, pursuers, leads) == "pursuer":
            return "drop farther behind, still riding"
        return match.group(0)

    return _FALL_BACK.sub(replacer, sentence)


def _nearer_party(before: str, pursuers: list[str], leads: list[str]) -> str:
    low = before.lower()
    last_pursuer = _last_name_at(low, pursuers)
    last_lead = _last_name_at(low, leads)
    if last_pursuer < 0 and last_lead < 0:
        return ""
    if last_pursuer > last_lead:
        return "pursuer"
    return "lead"


def _last_name_at(low: str, names: list[str]) -> int:
    found = -1
    for name in names:
        token = name.strip().lower()
        if len(token) < 3:
            continue
        at = low.rfind(token)
        if at > found:
            found = at
    return found


def _party_names(staging: StagingMap) -> tuple[list[str], list[str]]:
    pursuers: list[str] = []
    leads: list[str] = []
    for scene in staging.scenes:
        entities = {item.id: item for item in scene.entities}
        for relation in scene.relations:
            if relation.rel != "behind":
                continue
            _add_party_names(pursuers, _label(entities, relation.a), relation.a)
            _add_party_names(leads, _label(entities, relation.b), relation.b)
        if any(relation.rel == "behind" for relation in scene.relations):
            _add_party_names(pursuers, "the pursuers", "pursuers")
    return pursuers, leads


def _add_party_names(bucket: list[str], label: str, entity_id: str) -> None:
    for name in (label, entity_id.replace("_", " ")):
        cleaned = name.strip()
        if cleaned and cleaned not in bucket:
            bucket.append(cleaned)
    for hint in ("bandit", "bandits", "posse", "outlaw", "outlaws", "pursuer", "pursuers"):
        if hint in label.lower() and hint not in bucket:
            bucket.append(hint)


def _normalize_draft_prose(draft: dict[str, Any], staging: StagingMap, prompt: str) -> None:
    pursuers, leads = _party_names(staging)
    if not pursuers:
        return

    def rewrite(value: object) -> str:
        return normalize_pursuer_fall_back(str(value or ""), pursuers, leads, story=prompt)

    shots = draft.get("shots")
    if isinstance(shots, list):
        for shot in shots:
            if not isinstance(shot, dict):
                continue
            for field in ("prompt_still", "prompt_motion", "start_state", "end_state"):
                if field in shot:
                    shot[field] = rewrite(shot.get(field))
            camera = shot.get("camera")
            if isinstance(camera, dict) and "exit_frame" in camera:
                camera["exit_frame"] = rewrite(camera.get("exit_frame"))
    beat_map = draft.get("beat_map")
    if isinstance(beat_map, list):
        for item in beat_map:
            if isinstance(item, dict) and "summary" in item:
                item["summary"] = rewrite(item.get("summary"))


def _ensure_lookback_motion(shots: list[Any], staging: StagingMap, style: str) -> None:
    scenes = {scene.id: scene for scene in staging.scenes}
    for shot in shots:
        if not isinstance(shot, dict):
            continue
        stage = shot.get("stage")
        if not isinstance(stage, dict):
            continue
        scene = scenes.get(str(stage.get("scene_id") or ""))
        if scene is None and len(staging.scenes) == 1:
            scene = staging.scenes[0]
        entities = {item.id: item for item in (scene.entities if scene else [])}
        sentence = ""
        # stage.end is the action of this shot. stage.start is the previous
        # shot's exit, so a look-back that only opens the frame is not repeated.
        for block in stage.get("end") or []:
            if not isinstance(block, dict):
                continue
            look = str(block.get("look") or "")
            travel = str(block.get("travel") or "")
            if not _looks_back(look, travel) or not _is_mounted_rider(block, entities, style):
                continue
            sentence = _capitalize_sentences(_twist_sentence(look, travel) + ".")
            break
        if not sentence:
            continue
        motion = str(shot.get("prompt_motion") or "")
        if "twists at the waist in the saddle" in motion.lower():
            continue
        shot["prompt_motion"] = f"{motion.rstrip().rstrip('.')}. {sentence}".strip()


def _keep_pursuers_riding(shots: list[Any], staging: StagingMap, prompt: str) -> None:
    """Final-shot pursuers stay mounted unless the story says they stop."""
    if _STORY_ALLOWS_STOP.search(prompt or ""):
        return
    scenes = {scene.id: scene for scene in staging.scenes}
    for shot in shots:
        if not isinstance(shot, dict) or str(shot.get("beat") or "") != "button":
            continue
        stage = shot.get("stage")
        if not isinstance(stage, dict):
            continue
        scene = scenes.get(str(stage.get("scene_id") or ""))
        if scene is None and len(staging.scenes) == 1:
            scene = staging.scenes[0]
        if scene is None:
            continue
        travel = scene.travel if scene.travel and scene.travel != "static" else "screen_right"
        pursuers = _pursuer_ids(list(stage.get("relations") or []) + list(scene.relations))
        for block in stage.get("end") or []:
            if not isinstance(block, dict) or block.get("id") not in pursuers:
                continue
            if block.get("travel") == "static":
                block["travel"] = travel


def _strip_scene_mounts(staging: StagingMap) -> None:
    for scene in staging.scenes:
        entities = {item.id: item for item in scene.entities}
        scene.relations = [
            item for item in scene.relations if not _is_self_mount_relation(item, entities)
        ]


def _strip_shot_mounts(shots: list[Any], staging: StagingMap) -> None:
    scenes = {scene.id: scene for scene in staging.scenes}
    for shot in shots:
        if not isinstance(shot, dict):
            continue
        stage = shot.get("stage")
        if not isinstance(stage, dict):
            continue
        scene = scenes.get(str(stage.get("scene_id") or ""))
        if scene is None and len(staging.scenes) == 1:
            scene = staging.scenes[0]
        entities = {item.id: item for item in (scene.entities if scene else [])}
        stage["relations"] = [
            item
            for item in stage.get("relations") or []
            if not _is_self_mount_relation(item, entities)
        ]


def _looks_back(look: str, travel: str) -> bool:
    if not look or look == travel or travel == "static":
        return False
    return _OPPOSITE_TRAVEL.get(travel) == look


def _twist_sentence(look: str, travel: str) -> str:
    side = _LOOK_PHRASE.get(look, look.replace("_", " "))
    way = _LOOK_PHRASE.get(travel, travel.replace("_", " "))
    return (
        "twists at the waist in the saddle, head and shoulders turned toward "
        f"{side} to face the pursuers, revolver arm extended back toward them, "
        f"horse keeps galloping toward {way} in side profile"
    )


def _lookback_sentence(blocks: list[Any], entities: dict[str, Any], style: str) -> str:
    for block in blocks:
        look = str(_value(block, "look") or "")
        travel = str(_value(block, "travel") or "")
        if _looks_back(look, travel) and _is_mounted_rider(block, entities, style):
            label = _label(entities, str(_value(block, "id") or ""))
            return _capitalize_sentences(f"{label} {_twist_sentence(look, travel)}.")
    return ""


def _pursuer_profile(
    block: Any,
    entities: dict[str, Any],
    relations: list[Any],
    style: str,
) -> str:
    travel = str(_value(block, "travel") or "")
    if travel not in {"screen_left", "screen_right"}:
        return ""
    if not _is_pursuer(str(_value(block, "id") or ""), relations):
        return ""
    if not _scene_is_mounted(block, entities, style):
        return ""
    side = _LOOK_PHRASE.get(travel, travel.replace("_", " "))
    return f"horses in side profile, galloping toward {side}"


def _scene_is_mounted(block: Any, entities: dict[str, Any], style: str) -> bool:
    if style == "chase":
        return True
    blobs = [_label(entities, str(_value(block, "id") or ""))]
    blobs.extend(_label(entities, entity_id) for entity_id in entities)
    return any(_HORSE_PARTY.search(blob) for blob in blobs)


def _is_mounted_rider(block: Any, entities: dict[str, Any], style: str) -> bool:
    entity_id = str(_value(block, "id") or "")
    entity = entities.get(entity_id)
    if _is_mount_entity(entity):
        return False
    if style == "chase":
        return True
    label = _label(entities, entity_id)
    return _HORSE_PARTY.search(label) is not None


def _is_pursuer(entity_id: str, relations: list[Any]) -> bool:
    return entity_id in _pursuer_ids(relations)


def _pursuer_ids(relations: list[Any]) -> set[str]:
    found: set[str] = set()
    for item in relations:
        left, rel, _right = _rel_parts(item)
        if rel == "behind" and left:
            found.add(left)
    return found


def _self_mount_ids(relations: list[Any], entities: dict[str, Any]) -> set[str]:
    found: set[str] = set()
    mapped = _entity_map(entities)
    for item in relations:
        if not _is_self_mount_relation(item, mapped):
            continue
        left, _rel, right = _rel_parts(item)
        if _is_mount_entity(mapped.get(left)):
            found.add(left)
        if _is_mount_entity(mapped.get(right)):
            found.add(right)
    return found


def _self_mount_riders(relations: list[Any], entities: dict[str, Any]) -> set[str]:
    found: set[str] = set()
    mapped = _entity_map(entities)
    for item in relations:
        if not _is_self_mount_relation(item, mapped):
            continue
        left, _rel, right = _rel_parts(item)
        if not _is_mount_entity(mapped.get(left)):
            found.add(left)
        if not _is_mount_entity(mapped.get(right)):
            found.add(right)
    return found


def _is_self_mount_relation(relation: Any, entities: dict[str, Any]) -> bool:
    """True when the relation ties a rider to that rider's own horse."""
    left_id, rel, right_id = _rel_parts(relation)
    mapped = _entity_map(entities)
    left = mapped.get(left_id)
    right = mapped.get(right_id)
    if left is None or right is None:
        return False
    if _is_mount_entity(left) and not _is_mount_entity(right):
        mount, rider = left, right
    elif _is_mount_entity(right) and not _is_mount_entity(left):
        mount, rider = right, left
    else:
        return False
    if _belongs_to_rider(mount, rider):
        return True
    _label_text, _eid, kind = _entity_bits(rider)
    return rel == "beside" and kind == "character"


def _is_mount_entity(entity: Any) -> bool:
    if entity is None:
        return False
    label, eid, kind = _entity_bits(entity)
    if kind == "group" or _ASTRIDE.search(label):
        return False
    return _MOUNT_WORD.search(f"{label} {eid.replace('_', ' ')}") is not None


def _belongs_to_rider(mount: Any, rider: Any) -> bool:
    mount_label, mount_id, _mount_kind = _entity_bits(mount)
    rider_label, rider_id, _rider_kind = _entity_bits(rider)
    blob = f"{mount_label} {mount_id}".lower().replace("_", " ").replace("-", " ")
    rider_key = rider_id.lower().replace("-", "_")
    if rider_key and rider_key in mount_id.lower().replace("-", "_"):
        return True
    rider_words = rider_id.lower().replace("_", " ").replace("-", " ")
    if rider_words and rider_words in blob:
        return True
    for bit in re.findall(r"[a-z0-9']+", rider_label.lower()):
        if bit in _NAME_STOP or len(bit) < 3:
            continue
        if bit in blob:
            return True
    return re.search(r"\b(?:his|her|their)\b", mount_label, re.IGNORECASE) is not None


def _entity_map(entities: dict[str, Any]) -> dict[str, Any]:
    if not entities:
        return {}
    mapped: dict[str, Any] = {}
    for key, entity in entities.items():
        mapped[str(key)] = entity
        eid = _entity_bits(entity)[1]
        if eid:
            mapped[eid] = entity
    return mapped


def _entity_bits(entity: Any) -> tuple[str, str, str]:
    if isinstance(entity, dict):
        return (
            str(entity.get("label") or ""),
            str(entity.get("id") or ""),
            str(entity.get("kind") or ""),
        )
    return (
        str(getattr(entity, "label", "") or ""),
        str(getattr(entity, "id", "") or ""),
        str(getattr(entity, "kind", "") or ""),
    )


def _rel_parts(relation: Any) -> tuple[str, str, str]:
    if isinstance(relation, dict):
        return (
            str(relation.get("a") or ""),
            str(relation.get("rel") or ""),
            str(relation.get("b") or ""),
        )
    return (str(relation.a), str(relation.rel), str(relation.b))


def _value(item: Any, name: str) -> Any:
    if isinstance(item, dict):
        return item.get(name)
    return getattr(item, name, None)


def _capitalize_sentences(text: str) -> str:
    return _SENTENCE_START.sub(lambda match: match.group(1) + match.group(2).upper(), text)


def _polish_clause(text: str) -> str:
    polished: list[str] = []
    for line in text.split("\n"):
        if line.startswith("- "):
            polished.append(_AFTER_PERIOD.sub(lambda match: match.group(1).upper(), line))
        elif line == STAGING_HEADER:
            polished.append(line)
        else:
            polished.append(_capitalize_sentences(line))
    return "\n".join(polished)


def _signature(blocks: list[Any]) -> tuple[tuple[Any, ...], ...]:
    rows = [
        (block.id, block.x, block.depth, block.facing, block.look, block.travel, block.visible)
        for block in blocks
    ]
    return tuple(sorted(rows))
