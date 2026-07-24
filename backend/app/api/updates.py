"""Updates API — Authentik version detection + admin-confirmed staged upgrade.

The backend cannot edit compose/.env or run `docker compose` (no CLI, files not
mounted). So it does NOT perform the upgrade itself. Instead it:

  * detects the running Authentik version (via the Docker socket) and computes
    the sequential upgrade ladder (Authentik forbids skipping majors), and
  * on an admin's explicit confirmation, drops an update *request* file into the
    shared backups volume.

A small host-side agent (infra/installer/update-agent.sh, run by a systemd timer)
picks the request up and executes infra/installer/upgrade-authentik.sh — which
backs up, walks the ladder one major at a time, verifies health and rolls back on
failure — writing progress back into the same file for the GUI to poll.

Admin-only; the request endpoint additionally requires a human (not the agent).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth.jwt import require_human_role, require_role
from app.auth.models import UserInfo, UserRole

logger = structlog.get_logger()

router = APIRouter(prefix="/api/admin/updates", tags=["updates"])

_admin_dep = Depends(require_role(UserRole.admin))
_human_admin_dep = Depends(require_human_role(UserRole.admin))

BACKUP_DIR = Path(os.getenv("AIW_BACKUP_DIR", "/app/backups"))
CONTROL_DIR = BACKUP_DIR / "_control"
CONTROL_FILE = CONTROL_DIR / "authentik-update.json"
PROJECT = os.getenv("COMPOSE_PROJECT_NAME", os.getenv("AIW_PROJECT", "infra"))

# Ordered Authentik major ladder — MUST match infra/installer/upgrade-authentik.sh.
# Never skip a major; append new ones to the end as they ship.
AUTHENTIK_LADDER = [
    "2025.2", "2025.4", "2025.6", "2025.8", "2025.10", "2025.12", "2026.2", "2026.5",
]


class AuthentikUpdateInfo(BaseModel):
    current_version: str | None
    current_minor: str | None
    latest_minor: str
    remaining: list[str]           # majors still ahead of current, in order
    next_hop: str | None
    up_to_date: bool
    ladder: list[str]
    job: dict[str, Any] | None     # current control-file state, if any


class UpdateRequestIn(BaseModel):
    # "next" = advance one major; "latest" = walk to the end; "to" = up to `target`.
    mode: str = "latest"
    target: str | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _minor_of(tag: str) -> str:
    parts = tag.split(".")
    return ".".join(parts[:2]) if len(parts) >= 2 else tag


def _remaining_ladder(current_minor: str | None) -> list[str]:
    if current_minor is None:
        return list(AUTHENTIK_LADDER)
    if current_minor in AUTHENTIK_LADDER:
        i = AUTHENTIK_LADDER.index(current_minor)
        return AUTHENTIK_LADDER[i + 1:]
    # current older than the first rung → the whole ladder applies
    return list(AUTHENTIK_LADDER)


def _authentik_current_version() -> str | None:
    """Read the tag of the running authentik-server image via the Docker socket."""
    try:
        import docker
    except ImportError:
        return None
    try:
        client = docker.from_env()
        c = client.containers.get(f"{PROJECT}-authentik-server-1")
        for t in getattr(c.image, "tags", []) or []:
            if "goauthentik/server" in t and ":" in t:
                return t.rsplit(":", 1)[1]
        cfg = (c.attrs.get("Config") or {}).get("Image", "")
        if ":" in cfg:
            return cfg.rsplit(":", 1)[1]
    except Exception as exc:  # pragma: no cover - docker/env dependent
        logger.warning("authentik_version_probe_failed", error=str(exc))
    return None


def _read_control() -> dict[str, Any] | None:
    try:
        if CONTROL_FILE.is_file():
            return json.loads(CONTROL_FILE.read_text())
    except Exception as exc:
        logger.warning("update_control_read_failed", error=str(exc))
    return None


def _write_control(state: dict[str, Any]) -> None:
    CONTROL_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONTROL_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2))
    tmp.replace(CONTROL_FILE)  # atomic so the host agent never reads a half-write


@router.get("/authentik", response_model=AuthentikUpdateInfo)
async def authentik_info(_user: UserInfo = _admin_dep) -> AuthentikUpdateInfo:
    current = _authentik_current_version()
    current_minor = _minor_of(current) if current else None
    remaining = _remaining_ladder(current_minor)
    return AuthentikUpdateInfo(
        current_version=current,
        current_minor=current_minor,
        latest_minor=AUTHENTIK_LADDER[-1],
        remaining=remaining,
        next_hop=remaining[0] if remaining else None,
        up_to_date=not remaining,
        ladder=list(AUTHENTIK_LADDER),
        job=_read_control(),
    )


@router.get("/authentik/status")
async def authentik_status(_user: UserInfo = _admin_dep) -> dict[str, Any]:
    return {"job": _read_control()}


@router.post("/authentik/request", response_model=dict)
async def authentik_request(
    payload: UpdateRequestIn,
    admin: UserInfo = _human_admin_dep,
) -> dict[str, Any]:
    """Queue an admin-confirmed Authentik upgrade for the host agent to execute."""
    existing = _read_control()
    if existing and existing.get("status") in ("requested", "running"):
        raise HTTPException(409, "Обновление уже запрошено или выполняется.")

    current = _authentik_current_version()
    current_minor = _minor_of(current) if current else None
    remaining = _remaining_ladder(current_minor)
    if not remaining:
        raise HTTPException(400, "Authentik уже на последней известной версии.")

    mode = payload.mode
    target = payload.target
    if mode == "to":
        if not target or target not in remaining:
            raise HTTPException(
                422, f"Недопустимая цель '{target}'. Доступно: {', '.join(remaining)}"
            )
    elif mode not in ("next", "latest"):
        raise HTTPException(422, f"Недопустимый режим: {mode}")

    planned = (
        [remaining[0]] if mode == "next"
        else remaining if mode == "latest"
        else remaining[: remaining.index(target) + 1]
    )

    state = {
        "kind": "authentik",
        "status": "requested",
        "mode": mode,
        "target": target,
        "planned_hops": planned,
        "from_version": current,
        "requested_by": admin.email or admin.sub,
        "requested_utc": _now(),
        "started_utc": None,
        "finished_utc": None,
        "current_step": None,
        "log_tail": "",
        "error": None,
    }
    _write_control(state)
    logger.info("authentik_update_requested", admin=admin.sub, mode=mode, target=target,
                planned=planned)
    return {"ok": True, "job": state, "note": (
        "Заявка поставлена. Host-агент (systemd-таймер update-agent) выполнит "
        "обновление и вернёт статус сюда. Если агент не установлен — см. "
        "infra/installer/update-agent.README."
    )}


@router.post("/authentik/cancel", response_model=dict)
async def authentik_cancel(admin: UserInfo = _human_admin_dep) -> dict[str, Any]:
    state = _read_control()
    if not state:
        return {"ok": True, "note": "Нет активной заявки."}
    if state.get("status") == "running":
        raise HTTPException(409, "Обновление уже выполняется — отмена невозможна.")
    try:
        CONTROL_FILE.unlink(missing_ok=True)
    except Exception as exc:
        raise HTTPException(500, f"Не удалось отменить: {exc}")
    logger.info("authentik_update_cancelled", admin=admin.sub)
    return {"ok": True}
