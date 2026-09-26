"""Staging schema, clause, planner fields, and preflight checks. No xAI calls."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from omarchy_imagine.pipeline import build_motion_prompt, build_still_prompt
from omarchy_imagine.plan import PlanBrief, PlanIn, StoryDraft, _messages, plan_pack
from omarchy_imagine.preflight import preflight
from omarchy_imagine.schema import PackIn
from omarchy_imagine.staging import STAGING_HEADER, staging_clause
from tests.samples import AUTH

ROOT = Path(__file__).resolve().parent
FIXTURE = ROOT / "fixtures" / "cowboy_chase.json"

STAGING_BLOCKS = {
    "side_flip",
    "travel_flip",
    "relation_violation",
    "stage_handoff",
}
STAGING_WARNS = {
    "line_risk_camera",
    "r2v_no_anchor",
    "stage_missing",
    "clause_missing",
    "vague_position",
}


def _load() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _pack() -> PackIn:
    return PackIn.model_validate(_load())


def _codes(report: dict) -> set[str]:
    found: set[str] = set()
    for shot in report["shots"]:
        for issue in shot["issues"]:
            found.add(issue["code"])
    return found


def _block(entity: str, x: str, **extra: object) -> dict:
    base = {
        "id": entity,
        "x": x,
        "depth": "mid",
        "facing": "screen_right",
        "look": "",
        "travel": "screen_right",
        "visible": True,
    }
    base.update(extra)
    return base


def test_fixture_loads_and_a_pack_without_staging_still_validates() -> None:
    pack = _pack()
    assert pack.style_preset == "chase"
    assert pack.lock_staging is True
    assert pack.staging is not None
    assert pack.staging.scenes[0].relations[0].rel == "behind"
    assert pack.shots[2].stage is not None
    assert pack.shots[2].stage.end[0].look == "screen_left"
    bare = PackIn.model_validate(
        {
            "title": "Dawn",
            "shots": [{"id": "s01", "prompt_still": "A dock", "prompt_motion": "Water moves"}],
        }
    )
    assert bare.staging is None
    assert bare.lock_staging is True
    assert bare.shots[0].stage is None


def test_cross_motivation_is_accepted_as_cross_reason() -> None:
    body = _load()
    stage = body["shots"][2]["stage"]
    stage.pop("cross_reason")
    stage["cross_motivation"] = "Jack rides through the dust and the camera follows."
    stage["camera_side"] = "cross"
    pack = PackIn.model_validate(body)
    assert pack.shots[2].stage is not None
    assert pack.shots[2].stage.cross_reason.startswith("Jack rides")
    assert "cross_motivation" not in pack.shots[2].stage.model_dump()


def test_staging_clause_locks_positions_and_forbids_riding_beside() -> None:
    pack = _pack()
    still = staging_clause(pack, pack.shots[0], "still")
    motion = staging_clause(pack, pack.shots[2], "motion")
    assert still.startswith(STAGING_HEADER)
    assert "left-to-right" in still
    assert "same side of the line" in still
    assert "Jack on his buckskin horse" in still
    assert "left edge of frame" in still
    assert "BEHIND" in still
    assert "Nobody rides beside" in still
    assert "never pass" in still
    assert "never appear on the right of frame" in still
    assert "looks toward screen-left" in motion
    assert "Change:" in motion
    assert "Nobody rides beside" in motion
    unlocked = pack.model_copy(update={"lock_staging": False})
    assert staging_clause(unlocked, unlocked.shots[0], "still") == ""
    assert staging_clause(unlocked, unlocked.shots[0], "motion") == ""


def test_still_edit_and_motion_prompts_include_the_clause_and_end_state() -> None:
    pack = _pack()
    shot = pack.shots[2].model_dump()
    still = build_still_prompt(shot, seeded=False, pack=pack)
    edit = build_still_prompt(shot, seeded=True, pack=pack)
    motion = build_motion_prompt(shot, pack=pack)
    for prompt in (still, edit, motion):
        assert STAGING_HEADER in prompt
        assert "Nobody rides beside" in prompt
    assert "Only pose and action may change" in edit
    assert "same side of frame" in edit
    assert "unless the staging lines below" in edit
    assert STAGING_HEADER in edit
    assert edit.index("Only pose and action may change") < edit.index(STAGING_HEADER)
    assert "Locked start state:" in motion
    assert "By the last second:" in motion
    assert shot["end_state"] in motion
    assert "http://" not in still
    assert "https://" not in motion


def test_correct_cowboy_chase_passes_preflight() -> None:
    report = preflight(_pack())
    codes = _codes(report)
    assert report["blocking"] is False
    assert codes.isdisjoint(STAGING_BLOCKS)
    assert codes.isdisjoint(STAGING_WARNS)
    assert STAGING_HEADER in report["shots"][0]["still_prompt"]
    assert STAGING_HEADER in report["shots"][3]["motion_prompt"]
    assert "By the last second:" in report["shots"][3]["motion_prompt"]


def test_broken_cowboy_chase_triggers_each_staging_check() -> None:
    body = _load()
    body["shots"][1]["stage"]["start"][1]["x"] = "right_third"
    body["shots"][1]["stage"]["end"][1]["x"] = "right_third"
    body["shots"][1]["prompt_motion"] = "The bandits pull alongside him on the right."
    body["shots"][2]["camera"]["move"] = "orbit"
    body["shots"][2]["stage"]["end"][0]["travel"] = "screen_left"
    body["shots"][2]["video_mode"] = "reference_to_video"
    body["shots"][2]["voice_id"] = "eve"
    report = preflight(PackIn.model_validate(body))
    codes = _codes(report)
    assert "side_flip" in codes
    assert "travel_flip" in codes
    assert "relation_violation" in codes
    assert "line_risk_camera" in codes
    assert "r2v_no_anchor" in codes
    assert "vague_position" in codes
    assert report["blocking"] is True
    side = next(
        issue
        for shot in report["shots"]
        for issue in shot["issues"]
        if issue["code"] == "side_flip"
    )
    assert side["severity"] == "block"
    assert "cross_reason" in side["message"]
    travel = next(
        issue
        for shot in report["shots"]
        for issue in shot["issues"]
        if issue["code"] == "travel_flip"
    )
    assert travel["severity"] == "block"
    relation = next(
        issue
        for shot in report["shots"]
        for issue in shot["issues"]
        if issue["code"] == "relation_violation"
    )
    assert relation["severity"] == "block"
    assert "behind" in relation["message"]
    camera = next(
        issue
        for shot in report["shots"]
        for issue in shot["issues"]
        if issue["code"] == "line_risk_camera"
    )
    assert camera["severity"] == "warn"
    assert "orbit" in camera["message"]
    anchor = next(
        issue
        for shot in report["shots"]
        for issue in shot["issues"]
        if issue["code"] == "r2v_no_anchor"
    )
    assert anchor["severity"] == "warn"


def test_each_staging_check_can_fire_on_its_own() -> None:
    body = _load()
    side = copy.deepcopy(body)
    side["shots"][1]["stage"]["end"][0]["x"] = "left_third"
    side_report = preflight(PackIn.model_validate(side))
    assert "side_flip" in _codes(side_report)
    assert "travel_flip" not in _codes(side_report)

    travel = copy.deepcopy(body)
    travel["shots"][1]["stage"]["end"][0]["travel"] = "screen_left"
    travel["shots"][1]["stage"]["end"][0]["facing"] = "screen_left"
    travel_report = preflight(PackIn.model_validate(travel))
    assert "travel_flip" in _codes(travel_report)

    relation = copy.deepcopy(body)
    relation["shots"][0]["stage"]["start"][1]["x"] = "right_third"
    relation["shots"][0]["stage"]["end"][1]["x"] = "right_third"
    relation["shots"][1]["stage"]["start"][1]["x"] = "right_third"
    relation_report = preflight(PackIn.model_validate(relation))
    assert "relation_violation" in _codes(relation_report)

    camera = copy.deepcopy(body)
    camera["shots"][2]["camera"]["move"] = "whip_pan"
    camera_report = preflight(PackIn.model_validate(camera))
    assert "line_risk_camera" in _codes(camera_report)
    assert camera_report["blocking"] is False

    ots = copy.deepcopy(body)
    ots["shots"][1]["camera"]["angle"] = "ots"
    assert "line_risk_camera" in _codes(preflight(PackIn.model_validate(ots)))

    r2v = copy.deepcopy(body)
    r2v["shots"][3]["video_mode"] = "reference_to_video"
    r2v["shots"][3]["voice_id"] = "eve"
    r2v_report = preflight(PackIn.model_validate(r2v))
    assert "r2v_no_anchor" in _codes(r2v_report)
    assert r2v_report["blocking"] is False

    missing = {
        "title": "Two riders",
        "cast": [
            {"id": "ann", "name": "Ann", "role": "character"},
            {"id": "ben", "name": "Ben", "role": "character"},
        ],
        "shots": [
            {
                "id": "s01",
                "prompt_still": "Ann and Ben wait",
                "prompt_motion": "Ann waits",
            }
        ],
    }
    missing_report = preflight(PackIn.model_validate(missing))
    assert "stage_missing" in _codes(missing_report)
    assert missing_report["blocking"] is False

    handoff = copy.deepcopy(body)
    handoff["shots"][1]["stage"]["start"][1]["depth"] = "mid"
    assert "stage_handoff" in _codes(preflight(PackIn.model_validate(handoff)))

    vague = copy.deepcopy(body)
    vague["shots"][0]["prompt_motion"] = "The bandits ride near Jack."
    vague_report = preflight(PackIn.model_validate(vague))
    info = next(
        issue
        for shot in vague_report["shots"]
        for issue in shot["issues"]
        if issue["code"] == "vague_position"
    )
    assert info["severity"] == "info"


def test_cross_reason_and_on_axis_allow_a_side_change() -> None:
    body = _load()
    crossed = copy.deepcopy(body)
    crossed["shots"][2]["stage"]["camera_side"] = "cross"
    crossed["shots"][2]["stage"]["cross_reason"] = "The camera follows Jack through the dust."
    crossed["shots"][2]["stage"]["end"][0]["x"] = "left_third"
    crossed["shots"][2]["stage"]["end"][1]["x"] = "left_edge"
    crossed["shots"][3]["stage"]["start"] = copy.deepcopy(crossed["shots"][2]["stage"]["end"])
    crossed["shots"][3]["stage"]["end"][0]["x"] = "center"
    crossed["shots"][3]["stage"]["end"][1]["x"] = "left_edge"
    crossed["shots"][2]["camera"]["move"] = "orbit"
    report = preflight(PackIn.model_validate(crossed))
    assert "side_flip" not in _codes(report)
    assert "line_risk_camera" not in _codes(report)

    bare_cross = copy.deepcopy(body)
    bare_cross["shots"][2]["stage"]["camera_side"] = "cross"
    bare_cross["shots"][2]["stage"]["cross_reason"] = ""
    assert "side_flip" in _codes(preflight(PackIn.model_validate(bare_cross)))

    neutral = copy.deepcopy(body)
    neutral["shots"][1]["stage"]["camera_side"] = "on_axis"
    for shot in neutral["shots"][1:]:
        shot["stage"]["start"][0]["x"] = "left_third"
        shot["stage"]["end"][0]["x"] = "left_third"
        shot["stage"]["start"][1]["x"] = "left_edge"
        shot["stage"]["end"][1]["x"] = "left_edge"
    neutral["shots"][1]["stage"]["start"] = copy.deepcopy(neutral["shots"][0]["stage"]["end"])
    neutral["shots"][2]["stage"]["start"] = copy.deepcopy(neutral["shots"][1]["stage"]["end"])
    neutral["shots"][3]["stage"]["start"] = copy.deepcopy(neutral["shots"][2]["stage"]["end"])
    codes = _codes(preflight(PackIn.model_validate(neutral)))
    assert "side_flip" not in codes


def test_look_back_is_not_a_camera_direction_conflict() -> None:
    body = _load()
    body["shots"][2]["prompt_motion"] = (
        "Jack rides left-to-right and looks back right-to-left at the bandits."
    )
    report = preflight(PackIn.model_validate(body))
    assert "camera_conflict" not in _codes(report)

    both = _load()
    both["shots"][0]["prompt_motion"] = "The camera travels left-to-right, then right-to-left."
    assert "camera_conflict" in _codes(preflight(PackIn.model_validate(both)))


def test_clause_missing_when_the_assembled_prompt_drops_the_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "omarchy_imagine.preflight.build_still_prompt",
        lambda *args, **kwargs: "A frame with no staging header.",
    )
    monkeypatch.setattr(
        "omarchy_imagine.preflight.build_motion_prompt",
        lambda *args, **kwargs: "A move with no staging header.",
    )
    report = preflight(_pack())
    assert "clause_missing" in _codes(report)
    issue = next(
        item
        for shot in report["shots"]
        for item in shot["issues"]
        if item["code"] == "clause_missing"
    )
    assert issue["severity"] == "warn"


def test_heuristic_chase_is_staged_without_a_network_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("chase planning must not open an HTTP client")

    monkeypatch.setattr("omarchy_imagine.plan.httpx.Client", boom)
    pack = plan_pack(
        PlanIn(
            prompt="A lone cowboy is chased across the desert by bandits.",
            target_duration_sec=32,
        )
    )
    assert pack.style_preset == "chase"
    assert pack.staging is not None
    assert pack.staging.scenes[0].travel == "screen_right"
    assert pack.staging.scenes[0].relations[0].rel == "behind"
    moves = [shot.camera.move for shot in pack.shots]
    angles = [shot.camera.angle for shot in pack.shots]
    assert "orbit" not in moves
    assert "whip_pan" not in moves
    assert "ots" not in angles
    for index, shot in enumerate(pack.shots):
        assert shot.stage is not None
        assert "screen-right" in shot.prompt_motion or "screen-right" in shot.prompt_still
        if index:
            assert shot.stage.start == pack.shots[index - 1].stage.end
    report = preflight(pack)
    assert report["blocking"] is False
    assert "side_flip" not in _codes(report)
    assert "relation_violation" not in _codes(report)
    assert "Nobody rides beside" in report["shots"][0]["motion_prompt"]
    blob = json.dumps(pack.model_dump())
    assert "http://" not in blob
    assert "https://" not in blob


def test_planner_parses_staging_and_copies_the_handoff() -> None:
    story = StoryDraft.model_validate(
        {
            "title": "Dust",
            "logline": "Bandits chase a cowboy.",
            "opening_state": "Jack is ahead of the bandits.",
            "style_preset": "chase",
            "staging": {
                "scenes": [
                    {
                        "id": "sc1",
                        "shot_ids": ["s01", "s02"],
                        "axis": "the trail",
                        "travel": "screen_right",
                        "entities": [
                            {
                                "id": "jack",
                                "label": "Jack",
                                "kind": "character",
                                "cast_id": "jack",
                                "count": 1,
                            },
                            {
                                "id": "bandits",
                                "label": "the bandits",
                                "kind": "group",
                                "cast_id": "",
                                "count": 3,
                            },
                        ],
                        "relations": [
                            {"a": "bandits", "rel": "behind", "b": "jack", "gap": "far"}
                        ],
                    }
                ]
            },
            "shots": [
                {
                    "prompt_still": "Jack rides screen-right. The bandits are behind on the left.",
                    "prompt_motion": "Jack rides toward screen-right. The bandits stay behind.",
                    "end_state": "Jack on the right. Bandits behind on the left.",
                    "beat": "setup",
                    "camera": {
                        "scale": "wide",
                        "angle": "eye",
                        "move": "orbit",
                        "exit_frame": "Jack on the right.",
                    },
                    "stage": {
                        "scene_id": "sc1",
                        "camera_side": "same",
                        "cross_reason": "",
                        "start": [
                            _block("jack", "center"),
                            _block("bandits", "left_edge", depth="far"),
                        ],
                        "end": [
                            _block("jack", "right_third"),
                            _block("bandits", "left_third", depth="far"),
                        ],
                        "relations": [
                            {"a": "bandits", "rel": "behind", "b": "jack", "gap": "far"}
                        ],
                    },
                },
                {
                    "prompt_still": "Jack still screen-right. Bandits still behind.",
                    "prompt_motion": "The bandits close from behind. Jack keeps screen-right.",
                    "end_state": "Jack on the right. Bandits closer behind.",
                    "beat": "button",
                    "camera": {
                        "scale": "medium",
                        "angle": "ots",
                        "move": "whip_pan",
                        "exit_frame": "",
                    },
                    "stage": {
                        "scene_id": "sc1",
                        "camera_side": "same",
                        "cross_motivation": "",
                        "start": [
                            _block("jack", "center"),
                            _block("bandits", "right_third", depth="far"),
                        ],
                        "end": [
                            _block("jack", "right_third"),
                            _block("bandits", "left_third", depth="background"),
                        ],
                        "relations": [],
                    },
                },
            ],
        }
    )

    class FakePlanner:
        def plan_story(self, brief: object) -> StoryDraft:
            return story

    pack = plan_pack(
        PlanIn(
            prompt="A lone cowboy is chased by bandits.",
            target_duration_sec=16,
            cast=[],
            staging={
                "scenes": [
                    {
                        "id": "sc1",
                        "axis": "studio passed this axis",
                        "travel": "screen_right",
                        "entities": [
                            {
                                "id": "jack",
                                "label": "Jack",
                                "kind": "character",
                                "count": 1,
                            },
                            {
                                "id": "bandits",
                                "label": "the bandits",
                                "kind": "group",
                                "count": 3,
                            },
                        ],
                        "relations": [
                            {"a": "bandits", "rel": "behind", "b": "jack", "gap": "far"}
                        ],
                    }
                ]
            },
        ),
        planner=FakePlanner(),
    )
    assert pack.staging is not None
    assert pack.staging.scenes[0].axis == "studio passed this axis"
    assert pack.shots[1].stage is not None
    assert pack.shots[0].stage is not None
    assert pack.shots[1].stage.start == pack.shots[0].stage.end
    assert pack.shots[0].camera.move == "pan"
    assert pack.shots[1].camera.move == "static"
    assert pack.shots[1].camera.angle == "eye"


def test_plan_messages_restate_positions_and_keep_turns_as_twists() -> None:
    from omarchy_imagine.craft import grammar_for

    brief = PlanBrief(
        prompt="A lone cowboy is chased by bandits.",
        title="",
        aspect_ratio="16:9",
        resolution="720p",
        durations=(8, 8),
        style_preset="chase",
        grammar=tuple(grammar_for(2, "chase")),
    )
    system = _messages(brief)[0]["content"]
    assert "One action and one camera move per shot" in system
    assert "restate" in system.lower() or "Restate" in system
    assert "torso twist" in system
    assert "screen-left" in system
    assert "cross_reason" in system
    assert "only what changes" not in system


def test_plan_route_accepts_cast_and_staging(client, app) -> None:
    def boom() -> None:
        raise AssertionError("Imagine client must not be built for a plan")

    app.state.imagine_client_factory = boom
    staging = {
        "scenes": [
            {
                "id": "sc1",
                "axis": "passed through from studio",
                "travel": "screen_right",
                "entities": [
                    {"id": "jack", "label": "Jack", "kind": "character", "count": 1},
                    {"id": "bandits", "label": "the bandits", "kind": "group", "count": 3},
                ],
                "relations": [{"a": "bandits", "rel": "behind", "b": "jack", "gap": "far"}],
            }
        ]
    }
    planned = client.post(
        "/api/packs/plan",
        json={
            "prompt": "A fisher leaves the dock as the fog lifts.",
            "target_duration_sec": 16,
            "cast": [
                {"id": "jack", "name": "Jack", "role": "character", "markers": "tan hat"}
            ],
            "staging": staging,
            "lock_staging": True,
        },
        headers=AUTH,
    )
    assert planned.status_code == 200
    body = planned.json()
    assert body["cast"][0]["name"] == "Jack"
    assert body["staging"]["scenes"][0]["axis"] == "passed through from studio"
    assert body["lock_staging"] is True
    assert body["shots"][0]["stage"]["start"]
    assert body["shots"][1]["stage"]["start"] == body["shots"][0]["stage"]["end"]
    assert "id" not in body
    text = json.dumps(body)
    assert "http://" not in text
    assert "https://" not in text

    created = client.post("/api/packs", json=body, headers=AUTH)
    assert created.status_code == 422

    bare = client.post(
        "/api/packs/plan",
        json={"prompt": "Fog lifts over the harbor.", "target_duration_sec": 16},
        headers=AUTH,
    )
    saved = client.post("/api/packs", json=bare.json(), headers=AUTH)
    assert saved.status_code == 201
    pack_id = saved.json()["id"]
    started = client.post(f"/api/packs/{pack_id}/run", headers=AUTH)
    assert started.json()["status"] == "stub"
    job = client.get(f"/api/packs/{pack_id}/jobs", headers=AUTH).json()["jobs"][0]
    for gate in (
        "called_imagine_still",
        "produced_still",
        "called_imagine_video",
        "produced_mp4",
        "stitched_episode",
    ):
        assert job[gate] is False
