"""Director craft: beat map, camera grammar, and how a planned shot is written.

Plan calls this for both the text-model brief and the no-key heuristic.
The module does not call the network and does not invent media URLs.

Durations stay on the planner's even split. When that split has a remainder,
the extra seconds land on the earlier shots, so a later punchy beat is the
one that is already a second shorter. This module does not change those numbers.

``look_bible.camera`` stays the lens and grade lock. A shot ``camera`` card is
scale, angle, one move, and the exit frame the next shot should open on.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from omarchy_imagine.schema import (
    BEAT_ROLES,
    STYLE_PRESETS,
    LookBible,
)

_ACTION_WORDS = (
    "duel",
    "sword",
    "samurai",
    "ninja",
    "fight",
    "combat",
    "clash",
    "battle",
    "warrior",
    "blade",
)
_TREK_WORDS = (
    "starship",
    "star trek",
    "federation",
    "warp",
    "bridge crew",
    "away team",
    "phaser",
    "enterprise",
    "starfleet",
)
_QUIET_WORDS = (
    "quiet",
    "grief",
    "whisper",
    "silence",
    "intimate",
    "farewell",
    "letter",
    "unspoken",
)

_LENS = {
    "generic": (
        "35mm film still, natural color, one grade, "
        "no flicker and no lens change between shots."
    ),
    "action_duel": (
        "35mm, a slightly long lens on the clash, one grade, "
        "no flicker and no lens change between shots."
    ),
    "quiet_drama": (
        "50mm, soft natural color, one grade, "
        "no flicker and no lens change between shots."
    ),
    "trek": (
        "Anamorphic widescreen, cool practical lights, one grade, "
        "no flicker and no lens change between shots."
    ),
}

_SCALE_LABEL = {
    "wide": "Wide",
    "medium": "Medium",
    "close": "Close",
    "extreme_close": "Extreme close",
}
_ANGLE_LABEL = {
    "eye": "eye-level",
    "low": "low-angle",
    "high": "high-angle",
    "ots": "over-the-shoulder",
    "dutch": "Dutch-angle",
}
_MOVE_LINE = {
    "static": "The camera stays static",
    "dolly_in": "The camera dollies in",
    "dolly_out": "The camera dollies out",
    "orbit": "The camera orbits once",
    "pan": "The camera pans",
    "tilt": "The camera tilts",
    "whip_pan": "The camera whip-pans",
    "handheld": "The camera follows handheld",
}

# One primary move per beat. Repeated beats cycle the tuple so a long pack
# does not use the same move on every turn.
_MOVE_TABLE: dict[str, dict[str, tuple[str, ...]]] = {
    "generic": {
        "setup": ("static",),
        "turn": ("dolly_in", "pan", "tilt"),
        "climax": ("dolly_in", "dolly_out"),
        "button": ("dolly_out",),
    },
    "action_duel": {
        "setup": ("dolly_in",),
        "turn": ("handheld", "orbit", "pan"),
        "climax": ("whip_pan", "handheld", "orbit"),
        "button": ("static",),
    },
    "quiet_drama": {
        "setup": ("static",),
        "turn": ("dolly_in", "pan"),
        "climax": ("dolly_in",),
        "button": ("static",),
    },
    "trek": {
        "setup": ("dolly_in",),
        "turn": ("pan", "tilt"),
        "climax": ("dolly_in", "orbit"),
        "button": ("orbit",),
    },
}

_POSE = {
    "generic": {
        "setup": "stands in the opening space with one clear want",
        "turn": "turns against the obstacle and changes the emotion",
        "climax": "commits the hardest action so the outcome reads",
        "button": "holds still so the ending reads in one picture",
    },
    "action_duel": {
        "setup": "takes a ready stance for a choreographed duel and stays unharmed",
        "turn": "changes the duel line and meets the other figure",
        "climax": "lands the last rehearsed exchange, exhausted and unharmed",
        "button": "holds the victory still, and both figures stay unharmed",
    },
    "quiet_drama": {
        "setup": "stands in the quiet with one clear want and does not speak it yet",
        "turn": "turns toward the other person and the feeling changes",
        "climax": "lets the hardest truth show in the face and the hands",
        "button": "holds still so the feeling reads in one picture",
    },
    "trek": {
        "setup": "stands on the bridge with one clear order not yet given",
        "turn": "turns to the crew as the problem changes course",
        "climax": "gives the hardest order so the outcome reads on the faces",
        "button": "holds the bridge still so the ending reads",
    },
}

_POSE_END = {
    "generic": {
        "setup": "the figure set in the opening space with the want visible",
        "turn": "the figure mid-turn as the obstacle answers",
        "climax": "the hardest action just completed and the outcome readable",
        "button": "the final held picture with the ending readable",
    },
    "action_duel": {
        "setup": "both figures on their marks, unharmed, the duel not yet begun",
        "turn": "the duel line reversed, both figures unharmed",
        "climax": "the last exchange finished, exhaustion and a readable victory",
        "button": "the victor holding still, everyone unharmed",
    },
    "quiet_drama": {
        "setup": "the figure still in the quiet, the want not yet spoken",
        "turn": "the feeling reversed, the two figures closer",
        "climax": "the hardest truth just visible in the face",
        "button": "the final held picture, the feeling readable",
    },
    "trek": {
        "setup": "the bridge established, the order not yet given",
        "turn": "the crew turned toward a new course",
        "climax": "the hardest order just given, the outcome readable",
        "button": "the bridge holding on the final picture",
    },
}

_ACTION = {
    "generic": {
        "setup": "Steps into the opening space and shows the want",
        "turn": "Pushes against the obstacle and reverses the emotion",
        "climax": "Drives the hardest action through to a readable outcome",
        "button": "Settles and holds the final picture",
    },
    "action_duel": {
        "setup": "Steps onto the mark and raises a ready stance",
        "turn": "Changes the duel line and meets the other figure",
        "climax": "Lands the last rehearsed exchange and shows the victory",
        "button": "Holds the victory still",
    },
    "quiet_drama": {
        "setup": "Steps into the quiet and shows the want without a speech",
        "turn": "Turns toward the other person and reverses the feeling",
        "climax": "Lets the hardest truth land in a small gesture",
        "button": "Settles and holds the final picture",
    },
    "trek": {
        "setup": "Steps onto the bridge and sets the want",
        "turn": "Turns the crew to the new problem and changes course",
        "climax": "Gives the hardest order and drives it through",
        "button": "Holds the bridge on the final picture",
    },
}

_SUMMARY = {
    "generic": {
        "setup": "Setup. Want: {story}. Obstacle: the way forward is not open yet.",
        "turn": "Turn. {story}. The obstacle answers and the emotion reverses.",
        "climax": "Climax. Hardest moment: {story}. The outcome has to read.",
        "button": "Button. {story}. The ending is one held, readable picture.",
    },
    "action_duel": {
        "setup": (
            "Setup. Want: {story}. Obstacle: the other figure holds the ground. "
            "Choreography only."
        ),
        "turn": "Turn. The duel line changes. The emotion reverses. Nobody is harmed.",
        "climax": (
            "Climax. The last rehearsed exchange of {story}. Exhaustion, then a readable victory."
        ),
        "button": "Button. The victor holds still. The clash is over and everyone is unharmed.",
    },
    "quiet_drama": {
        "setup": "Setup. Want: {story}. Obstacle: what is unsaid.",
        "turn": "Turn. A quiet reversal. The emotion changes without a spectacle.",
        "climax": "Climax. The closest, hardest look at {story}.",
        "button": "Button. A still, readable ending. The feeling lands and holds.",
    },
    "trek": {
        "setup": "Setup. Want: {story}. Obstacle: the ship and the unknown ahead.",
        "turn": "Turn. The bridge problem turns. The crew commits to a new course.",
        "climax": "Climax. The hardest order in {story}. The outcome has to read on the faces.",
        "button": "Button. The ship holds. The ending is one readable picture.",
    },
}


@dataclass(frozen=True)
class ShotGrammar:
    beat: str
    scale: str
    angle: str
    move: str


def lens_line(style: str) -> str:
    """Film-stock line for ``look_bible.camera``. Generic matches the fill heuristic."""
    return _LENS.get(style, _LENS["generic"])


def infer_style(prompt: str) -> str:
    """Pick a preset from the story. ``generic`` when nothing in the prompt matches."""
    low = prompt.lower()
    scores = {
        "action_duel": _score(low, _ACTION_WORDS),
        "trek": _score(low, _TREK_WORDS),
        "quiet_drama": _score(low, _QUIET_WORDS),
    }
    best = max(scores.values())
    if best == 0:
        return "generic"
    for name in ("action_duel", "trek", "quiet_drama"):
        if scores[name] == best:
            return name
    return "generic"


def assign_beats(count: int) -> list[str]:
    """Map each shot to setup, turn, climax, or button.

    Two shots are setup then button. Three are setup, climax, button.
    Longer packs keep one setup and one button and split the middle between
    turn and climax, with climax on the later middle shots.
    """
    if count <= 0:
        return []
    if count == 1:
        return ["setup"]
    if count == 2:
        return ["setup", "button"]
    if count == 3:
        return ["setup", "climax", "button"]
    middle = count - 2
    turn_count = (middle + 1) // 2
    climax_count = middle - turn_count
    return ["setup", *["turn"] * turn_count, *["climax"] * climax_count, "button"]


def grammar_for(count: int, style: str) -> list[ShotGrammar]:
    """Camera cards for ``count`` shots. Scales alternate. Dutch appears at most once."""
    chosen = style if style in STYLE_PRESETS else "generic"
    beats = assign_beats(count)
    scales = _scales(beats, chosen)
    angles = _angles(beats, chosen)
    moves = _cycle(beats, _MOVE_TABLE[chosen])
    return [
        ShotGrammar(beat, scale, angle, move)
        for beat, scale, angle, move in zip(beats, scales, angles, moves, strict=True)
    ]


def beat_map_lines(prompt: str, beats: list[str], style: str) -> list[tuple[str, str]]:
    """One summary per role that appears, in story order."""
    chosen = style if style in _SUMMARY else "generic"
    story = prompt.strip().rstrip(".!?").strip() or "the story"
    seen: list[str] = []
    for beat in beats:
        if beat in BEAT_ROLES and beat not in seen:
            seen.append(beat)
    return [(role, _SUMMARY[chosen][role].format(story=story)) for role in seen]


def apply_heuristic_craft(draft: dict[str, object], style: str, prompt: str) -> None:
    """Attach the beat map and camera cards, and rewrite stills and motion.

    Stills stay locked frames. Motion is only the change plus one move.
    Look-bible wardrobe and light are repeated as a short continuity lock.
    ``end_state`` matches ``exit_frame`` so the next shot can open on it.
    """
    shots = draft.get("shots")
    if not isinstance(shots, list) or not shots:
        return
    chosen = style if style in STYLE_PRESETS else "generic"
    bible = LookBible.model_validate(draft.get("look_bible") or {})
    bible = bible.model_copy(update={"camera": lens_line(chosen)})
    draft["look_bible"] = bible.model_dump()
    draft["style_preset"] = chosen
    cards = grammar_for(len(shots), chosen)
    draft["beat_map"] = [
        {"role": role, "summary": summary}
        for role, summary in beat_map_lines(prompt, [card.beat for card in cards], chosen)
    ]
    wardrobe = _light_anchor(bible.wardrobe)
    lighting = _light_anchor(bible.lighting)
    basis = prompt.strip().rstrip(".!?").strip() or prompt.strip()
    subject = _subject(basis)
    exits: list[str] = []
    for index, (shot, card) in enumerate(zip(shots, cards, strict=True)):
        if not isinstance(shot, dict):
            continue
        opens = exits[-1] if exits else ""
        still = _locked_still(
            basis=basis,
            subject=subject,
            pose=_POSE[chosen][card.beat],
            scale=card.scale,
            angle=card.angle,
            wardrobe=wardrobe,
            lighting=lighting,
            opens_on=opens,
        )
        motion = _locked_motion(
            action=_ACTION[chosen][card.beat],
            move=card.move,
            wardrobe=wardrobe,
        )
        exit_frame = _exit_line(_POSE_END[chosen][card.beat], card.scale, card.angle)
        shot["prompt_still"] = still
        shot["prompt_motion"] = motion
        shot["beat"] = card.beat
        shot["end_state"] = exit_frame
        shot["camera"] = {
            "scale": card.scale,
            "angle": card.angle,
            "move": card.move,
            "exit_frame": exit_frame,
        }
        exits.append(exit_frame)
        if index == 0:
            shot["start_state"] = f"The image begins as {_lower_first(still.rstrip('.'))}."
        else:
            shot["start_state"] = exits[index - 1]


def lock_model_craft(
    shots: list[dict[str, object]],
    *,
    style: str,
    prompt: str,
    model_beat_map: list[tuple[str, str]],
    model_exits: list[str],
) -> list[dict[str, str]]:
    """Write the server camera cards onto a text-model draft.

    The model is asked to write prose for these cards. The server keeps the
    cards, so a drifted scale or a second Dutch angle does not land in the pack.
    A non-empty model ``exit_frame`` is kept. Otherwise the shot end state is used.
    A non-empty model summary replaces the heuristic line for that role.
    """
    chosen = style if style in STYLE_PRESETS else "generic"
    cards = grammar_for(len(shots), chosen)
    summaries = {role: text for role, text in model_beat_map if text.strip()}
    beat_map: list[dict[str, str]] = []
    for role, summary in beat_map_lines(prompt, [card.beat for card in cards], chosen):
        beat_map.append({"role": role, "summary": summaries.get(role) or summary})
    for index, (shot, card) in enumerate(zip(shots, cards, strict=True)):
        provided = model_exits[index].strip() if index < len(model_exits) else ""
        exit_frame = provided or str(shot.get("end_state") or "").strip()
        shot["beat"] = card.beat
        shot["camera"] = {
            "scale": card.scale,
            "angle": card.angle,
            "move": card.move,
            "exit_frame": exit_frame,
        }
    return beat_map


def _score(text: str, words: tuple[str, ...]) -> int:
    total = 0
    for word in words:
        if " " in word:
            if word in text:
                total += 1
            continue
        if re.search(rf"\b{re.escape(word)}\b", text):
            total += 1
    return total


def _scales(beats: list[str], style: str) -> list[str]:
    scales: list[str] = []
    for beat in beats:
        previous = scales[-1] if scales else ""
        if not scales and beat == "setup":
            choice = "wide"
        elif beat == "button":
            choice = "medium" if style == "quiet_drama" else "wide"
            if choice == previous:
                choice = "medium" if choice == "wide" else "wide"
        elif beat == "climax":
            choice = "extreme_close" if style == "quiet_drama" else "close"
            if choice == previous:
                choice = "close" if choice == "extreme_close" else "extreme_close"
        else:
            choice = "medium"
            for candidate in ("medium", "close", "wide", "extreme_close"):
                if candidate != previous:
                    choice = candidate
                    break
        scales.append(choice)
    return scales


def _angles(beats: list[str], style: str) -> list[str]:
    angles: list[str] = []
    dutch_used = False
    turn_index = 0
    climax_index = 0
    for beat in beats:
        if style == "action_duel" and beat == "climax" and not dutch_used:
            choice = "dutch"
            dutch_used = True
        elif style == "action_duel" and beat == "setup":
            choice = "low"
        elif style == "action_duel" and beat == "climax":
            choice = "high" if climax_index == 1 else "low"
        elif style in {"action_duel", "trek"} and beat == "turn":
            choice = "ots" if turn_index % 2 == 0 else "eye"
            turn_index += 1
        elif style == "trek" and beat == "climax":
            choice = "low"
        else:
            choice = "eye"
        if beat == "climax":
            climax_index += 1
        angles.append(choice)
    return angles


def _cycle(beats: list[str], table: dict[str, tuple[str, ...]]) -> list[str]:
    seen: dict[str, int] = {}
    moves: list[str] = []
    for beat in beats:
        options = table[beat]
        index = seen.get(beat, 0)
        moves.append(options[index % len(options)])
        seen[beat] = index + 1
    return moves


def _light_anchor(text: str, limit: int = 72) -> str:
    cleaned = " ".join(text.split()).strip().rstrip(".")
    if len(cleaned) <= limit:
        return cleaned
    cut = cleaned[:limit]
    if "," in cut:
        cut = cut.rsplit(",", 1)[0]
    else:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,;:")


def _subject(prompt: str) -> str:
    low = prompt.lower()
    if "samurai" in low:
        return "samurai"
    if "fisher" in low or "fisherman" in low:
        return "fisher"
    if "ninja" in low:
        return "ninja"
    if any(word in low for word in ("starship", "starfleet", "bridge crew")):
        return "officer"
    return "figure"


def _locked_still(
    *,
    basis: str,
    subject: str,
    pose: str,
    scale: str,
    angle: str,
    wardrobe: str,
    lighting: str,
    opens_on: str,
) -> str:
    frame = f"{_SCALE_LABEL[scale]} {_ANGLE_LABEL[angle]} frame"
    parts = []
    if opens_on:
        parts.append(f"Opens on the previous exit: {opens_on.rstrip('.')}")
    parts.append(f"{basis}, and the {subject} {pose}")
    parts.append(frame)
    if wardrobe:
        parts.append(wardrobe)
    if lighting:
        parts.append(lighting)
    return _sentence(*parts)


def _locked_motion(*, action: str, move: str, wardrobe: str) -> str:
    parts = [action, _MOVE_LINE[move]]
    if wardrobe:
        parts.append(f"Continuity lock: {wardrobe}")
    return _sentence(*parts)


def _exit_line(pose_end: str, scale: str, angle: str) -> str:
    picture = pose_end[:1].upper() + pose_end[1:] if pose_end else ""
    view = f"{_SCALE_LABEL[scale]} {_ANGLE_LABEL[angle]} view"
    return _sentence(picture, view)


def _sentence(*parts: str) -> str:
    cleaned = [part.strip().rstrip(".") for part in parts if part and part.strip()]
    if not cleaned:
        return ""
    return ". ".join(cleaned) + "."


def _lower_first(text: str) -> str:
    stripped = text.strip()
    if not stripped:
        return stripped
    return stripped[0].lower() + stripped[1:]
