"""Detect Imagine moderation rejections and soften a shot so it can be resubmitted.

Detection follows the responses this client actually sees:

- Video poll HTTP 400 whose error text is ``Generated video rejected by content moderation.``
- A finished video whose ``respect_moderation`` is false (the URL is empty).
- Image generation or edit HTTP 400 with that same moderation wording.
- An image body whose ``respect_moderation`` is false, which the image guide
  documents as "Image filtered by moderation".

The rewrite is deterministic. It does not call xAI. Gore and death become
exhaustion and victory. A fight becomes a choreographed clash. A shot may be
rewritten at most ``MAX_MODERATION_RETRIES`` times.
"""

from __future__ import annotations

import re

from omarchy_imagine.schema import BIBLE_HEADER

MAX_MODERATION_RETRIES = 2

_BIBLE_LINE = re.compile(r"^(Cast|Wardrobe|Palette|Lighting|Camera):", re.IGNORECASE)

_REPLACEMENTS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"violent bloody clash(?:es)?", re.IGNORECASE), "choreographed clash"),
    (re.compile(r"battered and bloody", re.IGNORECASE), "exhausted and dusty"),
    (re.compile(r"\bbloody\b", re.IGNORECASE), "dusty"),
    (re.compile(r"\bbloodied\b", re.IGNORECASE), "dusty"),
    (re.compile(r"\bblood\b", re.IGNORECASE), "dust"),
    (re.compile(r"\bbattered\b", re.IGNORECASE), "exhausted"),
    (re.compile(r"\blay dead\b", re.IGNORECASE), "kneels, exhausted, in victory"),
    (re.compile(r"\blies dead\b", re.IGNORECASE), "kneels, exhausted, in victory"),
    (re.compile(r"\bdead\b", re.IGNORECASE), "exhausted"),
    (re.compile(r"\bdeath\b", re.IGNORECASE), "a hard-won pause"),
    (re.compile(r"\bkilling blow\b", re.IGNORECASE), "final flourish"),
    (re.compile(r"\bkilling\b", re.IGNORECASE), "outmaneuvering"),
    (re.compile(r"\bkilled\b", re.IGNORECASE), "overcome"),
    (re.compile(r"\bkills\b", re.IGNORECASE), "overcomes"),
    (re.compile(r"\bkill\b", re.IGNORECASE), "overcome"),
    (re.compile(r"\bgory\b", re.IGNORECASE), "weathered"),
    (re.compile(r"\bgore\b", re.IGNORECASE), "dust"),
    (re.compile(r"\bwounded\b", re.IGNORECASE), "exhausted"),
    (re.compile(r"\bwounds\b", re.IGNORECASE), "scuffs"),
    (re.compile(r"\bfighting\b", re.IGNORECASE), "a choreographed duel"),
    (re.compile(r"\bfights\b", re.IGNORECASE), "meets in a choreographed duel"),
    (re.compile(r"\bfight\b", re.IGNORECASE), "choreographed duel"),
    (re.compile(r"\bcombat\b", re.IGNORECASE), "choreographed duel"),
)

_NOTES = (
    "No blood, no death, and no injury. The same story beat plays as exhaustion "
    "and victory, and any clash is choreographed.",
    "Family-safe staging only. Everyone is unharmed and tired, and the moment "
    "reads as a clear victory after a rehearsed clash.",
)


def is_moderation_failure(payload: object, text: str = "") -> bool:
    """True when an Imagine response is a moderation rejection."""
    if "content moderation" in text.lower():
        return True
    return _respect_moderation_false(payload)


def soften_wording(text: str) -> str:
    """Replace violent wording. Does not append a moderation-retry note.

    Planning uses this so a harbor story stays a harbor story, while a fight
    or a death still becomes exhaustion and a choreographed clash.
    """
    body = text.strip()
    for pattern, replacement in _REPLACEMENTS:
        body = pattern.sub(replacement, body)
    return re.sub(r"\s{2,}", " ", body).strip()


def soften_prompts(still: str, motion: str, *, attempt: int) -> tuple[str, str]:
    """Return still and motion prompts with violent wording replaced.

    ``attempt`` is 1-based. Each attempt appends a different staging note so a
    second retry is not the same string as the first. The note is not scanned
    for violent words, so "No blood" stays "No blood".

    A look-bible block is lifted out before the rewrite and appended again
    unchanged. Soften changes shot prose only.
    """
    note = _NOTES[min(max(attempt, 1), len(_NOTES)) - 1]
    still_prose, still_bible = split_look_bible(still)
    motion_prose, motion_bible = split_look_bible(motion)
    return (
        join_look_bible(_soften_one(still_prose, note), still_bible),
        join_look_bible(_soften_one(motion_prose, note), motion_bible),
    )


def split_look_bible(text: str) -> tuple[str, str]:
    """Split ``text`` into shot prose and a verbatim look-bible block.

    The bible is the header plus the Cast, Wardrobe, Palette, Lighting, and
    Camera lines that follow it. Anything else stays in the prose.
    """
    if BIBLE_HEADER not in text:
        return text.strip(), ""
    before, _, after = text.partition(BIBLE_HEADER)
    lines = after.splitlines()
    index = 0
    while index < len(lines) and not lines[index].strip():
        index += 1
    bible_lines = [BIBLE_HEADER]
    while index < len(lines) and _BIBLE_LINE.match(lines[index].strip()):
        bible_lines.append(lines[index].strip())
        index += 1
    if len(bible_lines) == 1:
        return text.strip(), ""
    rest = "\n".join(lines[index:]).strip()
    prose = "\n\n".join(part for part in (before.strip(), rest) if part)
    return prose, "\n".join(bible_lines)


def join_look_bible(prose: str, bible: str) -> str:
    """Put the look bible ahead of the shot prose."""
    prose = prose.strip()
    bible = bible.strip()
    if not bible:
        return prose
    if not prose:
        return bible
    return f"{bible}\n\n{prose}"


def _soften_one(text: str, note: str) -> str:
    body = soften_wording(_strip_note(text.strip()))
    if not body:
        return note
    if body[-1] not in ".!?":
        body = f"{body}."
    return f"{body} {note}"


def _strip_note(text: str) -> str:
    for note in _NOTES:
        suffix = f" {note}"
        if text.lower().endswith(suffix.lower()):
            return text[: -len(suffix)].strip()
    return text


def _respect_moderation_false(payload: object) -> bool:
    if isinstance(payload, dict):
        if payload.get("respect_moderation") is False:
            return True
        return any(_respect_moderation_false(value) for value in payload.values())
    if isinstance(payload, list):
        return any(_respect_moderation_false(value) for value in payload)
    return False
