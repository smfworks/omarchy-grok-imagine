"""Phase 2: cast references, per-shot revise, and episode audio."""

from __future__ import annotations

import base64
import json
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest

from omarchy_imagine.castref import ensure_cast_names, select_cast
from omarchy_imagine.config import reference_video_resolution
from omarchy_imagine.ffmpeg_util import has_audio_stream, mix_music_bed, stitch_clips
from omarchy_imagine.imagine import ImagineClient
from omarchy_imagine.plan import PlanIn, plan_pack
from omarchy_imagine.schema import PackIn
from tests.samples import AUTH, PNG_BYTES, pack_body

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")


def _mp4(path: Path, *, seconds: float = 1, audio: bool = False) -> bytes:
    cmd = [
        "ffmpeg",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"color=c=navy:s=320x180:d={seconds}",
    ]
    if audio:
        cmd.extend(
            [
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency=440:sample_rate=44100:d={seconds}",
                "-c:a",
                "aac",
                "-shortest",
            ]
        )
    cmd.extend(
        [
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-t",
            str(seconds),
            str(path),
        ]
    )
    subprocess.run(cmd, check=True, capture_output=True)
    return path.read_bytes()


def _wav(path: Path) -> bytes:
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=220:sample_rate=44100:d=2",
            "-c:a",
            "pcm_s16le",
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path.read_bytes()


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


def _video_handler(clip: bytes, bodies: list[dict]):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith(("/images/generations", "/images/edits", "/videos/generations")):
            bodies.append({"path": path, "json": json.loads(request.content)})
            if path.endswith("/videos/generations"):
                return httpx.Response(200, json={"request_id": f"req-{len(bodies)}"})
            return httpx.Response(
                200,
                json={"data": [{"b64_json": base64.b64encode(PNG_BYTES).decode("ascii")}]},
            )
        if path.endswith("/videos/edits") or path.endswith("/videos/extensions"):
            bodies.append({"path": path, "json": json.loads(request.content)})
            return httpx.Response(200, json={"request_id": f"edit-{len(bodies)}"})
        if "/videos/" in path:
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

    return handler


def _upload_cast(client, name: str, role: str = "character", markers: str = "same face") -> dict:
    response = client.post(
        "/api/references",
        headers=AUTH,
        files={"file": ("ref.png", PNG_BYTES, "image/png")},
        data={"name": name, "role": role, "markers": markers},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_old_pack_without_phase2_fields_is_valid() -> None:
    pack = PackIn.model_validate(
        {
            "title": "Dawn",
            "shots": [
                {"id": "s01", "prompt_still": "A dock", "prompt_motion": "Water moves"},
            ],
        }
    )
    assert pack.cast == []
    assert pack.music_path == ""
    assert pack.shots[0].video_mode == "image_to_video"
    assert pack.shots[0].dialogue == ""
    assert pack.shots[0].voice_id == ""


def test_voice_id_requires_reference_mode() -> None:
    with pytest.raises(ValueError, match="reference_to_video"):
        PackIn.model_validate(
            {
                "title": "Dawn",
                "shots": [
                    {
                        "id": "s01",
                        "prompt_still": "A dock",
                        "prompt_motion": "Water moves",
                        "voice_id": "eve",
                    }
                ],
            }
        )


def test_reference_resolution_cap() -> None:
    assert reference_video_resolution("720p") == ("720p", None)
    sent, note = reference_video_resolution("1080p")
    assert sent == "720p"
    assert note is not None and "720p" in note


def test_select_cast_prefers_characters() -> None:
    cast = [
        {"name": "Skiff", "role": "prop"},
        {"name": "Mara", "role": "character"},
        {"name": "Dock", "role": "location"},
        {"name": "Ned", "role": "character"},
        {"name": "Lantern", "role": "prop"},
    ]
    picked = [item["name"] for item in select_cast(cast, 3)]
    assert picked == ["Mara", "Ned", "Skiff"]
    assert "Mara" in ensure_cast_names("A quiet dock.", cast)


def test_reference_upload_and_reject_bad_type(client) -> None:
    saved = _upload_cast(client, "Mara")
    assert saved["image_path"].startswith("references/")
    assert saved["name"] == "Mara"
    fetched = client.get(f"/api/references/{saved['id']}", headers=AUTH)
    assert fetched.status_code == 200
    assert fetched.content == PNG_BYTES
    rejected = client.post(
        "/api/references",
        headers=AUTH,
        files={"file": ("note.txt", b"hello", "text/plain")},
        data={"name": "Mara", "role": "character", "markers": ""},
    )
    assert rejected.status_code == 422
    missing = client.post(
        "/api/packs",
        headers=AUTH,
        json={
            **pack_body(),
            "cast": [
                {
                    "id": "missing",
                    "name": "Mara",
                    "role": "character",
                    "markers": "",
                    "image_path": "references/missing.png",
                }
            ],
        },
    )
    assert missing.status_code == 422


def test_plan_mentions_cast_names() -> None:
    pack = plan_pack(
        PlanIn(
            prompt="A fisher leaves the dock as the fog lifts.",
            target_duration_sec=16,
            cast=[
                {
                    "id": "mara",
                    "name": "Mara",
                    "role": "character",
                    "markers": "grey coat",
                    "image_path": "",
                }
            ],
        )
    )
    blob = json.dumps(pack.model_dump())
    assert "Mara" in blob
    assert "http://" not in blob
    assert "https://" not in blob


@needs_ffmpeg
def test_cast_stills_and_reference_video(client, app, monkeypatch, tmp_path) -> None:
    clip = _mp4(tmp_path / "clip.mp4", audio=True)
    bodies: list[dict] = []
    _install_mock(app, _video_handler(clip, bodies), monkeypatch)
    mara = _upload_cast(client, "Mara", markers="grey coat")
    skiff = _upload_cast(client, "Skiff", role="prop", markers="blue hull")
    body = pack_body()
    body["resolution"] = "1080p"
    body["cast"] = [mara, skiff]
    body["shots"][0]["video_mode"] = "image_to_video"
    body["shots"][1]["video_mode"] = "reference_to_video"
    body["shots"][1]["voice_id"] = "eve"
    body["shots"][1]["dialogue"] = "The fog is lifting."
    created = client.post("/api/packs", json=body, headers=AUTH)
    assert created.status_code == 201, created.text
    pack_id = created.json()["id"]
    started = client.post(f"/api/packs/{pack_id}/run", headers=AUTH)
    assert started.json()["status"] == "done", started.text
    job = client.get(f"/api/packs/{pack_id}/jobs", headers=AUTH).json()["jobs"][0]
    assert job["shots"][0]["still_mode"] == "cast_reference"
    assert job["shots"][1]["still_mode"] == "last_frame_edit"
    assert job["shots"][1]["video_mode"] == "reference_to_video"
    assert job["shots"][1]["note"]
    assert "720p" in job["shots"][1]["note"]
    assert job["has_audio"] is True
    assert job["music_bed_applied"] is False
    opening = next(item for item in bodies if item["path"].endswith("/images/edits"))
    assert "images" in opening["json"]
    assert len(opening["json"]["images"]) == 2
    assert "Mara" in opening["json"]["prompt"]
    videos = [item for item in bodies if item["path"].endswith("/videos/generations")]
    assert "image" in videos[0]["json"]
    assert "reference_images" not in videos[0]["json"]
    assert "image" not in videos[1]["json"]
    assert len(videos[1]["json"]["reference_images"]) == 2
    assert videos[1]["json"]["resolution"] == "720p"
    assert videos[1]["json"]["reference_audios"] == [{"voice_id": "eve"}]
    assert "<AUDIO_0>" in videos[1]["json"]["prompt"]
    assert "The fog is lifting." in videos[1]["json"]["prompt"]
    assert "https://" not in json.dumps(job)


@needs_ffmpeg
def test_regenerate_edit_extend_and_versions(client, app, monkeypatch, tmp_path) -> None:
    clip_a = _mp4(tmp_path / "a.mp4", audio=True)
    clip_b = _mp4(tmp_path / "b.mp4", seconds=1, audio=False)
    clips = {"current": clip_a}
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith(("/images/generations", "/images/edits")):
            bodies.append({"path": path, "json": json.loads(request.content)})
            return httpx.Response(
                200,
                json={"data": [{"b64_json": base64.b64encode(PNG_BYTES).decode("ascii")}]},
            )
        if path.endswith(("/videos/generations", "/videos/edits", "/videos/extensions")):
            bodies.append({"path": path, "json": json.loads(request.content)})
            return httpx.Response(200, json={"request_id": f"req-{len(bodies)}"})
        if "/videos/" in path:
            return httpx.Response(
                200,
                json={
                    "status": "done",
                    "video": {
                        "url": "https://vidgen.x.ai/example/clip.mp4",
                        "duration": 1,
                        "respect_moderation": True,
                    },
                },
            )
        if request.url.host == "vidgen.x.ai":
            return httpx.Response(200, content=clips["current"])
        return httpx.Response(500, json={"error": {"message": f"unexpected {request.url}"}})

    _install_mock(app, handler, monkeypatch)
    created = client.post("/api/packs", json=pack_body(), headers=AUTH)
    pack_id = created.json()["id"]
    started = client.post(f"/api/packs/{pack_id}/run", headers=AUTH)
    assert started.json()["status"] == "done", started.text
    store = app.state.store
    shot1 = store.shot_dir(pack_id, "s01") / "clip.mp4"
    original_shot1 = shot1.read_bytes()
    before = len(bodies)

    clips["current"] = clip_b
    revised = client.post(
        f"/api/packs/{pack_id}/jobs/{started.json()['job_id']}/shots/s02/regenerate",
        headers=AUTH,
        json={"prompt_motion": "The bow lifts on a slower swell."},
    )
    assert revised.status_code == 200, revised.text
    job = revised.json()
    assert job["status"] == "done"
    assert job["stitched_episode"] is True
    shot = next(item for item in job["shots"] if item["id"] == "s02")
    assert [item["action"] for item in shot["revisions"]] == ["generate", "regenerate"]
    assert shot["revisions"][0]["clip_path"].endswith("/shots/s02/clip.v1.mp4")
    archived = store.data_dir / shot["revisions"][0]["clip_path"]
    assert archived.is_file()
    assert archived.read_bytes() == clip_a
    assert (store.shot_dir(pack_id, "s02") / "clip.mp4").read_bytes() == clip_b
    assert shot1.read_bytes() == original_shot1
    assert len(bodies) == before + 2
    regen_video = [item for item in bodies[before:] if item["path"].endswith("/videos/generations")]
    assert regen_video and "slower swell" in regen_video[0]["json"]["prompt"]

    edited = client.post(
        f"/api/packs/{pack_id}/jobs/{job['id']}/shots/s02/edit",
        headers=AUTH,
        json={"prompt": "Add a gull on the rail."},
    )
    assert edited.status_code == 200, edited.text
    edit_body = next(item for item in bodies if item["path"].endswith("/videos/edits"))
    assert edit_body["json"]["model"] == "grok-imagine-video"
    assert edit_body["json"]["video"]["url"].startswith("data:video/mp4;base64,")
    assert "duration" not in edit_body["json"]
    edited_shot = next(item for item in edited.json()["shots"] if item["id"] == "s02")
    assert edited_shot["revisions"][-1]["action"] == "edit"
    assert any(item["clip_path"].endswith("clip.v2.mp4") for item in edited_shot["revisions"])

    extended = client.post(
        f"/api/packs/{pack_id}/jobs/{job['id']}/shots/s02/extend",
        headers=AUTH,
        json={"prompt": "The boat keeps going.", "duration_sec": 4},
    )
    assert extended.status_code == 200, extended.text
    extend_body = next(item for item in bodies if item["path"].endswith("/videos/extensions"))
    assert extend_body["json"]["model"] == "grok-imagine-video"
    assert extend_body["json"]["duration"] == 4
    assert extended.json()["stitched_episode"] is True
    assert "https://" not in json.dumps(extended.json())


@needs_ffmpeg
def test_edit_refuses_a_long_clip_without_calling_imagine(
    client, app, monkeypatch, tmp_path
) -> None:
    clip = _mp4(tmp_path / "short.mp4")
    bodies: list[dict] = []
    _install_mock(app, _video_handler(clip, bodies), monkeypatch)
    created = client.post("/api/packs", json=pack_body(), headers=AUTH)
    pack_id = created.json()["id"]
    started = client.post(f"/api/packs/{pack_id}/run", headers=AUTH)
    assert started.json()["status"] == "done"
    long_clip = tmp_path / "long.mp4"
    _mp4(long_clip, seconds=9)
    target = app.state.store.shot_dir(pack_id, "s01") / "clip.mp4"
    target.write_bytes(long_clip.read_bytes())
    calls = len(bodies)
    response = client.post(
        f"/api/packs/{pack_id}/jobs/{started.json()['job_id']}/shots/s01/edit",
        headers=AUTH,
        json={"prompt": "Change the sky."},
    )
    assert response.status_code == 422
    assert "8.7" in response.json()["detail"]
    assert len(bodies) == calls
    job = client.get(f"/api/packs/{pack_id}/jobs", headers=AUTH).json()["jobs"][0]
    assert job["status"] == "done"
    assert job["stitched_episode"] is True


def test_stub_revise_does_not_call_imagine(client, app) -> None:
    def refuse() -> None:
        raise AssertionError("stub revise must not call Imagine")

    app.state.imagine_client_factory = refuse
    created = client.post("/api/packs", json=pack_body(), headers=AUTH)
    pack_id = created.json()["id"]
    started = client.post(f"/api/packs/{pack_id}/run", headers=AUTH)
    assert started.json()["status"] == "stub"
    response = client.post(
        f"/api/packs/{pack_id}/jobs/{started.json()['job_id']}/shots/s01/regenerate",
        headers=AUTH,
        json={},
    )
    assert response.status_code == 422
    assert "unset" in response.json()["detail"]


@needs_ffmpeg
def test_mixed_audio_and_music_bed(client, app, monkeypatch, tmp_path) -> None:
    silent = tmp_path / "silent.mp4"
    audible = tmp_path / "audible.mp4"
    _mp4(silent, audio=False)
    _mp4(audible, audio=True)
    episode = tmp_path / "episode.mp4"
    stitch_clips([silent, audible], episode)
    assert has_audio_stream(episode) is True

    music = tmp_path / "bed.wav"
    _wav(music)
    note = mix_music_bed(episode, music)
    assert "Music bed applied" in note
    assert has_audio_stream(episode) is True

    clip = _mp4(tmp_path / "run.mp4", audio=False)
    bodies: list[dict] = []
    _install_mock(app, _video_handler(clip, bodies), monkeypatch)
    uploaded = client.post(
        "/api/music",
        headers=AUTH,
        files={"file": ("bed.wav", music.read_bytes(), "audio/wav")},
    )
    assert uploaded.status_code == 201, uploaded.text
    body = pack_body()
    body["music_path"] = uploaded.json()["music_path"]
    created = client.post("/api/packs", json=body, headers=AUTH)
    assert created.status_code == 201, created.text
    pack_id = created.json()["id"]
    started = client.post(f"/api/packs/{pack_id}/run", headers=AUTH)
    assert started.json()["status"] == "done", started.text
    job = client.get(f"/api/packs/{pack_id}/jobs", headers=AUTH).json()["jobs"][0]
    assert job["music_bed_applied"] is True
    assert job["has_audio"] is True
    episode_path = app.state.store.data_dir / job["episode_path"]
    assert has_audio_stream(episode_path) is True
    assert (app.state.store.runs_root / pack_id / "episode.base.mp4").is_file()
