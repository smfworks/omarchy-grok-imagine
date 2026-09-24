"""Choose which cast images go to Imagine, and name them in prompts.

Image edits and reference-to-video examples both use a handful of images.
This module keeps the cap at three. Characters come first, then props, then
locations. Later entries stay in the prose lock and are not sent as files.
"""

from __future__ import annotations

from typing import Any

_ROLE_RANK = {"character": 0, "prop": 1, "location": 2}


def select_cast(cast: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """Stable pick: role rank, then the order the pack listed them."""
    ranked = sorted(
        enumerate(cast),
        key=lambda pair: (_ROLE_RANK.get(str(pair[1].get("role")), 9), pair[0]),
    )
    return [item for _, item in ranked[:limit]]


def cast_lock_line(cast: list[dict[str, Any]]) -> str:
    if not cast:
        return ""
    parts: list[str] = []
    for ref in cast:
        name = str(ref.get("name") or "").strip()
        if not name:
            continue
        role = str(ref.get("role") or "").strip()
        markers = str(ref.get("markers") or "").strip()
        label = f"{name} ({role}" if role else name
        if markers:
            label = f"{label}, {markers}" if role else f"{name} ({markers}"
        if role or markers:
            label += ")"
        parts.append(label)
    if not parts:
        return ""
    return "Named cast lock: " + "; ".join(parts) + ". Keep those identities."


def ensure_cast_names(text: str, cast: list[dict[str, Any]]) -> str:
    """Append any cast name the line does not already contain."""
    missing = [
        ref
        for ref in cast
        if str(ref.get("name") or "").strip()
        and str(ref["name"]).lower() not in text.lower()
    ]
    if not missing:
        return text
    extra = cast_lock_line(missing)
    if not extra:
        return text
    stripped = text.rstrip()
    if not stripped:
        return extra
    return f"{stripped} {extra}"


def image_tag_line(refs: list[dict[str, Any]], *, last_frame: bool) -> str:
    """``<IMAGE_0>`` tags, matching the image-edits REST reference."""
    lines: list[str] = []
    index = 0
    if last_frame:
        lines.append(
            "<IMAGE_0> is the previous shot's last frame. "
            "Keep that frame's face, clothes, and grade."
        )
        index = 1
    for offset, ref in enumerate(refs):
        name = str(ref.get("name") or "reference").strip()
        role = str(ref.get("role") or "reference").strip()
        lines.append(f"<IMAGE_{index + offset}> is {name} ({role}). Match it.")
    return " ".join(lines)


def video_reference_line(refs: list[dict[str, Any]]) -> str:
    """``<IMAGE_1>`` tags, matching the reference-to-video examples."""
    lines: list[str] = []
    for index, ref in enumerate(refs, start=1):
        name = str(ref.get("name") or "reference").strip()
        role = str(ref.get("role") or "reference").strip()
        lines.append(f"<IMAGE_{index}> is {name} ({role}).")
    return " ".join(lines)
