"""Maintenance API — backup / restore from the admin GUI.

Backups are produced via the Docker socket (mounted into this container): a
PostgreSQL dump plus tarred MinIO/Qdrant/Redis volumes, packed into a single
archive under /app/backups (a named volume). Mirrors infra/installer/backup.sh
so CLI and GUI produce interchangeable archives.

Admin-only. Requires DOCKER host access (docker.sock) and the `docker` SDK.
"""

from __future__ import annotations

import json
import os
import tarfile
from datetime import datetime, timezone
from pathlib import Path

import structlog
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
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


class RestoreResult(BaseModel):
    restored: list[str]
    skipped: list[str]
    note: str


def _safe_archive_name(name: str) -> str:
    """Reject path traversal; accept only our archive naming."""
    if "/" in name or ".." in name or not name.startswith("aiw-backup-") \
            or not name.endswith(".tar.gz"):
        raise HTTPException(400, "Некорректное имя архива.")
    return name


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
        name=name, size_bytes=size,
        created_utc=datetime.now(timezone.utc).isoformat(),
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


def _restore_volume(client, volume: str, member_bytes: bytes) -> bool:
    """Replace a named volume's contents from a tar.gz byte blob."""
    try:
        client.volumes.get(volume)
    except Exception:
        client.volumes.create(volume)
    import tempfile
    # Write the inner tar to a temp file the alpine helper can read via bind mount.
    with tempfile.NamedTemporaryFile(suffix=".tar.gz", dir=str(BACKUP_DIR), delete=False) as tf:
        tf.write(member_bytes)
        tmp_name = os.path.basename(tf.name)
    try:
        client.containers.run(
            "alpine",
            command=["sh", "-c", f"rm -rf /dst/* /dst/..?* 2>/dev/null; tar xzf /src/{tmp_name} -C /dst"],
            volumes={
                volume: {"bind": "/dst", "mode": "rw"},
                # BACKUP_DIR is the backups_data volume mounted at /app/backups here;
                # mount the same host volume into the helper at /src.
                f"{PROJECT}_backups_data": {"bind": "/src", "mode": "ro"},
            },
            remove=True, stdout=False, stderr=False,
        )
        return True
    finally:
        (BACKUP_DIR / tmp_name).unlink(missing_ok=True)


@router.post("/backups/{name}/restore", response_model=RestoreResult)
async def restore_backup(
    name: str,
    _user: UserInfo = Depends(require_role(UserRole.admin)),
) -> RestoreResult:
    """Restore from a server-side archive. DESTRUCTIVE.

    Volumes (MinIO/Qdrant/Redis): the owning container is stopped, its volume
    replaced, then started again. PostgreSQL is restored online (other sessions
    to the app DB are terminated; the dump is --clean). Backend keeps running but
    its DB pool will briefly reconnect — a backend restart afterwards is advised.
    """
    name = _safe_archive_name(name)
    archive = BACKUP_DIR / name
    if not archive.is_file():
        raise HTTPException(404, "Архив не найден.")

    client = _docker_client()
    restored: list[str] = []
    skipped: list[str] = []

    # Read archive members into memory map (archives are modest; volumes are the
    # large parts and we stream those straight to the helper).
    members: dict[str, bytes] = {}
    with tarfile.open(archive, "r:gz") as tar:
        for m in tar.getmembers():
            if m.isfile():
                f = tar.extractfile(m)
                if f:
                    members[os.path.basename(m.name)] = f.read()

    # ── Volumes: stop owner → restore → start ──
    vol_map = [
        ("minio_data.tar.gz",  f"{PROJECT}_minio_data",  "minio"),
        ("qdrant_data.tar.gz", f"{PROJECT}_qdrant_data", "qdrant"),
        ("redis_data.tar.gz",  f"{PROJECT}_redis_data",  "redis"),
    ]
    for fname, volume, svc in vol_map:
        if fname not in members:
            skipped.append(svc); continue
        try:
            cont = _container(client, svc)
            cont.stop(timeout=20)
            _restore_volume(client, volume, members[fname])
            cont.start()
            restored.append(svc)
        except Exception as exc:
            logger.warning("restore_volume_failed", svc=svc, error=str(exc))
            skipped.append(svc)

    # ── PostgreSQL (app) online restore ──
    if "postgres_app.sql" in members:
        pg = _container(client, "postgres")
        db = settings.postgres_db
        user = settings.postgres_user
        try:
            # Terminate other sessions so --clean DROPs aren't blocked.
            pg.exec_run([
                "psql", "-U", user, "-d", "postgres", "-c",
                f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                f"WHERE datname='{db}' AND pid<>pg_backend_pid();",
            ], stdout=False, stderr=False)
            # Copy the dump into the container via the Docker put_archive API,
            # then run psql -f against it (no shared volume needed).
            import io as _io
            sql_bytes = members["postgres_app.sql"]
            stream = _io.BytesIO()
            with tarfile.open(fileobj=stream, mode="w") as t:
                ti = tarfile.TarInfo(name="restore.sql")
                ti.size = len(sql_bytes)
                t.addfile(ti, _io.BytesIO(sql_bytes))
            stream.seek(0)
            pg.put_archive("/tmp", stream.getvalue())
            code, out = pg.exec_run(
                ["sh", "-c", f"psql -U {user} -d {db} -f /tmp/restore.sql"],
                stdout=True, stderr=True,
            )
            if code != 0:
                raise RuntimeError((out or b"")[-300:].decode("utf-8", "replace"))
            pg.exec_run(["rm", "-f", "/tmp/restore.sql"], stdout=False, stderr=False)
            restored.append("postgres")
        except Exception as exc:
            logger.warning("restore_postgres_failed", error=str(exc))
            skipped.append("postgres")

    logger.info("backup_restored", name=name, restored=restored, skipped=skipped)
    return RestoreResult(
        restored=restored, skipped=skipped,
        note="Рекомендуется перезапустить backend после восстановления.",
    )
