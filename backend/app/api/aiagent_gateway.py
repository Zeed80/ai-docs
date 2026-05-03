"""Official AiAgent Gateway control API.

These endpoints are not normal agent skills. They are control-plane callbacks
used by the Gateway to pause on approval-gated tool calls and resume safely
after a human decision.
"""

import json
import uuid
from pathlib import Path
from typing import Any

import httpx
import yaml
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.service import log_action
from app.db.models import AgentAction, Approval, ApprovalActionType, ApprovalStatus
from app.db.session import get_db
from app.domain.aiagent_gateway import (
    AiAgentApprovalRequest,
    AiAgentApprovalTicket,
    AiAgentResumeStatus,
)

router = APIRouter()

BACKEND_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = BACKEND_ROOT.parent
CONFIG_PATH = BACKEND_ROOT / "data" / "aiagent_config.json"
CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
REGISTRY_PATHS = [
    Path("/aiagent/skills/_registry.yml"),
    PROJECT_ROOT / "aiagent" / "skills" / "_registry.yml",
    BACKEND_ROOT / "aiagent" / "skills" / "_registry.yml",
]
STRICT_GATEWAY_PATHS = [
    Path("/aiagent/config/gateway.strict.yml"),
    PROJECT_ROOT / "aiagent" / "config" / "gateway.strict.yml",
    BACKEND_ROOT / "aiagent" / "config" / "gateway.strict.yml",
]
OFFICIAL_CONFIG_PATHS = [
    Path("/aiagent/official/aiagent.json"),
    PROJECT_ROOT / "aiagent" / "official" / "aiagent.json",
    BACKEND_ROOT / "aiagent" / "official" / "aiagent.json",
]


class AiAgentProjectSettings(BaseModel):
    first_run_completed: bool = False
    agent_ws_mode: str = Field(default="legacy", pattern="^(legacy|aiagent)$")
    aiagent_ws_url: str = "ws://localhost:18789"
    aiagent_http_url: str = "http://localhost:18789"
    legacy_ws_url: str = "ws://localhost:8000/ws/chat"
    fallback_to_legacy: bool = True
    gateway_bind: str = Field(default="lan", pattern="^(loopback|lan|tailnet|auto|custom)$")
    gateway_auth: str = Field(default="token", pattern="^(none|token|password|trusted-proxy)$")
    gateway_token_configured: bool = False
    gateway_token_env: str = "AIAGENT_GATEWAY_TOKEN"
    dashboard_url: str = "http://localhost:18789/"
    strict_allowlist: bool = True
    model_primary: str = ""
    model_fallbacks: list[str] = []
    model_allowlist: list[str] = []
    image_max_dimension_px: int = 1200
    telegram_enabled: bool = False
    telegram_bot_token_configured: bool = False
    telegram_bot_token_env: str = "TELEGRAM_BOT_TOKEN"
    telegram_dm_policy: str = Field(
        default="pairing",
        pattern="^(pairing|allowlist|open|disabled)$",
    )
    telegram_allow_from: list[str] = []
    telegram_groups_require_mention: bool = True
    session_dm_scope: str = Field(
        default="per-channel-peer",
        pattern="^(main|per-peer|per-channel-peer|per-account-channel-peer)$",
    )
    notes: str = ""


class AiAgentProjectSettingsUpdate(BaseModel):
    first_run_completed: bool | None = None
    agent_ws_mode: str | None = Field(default=None, pattern="^(legacy|aiagent)$")
    aiagent_ws_url: str | None = None
    aiagent_http_url: str | None = None
    legacy_ws_url: str | None = None
    fallback_to_legacy: bool | None = None
    gateway_bind: str | None = Field(default=None, pattern="^(loopback|lan|tailnet|auto|custom)$")
    gateway_auth: str | None = Field(default=None, pattern="^(none|token|password|trusted-proxy)$")
    gateway_token_configured: bool | None = None
    gateway_token_env: str | None = None
    dashboard_url: str | None = None
    strict_allowlist: bool | None = None
    model_primary: str | None = None
    model_fallbacks: list[str] | None = None
    model_allowlist: list[str] | None = None
    image_max_dimension_px: int | None = None
    telegram_enabled: bool | None = None
    telegram_bot_token_configured: bool | None = None
    telegram_bot_token_env: str | None = None
    telegram_dm_policy: str | None = Field(
        default=None,
        pattern="^(pairing|allowlist|open|disabled)$",
    )
    telegram_allow_from: list[str] | None = None
    telegram_groups_require_mention: bool | None = None
    session_dm_scope: str | None = Field(
        default=None,
        pattern="^(main|per-peer|per-channel-peer|per-account-channel-peer)$",
    )
    notes: str | None = None


class AiAgentStatus(BaseModel):
    settings: AiAgentProjectSettings
    gateway_available: bool
    gateway_status: str
    gateway_detail: dict[str, Any] | None = None
    registry_tools: int
    approval_gates: int
    supported_scenarios: list[str]
    official_config_available: bool
    official_config_path: str | None = None
    config_warnings: list[str]
    control_available: bool
    control_note: str


class AiAgentOfficialConfigApplyResult(BaseModel):
    written: bool
    path: str | None
    config: dict[str, Any]
    warnings: list[str]


@router.get("/settings", response_model=AiAgentProjectSettings)
async def get_aiagent_settings() -> AiAgentProjectSettings:
    return _load_aiagent_settings()


@router.patch("/settings", response_model=AiAgentProjectSettings)
async def update_aiagent_settings(
    payload: AiAgentProjectSettingsUpdate,
) -> AiAgentProjectSettings:
    settings = _load_aiagent_settings()
    next_settings = settings.model_copy(update=payload.model_dump(exclude_none=True))
    _save_aiagent_settings(next_settings)
    return next_settings


@router.post("/settings/reset", response_model=AiAgentProjectSettings)
async def reset_aiagent_settings() -> AiAgentProjectSettings:
    settings = AiAgentProjectSettings()
    _save_aiagent_settings(settings)
    return settings


@router.get("/status", response_model=AiAgentStatus)
async def get_aiagent_status() -> AiAgentStatus:
    project_settings = _load_aiagent_settings()
    gateway_available = False
    gateway_status = "unavailable"
    gateway_detail: dict[str, Any] | None = None
    gateway_urls = [
        project_settings.aiagent_http_url.rstrip("/"),
        "http://aiagent-gateway:18789",
        "http://host-gateway:18789",
    ]
    seen_gateway_urls: set[str] = set()
    async with httpx.AsyncClient(timeout=3) as client:
        for gateway_url in gateway_urls:
            if not gateway_url or gateway_url in seen_gateway_urls:
                continue
            seen_gateway_urls.add(gateway_url)
            try:
                response = await client.get(f"{gateway_url}/")
                gateway_available = response.is_success
                content_type = response.headers.get("content-type", "")
                if "application/json" in content_type and response.content:
                    gateway_detail = response.json()
                else:
                    gateway_detail = {"content_type": content_type, "url": gateway_url}
                gateway_status = str((gateway_detail or {}).get("status") or response.status_code)
                if gateway_available:
                    break
            except Exception as exc:
                gateway_status = str(exc)

    registry = _load_registry()
    tools = registry.get("tools", [])
    official_config_path = _first_existing_parent(OFFICIAL_CONFIG_PATHS)
    warnings = _config_warnings(project_settings)
    return AiAgentStatus(
        settings=project_settings,
        gateway_available=gateway_available,
        gateway_status=gateway_status,
        gateway_detail=gateway_detail,
        registry_tools=len(tools),
        approval_gates=len([tool for tool in tools if tool.get("approval_required")]),
        supported_scenarios=_supported_scenarios(),
        official_config_available=official_config_path is not None,
        official_config_path=str(official_config_path) if official_config_path else None,
        config_warnings=warnings,
        control_available=False,
        control_note=(
            "Backend не управляет Docker socket. Запуск/остановка Gateway остаются "
            "операторскими командами: make aiagent-official-up/down."
        ),
    )


@router.get("/official-config")
async def get_official_config_preview() -> dict[str, Any]:
    settings = _load_aiagent_settings()
    config, warnings = _build_official_config(settings)
    return {"config": config, "warnings": warnings}


@router.post("/official-config/apply", response_model=AiAgentOfficialConfigApplyResult)
async def apply_official_config() -> AiAgentOfficialConfigApplyResult:
    settings = _load_aiagent_settings().model_copy(update={"first_run_completed": True})
    config, warnings = _build_official_config(settings)
    target = _first_existing_parent(OFFICIAL_CONFIG_PATHS)
    if target is None:
        warnings.append("AiAgent official config path is not mounted into this backend runtime")
        return AiAgentOfficialConfigApplyResult(
            written=False,
            path=None,
            config=config,
            warnings=warnings,
        )
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        warnings.append(f"Cannot write AiAgent official config: {exc}")
        return AiAgentOfficialConfigApplyResult(
            written=False,
            path=str(target),
            config=config,
            warnings=warnings,
        )
    _save_aiagent_settings(settings)
    return AiAgentOfficialConfigApplyResult(
        written=True,
        path=str(target),
        config=config,
        warnings=warnings,
    )


@router.post("/approvals/request", response_model=AiAgentApprovalTicket, status_code=201)
async def request_gateway_approval(
    payload: AiAgentApprovalRequest,
    db: AsyncSession = Depends(get_db),
):
    """Gateway callback: create approval and pause an approval-gated tool call."""
    tool = _registry_tool(payload.tool_name)
    if tool is None:
        raise HTTPException(404, "Tool is not in generated registry")
    if not tool.get("approval_required"):
        raise HTTPException(400, "Tool does not require approval")

    action = AgentAction(
        session_id=payload.session_id,
        iteration=payload.iteration,
        action_type="approval_request",
        tool_name=payload.tool_name,
        tool_args=payload.tool_args,
        tool_result={"status": "pending"},
        content_text=payload.reason,
    )
    db.add(action)
    await db.flush()

    approval = Approval(
        action_type=ApprovalActionType.agent_tool_call,
        entity_type=payload.entity_type or "agent_action",
        entity_id=payload.entity_id or action.id,
        requested_by="aiagent",
        assigned_to=payload.assigned_to,
        context={
            "session_id": payload.session_id,
            "iteration": payload.iteration,
            "tool_name": payload.tool_name,
            "tool_args": payload.tool_args or {},
            "reason": payload.reason,
            "agent_action_id": str(action.id),
        },
    )
    db.add(approval)
    await db.flush()
    action.tool_result = {"status": "pending", "approval_id": str(approval.id)}

    await log_action(
        db,
        action="aiagent.approval_request",
        entity_type="approval",
        entity_id=approval.id,
        details={"tool_name": payload.tool_name, "agent_action_id": str(action.id)},
    )
    await db.commit()
    await db.refresh(approval)
    await db.refresh(action)
    return AiAgentApprovalTicket(
        approval_id=approval.id,
        agent_action_id=action.id,
        status=approval.status.value,
        tool_name=payload.tool_name,
        created_at=approval.created_at,
    )


@router.get("/approvals/{approval_id}/resume", response_model=AiAgentResumeStatus)
async def get_gateway_resume_status(
    approval_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Gateway callback: read approval decision before resuming a tool call."""
    result = await db.execute(select(Approval).where(Approval.id == approval_id))
    approval = result.scalar_one_or_none()
    if not approval:
        raise HTTPException(404, "Approval not found")
    context = approval.context or {}
    if approval.action_type != ApprovalActionType.agent_tool_call:
        raise HTTPException(400, "Approval is not an AiAgent tool-call approval")

    if approval.status != ApprovalStatus.pending:
        action_id = context.get("agent_action_id")
        if action_id:
            action = await db.get(AgentAction, uuid.UUID(action_id))
            if action:
                action.action_type = "approval_decision"
                action.tool_result = {
                    "approval_id": str(approval.id),
                    "status": approval.status.value,
                    "decided_by": approval.decided_by,
                }
                await db.commit()

    return AiAgentResumeStatus(
        approval_id=approval.id,
        status=approval.status.value,
        approved=approval.status == ApprovalStatus.approved,
        rejected=approval.status == ApprovalStatus.rejected,
        tool_name=context.get("tool_name"),
        tool_args=context.get("tool_args"),
        decision_comment=approval.decision_comment,
        decided_by=approval.decided_by,
        decided_at=approval.decided_at,
    )


def _registry_tool(tool_name: str) -> dict[str, Any] | None:
    registry = _load_registry()
    for tool in registry.get("tools", []):
        if tool.get("name") == tool_name:
            return tool
    return None


def _load_registry() -> dict[str, Any]:
    registry_path = _first_existing(REGISTRY_PATHS)
    if registry_path is None:
        return {}
    if registry_path.suffix.lower() in {".yml", ".yaml"}:
        return yaml.safe_load(registry_path.read_text(encoding="utf-8")) or {}
    return json.loads(registry_path.read_text(encoding="utf-8"))


def _supported_scenarios() -> list[str]:
    path = _first_existing(STRICT_GATEWAY_PATHS)
    if path is None:
        return []
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return [scenario["name"] for scenario in raw.get("scenarios", []) if scenario.get("name")]


def _load_aiagent_settings() -> AiAgentProjectSettings:
    if CONFIG_PATH.exists():
        try:
            return AiAgentProjectSettings(
                **json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            )
        except Exception:
            pass
    return AiAgentProjectSettings()


def _save_aiagent_settings(settings: AiAgentProjectSettings) -> None:
    CONFIG_PATH.write_text(
        json.dumps(settings.model_dump(mode="json"), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _first_existing(paths: list[Path]) -> Path | None:
    for path in paths:
        if path.exists():
            return path
    return None


def _first_existing_parent(paths: list[Path]) -> Path | None:
    for path in paths:
        if path.exists() or path.parent.exists():
            return path
    return None


def _config_warnings(settings: AiAgentProjectSettings) -> list[str]:
    warnings: list[str] = []
    if settings.agent_ws_mode == "aiagent" and not settings.gateway_token_configured:
        warnings.append("Gateway token is not marked as configured")
    if settings.telegram_enabled and not settings.telegram_bot_token_configured:
        warnings.append("Telegram is enabled but bot token is not marked as configured")
    if not settings.model_primary:
        warnings.append("Primary AiAgent model is not selected")
    if settings.telegram_dm_policy == "open" and "*" not in settings.telegram_allow_from:
        warnings.append("Telegram open DM policy should explicitly include '*' in allowFrom")
    return warnings


def _build_official_config(
    settings: AiAgentProjectSettings,
) -> tuple[dict[str, Any], list[str]]:
    registry = _load_registry()
    exposed_skills = [tool["name"] for tool in registry.get("tools", []) if tool.get("name")]
    skills = exposed_skills if settings.strict_allowlist else []
    models = {
        model: {"alias": model.rsplit("/", 1)[-1]}
        for model in settings.model_allowlist
        if model
    }
    if settings.model_primary and settings.model_primary not in models:
        models[settings.model_primary] = {"alias": settings.model_primary.rsplit("/", 1)[-1]}
    for model in settings.model_fallbacks:
        if model and model not in models:
            models[model] = {"alias": model.rsplit("/", 1)[-1]}

    config: dict[str, Any] = {
        "agents": {
            "defaults": {
                "skills": skills,
                "imageMaxDimensionPx": settings.image_max_dimension_px,
            }
        },
        "gateway": {
            "mode": "local",
            "bind": settings.gateway_bind,
            "auth": {"mode": settings.gateway_auth},
            "reload": {"mode": "hybrid", "debounceMs": 300},
            "controlUi": {
                "allowedOrigins": [
                    settings.aiagent_http_url.rstrip("/"),
                    settings.dashboard_url.rstrip("/"),
                    "http://localhost:18789",
                    "http://127.0.0.1:18789",
                ]
            },
        },
        "session": {"dmScope": settings.session_dm_scope},
    }
    if settings.gateway_auth == "token":
        config["gateway"]["auth"]["token"] = f"${{{settings.gateway_token_env}}}"
    if settings.model_primary:
        config["agents"]["defaults"]["model"] = {
            "primary": settings.model_primary,
            "fallbacks": [model for model in settings.model_fallbacks if model],
        }
    if models:
        config["agents"]["defaults"]["models"] = models
    if settings.telegram_enabled:
        telegram: dict[str, Any] = {
            "enabled": True,
            "botToken": f"${{{settings.telegram_bot_token_env}}}",
            "dmPolicy": settings.telegram_dm_policy,
            "groups": {"*": {"requireMention": settings.telegram_groups_require_mention}},
        }
        if settings.telegram_allow_from:
            telegram["allowFrom"] = settings.telegram_allow_from
        config["channels"] = {"telegram": telegram}
    return config, _config_warnings(settings)
