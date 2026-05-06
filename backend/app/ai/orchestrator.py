"""Department-level orchestrator for the built-in agent.

The orchestrator owns the user request lifecycle: intent routing, worker
assignment, rich-output policy, workspace verification, and post-run audit.
The existing AgentSession remains the tool-calling executor.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.ai.agent_config import BuiltinAgentConfig, get_builtin_agent_config
from app.ai.agent_loop import AgentSession
from app.ai.router import ai_router
from app.ai.schemas import AIRequest, AITask, ChatMessage
from app.domain.workspace import get_workspace_block, list_workspace_blocks

SendFn = Callable[[dict], Awaitable[None]]

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
        self._executor = AgentSession(self._send_from_executor)

    def hydrate_history(self, messages: list[dict[str, str]]) -> None:
        self._executor.hydrate_history(messages)

    async def on_approval(self, approved: bool) -> None:
        await self._executor.on_approval(approved)

    async def on_user_message(self, content: str) -> None:
        config = get_builtin_agent_config()
        if not config.department_enabled:
            await self._executor.on_user_message(content)
            return

        self._trace = _TurnTrace()
        plan = await self._plan_turn_with_model(content, config)
        self._workspace_before = _workspace_updated_at_snapshot()
        await self._announce_plan(plan)
        await self._executor.on_user_message(content)
        audit = await self._audit_turn(plan, config)
        await self._publish_audit(audit)
        if not audit.passed and self._should_report_capability_gap(plan, audit, config):
            await self._publish_capability_gap(plan, audit, config)
        await self._outer_send({"type": "done"})

    async def _plan_turn_with_model(
        self,
        content: str,
        config: BuiltinAgentConfig,
    ) -> OrchestratorPlan:
        prompt = _build_orchestrator_prompt(
            content=content,
            fallback_plan=self._plan_turn(content),
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
        except Exception:
            pass
        return self._plan_turn(content)

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
        skills: list[str] = ["memory.search", "table.query"]
        intent = "general"
        output_type: OutputType = "text"
        canvas_id: str | None = None

        if any(
            token in text
            for token in (
                "счет", "счёт", "invoice", "товар", "позици",
                "поставщик", "поставщика",
            )
        ) or _is_table_edit_request(text):
            role = "invoice_specialist"
            intent = "invoice_data"
            skills = [
                "invoice.list",
                "invoice.get",
                "workspace.invoice_table",
                "workspace.invoice_items_table",
                "workspace.invoice_items_grouped_table",
            ]
            if any(
                token in text
                for token in ("товар", "позици", "номенклатур", "материал", "столб", "колон")
            ):
                canvas_id = "agent:invoice-items"
                if "групп" in text or ("поставщик" in text and _is_table_edit_request(text)):
                    canvas_id = "agent:invoice-items-grouped"
            else:
                canvas_id = "agent:invoice-list"
        elif any(token in text for token in ("склад", "остат", "тмц", "фрез")):
            role = "warehouse_specialist"
            intent = "warehouse"
            skills = ["warehouse.list_inventory", "warehouse.get_item", "canvas.publish"]
            canvas_id = "agent:warehouse-list"
        elif any(token in text for token in ("письм", "почт", "email")):
            role = "procurement_specialist"
            intent = "email"
            skills = ["email.search", "email.get_thread", "email.draft", "email.send"]
        elif any(token in text for token in ("чертеж", "чертёж", "технолог", "операци")):
            role = "engineer"
            intent = "engineering"
            skills = ["doc.get", "tech.process_plan_draft_from_document", "canvas.publish"]
        elif any(token in text for token in ("памят", "найди", "поиск", "источник")):
            role = "memory_researcher"
            intent = "memory_search"
            skills = ["memory.search", "memory.explain", "doc.search"]

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
        return plan.workspace.required and not audit.workspace_verified

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
                "Оркестратор: обнаружил недостающую возможность. "
                "Подготовлю проект инструмента/скилла только после подтверждения."
            )
            if config.capability_builder_requires_approval
            else "Оркестратор: обнаружил недостающую возможность и подготовлю draft.",
            "gap": gap.model_dump(mode="json"),
        })


def _norm(text: str) -> str:
    return (text or "").lower().replace("ё", "е")


def _build_orchestrator_prompt(
    *,
    content: str,
    fallback_plan: OrchestratorPlan,
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
    return (
        "Запрос пользователя:\n"
        f"{content[:2000]}\n\n"
        "Текущие блоки Рабочего стола:\n"
        f"{workspace_summary}\n\n"
        "Fallback-план эвристики, который можно исправить:\n"
        f"{fallback_plan.model_dump(mode='json')}\n\n"
        "Верни OrchestratorPlan JSON. Для изменения уже показанной таблицы "
        "укажи channel=workspace, required=true и canvas_id существующего блока, "
        "если его можно определить."
    )


def _normalize_model_plan(plan: OrchestratorPlan, content: str) -> OrchestratorPlan:
    text = _norm(content)
    workspace_required = plan.workspace.required or _is_workspace_request(text)
    output_type = plan.workspace.output_type
    if workspace_required and output_type == "text":
        output_type = "table" if _is_table_edit_request(text) else "document"
    canvas_id = plan.workspace.canvas_id
    if workspace_required and not canvas_id:
        canvas_id = _fallback_canvas_id(content)
    return plan.model_copy(
        update={
            "goal": plan.goal or content[:500],
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
    if any(token in text for token in ("товар", "позици", "номенклатур", "материал")):
        return "agent:invoice-items-grouped" if "групп" in text else "agent:invoice-items"
    if _is_table_edit_request(text) and any(token in text for token in ("поставщик", "счет")):
        return "agent:invoice-items-grouped"
    if any(token in text for token in ("счет", "счёт", "invoice")):
        return "agent:invoice-list"
    if any(token in text for token in ("склад", "остат", "тмц")):
        return "agent:warehouse-list"
    return None


def _latest_workspace_table_id() -> str | None:
    for block in list_workspace_blocks():
        if block.get("type") == "table" and block.get("id"):
            return str(block["id"])
    return None


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
