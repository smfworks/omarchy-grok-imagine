from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from omarchy_imagine.fill import FillError, FillIn, fill_pack
from tests.samples import AUTH


def test_title_and_logline_fill_a_runnable_chain() -> None:
    pack = fill_pack(
        FillIn(
            title="Harbor dawn",
            logline="A fisher leaves the dock as the fog lifts.",
            shot_count=3,
        )
    )
    assert pack.aspect_ratio == "16:9"
    assert pack.resolution == "720p"
    assert pack.logline == "A fisher leaves the dock as the fog lifts."
    assert [shot.id for shot in pack.shots] == ["s01", "s02", "s03"]
    basis = pack.logline
    for index, shot in enumerate(pack.shots):
        assert shot.duration_sec == 8
        assert shot.prompt_still.strip()
        assert shot.prompt_motion.strip()
        assert basis in shot.prompt_still
        assert basis in shot.prompt_motion
        assert shot.start_state.strip()
        assert shot.end_state.strip()
        assert shot.start_state != shot.end_state
        if index:
            assert shot.start_state == pack.shots[index - 1].end_state
    assert len({shot.end_state for shot in pack.shots}) == 3


_META_MARKERS = (
    "Start locked to",
    "End locked to",
    "After beat",
    "before anything moves",
    "the story has advanced",
    "the story has landed",
    "One beat from the end",
    "Begin the story",
    "Locked start state",
)


def test_filled_motion_is_camera_direction_without_meta_text() -> None:
    pack = fill_pack(
        FillIn.model_validate(
            {
                "title": "Samurai vs Ninja",
                "logline": "A young samurai is chased through an autumn forest.",
                "shots": [
                    {
                        "id": "s01",
                        "prompt_still": (
                            "A young samurai in dark blue armor running from six "
                            "black-clad ninja through a bright autumn forest"
                        ),
                    },
                    {
                        "id": "s04",
                        "prompt_still": (
                            "The exhausted young samurai standing alone in the autumn "
                            "clearing at golden hour, armor scuffed and dusty, "
                            "breathing hard, sword lowered"
                        ),
                    },
                ],
            }
        )
    )
    opening, closing = pack.shots
    assert opening.prompt_still.startswith("A young samurai")
    assert opening.prompt_motion == (
        "Tracking shot alongside the samurai as he sprints between trees, "
        "leaves swirling in his wake, the ninja closing in behind him."
    )
    assert "Slow push in" in closing.prompt_motion
    assert "breath" in closing.prompt_motion
    assert "sword lowered" in closing.prompt_motion
    assert closing.start_state == opening.end_state
    assert opening.end_state == (
        "The young samurai bursts out of the trees into a sunlit clearing, "
        "the six ninja just behind him."
    )
    assert "clearing" in closing.end_state.lower()
    assert "samurai" in opening.start_state.lower()
    blob = json.dumps(pack.model_dump())
    for marker in _META_MARKERS:
        assert marker not in blob


def test_generated_pack_has_no_meta_markers() -> None:
    pack = fill_pack(
        FillIn(
            title="Harbor dawn",
            logline="A fisher leaves the dock as the fog lifts.",
            shot_count=3,
        )
    )
    blob = json.dumps(pack.model_dump())
    for marker in _META_MARKERS:
        assert marker not in blob
    assert "Wide tracking shot" in pack.shots[0].prompt_motion
    assert pack.logline in pack.shots[0].prompt_motion


def test_title_alone_defaults_to_two_shots() -> None:
    pack = fill_pack(FillIn(title="Night market"))
    assert pack.logline == ""
    assert len(pack.shots) == 2
    assert pack.shots[1].start_state == pack.shots[0].end_state
    assert "Night market" in pack.shots[0].prompt_still
    assert "Night market" in pack.shots[1].prompt_motion


def test_logline_alone_becomes_the_title() -> None:
    pack = fill_pack(FillIn(logline="Lanterns flare as the rain starts.", shot_count=1))
    assert pack.title == "Lanterns flare as the rain starts."
    assert pack.shots[0].prompt_still.startswith("Lanterns flare as the rain starts.")
    assert pack.shots[0].start_state != pack.shots[0].end_state


def test_nonempty_text_and_defaults_are_preserved() -> None:
    pack = fill_pack(
        FillIn.model_validate(
            {
                "title": "Harbor dawn",
                "logline": "Fog lifts over the harbor.",
                "aspect_ratio": "9:16",
                "resolution": "1080p",
                "shots": [
                    {
                        "id": "dock",
                        "prompt_still": "Keep this still",
                        "prompt_motion": "   ",
                        "duration_sec": 5,
                        "start_state": "Dawn on the dock.",
                        "end_state": "The boat is off the dock.",
                    },
                    {
                        "id": "water",
                        "prompt_still": "",
                        "prompt_motion": "Keep this motion",
                        "duration_sec": 11,
                    },
                ],
            }
        )
    )
    first, second = pack.shots
    assert pack.aspect_ratio == "9:16"
    assert pack.resolution == "1080p"
    assert pack.title == "Harbor dawn"
    assert first.prompt_still == "Keep this still"
    assert first.prompt_motion != "Keep this still"
    assert "Fog lifts over the harbor." in first.prompt_motion
    assert first.start_state == "Dawn on the dock."
    assert first.end_state == "The boat is off the dock."
    assert first.duration_sec == 5
    assert second.prompt_motion == "Keep this motion"
    assert "Fog lifts over the harbor." in second.prompt_still
    assert second.start_state == "The boat is off the dock."
    assert second.end_state.strip()
    assert second.duration_sec == 11
    assert second.id == "water"


def test_blank_neighbor_locks_to_user_continuity() -> None:
    pack = fill_pack(
        FillIn.model_validate(
            {
                "title": "Market",
                "logline": "The stall opens.",
                "shots": [
                    {"id": "s01", "end_state": ""},
                    {"id": "s02", "start_state": "The awning is up."},
                ],
            }
        )
    )
    assert pack.shots[0].end_state == "The awning is up."
    assert pack.shots[1].start_state == "The awning is up."
    assert pack.shots[0].prompt_still
    assert pack.shots[1].prompt_motion


def test_mismatch_is_not_overwritten() -> None:
    body = FillIn.model_validate(
        {
            "title": "Market",
            "logline": "The stall opens.",
            "shots": [
                {
                    "id": "s01",
                    "prompt_still": "A stall",
                    "prompt_motion": "Cloth lifts",
                    "end_state": "The awning is up.",
                },
                {
                    "id": "s02",
                    "prompt_still": "The stall",
                    "prompt_motion": "Rain starts",
                    "start_state": "Somewhere else.",
                },
            ],
        }
    )
    with pytest.raises(FillError, match="start_state must equal"):
        fill_pack(body)
    assert body.shots[0].end_state == "The awning is up."
    assert body.shots[1].start_state == "Somewhere else."
    assert body.shots[0].prompt_still == "A stall"


def test_smaller_shot_count_does_not_drop_user_shots() -> None:
    pack = fill_pack(
        FillIn.model_validate(
            {
                "title": "Market",
                "logline": "The stall opens.",
                "shot_count": 1,
                "shots": [
                    {"id": "s01", "prompt_still": "One"},
                    {"id": "s02", "prompt_motion": "Two"},
                ],
            }
        )
    )
    assert [shot.id for shot in pack.shots] == ["s01", "s02"]
    assert pack.shots[0].prompt_still == "One"
    assert pack.shots[1].prompt_motion == "Two"


def test_missing_description_is_rejected() -> None:
    with pytest.raises(FillError, match="title or a logline"):
        fill_pack(FillIn(shot_count=2))


def test_invalid_duration_is_rejected_without_rewriting() -> None:
    with pytest.raises(ValidationError):
        FillIn.model_validate(
            {
                "title": "Dawn",
                "shots": [{"id": "s01", "duration_sec": 0}],
            }
        )


def test_fill_route_returns_a_draft_and_does_not_store_it(client) -> None:
    denied = client.post(
        "/api/packs/fill",
        json={"title": "Harbor dawn", "logline": "Fog lifts.", "shot_count": 2},
    )
    assert denied.status_code == 401

    filled = client.post(
        "/api/packs/fill",
        json={"title": "Harbor dawn", "logline": "Fog lifts.", "shot_count": 2},
        headers=AUTH,
    )
    assert filled.status_code == 200
    body = filled.json()
    assert "id" not in body
    text = json.dumps(body)
    assert "http://" not in text
    assert "https://" not in text
    assert body["shots"][1]["start_state"] == body["shots"][0]["end_state"]

    listing = client.get("/api/packs", headers=AUTH)
    assert listing.json() == []

    created = client.post("/api/packs", json=body, headers=AUTH)
    assert created.status_code == 201
    assert created.json()["shots"][0]["prompt_still"] == body["shots"][0]["prompt_still"]


def test_filled_pack_stub_run_keeps_honesty_gates(client) -> None:
    filled = client.post(
        "/api/packs/fill",
        json={"title": "Harbor dawn", "logline": "Fog lifts.", "shot_count": 2},
        headers=AUTH,
    )
    created = client.post("/api/packs", json=filled.json(), headers=AUTH)
    pack_id = created.json()["id"]
    started = client.post(f"/api/packs/{pack_id}/run", headers=AUTH)
    assert started.status_code == 200
    assert started.json()["status"] == "stub"
    job = client.get(f"/api/packs/{pack_id}/jobs", headers=AUTH).json()["jobs"][0]
    assert job["status"] == "stub"
    assert job["called_imagine_still"] is False
    assert job["produced_still"] is False
    assert job["called_imagine_video"] is False
    assert job["produced_mp4"] is False
    assert job["stitched_episode"] is False
    assert job["episode_path"] is None
    text = json.dumps(job)
    assert "http://" not in text
    assert "https://" not in text


def test_fill_route_mismatch_is_422(client) -> None:
    response = client.post(
        "/api/packs/fill",
        json={
            "title": "Harbor dawn",
            "logline": "Fog lifts.",
            "shots": [
                {"id": "s01", "end_state": "Offshore."},
                {"id": "s02", "start_state": "Still at the dock."},
            ],
        },
        headers=AUTH,
    )
    assert response.status_code == 422
    assert "start_state" in response.text
