"""Maintenance API — backup / restore from the admin GUI.

Backups are produced via the Docker socket (mounted into this container): a
PostgreSQL dump plus tarred MinIO/Qdrant/Redis volumes, packed into a single
archive under /app/backups (a named volume). Mirrors infra/installer/backup.sh
so CLI and GUI produce interchangeable archives.

Admin-only. Requires DOCKER host access (docker.sock) and the `docker` SDK.
"""

from __future__ import annotations

import io
import json
import os
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path

import structlog
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.auth.jwt import require_role
from app.auth.models import UserInfo, UserRole
from app.config import settings

logger = structlog.get_logger()

router = APIRouter(prefix="/api/admin/maintenance", tags=["maintenance"])

BACKUP_DIR = Path(os.getenv("AIW_BACKUP_DIR", "/app/backups"))
PROJECT = os.getenv("COMPOSE_PROJECT_NAME", os.getenv("AIW_PROJECT", "infra"))


class BackupInfo(BaseModel):
    name: str
    size_bytes: int
    created_utc: str


class BackupResult(BaseModel):
    name: str
    size_bytes: int
    components: list[str]


def _docker_client():
    try:
        import docker  # lazy: optional dependency
    except ImportError as exc:  # pragma: no cover
        raise HTTPException(503, "docker SDK не установлен в backend-образе.") from exc
    try:
        return docker.from_env()
    except Exception as exc:  # pragma: no cover
        raise HTTPException(503, f"Docker socket недоступен: {exc}") from exc


def _container(client, suffix: str):
    """Resolve a compose container by service suffix (e.g. 'postgres')."""
    name = f"{PROJECT}-{suffix}-1"
    try:
        return client.containers.get(name)
    except Exception as exc:
        raise HTTPException(503, f"Контейнер {name} не найден: {exc}") from exc


def _tar_volume(client, volume: str, out_path: Path) -> bool:
    """Tar a named volume to out_path via a throwaway alpine container."""
    try:
        client.volumes.get(volume)
    except Exception:
        logger.warning("backup_volume_missing", volume=volume)
        return False
    # alpine streams the tar to stdout; we capture and write it out.
    stream = client.containers.run(
        "alpine",
        command=["sh", "-c", "tar czf - -C /src ."],
        volumes={volume: {"bind": "/src", "mode": "ro"}},
        remove=True,
        stdout=True,
        stderr=False,
    )
    out_path.write_bytes(stream)
    return True


@router.post("/backup", response_model=BackupResult)
async def create_backup(
    _user: UserInfo = Depends(require_role(UserRole.admin)),
) -> BackupResult:
    """Create a full backup archive (DB + volumes + env) and store it server-side."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    name = f"aiw-backup-{ts}"
    work = BACKUP_DIR / name
    work.mkdir(parents=True, exist_ok=True)
    components: list[str] = []
    client = _docker_client()

    # PostgreSQL (app) — online consistent dump.
    pg = _container(client, "postgres")
    code, out = pg.exec_run(
        ["pg_dump", "-U", settings.postgres_user, "-d", settings.postgres_db,
         "--clean", "--if-exists"],
        stdout=True, stderr=False,
    )
    if code == 0 and out:
        (work / "postgres_app.sql").write_bytes(out)
        components.append("postgres_app")
    else:
        raise HTTPException(500, "pg_dump приложения не удался.")

    # Volumes
    if _tar_volume(client, f"{PROJECT}_minio_data", work / "minio_data.tar.gz"):
        components.append("minio")
    if _tar_volume(client, f"{PROJECT}_qdrant_data", work / "qdrant_data.tar.gz"):
        components.append("qdrant")
    if _tar_volume(client, f"{PROJECT}_redis_data", work / "redis_data.tar.gz"):
        components.append("redis")

    (work / "manifest.json").write_text(json.dumps({
        "name": name, "created_utc": ts, "project": PROJECT,
        "components": components, "source": "gui",
    }, ensure_ascii=False, indent=2))

    # Pack into a single archive, then drop the work dir.
    archive = BACKUP_DIR / f"{name}.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        for f in work.iterdir():
            tar.add(f, arcname=f.name)
    for f in work.iterdir():
        f.unlink()
    work.rmdir()

    size = archive.stat().st_size
    logger.info("backup_created", name=name, size=size, components=components)
    return BackupResult(name=f"{name}.tar.gz", size_bytes=size, components=components)


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
        items.append(BackupInfo(
            name=f.name, size_bytes=st.st_size,
            created_utc=datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(),
        ))
    return items


@router.get("/backups/{name}/download")
async def download_backup(
    name: str,
    _user: UserInfo = Depends(require_role(UserRole.admin)),
) -> FileResponse:
    """Download a backup archive."""
    # Prevent path traversal — accept only known archive names.
    if "/" in name or ".." in name or not name.startswith("aiw-backup-"):
        raise HTTPException(400, "Некорректное имя архива.")
    path = BACKUP_DIR / name
    if not path.is_file():
        raise HTTPException(404, "Архив не найден.")
    return FileResponse(path, media_type="application/gzip", filename=name)
