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
        issues = _shot_issues(pack, index, anchors)
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


def _shot_issues(pack: PackIn, index: int, anchors: list[str]) -> list[PreflightIssue]:
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
    missing = [word for word in anchors if not _contains_word(shot.prompt_still, word)]
    if missing:
        shown = missing[:6]
        extra = len(missing) - len(shown)
        suffix = f" and {extra} more" if extra else ""
        issues.append(
            PreflightIssue(
                code="lock_drift",
                severity="warn",
                message=(
                    f"Shot {shot.id} still prompt is missing look-bible or cast anchors: "
                    f"{', '.join(shown)}{suffix}."
                ),
            )
        )
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


def _anchor_words(pack: PackIn) -> list[str]:
    """Content words from the look bible and cast that a still should repeat."""
    blobs: list[tuple[str, int]] = []
    bible = pack.look_bible
    for field in ("cast", "wardrobe", "palette", "lighting", "camera"):
        blobs.append((getattr(bible, field), 4))
    for ref in pack.cast:
        blobs.append((ref.name, 2))
        blobs.append((ref.markers, 4))
    words: list[str] = []
    seen: set[str] = set()
    for text, minimum in blobs:
        for token in re.findall(r"[A-Za-z][A-Za-z'-]*", text):
            if len(token) < minimum:
                continue
            key = token.lower()
            if key in _ANCHOR_STOP or key in seen:
                continue
            seen.add(key)
            words.append(token)
    return words


def _contains_word(text: str, word: str) -> bool:
    return (
        re.search(rf"(?<![A-Za-z]){re.escape(word)}(?![A-Za-z])", text, re.IGNORECASE) is not None
    )
