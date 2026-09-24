from __future__ import annotations

import base64
import json
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest

from omarchy_imagine.imagine import ImagineClient
from tests.samples import AUTH, PNG_BYTES, one_shot_body, pack_body


def _mp4(tmp_path: Path) -> bytes:
    target = tmp_path / "source.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=navy:s=320x180:d=1",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-t",
            "1",
            str(target),
        ],
        check=True,
        capture_output=True,
    )
    return target.read_bytes()


def _install_mock(app, handler, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XAI_API_KEY", "test-key")

    def factory() -> ImagineClient:
        return ImagineClient(
            "test-key",
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),
            poll_interval=0,
            poll_timeout=5,
        )

    app.state.imagine_client_factory = factory


def test_video_failure_sets_called_gate_and_leaves_mp4_false(client, app, monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/images/generations"):
            return httpx.Response(
                200,
                json={"data": [{"b64_json": base64.b64encode(PNG_BYTES).decode("ascii")}]},
            )
        if request.url.path.endswith("/videos/generations"):
            return httpx.Response(200, json={"request_id": "req-fail"})
        if "/videos/" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "status": "failed",
                    "error": {"code": "internal_error", "message": "render failed"},
                },
            )
        return httpx.Response(500, json={"error": {"message": "unexpected"}})

    _install_mock(app, handler, monkeypatch)
    created = client.post("/api/packs", json=one_shot_body(), headers=AUTH)
    pack_id = created.json()["id"]
    started = client.post(f"/api/packs/{pack_id}/run", headers=AUTH)
    assert started.status_code == 200
    assert started.json()["status"] == "error"
    job = client.get(f"/api/packs/{pack_id}/jobs", headers=AUTH).json()["jobs"][0]
    assert job["called_imagine_still"] is True
    assert job["produced_still"] is True
    assert job["called_imagine_video"] is True
    assert job["produced_mp4"] is False
    assert job["stitched_episode"] is False
    assert job["episode_path"] is None
    assert job["shots"][0]["still_path"].endswith("/shots/s01/still.png")
    assert job["shots"][0]["clip_path"] is None
    assert "https://" not in json.dumps(job)
    episode = client.get(f"/api/packs/{pack_id}/episode", headers=AUTH)
    assert episode.status_code == 404


def test_prose_regenerate_when_ffmpeg_missing(client, app, monkeypatch) -> None:
    monkeypatch.setattr("omarchy_imagine.ffmpeg_util.ffmpeg_path", lambda: None)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/images/generations"):
            return httpx.Response(
                200,
                json={"data": [{"b64_json": base64.b64encode(PNG_BYTES).decode("ascii")}]},
            )
        if request.url.path.endswith("/images/edits"):
            return httpx.Response(500, json={"error": {"message": "edit should not be called"}})
        if request.url.path.endswith("/videos/generations"):
            return httpx.Response(200, json={"request_id": f"req-{len(calls)}"})
        if "/videos/" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "status": "done",
                    "video": {
                        "url": "https://vidgen.x.ai/example/clip.mp4",
                        "duration": 8,
                        "respect_moderation": True,
                    },
                },
            )
        if request.url.host == "vidgen.x.ai":
            return httpx.Response(200, content=b"not-a-real-mp4")
        return httpx.Response(500, json={"error": {"message": "unexpected"}})

    _install_mock(app, handler, monkeypatch)
    created = client.post("/api/packs", json=pack_body(), headers=AUTH)
    pack_id = created.json()["id"]
    started = client.post(f"/api/packs/{pack_id}/run", headers=AUTH)
    assert started.json()["status"] == "error"
    job = client.get(f"/api/packs/{pack_id}/jobs", headers=AUTH).json()["jobs"][0]
    assert job["continuity_mode"] == "prose_regenerate"
    assert job["produced_mp4"] is True
    assert job["stitched_episode"] is False
    assert job["shots"][0]["still_mode"] == "text_to_image"
    assert job["shots"][1]["still_mode"] == "text_to_image"
    assert "/images/edits" not in "".join(calls)
    assert sum(path.endswith("/images/generations") for path in calls) == 2


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")
def test_last_frame_seed_and_ffmpeg_stitch(client, app, monkeypatch, tmp_path) -> None:
    clip = _mp4(tmp_path)
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/images/generations") or path.endswith("/images/edits"):
            bodies.append({"path": request.url.path, "json": json.loads(request.content)})
            return httpx.Response(
                200,
                json={"data": [{"b64_json": base64.b64encode(PNG_BYTES).decode("ascii")}]},
            )
        if request.url.path.endswith("/videos/generations"):
            bodies.append({"path": request.url.path, "json": json.loads(request.content)})
            return httpx.Response(200, json={"request_id": f"req-{len(bodies)}"})
        if "/videos/" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "status": "done",
                    "video": {
                        "url": "https://vidgen.x.ai/example/clip.mp4",
                        "duration": 8,
                        "respect_moderation": True,
                    },
                },
            )
        if request.url.host == "vidgen.x.ai":
            return httpx.Response(200, content=clip)
        return httpx.Response(500, json={"error": {"message": f"unexpected {request.url}"}})

    _install_mock(app, handler, monkeypatch)
    created = client.post("/api/packs", json=pack_body(), headers=AUTH)
    pack_id = created.json()["id"]
    started = client.post(f"/api/packs/{pack_id}/run", headers=AUTH)
    assert started.status_code == 200
    assert started.json()["status"] == "done"
    job = client.get(f"/api/packs/{pack_id}/jobs", headers=AUTH).json()["jobs"][0]
    assert job["continuity_mode"] == "last_frame_edit"
    assert job["called_imagine_still"] is True
    assert job["produced_still"] is True
    assert job["called_imagine_video"] is True
    assert job["produced_mp4"] is True
    assert job["stitched_episode"] is True
    assert job["episode_path"] == f"runs/{pack_id}/episode.mp4"
    assert job["shots"][0]["still_mode"] == "text_to_image"
    assert job["shots"][1]["still_mode"] == "last_frame_edit"
    assert job["shots"][0]["last_frame_path"].endswith("/shots/s01/last_frame.png")
    assert job["shots"][0]["still_path"].endswith("/shots/s01/still.png")
    assert job["shots"][1]["clip_path"].endswith("/shots/s02/clip.mp4")

    edit = next(item for item in bodies if item["path"].endswith("/images/edits"))
    assert edit["json"]["image"]["url"].startswith("data:image/")
    assert "Locked start state:" in edit["json"]["prompt"]
    assert "same face" in edit["json"]["prompt"]
    assert "Only pose, blocking, and action may change" in edit["json"]["prompt"]
    opening = next(item for item in bodies if item["path"].endswith("/images/generations"))
    assert "Only pose, blocking, and action may change" not in opening["json"]["prompt"]
    videos = [item for item in bodies if item["path"].endswith("/videos/generations")]
    assert videos[0]["json"]["model"] == "grok-imagine-video-1.5"
    assert videos[0]["json"]["image"]["url"].startswith("data:image/png;base64,")
    assert "Continue from this exact still" in videos[0]["json"]["prompt"]
    assert "Do not change costume, hair, identity, or lighting" in videos[1]["json"]["prompt"]
    assert job["grade_match"] is True
    assert "Grade match ran" in job["message"]

    episode = client.get(f"/api/packs/{pack_id}/episode", headers=AUTH)
    assert episode.status_code == 200
    assert episode.headers["content-type"].startswith("video/mp4")
    assert episode.content[:8] != b""
    episode_path = app.state.store.data_dir / "runs" / pack_id / "episode.mp4"
    assert episode_path.is_file()

    # A later stub run clears artifacts and must not keep serving the old file.
    monkeypatch.delenv("XAI_API_KEY", raising=False)

    def refuse() -> None:
        raise AssertionError("stub run must not call Imagine")

    app.state.imagine_client_factory = refuse
    rerun = client.post(f"/api/packs/{pack_id}/run", headers=AUTH)
    assert rerun.json()["status"] == "stub"
    episode_after = client.get(f"/api/packs/{pack_id}/episode", headers=AUTH)
    assert episode_after.status_code == 404
    latest = client.get(f"/api/packs/{pack_id}/jobs", headers=AUTH).json()["jobs"][0]
    assert latest["id"] == rerun.json()["job_id"]
    assert latest["stitched_episode"] is False
    assert latest["episode_path"] is None
