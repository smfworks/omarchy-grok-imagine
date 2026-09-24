from __future__ import annotations

import json

from tests.samples import AUTH, pack_body


def test_health_is_open(client) -> None:
    response = client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["imagine_configured"] is False
    assert body["image_model"] == "grok-imagine-image-2.0"
    assert body["video_model"] == "grok-imagine-video-1.5"
    assert body["continuity"] in {"last_frame_edit", "prose_regenerate"}


def test_packs_require_bearer_token(client) -> None:
    response = client.post("/api/packs", json=pack_body())
    assert response.status_code == 401
    denied = client.post(
        "/api/packs",
        json=pack_body(),
        headers={"Authorization": "Bearer wrong"},
    )
    assert denied.status_code == 401


def test_create_list_and_get_pack(client) -> None:
    created = client.post("/api/packs", json=pack_body(), headers=AUTH)
    assert created.status_code == 201
    pack = created.json()
    assert pack["title"] == "Harbor dawn"
    assert pack["shots"][0]["duration_sec"] == 8

    listing = client.get("/api/packs", headers=AUTH)
    assert listing.status_code == 200
    assert listing.json()[0]["id"] == pack["id"]

    fetched = client.get(f"/api/packs/{pack['id']}", headers=AUTH)
    assert fetched.status_code == 200
    assert fetched.json()["shots"][1]["id"] == "s02"

    missing = client.get("/api/packs/missing", headers=AUTH)
    assert missing.status_code == 404


def test_duration_defaults_through_the_api(client) -> None:
    body = pack_body()
    body["shots"] = [
        {
            "id": "s01",
            "prompt_still": "A dock",
            "prompt_motion": "Water moves",
        }
    ]
    response = client.post("/api/packs", json=body, headers=AUTH)
    assert response.status_code == 201
    assert response.json()["shots"][0]["duration_sec"] == 8


def test_continuity_mismatch_is_rejected(client) -> None:
    body = pack_body()
    body["shots"][1]["start_state"] = "somewhere else"
    response = client.post("/api/packs", json=body, headers=AUTH)
    assert response.status_code == 422
    assert "start_state" in response.text


def test_stub_dry_run_keeps_gates_false_and_invents_no_urls(client, app) -> None:
    def factory() -> None:
        raise AssertionError("Imagine client must not be built when XAI_API_KEY is unset")

    app.state.imagine_client_factory = factory
    created = client.post("/api/packs", json=pack_body(), headers=AUTH)
    pack_id = created.json()["id"]
    started = client.post(f"/api/packs/{pack_id}/run", headers=AUTH)
    assert started.status_code == 200
    assert started.json()["status"] == "stub"

    jobs = client.get(f"/api/packs/{pack_id}/jobs", headers=AUTH)
    assert jobs.status_code == 200
    payload = jobs.json()
    job = payload["jobs"][0]
    assert job["status"] == "stub"
    assert job["called_imagine_still"] is False
    assert job["produced_still"] is False
    assert job["called_imagine_video"] is False
    assert job["produced_mp4"] is False
    assert job["stitched_episode"] is False
    assert job["episode_path"] is None
    assert job["continuity_mode"] is None
    assert job["grade_match"] is False
    assert "XAI_API_KEY" in job["message"]
    for shot in job["shots"]:
        assert shot["called_imagine_still"] is False
        assert shot["produced_still"] is False
        assert shot["called_imagine_video"] is False
        assert shot["produced_mp4"] is False
        assert shot["still_path"] is None
        assert shot["clip_path"] is None
    text = json.dumps(payload)
    assert "http://" not in text
    assert "https://" not in text

    runs = app.state.store.data_dir / "runs"
    assert list(runs.rglob("*.png")) == []
    assert list(runs.rglob("*.mp4")) == []

    episode = client.get(f"/api/packs/{pack_id}/episode", headers=AUTH)
    assert episode.status_code == 404
    assert "not stitched" in episode.json()["detail"]


def test_second_run_blocked_while_active(client, app) -> None:
    created = client.post("/api/packs", json=pack_body(), headers=AUTH)
    pack_id = created.json()["id"]
    first = client.post(f"/api/packs/{pack_id}/run", headers=AUTH)
    assert first.json()["status"] == "stub"
    app.state.store.update_job(first.json()["job_id"], status="running")
    second = client.post(f"/api/packs/{pack_id}/run", headers=AUTH)
    assert second.status_code == 409


def test_localhost_cors_preflight(client) -> None:
    response = client.options(
        "/api/packs",
        headers={
            "Origin": "http://127.0.0.1:5180",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://127.0.0.1:5180"
