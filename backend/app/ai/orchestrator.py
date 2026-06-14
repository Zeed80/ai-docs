"""Department-level orchestrator for the built-in agent.

The orchestrator owns the user request lifecycle: intent routing, worker
assignment, rich-output policy, workspace verification, and post-run audit.
The existing AgentSession remains the tool-calling executor.
"""

from __future__ import annotations

import asyncio
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


def _agent_headers() -> dict:
    """X-API-Key for internal orchestrator → backend service calls."""
    from app.config import settings
    if settings.agent_service_key:
        return {"X-API-Key": settings.agent_service_key}
    return {}

from app.ai.agent_config import BuiltinAgentConfig, get_builtin_agent_config
from app.ai.agent_loop import AgentSession
from app.ai.audit import (
    CAPABILITY_GAP_CODES,
    AuditCode,
    AuditIssue,
    blocking as _blocking_issues,
    codes as _issue_codes,
    has_code as _has_code,
    messages as _issue_messages,
    retryable as _issues_retryable,
)
from app.ai.degradation import log_degraded
from app.ai.flow_awareness import format_flow_summary_human, get_flow_snapshot
from app.ai.model_tier import (
    Tier,
    aux_quality_budget,
    has_high_complexity_signal,
    inject_chain_of_draft,
    score_complexity,
    should_use_cod,
)
from app.ai.orchestrator_memory import TurnFeedback, build_tool_preference_hint, record_turn_feedback
from app.ai.policy_engine import check_tool_execution
from app.ai import route_table
from app.ai.router import ai_router
from app.ai.schemas import AIRequest, AITask, ChatMessage
from app.domain.workspace import get_workspace_block, list_workspace_blocks, upsert_workspace_block

SendFn = Callable[[dict], Awaitable[None]]


def invalidate_canvas_map_cache() -> None:
    """Force reload of routes.yml on next access (called on Redis skill_reload event)."""
    route_table.invalidate_cache()


def _response_budget_for(tier: "Tier", plan: "OrchestratorPlan") -> int:
    """Per-turn response token budget from task complexity and output shape.

    Short chat answers stay cheap (fast on local models); reports/tables/documents
    and complex reasoning get more room. Replaces the old hardcoded 4096.
    """
    output_type = plan.workspace.output_type
    if output_type in ("table", "document", "chart"):
        return 8192
    if tier >= Tier.LARGE:
        return 8192
    if tier >= Tier.MEDIUM:
        return 4096
    if tier <= Tier.MICRO:
        return 1024
    return 2048


# role → (mtime, text) — avoids re-reading role-*.md from disk on every turn.
_role_prompt_cache: dict[str, tuple[float, str]] = {}


def _load_role_prompt(role: str) -> str:
    """Return the role-specific prompt text with mtime-based caching.

    Returns "" when the role has no prompt file (e.g. builder roles not defined
    in gateway.yml) — the executor then runs with the base system prompt only.
    """
    from app.ai.gateway_config import gateway_config
    try:
        path = gateway_config.role_prompt_path(role)
        if not path or not path.exists():
            return ""
        mtime = path.stat().st_mtime
        cached = _role_prompt_cache.get(role)
        if cached is None or cached[0] != mtime:
            text = path.read_text(encoding="utf-8").strip()
            _role_prompt_cache[role] = (mtime, text)
            return text
        return cached[1]
    except Exception as exc:
        log_degraded("orchestrator.role_prompt", exc, role=role)
        return ""

# Specialist workers the secretary front-agent can dispatch to. Each role is
# declared in gateway.yml (prompt + capability allowlist). The secretary itself
# is NOT a worker: flow-status questions are answered by the orchestrator
# directly (see _answer_flow_status_directly). Builder is not a chat role —
# capability drafting runs through the proposal flow with builder_model.
WorkerRole = Literal[
    "data_analyst",
    "invoice_specialist",
    "warehouse_specialist",
    "procurement_specialist",
    "accountant",
    "engineer",
    "technologist",
    "memory_researcher",
]

OutputChannel = Literal["chat", "workspace"]
OutputType = Literal["text", "table", "document", "links", "chart", "drawing", "script"]

_ORCHESTRATOR_SYSTEM_BASE = """Ты — Света, секретарь-оркестратор отдела ИИ-сотрудников.
Ты держишь документооборот под контролем и распределяешь задачи специалистам.
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


def _orchestrator_system() -> str:
    """System prompt for the planner: static base + domain sections from routes.yml."""
    sections = route_table.prompt_sections()
    if sections:
        return f"{_ORCHESTRATOR_SYSTEM_BASE}\n{sections}\n"
    return _ORCHESTRATOR_SYSTEM_BASE


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
    issues: list[AuditIssue] = Field(default_factory=list)
    workspace_verified: bool = False
    final_channel: OutputChannel = "chat"
    # Semantic correctness signal — advisory, does not flip `passed`. Consumed by
    # the learning loop and surfaced to the user as a soft quality warning.
    # None = no verdict (audit not run or infra failure) — distinct from True so
    # the learning loop is not success-biased by flaky infrastructure.
    semantic_passed: bool | None = None
    semantic_reason: str = ""

    @property
    def issue_messages(self) -> list[str]:
        return _issue_messages(self.issues)

    @property
    def issue_codes(self) -> list[str]:
        return _issue_codes(self.issues)


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
    # Ordered (tool, args) pairs — preserves repeated calls for recipe recording
    # and per-step credit assignment.
    tool_call_seq: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    # Maps sanitized tool name → kwargs passed by the executor (for filter audit)
    tool_call_args: dict[str, dict[str, Any]] = field(default_factory=dict)
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    text_chunks: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    saw_done: bool = False
    parallel_used: bool = False

    @property
    def final_text(self) -> str:
        return "".join(self.text_chunks).strip()


class AgentOrchestrator:
    """Controller above a concrete AgentSession executor."""

    def __init__(self, send: SendFn) -> None:
        self._outer_send = send
        self._trace = _TurnTrace()
        self._workspace_before: dict[str, str] = {}
        self._plan_source: str = "heuristic"
        self._tier: Tier = Tier.NANO
        # Per-turn LLM call accounting. Lives on the instance (not the trace)
        # because the trace is reset on every retry/repair within a turn.
        # _llm_calls — telemetry of all orchestrator-visible LLM calls;
        # _aux_llm_calls — budgeted quality calls (semantic audit, refine).
        self._llm_calls: int = 0
        self._aux_llm_calls: int = 0
        self._executor = AgentSession(self._send_from_executor)

    def hydrate_history(self, messages: list[dict[str, str]]) -> None:
        # The executor owns the dialogue history (compression-aware); the planner
        # reads it back via recent_dialogue() so the two never drift apart.
        self._executor.hydrate_history(messages)

    def _recent_dialogue(self) -> list[dict[str, str]]:
        return self._executor.recent_dialogue(limit=20)

    async def on_approval(self, approved: bool) -> None:
        await self._executor.on_approval(approved)

    async def on_user_message(
        self, content: str, reasoning_mode: str = "normal"
    ) -> None:
        config = get_builtin_agent_config()
        if not config.department_enabled:
            # No department planning → clear any stale per-turn overrides.
            self._executor.set_role_context("")
            self._executor.set_response_budget(2048)
            self._executor.set_model_override(None)
            self._executor.set_active_role(None)
            await self._executor.on_user_message(content)
            return

        turn_started_at = time.time()
        self._trace = _TurnTrace()
        self._llm_calls = 0
        self._aux_llm_calls = 0

        # Score complexity → determines planning timeout and CoD injection
        history = self._recent_dialogue()
        context_tokens = sum(len(m.get("content", "")) // 4 for m in history)
        tier = score_complexity(content, context_tokens=context_tokens)
        self._tier = tier

        # Secretary direct path: flow-status questions are answered by the
        # front-agent itself from live data — no planning, no dispatch, 0 LLM.
        if _is_secretary_query(content):
            handled = await self._answer_flow_status_directly(content, config, turn_started_at)
            if handled:
                return

        # Spec-table edits: a recognised Russian edit command on an existing
        # spec table («добавь столбец с НДС перед суммой», «отсортируй по…»,
        # «покажи только…») is applied deterministically — 0 LLM, мгновенно.
        if await self._try_spec_table_patch_directly(content, config, turn_started_at):
            return

        # Heuristic-first for workspace tables: if a cheap heuristic plan already
        # resolves a self-sufficient canvas, execute it deterministically with
        # NO planner/worker LLM. Placed BEFORE the recipe lookup so common
        # "покажи таблицу/аналитику" turns skip the recipe embedding round-trip
        # entirely (that embedding competes with APEX for VRAM → 2-7s stalls).
        if reasoning_mode != "strict" and not has_high_complexity_signal(content):
            heuristic_plan = self._plan_turn(content)
            # Skipped only for explicit deep-reasoning verbs ("сравни",
            # "проанализируй", "построй план"…) — those need real worker
            # reasoning, not a short-circuit to a plain table. Analytical
            # pivots ("популярнее", "больше всего") still fire here: stacked
            # MEDIUM words shouldn't disqualify a deterministic pivot.
            if heuristic_plan.workspace.canvas_id in self._PROACTIVE_SAFE_CANVASES:
                self._plan_source = "proactive_workspace"
                plan = heuristic_plan
                self._workspace_before = _workspace_updated_at_snapshot()
                await self._announce_plan(plan)
                if await self._try_proactive_workspace_execution(plan, config):
                    audit = await self._audit_turn(plan, config)
                    await self._publish_audit(audit)
                    _record_feedback_async(
                        content=content, plan=plan, trace=self._trace, audit=audit,
                        retries=0, duration_ms=int((time.time() - turn_started_at) * 1000),
                    )
                    duration_ms = int((time.time() - turn_started_at) * 1000)
                    logger.info(
                        "agent_turn_complete",
                        intent=plan.intent, reasoning_mode=reasoning_mode,
                        plan_source="proactive_workspace",
                        tools_called=self._trace.tool_calls,
                        tool_count=len(self._trace.tool_calls),
                        parallel_used=self._trace.parallel_used,
                        errors=self._trace.errors,
                        workspace_required=plan.workspace.required,
                        audit_passed=audit.passed, audit_issues=audit.issue_codes,
                        retries=0, llm_calls=0, aux_llm_calls=self._aux_llm_calls,
                        duration_ms=duration_ms,
                    )
                    try:
                        from app.core.metrics import (
                            agent_turns_total, agent_turn_duration_seconds,
                            agent_tool_calls_total,
                        )
                        agent_turns_total.labels(
                            outcome="success" if audit.passed else "audit_failed"
                        ).inc()
                        agent_turn_duration_seconds.observe(duration_ms / 1000)
                        for tool in self._trace.tool_calls:
                            agent_tool_calls_total.labels(tool=tool).inc()
                    except Exception:
                        pass
                    chips = self._derive_action_chips(plan, content)
                    await self._outer_send({"type": "done", "action_chips": chips})
                    return

        # Learned recipes: a high-similarity ACTIVE recipe with resolvable slots
        # is replayed deterministically (0 planner LLM calls); a weaker match
        # becomes a planner/worker hint. Gated to tool-shaped turns so smalltalk
        # never pays the embedding round-trip.
        recipe_hint = ""
        if route_table.is_workspace_request(content) or tier >= Tier.SMALL:
            from app.ai import recipes as recipes_module
            recipe_hit = await recipes_module.find_recipe(content)
            if recipe_hit is not None:
                recipe, score = recipe_hit
                if (
                    score >= recipes_module.REPLAY_SCORE
                    and recipe.status == "active"
                ):
                    slots = recipes_module.resolve_slots(recipe.param_slots, content)
                    if slots is not None and await self._replay_recipe(
                        recipe, slots, content, config, turn_started_at
                    ):
                        return
                if score >= recipes_module.HINT_SCORE:
                    steps_text = " → ".join(
                        f"{s.get('capability')}.{s.get('action') or 'call'}"
                        for s in (recipe.steps or [])
                    )
                    recipe_hint = (
                        f"Похожая задача уже решалась успешно шагами: {steps_text}. "
                        "Используй эту последовательность как отправную точку."
                    )

        # LLM planning for anything beyond NANO/MICRO: SMALL+ uses the model.
        # Heuristic is reserved for trivial look-ups only (count fast-paths
        # are already handled above via on_user_message direct answers).
        if reasoning_mode == "strict" or tier >= Tier.SMALL:
            self._plan_source = "model"
            plan = await self._plan_turn_with_model(content, config)
        else:
            self._plan_source = "heuristic"
            plan = self._plan_turn(content)
        self._workspace_before = _workspace_updated_at_snapshot()
        await self._announce_plan(plan)

        # Inject Chain-of-Draft hint for medium/complex tasks on local models
        hint = _build_worker_hint(plan)
        if recipe_hint:
            hint = f"{hint}\n{recipe_hint}"
        worker_model = config.worker_model or ""
        if reasoning_mode == "strict" or should_use_cod(tier, worker_model):
            hint = inject_chain_of_draft(hint)
        self._executor.inject_orchestrator_hint(hint)
        # Load the role-specific system prompt so the worker actually adopts the
        # assigned role (accountant vs technologist, ...). Replaced per turn —
        # never accumulated in history.
        role_context = _load_role_prompt(plan.worker.role)
        self._executor.set_role_context(role_context)
        # Scope the visible tool set to the role's capability allowlist.
        self._executor.set_active_role(plan.worker.role)
        # Size the response budget to the task: cheap/fast for short answers,
        # roomy for reports/tables. Avoids the old hardcoded 4096 on every turn.
        self._executor.set_response_budget(_response_budget_for(tier, plan))
        # Tier-based model routing: simple turns → fast small model (if configured),
        # complex turns → the configured worker/model. No fast_model → no change.
        self._executor.set_model_override(
            config.fast_model if (config.fast_model and tier < Tier.MEDIUM) else None
        )
        self._llm_calls += 1
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
                "issue_codes": audit.issue_codes,
            })
            self._trace = _TurnTrace()
            self._workspace_before = _workspace_updated_at_snapshot()
            self._llm_calls += 1
            await self._executor.on_user_message(_build_correction_request(plan, audit))
            audit = await self._audit_turn(plan, config)
        if not audit.passed:
            repaired = await self._try_execute_planned_workspace_tool(plan, audit, config)
            if repaired:
                audit = await self._audit_turn(plan, config)
        # Semantic correctness check on the settled answer (advisory; runs once).
        await self._run_semantic_audit(plan, config, audit)
        # Reactive self-refine: only when the auditor flagged a generative answer.
        await self._maybe_refine_answer(plan, config, audit)
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
        # Self-learning: a clean multi-step turn becomes a draft recipe.
        self._maybe_record_recipe(content, plan, audit)

        # Structured agent trace log — every turn execution logged with full detail
        duration_ms = int((time.time() - turn_started_at) * 1000)
        logger.info(
            "agent_turn_complete",
            intent=plan.intent,
            reasoning_mode=reasoning_mode,
            plan_source=self._plan_source,
            tools_called=self._trace.tool_calls,
            tool_count=len(self._trace.tool_calls),
            parallel_used=self._trace.parallel_used,
            errors=self._trace.errors,
            workspace_required=plan.workspace.required,
            audit_passed=audit.passed,
            audit_issues=audit.issue_codes,
            retries=retry_count,
            llm_calls=self._llm_calls,
            aux_llm_calls=self._aux_llm_calls,
            duration_ms=duration_ms,
        )
        try:
            from app.core.metrics import agent_turns_total, agent_turn_duration_seconds, agent_tool_calls_total
            outcome = "success" if audit.passed else "audit_failed"
            agent_turns_total.labels(outcome=outcome).inc()
            agent_turn_duration_seconds.observe(duration_ms / 1000)
            for tool in self._trace.tool_calls:
                agent_tool_calls_total.labels(tool=tool).inc()
        except Exception:
            pass

        # No separate history to maintain — the executor records this turn in its
        # own (compression-aware) message list, which _recent_dialogue() reads back.
        chips = self._derive_action_chips(plan, content)
        await self._outer_send({"type": "done", "action_chips": chips})

    async def _answer_flow_status_directly(
        self,
        content: str,
        config: BuiltinAgentConfig,
        turn_started_at: float,
    ) -> bool:
        """Secretary front-agent: answer a flow-status question from live data.

        Deterministic (0 LLM calls): fetches the cached dashboard snapshot and
        formats a prioritised summary. Returns False when the snapshot is
        unavailable — the turn then falls through to normal dispatch so a
        specialist can fetch the data with tools.
        """
        try:
            snapshot = await get_flow_snapshot(config)
        except Exception as exc:
            log_degraded("orchestrator.flow_snapshot", exc)
            snapshot = None
        if not snapshot:
            return False

        await self._outer_send({
            "type": "orchestrator.status",
            "content": "Секретарь: отвечаю по состоянию документооборота.",
            "plan_source": "direct",
            "degraded": False,
        })
        # UI continuity: the frontend renders worker.assigned as a status line.
        await self._outer_send({
            "type": "worker.assigned",
            "content": "Исполнитель: secretary (прямой ответ, без LLM).",
            "role": "secretary",
            "skills": [],
        })
        answer = format_flow_summary_human(snapshot)
        await self._outer_send({"type": "text", "content": answer})
        # Keep dialogue history and episodic memory coherent.
        try:
            self._executor.record_external_turn(content, answer)
        except Exception as exc:
            log_degraded("orchestrator.flow_record_turn", exc)

        duration_ms = int((time.time() - turn_started_at) * 1000)
        logger.info(
            "agent_turn_complete",
            intent="flow_status",
            plan_source="direct",
            tools_called=[],
            tool_count=0,
            audit_passed=True,
            retries=0,
            llm_calls=0,
            aux_llm_calls=0,
            duration_ms=duration_ms,
        )
        try:
            from app.core.metrics import agent_turn_duration_seconds, agent_turns_total
            agent_turns_total.labels(outcome="success").inc()
            agent_turn_duration_seconds.observe(duration_ms / 1000)
        except Exception:
            pass
        chips = route_table.chips_for("flow_status", content)
        await self._outer_send({"type": "done", "action_chips": chips})
        return True

    async def _try_spec_table_patch_directly(
        self,
        content: str,
        config: BuiltinAgentConfig,
        turn_started_at: float,
    ) -> bool:
        """Apply a recognised table-edit command to the latest spec table (0 LLM).

        Returns False when there is no spec table on the workspace or the
        command is not deterministically recognisable — the turn then goes
        through normal dispatch (the worker LLM can still build patch ops).
        """
        from app.domain.table_spec import TableSpec, parse_patch_command

        blocks = [
            b for b in list_workspace_blocks()
            if isinstance(b, dict) and isinstance(b.get("spec"), dict)
        ]
        if not blocks:
            return False
        block = max(blocks, key=lambda b: str(b.get("updated_at") or ""))
        canvas_id = str(block.get("id") or "")
        try:
            spec = TableSpec.model_validate(block["spec"])
        except Exception:
            return False
        parsed = parse_patch_command(content, spec)
        if parsed is None:
            return False

        self._workspace_before = _workspace_updated_at_snapshot()
        await self._outer_send({
            "type": "orchestrator.status",
            "content": "Секретарь: правка таблицы распознана — применяю мгновенно (без LLM).",
            "plan_source": "table_patch",
            "degraded": False,
        })
        args = {
            "canvas_id": canvas_id,
            "ops": [op.model_dump(mode="json", exclude_none=True) for op in parsed.ops],
        }
        await self._record_orchestrator_tool_event({
            "type": "tool_call", "tool": "workspace", "args": args,
        })
        try:
            async with httpx.AsyncClient(timeout=float(config.backend_timeout_seconds)) as client:
                resp = await client.post(
                    f"{config.backend_url.rstrip('/')}/api/workspace/agent/spec-table/patch",
                    json=args,
                    headers=_agent_headers(),
                )
            result = resp.json() if resp.content else {}
        except Exception as exc:
            log_degraded("orchestrator.spec_table_patch", exc)
            return False
        if resp.status_code >= 400 or result.get("status") not in ("published",):
            await self._record_orchestrator_tool_event({
                "type": "tool_result", "tool": "workspace",
                "result": {"error": result.get("message") or f"HTTP {resp.status_code}"},
            })
            return False  # fall through to normal dispatch

        await self._record_orchestrator_tool_event({
            "type": "tool_result", "tool": "workspace", "result": result,
        })
        answer = str(result.get("message") or "Готово.")
        await self._outer_send({"type": "text", "content": answer})
        try:
            self._executor.record_external_turn(content, answer)
        except Exception as exc:
            log_degraded("orchestrator.table_patch_record_turn", exc)

        duration_ms = int((time.time() - turn_started_at) * 1000)
        logger.info(
            "agent_turn_complete",
            intent="table_patch",
            plan_source="table_patch",
            tools_called=["workspace"],
            tool_count=1,
            audit_passed=True,
            retries=0,
            llm_calls=0,
            aux_llm_calls=0,
            duration_ms=duration_ms,
        )
        try:
            from app.core.metrics import agent_turn_duration_seconds, agent_turns_total
            agent_turns_total.labels(outcome="success").inc()
            agent_turn_duration_seconds.observe(duration_ms / 1000)
        except Exception:
            pass
        chips = route_table.chips_for("table_patch", content, workspace_required=True)
        await self._outer_send({"type": "done", "action_chips": chips})
        return True

    async def _replay_recipe(
        self,
        recipe: Any,
        slots: dict[str, str],
        content: str,
        config: BuiltinAgentConfig,
        turn_started_at: float,
    ) -> bool:
        """Deterministically replay a learned recipe (0 planner LLM calls).

        Returns False on any step failure — the turn then falls through to
        normal dispatch, and the recipe's fail counter is already updated.
        """
        from app.ai import recipes as recipes_module

        self._workspace_before = _workspace_updated_at_snapshot()
        await self._outer_send({
            "type": "orchestrator.status",
            "content": f"Секретарь: задача знакома — выполняю выученный рецепт «{recipe.name}».",
            "plan_source": "recipe",
            "degraded": False,
            "recipe_id": str(recipe.id),
        })
        await self._outer_send({
            "type": "worker.assigned",
            "content": f"Исполнитель: {recipe.role} (replay рецепта, без LLM-планирования).",
            "role": recipe.role,
            "skills": [
                f"{s.get('capability')}" for s in (recipe.steps or [])
            ][:5],
        })

        ok = await recipes_module.replay(
            recipe, slots, config, on_event=self._send_from_executor
        )
        if not ok:
            await self._outer_send({
                "type": "orchestrator.status",
                "content": "Секретарь: рецепт не сработал, решаю задачу обычным путём.",
                "plan_source": "recipe_fallback",
                "degraded": True,
            })
            return False

        answer = "Готово — выполнено по выученному рецепту."
        last_result = next(
            (
                item.get("result")
                for item in reversed(self._trace.tool_results)
                if isinstance(item.get("result"), dict)
            ),
            None,
        )
        if isinstance(last_result, dict) and last_result.get("message"):
            answer = str(last_result["message"])
        await self._outer_send({"type": "text", "content": answer})
        try:
            self._executor.record_external_turn(content, answer)
        except Exception as exc:
            log_degraded("orchestrator.recipe_record_turn", exc)

        duration_ms = int((time.time() - turn_started_at) * 1000)
        logger.info(
            "agent_turn_complete",
            intent="recipe_replay",
            plan_source="recipe",
            recipe_id=str(recipe.id),
            tools_called=self._trace.tool_calls,
            tool_count=len(self._trace.tool_calls),
            audit_passed=True,
            retries=0,
            llm_calls=0,
            aux_llm_calls=0,
            duration_ms=duration_ms,
        )
        try:
            from app.core.metrics import agent_turn_duration_seconds, agent_turns_total
            agent_turns_total.labels(outcome="success").inc()
            agent_turn_duration_seconds.observe(duration_ms / 1000)
        except Exception:
            pass
        chips = route_table.chips_for("recipe_replay", content, workspace_required=True)
        await self._outer_send({"type": "done", "action_chips": chips})
        return True

    def _maybe_record_recipe(self, content: str, plan: OrchestratorPlan, audit: AuditReport) -> None:
        """Schedule recording of a successful turn as a draft recipe.

        Criteria: mechanical audit passed, no explicit semantic failure,
        2–6 tool calls, all from the capability dispatcher (plain names),
        no approval-gated actions (checked inside the recorder).
        """
        if not audit.passed or audit.semantic_passed is False:
            return
        seq = list(self._trace.tool_call_seq)
        if not (2 <= len(seq) <= 6):
            return
        # Only plain capability-dispatcher calls compose into recipes.
        if any("__" in name or "." in name for name, _ in seq):
            return
        steps = [
            {
                "capability": name,
                "action": str(args.get("action") or ""),
                "args_template": dict(args),
            }
            for name, args in seq
        ]
        from app.ai import recipes as recipes_module

        async def _record() -> None:
            try:
                await recipes_module.record_candidate(
                    user_text=content,
                    role=plan.worker.role,
                    intent=plan.intent,
                    steps=steps,
                )
            except Exception as exc:
                log_degraded("orchestrator.recipe_record", exc)

        try:
            asyncio.get_event_loop().create_task(_record())
        except Exception as exc:
            log_degraded("orchestrator.recipe_record", exc)

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
            history=self._recent_dialogue(),
            preference_hint=preference_hint,
            skill_context=skill_context,
        )
        _plan_timeout = float(config.orchestrator_plan_timeout_seconds)
        fallback_reason = "invalid_schema"
        self._llm_calls += 1
        try:
            response = await asyncio.wait_for(
                ai_router.run(
                    AIRequest(
                        task=AITask.ORCHESTRATOR_PLANNING,
                        messages=[
                            ChatMessage(role="system", content=_orchestrator_system()),
                            ChatMessage(role="user", content=prompt),
                        ],
                        response_schema=OrchestratorPlan,
                        confidential=False,
                        allow_cloud=True,
                        preferred_model=_registry_model_name(
                            config.orchestrator_model
                            or config.worker_model
                            or config.model
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
            fallback_reason = "timeout"
            logger.warning(
                "orchestrator_plan_model_timeout",
                timeout=_plan_timeout,
                model=config.orchestrator_model or config.worker_model,
            )
        except Exception as exc:
            fallback_reason = "error"
            logger.warning(
                "orchestrator_plan_model_failed",
                model=config.orchestrator_model or config.worker_model,
                error=str(exc),
            )

        # Reached only on timeout / error / invalid schema — degrade to heuristic.
        self._plan_source = "heuristic"
        try:
            from app.core.metrics import orchestrator_plan_fallback_total
            orchestrator_plan_fallback_total.labels(reason=fallback_reason).inc()
        except Exception:
            pass
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
        except Exception as exc:
            log_degraded("orchestrator.background_plan", exc)

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
            self._trace.tool_call_seq.append((tool_name, dict(raw_args)))
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
        elif msg_type == "tools.parallel":
            self._trace.parallel_used = True
            return  # internal observability marker — don't forward to the client
        await self._outer_send(data)

    def _derive_action_chips(
        self, plan: OrchestratorPlan, content: str
    ) -> list[dict]:
        """Return contextual action chips based on the completed turn's plan."""
        return route_table.chips_for(
            plan.intent, content, workspace_required=plan.workspace.required
        )

    def _plan_turn(self, content: str) -> OrchestratorPlan:
        """Lightweight heuristic plan — used only as a soft hint for the LLM planner."""
        text = _norm(content)
        workspace_required = _is_workspace_request(text)
        output_type: OutputType = "table" if workspace_required else "text"
        canvas_id: str | None = None

        # Broad domain detection for role hint only — not binding.
        # Flow-status (secretary) questions never reach this point: the
        # front-agent answers them directly in on_user_message.
        role: WorkerRole = "data_analyst"
        intent = "general"
        matched_route = _match_intent_route(text)
        if matched_route:
            role = matched_route.get("role", role)
            intent = matched_route.get("intent", intent)
            canvas_id = _resolve_canvas_from_route(matched_route, text)
            # If the route declares workspace_required or resolves a canvas_id,
            # mark this as a workspace request so the LLM hint is correct.
            if matched_route.get("workspace_required") or canvas_id:
                workspace_required = True
                output_type = "table"

        # References to an already open table take priority over grouping
        if not canvas_id and _references_existing_table(text):
            canvas_id = _latest_workspace_table_id()

        # Supplier grouping: "сгруппируй по поставщикам" → dedicated canvas
        if not canvas_id and _is_supplier_grouping_request(text):
            sg = route_table.supplier_grouping()
            workspace_required = True
            output_type = "table"
            canvas_id = sg.get("canvas_id", "agent:invoice-items-by-supplier")

        # Supplier-specific filter: carry it to the LLM as a hint in filters
        workspace_filters: dict[str, str] = {}
        supplier_name = _extract_supplier_name(text)
        if supplier_name:
            workspace_required = True
            output_type = "table"
            canvas_id = canvas_id or "agent:invoice-items"
            workspace_filters = {"supplier_query": supplier_name}

        # Resolve canvas_id from workspace state or JSON fallback rules if still unset
        if workspace_required and not canvas_id:
            canvas_id = _fallback_canvas_id(content)

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
        degraded = self._plan_source != "model"
        status_text = (
            f"Оркестратор: понял задачу, назначаю роль {plan.worker.role}."
            if not degraded
            else f"Оркестратор (упрощённый режим): назначаю роль {plan.worker.role}."
        )
        await self._outer_send({
            "type": "orchestrator.status",
            "content": status_text,
            "plan": plan.model_dump(mode="json"),
            "plan_source": self._plan_source,
            "degraded": degraded,
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

        issues: list[AuditIssue] = []
        workspace_verified = False
        if plan.workspace.required:
            workspace_verified = await self._verify_workspace(plan)
            if not workspace_verified:
                issues.append(AuditIssue(
                    code=AuditCode.WORKSPACE_NOT_PUBLISHED,
                    message="Запрошен rich-вывод, но публикация на Рабочий стол не подтверждена.",
                ))
            if _looks_like_chat_table(self._trace.final_text):
                issues.append(AuditIssue(
                    code=AuditCode.CHAT_TABLE_LEAK,
                    message="Табличный результат попал в чат вместо Рабочего стола.",
                ))
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
                # Advisory: the planned canvas is a heuristic guess. A verified
                # publication to another canvas is a fine answer — re-publishing
                # the «правильный» блок duplicates the table the user already
                # sees. Recorded for the learning loop only.
                issues.append(AuditIssue(
                    code=AuditCode.WRONG_CANVAS,
                    severity="advisory",
                    message=(
                        "Опубликован другой workspace-блок: план предлагал "
                        f"{expected_canvas}, опубликовано {sorted(published_canvas_ids)}."
                    ),
                    context={
                        "expected": expected_canvas,
                        "published": sorted(published_canvas_ids),
                    },
                ))

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
            # Advisory: the plan is a hint, not ground truth — a semantically
            # equivalent tool choice must not fail the turn by itself. The
            # workspace/filter checks above catch actually-wrong results.
            issues.append(AuditIssue(
                code=AuditCode.TOOL_OFF_PLAN,
                severity="advisory",
                message=(
                    "Исполнитель выбрал инструмент вне плана: "
                    f"ожидались {sorted(expected_workspace_skills)}, "
                    f"использованы {sorted(used_workspace_skills)}."
                ),
                context={
                    "expected": sorted(expected_workspace_skills),
                    "used": sorted(used_workspace_skills),
                },
            ))

        # ── Filter compliance check ────────────────────────────────────────────
        # Only enforce filter compliance when:
        #   a) the plan specifies required filters (e.g. supplier_query=X), AND
        #   b) the workspace tool that was actually called targets the SAME canvas_id
        #      as the plan (so we don't penalise the executor for choosing a
        #      semantically-equivalent but differently-shaped tool)
        if plan.workspace.filters and plan.workspace.canvas_id:
            # Audit the LAST tool result that targets the planned canvas — that
            # publish defines what the user sees. (The old code checked only the
            # FIRST match, so a turn that started wrong and self-corrected was
            # punished, and one that started right and then overwrote the canvas
            # with unfiltered data passed.)
            last_match: dict[str, Any] | None = None
            for item in self._trace.tool_results:
                result = item.get("result")
                if isinstance(result, dict) and result.get("canvas_id") == plan.workspace.canvas_id:
                    last_match = item
            if last_match is not None:
                result = last_match["result"]
                matched_tool_args = (
                    self._trace.tool_call_args.get(last_match.get("tool", "")) or {}
                )
                result_filters: dict[str, Any] = result.get("filters") or {}
                for fk, fv in plan.workspace.filters.items():
                    actual = matched_tool_args.get(fk)
                    if actual is None and result_filters.get(fk) is None:
                        issues.append(AuditIssue(
                            code=AuditCode.FILTER_MISSING,
                            message=(
                                f"фильтр не применён: исполнитель не передал {fk}={fv!r} "
                                "в инструмент. Повтори вызов с правильными аргументами."
                            ),
                            context={"filter": fk, "expected": str(fv)},
                        ))
                    elif actual is not None and str(actual).strip().lower() != str(fv).strip().lower():
                        issues.append(AuditIssue(
                            code=AuditCode.FILTER_MISMATCH,
                            message=(
                                f"неверный фильтр: ожидалось {fk}={fv!r}, "
                                f"передано {fk}={actual!r}. Повтори с правильным значением."
                            ),
                            context={
                                "filter": fk,
                                "expected": str(fv),
                                "actual": str(actual),
                                "source": "args",
                            },
                        ))
                    # Cross-check reported filters in the result
                    rf = result_filters.get(fk)
                    if rf is not None and str(rf).strip().lower() != str(fv).strip().lower():
                        issues.append(AuditIssue(
                            code=AuditCode.FILTER_MISMATCH,
                            message=(
                                f"Рабочий стол показывает {fk}={rf!r} вместо {fv!r}: "
                                "показаны данные от другого запроса. Требуется перезапрос."
                            ),
                            context={
                                "filter": fk,
                                "expected": str(fv),
                                "actual": str(rf),
                                "source": "result",
                            },
                        ))

        for item in self._trace.tool_results:
            result = item.get("result")
            if (
                isinstance(result, dict)
                and str(result.get("error") or "").startswith("Unknown skill")
            ):
                issues.append(AuditIssue(
                    code=AuditCode.UNKNOWN_SKILL,
                    message=str(result["error"]),
                    context={"tool": str(item.get("tool") or "")},
                ))

        return AuditReport(
            passed=not _blocking_issues(issues),
            issues=issues,
            workspace_verified=workspace_verified,
            final_channel=plan.workspace.channel,
        )

    async def _run_semantic_audit(
        self,
        plan: OrchestratorPlan,
        config: BuiltinAgentConfig,
        audit: AuditReport,
    ) -> None:
        """Advisory check that the final answer actually addresses the request.

        Gated: runs for complex turns (Tier>=LARGE) or as a diagnostic when the
        mechanical audit failed, within the per-turn aux-LLM budget. Never flips
        ``audit.passed`` — it emits a soft warning and records
        ``semantic_passed``/``semantic_reason`` for the learning loop. On infra
        failure the verdict stays ``None`` (unknown), not ``True``, so flaky
        infrastructure does not feed false successes to the learning loop.
        """
        if not config.audit_enabled:
            return
        if self._tier < Tier.LARGE and audit.passed:
            return
        final_text = self._trace.final_text
        if not final_text:
            return

        # Deterministic short-circuit: every tool call errored → clear failure.
        results = [
            r.get("result") for r in self._trace.tool_results
            if isinstance(r.get("result"), dict)
        ]
        if results and all(str(r.get("error") or "") for r in results):
            audit.semantic_passed = False
            audit.semantic_reason = "Все вызовы инструментов завершились ошибкой."
            await self._emit_semantic_audit(audit)
            return

        if self._aux_llm_calls >= aux_quality_budget(self._tier):
            return
        self._aux_llm_calls += 1
        self._llm_calls += 1

        tools_used = ", ".join(sorted(set(self._trace.tool_calls))) or "нет"
        prompt = (
            f"Задача пользователя: {plan.goal[:400]}\n"
            f"Использованные инструменты: {tools_used}\n"
            f"Ответ агента:\n{final_text[:1500]}\n\n"
            "Ответ по существу решает задачу пользователя? "
            'Верни строго JSON: {"ok": true|false, "reason": "<кратко на русском>"}'
        )
        try:
            response = await asyncio.wait_for(
                ai_router.run(
                    AIRequest(
                        task=AITask.CLASSIFICATION,
                        messages=[
                            ChatMessage(
                                role="system",
                                content="Ты аудитор качества ответов AI. Отвечай только JSON.",
                            ),
                            ChatMessage(role="user", content=prompt),
                        ],
                        confidential=False,
                        # Cloud auditor is opt-in (protected setting); the AI
                        # router still blocks confidential content from cloud.
                        allow_cloud=bool(config.auditor_allow_cloud),
                        preferred_model=_registry_model_name(
                            config.auditor_model
                            or config.worker_model
                            or config.model
                        ),
                    )
                ),
                timeout=float(config.orchestrator_plan_timeout_seconds),
            )
        except Exception as exc:
            log_degraded("orchestrator.semantic_audit", exc)
            return  # infra failure → verdict stays None (unknown)

        from app.ai.structured_output import parse_json_output
        parsed = parse_json_output(getattr(response, "text", "") or "", default={})
        if not isinstance(parsed, dict) or "ok" not in parsed:
            return  # unparseable verdict → stays None (unknown)
        audit.semantic_passed = bool(parsed.get("ok"))
        audit.semantic_reason = str(parsed.get("reason") or "").strip()
        if not audit.semantic_passed:
            await self._emit_semantic_audit(audit)

    async def _maybe_refine_answer(
        self,
        plan: OrchestratorPlan,
        config: BuiltinAgentConfig,
        audit: AuditReport,
    ) -> None:
        """Revise a generative chat answer once when the auditor flagged it.

        Gated tightly so it never slows down the common (good-answer) path:
        only generative text/document turns whose semantic audit returned an
        explicit failure, within the per-turn aux-LLM budget. Reuses the known
        failure reason — a single revise inference, no extra critique call.
        The revised answer is streamed as a follow-up.
        """
        if audit.semantic_passed is not False or not audit.semantic_reason:
            return
        if plan.workspace.output_type not in ("text", "document"):
            return
        original = self._trace.final_text
        if not original:
            return
        if self._aux_llm_calls >= aux_quality_budget(self._tier):
            return
        self._aux_llm_calls += 1
        self._llm_calls += 1

        async def _generate(prompt: str, system_prompt: str | None) -> str:
            messages = []
            if system_prompt:
                messages.append(ChatMessage(role="system", content=system_prompt))
            messages.append(ChatMessage(role="user", content=prompt))
            try:
                resp = await asyncio.wait_for(
                    ai_router.run(
                        AIRequest(
                            task=AITask.EMAIL_DRAFTING,
                            messages=messages,
                            confidential=True,   # generative answers may cite data → stay local
                            allow_cloud=False,
                            preferred_model=_registry_model_name(
                                config.worker_model or config.model
                            ),
                        )
                    ),
                    timeout=float(config.llm_timeout_seconds),
                )
                return getattr(resp, "text", "") or ""
            except Exception as exc:
                log_degraded("orchestrator.refine_generate", exc)
                return ""

        try:
            from app.ai.self_refine import revise_with_issues
            revised = await revise_with_issues(
                original, plan.goal, [audit.semantic_reason], _generate
            )
        except Exception as exc:
            log_degraded("orchestrator.self_refine", exc)
            return
        if revised and revised.strip() and revised.strip() != original.strip():
            await self._outer_send({
                "type": "answer.revised",
                "content": revised.strip(),
                "reason": audit.semantic_reason,
            })
            # The revised answer is now the effective final text.
            self._trace.text_chunks = [revised.strip()]

    async def _emit_semantic_audit(self, audit: AuditReport) -> None:
        await self._outer_send({
            "type": "audit.semantic",
            "content": (
                "Аудит качества: ответ может не полностью соответствовать запросу — "
                f"{audit.semantic_reason}"
                if not audit.semantic_passed
                else "Аудит качества: ответ соответствует запросу."
            ),
            "semantic_passed": audit.semantic_passed,
            "semantic_reason": audit.semantic_reason,
        })

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
                "issue_codes": audit.issue_codes,
            })
        else:
            await self._outer_send({
                "type": "audit.failed",
                "content": "Аудит: требуется исправление результата.",
                "audit": audit.model_dump(mode="json"),
                "issue_codes": audit.issue_codes,
            })

    def _should_report_capability_gap(
        self,
        plan: OrchestratorPlan,
        audit: AuditReport,
        config: BuiltinAgentConfig,
    ) -> bool:
        if not config.allow_capability_builder:
            return False
        if _has_code(audit.issues, *CAPABILITY_GAP_CODES):
            return True
        return plan.workspace.required and not audit.workspace_verified

    def _can_retry_with_executor(self, plan: OrchestratorPlan, audit: AuditReport) -> bool:
        if not plan.workspace.required:
            return False
        if not plan.worker.recommended_skills:
            return False
        if _has_code(audit.issues, AuditCode.UNKNOWN_SKILL):
            return False  # retrying cannot invent a missing tool
        return _issues_retryable(audit.issues)

    async def _try_execute_planned_workspace_tool(
        self,
        plan: OrchestratorPlan,
        audit: AuditReport,
        config: BuiltinAgentConfig,
    ) -> bool:
        if not plan.workspace.required:
            return False
        if not _issues_retryable(audit.issues):
            return False
        spec = _workspace_tool_spec_for_plan(plan)
        if not spec:
            return False
        return await self._execute_workspace_spec(
            spec, config,
            announce=(
                "Оркестратор: исполнитель выбрал не тот инструмент, "
                "запускаю правильный workspace tool напрямую."
            ),
        )

    # Canvases whose skill has self-sufficient default_args — the orchestrator
    # can run them deterministically (0 worker-LLM) the moment the plan resolves
    # the canvas. spec-table is intentionally excluded: it needs the LLM to
    # choose columns. invoice-pivot etc. carry sensible defaults.
    _PROACTIVE_SAFE_CANVASES = frozenset({
        "agent:invoices",
        "agent:suppliers",
        "agent:documents",
        "agent:invoice-pivot",
        "agent:invoice-items",
        "agent:invoice-items-by-supplier",
        "agent:invoice-items-grouped",
    })

    async def _try_proactive_workspace_execution(
        self,
        plan: OrchestratorPlan,
        config: BuiltinAgentConfig,
    ) -> bool:
        """Run a self-sufficient workspace tool directly, before the worker LLM.

        When the plan resolves to a safe canvas (table/pivot with usable
        defaults), the orchestrator already knows exactly which skill to call —
        spinning up the 35B worker just to emit that one tool call costs ~12s
        of prefill for nothing. Execute it here; on any miss fall through to the
        normal worker loop. Skipped for spec-table (needs LLM-chosen columns).
        """
        if not plan.workspace.required:
            return False
        if plan.workspace.canvas_id not in self._PROACTIVE_SAFE_CANVASES:
            return False
        spec = _workspace_tool_spec_for_plan(plan)
        if not spec:
            return False
        return await self._execute_workspace_spec(
            spec, config,
            announce="Готовлю результат на Рабочем столе…",
        )

    async def _execute_workspace_spec(
        self,
        spec: dict[str, Any],
        config: BuiltinAgentConfig,
        *,
        announce: str,
    ) -> bool:
        """Execute a resolved workspace tool spec via direct HTTP (no worker LLM).

        Shared by the proactive fast-path and the post-audit repair path.
        Resets the turn trace, runs the policy gate, POSTs to the skill endpoint
        and records tool_call/result/text events so audit + done see the output.
        """
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
                "требует подтверждения человеком (approval gate)"
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
            "content": announce,
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
                    headers=_agent_headers(),
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
            reason="; ".join(audit.issue_messages) or "Недостаточно существующих инструментов.",
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

        # Draft real code for the proposal package; activation stays behind
        # the human-approval flow.
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
                    headers=_agent_headers(),
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
        except Exception as exc:
            log_degraded("orchestrator.capability_proposal", exc)
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
                    headers=_agent_headers(),
                )
        except Exception as exc:
            log_degraded("orchestrator.sandbox_apply", exc)

    async def _invoke_capability_builder(
        self,
        gap: CapabilityGapRequest,
        draft: CapabilityBuildDraft,
        plan: OrchestratorPlan,
        config: BuiltinAgentConfig,
    ) -> None:
        """Invoke CapabilityBuilder to draft skill code for the pending proposal.

        The draft is written to generated_skills/ but is NOT registered or
        imported: activation requires the proposal flow (sandbox → human
        decision → promote).
        """
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
                        f"AgentDeveloper: черновик скилла **{result.skill_name}** готов. "
                        "Он станет доступен после проверки и подтверждения человеком "
                        "(предложение уже в очереди согласования)."
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


# Keyword heuristics are delegated to the declarative table in
# aiagent/config/routes.yml (see app.ai.route_table) — do not add markers here.
_norm = route_table.normalize
_is_secretary_query = route_table.is_flow_status_query
_is_workspace_request = route_table.is_workspace_request
_is_table_edit_request = route_table.is_table_edit_request
_references_existing_table = route_table.references_existing_table
_match_intent_route = route_table.match_route
_resolve_canvas_from_route = route_table.resolve_canvas_from_route
_extract_supplier_name = route_table.extract_supplier_name
_is_supplier_grouping_request = route_table.is_supplier_grouping_request


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


def _invalidate_skill_hints_if_changed(registry_path: "Path") -> None:
    """Flush orchestrator:skill:* Redis keys when registry file hash changes."""
    try:
        import hashlib
        current_hash = hashlib.md5(registry_path.read_bytes()).hexdigest()
        from app.ai.orchestrator_memory import _redis
        r = _redis()
        if r is None:
            return
        stored_hash = r.get("orchestrator:registry_hash")
        if stored_hash and stored_hash.decode() == current_hash:
            return
        # Hash changed — flush stale skill hint cache
        keys = r.keys("orchestrator:skill:*")
        if keys:
            r.delete(*keys)
        r.setex("orchestrator:registry_hash", 86400, current_hash)
    except Exception as exc:
        log_degraded("orchestrator.registry_hash_flush", exc)


def _build_skill_registry_context(user_text: str) -> str:
    """Return skills grouped by domain, with top relevant ones highlighted."""
    try:
        from app.ai.gateway_config import gateway_config as _gw_cfg
        registry_path = _gw_cfg.registry_path
        if not registry_path.exists():
            return ""
        _invalidate_skill_hints_if_changed(registry_path)
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
        sg = route_table.supplier_grouping()
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
    if _references_existing_table(content):
        latest_table = _latest_workspace_table_id()
        if latest_table:
            return latest_table
    return route_table.fallback_canvas(content)


def _expected_workspace_skill_for_canvas(canvas_id: str | None) -> str | None:
    return route_table.canvas_to_skill(canvas_id)


def _workspace_tool_spec_for_plan(plan: OrchestratorPlan) -> dict[str, Any] | None:
    skill = _expected_workspace_skill_for_canvas(plan.workspace.canvas_id)
    if not skill:
        return None
    spec_entry = route_table.skill_spec(skill)
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


def _build_worker_hint(plan: OrchestratorPlan) -> str:
    """Build a concise orchestrator hint injected into the worker's message history."""
    skills = plan.worker.recommended_skills[:5]
    # Fallback case: only memory.search — make the call mandatory so the model
    # doesn't answer from parametric knowledge instead of real project data.
    only_memory_fallback = skills == ["memory.search"]
    if only_memory_fallback:
        skill_directive = "ОБЯЗАТЕЛЬНО вызови memory.search перед ответом — не отвечай из памяти модели без проверки данных проекта."
    else:
        skill_directive = f"Используй инструменты: {', '.join(skills)}."
    lines = [
        f"[ОРКЕСТРАТОР] Роль: {plan.worker.role}. Задача: {plan.goal[:200]}",
        skill_directive,
    ]
    if len(plan.worker.recommended_skills) >= 2:
        lines.append(
            "Если нужно несколько НЕЗАВИСИМЫХ справочных данных (list/get/search) — "
            "запроси все нужные инструменты ОДНИМ сообщением (несколько tool_calls сразу), "
            "а не по очереди: они выполнятся параллельно и ответ будет быстрее."
        )
    if plan.workspace.required and plan.workspace.canvas_id:
        lines.append(
            f"Результат ОБЯЗАТЕЛЬНО опубликовать на Рабочий стол (canvas_id={plan.workspace.canvas_id}). "
            "Используй workspace.* инструмент. В чат — только краткое резюме."
        )
        if plan.workspace.filters:
            f_str = ", ".join(f"{k}={v!r}" for k, v in plan.workspace.filters.items())
            lines.append(f"Обязательные фильтры: {f_str}.")
    else:
        lines.append(
            "Формат вывода: текст в чат. "
            "НЕ используй workspace.*, canvas.publish — это простой запрос без rich-вывода."
        )
    return "\n".join(lines)


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
    text = f"{gap.reason} {' '.join(audit.issue_messages)}".lower()
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

    # Per-step verdicts from tool results: an errored call is a fail for that
    # skill, a clean call is a success — independent of the whole-turn outcome.
    skill_outcomes: dict[str, bool] = {}
    for item in trace.tool_results:
        tool = str(item.get("tool") or "").replace("__", ".")
        result = item.get("result")
        if not tool or not isinstance(result, dict):
            continue
        step_ok = not str(result.get("error") or "")
        # Any failure for a skill dominates earlier successes in the same turn.
        skill_outcomes[tool] = skill_outcomes.get(tool, True) and step_ok

    feedback = TurnFeedback(
        intent_text=content[:300],
        intent_category=plan.worker.role,
        skills_planned=list(plan.worker.recommended_skills),
        skills_used=skills_used,
        audit_passed=audit.passed,
        # None (no verdict / infra failure) must not count as a confirmed
        # failure for skill stats; only an explicit False does.
        semantic_passed=audit.semantic_passed is not False,
        retries=retries,
        duration_ms=duration_ms,
        errors=audit.issue_messages,
        skill_outcomes=skill_outcomes,
    )

    try:
        loop = asyncio.get_event_loop()
        loop.run_in_executor(None, record_turn_feedback, feedback)
    except Exception as exc:
        # Best-effort: don't block the main turn
        log_degraded("orchestrator.feedback_record", exc)


def _event_canvas_id(event: dict[str, Any]) -> str | None:
    raw = event.get("canvas_id")
    if raw:
        return str(raw)
    block = event.get("block")
    if isinstance(block, dict) and block.get("id"):
        return str(block["id"])
    return None


def _looks_like_chat_table(text: str) -> bool:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    for idx in range(len(lines) - 1):
        if "|" in lines[idx] and "|" in lines[idx + 1]:
            return True
    return False
