from __future__ import annotations

import json

import httpx
import pytest
from pydantic import ValidationError

from omarchy_imagine.app import default_text_planner_factory
from omarchy_imagine.plan import (
    PlanError,
    PlanIn,
    PlanUpstreamError,
    StoryDraft,
    XAITextPlanner,
    plan_pack,
    shot_count_for,
    split_durations,
)
from tests.samples import AUTH

_VIOLENT = "A bloody fight to the death as the samurai kills the ninja."


def test_shot_count_prefers_eight_second_clips() -> None:
    assert shot_count_for(8) == 2
    assert shot_count_for(12) == 2
    assert shot_count_for(16) == 2
    assert shot_count_for(20) == 3
    assert shot_count_for(24) == 3
    assert shot_count_for(32) == 4
    assert shot_count_for(60) == 8
    assert shot_count_for(100) == 8
    assert shot_count_for(120) == 8


@pytest.mark.parametrize("target", list(range(8, 121)))
def test_split_sums_to_the_target_inside_the_clip_clamp(target: int) -> None:
    count = shot_count_for(target)
    durations = split_durations(target, count)
    assert 2 <= count <= 8
    assert len(durations) == count
    assert sum(durations) == target
    assert all(1 <= item <= 15 for item in durations)
    # 6–10s is possible from 12s (two 6s clips) through 80s (eight 10s clips).
    if 12 <= target <= 80:
        assert all(6 <= item <= 10 for item in durations)


def test_edges_of_the_duration_split() -> None:
    assert split_durations(8, shot_count_for(8)) == [4, 4]
    assert split_durations(16, shot_count_for(16)) == [8, 8]
    assert split_durations(20, shot_count_for(20)) == [7, 7, 6]
    assert split_durations(100, shot_count_for(100)) == [13, 13, 13, 13, 12, 12, 12, 12]
    assert split_durations(120, shot_count_for(120)) == [15, 15, 15, 15, 15, 15, 15, 15]


def test_target_outside_8_to_120_is_rejected() -> None:
    for target in (0, 7, 121, 200):
        with pytest.raises(ValidationError, match="8 to 120"):
            PlanIn(prompt="Fog lifts.", target_duration_sec=target)


def test_empty_prompt_is_rejected() -> None:
    with pytest.raises(ValidationError, match="story prompt"):
        PlanIn(prompt="   ", target_duration_sec=16)


def test_heuristic_plan_builds_a_chain_without_http(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("heuristic planning must not open an HTTP client")

    monkeypatch.setattr("omarchy_imagine.plan.httpx.Client", boom)
    pack = plan_pack(
        PlanIn(
            prompt="A fisher leaves the dock as the fog lifts.",
            target_duration_sec=32,
            aspect_ratio="9:16",
            resolution="1080p",
        )
    )
    assert pack.title == "A fisher leaves the dock as the fog lifts"
    assert pack.logline == "A fisher leaves the dock as the fog lifts."
    assert pack.shots[0].prompt_still.count("A fisher leaves the dock as the fog lifts") == 1
    assert "(" not in pack.shots[0].prompt_still
    assert ".," not in pack.shots[0].prompt_still
    assert pack.aspect_ratio == "9:16"
    assert pack.resolution == "1080p"
    assert [shot.duration_sec for shot in pack.shots] == [8, 8, 8, 8]
    assert sum(shot.duration_sec for shot in pack.shots) == 32
    for index, shot in enumerate(pack.shots):
        assert shot.id == f"s{index + 1:02d}"
        assert shot.prompt_still.strip()
        assert shot.prompt_motion.strip()
        assert shot.start_state.strip()
        assert shot.end_state.strip()
        if index:
            assert shot.start_state == pack.shots[index - 1].end_state
    blob = json.dumps(pack.model_dump())
    assert "http://" not in blob
    assert "https://" not in blob


def test_explicit_title_is_kept() -> None:
    pack = plan_pack(
        PlanIn(
            prompt="A fisher leaves the dock as the fog lifts.",
            title="Harbor dawn",
            target_duration_sec=16,
        )
    )
    assert pack.title == "Harbor dawn"
    assert "fisher" in pack.logline
    assert [shot.duration_sec for shot in pack.shots] == [8, 8]


def test_heuristic_softens_violent_wording_and_keeps_the_chain() -> None:
    pack = plan_pack(PlanIn(prompt=_VIOLENT, target_duration_sec=16))
    blob = json.dumps(pack.model_dump()).lower()
    assert "bloody" not in blob
    assert "death" not in blob
    assert "kills" not in blob
    assert "choreographed duel" in blob
    assert pack.shots[1].start_state == pack.shots[0].end_state
    assert "http://" not in blob
    assert "https://" not in blob


def test_text_model_result_is_validated_into_the_pack() -> None:
    story = StoryDraft.model_validate(
        {
            "title": "Model title",
            "logline": "Fog lifts over the harbor.",
            "opening_state": "The boat is tied to the dock in the fog.",
            "shots": [
                {
                    "prompt_still": "A bloody fight on the dock",
                    "prompt_motion": "The camera rushes in on the killing blow",
                    "end_state": "The fighter lay dead on the dock.",
                },
                {
                    "prompt_still": "The same dock after the clash",
                    "prompt_motion": "A slow push in on the exhausted figure",
                    "end_state": "The exhausted figure stands on the quiet dock.",
                },
                {
                    "prompt_still": "The boat leaving the quiet dock",
                    "prompt_motion": "A wide shot as the boat eases away",
                    "end_state": "The boat is clear of the dock.",
                },
            ],
        }
    )

    class FakePlanner:
        def __init__(self) -> None:
            self.briefs: list[object] = []

        def plan_story(self, brief: object) -> StoryDraft:
            self.briefs.append(brief)
            return story

    planner = FakePlanner()
    pack = plan_pack(
        PlanIn(
            prompt="A fisher leaves the dock as the fog lifts.",
            title="Harbor dawn",
            target_duration_sec=20,
            aspect_ratio="16:9",
            resolution="720p",
        ),
        planner=planner,
    )
    assert len(planner.briefs) == 1
    brief = planner.briefs[0]
    assert brief.durations == (7, 7, 6)
    assert pack.title == "Harbor dawn"
    assert pack.logline == "Fog lifts over the harbor."
    assert [shot.duration_sec for shot in pack.shots] == [7, 7, 6]
    assert [shot.id for shot in pack.shots] == ["s01", "s02", "s03"]
    assert pack.shots[0].start_state == "The boat is tied to the dock in the fog."
    assert pack.shots[1].start_state == pack.shots[0].end_state
    assert pack.shots[2].start_state == pack.shots[1].end_state
    blob = json.dumps(pack.model_dump()).lower()
    assert "bloody" not in blob
    assert "killing" not in blob
    assert "dead" not in blob
    assert "http://" not in blob


def test_text_model_shot_count_mismatch_is_an_error() -> None:
    story = StoryDraft(
        title="Short",
        logline="Fog.",
        opening_state="Dawn.",
        shots=[],
    )

    class FakePlanner:
        def plan_story(self, brief: object) -> StoryDraft:
            return story

    with pytest.raises(PlanError, match="needs 2"):
        plan_pack(
            PlanIn(prompt="Fog lifts over the harbor.", target_duration_sec=16),
            planner=FakePlanner(),
        )


def test_chat_completions_request_is_strict_json_and_not_imagine() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content.decode())
        assert "images" not in request.url.path
        assert "videos" not in request.url.path
        story = {
            "title": "From the model",
            "logline": "Fog lifts.",
            "opening_state": "The boat is at the dock.",
            "shots": [
                {
                    "prompt_still": "A wooden boat at dawn",
                    "prompt_motion": "The boat eases away from the dock",
                    "end_state": "The boat is clear of the dock.",
                },
                {
                    "prompt_still": "The same boat in open water",
                    "prompt_motion": "Fog thins as the bow rises",
                    "end_state": "The boat is in open water.",
                },
            ],
        }
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(story)}}]},
        )

    planner = XAITextPlanner(
        "secret-key",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        base_url="https://api.x.ai/v1",
    )
    pack = plan_pack(
        PlanIn(prompt="A fisher leaves the dock as the fog lifts.", target_duration_sec=16),
        planner=planner,
    )
    body = seen["body"]
    assert isinstance(body, dict)
    assert seen["path"] == "/v1/chat/completions"
    assert seen["auth"] == "Bearer secret-key"
    assert body["model"] == "grok-4.6"
    schema = body["response_format"]
    assert schema["type"] == "json_schema"
    assert schema["json_schema"]["strict"] is True
    assert schema["json_schema"]["schema"]["properties"]["shots"]["minItems"] == 2
    assert schema["json_schema"]["schema"]["properties"]["shots"]["maxItems"] == 2
    assert "secret-key" not in json.dumps(body)
    assert pack.title == "From the model"
    assert [shot.duration_sec for shot in pack.shots] == [8, 8]
    assert pack.shots[1].start_state == pack.shots[0].end_state


def test_plan_route_without_key_does_not_touch_imagine_or_store(client, app) -> None:
    def boom() -> None:
        raise AssertionError("Imagine client must not be built for a plan")

    app.state.imagine_client_factory = boom
    denied = client.post(
        "/api/packs/plan",
        json={"prompt": "Fog lifts over the harbor.", "target_duration_sec": 16},
    )
    assert denied.status_code == 401

    planned = client.post(
        "/api/packs/plan",
        json={
            "prompt": "A fisher leaves the dock as the fog lifts.",
            "target_duration_sec": 24,
            "aspect_ratio": "16:9",
            "resolution": "720p",
        },
        headers=AUTH,
    )
    assert planned.status_code == 200
    body = planned.json()
    assert "id" not in body
    assert [shot["duration_sec"] for shot in body["shots"]] == [8, 8, 8]
    assert body["shots"][1]["start_state"] == body["shots"][0]["end_state"]
    assert body["shots"][2]["start_state"] == body["shots"][1]["end_state"]
    text = json.dumps(body)
    assert "http://" not in text
    assert "https://" not in text
    assert client.get("/api/packs", headers=AUTH).json() == []

    created = client.post("/api/packs", json=body, headers=AUTH)
    assert created.status_code == 201
    pack_id = created.json()["id"]
    started = client.post(f"/api/packs/{pack_id}/run", headers=AUTH)
    assert started.status_code == 200
    assert started.json()["status"] == "stub"
    job = client.get(f"/api/packs/{pack_id}/jobs", headers=AUTH).json()["jobs"][0]
    assert job["status"] == "stub"
    for gate in (
        "called_imagine_still",
        "produced_still",
        "called_imagine_video",
        "produced_mp4",
        "stitched_episode",
    ):
        assert job[gate] is False


def test_plan_route_rejects_out_of_range_targets(client, app) -> None:
    def boom() -> None:
        raise AssertionError("Imagine client must not be built for a rejected plan")

    app.state.imagine_client_factory = boom
    for target in (7, 121):
        response = client.post(
            "/api/packs/plan",
            json={"prompt": "Fog lifts.", "target_duration_sec": target},
            headers=AUTH,
        )
        assert response.status_code == 422
        assert "8 to 120" in response.text
    assert client.get("/api/packs", headers=AUTH).json() == []


def test_plan_route_uses_the_text_planner_and_not_imagine(client, app) -> None:
    def boom() -> None:
        raise AssertionError("Imagine client must not be built for a plan")

    app.state.imagine_client_factory = boom
    story = StoryDraft.model_validate(
        {
            "title": "Harbor dawn",
            "logline": "Fog lifts.",
            "opening_state": "Dawn on the dock.",
            "shots": [
                {
                    "prompt_still": "A quiet harbor",
                    "prompt_motion": "The boat eases off",
                    "end_state": "The boat is offshore.",
                },
                {
                    "prompt_still": "Open water",
                    "prompt_motion": "The fog thins",
                    "end_state": "The harbor is behind the boat.",
                },
            ],
        }
    )

    class FakePlanner:
        def plan_story(self, brief: object) -> StoryDraft:
            return story

    app.state.text_planner_factory = lambda: FakePlanner()
    response = client.post(
        "/api/packs/plan",
        json={"prompt": "A fisher leaves the dock as the fog lifts.", "target_duration_sec": 16},
        headers=AUTH,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["title"] == "Harbor dawn"
    assert body["shots"][0]["prompt_still"] == "A quiet harbor"
    assert body["shots"][1]["start_state"] == "The boat is offshore."
    assert client.get("/api/packs", headers=AUTH).json() == []


def test_plan_route_reports_text_model_failure(client, app) -> None:
    def boom() -> None:
        raise AssertionError("Imagine client must not be built when the text model fails")

    app.state.imagine_client_factory = boom

    class FakePlanner:
        def plan_story(self, brief: object) -> StoryDraft:
            raise PlanUpstreamError("The text model returned HTTP 401: bad key")

    app.state.text_planner_factory = lambda: FakePlanner()
    response = client.post(
        "/api/packs/plan",
        json={"prompt": "Fog lifts over the harbor.", "target_duration_sec": 16},
        headers=AUTH,
    )
    assert response.status_code == 502
    assert "text model" in response.text
    assert client.get("/api/packs", headers=AUTH).json() == []


def test_text_planner_factory_is_none_without_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    assert default_text_planner_factory() is None
