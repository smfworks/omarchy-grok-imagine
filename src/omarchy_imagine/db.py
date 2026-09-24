"""SQLite packs and jobs. Artifact paths are relative to the data directory."""

from __future__ import annotations

import json
import shutil
import sqlite3
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from omarchy_imagine.schema import LookBible, PackIn


class JobInProgress(Exception):
    def __init__(self, job_id: str) -> None:
        self.job_id = job_id
        super().__init__(f"Pack already has an active job ({job_id})")


class ReviseRejected(Exception):
    """The job cannot be revised. No Imagine call should follow."""


def utcnow() -> str:
    # Microseconds keep same-second jobs in order. rowid is the tie-break.
    return datetime.now(UTC).isoformat()


def blank_shot_record(shot_id: str) -> dict[str, Any]:
    return {
        "id": shot_id,
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
        "moderation": None,
        "video_mode": None,
        "note": None,
        "revisions": [],
    }


def aggregate_gates(
    shots: list[dict[str, Any]],
    *,
    stitched: bool,
    grade_match: bool = False,
    has_audio: bool = False,
    music_bed_applied: bool = False,
) -> dict[str, bool]:
    """Job-level gates flip true once that work has happened for any shot.

    ``stitched_episode`` is the pack-level ffmpeg concat, not a per-shot flag.
    Per-shot records remain the detail. A stub job keeps every gate false.
    """

    def any_flag(name: str) -> bool:
        return any(bool(shot.get(name)) for shot in shots)

    return {
        "called_imagine_still": any_flag("called_imagine_still"),
        "produced_still": any_flag("produced_still"),
        "called_imagine_video": any_flag("called_imagine_video"),
        "produced_mp4": any_flag("produced_mp4"),
        "stitched_episode": bool(stitched),
        "grade_match": bool(grade_match),
        "has_audio": bool(has_audio),
        "music_bed_applied": bool(music_bed_applied),
    }


class Store:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir.resolve()
        self.db_path = self.data_dir / "omarchy.sqlite"
        self.runs_root = self.data_dir / "runs"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.runs_root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()
        self._interrupt_abandoned_jobs()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_db(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS packs (
                    id TEXT PRIMARY KEY,
                    body_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    pack_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    message TEXT,
                    error TEXT,
                    continuity_mode TEXT,
                    gates_json TEXT NOT NULL,
                    shots_json TEXT NOT NULL,
                    episode_path TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (pack_id) REFERENCES packs(id)
                )
                """
            )

    def _interrupt_abandoned_jobs(self) -> None:
        """A previous process that died mid-run must not block the next one."""
        note = (
            "Job interrupted before completion. Gates reflect only work saved "
            "before the restart."
        )
        now = utcnow()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE jobs
                SET status = 'error',
                    error = COALESCE(error, ?),
                    updated_at = ?
                WHERE status IN ('queued', 'running')
                """,
                (note, now),
            )

    def create_pack(self, pack: PackIn) -> dict[str, Any]:
        pack_id = str(uuid.uuid4())
        created_at = utcnow()
        body = pack.model_dump()
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO packs (id, body_json, created_at) VALUES (?, ?, ?)",
                (pack_id, json.dumps(body), created_at),
            )
        return self._public_pack(pack_id, body, created_at)

    def list_packs(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM packs ORDER BY created_at DESC, rowid DESC"
            ).fetchall()
        return [self._pack_from_row(row) for row in rows]

    def get_pack(self, pack_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM packs WHERE id = ?", (pack_id,)).fetchone()
        if row is None:
            return None
        return self._pack_from_row(row)

    def create_job(self, pack_id: str) -> dict[str, Any]:
        with self._lock, self._connect() as conn:
            pack = conn.execute("SELECT * FROM packs WHERE id = ?", (pack_id,)).fetchone()
            if pack is None:
                raise KeyError(pack_id)
            active = conn.execute(
                """
                SELECT id FROM jobs
                WHERE pack_id = ? AND status IN ('queued', 'running')
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (pack_id,),
            ).fetchone()
            if active is not None:
                raise JobInProgress(str(active["id"]))
            body = json.loads(pack["body_json"])
            shots = [blank_shot_record(shot["id"]) for shot in body["shots"]]
            gates = aggregate_gates(shots, stitched=False)
            job_id = str(uuid.uuid4())
            now = utcnow()
            conn.execute(
                """
                INSERT INTO jobs (
                    id, pack_id, status, message, error, continuity_mode,
                    gates_json, shots_json, episode_path, created_at, updated_at
                ) VALUES (?, ?, 'queued', NULL, NULL, NULL, ?, ?, NULL, ?, ?)
                """,
                (job_id, pack_id, json.dumps(gates), json.dumps(shots), now, now),
            )
        job = self.get_job(job_id)
        if job is None:
            raise RuntimeError("job insert did not persist")
        return job

    def update_pack_body(self, pack_id: str, body: dict[str, Any]) -> None:
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE packs SET body_json = ? WHERE id = ?",
                (json.dumps(body), pack_id),
            )
            if cur.rowcount != 1:
                raise KeyError(pack_id)

    def claim_for_revise(self, job_id: str) -> dict[str, Any]:
        """Mark a finished or failed job running so a second revise cannot start."""
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(job_id)
            status = str(row["status"])
            if status in {"queued", "running"}:
                raise JobInProgress(job_id)
            if status == "stub":
                raise ReviseRejected("Stub job has no Imagine media to revise.")
            if status not in {"done", "error"}:
                raise ReviseRejected(f"Job status {status} cannot be revised.")
            conn.execute(
                """
                UPDATE jobs
                SET status = 'running', message = 'Revising one shot.', updated_at = ?
                WHERE id = ?
                """,
                (utcnow(), job_id),
            )
        job = self.get_job(job_id)
        if job is None:
            raise KeyError(job_id)
        return job

    def update_job(self, job_id: str, **changes: Any) -> dict[str, Any]:
        allowed = {
            "status",
            "message",
            "error",
            "continuity_mode",
            "gates",
            "shots",
            "episode_path",
        }
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"unknown job fields: {sorted(unknown)}")
        assignments: list[str] = []
        values: list[Any] = []
        column_for = {"gates": "gates_json", "shots": "shots_json"}
        for key, value in changes.items():
            column = column_for.get(key, key)
            if key in {"gates", "shots"}:
                value = json.dumps(value)
            assignments.append(f"{column} = ?")
            values.append(value)
        assignments.append("updated_at = ?")
        values.append(utcnow())
        values.append(job_id)
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                f"UPDATE jobs SET {', '.join(assignments)} WHERE id = ?",
                values,
            )
            if cur.rowcount != 1:
                raise KeyError(job_id)
        job = self.get_job(job_id)
        if job is None:
            raise KeyError(job_id)
        return job

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            return None
        return self._job_from_row(row)

    def list_jobs(self, pack_id: str) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE pack_id = ? ORDER BY created_at DESC, rowid DESC",
                (pack_id,),
            ).fetchall()
        return [self._job_from_row(row) for row in rows]

    def latest_job(self, pack_id: str) -> dict[str, Any] | None:
        jobs = self.list_jobs(pack_id)
        return jobs[0] if jobs else None

    def shot_dir(self, pack_id: str, shot_id: str) -> Path:
        return self.runs_root / pack_id / "shots" / shot_id

    def episode_file(self, pack_id: str) -> Path:
        return self.runs_root / pack_id / "episode.mp4"

    def clear_pack_runs(self, pack_id: str) -> None:
        target = (self.runs_root / pack_id).resolve()
        if not target.is_relative_to(self.runs_root.resolve()):
            raise ValueError("refusing to clear a path outside runs")
        if target.exists():
            shutil.rmtree(target)

    def rel(self, path: Path) -> str:
        return path.resolve().relative_to(self.data_dir).as_posix()

    def resolve_under_data(self, relative: str) -> Path:
        full = (self.data_dir / relative).resolve()
        if not full.is_relative_to(self.data_dir):
            raise ValueError("path escapes the data directory")
        return full

    def to_public_job(self, job: dict[str, Any]) -> dict[str, Any]:
        shots: list[dict[str, Any]] = []
        for shot in job["shots"]:
            public = dict(shot)
            for key in ("still_path", "clip_path", "last_frame_path"):
                public[key] = self._if_file(shot.get(key))
            revisions = []
            for item in shot.get("revisions") or []:
                revision = dict(item)
                revision["clip_path"] = self._if_file(revision.get("clip_path"))
                revisions.append(revision)
            public["revisions"] = revisions
            shots.append(public)
        gates = job["gates"]
        return {
            "id": job["id"],
            "pack_id": job["pack_id"],
            "status": job["status"],
            "message": job["message"],
            "error": job["error"],
            "continuity_mode": job["continuity_mode"],
            "grade_match": bool(gates.get("grade_match", False)),
            "has_audio": bool(gates.get("has_audio", False)),
            "music_bed_applied": bool(gates.get("music_bed_applied", False)),
            "called_imagine_still": bool(gates["called_imagine_still"]),
            "produced_still": bool(gates["produced_still"]),
            "called_imagine_video": bool(gates["called_imagine_video"]),
            "produced_mp4": bool(gates["produced_mp4"]),
            "stitched_episode": bool(gates["stitched_episode"]),
            "episode_path": self._if_file(job.get("episode_path")),
            "shots": shots,
            "created_at": job["created_at"],
            "updated_at": job["updated_at"],
        }

    def _if_file(self, relative: str | None) -> str | None:
        if not relative:
            return None
        try:
            full = self.resolve_under_data(relative)
        except ValueError:
            return None
        if not full.is_file() or full.stat().st_size == 0:
            return None
        return relative

    def _pack_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        body = json.loads(row["body_json"])
        return self._public_pack(row["id"], body, row["created_at"])

    @staticmethod
    def _public_pack(pack_id: str, body: dict[str, Any], created_at: str) -> dict[str, Any]:
        bible = body.get("look_bible") or LookBible().model_dump()
        return {
            "id": pack_id,
            "title": body["title"],
            "logline": body.get("logline", ""),
            "aspect_ratio": body["aspect_ratio"],
            "resolution": body["resolution"],
            "look_bible": bible,
            "style_preset": body.get("style_preset") or "",
            "beat_map": body.get("beat_map") or [],
            "cast": body.get("cast") or [],
            "music_path": body.get("music_path") or "",
            "shots": body["shots"],
            "created_at": created_at,
        }

    @staticmethod
    def _job_from_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "pack_id": row["pack_id"],
            "status": row["status"],
            "message": row["message"],
            "error": row["error"],
            "continuity_mode": row["continuity_mode"],
            "gates": json.loads(row["gates_json"]),
            "shots": json.loads(row["shots_json"]),
            "episode_path": row["episode_path"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
