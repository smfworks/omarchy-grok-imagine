from __future__ import annotations

import pytest
from pydantic import ValidationError

from omarchy_imagine.db import aggregate_gates
from omarchy_imagine.schema import PackIn


def test_pack_without_craft_fields_stays_valid() -> None:
    pack = PackIn.model_validate(
        {
            "title": "Dawn",
            "shots": [
                {
                    "id": "s01",
                    "prompt_still": "A dock",
                    "prompt_motion": "Water moves",
                }
            ],
        }
    )
    assert pack.style_preset == ""
    assert pack.beat_map == []
    assert pack.shots[0].beat == ""
    assert pack.shots[0].camera.scale == ""
    assert pack.shots[0].camera.angle == ""
    assert pack.shots[0].camera.move == ""
    assert pack.shots[0].camera.exit_frame == ""


def test_camera_card_accepts_aliases_and_rejects_unknown_moves() -> None:
    pack = PackIn.model_validate(
        {
            "title": "Dawn",
            "style_preset": "quiet_drama",
            "beat_map": [{"role": "Setup", "summary": "The dock is quiet."}],
            "shots": [
                {
                    "id": "s01",
                    "prompt_still": "A dock",
                    "prompt_motion": "Water moves",
                    "beat": "setup",
                    "camera": {
                        "scale": "extreme close",
                        "angle": "eye-level",
                        "move": "push in",
                        "exit_frame": "The boat is clear of the dock.",
                    },
                }
            ],
        }
    )
    card = pack.shots[0].camera
    assert pack.style_preset == "quiet_drama"
    assert pack.beat_map[0].role == "setup"
    assert card.scale == "extreme_close"
    assert card.angle == "eye"
    assert card.move == "dolly_in"
    with pytest.raises(ValidationError, match="move"):
        PackIn.model_validate(
            {
                "title": "Dawn",
                "shots": [
                    {
                        "id": "s01",
                        "prompt_still": "A dock",
                        "prompt_motion": "Water moves",
                        "camera": {"move": "zoom"},
                    }
                ],
            }
        )


def test_duration_defaults_to_8() -> None:
    pack = PackIn.model_validate(
        {
            "title": "Dawn",
            "logline": "",
            "shots": [
                {
                    "id": "s01",
                    "prompt_still": "A dock",
                    "prompt_motion": "Water moves",
                }
            ],
        }
    )
    assert pack.shots[0].duration_sec == 8
    assert pack.aspect_ratio == "16:9"
    assert pack.resolution == "720p"


def test_duration_bounds() -> None:
    base = {
        "title": "Dawn",
        "shots": [
            {
                "id": "s01",
                "prompt_still": "A dock",
                "prompt_motion": "Water moves",
                "duration_sec": 0,
            }
        ],
    }
    with pytest.raises(ValidationError):
        PackIn.model_validate(base)
    base["shots"][0]["duration_sec"] = 16
    with pytest.raises(ValidationError):
        PackIn.model_validate(base)
    base["shots"][0]["duration_sec"] = 15
    assert PackIn.model_validate(base).shots[0].duration_sec == 15


def test_start_state_must_match_previous_end_state() -> None:
    with pytest.raises(ValidationError, match="start_state must equal"):
        PackIn.model_validate(
            {
                "title": "Dawn",
                "shots": [
                    {
                        "id": "s01",
                        "prompt_still": "A dock",
                        "prompt_motion": "Leave",
                        "end_state": "Boat is offshore.",
                    },
                    {
                        "id": "s02",
                        "prompt_still": "Open water",
                        "prompt_motion": "Drift",
                        "start_state": "A different place.",
                        "end_state": "Fog lifts.",
                    },
                ],
            }
        )


def test_missing_start_state_is_allowed() -> None:
    pack = PackIn.model_validate(
        {
            "title": "Dawn",
            "shots": [
                {
                    "id": "s01",
                    "prompt_still": "A dock",
                    "prompt_motion": "Leave",
                    "end_state": "Boat is offshore.",
                },
                {
                    "id": "s02",
                    "prompt_still": "Open water",
                    "prompt_motion": "Drift",
                },
            ],
        }
    )
    assert pack.shots[1].start_state == ""


def test_matching_states_and_whitespace() -> None:
    pack = PackIn.model_validate(
        {
            "title": "Dawn",
            "shots": [
                {
                    "id": "s01",
                    "prompt_still": "A dock",
                    "prompt_motion": "Leave",
                    "end_state": "  Boat is offshore.  ",
                },
                {
                    "id": "s02",
                    "prompt_still": "Open water",
                    "prompt_motion": "Drift",
                    "start_state": "Boat is offshore.",
                },
            ],
        }
    )
    assert pack.shots[0].end_state == pack.shots[1].start_state


def test_rejects_path_like_shot_ids_and_duplicates() -> None:
    with pytest.raises(ValidationError):
        PackIn.model_validate(
            {
                "title": "Dawn",
                "shots": [
                    {
                        "id": "../escape",
                        "prompt_still": "A dock",
                        "prompt_motion": "Leave",
                    }
                ],
            }
        )
    with pytest.raises(ValidationError, match="unique"):
        PackIn.model_validate(
            {
                "title": "Dawn",
                "shots": [
                    {"id": "s01", "prompt_still": "A", "prompt_motion": "B"},
                    {"id": "s01", "prompt_still": "C", "prompt_motion": "D"},
                ],
            }
        )


def test_aggregate_gates_stay_false_until_work_happens() -> None:
    blank = {
        "called_imagine_still": False,
        "produced_still": False,
        "called_imagine_video": False,
        "produced_mp4": False,
    }
    gates = aggregate_gates([blank], stitched=False)
    assert gates == {
        "called_imagine_still": False,
        "produced_still": False,
        "called_imagine_video": False,
        "produced_mp4": False,
        "stitched_episode": False,
        "grade_match": False,
        "has_audio": False,
        "music_bed_applied": False,
    }
    done = dict(blank)
    done["produced_still"] = True
    assert aggregate_gates([blank, done], stitched=False)["produced_still"] is True
    assert aggregate_gates([done], stitched=True)["stitched_episode"] is True
