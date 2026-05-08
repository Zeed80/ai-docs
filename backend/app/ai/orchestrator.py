"""Department-level orchestrator for the built-in agent.

The orchestrator owns the user request lifecycle: intent routing, worker
assignment, rich-output policy, workspace verification, and post-run audit.
The existing AgentSession remains the tool-calling executor.
"""

from __future__ import annotations

import asyncio
import json
import re
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

Задача: понять цель пользователя, выбрать подходящие инструменты и исполнителя.
Исполнитель самостоятельно решит порядок вызовов и детали — не расписывай шаги.

Ключевые решения:
- workspace.required=true когда нужна таблица, список, документ, файл, график или
  изменение уже открытой таблицы. Иначе false (короткий ответ в чат).
- recommended_skills: укажи 1-3 наиболее подходящих инструмента как отправную
  точку; исполнитель может вызвать дополнительные сам.
- Если НИ ОДИН skill не подходит: intent="capability_gap".
- Роли только из enum схемы.
"""


class WorkspaceOutputSpec(BaseModel):
    channel: OutputChannel = "chat"
    output_type: OutputType = "text"
    required: bool = False
    canvas_id: str | None = None
    description: str = ""
    filters: dict[str, str] = Field(default_factory=dict)


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
    # Maps sanitized tool name → kwargs passed by the executor (for filter audit)
    tool_call_args: dict[str, dict[str, Any]] = field(default_factory=dict)
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
        # Use heuristic plan immediately — zero wait for GPU resources.
        plan = self._plan_turn(content)
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
        # Build heuristic plan as a lightweight hint (not an anchor).
        heuristic_plan = self._plan_turn(content)
        preference_hint = build_tool_preference_hint(
            intent_text=content,
            intent_category=heuristic_plan.worker.role,
            candidate_skills=list(heuristic_plan.worker.recommended_skills),
        )
        skill_context = _build_skill_registry_context(content)
        prompt = _build_orchestrator_prompt(
            content=content,
            heuristic_hint=heuristic_plan,
            history=self._history,
            preference_hint=preference_hint,
            skill_context=skill_context,
        )
        try:
            _plan_timeout = 5.0  # fast models plan in < 5s; slow ones fall to heuristic
            response = await asyncio.wait_for(
                ai_router.run(
                    AIRequest(
                        task=AITask.ORCHESTRATOR_PLANNING,
                        messages=[
                            ChatMessage(role="system", content=_ORCHESTRATOR_SYSTEM),
                            ChatMessage(role="user", content=prompt),
                        ],
                        response_schema=OrchestratorPlan,
                        confidential=False,
                        allow_cloud=True,
                        preferred_model=_registry_model_name(
                            config.orchestrator_model or config.worker_model
                        ),
                    )
                ),
                timeout=_plan_timeout,
            )
            if isinstance(response.data, OrchestratorPlan):
                plan = _normalize_model_plan(response.data, content)
                logger.info(
                    "orchestrator_plan_model_ok",
                    intent=plan.intent,
                    skills=plan.worker.recommended_skills,
                    workspace=plan.workspace.required,
                    canvas=plan.workspace.canvas_id,
                    filters=plan.workspace.filters,
                )
                return plan
        except asyncio.TimeoutError:
            logger.warning(
                "orchestrator_plan_model_timeout",
                timeout=_plan_timeout,
                model=config.orchestrator_model or config.worker_model,
            )
        except Exception as exc:
            logger.warning(
                "orchestrator_plan_model_failed",
                model=config.orchestrator_model or config.worker_model,
                error=str(exc),
            )
        return heuristic_plan

    async def _background_refine_plan(
        self, content: str, config: BuiltinAgentConfig
    ) -> None:
        """Run LLM orchestrator plan in background — result logged for future use."""
        try:
            plan = await self._plan_turn_with_model(content, config)
            logger.debug(
                "orchestrator_background_plan_ready",
                intent=plan.intent,
                skills=plan.worker.recommended_skills,
            )
        except Exception:
            pass

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
            tool_name = str(data.get("tool") or "")
            self._trace.tool_calls.append(tool_name)
            # Capture args so the auditor can verify filters were applied correctly
            raw_args = data.get("args") or data.get("input") or {}
            if isinstance(raw_args, str):
                try:
                    import json as _json
                    raw_args = _json.loads(raw_args)
                except Exception:
                    raw_args = {}
            self._trace.tool_call_args[tool_name] = dict(raw_args)
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
        """Lightweight heuristic plan — used only as a soft hint for the LLM planner."""
        text = _norm(content)
        workspace_required = _is_workspace_request(text)
        output_type: OutputType = "table" if workspace_required else "text"
        canvas_id: str | None = None

        # Broad domain detection for role hint only — not binding
        role: WorkerRole = "data_analyst"
        intent = "general"
        matched_route = _match_intent_route(text)
        if matched_route:
            role = matched_route.get("role", role)
            intent = matched_route.get("intent", intent)
            canvas_id = _resolve_canvas_from_route(matched_route, text)

        # Supplier-specific filter: carry it to the LLM as a hint in filters
        workspace_filters: dict[str, str] = {}
        supplier_name = _extract_supplier_name(text)
        if supplier_name:
            workspace_required = True
            output_type = "table"
            canvas_id = canvas_id or "agent:invoice-items"
            workspace_filters = {"supplier_query": supplier_name}

        # Pass just 1-2 broad skills as a starting hint; LLM picks the exact ones
        skills: list[str] = []
        if matched_route:
            skills = list(matched_route.get("skills", []))[:2]
        if not skills:
            skills = ["memory.search"]

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
                filters=workspace_filters,
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

        # ── Filter compliance check ────────────────────────────────────────────
        # Only enforce filter compliance when:
        #   a) the plan specifies required filters (e.g. supplier_query=X), AND
        #   b) the workspace tool that was actually called targets the SAME canvas_id
        #      as the plan (so we don't penalise the executor for choosing a
        #      semantically-equivalent but differently-shaped tool)
        if plan.workspace.filters and plan.workspace.canvas_id:
            # Find the first workspace tool result that targets the planned canvas
            matched_tool_args: dict[str, Any] = {}
            for item in self._trace.tool_results:
                result = item.get("result")
                if not isinstance(result, dict):
                    continue
                if result.get("canvas_id") == plan.workspace.canvas_id:
                    matched_tool_args = (
                        self._trace.tool_call_args.get(item.get("tool", "")) or {}
                    )
                    # Check filter presence in the result itself
                    result_filters: dict[str, Any] = result.get("filters") or {}
                    for fk, fv in plan.workspace.filters.items():
                        # Check args that were passed to the tool
                        actual = matched_tool_args.get(fk)
                        if actual is None and result_filters.get(fk) is None:
                            issues.append(
                                f"фильтр не применён: исполнитель не передал {fk}={fv!r} "
                                "в инструмент. Повтори вызов с правильными аргументами."
                            )
                        elif actual is not None and str(actual).strip().lower() != str(fv).strip().lower():
                            issues.append(
                                f"неверный фильтр: ожидалось {fk}={fv!r}, "
                                f"передано {fk}={actual!r}. Повтори с правильным значением."
                            )
                        # Cross-check reported filters in the result
                        rf = result_filters.get(fk)
                        if rf is not None and str(rf).strip().lower() != str(fv).strip().lower():
                            issues.append(
                                f"Рабочий стол показывает {fk}={rf!r} вместо {fv!r}: "
                                "показаны данные от другого запроса. Требуется перезапрос."
                            )
                    break  # only check the first matching result

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
                "фильтр не применён",
                "неверный фильтр",
                "не отфильтрованы",
                "показывает",
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
                "фильтр не применён",
                "неверный фильтр",
                "не отфильтрованы",
                "показывает",
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

        # Invoke real CapabilityBuilder to write working code immediately
        if config.allow_capability_builder:
            await self._invoke_capability_builder(gap, draft, plan, config)
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
                    # CODE_GENERATION → Claude API preferred for code tasks
                    task=AITask.CODE_GENERATION,
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
                    confidential=False,
                    allow_cloud=True,
                    preferred_model=_registry_model_name(
                        config.builder_model or config.orchestrator_model
                    ),
                )
            )
            if isinstance(response.data, CapabilityBuildDraft):
                return response.data
        except Exception as exc:
            logger.warning("capability_draft_model_failed", error=str(exc))
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

    async def _invoke_capability_builder(
        self,
        gap: CapabilityGapRequest,
        draft: CapabilityBuildDraft,
        plan: OrchestratorPlan,
        config: BuiltinAgentConfig,
    ) -> None:
        """Invoke real CapabilityBuilder to write working code and register the skill live."""
        from app.ai.capability_builder import build_capability

        gap_text = (
            f"{gap.missing_capability}. "
            f"Причина: {gap.reason}. "
            f"Запрошенный артефакт: {gap.suggested_artifact}."
        )
        skill_name = str(draft.tool_name or "").replace(".", "_") or None
        await self._outer_send({
            "type": "capability_gap.building",
            "content": "AgentDeveloper: пишу код нового скилла...",
        })
        try:
            result = await build_capability(
                gap_description=gap_text,
                skill_name=skill_name,
                context_skills=list(plan.worker.recommended_skills),
            )
            if result.ok:
                await self._outer_send({
                    "type": "capability_gap.built",
                    "content": (
                        f"AgentDeveloper: скилл **{result.skill_name}** создан и зарегистрирован. "
                        "Попробую выполнить исходный запрос с новым инструментом."
                    ),
                    "skill_name": result.skill_name,
                    "skill_path": result.skill_path,
                })
                logger.info(
                    "capability_builder_success",
                    skill=result.skill_name,
                    path=result.skill_path,
                )
            else:
                await self._outer_send({
                    "type": "capability_gap.build_failed",
                    "content": f"AgentDeveloper: не удалось создать скилл: {'; '.join(result.errors)}",
                    "errors": result.errors,
                })
        except Exception as exc:
            logger.error("capability_builder_invoke_failed", error=str(exc))
            await self._outer_send({
                "type": "capability_gap.build_failed",
                "content": f"AgentDeveloper: ошибка при создании скилла: {exc}",
            })


def _norm(text: str) -> str:
    return (text or "").lower().replace("ё", "е")


def _build_orchestrator_prompt(
    *,
    content: str,
    heuristic_hint: OrchestratorPlan,
    history: list[dict[str, str]],
    preference_hint: str = "",
    skill_context: str = "",
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
    parts: list[str] = []

    # Conversation context
    if history:
        recent = history[-6:]
        parts.append("## Контекст диалога\n" + "\n".join(
            f"{m.get('role','?')}: {str(m.get('content',''))[:200]}" for m in recent
        ))

    # Available skills grouped by domain
    if skill_context:
        parts.append("## Доступные инструменты\n" + skill_context)

    # Current workspace state (only non-empty)
    if workspace_summary:
        parts.append("## Открытые блоки Рабочего стола\n" + str(workspace_summary))

    # Adaptive hints from past outcomes
    if preference_hint:
        parts.append("## Статистика инструментов (успешность)\n" + preference_hint)

    # The actual request
    parts.append("## Запрос\n" + content[:2000])

    # Heuristic hint as soft suggestion only
    hint_dict = heuristic_hint.model_dump(mode="json")
    hint_str = str({k: hint_dict[k] for k in ("intent", "worker") if k in hint_dict})
    parts.append(f"## Эвристическая подсказка (не обязательно точная)\n{hint_str}")

    parts.append(
        "## Что нужно сделать\n"
        "Верни OrchestratorPlan JSON.\n"
        "- goal: одна фраза — что должен сделать исполнитель.\n"
        "- recommended_skills: 1-3 инструмента как отправная точка (исполнитель может взять дополнительные).\n"
        "- workspace.required=true если нужна таблица, список, файл, документ или обновление открытого блока.\n"
        "- Если НИ ОДИН инструмент не подходит: intent=capability_gap, recommended_skills=[]."
    )
    return "\n\n".join(parts)


def _build_skill_registry_context(user_text: str) -> str:
    """Return skills grouped by domain, with top relevant ones highlighted."""
    try:
        from app.ai.gateway_config import gateway_config as _gw_cfg
        registry_path = _gw_cfg.registry_path
        if not registry_path.exists():
            return ""
        import yaml as _yaml
        data = _yaml.safe_load(registry_path.read_text(encoding="utf-8")) or {}
        skills: list[dict] = data.get("tools") or data.get("skills") or []

        text_lower = user_text.lower()

        def _relevance(skill: dict) -> int:
            name = skill.get("name", "").lower()
            desc = skill.get("description", "").lower()
            score = 0
            for word in text_lower.split():
                if len(word) < 3:
                    continue
                if word in name:
                    score += 3
                elif word in desc:
                    score += 1
            return score

        # Top-12 most relevant skills shown individually with descriptions
        scored = sorted(skills, key=_relevance, reverse=True)
        top12 = scored[:12]
        top12_names = {s["name"] for s in top12}

        lines = ["### Наиболее релевантные"]
        for s in top12:
            lines.append(f"- {s['name']}: {(s.get('description') or '')[:100]}")

        # Remaining skills grouped by category (names only)
        from collections import defaultdict
        by_cat: dict[str, list[str]] = defaultdict(list)
        for s in skills:
            if s["name"] not in top12_names:
                cat = s.get("category") or "other"
                by_cat[cat].append(s["name"])

        # Generated skills always shown fully
        generated = [s for s in skills if s.get("category") == "agent_generated"
                     and s["name"] not in top12_names]
        if generated:
            lines.append("\n### Созданные агентом")
            for s in generated:
                lines.append(f"- {s['name']}: {(s.get('description') or '')[:100]}")

        if by_cat:
            lines.append("\n### Все остальные (по группам)")
            for cat, names in sorted(by_cat.items()):
                if cat == "agent_generated":
                    continue
                lines.append(f"**{cat}**: {', '.join(names)}")

        return "\n".join(lines)
    except Exception:
        return ""


def _normalize_model_plan(plan: OrchestratorPlan, content: str) -> OrchestratorPlan:
    text = _norm(content)
    workspace_required = plan.workspace.required or _is_workspace_request(text)
    output_type = plan.workspace.output_type
    if workspace_required and output_type == "text":
        output_type = "table" if _is_table_edit_request(text) else "document"
    canvas_id = plan.workspace.canvas_id
    recommended_skills = list(plan.worker.recommended_skills)
    workspace_filters = dict(plan.workspace.filters)

    # Specific-supplier filter takes priority over group-by
    supplier_name = _extract_supplier_name(text)
    if supplier_name:
        workspace_required = True
        output_type = "table"
        canvas_id = "agent:invoice-items"
        workspace_filters["supplier_query"] = supplier_name
        for skill in ("workspace.invoice_items_table", "supplier.search"):
            if skill not in recommended_skills:
                recommended_skills.insert(0, skill)
        # Remove group-by skill if it crept in
        recommended_skills = [
            s for s in recommended_skills if s != "workspace.invoice_items_by_supplier_table"
        ]
    elif _is_supplier_grouping_request(text):
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
                    "filters": workspace_filters,
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


_SUPPLIER_NAME_STOPWORDS = frozenset({
    "всех", "всем", "всеми", "всё", "все", "другим", "другие", "другого",
    "любой", "каждый", "каждого", "одного", "один", "без", "только",
    "кроме", "нескольких", "нескольким", "поставщикам", "поставщиках",
})


def _extract_supplier_name(text: str) -> str | None:
    """Extract a specific supplier name from user text.

    Detects patterns: "поставщика ХОФФМАН", "поставщику Иванов",
    "поставщик 'Ромашка'". Returns None for generic "по поставщикам".
    """
    m = re.search(
        r"поставщик[аиу]?\s+([«»\"']?[а-яёa-z][а-яёa-z0-9«»\"'\-\.]{1,}(?:\s+[а-яёa-z0-9«»\"'\-\.]{2,}){0,3}[«»\"']?)",
        text,
        re.IGNORECASE,
    )
    if m:
        name = m.group(1).strip().strip("«»\"'.,;:!?")
        if name.lower() not in _SUPPLIER_NAME_STOPWORDS and len(name) > 1:
            return name
    return None


def _is_supplier_grouping_request(text: str) -> bool:
    sg = _canvas_map().get("supplier_grouping", {})
    has_trigger = any(kw in text for kw in sg.get("trigger_keywords", []))
    has_items = any(kw in text for kw in sg.get("item_keywords", []))
    if not (has_trigger and has_items):
        return False
    # A specific supplier name makes this a filter request, not a group-by
    if _extract_supplier_name(text):
        return False
    return True


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
    if plan.workspace.filters:
        args.update(plan.workspace.filters)
    return {
        "tool": tool,
        "path": spec_entry["path"],
        "args": args,
    }


def _build_correction_request(plan: OrchestratorPlan, audit: AuditReport) -> str:
    skill_hint = ", ".join(plan.worker.recommended_skills)
    # Build a precise correction — avoid leaking audit issue text to the LLM
    # since it may contain misleading fragments (e.g. "другого запроса")
    lines = [
        "Предыдущий результат не соответствует задаче. Повтори вызов с исправлениями.",
        "",
        "Требования:",
        f"- цель: {plan.goal}",
        f"- используй один из skills: {skill_hint}",
        f"- canvas_id: {plan.workspace.canvas_id or 'auto'}",
    ]
    if plan.workspace.filters:
        filter_str = ", ".join(f"{k}={v!r}" for k, v in plan.workspace.filters.items())
        lines.append(f"- ОБЯЗАТЕЛЬНЫЕ фильтры: {filter_str}")
    if plan.workspace.required:
        lines.append("- Результат должен быть опубликован в Рабочий стол (не только текст в чат).")
    return "\n".join(lines)


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
            "ссылк", "документ", "чертеж", "чертёж",
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
