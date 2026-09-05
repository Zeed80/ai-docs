"""Mail server deployment — admin-confirmed, executed by the host agent.

Mailcow is a separate docker-compose project living in the repo checkout
(infra/mailcow), so the backend cannot deploy it itself: it has no compose CLI,
no repo mount and no business holding the Docker socket for that. The same
hand-off as the Authentik upgrader is used instead (see app/api/updates.py):

    GUI → request file in the shared backups volume → host agent (systemd timer,
    infra/installer/update-agent.sh) runs infra/installer/install-mailcow.sh →
    progress written back into the same file → GUI polls it.

Everything the installer cannot do — DNS records, firewall ports, DKIM
publication, the Mailcow API key and its IP allow-list — is deliberately left to
the human and documented in the GUI guide (/admin/integrations/mailcow-guide)
and infra/installer/mailcow.README.
"""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.auth.jwt import require_human_role, require_role
from app.auth.models import UserInfo, UserRole

logger = structlog.get_logger()

router = APIRouter(prefix="/api/admin/mail-server", tags=["mail-server"])

_admin_dep = Depends(require_role(UserRole.admin))
_human_admin_dep = Depends(require_human_role(UserRole.admin))

BACKUP_DIR = Path(os.getenv("AIW_BACKUP_DIR", "/app/backups"))
CONTROL_DIR = BACKUP_DIR / "_control"
CONTROL_FILE = CONTROL_DIR / "mailcow-install.json"

# Kept in sync with install-mailcow.sh (FALLBACK_TAG) — only used to prefill the
# form; the installer itself decides what to check out.
DEFAULT_TAG = "2026-07"

_HOSTNAME_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$")


class DeployRequestIn(BaseModel):
    mail_domain: str = Field(..., description="Хост вебмейла/админки, например mail.example.com")
    timezone: str = Field(default="Europe/Moscow")
    tag: str | None = Field(default=None, description="Релиз Mailcow; пусто — рекомендованный")


class DeployState(BaseModel):
    status: str  # idle | requested | running | done | error
    mail_domain: str | None = None
    tag: str | None = None
    timezone: str | None = None
    requested_by: str | None = None
    requested_utc: str | None = None
    started_utc: str | None = None
    finished_utc: str | None = None
    current_step: str | None = None
    log_tail: str = ""
    error: str | None = None


class DeployStatusOut(BaseModel):
    installed: bool
    agent_available: bool
    job: DeployState | None = None
    default_tag: str = DEFAULT_TAG
    suggested_domain: str | None = None
    note: str | None = None


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def read_control() -> dict[str, Any] | None:
    try:
        return json.loads(CONTROL_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_control(state: dict[str, Any]) -> None:
    CONTROL_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONTROL_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(CONTROL_FILE)


def installed_tag() -> str | None:
    """The deployed Mailcow release, if any.

    ``MAILCOW_TAG`` reaches the backend only after the stack is recreated with the
    updated infra/.env, so a fresh deployment would otherwise look "not installed"
    until the next restart. The finished job is the second source of truth.
    """
    env_tag = (os.getenv("MAILCOW_TAG") or "").strip()
    if env_tag:
        return env_tag
    job = read_control() or {}
    if job.get("status") == "done":
        return (job.get("tag") or DEFAULT_TAG).strip() or DEFAULT_TAG
    return None


# The agent fires every minute; treat it as alive within a few missed ticks.
_HEARTBEAT_MAX_AGE_S = 300


def _agent_available() -> bool:
    """Whether the host agent is actually running.

    Only a FRESH heartbeat counts. The lock file it creates stays behind forever
    after the first run, so using it would report a long-removed agent as alive
    and the GUI would happily queue a request nobody executes.
    """
    beat = CONTROL_DIR / "agent.heartbeat"
    try:
        age = datetime.now(UTC).timestamp() - beat.stat().st_mtime
    except OSError:
        return False
    return age <= _HEARTBEAT_MAX_AGE_S


@router.get("/deploy/status", response_model=DeployStatusOut)
async def deploy_status(_user: UserInfo = _admin_dep) -> DeployStatusOut:
    job = read_control()
    tag = installed_tag()
    suggested = None
    domain = (os.getenv("TRAEFIK_DOMAIN") or "").strip()
    if domain and domain != "localhost":
        suggested = f"mail.{domain}"
    return DeployStatusOut(
        installed=bool(tag),
        agent_available=_agent_available(),
        job=DeployState(**{**{"status": "idle"}, **(job or {})}) if job else None,
        suggested_domain=suggested,
        note=None
        if _agent_available()
        else (
            "Host-агент не обнаружен: заявка будет ждать его установки "
            "(infra/installer/update-agent.README). Либо разверните вручную: "
            "bash infra/installer/install-mailcow.sh --domain <хост> --yes"
        ),
    )


@router.post("/deploy", response_model=dict)
async def request_deploy(
    payload: DeployRequestIn,
    admin: UserInfo = _human_admin_dep,
) -> dict[str, Any]:
    """Queue the Mailcow deployment for the host agent.

    Human-only (not the agent): this brings up a public mail server on the host.
    """
    existing = read_control()
    if existing and existing.get("status") in ("requested", "running"):
        raise HTTPException(409, "Развёртывание уже запрошено или выполняется.")
    if installed_tag():
        raise HTTPException(
            400,
            "Mailcow уже развёрнут. Обновление — через infra/installer/update-mailcow.sh.",
        )

    mail_domain = (payload.mail_domain or "").strip().lower().rstrip(".")
    if not _HOSTNAME_RE.match(mail_domain):
        raise HTTPException(
            422,
            "Укажите полное доменное имя хоста почты, например mail.example.com "
            "(без схемы и пути).",
        )
    tag = (payload.tag or DEFAULT_TAG).strip()
    if not re.match(r"^[\w.\-]{1,40}$", tag):
        raise HTTPException(422, f"Недопустимый тег релиза: {tag}")
    tz = (payload.timezone or "Europe/Moscow").strip()
    if not re.match(r"^[\w/+\-]{1,64}$", tz):
        raise HTTPException(422, f"Недопустимый часовой пояс: {tz}")

    state = {
        "kind": "mailcow-install",
        "status": "requested",
        "mail_domain": mail_domain,
        "tag": tag,
        "timezone": tz,
        "requested_by": admin.email or admin.sub,
        "requested_utc": _now(),
        "started_utc": None,
        "finished_utc": None,
        "current_step": None,
        "log_tail": "",
        "error": None,
    }
    _write_control(state)
    logger.info("mailcow_deploy_requested", admin=admin.sub, domain=mail_domain, tag=tag)
    return {
        "ok": True,
        "job": state,
        "note": (
            "Заявка поставлена. Host-агент развернёт Mailcow и вернёт прогресс сюда. "
            "DNS, порты фаервола, DKIM и API-ключ настраиваются вручную — см. руководство."
        ),
    }


@router.post("/deploy/cancel", response_model=dict)
async def cancel_deploy(admin: UserInfo = _human_admin_dep) -> dict[str, Any]:
    state = read_control()
    if not state:
        return {"ok": True, "note": "Нет активной заявки."}
    if state.get("status") == "running":
        raise HTTPException(409, "Развёртывание уже выполняется — отмена невозможна.")
    try:
        CONTROL_FILE.unlink(missing_ok=True)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"Не удалось отменить: {exc}")
    logger.info("mailcow_deploy_cancelled", admin=admin.sub)
    return {"ok": True}


@router.post("/deploy/dismiss", response_model=dict)
async def dismiss_deploy_result(admin: UserInfo = _human_admin_dep) -> dict[str, Any]:
    """Clear a finished/failed job so the card returns to its normal state."""
    state = read_control()
    if state and state.get("status") in ("requested", "running"):
        raise HTTPException(409, "Заявка ещё активна.")
    CONTROL_FILE.unlink(missing_ok=True)
    logger.info("mailcow_deploy_dismissed", admin=admin.sub)
    return {"ok": True}
