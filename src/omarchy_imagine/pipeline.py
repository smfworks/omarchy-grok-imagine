"""Run a pack: Imagine stills, image-to-video, optional last-frame seed, ffmpeg stitch.

Continuity when ffmpeg is on PATH (``last_frame_edit``):

1. Shot 1 still is text-to-image (``POST /v1/images/generations``).
2. That still is the image-to-video first frame (``POST /v1/videos/generations``).
3. ffmpeg extracts the clip's last frame.
4. The next still is an image edit (``POST /v1/images/edits``) seeded by that frame,
   with the shot prompt and locked start/end state in the edit prompt.
5. Repeat. ffmpeg concat writes ``episode.mp4``. Imagine is not used to stitch.

When ffmpeg is missing (``prose_regenerate``), every still is text-to-image and
the locked states are written into the prompt. ``stitched_episode`` stays false
because concat did not run.

Stub mode (no ``XAI_API_KEY``) does not construct an Imagine client, does not
write media, and leaves every gate false.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from omarchy_imagine import ffmpeg_util
from omarchy_imagine.config import image_resolution_for
from omarchy_imagine.db import Store, aggregate_gates
from omarchy_imagine.ffmpeg_util import FfmpegError, FfmpegNotFound
from omarchy_imagine.imagine import ImagineClient, ImagineError
from omarchy_imagine.moderate import MAX_MODERATION_RETRIES, soften_prompts

logger = logging.getLogger("omarchy_imagine")

STUB_MESSAGE = (
    "XAI_API_KEY is unset. Stub dry-run: the pack was accepted and the job was "
    "queued, but no Imagine request was sent and no media URLs were created. "
    "Gates stay false."
)


def run_pack_job(
    store: Store,
    job_id: str,
    client_factory: Callable[[], ImagineClient],
) -> None:
    job = store.get_job(job_id)
    if job is None:
        logger.error("job %s disappeared before run", job_id)
        return
    pack = store.get_pack(job["pack_id"])
    if pack is None:
        store.update_job(job_id, status="error", error="Pack not found")
        return

    store.clear_pack_runs(pack["id"])
    api_key = os.environ.get("XAI_API_KEY", "").strip()
    if not api_key:
        store.update_job(
            job_id,
            status="stub",
            message=STUB_MESSAGE,
            error=None,
            continuity_mode=None,
            shots=job["shots"],
            gates=aggregate_gates(job["shots"], stitched=False),
            episode_path=None,
        )
        logger.info("job %s stub (no XAI_API_KEY)", job_id)
        return

    continuity = "last_frame_edit" if ffmpeg_util.ffmpeg_path() else "prose_regenerate"
    store.update_job(
        job_id,
        status="running",
        continuity_mode=continuity,
        message=None,
        error=None,
    )
    try:
        client = client_factory()
    except Exception as exc:
        store.update_job(job_id, status="error", error=f"Imagine client was not created: {exc}")
        logger.exception("job %s client factory failed", job_id)
        return

    try:
        _run_live(store, job_id, pack, client, continuity)
    except Exception as exc:
        current = store.get_job(job_id)
        if current is None:
            logger.exception("job %s missing after failure", job_id)
            return
        store.update_job(
            job_id,
            status="error",
            error=str(exc),
            shots=current["shots"],
            gates=current["gates"],
            episode_path=current["episode_path"],
        )
        logger.info("job %s error: %s", job_id, exc)
    finally:
        client.close()


def build_still_prompt(shot: dict[str, Any], *, seeded: bool) -> str:
    parts: list[str] = []
    if seeded:
        parts.append(
            "Use the provided frame as the exact opening of this shot. "
            "Preserve identity, wardrobe, lighting, and setting. "
            "Advance the image only as far as the still prompt and end state require."
        )
    parts.append(str(shot["prompt_still"]).strip())
    if str(shot.get("start_state") or "").strip():
        parts.append(f"Locked start state: {shot['start_state'].strip()}")
    if str(shot.get("end_state") or "").strip():
        parts.append(f"Locked end state: {shot['end_state'].strip()}")
    return "\n\n".join(parts)


def write_still_artifact(image_bytes: bytes, shot_dir: Path) -> Path:
    """Write ``still.png``. JPEG or WebP bytes are transcoded with ffmpeg."""
    shot_dir.mkdir(parents=True, exist_ok=True)
    png_path = shot_dir / "still.png"
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        png_path.write_bytes(image_bytes)
        return png_path
    if image_bytes.startswith(b"\xff\xd8\xff"):
        suffix = ".jpg"
    elif image_bytes.startswith(b"RIFF") and image_bytes[8:12] == b"WEBP":
        suffix = ".webp"
    else:
        suffix = ".img"
    raw_path = shot_dir / f"still-source{suffix}"
    raw_path.write_bytes(image_bytes)
    if ffmpeg_util.ffmpeg_path():
        ffmpeg_util.transcode_to_png(raw_path, png_path)
        return png_path
    return raw_path


def _run_live(
    store: Store,
    job_id: str,
    pack: dict[str, Any],
    client: ImagineClient,
    continuity: str,
) -> None:
    records: list[dict[str, Any]] = [
        {
            "id": shot["id"],
            "called_imagine_still": False,
            "produced_still": False,
            "called_imagine_video": False,
            "produced_mp4": False,
            "still_path": None,
            "clip_path": None,
            "last_frame_path": None,
            "still_mode": None,
            "video_request_id": None,
            "error": None,
        }
        for shot in pack["shots"]
    ]
    _persist(store, job_id, records, stitched=False, episode_rel=None)
    image_resolution = image_resolution_for(pack["resolution"])
    last_frame: Path | None = None
    clip_paths: list[Path] = []

    for index, shot in enumerate(pack["shots"]):
        record = records[index]
        shot_dir = store.shot_dir(pack["id"], shot["id"])
        seed_frame = last_frame if index > 0 and continuity == "last_frame_edit" else None
        clip_path = _render_shot(
            store,
            job_id,
            pack,
            client,
            records,
            record,
            shot,
            shot_dir,
            seed_frame,
            image_resolution,
        )
        clip_paths.append(clip_path)

        if index < len(pack["shots"]) - 1 and continuity == "last_frame_edit":
            frame_path = shot_dir / "last_frame.png"
            try:
                ffmpeg_util.extract_last_frame(clip_path, frame_path)
            except (FfmpegError, FfmpegNotFound) as exc:
                record["error"] = str(exc)
                _persist(store, job_id, records, stitched=False, episode_rel=None)
                raise
            record["last_frame_path"] = store.rel(frame_path)
            last_frame = frame_path
            _persist(store, job_id, records, stitched=False, episode_rel=None)

    episode_path = store.episode_file(pack["id"])
    try:
        ffmpeg_util.stitch_clips(clip_paths, episode_path)
    except (FfmpegError, FfmpegNotFound):
        if episode_path.exists():
            episode_path.unlink()
        raise
    episode_rel = store.rel(episode_path)
    _persist(store, job_id, records, stitched=True, episode_rel=episode_rel)
    store.update_job(
        job_id,
        status="done",
        error=None,
        message="Episode stitched with ffmpeg.",
        shots=records,
        gates=aggregate_gates(records, stitched=True),
        episode_path=episode_rel,
    )
    logger.info("job %s done episode=%s", job_id, episode_rel)


def _render_shot(
    store: Store,
    job_id: str,
    pack: dict[str, Any],
    client: ImagineClient,
    records: list[dict[str, Any]],
    record: dict[str, Any],
    shot: dict[str, Any],
    shot_dir: Path,
    seed_frame: Path | None,
    image_resolution: str,
) -> Path:
    """Render one shot, softening and resubmitting when moderation rejects it.

    Earlier shots are not rendered again. A moderation rejection rewrites this
    shot's still and motion prompts and tries the shot over, up to
    ``MAX_MODERATION_RETRIES`` rewrites. The still is generated again when the
    still prompt changed, including after a video rejection, because the
    rejected clip was seeded by that still.
    """
    working_still = str(shot["prompt_still"])
    working_motion = str(shot["prompt_motion"])
    original_still = working_still
    original_motion = working_motion
    rewrites = 0
    while True:
        prompt = build_still_prompt(
            {**shot, "prompt_still": working_still},
            seeded=seed_frame is not None,
        )
        try:
            if seed_frame is not None:
                image_bytes = client.edit_still(
                    prompt,
                    seed_frame.read_bytes(),
                    pack["aspect_ratio"],
                    image_resolution,
                )
                record["still_mode"] = "last_frame_edit"
            else:
                image_bytes = client.generate_still(
                    prompt,
                    pack["aspect_ratio"],
                    image_resolution,
                )
                record["still_mode"] = "text_to_image"
            record["called_imagine_still"] = True
        except ImagineError as exc:
            record["called_imagine_still"] = exc.request_sent or record["called_imagine_still"]
            if _retry_moderation(
                store,
                job_id,
                records,
                record,
                exc,
                rewrites,
                original_still,
                original_motion,
                working_still,
                working_motion,
            ):
                rewrites += 1
                working_still, working_motion = _softened(record)
                continue
            record["error"] = str(exc)
            _persist(store, job_id, records, stitched=False, episode_rel=None)
            raise
        try:
            still_path = write_still_artifact(image_bytes, shot_dir)
        except (FfmpegError, FfmpegNotFound, OSError) as exc:
            record["error"] = str(exc)
            _persist(store, job_id, records, stitched=False, episode_rel=None)
            raise
        record["produced_still"] = True
        record["still_path"] = store.rel(still_path)
        record["error"] = None
        _persist(store, job_id, records, stitched=False, episode_rel=None)

        try:
            clip_bytes, request_id = client.image_to_video(
                prompt=working_motion,
                image=still_path.read_bytes(),
                duration_sec=int(shot["duration_sec"]),
                aspect_ratio=pack["aspect_ratio"],
                resolution=pack["resolution"],
            )
            record["called_imagine_video"] = True
            record["video_request_id"] = request_id
        except ImagineError as exc:
            record["called_imagine_video"] = exc.request_sent or record["called_imagine_video"]
            record["video_request_id"] = exc.request_id or record["video_request_id"]
            if _retry_moderation(
                store,
                job_id,
                records,
                record,
                exc,
                rewrites,
                original_still,
                original_motion,
                working_still,
                working_motion,
            ):
                rewrites += 1
                working_still, working_motion = _softened(record)
                continue
            record["error"] = str(exc)
            _persist(store, job_id, records, stitched=False, episode_rel=None)
            raise

        clip_path = shot_dir / "clip.mp4"
        clip_path.write_bytes(clip_bytes)
        if not clip_path.is_file() or clip_path.stat().st_size == 0:
            record["error"] = "Imagine video download was empty"
            _persist(store, job_id, records, stitched=False, episode_rel=None)
            raise ImagineError("Imagine video download was empty", request_sent=True)
        record["produced_mp4"] = True
        record["clip_path"] = store.rel(clip_path)
        record["error"] = None
        _persist(store, job_id, records, stitched=False, episode_rel=None)
        return clip_path


def _retry_moderation(
    store: Store,
    job_id: str,
    records: list[dict[str, Any]],
    record: dict[str, Any],
    exc: ImagineError,
    rewrites: int,
    original_still: str,
    original_motion: str,
    working_still: str,
    working_motion: str,
) -> bool:
    if not exc.moderation or rewrites >= MAX_MODERATION_RETRIES:
        return False
    softened_still, softened_motion = soften_prompts(
        working_still,
        working_motion,
        attempt=rewrites + 1,
    )
    record["moderation"] = {
        "retry_count": rewrites + 1,
        "original_prompt_still": original_still,
        "original_prompt_motion": original_motion,
        "softened_prompt_still": softened_still,
        "softened_prompt_motion": softened_motion,
    }
    # The rejected still is not the delivered frame. The next pass writes a new one.
    record["produced_still"] = False
    record["still_path"] = None
    record["produced_mp4"] = False
    record["clip_path"] = None
    record["error"] = None
    _persist(store, job_id, records, stitched=False, episode_rel=None)
    return True


def _softened(record: dict[str, Any]) -> tuple[str, str]:
    note = record["moderation"]
    return str(note["softened_prompt_still"]), str(note["softened_prompt_motion"])


def _persist(
    store: Store,
    job_id: str,
    records: list[dict[str, Any]],
    *,
    stitched: bool,
    episode_rel: str | None,
) -> None:
    store.update_job(
        job_id,
        shots=records,
        gates=aggregate_gates(records, stitched=stitched),
        episode_path=episode_rel,
    )
