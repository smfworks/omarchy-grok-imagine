"""Preflight reports prompts and call counts without opening an Imagine client."""

from __future__ import annotations

import pytest

from omarchy_imagine.pipeline import SEEDED_STILL_CONTRACT, build_motion_prompt, build_still_prompt
from omarchy_imagine.preflight import SEED_FRAME_NOTE, preflight
from omarchy_imagine.schema import CameraCard, LookBible, PackIn, Shot, render_look_bible
from tests.samples import AUTH, pack_body


def _codes(shot: dict) -> list[str]:
    return [issue["code"] for issue in shot["issues"]]


def _pack(**overrides: object) -> PackIn:
    body = pack_body()
    body.update(overrides)
    return PackIn.model_validate(body)


def test_totals_count_every_shot_and_sum_durations() -> None:
    body = pack_body()
    body["shots"][0]["duration_sec"] = 6
    body["shots"][1]["duration_sec"] = 9
    report = preflight(PackIn.model_validate(body))
    assert report["totals"] == {"stills": 2, "videos": 2, "video_seconds": 15}
    assert report["blocking"] is False


def test_seeded_prompt_uses_the_pipeline_builder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("omarchy_imagine.preflight.ffmpeg_path", lambda: "/usr/bin/ffmpeg")
    pack = _pack()
    report = preflight(pack)
    bible = render_look_bible(pack.look_bible)
    first, second = report["shots"]
    assert first["still_mode_expected"] == "text_to_image"
    assert second["still_mode_expected"] == "last_frame_edit"
    assert SEED_FRAME_NOTE not in first["still_prompt"]
    assert SEEDED_STILL_CONTRACT not in first["still_prompt"]
    expected = build_still_prompt(
        pack.shots[1].model_dump(),
        seeded=True,
        look_bible=bible,
        cast=[],
        image_note="",
    )
    assert second["still_prompt"] == f"{expected}\n\n{SEED_FRAME_NOTE}"
    motion = build_motion_prompt(pack.shots[1].model_dump(), look_bible=bible, cast=[])
    assert second["motion_prompt"] == motion
    assert second["video_mode"] == "image_to_video"


def test_unseeded_prompts_when_ffmpeg_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("omarchy_imagine.preflight.ffmpeg_path", lambda: None)
    pack = _pack()
    report = preflight(pack)
    bible = render_look_bible(pack.look_bible)
    for index, shot in enumerate(report["shots"]):
        assert shot["still_mode_expected"] == "text_to_image"
        assert SEED_FRAME_NOTE not in shot["still_prompt"]
        assert SEEDED_STILL_CONTRACT not in shot["still_prompt"]
        expected = build_still_prompt(
            pack.shots[index].model_dump(),
            seeded=False,
            look_bible=bible,
            cast=[],
            image_note="",
        )
        assert shot["still_prompt"] == expected


def test_cast_selects_cast_reference_until_a_frame_can_seed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("omarchy_imagine.preflight.ffmpeg_path", lambda: "/usr/bin/ffmpeg")
    body = pack_body()
    body["cast"] = [
        {
            "id": "mara",
            "name": "Mara",
            "role": "character",
            "markers": "grey coat",
            "image_path": "",
        }
    ]
    for shot in body["shots"]:
        shot["prompt_still"] = "Mara in a grey coat on the dock"
    report = preflight(PackIn.model_validate(body))
    assert report["shots"][0]["still_mode_expected"] == "cast_reference"
    assert "<IMAGE_0> is Mara (character)." in report["shots"][0]["still_prompt"]
    assert report["shots"][1]["still_mode_expected"] == "last_frame_edit"
    assert "previous shot's last frame" in report["shots"][1]["still_prompt"]
    assert SEED_FRAME_NOTE in report["shots"][1]["still_prompt"]


def test_verb_count_warns_only_when_more_than_one_bank_verb() -> None:
    body = pack_body()
    body["shots"][0]["prompt_motion"] = "She walks to the rail."
    body["shots"][1]["prompt_motion"] = "She walks to the bow and turns toward the sea."
    report = preflight(PackIn.model_validate(body))
    assert "verb_count" not in _codes(report["shots"][0])
    issue = next(item for item in report["shots"][1]["issues"] if item["code"] == "verb_count")
    assert issue["severity"] == "warn"
    assert "walks" in issue["message"] and "turns" in issue["message"]
    assert report["blocking"] is False


def test_camera_conflict_warns_when_text_opposes_the_card() -> None:
    body = pack_body()
    card = {"scale": "wide", "angle": "eye", "move": "dolly_in", "exit_frame": ""}
    body["shots"][0]["camera"] = card
    body["shots"][0]["prompt_motion"] = "The camera pulls back as the fog thins."
    body["shots"][1]["camera"] = dict(card)
    body["shots"][1]["prompt_motion"] = "The camera pushes in as the bow rises."
    report = preflight(PackIn.model_validate(body))
    conflict = next(
        item for item in report["shots"][0]["issues"] if item["code"] == "camera_conflict"
    )
    assert conflict["severity"] == "warn"
    assert "pull" in conflict["message"]
    assert "camera_conflict" not in _codes(report["shots"][1])

    stacked = pack_body()
    stacked["shots"][0]["prompt_motion"] = "The camera pushes in and then pulls back."
    stacked["shots"][1]["prompt_motion"] = "The boat travels left-to-right, then right-to-left."
    both = preflight(PackIn.model_validate(stacked))
    assert "camera_conflict" in _codes(both["shots"][0])
    assert "camera_conflict" in _codes(both["shots"][1])


def test_banned_cut_warns_on_editorial_language() -> None:
    body = pack_body()
    body["shots"][0]["prompt_motion"] = "Then cut to the open water."
    body["shots"][1]["prompt_still"] = "Open water [shot 2] after a dissolve."
    report = preflight(PackIn.model_validate(body))
    first = next(item for item in report["shots"][0]["issues"] if item["code"] == "banned_cut")
    second = next(item for item in report["shots"][1]["issues"] if item["code"] == "banned_cut")
    assert first["severity"] == "warn"
    assert "cut to" in first["message"]
    assert "dissolve" in second["message"]
    assert "[shot 2]" in second["message"]


def test_handoff_state_blocks_when_states_differ_after_strip() -> None:
    body = pack_body()
    body["shots"][1]["start_state"] = ""
    report = preflight(PackIn.model_validate(body))
    assert report["blocking"] is True
    assert _codes(report["shots"][0]) == []
    issue = report["shots"][1]["issues"][0]
    assert issue["code"] == "handoff_state"
    assert issue["severity"] == "block"
    assert "s02" in issue["message"]
    assert "s01" in issue["message"]

    mismatched = PackIn.model_construct(
        title="Dawn",
        logline="",
        aspect_ratio="16:9",
        resolution="720p",
        look_bible=LookBible(),
        style_preset="",
        beat_map=[],
        cast=[],
        music_path="",
        shots=[
            Shot.model_construct(
                id="s01",
                prompt_still="A dock",
                prompt_motion="The boat eases off",
                duration_sec=8,
                end_state="Boat is offshore.",
                start_state="",
                beat="",
                camera=CameraCard(),
                video_mode="image_to_video",
                dialogue="",
                voice_id="",
            ),
            Shot.model_construct(
                id="s02",
                prompt_still="Open water",
                prompt_motion="The bow rises",
                duration_sec=8,
                end_state="Fog lifts.",
                start_state="  Boat is offshore.  ",
                beat="",
                camera=CameraCard(),
                video_mode="image_to_video",
                dialogue="",
                voice_id="",
            ),
        ],
    )
    equal = preflight(mismatched)
    assert "handoff_state" not in _codes(equal["shots"][1])

    mismatched.shots[1].start_state = "A different place."
    paraphrase = preflight(mismatched)
    block = next(
        item for item in paraphrase["shots"][1]["issues"] if item["code"] == "handoff_state"
    )
    assert block["severity"] == "block"
    assert paraphrase["blocking"] is True


def test_lock_drift_warns_only_when_a_locked_anchor_changes() -> None:
    body = pack_body()
    body["look_bible"] = {
        "cast": "Mara Voss",
        "wardrobe": "red coat",
        "palette": "",
        "lighting": "",
        "camera": "",
    }
    body["cast"] = [
        {
            "id": "mara",
            "name": "Mara",
            "role": "character",
            "markers": "scarred brow",
            "image_path": "",
        }
    ]
    # The same anchors are missing from every still. That is not drift.
    quiet = preflight(PackIn.model_validate(body))
    assert "lock_drift" not in _codes(quiet["shots"][0])
    assert "lock_drift" not in _codes(quiet["shots"][1])

    body["shots"][0]["prompt_still"] = "Mara Voss in a red coat, scarred brow, on the dock"
    body["shots"][1]["prompt_still"] = "The boat is in open water"
    drifted = preflight(PackIn.model_validate(body))
    assert "lock_drift" not in _codes(drifted["shots"][0])
    issue = next(item for item in drifted["shots"][1]["issues"] if item["code"] == "lock_drift")
    assert issue["severity"] == "warn"
    assert "Mara" in issue["message"]
    assert "scarred" in issue["message"]

    for shot in body["shots"]:
        shot["prompt_still"] = "Mara Voss, scarred brow, red coat, stands on the dock"
    clean = preflight(PackIn.model_validate(body))
    assert "lock_drift" not in _codes(clean["shots"][0])
    assert "lock_drift" not in _codes(clean["shots"][1])


def test_r2v_resolution_is_info_and_does_not_block() -> None:
    body = pack_body()
    body["shots"] = [body["shots"][0]]
    body["resolution"] = "1080p"
    body["shots"][0]["video_mode"] = "reference_to_video"
    body["shots"][0]["voice_id"] = "eve"
    report = preflight(PackIn.model_validate(body))
    issue = report["shots"][0]["issues"][0]
    assert issue["code"] == "r2v_resolution"
    assert issue["severity"] == "info"
    assert "720p" in issue["message"]
    assert report["blocking"] is False
    assert report["shots"][0]["video_mode"] == "reference_to_video"

    body["resolution"] = "720p"
    quiet = preflight(PackIn.model_validate(body))
    assert "r2v_resolution" not in _codes(quiet["shots"][0])


def test_preflight_route_does_not_persist_or_build_a_client(
    client,
    app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XAI_API_KEY", "test-key-should-not-be-used")

    def factory() -> None:
        raise AssertionError("Imagine client must not be built for preflight")

    app.state.imagine_client_factory = factory
    monkeypatch.setattr(
        "omarchy_imagine.app.ImagineClient",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("client constructed")),
    )
    response = client.post("/api/packs/preflight", json=pack_body(), headers=AUTH)
    assert response.status_code == 200
    body = response.json()
    assert body["totals"]["stills"] == 2
    assert body["totals"]["videos"] == 2
    assert body["totals"]["video_seconds"] == 16
    assert body["blocking"] is False
    assert client.get("/api/packs", headers=AUTH).json() == []

    denied = client.post("/api/packs/preflight", json=pack_body())
    assert denied.status_code == 401
