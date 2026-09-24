"""Local FastAPI app. Bearer token is the fixed local-dev-token from the Phase 1 spec."""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from omarchy_imagine import __version__
from omarchy_imagine.config import IMAGE_MODEL, LOCAL_DEV_TOKEN, VIDEO_MODEL
from omarchy_imagine.db import JobInProgress, Store
from omarchy_imagine.ffmpeg_util import ffmpeg_path
from omarchy_imagine.fill import FillError, FillIn, fill_pack
from omarchy_imagine.imagine import ImagineClient
from omarchy_imagine.pipeline import run_pack_job
from omarchy_imagine.schema import JobsOut, PackIn, PackOut, RunOut


def default_imagine_client_factory() -> ImagineClient:
    api_key = os.environ.get("XAI_API_KEY", "").strip()
    return ImagineClient(api_key)


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

    @app.post("/api/packs", response_model=PackOut, status_code=201)
    def create_pack(pack: PackIn, _: None = Depends(_bearer)) -> dict:
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

    return app


app = create_app()
