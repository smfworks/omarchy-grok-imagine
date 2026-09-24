"""Look bible, seeded still contract, motion lock, and ffmpeg grade match."""

from __future__ import annotations

import base64
import json
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest

from omarchy_imagine.ffmpeg_util import GradeMatch, match_grade
from omarchy_imagine.fill import FillIn, fill_pack
from omarchy_imagine.imagine import ImagineClient
from omarchy_imagine.moderate import soften_prompts
from omarchy_imagine.pipeline import build_motion_prompt, build_still_prompt
from omarchy_imagine.plan import PlanIn, StoryDraft, plan_pack
from omarchy_imagine.schema import BIBLE_HEADER, LookBible
from tests.samples import AUTH, PNG_BYTES, pack_body

BIBLE = LookBible(
    cast="The same fisher, with the same face and the same body type, in every shot.",
    wardrobe="The same working coat, boots, and knit cap on the fisher, unchanged.",
    palette=(
        "Cool harbor gray, fog white, weathered wood brown, and muted dawn blue, held constant."
    ),
    lighting=(
        "Soft dawn key, low contrast, fog diffusion, and no hard shadow change between shots."
    ),
    camera=(
        "35mm film still, natural color, one grade, no flicker and no lens change between shots."
    ),
)
BLOCK = BIBLE.prompt_block()
SHOT = {
    "prompt_still": "The boat clears the dock",
    "prompt_motion": "The bow rises on a swell",
    "start_state": "The boat is still at the dock.",
    "end_state": "The boat is in open water.",
}


def test_bible_is_injected_into_still_and_motion_prompts() -> None:
    still = build_still_prompt(SHOT, seeded=False, look_bible=BLOCK)
    motion = build_motion_prompt(SHOT, look_bible=BLOCK)
    for prompt in (still, motion):
        assert BIBLE_HEADER in prompt
        assert "The same fisher" in prompt
        assert "working coat" in prompt
        assert "Cool harbor gray" in prompt
        assert "Soft dawn key" in prompt
        assert "35mm film still" in prompt
    assert "The boat clears the dock" in still
    assert "The bow rises on a swell" in motion
    assert "Continue from this exact still" in motion
    assert "Do not change costume, hair, identity, or lighting" in motion
    assert "Only animate the described motion" in motion


def test_seeded_still_prompt_locks_the_source_frame() -> None:
    seeded = build_still_prompt(SHOT, seeded=True, look_bible=BLOCK)
    plain = build_still_prompt(SHOT, seeded=False, look_bible=BLOCK)
    assert "same face" in seeded
    assert "same body type" in seeded
    assert "same clothes" in seeded
    assert "same color grade and lighting" in seeded
    assert "Only pose, blocking, and action may change" in seeded
    assert "Locked end state:" in seeded
    assert "Only pose, blocking, and action may change" not in plain
    contract = "Keep the same face, the same body type, the same clothes"
    assert contract in seeded
    assert contract not in plain
    assert BIBLE_HEADER in plain
    assert plain != seeded


def test_soften_keeps_the_bible_and_rewrites_shot_prose() -> None:
    still = f"A bloody clash on the dock.\n\n{BLOCK}"
    motion = f"{BLOCK}\n\nThe camera rushes in on the killing blow."
    softened_still, softened_motion = soften_prompts(still, motion, attempt=1)
    for prompt in (softened_still, softened_motion):
        assert "The same fisher" in prompt
        assert "working coat" in prompt
        assert "Cool harbor gray" in prompt
        assert "Soft dawn key" in prompt
        assert "35mm film still" in prompt
        assert BIBLE_HEADER in prompt
    assert "bloody" not in softened_still.lower()
    assert "killing" not in softened_motion.lower()
    assert "No blood" in softened_still
    assert "No blood" in softened_motion
    assert softened_still.index(BIBLE_HEADER) < softened_still.lower().index("no blood")


def test_fill_and_plan_return_a_look_bible() -> None:
    filled = fill_pack(
        FillIn(
            title="Harbor dawn",
            logline="A fisher leaves the dock as the fog lifts.",
            shot_count=2,
        )
    )
    _assert_bible(filled.look_bible)
    assert "fisher" in filled.look_bible.cast
    palette = filled.look_bible.palette.lower()
    assert "harbor" in palette or "fog" in palette

    planned = plan_pack(
        PlanIn(
            prompt="A fisher leaves the dock as the fog lifts.",
            target_duration_sec=16,
        )
    )
    _assert_bible(planned.look_bible)
    assert "http://" not in json.dumps(planned.model_dump())


def test_fill_keeps_a_supplied_bible_line_and_fills_the_rest() -> None:
    pack = fill_pack(
        FillIn(
            title="Harbor dawn",
            logline="A fisher leaves the dock as the fog lifts.",
            look_bible=LookBible(cast="Mara, the same weathered face, in every shot."),
            shot_count=2,
        )
    )
    assert pack.look_bible.cast == "Mara, the same weathered face, in every shot."
    assert pack.look_bible.wardrobe
    assert pack.look_bible.palette
    assert pack.look_bible.lighting
    assert pack.look_bible.camera


def test_plan_uses_the_model_bible_and_fills_blanks() -> None:
    story = StoryDraft.model_validate(
        {
            "title": "Harbor dawn",
            "logline": "Fog lifts.",
            "opening_state": "The boat is tied up.",
            "look_bible": {
                "cast": "The same fisher, face unchanged.",
                "wardrobe": "A bloody oilskin, unchanged.",
                "palette": "",
                "lighting": "",
                "camera": "",
            },
            "shots": [
                {
                    "prompt_still": "The boat at the dock",
                    "prompt_motion": "The boat eases off",
                    "end_state": "The boat is clear of the dock.",
                },
                {
                    "prompt_still": "Open water",
                    "prompt_motion": "The fog thins",
                    "end_state": "The fog has lifted.",
                },
            ],
        }
    )

    class FakePlanner:
        def plan_story(self, brief: object) -> StoryDraft:
            return story

    pack = plan_pack(
        PlanIn(prompt="A fisher leaves the dock as the fog lifts.", target_duration_sec=16),
        planner=FakePlanner(),
    )
    assert pack.look_bible.cast == "The same fisher, face unchanged."
    assert "bloody" not in pack.look_bible.wardrobe.lower()
    assert pack.look_bible.palette
    assert pack.look_bible.lighting
    assert pack.look_bible.camera


def test_grade_match_skips_when_filters_are_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("omarchy_imagine.ffmpeg_util.ffmpeg_filter_names", lambda: {"scale"})
    first = tmp_path / "a.mp4"
    second = tmp_path / "b.mp4"
    first.write_bytes(b"a")
    second.write_bytes(b"b")
    result = match_grade([first, second])
    assert result.ran is False
    assert result.clips == [first, second]
    assert "skipped" in result.note.lower()
    assert "eq" in result.note
    assert not (tmp_path / "b.graded.mp4").exists()


def test_grade_match_skips_when_disabled(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OMARCHY_GRADE_MATCH", "0")
    called = {"filters": 0}

    def filters() -> set[str]:
        called["filters"] += 1
        return {"eq", "signalstats"}

    monkeypatch.setattr("omarchy_imagine.ffmpeg_util.ffmpeg_filter_names", filters)
    clip = tmp_path / "only.mp4"
    other = tmp_path / "other.mp4"
    result = match_grade([clip, other])
    assert result == GradeMatch(
        [clip, other],
        False,
        "Grade match skipped: OMARCHY_GRADE_MATCH is off. Clips were concatenated unchanged.",
    )
    assert called["filters"] == 0


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")
def test_grade_match_moves_a_later_clip_toward_clip_1(tmp_path: Path) -> None:
    reference = tmp_path / "ref.mp4"
    bright = tmp_path / "bright.mp4"
    _color_clip(reference, "black")
    _color_clip(bright, "white")
    before = _yavg(bright)
    result = match_grade([reference, bright])
    assert result.ran is True
    assert result.clips[0] == reference
    assert result.clips[1] == tmp_path / "bright.graded.mp4"
    assert result.clips[1].is_file()
    assert "Grade match ran" in result.note
    after = _yavg(result.clips[1])
    target = _yavg(reference)
    assert abs(after - target) < abs(before - target)


def test_grade_match_failure_still_stitches(client, app, monkeypatch, tmp_path) -> None:
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg is not installed")
    clip = _mp4_bytes(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/images/generations") or path.endswith("/images/edits"):
            body = json.loads(request.content)
            assert "The same fisher" in body["prompt"]
            if path.endswith("/images/edits"):
                assert "same face" in body["prompt"]
                assert "Only pose, blocking, and action may change" in body["prompt"]
            else:
                assert "Only pose, blocking, and action may change" not in body["prompt"]
            return httpx.Response(
                200,
                json={"data": [{"b64_json": base64.b64encode(PNG_BYTES).decode("ascii")}]},
            )
        if path.endswith("/videos/generations"):
            body = json.loads(request.content)
            assert "The same fisher" in body["prompt"]
            assert "Continue from this exact still" in body["prompt"]
            return httpx.Response(200, json={"request_id": "req-grade"})
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

    def boom(_paths: list[Path]) -> GradeMatch:
        raise RuntimeError("lut missing")

    monkeypatch.setenv("XAI_API_KEY", "test-key")
    monkeypatch.setattr("omarchy_imagine.pipeline.ffmpeg_util.match_grade", boom)

    def factory() -> ImagineClient:
        return ImagineClient(
            "test-key",
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),
            poll_interval=0,
            poll_timeout=5,
        )

    app.state.imagine_client_factory = factory
    body = pack_body()
    body["look_bible"] = BIBLE.model_dump()
    created = client.post("/api/packs", json=body, headers=AUTH)
    pack_id = created.json()["id"]
    assert created.json()["look_bible"]["cast"] == BIBLE.cast
    started = client.post(f"/api/packs/{pack_id}/run", headers=AUTH)
    assert started.status_code == 200
    assert started.json()["status"] == "done"
    job = client.get(f"/api/packs/{pack_id}/jobs", headers=AUTH).json()["jobs"][0]
    assert job["stitched_episode"] is True
    assert job["grade_match"] is False
    assert "lut missing" in job["message"]
    assert "skipped" in job["message"].lower()
    assert job["shots"][0]["still_mode"] == "text_to_image"
    assert job["shots"][1]["still_mode"] == "last_frame_edit"
    blob = json.dumps(job)
    assert "https://" not in blob
    assert "http://" not in blob


def _assert_bible(bible: LookBible) -> None:
    assert bible.cast.strip()
    assert bible.wardrobe.strip()
    assert bible.palette.strip()
    assert bible.lighting.strip()
    assert bible.camera.strip()


def _color_clip(path: Path, color: str) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c={color}:s=160x90:d=0.4",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-t",
            "0.4",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


def _yavg(path: Path) -> float:
    result = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-i",
            str(path),
            "-t",
            "1",
            "-vf",
            "signalstats,metadata=print",
            "-f",
            "null",
            "-",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    values = [
        float(line.split("YAVG=", 1)[1])
        for line in result.stderr.splitlines()
        if "lavfi.signalstats.YAVG=" in line
    ]
    assert values
    return sum(values) / len(values)


def _mp4_bytes(tmp_path: Path) -> bytes:
    target = tmp_path / "source.mp4"
    _color_clip(target, "navy")
    return target.read_bytes()
