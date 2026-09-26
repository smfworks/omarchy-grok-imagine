"""Director craft on POST /api/packs/plan. No media URLs, durations stay put."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from omarchy_imagine.craft import assign_beats, grammar_for
from omarchy_imagine.fill import FillIn, fill_pack
from omarchy_imagine.plan import PlanIn, StoryDraft, plan_pack
from omarchy_imagine.schema import CAMERA_MOVES, CAMERA_SCALES, STYLE_PRESETS
from tests.samples import AUTH

_FISHER = "A fisher leaves the dock as the fog lifts."


def test_assign_beats_is_not_equal_filler() -> None:
    assert assign_beats(2) == ["setup", "button"]
    assert assign_beats(3) == ["setup", "climax", "button"]
    assert assign_beats(4) == ["setup", "turn", "climax", "button"]
    assert assign_beats(8) == [
        "setup",
        "turn",
        "turn",
        "turn",
        "climax",
        "climax",
        "climax",
        "button",
    ]


@pytest.mark.parametrize("style", STYLE_PRESETS)
@pytest.mark.parametrize("count", range(2, 9))
def test_grammar_alternates_scale_and_uses_one_move(style: str, count: int) -> None:
    cards = grammar_for(count, style)
    assert [card.beat for card in cards] == assign_beats(count)
    scales = [card.scale for card in cards]
    assert scales[0] == "wide"
    assert all(scale in CAMERA_SCALES for scale in scales)
    assert all(left != right for left, right in zip(scales, scales[1:], strict=False))
    assert all(card.move in CAMERA_MOVES for card in cards)
    dutch = [card for card in cards if card.angle == "dutch"]
    assert len(dutch) <= 1
    if style != "action_duel":
        assert dutch == []
    elif "climax" in [card.beat for card in cards]:
        assert len(dutch) == 1
        assert dutch[0].beat == "climax"


def test_generic_four_shot_cards_match_the_grammar() -> None:
    cards = grammar_for(4, "generic")
    assert [card.scale for card in cards] == ["wide", "medium", "close", "wide"]
    assert [card.angle for card in cards] == ["eye", "eye", "eye", "eye"]
    assert [card.move for card in cards] == ["static", "dolly_in", "dolly_in", "dolly_out"]


def test_heuristic_plan_writes_craft_without_urls_or_duration_changes() -> None:
    pack = plan_pack(PlanIn(prompt=_FISHER, target_duration_sec=32, aspect_ratio="9:16"))
    assert pack.style_preset == "generic"
    assert [beat.role for beat in pack.beat_map] == ["setup", "turn", "climax", "button"]
    assert [shot.beat for shot in pack.shots] == ["setup", "turn", "climax", "button"]
    assert [shot.camera.scale for shot in pack.shots] == ["wide", "medium", "close", "wide"]
    assert [shot.duration_sec for shot in pack.shots] == [8, 8, 8, 8]
    assert pack.shots[0].prompt_still.count("A fisher leaves the dock as the fog lifts") == 1
    assert "(" not in pack.shots[0].prompt_still
    assert ".," not in pack.shots[0].prompt_still
    assert "Wide eye-level frame" in pack.shots[0].prompt_still
    assert "dollies" not in pack.shots[0].prompt_still.lower()
    assert "The camera stays static" in pack.shots[0].prompt_motion
    assert "working coat" in pack.shots[0].prompt_still.lower()
    assert pack.shots[0].camera.exit_frame == pack.shots[0].end_state
    assert pack.shots[1].prompt_still.startswith("Opens on the previous exit:")
    for index, shot in enumerate(pack.shots):
        assert shot.camera.move
        assert shot.camera.exit_frame == shot.end_state
        if index:
            assert shot.start_state == pack.shots[index - 1].end_state
    blob = json.dumps(pack.model_dump())
    assert "http://" not in blob
    assert "https://" not in blob


def test_shorter_remainder_lands_on_the_later_beat() -> None:
    pack = plan_pack(PlanIn(prompt=_FISHER, target_duration_sec=20))
    assert [shot.duration_sec for shot in pack.shots] == [7, 7, 6]
    assert [shot.beat for shot in pack.shots] == ["setup", "climax", "button"]
    assert pack.shots[-1].duration_sec < pack.shots[0].duration_sec


def test_style_preset_is_inferred_or_forced() -> None:
    duel = plan_pack(
        PlanIn(prompt="Two samurai meet for a sword duel.", target_duration_sec=16)
    )
    assert duel.style_preset == "action_duel"
    trek = plan_pack(
        PlanIn(
            prompt="The bridge crew takes the starship to warp.",
            target_duration_sec=16,
        )
    )
    assert trek.style_preset == "trek"
    quiet = plan_pack(
        PlanIn(prompt="A quiet farewell on the evening dock.", target_duration_sec=16)
    )
    assert quiet.style_preset == "quiet_drama"
    forced = plan_pack(
        PlanIn(prompt=_FISHER, target_duration_sec=16, style_preset="trek")
    )
    assert forced.style_preset == "trek"
    assert "Anamorphic" in forced.look_bible.camera
    with pytest.raises(ValidationError, match="style_preset"):
        PlanIn(prompt="Fog lifts.", target_duration_sec=16, style_preset="anime")


def test_action_duel_uses_dutch_once_and_stays_bloodless() -> None:
    pack = plan_pack(
        PlanIn(
            prompt="A bloody fight to the death as the samurai kills the ninja.",
            target_duration_sec=32,
            style_preset="action_duel",
        )
    )
    dutch = [shot for shot in pack.shots if shot.camera.angle == "dutch"]
    assert len(dutch) == 1
    assert dutch[0].beat == "climax"
    assert dutch[0].camera.move == "dolly_in"
    assert "whip" not in dutch[0].prompt_still.lower()
    assert "dollies in" in dutch[0].prompt_motion
    assert "orbits" not in dutch[0].prompt_motion
    blob = json.dumps(pack.model_dump()).lower()
    assert "meets the other figure" not in blob
    assert "duel line" not in blob
    assert "bloody" not in blob
    assert "death" not in blob
    assert "kills" not in blob
    assert "http://" not in blob
    assert "https://" not in blob


def test_text_model_prose_stays_and_server_locks_the_camera_card() -> None:
    story = StoryDraft.model_validate(
        {
            "title": "Harbor dawn",
            "logline": "Fog lifts over the harbor.",
            "opening_state": "The boat is tied to the dock in the fog.",
            "style_preset": "quiet_drama",
            "beat_map": [
                {"role": "setup", "summary": "The fisher wants the fog to open."},
                {"role": "button", "summary": "The boat holds on open water."},
            ],
            "shots": [
                {
                    "prompt_still": "A quiet harbor",
                    "prompt_motion": "The boat eases away from the dock",
                    "end_state": "The boat is clear of the dock.",
                    "beat": "climax",
                    "camera": {
                        "scale": "zoom",
                        "angle": "dutch",
                        "move": "crash",
                        "exit_frame": "The bow clears the last piling.",
                    },
                },
                {
                    "prompt_still": "The same boat in open water",
                    "prompt_motion": "Fog thins as the bow rises",
                    "end_state": "The boat is in open water.",
                    "beat": "setup",
                    "camera": {
                        "scale": "dutch",
                        "angle": "dutch",
                        "move": "zoom",
                        "exit_frame": "",
                    },
                },
            ],
        }
    )

    class FakePlanner:
        def plan_story(self, brief: object) -> StoryDraft:
            assert getattr(brief, "style_preset", "") == "generic"
            assert len(getattr(brief, "grammar", ())) == 2
            return story

    pack = plan_pack(
        PlanIn(prompt=_FISHER, target_duration_sec=16),
        planner=FakePlanner(),
    )
    assert pack.style_preset == "generic"
    assert pack.shots[0].prompt_still == "A quiet harbor"
    assert pack.shots[0].prompt_motion == "The boat eases away from the dock"
    assert [shot.duration_sec for shot in pack.shots] == [8, 8]
    assert [shot.beat for shot in pack.shots] == ["setup", "button"]
    assert [shot.camera.scale for shot in pack.shots] == ["wide", "medium"]
    assert pack.shots[0].camera.angle == "eye"
    assert pack.shots[0].camera.move == "static"
    assert pack.shots[0].camera.exit_frame == "The bow clears the last piling."
    assert pack.shots[1].camera.exit_frame == "The boat is in open water."
    assert pack.beat_map[0].summary == "The fisher wants the fog to open."
    assert pack.shots[1].start_state == pack.shots[0].end_state
    blob = json.dumps(pack.model_dump())
    assert "http://" not in blob
    assert "https://" not in blob


def test_fill_keeps_a_supplied_camera_card() -> None:
    pack = fill_pack(
        FillIn(
            title="Harbor dawn",
            logline=_FISHER,
            style_preset="quiet_drama",
            beat_map=[{"role": "setup", "summary": "Leave the dock."}],
            shots=[
                {
                    "beat": "setup",
                    "camera": {
                        "scale": "wide",
                        "angle": "eye",
                        "move": "static",
                        "exit_frame": "The boat is still tied.",
                    },
                }
            ],
            shot_count=1,
        )
    )
    assert pack.style_preset == "quiet_drama"
    assert pack.beat_map[0].summary == "Leave the dock."
    assert pack.shots[0].beat == "setup"
    assert pack.shots[0].camera.move == "static"
    assert pack.shots[0].camera.exit_frame == "The boat is still tied."
    assert pack.shots[0].prompt_still.strip()


def test_plan_route_returns_craft_and_an_old_pack_still_runs(client, app) -> None:
    def boom() -> None:
        raise AssertionError("Imagine client must not be built for a plan")

    app.state.imagine_client_factory = boom
    planned = client.post(
        "/api/packs/plan",
        json={"prompt": _FISHER, "target_duration_sec": 24, "style_preset": "quiet_drama"},
        headers=AUTH,
    )
    assert planned.status_code == 200
    body = planned.json()
    assert body["style_preset"] == "quiet_drama"
    assert [beat["role"] for beat in body["beat_map"]] == ["setup", "climax", "button"]
    assert [shot["beat"] for shot in body["shots"]] == ["setup", "climax", "button"]
    assert body["shots"][0]["camera"]["scale"] == "wide"
    assert body["shots"][0]["camera"]["exit_frame"]
    assert "id" not in body
    text = json.dumps(body)
    assert "http://" not in text
    assert "https://" not in text

    created = client.post("/api/packs", json=body, headers=AUTH)
    assert created.status_code == 201
    stored = created.json()
    assert stored["style_preset"] == "quiet_drama"
    assert stored["shots"][1]["camera"]["scale"]
    fetched = client.get(f"/api/packs/{stored['id']}", headers=AUTH)
    assert fetched.json()["beat_map"][0]["role"] == "setup"

    bare = client.post(
        "/api/packs",
        json={
            "title": "Dawn",
            "shots": [
                {"id": "s01", "prompt_still": "A dock", "prompt_motion": "Water moves"}
            ],
        },
        headers=AUTH,
    )
    assert bare.status_code == 201
    assert bare.json()["style_preset"] == ""
    assert bare.json()["beat_map"] == []
    assert bare.json()["shots"][0]["beat"] == ""
    assert bare.json()["shots"][0]["camera"]["move"] == ""
