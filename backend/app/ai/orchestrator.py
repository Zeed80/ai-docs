"""Department-level orchestrator for the built-in agent.

The orchestrator owns the user request lifecycle: intent routing, worker
assignment, rich-output policy, workspace verification, and post-run audit.
The existing AgentSession remains the tool-calling executor.
"""

from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import httpx
import structlog
from pydantic import BaseModel, Field

logger = structlog.get_logger()

from app.ai.agent_config import BuiltinAgentConfig, get_builtin_agent_config
from app.ai.agent_loop import AgentSession
from app.ai.orchestrator_memory import TurnFeedback, build_tool_preference_hint, record_turn_feedback
from app.ai.policy_engine import check_tool_execution
from app.ai.router import ai_router
from app.ai.schemas import AIRequest, AITask, ChatMessage
from app.domain.workspace import get_workspace_block, list_workspace_blocks, upsert_workspace_block

SendFn = Callable[[dict], Awaitable[None]]

_CANVAS_MAP_PATH = Path(__file__).parent.parent.parent / "data" / "canvas_skill_map.json"
_canvas_map_cache: dict[str, Any] | None = None
_canvas_map_mtime: float = 0.0


def _canvas_map() -> dict[str, Any]:
    """Load canvas_skill_map.json with mtime-based refresh."""
    global _canvas_map_cache, _canvas_map_mtime
    try:
        mtime = _CANVAS_MAP_PATH.stat().st_mtime
        if _canvas_map_cache is None or mtime != _canvas_map_mtime:
            _canvas_map_cache = json.loads(_CANVAS_MAP_PATH.read_text(encoding="utf-8"))
            _canvas_map_mtime = mtime
    except Exception:
        if _canvas_map_cache is None:
            _canvas_map_cache = {}
    return _canvas_map_cache

WorkerRole = Literal[
    "data_analyst",
    "invoice_specialist",
    "warehouse_specialist",
    "procurement_specialist",
    "accountant",
    "engineer",
    "memory_researcher",
    "document_builder",
    "script_builder",
]

OutputChannel = Literal["chat", "workspace"]
OutputType = Literal["text", "table", "document", "links", "chart", "drawing", "script"]

_ORCHESTRATOR_SYSTEM = """Ты оркестратор отдела ИИ-сотрудников.
Верни только JSON по заданной схеме. Не отвечай пользователю текстом.
Твоя задача: понять намерение, выбрать профильного исполнителя, определить,
нужен ли Рабочий стол, какие skills рекомендовать и какой canvas_id обновлять.

Правила:
- Простые короткие ответы: channel=chat, required=false.
- Таблицы, документы, ссылки, файлы, графики, чертежи, полные списки,
  изменения существующих таблиц и просьбы "добавь/убери/переставь столбец"
  всегда channel=workspace, required=true.
- Для follow-up изменения таблицы не подтверждай старый блок: план должен
  требовать обновления существующего workspace-блока.
- Если пользователь просит дополнительные данные к уже выведенной таблице,
  это workspace update, а не экспорт и не только текст в чат.
- Используй роли только из допустимого enum схемы.
"""


class WorkspaceOutputSpec(BaseModel):
    channel: OutputChannel = "chat"
    output_type: OutputType = "text"
    required: bool = False
    canvas_id: str | None = None
    description: str = ""


class WorkerAssignment(BaseModel):
    role: WorkerRole
    task: str
    recommended_skills: list[str] = Field(default_factory=list)
    allow_skill_expansion: bool = True


class OrchestratorPlan(BaseModel):
    goal: str
    intent: str
    worker: WorkerAssignment
    workspace: WorkspaceOutputSpec
    audit_required: bool = True


class AuditReport(BaseModel):
    passed: bool
    issues: list[str] = Field(default_factory=list)
    workspace_verified: bool = False
    final_channel: OutputChannel = "chat"


class CapabilityGapRequest(BaseModel):
    missing_capability: str
    reason: str
    suggested_artifact: Literal["tool", "skill", "script", "workspace_template"] = "tool"
    builder_model: str | None = None


class CapabilityBuildDraft(BaseModel):
    title: str
    tool_name: str
    endpoint_path: str
    method: str = "POST"
    skill_registry_entry: dict[str, Any] = Field(default_factory=dict)
    request_schema: dict[str, Any] = Field(default_factory=dict)
    response_schema: dict[str, Any] = Field(default_factory=dict)
    implementation_plan: list[str] = Field(default_factory=list)
    validation_plan: list[str] = Field(default_factory=list)
    notes: str = ""


@dataclass
class _TurnTrace:
    workspace_events: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: list[str] = field(default_factory=list)
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    text_chunks: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    saw_done: bool = False

    @property
    def final_text(self) -> str:
        return "".join(self.text_chunks).strip()


class AgentOrchestrator:
    """Controller above a concrete AgentSession executor."""

    def __init__(self, send: SendFn) -> None:
        self._outer_send = send
        self._trace = _TurnTrace()
        self._workspace_before: dict[str, str] = {}
        self._history: list[dict[str, str]] = []
        self._executor = AgentSession(self._send_from_executor)

    def hydrate_history(self, messages: list[dict[str, str]]) -> None:
        self._history = list(messages[-20:])
        self._executor.hydrate_history(messages)

    async def on_approval(self, approved: bool) -> None:
        await self._executor.on_approval(approved)

    async def on_user_message(self, content: str) -> None:
        config = get_builtin_agent_config()
        if not config.department_enabled:
            await self._executor.on_user_message(content)
            return

        turn_started_at = time.time()
        self._trace = _TurnTrace()
        plan = await self._plan_turn_with_model(content, config)
        self._workspace_before = _workspace_updated_at_snapshot()
        await self._announce_plan(plan)
        await self._executor.on_user_message(content)
        audit = await self._audit_turn(plan, config)
        retry_count = 0
        while (
            not audit.passed
            and retry_count < config.max_audit_retries
            and self._can_retry_with_executor(plan, audit)
        ):
            retry_count += 1
            await self._outer_send({
                "type": "audit.retry_started",
                "content": "Аудит: инструмент/вывод не соответствуют задаче, запускаю исправление.",
                "audit": audit.model_dump(mode="json"),
            })
            self._trace = _TurnTrace()
            self._workspace_before = _workspace_updated_at_snapshot()
            await self._executor.on_user_message(_build_correction_request(plan, audit))
            audit = await self._audit_turn(plan, config)
        if not audit.passed:
            repaired = await self._try_execute_planned_workspace_tool(plan, audit, config)
            if repaired:
                audit = await self._audit_turn(plan, config)
        await self._publish_audit(audit)
        if not audit.passed and self._should_report_capability_gap(plan, audit, config):
            await self._publish_capability_gap(plan, audit, config)

        # Record turn outcome for adaptive planning in future turns
        _record_feedback_async(
            content=content,
            plan=plan,
            trace=self._trace,
            audit=audit,
            retries=retry_count,
            duration_ms=int((time.time() - turn_started_at) * 1000),
        )

        self._history.append({"role": "user", "content": content})
        self._history = self._history[-20:]
        await self._outer_send({"type": "done"})

    async def _plan_turn_with_model(
        self,
        content: str,
        config: BuiltinAgentConfig,
    ) -> OrchestratorPlan:
        heuristic_plan = self._plan_turn(content)
        preference_hint = build_tool_preference_hint(
            intent_text=content,
            intent_category=heuristic_plan.worker.role,
            candidate_skills=list(heuristic_plan.worker.recommended_skills),
        )
        prompt = _build_orchestrator_prompt(
            content=content,
            fallback_plan=heuristic_plan,
            history=self._history,
            preference_hint=preference_hint,
        )
        try:
            response = await ai_router.run(
                AIRequest(
                    task=AITask.CLASSIFICATION,
                    messages=[
                        ChatMessage(role="system", content=_ORCHESTRATOR_SYSTEM),
                        ChatMessage(role="user", content=prompt),
                    ],
                    response_schema=OrchestratorPlan,
                    confidential=True,
                    allow_cloud=False,
                    preferred_model=_registry_model_name(
                        config.orchestrator_model or config.fast_model
                    ),
                )
            )
            if isinstance(response.data, OrchestratorPlan):
                return _normalize_model_plan(response.data, content)
        except Exception as exc:
            logger.warning(
                "orchestrator_plan_model_failed",
                model=config.orchestrator_model or config.fast_model,
                error=str(exc),
            )
        return heuristic_plan

    async def _send_from_executor(self, data: dict) -> None:
        msg_type = str(data.get("type") or "")
        if msg_type == "done":
            self._trace.saw_done = True
            return
        if msg_type == "text":
            self._trace.text_chunks.append(str(data.get("content") or ""))
        elif msg_type in {"canvas", "workspace.updated"}:
            self._trace.workspace_events.append(data)
        elif msg_type == "tool_call":
            self._trace.tool_calls.append(str(data.get("tool") or ""))
        elif msg_type == "tool_result":
            self._trace.tool_results.append(data)
            result = data.get("result")
            if isinstance(result, dict) and result.get("canvas_id"):
                self._trace.workspace_events.append({
                    "type": "workspace.updated",
                    "canvas_id": result.get("canvas_id"),
                })
        elif msg_type == "error":
            self._trace.errors.append(str(data.get("content") or ""))
        await self._outer_send(data)

    def _plan_turn(self, content: str) -> OrchestratorPlan:
        text = _norm(content)
        workspace_required = _is_workspace_request(text)
        role: WorkerRole = "data_analyst"
        skills: list[str] = list(_canvas_map().get("default_skills", ["memory.search", "table.query"]))
        intent = "general"
        output_type: OutputType = "text"
        canvas_id: str | None = None

        matched_route = _match_intent_route(text)
        if matched_route or _is_table_edit_request(text):
            if matched_route:
                role = matched_route["role"]
                intent = matched_route["intent"]
                skills = list(matched_route.get("skills", skills))
                canvas_id = _resolve_canvas_from_route(matched_route, text)
        else:
            canvas_id = None

        if workspace_required:
            output_type = (
                "table"
                if any(token in text for token in ("таблиц", "список", "столб", "колон"))
                else "document"
            )
            if output_type == "table" and _references_existing_table(text):
                canvas_id = _latest_workspace_table_id() or canvas_id

        return OrchestratorPlan(
            goal=content.strip()[:500],
            intent=intent,
            worker=WorkerAssignment(
                role=role,
                task=content.strip(),
                recommended_skills=skills,
                allow_skill_expansion=True,
            ),
            workspace=WorkspaceOutputSpec(
                channel="workspace" if workspace_required else "chat",
                output_type=output_type,
                required=workspace_required,
                canvas_id=canvas_id,
                description="Rich-вывод должен быть опубликован на существующий Рабочий стол."
                if workspace_required
                else "Короткий ответ допустим в чате.",
            ),
            audit_required=True,
        )

    async def _announce_plan(self, plan: OrchestratorPlan) -> None:
        await self._outer_send({
            "type": "orchestrator.status",
            "content": f"Оркестратор: понял задачу, назначаю роль {plan.worker.role}.",
            "plan": plan.model_dump(mode="json"),
        })
        await self._outer_send({
            "type": "worker.assigned",
            "content": (
                "Исполнитель: "
                f"{plan.worker.role}; рекомендованные инструменты: "
                f"{', '.join(plan.worker.recommended_skills[:5])}."
            ),
            "role": plan.worker.role,
            "skills": plan.worker.recommended_skills,
        })
        if plan.workspace.required:
            await self._outer_send({
                "type": "workspace.publish_started",
                "content": "Рабочий стол: готовлю rich-вывод и проверю публикацию.",
                "canvas_id": plan.workspace.canvas_id,
            })

    async def _audit_turn(
        self,
        plan: OrchestratorPlan,
        config: BuiltinAgentConfig,
    ) -> AuditReport:
        if not config.audit_enabled:
            return AuditReport(
                passed=True,
                workspace_verified=bool(self._trace.workspace_events),
                final_channel=plan.workspace.channel,
            )

        issues: list[str] = []
        workspace_verified = False
        if plan.workspace.required:
            workspace_verified = await self._verify_workspace(plan)
            if not workspace_verified:
                issues.append("Запрошен rich-вывод, но публикация на Рабочий стол не подтверждена.")
            if _looks_like_chat_table(self._trace.final_text):
                issues.append("Табличный результат попал в чат вместо Рабочего стола.")
            expected_canvas = plan.workspace.canvas_id
            published_canvas_ids = {
                str(canvas_id)
                for canvas_id in (_event_canvas_id(event) for event in self._trace.workspace_events)
                if canvas_id
            }
            if (
                expected_canvas
                and published_canvas_ids
                and expected_canvas not in published_canvas_ids
            ):
                issues.append(
                    "Использован неправильный workspace-блок: "
                    f"ожидался {expected_canvas}, опубликовано {sorted(published_canvas_ids)}."
                )

        expected_from_canvas = _expected_workspace_skill_for_canvas(plan.workspace.canvas_id)
        expected_workspace_skills = (
            {expected_from_canvas.replace(".", "__")}
            if expected_from_canvas
            else {
                skill.replace(".", "__")
                for skill in plan.worker.recommended_skills
                if skill.startswith("workspace.")
            }
        )
        used_workspace_skills = {
            tool for tool in self._trace.tool_calls if tool.startswith("workspace__")
        }
        if (
            expected_workspace_skills
            and used_workspace_skills
            and not expected_workspace_skills.intersection(used_workspace_skills)
        ):
            issues.append(
                "Исполнитель выбрал инструмент вне плана: "
                f"ожидались {sorted(expected_workspace_skills)}, "
                f"использованы {sorted(used_workspace_skills)}."
            )

        for item in self._trace.tool_results:
            result = item.get("result")
            if (
                isinstance(result, dict)
                and str(result.get("error") or "").startswith("Unknown skill")
            ):
                issues.append(str(result["error"]))

        return AuditReport(
            passed=not issues,
            issues=issues,
            workspace_verified=workspace_verified,
            final_channel=plan.workspace.channel,
        )

    async def _verify_workspace(self, plan: OrchestratorPlan) -> bool:
        for event in self._trace.workspace_events:
            canvas_id = _event_canvas_id(event)
            if canvas_id and self._workspace_block_changed(str(canvas_id)):
                return True
        return False

    def _workspace_block_changed(self, canvas_id: str) -> bool:
        block = get_workspace_block(canvas_id)
        if not block:
            return False
        updated_at = str(block.get("updated_at") or "")
        before = self._workspace_before.get(canvas_id)
        return bool(updated_at) and updated_at != before

    async def _publish_audit(self, audit: AuditReport) -> None:
        if audit.workspace_verified:
            await self._outer_send({
                "type": "workspace.publish_verified",
                "content": "Рабочий стол: публикация подтверждена.",
                "audit": audit.model_dump(mode="json"),
            })
        if audit.passed:
            await self._outer_send({
                "type": "audit.passed",
                "content": "Аудит: результат проверен, канал вывода корректный.",
                "audit": audit.model_dump(mode="json"),
            })
        else:
            await self._outer_send({
                "type": "audit.failed",
                "content": "Аудит: требуется исправление результата.",
                "audit": audit.model_dump(mode="json"),
            })

    def _should_report_capability_gap(
        self,
        plan: OrchestratorPlan,
        audit: AuditReport,
        config: BuiltinAgentConfig,
    ) -> bool:
        if not config.allow_capability_builder:
            return False
        if any("Unknown skill" in issue for issue in audit.issues):
            return True
        if any("неправильный workspace-блок" in issue for issue in audit.issues):
            return True
        if any("инструмент вне плана" in issue for issue in audit.issues):
            return True
        return plan.workspace.required and not audit.workspace_verified

    def _can_retry_with_executor(self, plan: OrchestratorPlan, audit: AuditReport) -> bool:
        if not plan.workspace.required:
            return False
        if not plan.worker.recommended_skills:
            return False
        if any("Unknown skill" in issue for issue in audit.issues):
            return False
        return any(
            marker in issue
            for issue in audit.issues
            for marker in (
                "не подтверждена",
                "неправильный workspace-блок",
                "инструмент вне плана",
            )
        )

    async def _try_execute_planned_workspace_tool(
        self,
        plan: OrchestratorPlan,
        audit: AuditReport,
        config: BuiltinAgentConfig,
    ) -> bool:
        if not plan.workspace.required:
            return False
        if not any(
            marker in issue
            for issue in audit.issues
            for marker in (
                "не подтверждена",
                "неправильный workspace-блок",
                "инструмент вне плана",
            )
        ):
            return False
        spec = _workspace_tool_spec_for_plan(plan)
        if not spec:
            return False

        tool_name: str = spec["tool"]
        approval_gates: set[str] = set(config.approval_gates or [])
        policy = check_tool_execution(
            skill_name=tool_name,
            args=spec["args"],
            config=config,
            approval_gates=approval_gates,
        )
        if not policy.allowed or tool_name in approval_gates:
            reason = (
                f"требует подтверждения человеком (approval gate)"
                if tool_name in approval_gates
                else policy.reason
            )
            await self._outer_send({
                "type": "orchestrator.direct_tool_blocked",
                "content": (
                    f"Оркестратор: инструмент {tool_name!r} заблокирован политикой — {reason}. "
                    "Требуется явное подтверждение через интерфейс."
                ),
                "tool": tool_name,
                "reason": reason,
                "risk_level": policy.risk_level,
            })
            return False

        self._trace = _TurnTrace()
        self._workspace_before = _workspace_updated_at_snapshot()
        await self._outer_send({
            "type": "orchestrator.direct_tool_started",
            "content": (
                "Оркестратор: исполнитель выбрал не тот инструмент, "
                "запускаю правильный workspace tool напрямую."
            ),
            "tool": tool_name,
            "args": spec["args"],
        })
        await self._record_orchestrator_tool_event({
            "type": "tool_call",
            "tool": tool_name,
            "args": spec["args"],
        })
        try:
            async with httpx.AsyncClient(timeout=float(config.backend_timeout_seconds)) as client:
                resp = await client.post(
                    f"{config.backend_url.rstrip('/')}{spec['path']}",
                    json=spec["args"],
                )
            if resp.status_code >= 400:
                await self._record_orchestrator_tool_event({
                    "type": "tool_result",
                    "tool": tool_name,
                    "result": {
                        "error": f"HTTP {resp.status_code}",
                        "detail": resp.text[:300],
                    },
                })
                return False
            result = resp.json()
        except Exception as exc:
            await self._record_orchestrator_tool_event({
                "type": "tool_result",
                "tool": tool_name,
                "result": {"error": str(exc)},
            })
            return False

        await self._record_orchestrator_tool_event({
            "type": "tool_result",
            "tool": tool_name,
            "result": result,
        })
        message = str(result.get("message") or "") if isinstance(result, dict) else ""
        if message:
            await self._record_orchestrator_tool_event({
                "type": "text",
                "content": message,
            })
        return True

    async def _record_orchestrator_tool_event(self, event: dict[str, Any]) -> None:
        await self._send_from_executor(event)

    async def _publish_capability_gap(
        self,
        plan: OrchestratorPlan,
        audit: AuditReport,
        config: BuiltinAgentConfig,
    ) -> None:
        gap = CapabilityGapRequest(
            missing_capability=plan.workspace.description or plan.intent,
            reason="; ".join(audit.issues) or "Недостаточно существующих инструментов.",
            suggested_artifact="workspace_template" if plan.workspace.required else "tool",
            builder_model=(
                config.builder_model
                or config.orchestrator_model
                or config.model
            ),
        )
        await self._outer_send({
            "type": "capability_gap.detected",
            "content": (
                "Оркестратор: обнаружил недостающую исполнимую возможность. "
                "Передаю задачу builder-модели и готовлю новый tool/skill draft."
            ),
            "gap": gap.model_dump(mode="json"),
        })
        draft = await self._build_capability_draft(gap, plan, config)
        proposal_id = await self._persist_capability_proposal(gap, draft, plan, audit, config)
        await self._outer_send({
            "type": "capability_gap.builder_draft",
            "content": "Builder: подготовил проект недостающего инструмента и skill-записи.",
            "draft": draft.model_dump(mode="json"),
            "proposal_id": proposal_id,
        })
        upsert_workspace_block(
            "agent:capability-builder-draft",
            {
                "id": "agent:capability-builder-draft",
                "type": "markdown",
                "title": "Проект недостающей возможности",
                "content": _format_capability_draft_markdown(draft, proposal_id=proposal_id),
                "source": "orchestrator.capability_builder",
            },
        )
        await self._outer_send({
            "type": "workspace.updated",
            "canvas_id": "agent:capability-builder-draft",
        })

    async def _build_capability_draft(
        self,
        gap: CapabilityGapRequest,
        plan: OrchestratorPlan,
        config: BuiltinAgentConfig,
    ) -> CapabilityBuildDraft:
        prompt = (
            "Нужно спроектировать недостающий backend tool и AiAgent skill.\n"
            f"Gap: {gap.model_dump(mode='json')}\n"
            f"Plan: {plan.model_dump(mode='json')}\n"
            f"Used tools: {self._trace.tool_calls}\n"
            f"Errors: {self._trace.errors}\n"
            "Верни CapabilityBuildDraft JSON. Не пиши prose вне JSON."
        )
        try:
            response = await ai_router.run(
                AIRequest(
                    task=AITask.CLASSIFICATION,
                    messages=[
                        ChatMessage(
                            role="system",
                            content=(
                                "Ты builder-инженер. Проектируешь недостающие "
                                "FastAPI tools, workspace templates и AiAgent skills."
                            ),
                        ),
                        ChatMessage(role="user", content=prompt),
                    ],
                    response_schema=CapabilityBuildDraft,
                    confidential=True,
                    allow_cloud=False,
                    preferred_model=_registry_model_name(
                        config.builder_model or config.orchestrator_model
                    ),
                )
            )
            if isinstance(response.data, CapabilityBuildDraft):
                return response.data
        except Exception:
            pass
        return _fallback_capability_draft(gap, plan)

    async def _persist_capability_proposal(
        self,
        gap: CapabilityGapRequest,
        draft: CapabilityBuildDraft,
        plan: OrchestratorPlan,
        audit: AuditReport,
        config: BuiltinAgentConfig,
    ) -> str | None:
        try:
            async with httpx.AsyncClient(timeout=float(config.backend_timeout_seconds)) as client:
                resp = await client.post(
                    f"{config.backend_url.rstrip('/')}/api/agent/capabilities/propose",
                    json={
                        "title": draft.title,
                        "missing_capability": gap.missing_capability,
                        "reason": gap.reason,
                        "suggested_artifact": gap.suggested_artifact,
                        "draft": draft.model_dump(mode="json"),
                        "risk_level": _capability_risk_level(gap, audit),
                        "rollback_plan": [
                            "Do not promote generated files until tests and audit pass.",
                            "Disable the generated skill and remove it from exposed_skills on rollback.",
                            "Revert sandbox branch or discard draft files if promotion is rejected.",
                        ],
                        "metadata": {
                            "plan": plan.model_dump(mode="json"),
                            "audit": audit.model_dump(mode="json"),
                            "used_tools": self._trace.tool_calls,
                        },
                    },
                )
            if resp.status_code >= 400:
                return None
            data = resp.json()
            proposal_id = data.get("id")
            if proposal_id and config.safe_auto_apply_enabled and data.get("risk_level") in {
                "low",
                "medium",
            }:
                await self._sandbox_capability_proposal(str(proposal_id), config)
            return str(proposal_id) if proposal_id else None
        except Exception:
            return None

    async def _sandbox_capability_proposal(
        self,
        proposal_id: str,
        config: BuiltinAgentConfig,
    ) -> None:
        try:
            async with httpx.AsyncClient(timeout=float(config.backend_timeout_seconds)) as client:
                await client.post(
                    f"{config.backend_url.rstrip('/')}/api/agent/capabilities/"
                    f"{proposal_id}/sandbox-apply",
                )
        except Exception:
            pass


def _norm(text: str) -> str:
    return (text or "").lower().replace("ё", "е")


def _build_orchestrator_prompt(
    *,
    content: str,
    fallback_plan: OrchestratorPlan,
    history: list[dict[str, str]],
    preference_hint: str = "",
) -> str:
    blocks = list_workspace_blocks()[:8]
    workspace_summary = [
        {
            "id": str(block.get("id") or ""),
            "type": str(block.get("type") or ""),
            "title": str(block.get("title") or ""),
            "source": str(block.get("source") or ""),
            "columns": [
                str(column.get("header") or column.get("key") or "")
                for column in block.get("columns") or []
                if isinstance(column, dict)
            ][:20],
        }
        for block in blocks
        if isinstance(block, dict)
    ]
    parts = [
        "Последние сообщения диалога:\n" + str(history[-12:]),
        "\nЗапрос пользователя:\n" + content[:2000],
        "\nТекущие блоки Рабочего стола:\n" + str(workspace_summary),
    ]
    if preference_hint:
        parts.append("\n" + preference_hint)
    parts.append(
        "\nFallback-план эвристики, который можно исправить:\n"
        + str(fallback_plan.model_dump(mode="json"))
    )
    parts.append(
        "\nВерни OrchestratorPlan JSON. Для изменения уже показанной таблицы "
        "укажи channel=workspace, required=true и canvas_id существующего блока, "
        "если его можно определить. "
        "Если в истории инструментов есть предпочтительные — отдай им приоритет в recommended_skills."
    )
    return "\n".join(parts)


def _normalize_model_plan(plan: OrchestratorPlan, content: str) -> OrchestratorPlan:
    text = _norm(content)
    workspace_required = plan.workspace.required or _is_workspace_request(text)
    output_type = plan.workspace.output_type
    if workspace_required and output_type == "text":
        output_type = "table" if _is_table_edit_request(text) else "document"
    canvas_id = plan.workspace.canvas_id
    recommended_skills = list(plan.worker.recommended_skills)
    if _is_supplier_grouping_request(text):
        sg = _canvas_map().get("supplier_grouping", {})
        workspace_required = True
        output_type = "table"
        canvas_id = sg.get("canvas_id", "agent:invoice-items-by-supplier")
        supplier_skill = sg.get("skill", "workspace.invoice_items_by_supplier_table")
        if supplier_skill not in recommended_skills:
            recommended_skills.insert(0, supplier_skill)
    if workspace_required and not canvas_id:
        canvas_id = _fallback_canvas_id(content)
    return plan.model_copy(
        update={
            "goal": plan.goal or content[:500],
            "worker": plan.worker.model_copy(update={"recommended_skills": recommended_skills}),
            "workspace": plan.workspace.model_copy(
                update={
                    "channel": "workspace" if workspace_required else plan.workspace.channel,
                    "required": workspace_required,
                    "output_type": output_type,
                    "canvas_id": canvas_id,
                }
            ),
        }
    )


def _workspace_updated_at_snapshot() -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for block in list_workspace_blocks():
        block_id = str(block.get("id") or "")
        updated_at = str(block.get("updated_at") or "")
        if block_id:
            snapshot[block_id] = updated_at
    return snapshot


def _registry_model_name(model_name: str | None) -> str | None:
    if not model_name:
        return None
    return model_name if model_name in ai_router.registry.models else None


def _fallback_canvas_id(content: str) -> str | None:
    text = _norm(content)
    if _references_existing_table(text):
        latest_table = _latest_workspace_table_id()
        if latest_table:
            return latest_table
    for rule in _canvas_map().get("fallback_canvas_rules", []):
        if not any(kw in text for kw in rule.get("require_any", [])):
            continue
        if rule.get("require_table_edit") and not _is_table_edit_request(text):
            continue
        if rule.get("require_extra_any") and not any(
            kw in text for kw in rule["require_extra_any"]
        ):
            continue
        for sub in rule.get("sub_rules", []):
            if any(kw in text for kw in sub.get("require_any", [])):
                return sub["canvas_id"]
        return rule["canvas_id"]
    return None


def _match_intent_route(text: str) -> dict[str, Any] | None:
    """Return first matching intent_routing entry from canvas_skill_map.json."""
    for route in _canvas_map().get("intent_routing", []):
        if any(kw in text for kw in route.get("keywords", [])):
            return route
    return None


def _resolve_canvas_from_route(route: dict[str, Any], text: str) -> str | None:
    """Walk canvas_rules in the route to determine canvas_id for the given text."""
    for rule in route.get("canvas_rules", []):
        if any(kw in text for kw in rule.get("require_any", [])):
            for sub in rule.get("sub_rules", []):
                if any(kw in text for kw in sub.get("require_any", [])):
                    return sub["canvas_id"]
            return rule["canvas_id"]
    return route.get("default_canvas")


def _is_supplier_grouping_request(text: str) -> bool:
    sg = _canvas_map().get("supplier_grouping", {})
    return any(kw in text for kw in sg.get("trigger_keywords", [])) and any(
        kw in text for kw in sg.get("item_keywords", [])
    )


def _expected_workspace_skill_for_canvas(canvas_id: str | None) -> str | None:
    return _canvas_map().get("canvas_to_skill", {}).get(canvas_id or "")


def _workspace_tool_spec_for_plan(plan: OrchestratorPlan) -> dict[str, Any] | None:
    skill = _expected_workspace_skill_for_canvas(plan.workspace.canvas_id)
    if not skill:
        return None
    spec_entry = _canvas_map().get("skill_to_spec", {}).get(skill)
    if not spec_entry:
        return None
    tool = skill.replace(".", "__")
    canvas_id = plan.workspace.canvas_id
    args: dict[str, Any] = {**spec_entry.get("default_args", {}), "canvas_id": canvas_id}
    return {
        "tool": tool,
        "path": spec_entry["path"],
        "args": args,
    }


def _build_correction_request(plan: OrchestratorPlan, audit: AuditReport) -> str:
    skill_hint = ", ".join(plan.worker.recommended_skills)
    return (
        "Исправь предыдущий результат. Аудит нашел несоответствие:\n"
        f"{'; '.join(audit.issues)}\n\n"
        "Требования оркестратора:\n"
        f"- цель: {plan.goal}\n"
        f"- канал: {plan.workspace.channel}\n"
        f"- тип вывода: {plan.workspace.output_type}\n"
        f"- canvas_id: {plan.workspace.canvas_id}\n"
        f"- используй один из рекомендованных skills: {skill_hint}\n"
        "Не отвечай только текстом, если требуется Рабочий стол. "
        "Опубликуй исправленный rich-вывод и дождись tool result."
    )


def _fallback_capability_draft(
    gap: CapabilityGapRequest,
    plan: OrchestratorPlan,
) -> CapabilityBuildDraft:
    base_name = (plan.intent or "generated_capability").replace(".", "_")
    tool_name = f"workspace.{base_name}_tool"
    return CapabilityBuildDraft(
        title=f"Draft: {gap.missing_capability}",
        tool_name=tool_name,
        endpoint_path=f"/api/workspace/agent/generated/{base_name}",
        method="POST",
        skill_registry_entry={
            "name": tool_name,
            "category": "workspace" if plan.workspace.required else "agent",
            "method": "POST",
            "path": f"/api/workspace/agent/generated/{base_name}",
            "approval_required": False,
        },
        request_schema={
            "type": "object",
            "properties": {
                "canvas_id": {"type": "string", "default": plan.workspace.canvas_id},
                "limit": {"type": "integer", "default": 5000},
            },
        },
        response_schema={
            "type": "object",
            "properties": {
                "status": {"type": "string"},
                "canvas_id": {"type": "string"},
                "message": {"type": "string"},
            },
        },
        implementation_plan=[
            "Add typed request/response model to the relevant FastAPI router.",
            "Query SQL/vector/graph data required by the user request.",
            "Build a stable Workspace block schema and upsert it to the existing Workspace.",
            "Register the tool in AiAgent registry and gateway exposed skills.",
            "Add regression tests for data correctness and workspace publication.",
        ],
        validation_plan=[
            "Verify the tool returns a canvas_id and updates the Workspace block updated_at.",
            "Run ruff, targeted pytest, and strict AiAgent contract check.",
        ],
        notes=gap.reason,
    )


def _capability_risk_level(gap: CapabilityGapRequest, audit: AuditReport) -> str:
    text = f"{gap.reason} {' '.join(audit.issues)}".lower()
    if any(marker in text for marker in ("external", "email.send", "delete", "approval")):
        return "high"
    if gap.suggested_artifact in {"script", "tool"}:
        return "medium"
    return "low"


def _format_capability_draft_markdown(
    draft: CapabilityBuildDraft,
    *,
    proposal_id: str | None = None,
) -> str:
    return "\n\n".join([
        f"# {draft.title}",
        f"Proposal ID: `{proposal_id}`" if proposal_id else "Proposal: not persisted",
        f"Tool: `{draft.tool_name}`",
        f"Endpoint: `{draft.method} {draft.endpoint_path}`",
        "## Skill registry entry\n"
        f"```json\n{json.dumps(draft.skill_registry_entry, ensure_ascii=False, indent=2)}\n```",
        "## Request schema\n"
        f"```json\n{json.dumps(draft.request_schema, ensure_ascii=False, indent=2)}\n```",
        "## Response schema\n"
        f"```json\n{json.dumps(draft.response_schema, ensure_ascii=False, indent=2)}\n```",
        "## Implementation plan\n"
        + "\n".join(f"- {item}" for item in draft.implementation_plan),
        "## Validation plan\n"
        + "\n".join(f"- {item}" for item in draft.validation_plan),
        f"## Notes\n{draft.notes}",
    ])


def _latest_workspace_table_id() -> str | None:
    for block in list_workspace_blocks():
        if block.get("type") == "table" and block.get("id"):
            return str(block["id"])
    return None


def _record_feedback_async(
    *,
    content: str,
    plan: "OrchestratorPlan",
    trace: "_TurnTrace",
    audit: "AuditReport",
    retries: int,
    duration_ms: int,
) -> None:
    """Schedule feedback recording as a background task (non-blocking)."""
    import asyncio

    # Sanitise tool names: trace stores them as "skill__name" format
    skills_used = [
        t.replace("__", ".") for t in trace.tool_calls
        if not t.startswith("_")
    ]

    feedback = TurnFeedback(
        intent_text=content[:300],
        intent_category=plan.worker.role,
        skills_planned=list(plan.worker.recommended_skills),
        skills_used=skills_used,
        audit_passed=audit.passed,
        retries=retries,
        duration_ms=duration_ms,
        errors=list(audit.issues),
    )

    try:
        loop = asyncio.get_event_loop()
        loop.run_in_executor(None, record_turn_feedback, feedback)
    except Exception:
        # Best-effort: don't block the main turn
        pass


def _event_canvas_id(event: dict[str, Any]) -> str | None:
    raw = event.get("canvas_id")
    if raw:
        return str(raw)
    block = event.get("block")
    if isinstance(block, dict) and block.get("id"):
        return str(block["id"])
    return None


def _is_workspace_request(text: str) -> bool:
    return any(
        token in text
        for token in (
            "таблиц", "полный список", "все списком", "выведи список",
            "покажи список", "ссылк", "документ", "чертеж", "чертёж",
            "график", "диаграм", "excel", "csv", "скача", "файл",
            "сравн", "отчет", "отчёт", "столбец", "столбц", "колонк",
            "добавь поле", "убери поле", "отсортируй",
        )
    )


def _is_table_edit_request(text: str) -> bool:
    return any(
        token in text
        for token in (
            "добавь столб", "добавить столб", "добавь колон", "добавить колон",
            "убери столб", "убрать столб", "убери колон", "убрать колон",
            "перед номер", "после номер", "отсортируй",
        )
    )


def _references_existing_table(text: str) -> bool:
    return any(
        token in text
        for token in (
            "уже открыт", "открытую таблиц", "открытой таблиц", "эту таблиц",
            "текущую таблиц", "в таблицу", "в нее", "в неё",
        )
    )


def _looks_like_chat_table(text: str) -> bool:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    for idx in range(len(lines) - 1):
        if "|" in lines[idx] and "|" in lines[idx + 1]:
            return True
    return False
