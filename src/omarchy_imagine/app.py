"""Local FastAPI app. Bearer token is the fixed local-dev-token from the Phase 1 spec."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from pydantic import ValidationError

from omarchy_imagine import __version__
from omarchy_imagine.config import (
    IMAGE_MODEL,
    LOCAL_DEV_TOKEN,
    MAX_UPLOAD_AUDIO_BYTES,
    MAX_UPLOAD_IMAGE_BYTES,
    VIDEO_MODEL,
)
from omarchy_imagine.db import JobInProgress, ReviseRejected, Store
from omarchy_imagine.ffmpeg_util import ffmpeg_path
from omarchy_imagine.fill import FillError, FillIn, fill_pack
from omarchy_imagine.imagine import ImagineClient
from omarchy_imagine.pipeline import (
    apply_music_to_job,
    check_edit_duration,
    revise_shot,
    run_pack_job,
)
from omarchy_imagine.plan import (
    PlanError,
    PlanIn,
    PlanUpstreamError,
    TextPlanner,
    XAITextPlanner,
    plan_pack,
)
from omarchy_imagine.schema import (
    CastRef,
    EditClipIn,
    ExtendClipIn,
    JobsOut,
    MusicOut,
    PackIn,
    PackOut,
    RegenerateIn,
    RunOut,
)
from omarchy_imagine.uploads import (
    UploadError,
    content_type_for,
    find_reference,
    store_music,
    store_reference,
)


def default_imagine_client_factory() -> ImagineClient:
    api_key = os.environ.get("XAI_API_KEY", "").strip()
    return ImagineClient(api_key)


def default_text_planner_factory() -> TextPlanner | None:
    """Text model for director briefs. None when no key is set, so planning stays local."""
    api_key = os.environ.get("XAI_API_KEY", "").strip()
    if not api_key:
        return None
    return XAITextPlanner(api_key)


def _bearer(authorization: str | None = Header(default=None)) -> None:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token = authorization[7:].strip()
    if token != LOCAL_DEV_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid bearer token")


def create_app() -> FastAPI:
    data_dir = Path(os.environ.get("OMARCHY_DATA_DIR", "data")).resolve()
    store = Store(data_dir)
    app = FastAPI(
        title="Omarchy Grok Imagine",
        version=__version__,
        description=(
            "Local pack API for Grok Imagine stills, image-to-video clips, and ffmpeg stitch."
        ),
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"^http://(localhost|127\.0\.0\.1)(:\d+)?$",
        allow_methods=["*"],
        allow_headers=["Authorization", "Content-Type"],
    )
    app.state.store = store
    app.state.imagine_client_factory = default_imagine_client_factory
    app.state.text_planner_factory = default_text_planner_factory

    @app.get("/")
    def root() -> dict[str, str]:
        return {
            "service": "omarchy-grok-imagine",
            "health": "/api/health",
            "docs": "/docs",
        }

    @app.get("/api/health")
    def health() -> dict[str, object]:
        ffmpeg_ready = ffmpeg_path() is not None
        return {
            "ok": True,
            "imagine_configured": bool(os.environ.get("XAI_API_KEY", "").strip()),
            "ffmpeg": ffmpeg_ready,
            "image_model": IMAGE_MODEL,
            "video_model": VIDEO_MODEL,
            "continuity": "last_frame_edit" if ffmpeg_ready else "prose_regenerate",
        }

    @app.post("/api/packs/fill", response_model=PackIn)
    def fill_pack_route(body: FillIn, _: None = Depends(_bearer)) -> PackIn:
        """Fill blank prompts and continuity states from the title and logline.

        Does not persist a pack, call Imagine, or invent media URLs.
        Non-empty user text is kept. Aspect, resolution, and duration stay
        as sent when they are already set.
        """
        try:
            return fill_pack(body)
        except FillError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/packs/plan", response_model=PackIn)
    def plan_pack_route(body: PlanIn, _: None = Depends(_bearer)) -> PackIn:
        """Expand one story prompt and a target length into a pack draft.

        Does not persist a pack, call Imagine, or invent media URLs.
        Shot count and durations come from the target length. With
        ``XAI_API_KEY`` set, prose comes from the text model. Without a key,
        the fill heuristic writes the same chain.
        """
        planner = app.state.text_planner_factory()
        try:
            return plan_pack(body, planner=planner)
        except PlanUpstreamError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except PlanError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        finally:
            close = getattr(planner, "close", None)
            if close is not None:
                close()

    @app.post("/api/references", response_model=CastRef, status_code=201)
    def upload_reference(
        file: UploadFile = File(...),
        name: str = Form(...),
        role: str = Form(...),
        markers: str = Form(""),
        _: None = Depends(_bearer),
    ) -> CastRef:
        """Store one cast image under data/references. No remote URL is created."""
        raw = file.file.read(MAX_UPLOAD_IMAGE_BYTES + 1)
        try:
            ref_id, relative = store_reference(store.data_dir, raw)
            return CastRef(
                id=ref_id,
                name=name,
                role=role,
                markers=markers,
                image_path=relative,
            )
        except UploadError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc.errors()[0]["msg"])) from exc

    @app.get("/api/references/{ref_id}")
    def download_reference(ref_id: str, _: None = Depends(_bearer)) -> FileResponse:
        path = find_reference(store.data_dir, ref_id)
        if path is None:
            raise HTTPException(status_code=404, detail="Reference image is not on disk.")
        return FileResponse(path, media_type=content_type_for(path), filename=path.name)

    @app.delete("/api/references/{ref_id}", status_code=204)
    def delete_reference(ref_id: str, _: None = Depends(_bearer)) -> Response:
        path = find_reference(store.data_dir, ref_id)
        if path is None:
            raise HTTPException(status_code=404, detail="Reference image is not on disk.")
        path.unlink()
        return Response(status_code=204)

    @app.post("/api/music", response_model=MusicOut, status_code=201)
    def upload_music(
        file: UploadFile = File(...),
        _: None = Depends(_bearer),
    ) -> dict[str, object]:
        """Store a local music file. The server does not generate or download music."""
        raw = file.file.read(MAX_UPLOAD_AUDIO_BYTES + 1)
        try:
            relative = store_music(store.data_dir, raw)
        except UploadError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        return {"music_path": relative, "music_bed_applied": False, "has_audio": False}

    @app.post("/api/packs/{pack_id}/music", response_model=MusicOut)
    def attach_music(
        pack_id: str,
        file: UploadFile = File(...),
        _: None = Depends(_bearer),
    ) -> dict[str, object]:
        """Save a music bed on the pack and remix a stitched episode when one exists."""
        pack = store.get_pack(pack_id)
        if pack is None:
            raise HTTPException(status_code=404, detail="Pack not found")
        raw = file.file.read(MAX_UPLOAD_AUDIO_BYTES + 1)
        try:
            relative = store_music(store.data_dir, raw)
        except UploadError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        pack["music_path"] = relative
        store.update_pack_body(pack_id, _stored_body(pack))
        job = store.latest_job(pack_id)
        applied = False
        audible = False
        if job is not None and job["gates"].get("stitched_episode") and job["status"] == "done":
            audible, applied = apply_music_to_job(store, pack, job)
        return {
            "music_path": relative,
            "music_bed_applied": applied,
            "has_audio": audible,
        }

    @app.post("/api/packs", response_model=PackOut, status_code=201)
    def create_pack(pack: PackIn, _: None = Depends(_bearer)) -> dict:
        _require_local_files(store, pack)
        return store.create_pack(pack)

    @app.get("/api/packs", response_model=list[PackOut])
    def list_packs(_: None = Depends(_bearer)) -> list[dict]:
        return store.list_packs()

    @app.get("/api/packs/{pack_id}", response_model=PackOut)
    def get_pack(pack_id: str, _: None = Depends(_bearer)) -> dict:
        pack = store.get_pack(pack_id)
        if pack is None:
            raise HTTPException(status_code=404, detail="Pack not found")
        return pack

    @app.post("/api/packs/{pack_id}/run", response_model=RunOut)
    def run_pack(
        pack_id: str,
        background: BackgroundTasks,
        _: None = Depends(_bearer),
    ) -> dict[str, str]:
        if store.get_pack(pack_id) is None:
            raise HTTPException(status_code=404, detail="Pack not found")
        try:
            job = store.create_job(pack_id)
        except JobInProgress as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        factory = app.state.imagine_client_factory
        sync = os.environ.get("OMARCHY_SYNC_JOBS", "").strip().lower() in {"1", "true", "yes"}
        if sync:
            run_pack_job(store, job["id"], factory)
            finished = store.get_job(job["id"])
            status = finished["status"] if finished else "queued"
            return {"job_id": job["id"], "pack_id": pack_id, "status": status}
        background.add_task(run_pack_job, store, job["id"], factory)
        return {"job_id": job["id"], "pack_id": pack_id, "status": "queued"}

    @app.get("/api/packs/{pack_id}/jobs", response_model=JobsOut)
    def list_jobs(pack_id: str, _: None = Depends(_bearer)) -> dict:
        if store.get_pack(pack_id) is None:
            raise HTTPException(status_code=404, detail="Pack not found")
        return {"jobs": [store.to_public_job(job) for job in store.list_jobs(pack_id)]}

    @app.get("/api/packs/{pack_id}/episode")
    def download_episode(pack_id: str, _: None = Depends(_bearer)) -> FileResponse:
        if store.get_pack(pack_id) is None:
            raise HTTPException(status_code=404, detail="Pack not found")
        job = store.latest_job(pack_id)
        if job is None or not job["gates"].get("stitched_episode"):
            raise HTTPException(
                status_code=404,
                detail="Episode is not stitched. No media URL is available.",
            )
        relative = job.get("episode_path")
        if not relative:
            raise HTTPException(
                status_code=404,
                detail="Episode is not stitched. No media URL is available.",
            )
        path = store.resolve_under_data(relative)
        if not path.is_file() or path.stat().st_size == 0:
            raise HTTPException(status_code=404, detail="Episode file is not on disk.")
        return FileResponse(path, media_type="video/mp4", filename="episode.mp4")

    @app.post("/api/packs/{pack_id}/jobs/{job_id}/shots/{shot_id}/regenerate")
    def regenerate_shot(
        pack_id: str,
        job_id: str,
        shot_id: str,
        body: RegenerateIn,
        background: BackgroundTasks,
        _: None = Depends(_bearer),
    ) -> dict:
        return _queue_revise(
            store,
            background,
            pack_id,
            job_id,
            shot_id,
            "regenerate",
            app.state.imagine_client_factory,
            prompt_still=body.prompt_still,
            prompt_motion=body.prompt_motion,
        )

    @app.post("/api/packs/{pack_id}/jobs/{job_id}/shots/{shot_id}/edit")
    def edit_shot(
        pack_id: str,
        job_id: str,
        shot_id: str,
        body: EditClipIn,
        background: BackgroundTasks,
        _: None = Depends(_bearer),
    ) -> dict:
        return _queue_revise(
            store,
            background,
            pack_id,
            job_id,
            shot_id,
            "edit",
            app.state.imagine_client_factory,
            edit_prompt=body.prompt,
        )

    @app.post("/api/packs/{pack_id}/jobs/{job_id}/shots/{shot_id}/extend")
    def extend_shot(
        pack_id: str,
        job_id: str,
        shot_id: str,
        body: ExtendClipIn,
        background: BackgroundTasks,
        _: None = Depends(_bearer),
    ) -> dict:
        return _queue_revise(
            store,
            background,
            pack_id,
            job_id,
            shot_id,
            "extend",
            app.state.imagine_client_factory,
            edit_prompt=body.prompt,
            extend_sec=body.duration_sec,
        )

    return app


def _stored_body(pack: dict) -> dict:
    return {key: value for key, value in pack.items() if key not in {"id", "created_at"}}


def _require_local_files(store: Store, pack: PackIn) -> None:
    for ref in pack.cast:
        if not ref.image_path.startswith("references/"):
            raise HTTPException(
                status_code=422,
                detail="Cast images must be uploaded under references/.",
            )
        try:
            path = store.resolve_under_data(ref.image_path)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if not path.is_file() or path.stat().st_size == 0:
            raise HTTPException(
                status_code=422,
                detail=f"Cast image for {ref.name} is not on disk.",
            )
    if not pack.music_path:
        return
    if not pack.music_path.startswith("music/"):
        raise HTTPException(status_code=422, detail="Music must be uploaded under music/.")
    try:
        music = store.resolve_under_data(pack.music_path)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not music.is_file() or music.stat().st_size == 0:
        raise HTTPException(status_code=422, detail="Music file is not on disk.")


def _queue_revise(
    store: Store,
    background: BackgroundTasks,
    pack_id: str,
    job_id: str,
    shot_id: str,
    action: str,
    client_factory: Callable[[], ImagineClient],
    *,
    prompt_still: str = "",
    prompt_motion: str = "",
    edit_prompt: str = "",
    extend_sec: int = 6,
) -> dict:
    if store.get_pack(pack_id) is None:
        raise HTTPException(status_code=404, detail="Pack not found")
    if not os.environ.get("XAI_API_KEY", "").strip():
        raise HTTPException(
            status_code=422,
            detail="XAI_API_KEY is unset. Nothing was sent.",
        )
    job = store.get_job(job_id)
    if job is None or job["pack_id"] != pack_id:
        raise HTTPException(status_code=404, detail="Job not found")
    if not any(shot["id"] == shot_id for shot in job["shots"]):
        raise HTTPException(status_code=404, detail="Shot not found")
    if action in {"edit", "extend"}:
        clip = store.shot_dir(pack_id, shot_id) / "clip.mp4"
        if not clip.is_file() or clip.stat().st_size == 0:
            raise HTTPException(
                status_code=422,
                detail=f"Shot {shot_id} has no clip to {action}.",
            )
        if action == "edit":
            try:
                check_edit_duration(clip)
            except ReviseRejected as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
    previous = str(job["status"])
    try:
        store.claim_for_revise(job_id)
    except JobInProgress as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ReviseRejected as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Job not found") from exc

    def run() -> None:
        revise_shot(
            store,
            job_id,
            shot_id,
            action,
            client_factory,
            previous_status=previous,
            prompt_still=prompt_still,
            prompt_motion=prompt_motion,
            edit_prompt=edit_prompt,
            extend_sec=extend_sec,
        )

    sync = os.environ.get("OMARCHY_SYNC_JOBS", "").strip().lower() in {"1", "true", "yes"}
    if sync:
        run()
    else:
        background.add_task(run)
    finished = store.get_job(job_id)
    if finished is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return store.to_public_job(finished)


app = create_app()
