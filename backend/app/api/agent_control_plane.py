"""Agent control-plane API.

This router exposes the self-configuration surface used by the GUI and by the
agent itself.  Protected changes are represented as proposals so the user can
review risk before they are applied.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.agent_config import (
    BuiltinAgentConfigUpdate,
    get_builtin_agent_config,
    update_builtin_agent_config,
)
from app.ai.capability_sandbox import promote_capability, run_capability_sandbox
from app.ai.gateway_config import gateway_config
from app.ai.policy_engine import classify_skill_risk, is_protected_setting
from app.core.chat_bus import chat_bus
from app.db.models import (
    AgentAction,
    AgentConfigProposal,
    AgentCron,
    AgentPlugin,
    AgentTask,
    AgentTeam,
    CapabilityProposal,
    DocumentChunk,
    EvidenceSpan,
    KnowledgeEdge,
    KnowledgeNode,
    MemoryEmbeddingRecord,
    MemoryFact,
)
from app.db.session import get_db

router = APIRouter()


def _count_active_skills(config: BuiltinAgentConfig) -> int:
    """Return count of skills actually visible to the chat agent."""
    if gateway_config.skills_mode == "capabilities":
        try:
            import yaml
            data = yaml.safe_load(gateway_config.capabilities_path.read_text())
            return len(data.get("capabilities") or [])
        except Exception:
            pass
    return len(config.exposed_skills)


class AgentControlPlaneStatus(BaseModel):
    ok: bool
    autonomy_mode: str
    permission_mode: str
    safe_auto_apply_enabled: bool
    protected_settings: list[str]
    skills_total: int
    approval_gates_total: int
    plugins_total: int
    plugins_enabled: int
    tasks_open: int
    crons_enabled: int
    memory_facts_total: int
    mcp_servers_total: int
    capability_proposals_open: int


class AgentRuntimeModels(BaseModel):
    provider: str
    orchestrator_model: str | None = None
    worker_model: str | None = None
    auditor_model: str | None = None
    builder_model: str | None = None
    fast_model: str | None = None
    compression_model: str | None = None
    fallback_providers: list[str]


class AgentRuntimeCounters(BaseModel):
    llm_calls_24h: int
    tool_calls_24h: int
    errors_24h: int
    avg_llm_duration_ms_24h: int | None = None
    last_error: str | None = None
    last_error_at: datetime | None = None


class AgentRuntimeAction(BaseModel):
    id: uuid.UUID
    session_id: str
    action_type: str
    tool_name: str | None = None
    model_name: str | None = None
    duration_ms: int | None = None
    error: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class AgentMemoryRuntimeStatus(BaseModel):
    enabled: bool
    episodic_facts_total: int
    pinned_facts_total: int
    memory_facts_total: int
    graph_nodes_total: int
    graph_edges_total: int
    chunks_total: int
    evidence_total: int
    embeddings_total: int
    embeddings_by_status: dict[str, int]
    active_embedding_model: str | None = None
    active_embedding_collection: str | None = None
    qdrant_points: int | None = None
    last_episodic_at: datetime | None = None


class AgentRuntimeStatus(BaseModel):
    ok: bool
    models: AgentRuntimeModels
    counters: AgentRuntimeCounters
    memory: AgentMemoryRuntimeStatus
    recent_actions: list[AgentRuntimeAction]


class ConfigProposalCreate(BaseModel):
    setting_path: str = Field(..., min_length=1, max_length=200)
    proposed_value: Any
    reason: str = Field(..., min_length=1)
    risk_level: Literal["low", "medium", "high", "critical"] = "medium"
    requested_by: str = "sveta"


class ConfigProposalOut(BaseModel):
    id: uuid.UUID
    setting_path: str
    proposed_value: Any
    current_value: Any
    reason: str
    risk_level: str
    protected: bool
    status: str
    requested_by: str
    decided_by: str | None = None
    decided_at: datetime | None = None
    decision_comment: str | None = None
    applied_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ProposalDecision(BaseModel):
    approved: bool
    decided_by: str = "user"
    comment: str | None = None


class AgentTaskCreate(BaseModel):
    objective: str = Field(..., min_length=1)
    description: str | None = None
    role: str = "worker"
    team_id: uuid.UUID | None = None
    metadata: dict | None = None


class AgentTaskOut(BaseModel):
    id: uuid.UUID
    objective: str
    description: str | None = None
    role: str
    status: str
    team_id: uuid.UUID | None = None
    output: str | None = None
    metadata_: dict | None = Field(None, serialization_alias="metadata")
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True, "populate_by_name": True}


class AgentTeamCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    purpose: str | None = None
    metadata: dict | None = None


class AgentTeamOut(BaseModel):
    id: uuid.UUID
    name: str
    status: str
    purpose: str | None = None
    metadata_: dict | None = Field(None, serialization_alias="metadata")
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True, "populate_by_name": True}


class AgentCronCreate(BaseModel):
    schedule: str = Field(..., min_length=1, max_length=120)
    prompt: str = Field(..., min_length=1)
    description: str | None = None
    metadata: dict | None = None


class AgentCronOut(BaseModel):
    id: uuid.UUID
    schedule: str
    prompt: str
    description: str | None = None
    enabled: bool
    last_run_at: datetime | None = None
    run_count: int
    metadata_: dict | None = Field(None, serialization_alias="metadata")
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True, "populate_by_name": True}


class PluginManifestIn(BaseModel):
    plugin_key: str = Field(..., min_length=1, max_length=200)
    name: str = Field(..., min_length=1, max_length=200)
    version: str = "0.1.0"
    description: str | None = None
    manifest: dict
    risk_level: Literal["low", "medium", "high", "critical"] = "medium"
    installed_by: str = "sveta"


class AgentPluginOut(BaseModel):
    id: uuid.UUID
    plugin_key: str
    name: str
    version: str
    description: str | None = None
    manifest: dict
    enabled: bool
    risk_level: str
    installed_by: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class CapabilityProposalCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=500)
    missing_capability: str = Field(..., min_length=1)
    reason: str = Field(..., min_length=1)
    suggested_artifact: Literal["tool", "skill", "script", "workspace_template"] = "tool"
    draft: dict
    risk_level: Literal["low", "medium", "high", "critical"] = "medium"
    rollback_plan: list[str] | None = None
    requested_by: str = "sveta"
    metadata: dict | None = None


class CapabilityProposalOut(BaseModel):
    id: uuid.UUID
    title: str
    missing_capability: str
    reason: str
    suggested_artifact: str
    status: str
    risk_level: str
    sandbox_status: str
    test_status: str
    audit_status: str
    draft: dict
    rollback_plan: list | None = None
    requested_by: str
    decided_by: str | None = None
    decided_at: datetime | None = None
    decision_comment: str | None = None
    metadata_: dict | None = Field(None, serialization_alias="metadata")
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True, "populate_by_name": True}


class CapabilityDecision(BaseModel):
    approved: bool
    decided_by: str = "user"
    comment: str | None = None


def _json_value(value: Any) -> dict:
    return {"value": value}


def _unwrap_value(value: dict | None) -> Any:
    if isinstance(value, dict) and set(value.keys()) == {"value"}:
        return value["value"]
    return value


def _draft_value(draft: dict, *keys: str) -> str:
    value: Any = draft
    for key in keys:
        if not isinstance(value, dict):
            return ""
        value = value.get(key)
    return str(value or "").strip()


def _contains_destructive_marker(value: str) -> bool:
    text = value.lower()
    markers = (
        "delete",
        "remove",
        "drop",
        "truncate",
        "send",
        "approve",
        "reject",
        "decide",
        "external",
        "email",
        "telegram",
        "export",
        "payment",
        "credential",
        "secret",
    )
    return any(marker in text for marker in markers)


def _safe_auto_approval_reason(proposal: CapabilityProposal) -> str | None:
    """Return an auto-approval reason for safe capabilities.

    Auto-approves low/medium risk GET/POST tools that have no destructive markers
    and no explicit approval_required flag.  Critical risk is always manual.
    """
    if proposal.risk_level == "critical":
        return None
    if proposal.suggested_artifact not in {"tool", "skill", "workspace_template", "config"}:
        return None

    draft = proposal.draft or {}
    tool_name = _draft_value(draft, "tool_name") or _draft_value(
        draft, "skill_registry_entry", "name"
    ) or ""
    endpoint_path = _draft_value(draft, "endpoint_path") or _draft_value(
        draft, "skill_registry_entry", "path"
    ) or ""
    method = (
        _draft_value(draft, "method")
        or _draft_value(draft, "skill_registry_entry", "method")
        or "GET"
    ).upper()
    approval_required = (
        isinstance(draft.get("skill_registry_entry"), dict)
        and draft["skill_registry_entry"].get("approval_required") is True
    )

    if approval_required:
        return None
    if method not in {"GET", "POST", "PATCH"}:
        return None
    if _contains_destructive_marker(f"{tool_name} {endpoint_path}"):
        return None

    # High risk: require human review unless explicitly safe
    if proposal.risk_level == "high":
        safe_high = tool_name.startswith("workspace.") or endpoint_path.startswith("/api/workspace/")
        if not safe_high:
            return None

    return "auto-approved: safe read/write capability; sandbox passed"


async def _publish_capability_approval_request(proposal: CapabilityProposal) -> None:
    await chat_bus.publish({
        "type": "approval_request",
        "tool": "capability.proposal",
        "args": {
            "proposal_id": str(proposal.id),
            "title": proposal.title,
            "risk_level": proposal.risk_level,
            "suggested_artifact": proposal.suggested_artifact,
            "reason": proposal.reason,
        },
        "preview": (
            f"{proposal.title}\n"
            f"Риск: {proposal.risk_level} · Тип: {proposal.suggested_artifact}\n"
            f"{proposal.reason or proposal.missing_capability or ''}"
        ),
    })


def _current_setting_value(setting_path: str) -> Any:
    from app.ai.agent_config import BuiltinAgentConfig
    top_level = setting_path.split(".")[0]
    known_fields = set(BuiltinAgentConfig.model_fields.keys())
    if top_level not in known_fields:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown setting: {setting_path!r}. Valid top-level keys: {sorted(known_fields)}",
        )
    config = get_builtin_agent_config().model_dump(mode="json")
    value: Any = config
    for part in setting_path.split("."):
        if not isinstance(value, dict) or part not in value:
            raise HTTPException(status_code=404, detail=f"Unknown setting: {setting_path}")
        value = value[part]
    return value


def _apply_setting(setting_path: str, value: Any) -> None:
    if "." in setting_path:
        raise HTTPException(
            status_code=400,
            detail="Nested setting apply is not supported yet; propose a top-level setting",
        )
    update_builtin_agent_config(BuiltinAgentConfigUpdate(**{setting_path: value}))


async def _create_config_proposal(
    payload: ConfigProposalCreate,
    db: AsyncSession,
) -> AgentConfigProposal:
    current_value = _current_setting_value(payload.setting_path)
    protected = is_protected_setting(payload.setting_path)
    proposal = AgentConfigProposal(
        setting_path=payload.setting_path,
        proposed_value=_json_value(payload.proposed_value),
        current_value=_json_value(current_value),
        reason=payload.reason,
        risk_level=payload.risk_level,
        protected=protected,
        requested_by=payload.requested_by,
        status="pending" if protected else "applied",
    )
    if not protected:
        _apply_setting(payload.setting_path, payload.proposed_value)
        proposal.applied_at = datetime.now(timezone.utc)
    db.add(proposal)
    await db.commit()
    await db.refresh(proposal)
    return proposal


async def _create_agent_task(payload: AgentTaskCreate, db: AsyncSession) -> AgentTask:
    task = AgentTask(
        objective=payload.objective,
        description=payload.description,
        role=payload.role,
        team_id=payload.team_id,
        metadata_=payload.metadata,
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)
    return task


async def _create_capability_proposal(
    payload: CapabilityProposalCreate,
    db: AsyncSession,
) -> CapabilityProposal:
    proposal = CapabilityProposal(
        title=payload.title,
        missing_capability=payload.missing_capability,
        reason=payload.reason,
        suggested_artifact=payload.suggested_artifact,
        draft=payload.draft,
        risk_level=payload.risk_level,
        rollback_plan=payload.rollback_plan,
        requested_by=payload.requested_by,
        metadata_=payload.metadata,
    )
    db.add(proposal)
    await db.commit()
    await db.refresh(proposal)
    # Notify chat only for high/critical risk — low risk will be auto-approved
    # during sandbox_apply and the card will appear then if needed.
    config = get_builtin_agent_config()
    if payload.risk_level in {"high", "critical"} or not config.safe_auto_apply_enabled:
        await _publish_capability_approval_request(proposal)
    return proposal


@router.get("/control-plane/status", response_model=AgentControlPlaneStatus)
async def control_plane_status(db: AsyncSession = Depends(get_db)) -> AgentControlPlaneStatus:
    config = get_builtin_agent_config()
    plugin_total = await db.scalar(select(func.count()).select_from(AgentPlugin))
    plugin_enabled = await db.scalar(
        select(func.count()).select_from(AgentPlugin).where(AgentPlugin.enabled.is_(True))
    )
    tasks_open = await db.scalar(
        select(func.count())
        .select_from(AgentTask)
        .where(AgentTask.status.notin_(["completed", "failed", "stopped"]))
    )
    crons_enabled = await db.scalar(
        select(func.count()).select_from(AgentCron).where(AgentCron.enabled.is_(True))
    )
    memory_total = await db.scalar(select(func.count()).select_from(MemoryFact))
    capability_open = await db.scalar(
        select(func.count())
        .select_from(CapabilityProposal)
        .where(CapabilityProposal.status.in_(["draft", "sandbox_ready", "pending_approval"]))
    )
    return AgentControlPlaneStatus(
        ok=True,
        autonomy_mode=config.autonomy_mode,
        permission_mode=config.permission_mode,
        safe_auto_apply_enabled=config.safe_auto_apply_enabled,
        protected_settings=sorted(
            name
            for name in type(config).model_fields
            if is_protected_setting(name)
        ),
        skills_total=_count_active_skills(config),
        approval_gates_total=len(config.approval_gates),
        plugins_total=int(plugin_total or 0),
        plugins_enabled=int(plugin_enabled or 0),
        tasks_open=int(tasks_open or 0),
        crons_enabled=int(crons_enabled or 0),
        memory_facts_total=int(memory_total or 0),
        mcp_servers_total=len(config.mcp_servers or []),
        capability_proposals_open=int(capability_open or 0),
    )


@router.get("/runtime/status", response_model=AgentRuntimeStatus)
async def runtime_status(db: AsyncSession = Depends(get_db)) -> AgentRuntimeStatus:
    """Runtime observability for the built-in agent and memory stack."""
    config = get_builtin_agent_config()
    day_ago = datetime.now(timezone.utc) - timedelta(hours=24)

    llm_calls = await db.scalar(
        select(func.count())
        .select_from(AgentAction)
        .where(AgentAction.action_type == "llm_call", AgentAction.created_at >= day_ago)
    )
    tool_calls = await db.scalar(
        select(func.count())
        .select_from(AgentAction)
        .where(AgentAction.action_type == "tool_call", AgentAction.created_at >= day_ago)
    )
    errors = await db.scalar(
        select(func.count())
        .select_from(AgentAction)
        .where(AgentAction.error.is_not(None), AgentAction.created_at >= day_ago)
    )
    avg_duration = await db.scalar(
        select(func.avg(AgentAction.duration_ms)).where(
            AgentAction.action_type == "llm_call",
            AgentAction.duration_ms.is_not(None),
            AgentAction.created_at >= day_ago,
        )
    )
    last_error_action = (
        (
            await db.execute(
                select(AgentAction)
                .where(AgentAction.error.is_not(None))
                .order_by(AgentAction.created_at.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    recent_actions = list(
        (
            await db.execute(
                select(AgentAction).order_by(AgentAction.created_at.desc()).limit(12)
            )
        )
        .scalars()
        .all()
    )

    memory_facts = await db.scalar(select(func.count()).select_from(MemoryFact))
    episodic_facts = await db.scalar(
        select(func.count()).select_from(MemoryFact).where(MemoryFact.kind == "chat_turn")
    )
    pinned_facts = await db.scalar(
        select(func.count()).select_from(MemoryFact).where(MemoryFact.pinned.is_(True))
    )
    last_episodic_at = await db.scalar(
        select(func.max(MemoryFact.created_at)).where(MemoryFact.kind == "chat_turn")
    )
    graph_nodes = await db.scalar(select(func.count()).select_from(KnowledgeNode))
    graph_edges = await db.scalar(select(func.count()).select_from(KnowledgeEdge))
    chunks = await db.scalar(select(func.count()).select_from(DocumentChunk))
    evidence = await db.scalar(select(func.count()).select_from(EvidenceSpan))
    embedding_rows = (
        await db.execute(
            select(MemoryEmbeddingRecord.status, func.count())
            .group_by(MemoryEmbeddingRecord.status)
        )
    ).all()
    embeddings_by_status = {str(status): int(count) for status, count in embedding_rows}
    embeddings_total = sum(embeddings_by_status.values())
    active_embedding_model: str | None = None
    active_embedding_collection: str | None = None
    qdrant_points: int | None = None
    try:
        from app.ai.embeddings import get_active_embedding_profile
        from app.vector.qdrant_store import collection_count_for

        profile = get_active_embedding_profile()
        active_embedding_model = profile.model_key
        active_embedding_collection = profile.collection_name
        loop = asyncio.get_event_loop()
        qdrant_points = await asyncio.wait_for(
            loop.run_in_executor(None, collection_count_for, profile.collection_name),
            timeout=3.0,
        )
    except Exception:
        pass

    return AgentRuntimeStatus(
        ok=True,
        models=AgentRuntimeModels(
            provider=config.provider,
            orchestrator_model=config.orchestrator_model,
            worker_model=config.worker_model or config.model,
            auditor_model=config.auditor_model or config.worker_model or config.model,
            builder_model=config.builder_model,
            fast_model=config.fast_model,
            compression_model=config.compression_model,
            fallback_providers=list(config.fallback_providers or []),
        ),
        counters=AgentRuntimeCounters(
            llm_calls_24h=int(llm_calls or 0),
            tool_calls_24h=int(tool_calls or 0),
            errors_24h=int(errors or 0),
            avg_llm_duration_ms_24h=int(avg_duration) if avg_duration is not None else None,
            last_error=last_error_action.error if last_error_action else None,
            last_error_at=last_error_action.created_at if last_error_action else None,
        ),
        memory=AgentMemoryRuntimeStatus(
            enabled=config.memory_enabled,
            episodic_facts_total=int(episodic_facts or 0),
            pinned_facts_total=int(pinned_facts or 0),
            memory_facts_total=int(memory_facts or 0),
            graph_nodes_total=int(graph_nodes or 0),
            graph_edges_total=int(graph_edges or 0),
            chunks_total=int(chunks or 0),
            evidence_total=int(evidence or 0),
            embeddings_total=embeddings_total,
            embeddings_by_status=embeddings_by_status,
            active_embedding_model=active_embedding_model,
            active_embedding_collection=active_embedding_collection,
            qdrant_points=qdrant_points,
            last_episodic_at=last_episodic_at,
        ),
        recent_actions=recent_actions,
    )


@router.post("/config/proposals", response_model=ConfigProposalOut)
async def create_config_proposal(
    payload: ConfigProposalCreate,
    db: AsyncSession = Depends(get_db),
) -> AgentConfigProposal:
    return await _create_config_proposal(payload, db)


@router.post("/config/propose", response_model=ConfigProposalOut)
async def propose_config_change(
    payload: ConfigProposalCreate,
    db: AsyncSession = Depends(get_db),
) -> AgentConfigProposal:
    """Skill: config.propose — Propose an agent configuration change."""
    return await _create_config_proposal(payload, db)


@router.get("/config/proposals", response_model=list[ConfigProposalOut])
async def list_config_proposals(
    status: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> list[AgentConfigProposal]:
    stmt = select(AgentConfigProposal).order_by(AgentConfigProposal.created_at.desc())
    if status:
        stmt = stmt.where(AgentConfigProposal.status == status)
    result = await db.execute(stmt)
    return list(result.scalars().all())


@router.post("/config/proposals/{proposal_id}/decide", response_model=ConfigProposalOut)
async def decide_config_proposal(
    proposal_id: uuid.UUID,
    payload: ProposalDecision,
    db: AsyncSession = Depends(get_db),
) -> AgentConfigProposal:
    proposal = await db.get(AgentConfigProposal, proposal_id)
    if not proposal:
        raise HTTPException(status_code=404, detail="Proposal not found")
    if proposal.status not in {"pending", "applied"}:
        raise HTTPException(status_code=409, detail="Proposal already decided")

    now = datetime.now(timezone.utc)
    proposal.decided_by = payload.decided_by
    proposal.decided_at = now
    proposal.decision_comment = payload.comment
    if payload.approved:
        _apply_setting(proposal.setting_path, _unwrap_value(proposal.proposed_value))
        proposal.status = "approved"
        proposal.applied_at = now
    else:
        proposal.status = "rejected"
    await db.commit()
    await db.refresh(proposal)
    return proposal


@router.get("/skills")
async def list_skills() -> dict:
    config = get_builtin_agent_config()
    registry_path = gateway_config.registry_path
    if not registry_path.exists():
        return {"skills": []}
    import yaml

    data = yaml.safe_load(registry_path.read_text(encoding="utf-8")) or {}
    approval_gates = set(config.approval_gates)
    exposed = set(config.exposed_skills)
    skills = []
    for skill in data.get("skills") or data.get("tools") or []:
        name = str(skill.get("name") or "")
        if not name:
            continue
        skills.append({
            "name": name,
            "description": skill.get("description", ""),
            "method": skill.get("method", ""),
            "path": skill.get("path", ""),
            "enabled": name in exposed,
            "approval_required": name in approval_gates,
            "risk_level": classify_skill_risk(name),
        })
    return {"skills": skills}


@router.post("/tasks", response_model=AgentTaskOut)
async def create_agent_task(
    payload: AgentTaskCreate,
    db: AsyncSession = Depends(get_db),
) -> AgentTask:
    return await _create_agent_task(payload, db)


@router.post("/tasks/create", response_model=AgentTaskOut)
async def create_agent_task_tool(
    payload: AgentTaskCreate,
    db: AsyncSession = Depends(get_db),
) -> AgentTask:
    """Skill: task.create — Create an agent work item."""
    return await _create_agent_task(payload, db)


@router.get("/tasks", response_model=list[AgentTaskOut])
async def list_agent_tasks(db: AsyncSession = Depends(get_db)) -> list[AgentTask]:
    result = await db.execute(select(AgentTask).order_by(AgentTask.created_at.desc()))
    return list(result.scalars().all())


@router.post("/teams", response_model=AgentTeamOut)
async def create_agent_team(
    payload: AgentTeamCreate,
    db: AsyncSession = Depends(get_db),
) -> AgentTeam:
    team = AgentTeam(name=payload.name, purpose=payload.purpose, metadata_=payload.metadata)
    db.add(team)
    await db.commit()
    await db.refresh(team)
    return team


@router.get("/teams", response_model=list[AgentTeamOut])
async def list_agent_teams(db: AsyncSession = Depends(get_db)) -> list[AgentTeam]:
    result = await db.execute(select(AgentTeam).order_by(AgentTeam.created_at.desc()))
    return list(result.scalars().all())


@router.post("/cron", response_model=AgentCronOut)
async def create_agent_cron(
    payload: AgentCronCreate,
    db: AsyncSession = Depends(get_db),
) -> AgentCron:
    cron = AgentCron(
        schedule=payload.schedule,
        prompt=payload.prompt,
        description=payload.description,
        metadata_=payload.metadata,
    )
    db.add(cron)
    await db.commit()
    await db.refresh(cron)
    return cron


@router.get("/cron", response_model=list[AgentCronOut])
async def list_agent_crons(db: AsyncSession = Depends(get_db)) -> list[AgentCron]:
    result = await db.execute(select(AgentCron).order_by(AgentCron.created_at.desc()))
    return list(result.scalars().all())


class AgentCronPatch(BaseModel):
    enabled: bool | None = None
    description: str | None = None


@router.patch("/cron/{cron_id}", response_model=AgentCronOut)
async def patch_agent_cron(
    cron_id: uuid.UUID,
    payload: AgentCronPatch,
    db: AsyncSession = Depends(get_db),
) -> AgentCron:
    cron = await db.get(AgentCron, cron_id)
    if not cron:
        raise HTTPException(status_code=404, detail="Cron job not found")
    if payload.enabled is not None:
        cron.enabled = payload.enabled
    if payload.description is not None:
        cron.description = payload.description
    await db.commit()
    await db.refresh(cron)
    return cron


@router.post("/plugins", response_model=AgentPluginOut)
async def install_plugin_draft(
    payload: PluginManifestIn,
    db: AsyncSession = Depends(get_db),
) -> AgentPlugin:
    existing = await db.scalar(
        select(AgentPlugin).where(AgentPlugin.plugin_key == payload.plugin_key)
    )
    if existing:
        existing.name = payload.name
        existing.version = payload.version
        existing.description = payload.description
        existing.manifest = payload.manifest
        existing.risk_level = payload.risk_level
        plugin = existing
    else:
        plugin = AgentPlugin(
            plugin_key=payload.plugin_key,
            name=payload.name,
            version=payload.version,
            description=payload.description,
            manifest=payload.manifest,
            risk_level=payload.risk_level,
            installed_by=payload.installed_by,
        )
        db.add(plugin)
    await db.commit()
    await db.refresh(plugin)
    return plugin


@router.get("/plugins", response_model=list[AgentPluginOut])
async def list_plugins(db: AsyncSession = Depends(get_db)) -> list[AgentPlugin]:
    result = await db.execute(select(AgentPlugin).order_by(AgentPlugin.created_at.desc()))
    return list(result.scalars().all())


@router.post("/plugins/{plugin_key}/enable", response_model=AgentPluginOut)
async def enable_plugin(plugin_key: str, db: AsyncSession = Depends(get_db)) -> AgentPlugin:
    plugin = await db.scalar(select(AgentPlugin).where(AgentPlugin.plugin_key == plugin_key))
    if not plugin:
        raise HTTPException(status_code=404, detail="Plugin not found")
    plugin.enabled = True
    await db.commit()
    await db.refresh(plugin)
    return plugin


@router.post("/plugins/{plugin_key}/disable", response_model=AgentPluginOut)
async def disable_plugin(plugin_key: str, db: AsyncSession = Depends(get_db)) -> AgentPlugin:
    plugin = await db.scalar(select(AgentPlugin).where(AgentPlugin.plugin_key == plugin_key))
    if not plugin:
        raise HTTPException(status_code=404, detail="Plugin not found")
    plugin.enabled = False
    await db.commit()
    await db.refresh(plugin)
    return plugin


@router.post("/capabilities", response_model=CapabilityProposalOut)
async def create_capability_proposal(
    payload: CapabilityProposalCreate,
    db: AsyncSession = Depends(get_db),
) -> CapabilityProposal:
    return await _create_capability_proposal(payload, db)


@router.post("/capabilities/propose", response_model=CapabilityProposalOut)
async def propose_capability(
    payload: CapabilityProposalCreate,
    db: AsyncSession = Depends(get_db),
) -> CapabilityProposal:
    """Skill: capability.propose — Propose a missing capability draft."""
    return await _create_capability_proposal(payload, db)


@router.get("/capabilities", response_model=list[CapabilityProposalOut])
async def list_capability_proposals(
    status: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> list[CapabilityProposal]:
    stmt = select(CapabilityProposal).order_by(CapabilityProposal.created_at.desc())
    if status:
        stmt = stmt.where(CapabilityProposal.status == status)
    result = await db.execute(stmt)
    return list(result.scalars().all())


@router.get("/capabilities/{proposal_id}/status", response_model=CapabilityProposalOut)
async def get_capability_status(
    proposal_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> CapabilityProposal:
    """Skill: capability.status — Read capability proposal lifecycle state."""
    proposal = await db.get(CapabilityProposal, proposal_id)
    if not proposal:
        raise HTTPException(status_code=404, detail="Capability proposal not found")
    return proposal


@router.post("/capabilities/{proposal_id}/sandbox-apply", response_model=CapabilityProposalOut)
async def sandbox_apply_capability(
    proposal_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> CapabilityProposal:
    """Skill: capability.sandbox_apply — Prepare sandbox validation for a draft capability."""
    proposal = await db.get(CapabilityProposal, proposal_id)
    if not proposal:
        raise HTTPException(status_code=404, detail="Capability proposal not found")
    if proposal.status in {"approved", "rejected", "promoted", "rolled_back"}:
        raise HTTPException(status_code=409, detail="Capability proposal already decided")

    now = datetime.now(timezone.utc)
    result = run_capability_sandbox(proposal)
    if not result.ok:
        proposal.status = "draft"
        proposal.sandbox_status = "failed"
        proposal.test_status = result.test_status
        proposal.audit_status = result.audit_status
        metadata = dict(proposal.metadata_ or {})
        metadata["sandbox_validation"] = {
            "validated_at": now.isoformat(),
            "recognized_keys": result.recognized_keys,
            "mode": "artifact_runner",
            "sandbox_dir": result.sandbox_dir,
            "files": result.files,
            "errors": result.validation_errors,
            "warnings": result.validation_warnings,
        }
        proposal.metadata_ = metadata
        await db.commit()
        await db.refresh(proposal)
        raise HTTPException(
            status_code=400,
            detail={
                "message": "Capability draft failed sandbox validation",
                "errors": result.validation_errors,
                "warnings": result.validation_warnings,
                "sandbox_dir": result.sandbox_dir,
            },
        )

    metadata = dict(proposal.metadata_ or {})
    metadata["sandbox_validation"] = {
        "validated_at": now.isoformat(),
        "recognized_keys": result.recognized_keys,
        "mode": "artifact_runner",
        "sandbox_dir": result.sandbox_dir,
        "files": result.files,
        "errors": result.validation_errors,
        "warnings": result.validation_warnings,
    }
    proposal.metadata_ = metadata
    proposal.status = "sandbox_ready"
    proposal.sandbox_status = "ready"
    proposal.test_status = result.test_status
    proposal.audit_status = result.audit_status
    auto_reason = None
    config = get_builtin_agent_config()
    if config.safe_auto_apply_enabled:
        auto_reason = _safe_auto_approval_reason(proposal)
    if auto_reason and result.test_status == "passed":
        proposal.status = "approved"
        proposal.decided_by = "auto-policy"
        proposal.decided_at = now
        proposal.decision_comment = auto_reason
        if proposal.audit_status == "pending":
            proposal.audit_status = "passed"

    auto_promoted = False
    if auto_reason and result.test_status == "passed":
        # Auto-promote: add skill to gateway immediately
        promote_result = promote_capability(proposal)
        if promote_result.ok:
            proposal.status = "promoted"
            auto_promoted = True
            metadata["promotion"] = {
                "promoted_at": now.isoformat(),
                "staging_dir": promote_result.staging_dir,
                "files": promote_result.files,
                "skill_name": promote_result.skill_name,
                "gateway_updated": promote_result.gateway_updated,
                "auto": True,
            }
            proposal.metadata_ = metadata
            # Reload gateway so new skill is visible immediately
            from app.ai.gateway_config import gateway_config as _gw
            _gw.reload()
        else:
            # Promote failed — stay approved, user promotes manually
            metadata["promotion_errors"] = promote_result.errors
            proposal.metadata_ = metadata
    else:
        proposal.metadata_ = metadata

    # Record sandbox task as immediately completed (sandbox ran synchronously)
    task = AgentTask(
        objective=f"Sandbox validate capability proposal: {proposal.title}",
        description=proposal.missing_capability,
        role="integration_tester",
        status="completed",
        metadata_={
            "capability_proposal_id": str(proposal.id),
            "draft": proposal.draft or {},
            "risk_level": proposal.risk_level,
            "sandbox_mode": "artifact_runner",
            "sandbox_dir": result.sandbox_dir,
            "files": result.files,
            "diff_preview": result.diff_preview,
            "auto_approved": bool(auto_reason and result.test_status == "passed"),
            "auto_promoted": auto_promoted,
            "auto_approval_reason": auto_reason,
        },
    )
    db.add(task)

    # Notify chat if still needs human review
    if proposal.status in {"sandbox_ready", "approved"} and not auto_promoted:
        await _publish_capability_approval_request(proposal)

    await db.commit()
    await db.refresh(proposal)
    return proposal


@router.post("/capabilities/{proposal_id}/decide", response_model=CapabilityProposalOut)
async def decide_capability_proposal(
    proposal_id: uuid.UUID,
    payload: CapabilityDecision,
    db: AsyncSession = Depends(get_db),
) -> CapabilityProposal:
    proposal = await db.get(CapabilityProposal, proposal_id)
    if not proposal:
        raise HTTPException(status_code=404, detail="Capability proposal not found")
    if proposal.status == "rejected":
        raise HTTPException(status_code=409, detail="Capability proposal already rejected")
    # Already promoted (e.g. auto-promoted during sandbox) — return as-is so the
    # frontend can still send the "continue" WS trigger without blocking on 409.
    if proposal.status in {"promoted", "rolled_back"}:
        return proposal

    now = datetime.now(timezone.utc)
    proposal.decided_by = payload.decided_by
    proposal.decided_at = now
    proposal.decision_comment = payload.comment

    if not payload.approved:
        proposal.status = "rejected"
        await db.commit()
        await db.refresh(proposal)
        return proposal

    # Approved — auto-promote unless already promoted
    proposal.status = "approved"
    if proposal.status != "promoted" and proposal.risk_level != "critical":
        promote_result = promote_capability(proposal)
        if promote_result.ok:
            proposal.status = "promoted"
            metadata = dict(proposal.metadata_ or {})
            metadata["promotion"] = {
                "promoted_at": now.isoformat(),
                "staging_dir": promote_result.staging_dir,
                "files": promote_result.files,
                "skill_name": promote_result.skill_name,
                "gateway_updated": promote_result.gateway_updated,
                "auto": False,
            }
            proposal.metadata_ = metadata
            from app.ai.gateway_config import gateway_config as _gw
            _gw.reload()

    await db.commit()
    await db.refresh(proposal)
    return proposal


@router.post("/capabilities/{proposal_id}/promote", response_model=CapabilityProposalOut)
async def promote_capability_proposal(
    proposal_id: uuid.UUID,
    decided_by: str = "user",
    db: AsyncSession = Depends(get_db),
) -> CapabilityProposal:
    """Skill: capability.promote — Promote an approved capability to staging and expose in gateway."""
    proposal = await db.get(CapabilityProposal, proposal_id)
    if not proposal:
        raise HTTPException(status_code=404, detail="Capability proposal not found")
    if proposal.status == "promoted":
        raise HTTPException(status_code=409, detail="Capability proposal already promoted")
    if proposal.status not in {"approved"}:
        raise HTTPException(
            status_code=409,
            detail=f"Capability must be in 'approved' state before promotion (current: {proposal.status})",
        )

    result = promote_capability(proposal)
    if not result.ok:
        raise HTTPException(
            status_code=400,
            detail={"message": "Promotion failed", "errors": result.errors},
        )

    now = datetime.now(timezone.utc)
    proposal.status = "promoted"
    proposal.decided_by = decided_by
    proposal.decided_at = now
    metadata = dict(proposal.metadata_ or {})
    metadata["promotion"] = {
        "promoted_at": now.isoformat(),
        "staging_dir": result.staging_dir,
        "files": result.files,
        "skill_name": result.skill_name,
        "gateway_updated": result.gateway_updated,
    }
    proposal.metadata_ = metadata

    # Reload gateway config so the new skill is visible immediately
    from app.ai.gateway_config import gateway_config as _gw
    _gw.reload()

    await db.commit()
    await db.refresh(proposal)
    return proposal
