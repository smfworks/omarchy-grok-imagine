from __future__ import annotations

import base64
import json
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest

from omarchy_imagine.imagine import ImagineClient
from omarchy_imagine.moderate import soften_prompts
from tests.samples import AUTH, PNG_BYTES, one_shot_body, pack_body

VIDEO_MODERATION = {
    "code": "Client specified an invalid argument",
    "error": "Generated video rejected by content moderation.",
}
IMAGE_MODERATION = {"error": {"message": "Generated image rejected by content moderation."}}


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


def _violent_shot() -> dict:
    body = one_shot_body()
    body["shots"][0]["prompt_still"] = (
        "The battered and bloody ninja lay dead after a violent bloody clash"
    )
    body["shots"][0]["prompt_motion"] = "The camera rushes in on the killing blow"
    return body


def _png() -> dict:
    return {"data": [{"b64_json": base64.b64encode(PNG_BYTES).decode("ascii")}]}


def _done(url: str = "https://vidgen.x.ai/example/clip.mp4") -> dict:
    return {
        "status": "done",
        "video": {"url": url, "duration": 8, "respect_moderation": True},
    }


def test_soften_replaces_gore_and_changes_on_the_second_attempt() -> None:
    still = "The battered and bloody ninja lay dead after a violent bloody clash"
    motion = "The camera rushes in on the killing blow"
    softened_still, softened_motion = soften_prompts(still, motion, attempt=1)
    assert "bloody" not in softened_still.lower()
    assert "lay dead" not in softened_still.lower()
    assert "violent bloody" not in softened_still.lower()
    assert "killing" not in softened_motion.lower()
    assert "No blood" in softened_still
    assert "choreographed" in softened_still
    again_still, _again_motion = soften_prompts(softened_still, softened_motion, attempt=2)
    assert again_still != softened_still
    assert "Family-safe" in again_still
    assert "No dust" not in again_still
    assert "hard-won pause" not in again_still


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")
def test_still_moderation_retries_once_then_succeeds(client, app, monkeypatch, tmp_path) -> None:
    clip = _mp4(tmp_path)
    image_prompts: list[str] = []
    image_calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/images/generations"):
            image_calls["n"] += 1
            image_prompts.append(json.loads(request.content)["prompt"])
            if image_calls["n"] == 1:
                return httpx.Response(400, json=IMAGE_MODERATION)
            return httpx.Response(200, json=_png())
        if request.url.path.endswith("/videos/generations"):
            return httpx.Response(200, json={"request_id": "req-ok"})
        if "/videos/" in request.url.path:
            return httpx.Response(200, json=_done())
        if request.url.host == "vidgen.x.ai":
            return httpx.Response(200, content=clip)
        return httpx.Response(500, json={"error": {"message": "unexpected"}})

    _install_mock(app, handler, monkeypatch)
    created = client.post("/api/packs", json=_violent_shot(), headers=AUTH)
    pack_id = created.json()["id"]
    started = client.post(f"/api/packs/{pack_id}/run", headers=AUTH)
    assert started.json()["status"] == "done"
    job = client.get(f"/api/packs/{pack_id}/jobs", headers=AUTH).json()["jobs"][0]
    note = job["shots"][0]["moderation"]
    assert note["retry_count"] == 1
    assert note["original_prompt_still"].startswith("The battered and bloody")
    assert note["original_prompt_motion"] == "The camera rushes in on the killing blow"
    assert "bloody" not in note["softened_prompt_still"].lower()
    assert "No blood" in note["softened_prompt_still"]
    assert image_calls["n"] == 2
    assert "No blood" in image_prompts[1]
    assert job["produced_still"] is True
    assert job["produced_mp4"] is True
    assert job["stitched_episode"] is True
    assert "https://" not in json.dumps(note)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")
def test_video_poll_400_retries_without_redoing_the_previous_shot(
    client, app, monkeypatch, tmp_path
) -> None:
    clip = _mp4(tmp_path)
    image_prompts: list[str] = []
    video_prompts: list[str] = []
    moderated = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/images/generations") or path.endswith("/images/edits"):
            image_prompts.append(json.loads(request.content)["prompt"])
            return httpx.Response(200, json=_png())
        if path.endswith("/videos/generations"):
            body = json.loads(request.content)
            video_prompts.append(body["prompt"])
            return httpx.Response(200, json={"request_id": f"req-{len(video_prompts)}"})
        if "/videos/req-" in path:
            request_id = path.rsplit("/", 1)[-1]
            # The second video post is shot 2's first attempt.
            if request_id == "req-2" and moderated["n"] == 0:
                moderated["n"] += 1
                return httpx.Response(400, json=VIDEO_MODERATION)
            return httpx.Response(200, json=_done())
        if request.url.host == "vidgen.x.ai":
            return httpx.Response(200, content=clip)
        return httpx.Response(500, json={"error": {"message": f"unexpected {request.url}"}})

    _install_mock(app, handler, monkeypatch)
    created = client.post("/api/packs", json=pack_body(), headers=AUTH)
    pack_id = created.json()["id"]
    started = client.post(f"/api/packs/{pack_id}/run", headers=AUTH)
    assert started.json()["status"] == "done"
    job = client.get(f"/api/packs/{pack_id}/jobs", headers=AUTH).json()["jobs"][0]
    assert job["shots"][0]["moderation"] is None
    note = job["shots"][1]["moderation"]
    assert note["retry_count"] == 1
    assert note["original_prompt_motion"] == "A slow push in as the fog thins"
    assert "No blood" in note["softened_prompt_motion"]
    assert sum("wooden fishing boat" in prompt for prompt in image_prompts) == 1
    assert sum("fog thinning" in prompt for prompt in image_prompts) == 2
    assert video_prompts.count("The boat eases away from the dock") == 1
    assert sum(prompt == "A slow push in as the fog thins" for prompt in video_prompts) == 1
    assert job["shots"][0]["produced_mp4"] is True
    assert job["shots"][1]["produced_mp4"] is True


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")
def test_moderation_retry_cap_stops_and_keeps_the_finished_shot(
    client, app, monkeypatch, tmp_path
) -> None:
    clip = _mp4(tmp_path)
    image_calls = {"s01": 0, "s02": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/images/generations") or path.endswith("/images/edits"):
            prompt = json.loads(request.content)["prompt"]
            if "wooden fishing boat" in prompt:
                image_calls["s01"] += 1
                return httpx.Response(200, json=_png())
            image_calls["s02"] += 1
            return httpx.Response(400, json=IMAGE_MODERATION)
        if path.endswith("/videos/generations"):
            return httpx.Response(200, json={"request_id": "req-s01"})
        if "/videos/" in path:
            return httpx.Response(200, json=_done())
        if request.url.host == "vidgen.x.ai":
            return httpx.Response(200, content=clip)
        return httpx.Response(500, json={"error": {"message": "unexpected"}})

    _install_mock(app, handler, monkeypatch)
    body = pack_body()
    body["shots"][1]["prompt_still"] = "A violent bloody clash"
    body["shots"][1]["prompt_motion"] = "The killing blow lands"
    created = client.post("/api/packs", json=body, headers=AUTH)
    pack_id = created.json()["id"]
    started = client.post(f"/api/packs/{pack_id}/run", headers=AUTH)
    assert started.json()["status"] == "error"
    job = client.get(f"/api/packs/{pack_id}/jobs", headers=AUTH).json()["jobs"][0]
    assert image_calls["s01"] == 1
    assert image_calls["s02"] == 3
    assert job["shots"][0]["moderation"] is None
    assert job["shots"][0]["produced_mp4"] is True
    assert job["shots"][0]["called_imagine_video"] is True
    failed = job["shots"][1]
    assert failed["moderation"]["retry_count"] == 2
    assert failed["moderation"]["original_prompt_still"] == "A violent bloody clash"
    assert "content moderation" in failed["error"]
    assert failed["produced_still"] is False
    assert failed["produced_mp4"] is False
    assert failed["called_imagine_still"] is True
    assert failed["called_imagine_video"] is False
    assert job["stitched_episode"] is False
    assert "https://" not in json.dumps(failed["moderation"])
