"""Maintenance API — backup / restore from the admin GUI.

Backups are produced via the Docker socket (mounted into this container): a
PostgreSQL dump plus tarred MinIO/Qdrant/Redis volumes, packed into a single
archive under /app/backups (a named volume). Mirrors infra/installer/backup.sh
so CLI and GUI produce interchangeable archives.

Backup and restore are long, blocking Docker operations. They MUST NOT run
inline in the request handler: the backend serves a single uvicorn worker, so a
synchronous multi-minute Docker job would freeze the whole event loop (auth,
listings, health — everything hangs). Instead the endpoints spawn a background
worker thread and return a job id immediately; the GUI polls job status. Volume
tars are streamed straight into the backups volume by an alpine helper (never
buffered in the backend's memory) to keep RAM bounded on large volumes.

Admin-only. Requires DOCKER host access (docker.sock) and the `docker` SDK.
"""

from __future__ import annotations

import json
import os
import shutil
import tarfile
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.auth.jwt import require_role
from app.auth.models import UserInfo, UserRole
from app.config import settings

logger = structlog.get_logger()

router = APIRouter(prefix="/api/admin/maintenance", tags=["maintenance"])

BACKUP_DIR = Path(os.getenv("AIW_BACKUP_DIR", "/app/backups"))
PROJECT = os.getenv("COMPOSE_PROJECT_NAME", os.getenv("AIW_PROJECT", "infra"))

# Helper image used to tar/untar named volumes. Pinned so an offline registry
# can't leave the job hanging on an implicit ":latest" pull mid-run.
HELPER_IMAGE = os.getenv("AIW_BACKUP_HELPER_IMAGE", "alpine:3.20")


class BackupInfo(BaseModel):
    name: str
    size_bytes: int
    created_utc: str


class BackupResult(BaseModel):
    name: str
    size_bytes: int
    components: list[str]


class RestoreResult(BaseModel):
    restored: list[str]
    skipped: list[str]
    note: str


class JobStatus(BaseModel):
    id: str
    kind: str  # "backup" | "restore"
    status: str  # "running" | "done" | "error"
    step: str | None = None  # human-readable current step
    target: str | None = None  # archive name for restore
    started_utc: str
    finished_utc: str | None = None
    error: str | None = None
    result: dict[str, Any] | None = None


# ── In-process job registry ──────────────────────────────────────────────────
# Single uvicorn worker → a module-level dict is shared across all requests and
# is enough to drive GUI polling. State is intentionally not persisted: if the
# backend restarts mid-job the job is gone (and any partial archive is cleaned up
# by the next run), which is the correct, safe outcome.

_jobs: dict[str, dict[str, Any]] = {}
_jobs_lock = threading.Lock()
_MAX_JOBS = 20  # keep a small history; prune oldest finished beyond this


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _active_job() -> dict[str, Any] | None:
    with _jobs_lock:
        for job in _jobs.values():
            if job["status"] == "running":
                return dict(job)
    return None


def _create_job(kind: str, target: str | None = None) -> dict[str, Any]:
    with _jobs_lock:
        for job in _jobs.values():
            if job["status"] == "running":
                raise HTTPException(
                    409,
                    "Уже выполняется операция обслуживания "
                    f"({job['kind']}). Дождитесь её завершения.",
                )
        job = {
            "id": uuid.uuid4().hex,
            "kind": kind,
            "status": "running",
            "step": None,
            "target": target,
            "started_utc": _now(),
            "finished_utc": None,
            "error": None,
            "result": None,
        }
        _jobs[job["id"]] = job
        # Prune finished jobs beyond the history cap (oldest first).
        finished = [
            (j["finished_utc"] or "", j["id"]) for j in _jobs.values() if j["status"] != "running"
        ]
        finished.sort()
        while len(_jobs) > _MAX_JOBS and finished:
            _, jid = finished.pop(0)
            _jobs.pop(jid, None)
        return dict(job)


def _update_job(job_id: str, **fields: Any) -> None:
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is not None:
            job.update(fields)


def _safe_archive_name(name: str) -> str:
    """Reject path traversal; accept only our archive naming."""
    if (
        "/" in name
        or ".." in name
        or not name.startswith("aiw-backup-")
        or not name.endswith(".tar.gz")
    ):
        raise HTTPException(400, "Некорректное имя архива.")
    return name


def _docker_client():
    try:
        import docker  # lazy: optional dependency
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("docker SDK не установлен в backend-образе.") from exc
    try:
        return docker.from_env()
    except Exception as exc:  # pragma: no cover
        raise RuntimeError(f"Docker socket недоступен: {exc}") from exc


def _container(client, suffix: str):
    """Resolve a compose container by service suffix (e.g. 'postgres')."""
    name = f"{PROJECT}-{suffix}-1"
    try:
        return client.containers.get(name)
    except Exception as exc:
        raise RuntimeError(f"Контейнер {name} не найден: {exc}") from exc


def _ensure_helper_image(client) -> None:
    """Make sure the alpine helper image is present so runs don't stall on an
    implicit pull halfway through (and fail fast+clearly if it can't be pulled)."""
    try:
        client.images.get(HELPER_IMAGE)
        return
    except Exception:
        pass
    try:
        client.images.pull(HELPER_IMAGE)
    except Exception as exc:
        raise RuntimeError(
            f"Не удалось получить вспомогательный образ {HELPER_IMAGE}: {exc}"
        ) from exc


def _tar_volume(client, volume: str, work_subdir: str, out_name: str) -> bool:
    """Tar a named volume directly into the backups volume via an alpine helper.

    Streams to disk inside the helper — nothing is buffered in the backend's
    memory, so volume size doesn't drive backend RAM. Returns False if the
    source volume doesn't exist.
    """
    try:
        client.volumes.get(volume)
    except Exception:
        logger.warning("backup_volume_missing", volume=volume)
        return False
    client.containers.run(
        HELPER_IMAGE,
        command=["sh", "-c", f"tar czf /out/{work_subdir}/{out_name} -C /src ."],
        volumes={
            volume: {"bind": "/src", "mode": "ro"},
            f"{PROJECT}_backups_data": {"bind": "/out", "mode": "rw"},
        },
        remove=True,
        stdout=True,
        stderr=True,
    )
    return True


# ── Backup ───────────────────────────────────────────────────────────────────


def _run_backup(job_id: str) -> None:
    """Heavy backup work — runs in a background thread (never the event loop)."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    name = f"aiw-backup-{ts}"
    work = BACKUP_DIR / name
    archive = BACKUP_DIR / f"{name}.tar.gz"
    components: list[str] = []
    try:
        client = _docker_client()
        _update_job(job_id, step="Подготовка")
        _ensure_helper_image(client)
        work.mkdir(parents=True, exist_ok=True)

        # PostgreSQL (app) — online consistent dump.
        _update_job(job_id, step="PostgreSQL")
        pg = _container(client, "postgres")
        code, out = pg.exec_run(
            [
                "pg_dump",
                "-U",
                settings.postgres_user,
                "-d",
                settings.postgres_db,
                "--clean",
                "--if-exists",
            ],
            stdout=True,
            stderr=False,
        )
        if code == 0 and out:
            (work / "postgres_app.sql").write_bytes(out)
            components.append("postgres_app")
        else:
            raise RuntimeError("pg_dump приложения не удался.")

        # Volumes (streamed straight into the backups volume).
        _update_job(job_id, step="MinIO")
        if _tar_volume(client, f"{PROJECT}_minio_data", name, "minio_data.tar.gz"):
            components.append("minio")
        _update_job(job_id, step="Qdrant")
        if _tar_volume(client, f"{PROJECT}_qdrant_data", name, "qdrant_data.tar.gz"):
            components.append("qdrant")
        _update_job(job_id, step="Redis")
        if _tar_volume(client, f"{PROJECT}_redis_data", name, "redis_data.tar.gz"):
            components.append("redis")

        (work / "manifest.json").write_text(
            json.dumps(
                {
                    "name": name,
                    "created_utc": ts,
                    "project": PROJECT,
                    "components": components,
                    "source": "gui",
                },
                ensure_ascii=False,
                indent=2,
            )
        )

        # Pack into a single archive, then drop the work dir.
        _update_job(job_id, step="Упаковка архива")
        with tarfile.open(archive, "w:gz") as tar:
            for f in sorted(work.iterdir()):
                tar.add(f, arcname=f.name)
        shutil.rmtree(work, ignore_errors=True)

        size = archive.stat().st_size
        logger.info("backup_created", name=name, size=size, components=components)
        _update_job(
            job_id,
            status="done",
            step=None,
            finished_utc=_now(),
            result={"name": f"{name}.tar.gz", "size_bytes": size, "components": components},
        )
    except Exception as exc:
        logger.warning("backup_failed", name=name, error=str(exc))
        # Clean up any partial output so the backups list stays clean.
        shutil.rmtree(work, ignore_errors=True)
        archive.unlink(missing_ok=True)
        _update_job(job_id, status="error", step=None, finished_utc=_now(), error=str(exc))


@router.post("/backup", response_model=JobStatus, status_code=202)
async def create_backup(
    _user: UserInfo = Depends(require_role(UserRole.admin)),
) -> JobStatus:
    """Start a full backup (DB + volumes) as a background job. Returns a job id;
    poll GET /jobs/{id} for progress and the final result."""
    job = _create_job("backup")
    threading.Thread(
        target=_run_backup,
        args=(job["id"],),
        name=f"backup-{job['id'][:8]}",
        daemon=True,
    ).start()
    return JobStatus(**job)


# ── Job status ───────────────────────────────────────────────────────────────


@router.get("/jobs/active", response_model=JobStatus | None)
async def get_active_job(
    _user: UserInfo = Depends(require_role(UserRole.admin)),
) -> JobStatus | None:
    """Return the currently running maintenance job, or null. Lets the GUI resume
    progress after a page reload instead of showing a blank screen."""
    job = _active_job()
    return JobStatus(**job) if job else None


@router.get("/jobs/{job_id}", response_model=JobStatus)
async def get_job(
    job_id: str,
    _user: UserInfo = Depends(require_role(UserRole.admin)),
) -> JobStatus:
    with _jobs_lock:
        job = _jobs.get(job_id)
        snapshot = dict(job) if job else None
    if snapshot is None:
        raise HTTPException(404, "Задача не найдена (возможно, backend перезапускался).")
    return JobStatus(**snapshot)


# ── Listing / download / upload / delete ─────────────────────────────────────


@router.get("/backups", response_model=list[BackupInfo])
async def list_backups(
    _user: UserInfo = Depends(require_role(UserRole.admin)),
) -> list[BackupInfo]:
    """List server-side backup archives, newest first."""
    if not BACKUP_DIR.exists():
        return []
    items = []
    for f in sorted(BACKUP_DIR.glob("aiw-backup-*.tar.gz"), reverse=True):
        st = f.stat()
        items.append(
            BackupInfo(
                name=f.name,
                size_bytes=st.st_size,
                created_utc=datetime.fromtimestamp(st.st_mtime, UTC).isoformat(),
            )
        )
    return items


@router.get("/backups/{name}/download")
async def download_backup(
    name: str,
    _user: UserInfo = Depends(require_role(UserRole.admin)),
) -> FileResponse:
    """Download a backup archive."""
    name = _safe_archive_name(name)
    path = BACKUP_DIR / name
    if not path.is_file():
        raise HTTPException(404, "Архив не найден.")
    return FileResponse(path, media_type="application/gzip", filename=name)


@router.post("/backups/upload", response_model=BackupInfo)
async def upload_backup(
    file: UploadFile = File(...),
    _user: UserInfo = Depends(require_role(UserRole.admin)),
) -> BackupInfo:
    """Upload a backup archive (e.g. migrating from another server)."""
    fname = file.filename or ""
    name = _safe_archive_name(fname)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    dest = BACKUP_DIR / name
    size = 0
    with dest.open("wb") as out:
        while chunk := await file.read(1024 * 1024):
            out.write(chunk)
            size += len(chunk)
    # Sanity: must be a valid gzip tar with our manifest.
    try:
        with tarfile.open(dest, "r:gz") as tar:
            members = tar.getnames()
        if "manifest.json" not in members and "./manifest.json" not in members:
            dest.unlink(missing_ok=True)
            raise HTTPException(400, "Архив не похож на бэкап AI Workspace (нет manifest.json).")
    except tarfile.TarError as exc:
        dest.unlink(missing_ok=True)
        raise HTTPException(400, f"Повреждённый архив: {exc}") from exc
    logger.info("backup_uploaded", name=name, size=size)
    return BackupInfo(
        name=name,
        size_bytes=size,
        created_utc=datetime.now(UTC).isoformat(),
    )


@router.delete("/backups/{name}", status_code=204)
async def delete_backup(
    name: str,
    _user: UserInfo = Depends(require_role(UserRole.admin)),
) -> None:
    """Delete a server-side backup archive."""
    name = _safe_archive_name(name)
    path = BACKUP_DIR / name
    if not path.is_file():
        raise HTTPException(404, "Архив не найден.")
    path.unlink()
    logger.info("backup_deleted", name=name)


# ── Restore ──────────────────────────────────────────────────────────────────


def _restore_volume(client, volume: str, src_rel: str) -> None:
    """Replace a named volume's contents from a tar.gz that already lives in the
    backups volume (at src_rel, relative to the backups volume root)."""
    try:
        client.volumes.get(volume)
    except Exception:
        client.volumes.create(volume)
    client.containers.run(
        HELPER_IMAGE,
        command=[
            "sh",
            "-c",
            f"rm -rf /dst/* /dst/..?* 2>/dev/null; tar xzf /src/{src_rel} -C /dst",
        ],
        volumes={
            volume: {"bind": "/dst", "mode": "rw"},
            f"{PROJECT}_backups_data": {"bind": "/src", "mode": "ro"},
        },
        remove=True,
        stdout=True,
        stderr=True,
    )


def _run_restore(job_id: str, name: str) -> None:
    """Heavy restore work — runs in a background thread. DESTRUCTIVE."""
    archive = BACKUP_DIR / name
    # Unpack the archive to a temp dir inside the backups volume so the alpine
    # helper can read individual members without the backend buffering GBs.
    workdir = BACKUP_DIR / f".restore-{job_id[:8]}"
    restored: list[str] = []
    skipped: list[str] = []
    try:
        client = _docker_client()
        _update_job(job_id, step="Подготовка")
        _ensure_helper_image(client)

        workdir.mkdir(parents=True, exist_ok=True)
        with tarfile.open(archive, "r:gz") as tar:
            # Flatten member names (archives store bare filenames) and guard traversal.
            for m in tar.getmembers():
                if not m.isfile():
                    continue
                base = os.path.basename(m.name)
                if not base:
                    continue
                src = tar.extractfile(m)
                if src is None:
                    continue
                with (workdir / base).open("wb") as dst:
                    shutil.copyfileobj(src, dst)

        rel = workdir.name  # relative to backups volume root

        # ── Volumes: stop owner → restore → start ──
        vol_map = [
            ("minio_data.tar.gz", f"{PROJECT}_minio_data", "minio"),
            ("qdrant_data.tar.gz", f"{PROJECT}_qdrant_data", "qdrant"),
            ("redis_data.tar.gz", f"{PROJECT}_redis_data", "redis"),
        ]
        for fname, volume, svc in vol_map:
            if not (workdir / fname).is_file():
                skipped.append(svc)
                continue
            try:
                _update_job(job_id, step=f"Восстановление: {svc}")
                cont = _container(client, svc)
                cont.stop(timeout=20)
                _restore_volume(client, volume, f"{rel}/{fname}")
                cont.start()
                restored.append(svc)
            except Exception as exc:
                logger.warning("restore_volume_failed", svc=svc, error=str(exc))
                skipped.append(svc)

        # ── PostgreSQL (app) online restore ──
        if (workdir / "postgres_app.sql").is_file():
            _update_job(job_id, step="Восстановление: PostgreSQL")
            try:
                _restore_postgres(client, workdir / "postgres_app.sql")
                restored.append("postgres")
            except Exception as exc:
                logger.warning("restore_postgres_failed", error=str(exc))
                skipped.append("postgres")

        logger.info("backup_restored", name=name, restored=restored, skipped=skipped)
        _update_job(
            job_id,
            status="done",
            step=None,
            finished_utc=_now(),
            result={
                "restored": restored,
                "skipped": skipped,
                "note": "Рекомендуется перезапустить backend после восстановления.",
            },
        )
    except Exception as exc:
        logger.warning("restore_failed", name=name, error=str(exc))
        _update_job(job_id, status="error", step=None, finished_utc=_now(), error=str(exc))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _restore_postgres(client, sql_path: Path) -> None:
    """Restore the app DB from a --clean dump, online."""
    import io as _io

    pg = _container(client, "postgres")
    db = settings.postgres_db
    user = settings.postgres_user
    # Terminate other sessions so --clean DROPs aren't blocked.
    pg.exec_run(
        [
            "psql",
            "-U",
            user,
            "-d",
            "postgres",
            "-c",
            f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            f"WHERE datname='{db}' AND pid<>pg_backend_pid();",
        ],
        stdout=False,
        stderr=False,
    )
    # Copy the dump into the container, then run psql -f against it.
    sql_bytes = sql_path.read_bytes()
    stream = _io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as t:
        ti = tarfile.TarInfo(name="restore.sql")
        ti.size = len(sql_bytes)
        t.addfile(ti, _io.BytesIO(sql_bytes))
    stream.seek(0)
    pg.put_archive("/tmp", stream.getvalue())
    code, out = pg.exec_run(
        ["sh", "-c", f"psql -U {user} -d {db} -f /tmp/restore.sql"],
        stdout=True,
        stderr=True,
    )
    pg.exec_run(["rm", "-f", "/tmp/restore.sql"], stdout=False, stderr=False)
    if code != 0:
        raise RuntimeError((out or b"")[-300:].decode("utf-8", "replace"))


@router.post("/backups/{name}/restore", response_model=JobStatus, status_code=202)
async def restore_backup(
    name: str,
    _user: UserInfo = Depends(require_role(UserRole.admin)),
) -> JobStatus:
    """Start a restore from a server-side archive as a background job. DESTRUCTIVE.

    Volumes (MinIO/Qdrant/Redis): the owning container is stopped, its volume
    replaced, then started again. PostgreSQL is restored online (other sessions
    to the app DB are terminated; the dump is --clean). A backend restart
    afterwards is advised. Poll GET /jobs/{id} for progress and the result.
    """
    name = _safe_archive_name(name)
    archive = BACKUP_DIR / name
    if not archive.is_file():
        raise HTTPException(404, "Архив не найден.")
    job = _create_job("restore", target=name)
    threading.Thread(
        target=_run_restore,
        args=(job["id"], name),
        name=f"restore-{job['id'][:8]}",
        daemon=True,
    ).start()
    return JobStatus(**job)
