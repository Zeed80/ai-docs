"""Admin API to (re)build the Android APK on the server and publish it to /download.

Launches the prebuilt `apk-builder` image (toolchain + shell source) on demand via
the Docker socket; it writes a signed latest.apk + version.json into the releases
volume, which the backend serves at /download. The signing keystore is persisted in
a volume so rebuilds share one signature (in-app self-update works).
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth.jwt import require_role
from app.auth.models import UserInfo, UserRole
from app.config import settings

router = APIRouter()
logger = structlog.get_logger()

PROJECT = os.getenv("COMPOSE_PROJECT_NAME", os.getenv("AIW_PROJECT", "infra"))
_RUN_NAME = f"{PROJECT}-apk-build-run"
_IMAGE = f"{PROJECT}-apk-builder"
_RELEASES = Path(settings.releases_dir)


def _docker_client():
    try:
        import docker  # lazy: optional dependency
    except ImportError as exc:  # pragma: no cover
        raise HTTPException(503, "docker SDK не установлен в backend-образе.") from exc
    try:
        return docker.from_env()
    except Exception as exc:  # pragma: no cover
        raise HTTPException(503, f"Docker socket недоступен: {exc}") from exc


def _current_version() -> dict | None:
    f = _RELEASES / "version.json"
    if f.is_file():
        try:
            return json.loads(f.read_text())
        except Exception:
            return None
    return None


class BuildStatus(BaseModel):
    state: str  # idle | building | success | failed
    version_name: str | None = None
    version_code: int | None = None
    apk_available: bool = False
    log_tail: str | None = None


def _find_run(client):
    try:
        return client.containers.get(_RUN_NAME)
    except Exception:
        return None


@router.get("/status", response_model=BuildStatus)
async def build_status(_user: UserInfo = Depends(require_role(UserRole.admin))) -> BuildStatus:
    client = _docker_client()
    cur = _current_version() or {}
    apk = (_RELEASES / "latest.apk").is_file()
    run = _find_run(client)
    state = "idle"
    log_tail = None
    if run is not None:
        run.reload()
        if run.status == "running":
            state = "building"
        else:
            rc = (run.attrs.get("State", {}) or {}).get("ExitCode", 0)
            state = "success" if rc == 0 else "failed"
        try:
            log_tail = run.logs(tail=40).decode("utf-8", "replace")
        except Exception:
            log_tail = None
    return BuildStatus(
        state=state,
        version_name=cur.get("versionName"),
        version_code=cur.get("versionCode"),
        apk_available=apk,
        log_tail=log_tail,
    )


@router.post("/build", response_model=BuildStatus)
async def start_build(_user: UserInfo = Depends(require_role(UserRole.admin))) -> BuildStatus:
    client = _docker_client()

    # Refuse if a build is already running; clear a finished one.
    run = _find_run(client)
    if run is not None:
        run.reload()
        if run.status == "running":
            raise HTTPException(409, "Сборка уже выполняется.")
        try:
            run.remove(force=True)
        except Exception:
            pass

    # Image must be built (docker compose --profile apk-builder build apk-builder).
    try:
        client.images.get(_IMAGE)
    except Exception as exc:
        raise HTTPException(
            503,
            "Образ apk-builder не собран. Выполните: docker compose --profile apk-builder build apk-builder",
        ) from exc

    cur = _current_version() or {}
    next_code = int(cur.get("versionCode", 0)) + 1
    version_name = f"1.0.{next_code}"

    try:
        client.containers.run(
            _IMAGE,
            environment={"VERSION_NAME": version_name, "VERSION_CODE": str(next_code)},
            volumes={
                f"{PROJECT}_releases_data": {"bind": "/releases", "mode": "rw"},
                f"{PROJECT}_apk_keystore": {"bind": "/keystore", "mode": "rw"},
            },
            detach=True,
            name=_RUN_NAME,
        )
    except Exception as exc:
        raise HTTPException(500, f"Не удалось запустить сборку: {exc}") from exc

    logger.info("apk_build_started", admin=_user.sub, version=version_name)
    return BuildStatus(state="building", version_name=version_name, version_code=next_code,
                       apk_available=(_RELEASES / "latest.apk").is_file())
