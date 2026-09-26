"""Show the prompts and call counts a run would send, before any xAI call.

This module does not construct an Imagine client, does not touch the network,
and does not persist a pack. Prompt text comes from the same builders the
pipeline uses. Issue checks are adapted from AIGC Studio's CLIP_BRIDGE
validators (``verbs_in``, ``camera_issue``, ``prompt_text_issues``,
``handoff_equal``). H3-only rules (10.00s, 24 fps, frame 240, 1344x768)
are not applied.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field

from omarchy_imagine.castref import image_tag_line, select_cast, video_reference_line
from omarchy_imagine.config import MAX_REFERENCE_IMAGES, reference_video_resolution
from omarchy_imagine.ffmpeg_util import ffmpeg_path
from omarchy_imagine.pipeline import build_motion_prompt, build_still_prompt
from omarchy_imagine.schema import PackIn, render_look_bible
from omarchy_imagine.staging import check_staging

Severity = Literal["block", "warn", "info"]

SEED_FRAME_NOTE = "seed frame is the previous clip's last frame at run time"

# CLIP_BRIDGE verb bank. One action verb per clip; we warn only when a motion
# prompt contains more than one.
VERB_BANK: tuple[str, ...] = (
    "walks",
    "turns",
    "raises",
    "lowers",
    "strikes",
    "sheathes",
    "unsheathes",
    "opens",
    "closes",
    "hands-off",
    "picks-up",
    "looks-up",
    "sits",
    "stands",
    "crosses-threshold",
    "mounts",
    "dismounts",
    "kindles",
    "quenches",
    "wipes-brow",
    "exhales",
)

# Editorial cuts. ``[shot N]`` is the same class as the literal ``[shot 2]``.
_BANNED_CUTS = ("cut to", "smash cut", "dissolve", "fade to", "jump cut", "match cut", "wipe to")
_SHOT_TAG = re.compile(r"\[shot\s*\d+\]", re.IGNORECASE)

# Camera families, adapted from CLIP_BRIDGE ``_MOVE`` plus this app's move card.
_FAMILY = (
    (
        "push",
        re.compile(
            r"\b(?:dolly(?:ing)?[\s-]in|dollies in|push(?:es|ing)?(?:\s+in)?"
            r"|zoom(?:s|ing)?\s+in)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "pull",
        re.compile(
            r"\b(?:dolly(?:ing)?[\s-]out|dollies out|pull(?:s|ing)?(?:\s+back)?"
            r"|zoom(?:s|ing)?\s+out)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "track",
        re.compile(r"\b(?:tracks?|tracking|pans?|panning|whip[\s-]?pans?)\b", re.IGNORECASE),
    ),
    (
        "arc",
        re.compile(r"\b(?:arcs?|arcing|orbits?|orbiting)\b", re.IGNORECASE),
    ),
    (
        "tilt",
        re.compile(r"\b(?:tilts?|tilting|pedestals?)\b", re.IGNORECASE),
    ),
    ("handheld", re.compile(r"\bhandheld\b", re.IGNORECASE)),
    (
        "static",
        re.compile(r"\b(?:holds static|stays static|locked(?:\s|-)?off|lock off)\b", re.IGNORECASE),
    ),
)
_CARD_FAMILY = {
    "dolly_in": "push",
    "dolly_out": "pull",
    "orbit": "arc",
    "pan": "track",
    "tilt": "tilt",
    "whip_pan": "track",
    "handheld": "handheld",
    "static": "static",
}
_OPPOSED = frozenset({"push", "pull"})

_ANCHOR_STOP = frozenset(
    {
        "the",
        "and",
        "with",
        "same",
        "every",
        "shot",
        "shots",
        "from",
        "that",
        "this",
        "into",
        "over",
        "under",
        "between",
        "unchanged",
        "held",
        "constant",
        "film",
        "still",
        "natural",
        "color",
        "grade",
        "flicker",
        "lens",
        "change",
        "face",
        "body",
        "type",
        "one",
        "for",
        "all",
        "across",
        "keep",
        "kept",
        "their",
        "there",
        "these",
        "those",
        "each",
        "both",
        "only",
        "also",
        "have",
        "has",
        "been",
        "being",
        "were",
        "was",
        "are",
        "not",
        "but",
        "its",
        "his",
        "her",
        "she",
        "him",
        "who",
        "what",
        "when",
        "where",
        "which",
        "while",
        "than",
        "then",
        "them",
        "they",
        "your",
        "you",
        "our",
        "out",
        "off",
        "any",
        "can",
        "will",
        "just",
        "more",
        "most",
        "some",
        "such",
        "very",
        "without",
        "within",
        "during",
        "before",
        "after",
        "about",
        "again",
        "once",
        "onto",
        "upon",
        "through",
        "toward",
        "towards",
    }
)

# Descriptive words the planner varies from shot to shot. They are not identity.
_GENERIC = frozenset(
    {
        "long",
        "short",
        "tall",
        "wide",
        "narrow",
        "big",
        "small",
        "large",
        "little",
        "high",
        "low",
        "deep",
        "soft",
        "hard",
        "warm",
        "cool",
        "cold",
        "hot",
        "bright",
        "dark",
        "light",
        "heavy",
        "thin",
        "thick",
        "old",
        "young",
        "new",
        "worn",
        "dusty",
        "dirty",
        "clean",
        "rough",
        "smooth",
        "sharp",
        "dull",
        "pale",
        "rich",
        "muted",
        "bare",
        "lone",
        "full",
        "open",
        "closed",
        "faded",
        "weathered",
        "lean",
        "raw",
        "plain",
        "early",
        "late",
        "strong",
        "weak",
        "quick",
        "slow",
        "fast",
        "rugged",
        "grizzled",
        "colored",
        "coloured",
        "slight",
        "slightly",
        "natural",
        "visual",
        "cinematic",
    }
)
_GENERIC_NOUNS = frozenset(
    {
        "shadow",
        "shadows",
        "sun",
        "sunlight",
        "sunset",
        "sundown",
        "sunrise",
        "dawn",
        "dusk",
        "sky",
        "skies",
        "dust",
        "haze",
        "fog",
        "lighting",
        "contrast",
        "grade",
        "palette",
        "color",
        "colors",
        "colour",
        "colours",
        "shot",
        "shots",
        "frame",
        "frames",
        "scene",
        "film",
        "lens",
        "camera",
        "clothes",
        "clothing",
        "outfit",
        "garment",
        "garments",
        "wear",
        "wearing",
        "dressed",
        "figure",
        "person",
        "people",
        "man",
        "woman",
        "rider",
        "riders",
        "horse",
        "horses",
        "face",
        "body",
        "type",
        "look",
        "style",
        "image",
        "unchanged",
        "constant",
        "direction",
        "hour",
        "first",
        "last",
        "ground",
        "desert",
        "trail",
        "light",
        "lights",
    }
)
# A bare palette list is not a lock. These count only from wardrobe, markers,
# or an explicit "locked <color>" / "the same <color>" phrase.
_COLOR_WORDS = frozenset(
    {
        "red",
        "blue",
        "rust",
        "gold",
        "indigo",
        "amber",
        "sand",
        "crimson",
        "scarlet",
        "navy",
        "black",
        "white",
        "brown",
        "green",
        "grey",
        "gray",
        "tan",
        "ochre",
        "ocher",
        "copper",
        "bronze",
        "silver",
        "yellow",
        "orange",
        "purple",
        "violet",
        "pink",
        "maroon",
        "burgundy",
        "cream",
        "ivory",
        "charcoal",
        "olive",
        "teal",
        "khaki",
        "denim",
        "chestnut",
        "buckskin",
        "blonde",
        "blond",
        "auburn",
        "cyan",
        "beige",
        "pearl",
        "cobalt",
        "azure",
        "sienna",
        "umber",
        "sepia",
        "turquoise",
        "lavender",
        "coral",
    }
)
_EXPLICIT_COLOR = re.compile(
    r"\b(?:locked|unchanged|always)\s+([A-Za-z]+)"
    r"|\b([A-Za-z]+)\s+(?:locked|unchanged)\b"
    r"|\bsame\s+([A-Za-z]+)\b",
    re.IGNORECASE,
)
_WORD = re.compile(r"[A-Za-z][A-Za-z'-]*")


@dataclass(frozen=True)
class _Member:
    """Display-cased names. ``keys`` is the lowercase set used for matching."""

    names: tuple[str, ...]

    @property
    def keys(self) -> frozenset[str]:
        return frozenset(name.lower() for name in self.names)


@dataclass(frozen=True)
class _Anchor:
    """One identity word and the cast names it belongs to.

    Empty ``owners`` means the lock is pack-wide. Owners are lowercase.
    """

    word: str
    owners: frozenset[str]


class PreflightIssue(BaseModel):
    code: str
    severity: Severity
    message: str


class PreflightShot(BaseModel):
    id: str
    still_prompt: str
    motion_prompt: str
    still_mode_expected: str
    video_mode: str
    issues: list[PreflightIssue] = Field(default_factory=list)


class PreflightTotals(BaseModel):
    stills: int
    videos: int
    video_seconds: int


class PreflightOut(BaseModel):
    shots: list[PreflightShot]
    totals: PreflightTotals
    blocking: bool


def handoff_equal(left: str, right: str) -> bool:
    """CLIP_BRIDGE compare: strip only. No case fold, no fuzzy match."""
    return left.strip() == right.strip()


def verbs_in(action: str) -> list[str]:
    """Action verbs from the CLIP_BRIDGE bank, longest match winning on overlap."""
    text = action.strip().lower()
    hits: list[tuple[int, int, str]] = []
    for verb in VERB_BANK:
        pattern = re.compile(rf"(?<![a-z-]){re.escape(verb)}(?![a-z-])")
        for match in pattern.finditer(text):
            hits.append((match.start(), match.end(), verb))
    hits.sort(key=lambda item: (item[0], -(item[1] - item[0])))
    kept: list[tuple[int, int, str]] = []
    for start, end, verb in hits:
        overlaps = any(
            not (end <= prev_start or start >= prev_end) for prev_start, prev_end, _verb in kept
        )
        if overlaps:
            continue
        kept.append((start, end, verb))
    kept.sort()
    return [verb for _start, _end, verb in kept]


def preflight(pack: PackIn) -> dict[str, Any]:
    """Assemble the run's prompts and flag obvious problems. No network, no save."""
    ffmpeg_ready = ffmpeg_path() is not None
    bible = render_look_bible(pack.look_bible)
    cast = [item.model_dump() for item in pack.cast]
    anchors = _anchor_words(pack)
    established: set[str] = set()
    shots: list[PreflightShot] = []
    for index, shot in enumerate(pack.shots):
        seeded, mode, image_note = _still_plan(index, cast, ffmpeg_ready)
        shot_dict = shot.model_dump()
        reference_note = ""
        if shot.video_mode == "reference_to_video":
            reference_note = video_reference_line(select_cast(cast, MAX_REFERENCE_IMAGES))
        still_prompt = build_still_prompt(
            shot_dict,
            seeded=seeded,
            look_bible=bible,
            cast=cast,
            image_note=image_note,
            pack=pack,
        )
        if seeded:
            still_prompt = f"{still_prompt}\n\n{SEED_FRAME_NOTE}"
        motion_prompt = build_motion_prompt(
            shot_dict,
            look_bible=bible,
            cast=cast,
            reference_note=reference_note,
            pack=pack,
        )
        issues = _shot_issues(pack, index, anchors, established)
        shots.append(
            PreflightShot(
                id=shot.id,
                still_prompt=still_prompt,
                motion_prompt=motion_prompt,
                still_mode_expected=mode,
                video_mode=shot.video_mode,
                issues=issues,
            )
        )
    stills = [shot.still_prompt for shot in shots]
    motions = [shot.motion_prompt for shot in shots]
    for item in check_staging(pack, still_prompts=stills, motion_prompts=motions):
        shots[item.shot_index].issues.append(
            PreflightIssue(code=item.code, severity=item.severity, message=item.message)
        )
    blocking = any(issue.severity == "block" for shot in shots for issue in shot.issues)
    report = PreflightOut(
        shots=shots,
        totals=PreflightTotals(
            stills=len(pack.shots),
            videos=len(pack.shots),
            video_seconds=sum(shot.duration_sec for shot in pack.shots),
        ),
        blocking=blocking,
    )
    return report.model_dump()


def _still_plan(
    index: int,
    cast: list[dict[str, Any]],
    ffmpeg_ready: bool,
) -> tuple[bool, str, str]:
    """Match ``_still_sources``: text, cast edit, or last-frame edit."""
    seeded = index > 0 and ffmpeg_ready
    chosen = select_cast(cast, MAX_REFERENCE_IMAGES)
    if seeded and chosen:
        used = chosen[: MAX_REFERENCE_IMAGES - 1]
        return True, "last_frame_edit", image_tag_line(used, last_frame=True)
    if seeded:
        return True, "last_frame_edit", ""
    if chosen:
        used = chosen[:MAX_REFERENCE_IMAGES]
        return False, "cast_reference", image_tag_line(used, last_frame=False)
    return False, "text_to_image", ""


def _shot_issues(
    pack: PackIn,
    index: int,
    anchors: list[_Anchor],
    established: set[str],
) -> list[PreflightIssue]:
    shot = pack.shots[index]
    issues: list[PreflightIssue] = []
    if index > 0:
        previous = pack.shots[index - 1]
        if not handoff_equal(shot.start_state, previous.end_state):
            issues.append(
                PreflightIssue(
                    code="handoff_state",
                    severity="block",
                    message=(
                        f"Shot {shot.id} start_state is not shot {previous.id} end_state "
                        "after strip. A paraphrase is a fail."
                    ),
                )
            )
    verbs = verbs_in(shot.prompt_motion)
    if len(verbs) > 1:
        listed = ", ".join(verbs)
        issues.append(
            PreflightIssue(
                code="verb_count",
                severity="warn",
                message=(
                    f"Shot {shot.id} motion prompt has {len(verbs)} action verbs ({listed}). "
                    "Use one."
                ),
            )
        )
    camera = _camera_conflict(shot.prompt_motion, shot.camera.move, shot.id)
    if camera is not None:
        issues.append(camera)
    banned = _banned_cuts(shot.prompt_still, shot.prompt_motion)
    if banned:
        issues.append(
            PreflightIssue(
                code="banned_cut",
                severity="warn",
                message=(
                    f"Shot {shot.id} prompt contains editorial cut language: {', '.join(banned)}."
                ),
            )
        )
    # Generic adjectives and palette lists are not locks. Warn only when a
    # name, wardrobe item, or explicitly locked color is missing from a shot
    # where that character appears, after an earlier shot established it.
    if index > 0 and anchors:
        drifted = [
            anchor.word
            for anchor in anchors
            if anchor.word.lower() in established
            and not _contains_word(shot.prompt_still, anchor.word)
            and _anchor_relevant(pack, shot, anchor)
        ]
        if drifted:
            shown = drifted[:6]
            extra = len(drifted) - len(shown)
            suffix = f" and {extra} more" if extra else ""
            issues.append(
                PreflightIssue(
                    code="lock_drift",
                    severity="warn",
                    message=(
                        f"Shot {shot.id} still prompt changes locked look-bible or cast "
                        f"anchors from the previous shot: {', '.join(shown)}{suffix}."
                    ),
                )
            )
    for anchor in anchors:
        if _contains_word(shot.prompt_still, anchor.word):
            established.add(anchor.word.lower())
    if pack.resolution == "1080p" and shot.video_mode == "reference_to_video":
        _sent, note = reference_video_resolution(pack.resolution)
        issues.append(
            PreflightIssue(
                code="r2v_resolution",
                severity="info",
                message=note or "Reference-to-video renders at 720p.",
            )
        )
    return issues


def _camera_conflict(motion: str, move: str, shot_id: str) -> PreflightIssue | None:
    """Warn when the motion text and ``camera.move`` name opposing moves."""
    families = _families_in(motion)
    card = _CARD_FAMILY.get(move, "")
    combined = set(families)
    if card:
        combined.add(card)
    text = motion.lower()
    # A look, aim, or twist toward the other side is not a travel reversal.
    travel_text = re.sub(
        r"[^.]*\b(?:looks?|looking|aims?|aiming|twists?|twisting|faces?|facing|glances?)\b[^.]*",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    reverses = ("left-to-right" in travel_text and "right-to-left" in travel_text) or (
        "camera-left" in travel_text and "camera-right" in travel_text
    )
    tilts_both = bool(re.search(r"\btilt(?:s|ing)? up\b", text)) and bool(
        re.search(r"\btilt(?:s|ing)? down\b", text)
    )
    opposed = _OPPOSED <= combined
    disagrees = bool(card and families and families != {card})
    if not (opposed or reverses or tilts_both or disagrees):
        return None
    if opposed:
        detail = f"the prompt includes pull while camera.move is {move or 'unset'} (push/pull)"
        if "push" in families and "pull" in families:
            detail = "the motion text stacks push and pull"
        elif card == "push":
            detail = f"the motion text pulls while camera.move is {move}"
        elif card == "pull":
            detail = f"the motion text pushes while camera.move is {move}"
    elif reverses:
        detail = "the motion text reverses screen direction"
    elif tilts_both:
        detail = "the motion text tilts up and down"
    else:
        named = ", ".join(sorted(families))
        detail = f"the motion text names {named} while camera.move is {move}"
    return PreflightIssue(
        code="camera_conflict",
        severity="warn",
        message=f"Shot {shot_id} camera conflicts: {detail}.",
    )


def _families_in(text: str) -> set[str]:
    found: set[str] = set()
    for name, pattern in _FAMILY:
        if pattern.search(text):
            found.add(name)
    return found


def _banned_cuts(*parts: str) -> list[str]:
    blob = "\n".join(parts).lower()
    hits = [phrase for phrase in _BANNED_CUTS if phrase in blob]
    tags = [match.group(0).lower() for match in _SHOT_TAG.finditer(blob)]
    seen: list[str] = []
    for item in [*hits, *tags]:
        if item not in seen:
            seen.append(item)
    return seen


def _anchor_words(pack: PackIn) -> list[_Anchor]:
    """Names, wardrobe items, and explicitly locked colors. Not palette filler."""
    members = _cast_members(pack)
    anchors: list[_Anchor] = []
    seen: set[str] = set()

    def add(word: str, owners: frozenset[str]) -> None:
        bare = _bare_token(word)
        key = bare.lower()
        if len(key) < 2 or key in _ANCHOR_STOP or key in seen:
            return
        seen.add(key)
        anchors.append(_Anchor(word=bare, owners=owners))

    for member in members:
        for name in member.names:
            add(name, member.keys)
    for ref in pack.cast:
        owners = _owners_matching(ref.name, members)
        for token in _content_tokens(ref.markers):
            add(token, owners)
    for word, owners in _wardrobe_anchors(pack.look_bible.wardrobe, members):
        add(word, owners)
    for field in (pack.look_bible.palette, pack.look_bible.lighting):
        for token in _explicit_color_tokens(field):
            add(token, _owners_matching(field, members))
    return anchors


def _cast_members(pack: PackIn) -> list[_Member]:
    members: list[_Member] = []
    for ref in pack.cast:
        names = _unique_names(_name_tokens(ref.name))
        if names:
            members.append(_Member(names=names))
    bible = pack.look_bible.cast
    extra = [
        _bare_token(token)
        for token in _tokens(bible)
        if _bare_token(token)[:1].isupper() and not _skip_name(_bare_token(token))
    ]
    if not extra:
        return members
    mentioned = [
        member for member in members if any(_contains_word(bible, name) for name in member.names)
    ]
    if len(mentioned) == 1:
        merged = _unique_names([*mentioned[0].names, *extra])
        return [
            _Member(names=merged) if member is mentioned[0] else member for member in members
        ]
    known = {key for member in members for key in member.keys}
    for token in extra:
        key = token.lower()
        if key in known:
            continue
        members.append(_Member(names=(token,)))
        known.add(key)
    return members


def _unique_names(tokens: list[str]) -> tuple[str, ...]:
    names: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        key = token.lower()
        if not key or key in seen:
            continue
        seen.add(key)
        names.append(token)
    return tuple(names)


def _wardrobe_anchors(text: str, members: list[_Member]) -> list[tuple[str, frozenset[str]]]:
    if not text.strip():
        return []
    if len(members) <= 1:
        owners = members[0].keys if members else frozenset()
        return [(word, owners) for word in _content_tokens(text)]
    found: list[tuple[str, frozenset[str]]] = []
    for chunk in re.split(r";|\band\b", text):
        owners = _owners_matching(chunk, members)
        for word in _content_tokens(chunk):
            found.append((word, owners))
    return found


def _owners_matching(text: str, members: list[_Member]) -> frozenset[str]:
    matched: list[str] = []
    for member in members:
        if any(_contains_word(text, name) for name in member.keys):
            matched.extend(member.keys)
    return frozenset(matched)


def _content_tokens(text: str) -> list[str]:
    words: list[str] = []
    seen: set[str] = set()
    for token in _tokens(text):
        for piece in _bare_token(token).split("-"):
            bare = piece.strip("'")
            key = bare.lower()
            if len(key) < 3 or key in seen:
                continue
            if key in _ANCHOR_STOP or key in _GENERIC or key in _GENERIC_NOUNS:
                continue
            seen.add(key)
            words.append(bare)
    return words


def _explicit_color_tokens(text: str) -> list[str]:
    words: list[str] = []
    seen: set[str] = set()
    for match in _EXPLICIT_COLOR.finditer(text or ""):
        raw = next(group for group in match.groups() if group)
        key = raw.lower()
        if key not in _COLOR_WORDS or key in seen:
            continue
        seen.add(key)
        words.append(raw)
    return words


def _name_tokens(text: str) -> list[str]:
    names: list[str] = []
    for token in _tokens(text):
        bare = _bare_token(token)
        if _skip_name(bare):
            continue
        names.append(bare)
    return names


def _skip_name(token: str) -> bool:
    key = token.lower()
    return len(key) < 2 or key in _ANCHOR_STOP


def _tokens(text: str) -> list[str]:
    return _WORD.findall(text or "")


def _bare_token(token: str) -> str:
    cleaned = token.strip("'")
    if cleaned.lower().endswith("'s"):
        cleaned = cleaned[:-2]
    return cleaned.strip("'")


def _anchor_relevant(pack: PackIn, shot: Any, anchor: _Anchor) -> bool:
    """True when this still is a picture that should keep the lock.

    With no stage, the look bible applies to every shot. With a stage, a
    character's lock matters only when that character is in the frame.
    """
    stage = getattr(shot, "stage", None)
    if stage is None:
        return True
    if _owner_in_text(shot.prompt_still, anchor, pack):
        return True
    return _owner_visible(pack, shot, anchor)


def _owner_in_text(text: str, anchor: _Anchor, pack: PackIn) -> bool:
    names = anchor.owners or _all_cast_names(pack)
    if not names:
        return False
    return any(_contains_word(text, name) for name in names)


def _all_cast_names(pack: PackIn) -> frozenset[str]:
    names: set[str] = set()
    for member in _cast_members(pack):
        names.update(member.keys)
    return frozenset(names)


def _owner_visible(pack: PackIn, shot: Any, anchor: _Anchor) -> bool:
    stage = shot.stage
    blocks = list(stage.start or stage.end)
    entities = _scene_entities(pack, shot)
    if not anchor.owners:
        if any(block.visible for block in blocks):
            return True
        return _owner_in_text(shot.prompt_still, anchor, pack)
    for block in blocks:
        if _block_matches_owner(block, entities, anchor.owners, pack):
            return True
    return False


def _scene_entities(pack: PackIn, shot: Any) -> dict[str, Any]:
    staging = pack.staging
    stage = shot.stage
    if staging is None or stage is None:
        return {}
    scene = None
    if stage.scene_id:
        scene = next((item for item in staging.scenes if item.id == stage.scene_id), None)
    if scene is None:
        scene = next((item for item in staging.scenes if shot.id in item.shot_ids), None)
    if scene is None and len(staging.scenes) == 1:
        scene = staging.scenes[0]
    if scene is None:
        return {}
    return {entity.id: entity for entity in scene.entities}


def _block_matches_owner(
    block: Any,
    entities: dict[str, Any],
    owners: frozenset[str],
    pack: PackIn,
) -> bool:
    if not block.visible:
        return False
    parts = [str(block.id).replace("_", " ").replace("-", " ")]
    entity = entities.get(block.id)
    if entity is not None:
        parts.append(entity.label)
        if entity.cast_id:
            parts.append(entity.cast_id.replace("_", " ").replace("-", " "))
            for ref in pack.cast:
                if ref.id == entity.cast_id:
                    parts.append(ref.name)
    blob = " ".join(parts)
    return any(_contains_word(blob, name) for name in owners)


def _contains_word(text: str, word: str) -> bool:
    return (
        re.search(rf"(?<![A-Za-z]){re.escape(word)}(?![A-Za-z])", text, re.IGNORECASE) is not None
    )
