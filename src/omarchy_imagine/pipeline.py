"""Run a pack: Imagine stills, image-to-video, optional last-frame seed, ffmpeg stitch.

Continuity when ffmpeg is on PATH (``last_frame_edit``):

1. Shot 1 still is text-to-image (``POST /v1/images/generations``).
2. That still is the image-to-video first frame (``POST /v1/videos/generations``).
3. ffmpeg extracts the clip's last frame.
4. The next still is an image edit (``POST /v1/images/edits``) seeded by that frame.
   The edit prompt keeps the look bible and the source frame's face, clothes,
   and grade. Only pose, blocking, and action may change.
5. Every image-to-video prompt repeats the look bible and tells the model to
   animate that still without changing costume, hair, identity, or lighting.
6. Before concat, ffmpeg may soft-match later clips toward clip 1. A missing
   filter skips that pass and still stitches. Imagine is not used to stitch.

When ffmpeg is missing (``prose_regenerate``), every still is text-to-image and
the locked states are written into the prompt. ``stitched_episode`` stays false
because concat did not run.

Stub mode (no ``XAI_API_KEY``) does not construct an Imagine client, does not
write media, and leaves every gate false.
"""

from __future__ import annotations

import logging
import os
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

from omarchy_imagine import ffmpeg_util
from omarchy_imagine.castref import (
    cast_lock_line,
    image_tag_line,
    select_cast,
    video_reference_line,
)
from omarchy_imagine.config import (
    EDIT_MAX_INPUT_SEC,
    MAX_REFERENCE_IMAGES,
    image_resolution_for,
    reference_video_resolution,
)
from omarchy_imagine.db import ReviseRejected, Store, aggregate_gates, utcnow
from omarchy_imagine.ffmpeg_util import FfmpegError, FfmpegNotFound
from omarchy_imagine.imagine import ImagineClient, ImagineError
from omarchy_imagine.moderate import MAX_MODERATION_RETRIES, soften_prompts
from omarchy_imagine.schema import render_look_bible

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


SEEDED_STILL_CONTRACT = (
    "Use the provided frame as the exact visual source. "
    "Keep the same face, the same body type, the same clothes, "
    "and the same color grade and lighting as that source frame. "
    "Only pose, blocking, and action may change, and only as the still prompt "
    "and the locked end state require."
)

MOTION_SEED_LOCK = (
    "Continue from this exact still. "
    "Do not change costume, hair, identity, or lighting. "
    "Only animate the described motion."
)


def build_still_prompt(
    shot: dict[str, Any],
    *,
    seeded: bool,
    look_bible: str = "",
    cast: list[dict[str, Any]] | None = None,
    image_note: str = "",
) -> str:
    parts: list[str] = []
    bible = look_bible.strip()
    if bible:
        parts.append(bible)
    lock = cast_lock_line(list(cast or []))
    if lock:
        parts.append(lock)
    if seeded:
        parts.append(SEEDED_STILL_CONTRACT)
    note = image_note.strip()
    if note:
        parts.append(note)
    parts.append(str(shot["prompt_still"]).strip())
    if str(shot.get("start_state") or "").strip():
        parts.append(f"Locked start state: {shot['start_state'].strip()}")
    if str(shot.get("end_state") or "").strip():
        parts.append(f"Locked end state: {shot['end_state'].strip()}")
    return "\n\n".join(parts)


def build_motion_prompt(
    shot: dict[str, Any],
    *,
    look_bible: str = "",
    cast: list[dict[str, Any]] | None = None,
    reference_note: str = "",
) -> str:
    parts: list[str] = []
    bible = look_bible.strip()
    if bible:
        parts.append(bible)
    lock = cast_lock_line(list(cast or []))
    if lock:
        parts.append(lock)
    note = reference_note.strip()
    if note:
        parts.append(note)
    parts.append(MOTION_SEED_LOCK)
    motion = str(shot.get("prompt_motion") or "").strip()
    if motion:
        parts.append(motion)
    dialogue = str(shot.get("dialogue") or "").strip()
    if dialogue:
        voice = str(shot.get("voice_id") or "").strip()
        if voice and shot.get("video_mode") == "reference_to_video":
            parts.append(f'Spoken line, voice <AUDIO_0> ({voice}): "{dialogue}"')
        else:
            parts.append(f'Spoken line: "{dialogue}"')
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
            "video_mode": None,
            "video_request_id": None,
            "note": None,
            "error": None,
            "revisions": [],
        }
        for shot in pack["shots"]
    ]
    _persist(store, job_id, records, stitched=False, episode_rel=None)
    image_resolution = image_resolution_for(pack["resolution"])
    bible = render_look_bible(pack.get("look_bible"))
    loaded_cast = _load_cast(store, pack)
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
            bible,
            loaded_cast,
            action="generate",
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

    _publish_episode(store, job_id, pack, records, clip_paths)
    logger.info("job %s done episode=%s", job_id, store.rel(store.episode_file(pack["id"])))


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
    look_bible: str,
    loaded_cast: list[dict[str, Any]],
    *,
    action: str = "generate",
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
        image_note, still_sources = _still_sources(seed_frame, loaded_cast)
        prompt = build_still_prompt(
            {**shot, "prompt_still": working_still},
            seeded=seed_frame is not None,
            look_bible=look_bible,
            cast=list(pack.get("cast") or []),
            image_note=image_note,
        )
        reference_note = ""
        if shot.get("video_mode") == "reference_to_video":
            reference_note = video_reference_line(loaded_cast)
        motion_prompt = build_motion_prompt(
            {**shot, "prompt_motion": working_motion},
            look_bible=look_bible,
            cast=list(pack.get("cast") or []),
            reference_note=reference_note,
        )
        try:
            if still_sources is None:
                image_bytes = client.generate_still(
                    prompt,
                    pack["aspect_ratio"],
                    image_resolution,
                )
                record["still_mode"] = "text_to_image"
            else:
                first, extras = still_sources
                image_bytes = client.edit_still(
                    prompt,
                    first,
                    pack["aspect_ratio"],
                    image_resolution,
                    extra_images=extras,
                )
                record["still_mode"] = (
                    "last_frame_edit" if seed_frame is not None else "cast_reference"
                )
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
            clip_bytes, request_id = _request_clip(
                client,
                shot,
                record,
                motion_prompt,
                still_path.read_bytes(),
                loaded_cast,
                pack,
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
        pending = shot_dir / "clip.next.mp4"
        pending.write_bytes(clip_bytes)
        if not pending.is_file() or pending.stat().st_size == 0:
            pending.unlink(missing_ok=True)
            record["error"] = "Imagine video download was empty"
            _persist(store, job_id, records, stitched=False, episode_rel=None)
            raise ImagineError("Imagine video download was empty", request_sent=True)
        _archive_current_clip(store, shot_dir, record)
        pending.replace(clip_path)
        _remember_clip(store, record, clip_path, action, motion_prompt)
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
    grade_match: bool = False,
    has_audio: bool = False,
    music_bed_applied: bool = False,
) -> None:
    store.update_job(
        job_id,
        shots=records,
        gates=aggregate_gates(
            records,
            stitched=stitched,
            grade_match=grade_match,
            has_audio=has_audio,
            music_bed_applied=music_bed_applied,
        ),
        episode_path=episode_rel,
    )


def _load_cast(store: Store, pack: dict[str, Any]) -> list[dict[str, Any]]:
    chosen = select_cast(list(pack.get("cast") or []), MAX_REFERENCE_IMAGES)
    loaded: list[dict[str, Any]] = []
    for ref in chosen:
        relative = str(ref.get("image_path") or "").strip()
        name = str(ref.get("name") or "cast")
        if not relative:
            raise FileNotFoundError(f"Cast reference {name} has no uploaded image.")
        path = store.resolve_under_data(relative)
        if not path.is_file() or path.stat().st_size == 0:
            raise FileNotFoundError(f"Cast image for {name} is not on disk.")
        loaded.append({**ref, "_bytes": path.read_bytes()})
    return loaded


def _still_sources(
    seed_frame: Path | None,
    loaded_cast: list[dict[str, Any]],
) -> tuple[str, tuple[bytes, list[bytes]] | None]:
    """Return the edit prompt note and the image bytes, or None for text-to-image."""
    if seed_frame is not None and loaded_cast:
        room = MAX_REFERENCE_IMAGES - 1
        used = loaded_cast[:room]
        extras = [item["_bytes"] for item in used]
        return image_tag_line(used, last_frame=True), (seed_frame.read_bytes(), extras)
    if seed_frame is not None:
        return "", (seed_frame.read_bytes(), [])
    if loaded_cast:
        used = loaded_cast[:MAX_REFERENCE_IMAGES]
        first, *rest = [item["_bytes"] for item in used]
        return image_tag_line(used, last_frame=False), (first, rest)
    return "", None


def _request_clip(
    client: ImagineClient,
    shot: dict[str, Any],
    record: dict[str, Any],
    motion_prompt: str,
    still: bytes,
    loaded_cast: list[dict[str, Any]],
    pack: dict[str, Any],
) -> tuple[bytes, str]:
    mode = str(shot.get("video_mode") or "image_to_video")
    record["video_mode"] = mode
    if mode == "reference_to_video":
        resolution, note = reference_video_resolution(str(pack["resolution"]))
        record["note"] = note
        return client.reference_to_video(
            prompt=motion_prompt,
            images=[item["_bytes"] for item in loaded_cast],
            duration_sec=int(shot["duration_sec"]),
            aspect_ratio=str(pack["aspect_ratio"]),
            resolution=resolution,
            voice_id=str(shot.get("voice_id") or ""),
        )
    record["note"] = None
    return client.image_to_video(
        prompt=motion_prompt,
        image=still,
        duration_sec=int(shot["duration_sec"]),
        aspect_ratio=str(pack["aspect_ratio"]),
        resolution=str(pack["resolution"]),
    )


def _archive_current_clip(store: Store, shot_dir: Path, record: dict[str, Any]) -> None:
    current = shot_dir / "clip.mp4"
    if not current.is_file() or current.stat().st_size == 0:
        return
    revisions = record.setdefault("revisions", [])
    version = len(revisions) or 1
    archive = shot_dir / f"clip.v{version}.mp4"
    if archive.resolve() != current.resolve():
        shutil.copy2(current, archive)
    relative = store.rel(archive)
    if revisions:
        revisions[-1]["clip_path"] = relative
    else:
        revisions.append(
            {
                "version": 1,
                "action": "generate",
                "clip_path": relative,
                "prompt": "",
                "created_at": utcnow(),
            }
        )


def _remember_clip(
    store: Store,
    record: dict[str, Any],
    clip_path: Path,
    action: str,
    prompt: str,
) -> None:
    revisions = record.setdefault("revisions", [])
    revisions.append(
        {
            "version": len(revisions) + 1,
            "action": action,
            "clip_path": store.rel(clip_path),
            "prompt": prompt,
            "created_at": utcnow(),
        }
    )


def _clip_paths(store: Store, records: list[dict[str, Any]]) -> list[Path] | None:
    clips: list[Path] = []
    for record in records:
        relative = record.get("clip_path")
        if not relative:
            return None
        path = store.resolve_under_data(str(relative))
        if not path.is_file() or path.stat().st_size == 0:
            return None
        clips.append(path)
    return clips


def _drop_episode(pack_id: str, store: Store) -> None:
    folder = store.runs_root / pack_id
    for name in ("episode.mp4", "episode.base.mp4", "episode.music.mp4"):
        path = folder / name
        if path.exists():
            path.unlink()


def _apply_music(
    store: Store,
    pack: dict[str, Any],
    episode_path: Path,
    note: str,
) -> tuple[bool, bool, str]:
    music_rel = str(pack.get("music_path") or "").strip()
    if not music_rel:
        return ffmpeg_util.has_audio_stream(episode_path), False, note
    try:
        music = store.resolve_under_data(music_rel)
        mix_note = ffmpeg_util.mix_music_bed(episode_path, music)
    except (FfmpegError, FfmpegNotFound, ValueError, OSError) as exc:
        audible = ffmpeg_util.has_audio_stream(episode_path)
        return audible, False, f"{note} Music bed was not applied: {exc}"
    audible = ffmpeg_util.has_audio_stream(episode_path)
    if not audible:
        return False, False, f"{note} Music bed was not applied: ffprobe found no audio stream."
    return True, True, f"{note} {mix_note}"


def _publish_episode(
    store: Store,
    job_id: str,
    pack: dict[str, Any],
    records: list[dict[str, Any]],
    clip_paths: list[Path],
) -> None:
    episode_path = store.episode_file(pack["id"])
    base_path = episode_path.with_name("episode.base.mp4")
    try:
        graded = ffmpeg_util.match_grade(clip_paths)
    except Exception as exc:
        logger.warning("grade match skipped after an unexpected error: %s", exc)
        graded = ffmpeg_util.GradeMatch(
            list(clip_paths),
            False,
            f"Grade match skipped: {exc}. Clips were concatenated unchanged.",
        )
    _persist(
        store,
        job_id,
        records,
        stitched=False,
        episode_rel=None,
        grade_match=graded.ran,
    )
    try:
        ffmpeg_util.stitch_clips(graded.clips, base_path)
        shutil.copy2(base_path, episode_path)
    except (FfmpegError, FfmpegNotFound):
        _drop_episode(pack["id"], store)
        store.update_job(job_id, message=graded.note)
        raise
    has_audio, music_applied, note = _apply_music(store, pack, episode_path, graded.note)
    episode_rel = store.rel(episode_path)
    _persist(
        store,
        job_id,
        records,
        stitched=True,
        episode_rel=episode_rel,
        grade_match=graded.ran,
        has_audio=has_audio,
        music_bed_applied=music_applied,
    )
    store.update_job(
        job_id,
        status="done",
        error=None,
        message=f"Episode stitched with ffmpeg. {note}",
        shots=records,
        gates=aggregate_gates(
            records,
            stitched=True,
            grade_match=graded.ran,
            has_audio=has_audio,
            music_bed_applied=music_applied,
        ),
        episode_path=episode_rel,
    )


def apply_music_to_job(
    store: Store, pack: dict[str, Any], job: dict[str, Any]
) -> tuple[bool, bool]:
    """Remix the stitched episode from ``episode.base.mp4`` when that file exists.

    Does not call Imagine. Returns ``(has_audio, music_bed_applied)``.
    """
    episode_path = store.episode_file(pack["id"])
    base_path = episode_path.with_name("episode.base.mp4")
    source = base_path if base_path.is_file() else episode_path
    if not source.is_file():
        return False, False
    if source != episode_path:
        shutil.copy2(source, episode_path)
    has_audio, music_applied, note = _apply_music(
        store,
        pack,
        episode_path,
        str(job.get("message") or "Episode stitched with ffmpeg."),
    )
    gates = dict(job["gates"])
    gates["has_audio"] = has_audio
    gates["music_bed_applied"] = music_applied
    store.update_job(
        job["id"],
        message=note,
        gates=gates,
        episode_path=store.rel(episode_path),
        shots=job["shots"],
    )
    return has_audio, music_applied


def check_edit_duration(clip_path: Path) -> None:
    """Refuse an edit the docs would reject. Does not call xAI."""
    duration = ffmpeg_util.media_duration(clip_path)
    if duration is None:
        raise ReviseRejected(
            "Could not read the clip duration, so the edit was not sent. "
            "Video edits keep the input length, and the docs cap that input at 8.7 seconds."
        )
    if duration > EDIT_MAX_INPUT_SEC:
        raise ReviseRejected(
            f"This clip is {duration:.1f}s. Video edits keep the input duration, "
            "which the docs cap at 8.7 seconds, and the output is capped at 720p. "
            "The edit was not sent."
        )


def revise_shot(
    store: Store,
    job_id: str,
    shot_id: str,
    action: str,
    client_factory: Callable[[], ImagineClient],
    *,
    previous_status: str,
    prompt_still: str = "",
    prompt_motion: str = "",
    edit_prompt: str = "",
    extend_sec: int = 6,
) -> None:
    """Revise one shot on a job that is already claimed as running."""
    job = store.get_job(job_id)
    if job is None:
        return
    pack = store.get_pack(job["pack_id"])
    if pack is None:
        store.update_job(job_id, status="error", error="Pack not found")
        return
    records = [dict(item) for item in job["shots"]]
    match = next((record for record in records if record["id"] == shot_id), None)
    shot = next((item for item in pack["shots"] if item["id"] == shot_id), None)
    if match is None or shot is None:
        store.update_job(job_id, status=previous_status, message="Shot was not on this job.")
        return
    record = match
    replaced = False
    saved_gates = dict(job["gates"])
    saved_episode = job.get("episode_path")
    client: ImagineClient | None = None
    try:
        client = client_factory()
    except Exception as exc:
        record["error"] = f"Imagine client was not created: {exc}"
        _abort_revise(
            store, job_id, records, previous_status, str(exc), saved_gates, saved_episode
        )
        return
    try:
        if action == "regenerate":
            if prompt_still:
                shot["prompt_still"] = prompt_still
            if prompt_motion:
                shot["prompt_motion"] = prompt_motion
            store.update_pack_body(pack["id"], _stored_pack(pack))
            index = records.index(record)
            seed = _seed_frame(store, pack, records, index, job.get("continuity_mode"))
            loaded = _load_cast(store, pack)
            _render_shot(
                store,
                job_id,
                pack,
                client,
                records,
                record,
                shot,
                store.shot_dir(pack["id"], shot_id),
                seed,
                image_resolution_for(pack["resolution"]),
                render_look_bible(pack.get("look_bible")),
                loaded,
                action="regenerate",
            )
            replaced = True
            clip_path = store.shot_dir(pack["id"], shot_id) / "clip.mp4"
            _refresh_last_frame(store, pack, record, clip_path)
        elif action in {"edit", "extend"}:
            _revise_existing_clip(
                store,
                job_id,
                pack,
                records,
                record,
                client,
                action,
                edit_prompt,
                extend_sec,
            )
            replaced = True
        else:
            raise ReviseRejected(f"Unknown revise action {action}.")
        clips = _clip_paths(store, records)
        if clips is None:
            _drop_episode(pack["id"], store)
            store.update_job(
                job_id,
                status="error",
                error=None,
                message=(
                    f"Shot {shot_id} was revised, but the episode was not stitched "
                    "because another shot has no clip."
                ),
                shots=records,
                gates=aggregate_gates(records, stitched=False),
                episode_path=None,
            )
            return
        _publish_episode(store, job_id, pack, records, clips)
    except Exception as exc:
        record["error"] = str(exc)
        if isinstance(exc, ImagineError):
            if action == "regenerate":
                record["called_imagine_still"] = exc.request_sent or record["called_imagine_still"]
            record["called_imagine_video"] = exc.request_sent or record["called_imagine_video"]
            record["video_request_id"] = exc.request_id or record.get("video_request_id")
        if not replaced:
            _abort_revise(
                store,
                job_id,
                records,
                previous_status,
                str(exc),
                saved_gates,
                saved_episode,
            )
        else:
            _drop_episode(pack["id"], store)
            store.update_job(
                job_id,
                status="error",
                error=str(exc),
                message=str(exc),
                shots=records,
                gates=aggregate_gates(records, stitched=False),
                episode_path=None,
            )
        logger.info("job %s revise %s failed: %s", job_id, action, exc)
    finally:
        if client is not None:
            client.close()


def _revise_existing_clip(
    store: Store,
    job_id: str,
    pack: dict[str, Any],
    records: list[dict[str, Any]],
    record: dict[str, Any],
    client: ImagineClient,
    action: str,
    prompt: str,
    extend_sec: int,
) -> None:
    shot_dir = store.shot_dir(pack["id"], str(record["id"]))
    clip_path = shot_dir / "clip.mp4"
    if not clip_path.is_file() or clip_path.stat().st_size == 0:
        raise ReviseRejected(f"Shot {record['id']} has no clip to {action}.")
    if action == "edit":
        check_edit_duration(clip_path)
        clip_bytes, request_id = client.edit_video(prompt=prompt, video=clip_path.read_bytes())
    else:
        clip_bytes, request_id = client.extend_video(
            prompt=prompt,
            video=clip_path.read_bytes(),
            duration_sec=extend_sec,
        )
    record["called_imagine_video"] = True
    record["video_request_id"] = request_id
    pending = shot_dir / "clip.next.mp4"
    pending.write_bytes(clip_bytes)
    if pending.stat().st_size == 0:
        pending.unlink(missing_ok=True)
        raise ImagineError(
            "Imagine video download was empty",
            request_sent=True,
            request_id=request_id,
        )
    _archive_current_clip(store, shot_dir, record)
    pending.replace(clip_path)
    _remember_clip(store, record, clip_path, action, prompt)
    record["produced_mp4"] = True
    record["clip_path"] = store.rel(clip_path)
    record["error"] = None
    _refresh_last_frame(store, pack, record, clip_path)
    _persist(store, job_id, records, stitched=False, episode_rel=None)


def _seed_frame(
    store: Store,
    pack: dict[str, Any],
    records: list[dict[str, Any]],
    index: int,
    continuity: str | None,
) -> Path | None:
    if index <= 0 or continuity != "last_frame_edit" or not ffmpeg_util.ffmpeg_path():
        return None
    previous = records[index - 1]
    relative = previous.get("last_frame_path")
    if relative:
        path = store.resolve_under_data(str(relative))
        if path.is_file() and path.stat().st_size > 0:
            return path
    clip_relative = previous.get("clip_path")
    if not clip_relative:
        return None
    clip = store.resolve_under_data(str(clip_relative))
    if not clip.is_file():
        return None
    dest = store.shot_dir(pack["id"], str(previous["id"])) / "last_frame.png"
    ffmpeg_util.extract_last_frame(clip, dest)
    previous["last_frame_path"] = store.rel(dest)
    return dest


def _refresh_last_frame(
    store: Store,
    pack: dict[str, Any],
    record: dict[str, Any],
    clip_path: Path,
) -> None:
    if not ffmpeg_util.ffmpeg_path() or not clip_path.is_file():
        return
    dest = store.shot_dir(pack["id"], str(record["id"])) / "last_frame.png"
    try:
        ffmpeg_util.extract_last_frame(clip_path, dest)
    except (FfmpegError, FfmpegNotFound):
        return
    record["last_frame_path"] = store.rel(dest)


def _stored_pack(pack: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in pack.items() if key not in {"id", "created_at"}}


def _abort_revise(
    store: Store,
    job_id: str,
    records: list[dict[str, Any]],
    previous_status: str,
    message: str,
    saved_gates: dict[str, Any],
    saved_episode: str | None,
) -> None:
    episode_file: Path | None = None
    if saved_episode:
        try:
            episode_file = store.resolve_under_data(saved_episode)
        except ValueError:
            episode_file = None
    stitched = (
        previous_status == "done"
        and episode_file is not None
        and episode_file.is_file()
        and episode_file.stat().st_size > 0
    )
    store.update_job(
        job_id,
        status=previous_status,
        error=None if previous_status == "done" else message,
        message=message,
        shots=records,
        gates=aggregate_gates(
            records,
            stitched=stitched,
            grade_match=bool(saved_gates.get("grade_match")) if stitched else False,
            has_audio=bool(saved_gates.get("has_audio")) if stitched else False,
            music_bed_applied=bool(saved_gates.get("music_bed_applied")) if stitched else False,
        ),
        episode_path=saved_episode if stitched else None,
    )
