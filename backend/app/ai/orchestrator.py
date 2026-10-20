"""Department-level orchestrator for the built-in agent.

The orchestrator owns the user request lifecycle: intent routing, worker
assignment, rich-output policy, workspace verification, and post-run audit.
The existing AgentSession remains the tool-calling executor.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx
import structlog
from pydantic import BaseModel, Field

logger = structlog.get_logger()


def _agent_headers() -> dict:
    """X-API-Key (+ acting user) for internal orchestrator → backend calls.

    The deterministic fast-paths below (spec-table patches, flow status) bypass
    the capability dispatcher, so they must carry the acting user themselves —
    otherwise a 0-LLM path would see more than the person it answers.
    """
    from app.ai.actor_context import get_acting_user
    from app.config import settings

    headers: dict = {}
    if settings.agent_service_key:
        headers["X-API-Key"] = settings.agent_service_key
    actor = get_acting_user()
    if actor:
        headers["X-Acting-User"] = actor
        from app.auth.execution_context import sign_execution_context

        headers["X-Execution-Context"] = sign_execution_context(actor)
    return headers


from app.ai import route_table
from app.ai.agent_config import BuiltinAgentConfig, get_builtin_agent_config
from app.ai.agent_loop import AgentSession
from app.ai.audit import (
    AuditCode,
    AuditIssue,
)
from app.ai.audit import (
    blocking as _blocking_issues,
)
from app.ai.audit import (
    codes as _issue_codes,
)
from app.ai.audit import (
    messages as _issue_messages,
)
from app.ai.corrections import is_correction, record_correction
from app.ai.degradation import log_degraded
from app.ai.flow_awareness import format_flow_summary_human, get_flow_snapshot
from app.ai.model_tier import (
    Tier,
    aux_quality_budget,
    score_complexity,
)
from app.ai.orchestrator_memory import (
    TurnFeedback,
    record_turn_feedback,
)
from app.ai.policy_engine import check_tool_execution
from app.ai.router import ai_router
from app.ai.schemas import AIRequest, AITask, ChatMessage
from app.domain.workspace import get_workspace_block, list_workspace_blocks

# Только для аннотаций: имена стоят в строковых типах, поэтому в рантайме
# не вычисляются — но без импорта их не видят ни type checker, ни IDE, и
# опечатка в такой аннотации не находится ничем.
SendFn = Callable[[dict], Awaitable[None]]


def invalidate_canvas_map_cache() -> None:
    """Force reload of routes.yml on next access (called on Redis skill_reload event)."""
    route_table.invalidate_cache()


def _response_budget_for(tier: Tier, plan: OrchestratorPlan) -> int:
    """Per-turn response token budget from task complexity and output shape.

    Short chat answers stay cheap (fast on local models); reports/tables/documents
    and complex reasoning get more room. Thresholds live in routes.yml
    (response_budgets, A4) — this function only encodes which Tier/output_type
    comparison applies, not the numbers themselves; a threshold change is a
    YAML edit, not a deploy. Fallback literals match the pre-A4 hardcoded
    values, so a missing/partial config section degrades to old behaviour
    rather than erroring.
    """
    budgets = route_table.response_budgets()
    by_output_type = budgets.get("by_output_type") or {}
    by_tier = budgets.get("by_tier") or {}
    default = int(budgets.get("default", 2048))

    output_type = plan.workspace.output_type
    if output_type in by_output_type:
        return int(by_output_type[output_type])
    if tier >= Tier.LARGE:
        return int(by_tier.get("large", 8192))
    if tier >= Tier.MEDIUM:
        return int(by_tier.get("medium", 4096))
    if tier <= Tier.MICRO:
        return int(by_tier.get("micro", 1024))
    return default


OutputChannel = Literal["chat", "workspace"]
OutputType = Literal["text", "table", "document", "links", "chart", "drawing", "script"]


class WorkspaceOutputSpec(BaseModel):
    channel: OutputChannel = "chat"
    output_type: OutputType = "text"
    required: bool = False
    canvas_id: str | None = None
    description: str = ""
    filters: dict[str, str] = Field(default_factory=dict)


class WorkerAssignment(BaseModel):
    role: str
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
        # Phase 3 (learning): the request that built the current spec-table, so a
        # follow-up correction can be learned against it and replayed later.
        # Persists across turns (one orchestrator per chat session).
        self._turn_content: str = ""
        # A state-changing call already made in THIS turn (survives the trace
        # reset a retry does) — the action must not be demanded twice.
        self._last_spec_request: str = ""
        self._last_spec_source: str = ""
        # Last successfully-replayed recipe — penalised if the user corrects it.
        self._last_recipe_id: str = ""
        # Result payload of the last successful _execute_workspace_spec call, for
        # callers (e.g. table-patch correction learning) that need it without a
        # second round trip.
        self._last_direct_tool_result: dict[str, Any] = {}
        # Отказ политики и сетевой сбой возвращались из _execute_workspace_spec
        # одним и тем же False, и вызывающие одинаково «шли дальше». В итоге
        # после «требуется подтверждение» ход продолжался обычной
        # диспетчеризацией и выдавал человеку результат вместо запроса
        # подтверждения. Флаг отличает одно от другого.
        self._last_direct_tool_blocked: bool = False
        self._plan_source: str = "unplanned"
        self._tier: Tier = Tier.NANO
        # Per-turn LLM call accounting. Lives on the instance (not the trace)
        # because the trace is reset on every retry/repair within a turn.
        # _llm_calls — telemetry of all orchestrator-visible LLM calls;
        # _aux_llm_calls — budgeted quality calls (semantic audit, refine).
        self._llm_calls: int = 0
        self._aux_llm_calls: int = 0
        self._workspace_context: dict[str, Any] = {}
        self._executor = AgentSession(self._send_from_executor)

    def hydrate_history(self, messages: list[dict[str, str]]) -> None:
        # The executor owns the dialogue history (compression-aware); the planner
        # reads it back via recent_dialogue() so the two never drift apart.
        self._executor.hydrate_history(messages)

    def _recent_dialogue(self) -> list[dict[str, str]]:
        return self._executor.recent_dialogue(limit=20)

    async def on_approval(
        self,
        approved: bool,
        approval_id: str | None = None,
        db_id: str | None = None,
        args_override: dict | None = None,
    ) -> None:
        await self._executor.on_approval(
            approved,
            approval_id=approval_id,
            db_id=db_id,
            args_override=args_override,
        )

    async def on_user_message(
        self,
        content: str,
        reasoning_mode: str = "normal",
        workspace_context: dict[str, Any] | None = None,
    ) -> None:
        self._workspace_context = workspace_context if isinstance(workspace_context, dict) else {}
        # Человек ждёт ответа — фоновая работа (разбор каталогов) на это время
        # уступает видеокарту. По состоянию памяти этого не понять: модель
        # агента закреплена в ней навсегда ради мгновенного первого ответа.
        try:
            from app.tasks.gpu_courtesy import mark_interactive

            mark_interactive()
        except Exception:  # noqa: BLE001 — приоритет не должен ломать ход
            pass
        config = get_builtin_agent_config()

        turn_started_at = time.time()
        self._trace = _TurnTrace()
        self._llm_calls = 0
        self._aux_llm_calls = 0

        # Score complexity → determines planning timeout and CoD injection
        history = self._recent_dialogue()
        context_tokens = sum(len(m.get("content", "")) // 4 for m in history)
        tier = score_complexity(content, context_tokens=context_tokens)
        self._tier = tier

        # Phase 3 — if the user is correcting the result of a just-replayed recipe,
        # penalise that recipe so the fail-rate retire logic demotes it.
        await self._penalise_recipe_on_correction(content)

        # Learned recipes are deterministic plans: trusted repeated tasks run
        # with no model call at all.
        recipe_result = await self._try_recipe_for_turn(content, config, turn_started_at)
        if recipe_result is True:
            return
        # A draft (or below-replay-score) recipe is not executed — it travels on
        # as a worker hint. Dropping it here silently un-learned every draft.
        recipe_hint = recipe_result or ""

        # Secretary direct path: flow-status questions are answered by the
        # front-agent itself from live data — no planning, no dispatch, 0 LLM.
        if _is_secretary_query(content) and await self._answer_flow_status_directly(
            content, config, turn_started_at
        ):
            return

        # Spec-table edits: a recognised Russian edit command on an existing
        # spec table («добавь столбец с НДС перед суммой», «отсортируй по…»,
        # «покажи только…») is applied deterministically — 0 LLM, мгновенно.
        if await self._try_sheet_edit_directly(content, config, turn_started_at):
            return
        if await self._try_spec_table_patch_directly(content, config, turn_started_at):
            return

        await self._wait_for_strata_load(config)
        # One employee: the executor reads the request itself, with one
        # constant prompt and every tool — no routing call, no role.
        self._plan_source = "employee"
        await self._run_planned_turn(
            content, _employee_plan(content), config, turn_started_at, reasoning_mode, recipe_hint
        )

    async def _run_planned_turn(
        self,
        content: str,
        plan: OrchestratorPlan,
        config: BuiltinAgentConfig,
        turn_started_at: float,
        reasoning_mode: str,
        recipe_hint: str = "",
    ) -> None:
        """Run the worker for a settled plan, then audit/retry/repair/finish.

        The worker-run + audit + recipe + telemetry tail of every model turn.
        """
        tier = self._tier
        # Remembered for the audit below: whether the user asked to DO something
        # is a property of the request, not of the plan the model produced.
        self._turn_content = content
        self._workspace_before = _workspace_updated_at_snapshot()
        await self._announce_plan(plan)

        if recipe_hint:
            self._executor.inject_orchestrator_hint(recipe_hint)
        # Size the response budget to the task: cheap/fast for short answers,
        # roomy for reports/tables. Avoids the old hardcoded 4096 on every turn.
        self._executor.set_response_budget(_response_budget_for(tier, plan))
        self._llm_calls += 1
        set_references = getattr(self._executor, "set_turn_references", None)
        if callable(set_references):
            set_references(self._workspace_context.get("references"))
        await self._executor.on_user_message(content)
        # Deterministically enforce grouping/sort/aggregates the request asked for
        # but the worker may have dropped (e.g. «сгруппируй по поставщику»). Fires
        # whenever a spec-table was actually published this turn — not only when the
        # PLAN's canvas was agent:spec-table (the worker often publishes there while
        # the plan guessed another canvas, which previously skipped reconcile).
        # The turn is not re-run after the audit: a turn that announced work
        # and stopped is caught inside the executor (COMPLETION_CHECK_NOTE),
        # and the audit codes left (empty answer, tool errors, overstated total)
        # are facts for the verifier and the learning loop, not a reason to
        # ask the model the same thing again.
        audit = await self._audit_turn(plan, config)
        # Semantic correctness check on the settled answer (advisory; runs once).
        await self._run_semantic_audit(plan, config, audit)
        # Reactive self-refine: only when the auditor flagged a generative answer.
        await self._maybe_refine_answer(plan, config, audit)
        await self._publish_audit(audit)

        # Record turn outcome for adaptive planning in future turns
        _record_feedback_async(
            content=content,
            plan=plan,
            trace=self._trace,
            audit=audit,
            retries=0,
            duration_ms=int((time.time() - turn_started_at) * 1000),
        )

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
            llm_calls=self._llm_calls,
            aux_llm_calls=self._aux_llm_calls,
            duration_ms=duration_ms,
        )
        try:
            from app.core.metrics import (
                agent_tool_calls_total,
                agent_turn_duration_seconds,
                agent_turns_total,
            )

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

    async def _try_recipe_for_turn(
        self,
        content: str,
        config: BuiltinAgentConfig,
        turn_started_at: float,
    ) -> bool | str:
        """Recipe replay or hint before the model turn.

        Returns ``True`` if a recipe was replayed deterministically (turn done),
        otherwise a hint string (possibly empty) for the worker. Matching is
        vector-based and gated by ``replay_gate_ok``.
        """
        from app.ai import recipes as recipes_module

        recipe_hit = await recipes_module.find_recipe(content)
        if recipe_hit is None:
            return ""
        recipe, score, margin = recipe_hit
        replay_on = bool(getattr(config, "recipe_replay_enabled", False))
        if replay_on and score >= recipes_module.REPLAY_SCORE and recipe.status == "active":
            slots = recipes_module.resolve_slots(recipe.param_slots, content)
            gate_ok, gate_reason = recipes_module.replay_gate_ok(recipe, score, margin, content)
            if slots is not None and gate_ok:
                if await self._replay_recipe(recipe, slots, content, config, turn_started_at):
                    return True
            elif not gate_ok:
                logger.info(
                    "recipe_replay_gated",
                    recipe=str(recipe.id),
                    reason=gate_reason,
                    score=score,
                )
        if score >= recipes_module.HINT_SCORE:
            steps_text = " → ".join(
                f"{s.get('capability')}.{s.get('action') or 'call'}" for s in (recipe.steps or [])
            )
            return (
                f"Похожая задача уже решалась успешно шагами: {steps_text}. "
                "Используй эту последовательность как отправную точку."
            )
        return ""

    async def _wait_for_strata_load(self, config: BuiltinAgentConfig) -> None:
        """With the agent on Strata, wait out a reload after its idle unload.

        The reload took 86 s live and the 45 s turn router gave up first, so
        the turn ended as "model unavailable" (2026-10-07). Only an unloaded
        model is waited for; a loaded one costs one /health request. An agent
        on another provider does not wait: the GPU being Strata's said nothing
        about the turn, which loaded 40 GB into RAM for nothing and kept a
        MiniMax turn waiting ~25 s (live, 2026-10-20).
        """
        from app.ai import gpu_runtime
        from app.ai.agent_loop import _get_agent_provider
        from app.ai.providers import strata_manager

        if _get_agent_provider(config) != "strata":
            return
        if gpu_runtime.current_owner() != gpu_runtime.STRATA:
            return
        h = await strata_manager.health()
        if h is not None and h.get("loaded", True):
            return
        await self._outer_send(
            {
                "type": "status",
                "content": "Загружаю модель Strata после простоя — обычно до двух минут…",
            }
        )
        if not await strata_manager.ensure_loaded():
            logger.warning("strata_load_wait_timed_out")

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

        await self._outer_send(
            {
                "type": "orchestrator.status",
                "content": "Секретарь: отвечаю по состоянию документооборота.",
                "plan_source": "direct",
                "degraded": False,
            }
        )
        # UI continuity: the frontend renders worker.assigned as a status line.
        await self._outer_send(
            {
                "type": "worker.assigned",
                "content": "Исполнитель: secretary (прямой ответ, без LLM).",
                "role": "secretary",
                "skills": [],
            }
        )
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
            b
            for b in list_workspace_blocks()
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

        # Routed through the same policy gate (check_tool_execution) and HTTP
        # helper as every other direct tool call — see _execute_workspace_spec.
        # A fast-path (0 LLM) must never be a second, ungated way to reach a
        # skill; if workspace.spec_table_patch is ever added to approval_gates
        # in gateway.yml, this call is blocked exactly like the worker-LLM path.
        tool_spec = route_table.skill_spec("workspace.spec_table_patch")
        if not tool_spec:
            return False
        args = {
            "canvas_id": canvas_id,
            "ops": [op.model_dump(mode="json", exclude_none=True) for op in parsed.ops],
        }
        await self._outer_send(
            {
                "type": "orchestrator.status",
                "content": "Секретарь: правка таблицы распознана — применяю мгновенно (без LLM).",
                "plan_source": "table_patch",
                "degraded": False,
            }
        )
        ok = await self._execute_workspace_spec(
            {"tool": "workspace__spec_table_patch", "path": tool_spec["path"], "args": args},
            config,
            announce="Секретарь: применяю правку таблицы…",
        )
        if not ok:
            if self._last_direct_tool_blocked:
                # Инструмент требует подтверждения. Продолжать ход нельзя:
                # обычная диспетчеризация выдала бы человеку таблицу вместо
                # запроса подтверждения, то есть гейт был бы обойдён по сути,
                # оставшись формально соблюдённым.
                await self._outer_send({"type": "done", "action_chips": []})
                return True
            return False  # HTTP failure — fall through to normal dispatch

        result = self._last_direct_tool_result
        if result.get("status") not in ("published",):
            return False  # backend rejected the patch — let the worker LLM retry it

        # Phase 3 — learn this correction against the request that built the table,
        # so a future identical request applies the fix without being corrected.
        if is_correction(content) and self._last_spec_request:
            await record_correction(self._last_spec_request, spec.source, content)
        answer = str(result.get("message") or "Готово.")
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

    async def _try_sheet_edit_directly(
        self,
        content: str,
        config: BuiltinAgentConfig,
        turn_started_at: float,
    ) -> bool:
        """Apply recognised edits to the active WorkspaceSheet.

        This protects the user's mental model: when the active surface is a
        scratch sheet, "добавь строку" or "объедини A1:B1" must not patch an
        older spec-table that happens to be the latest SQL table in workspace.
        """
        surface = self._active_tabular_surface()
        if surface.get("kind") != "sheet":
            return False
        sheet_id = str(surface.get("sheet_id") or "").strip()
        if not sheet_id:
            block_id = str(surface.get("id") or "")
            block = get_workspace_block(block_id) if block_id else None
            sheet_id = str((block or {}).get("sheet_id") or "")
        if not sheet_id:
            return False
        block = get_workspace_block(f"sheet:{sheet_id}") or get_workspace_block(
            str(surface.get("id") or "")
        )
        columns = block.get("columns") if isinstance(block, dict) else None
        column_keys = [
            str(c.get("key")) for c in columns or [] if isinstance(c, dict) and c.get("key")
        ]
        parsed = _parse_sheet_edit_command(content, column_keys)
        if parsed is None:
            return False

        action, path, body, label = parsed
        # Same policy gate as _execute_workspace_spec — a scratch-sheet edit has
        # no dedicated route_table skill entry (the target path is built from the
        # parsed command, not a static manifest lookup), so the check is inlined
        # here rather than delegated. No gate targets sheet edits today, but the
        # fast-path must not be structurally exempt if one ever does. Checked
        # before announcing "applying" — a blocked edit must not claim to run.
        tool_name = "workspace__sheet_patch"
        approval_gates: set[str] = set(config.approval_gates or [])
        args = {"action": action, "sheet_id": sheet_id, **body}
        policy = check_tool_execution(
            skill_name=tool_name,
            args=args,
            config=config,
            approval_gates=approval_gates,
        )
        if not policy.allowed or tool_name in approval_gates:
            reason = (
                "требует подтверждения человеком (approval gate)"
                if tool_name in approval_gates
                else policy.reason
            )
            await self._outer_send(
                {
                    "type": "orchestrator.direct_tool_blocked",
                    "content": (
                        f"Оркестратор: инструмент {tool_name!r} заблокирован политикой — {reason}. "
                        "Требуется явное подтверждение через интерфейс."
                    ),
                    "tool": tool_name,
                    "reason": reason,
                    "risk_level": policy.risk_level,
                }
            )
            return False

        self._workspace_before = _workspace_updated_at_snapshot()
        await self._outer_send(
            {
                "type": "orchestrator.status",
                "content": "Секретарь: правка листа распознана — применяю к активной таблице.",
                "plan_source": "sheet_patch",
                "degraded": False,
            }
        )
        await self._record_orchestrator_tool_event(
            {
                "type": "tool_call",
                "tool": "sheets",
                "args": args,
            }
        )
        try:
            async with httpx.AsyncClient(timeout=float(config.backend_timeout_seconds)) as client:
                resp = await client.post(
                    f"{config.backend_url.rstrip('/')}/api/workspace/sheets/{sheet_id}{path}",
                    json=body,
                    headers=_agent_headers(),
                )
            result = resp.json() if resp.content else {}
        except Exception as exc:
            log_degraded("orchestrator.sheet_patch", exc)
            return False
        if resp.status_code >= 400:
            await self._record_orchestrator_tool_event(
                {
                    "type": "tool_result",
                    "tool": "sheets",
                    "result": {"error": result.get("detail") or f"HTTP {resp.status_code}"},
                }
            )
            return False
        await self._record_orchestrator_tool_event(
            {
                "type": "tool_result",
                "tool": "sheets",
                "result": result,
            }
        )
        await self._outer_send({"type": "text", "content": label})
        try:
            self._executor.record_external_turn(content, label)
        except Exception as exc:
            log_degraded("orchestrator.sheet_patch_record_turn", exc)
        duration_ms = int((time.time() - turn_started_at) * 1000)
        logger.info(
            "agent_turn_complete",
            intent="sheet_patch",
            plan_source="sheet_patch",
            tools_called=["sheets"],
            tool_count=1,
            audit_passed=True,
            retries=0,
            llm_calls=0,
            aux_llm_calls=0,
            duration_ms=duration_ms,
        )
        await self._outer_send({"type": "done", "action_chips": []})
        return True

    def _active_tabular_surface(self) -> dict[str, Any]:
        surface = self._workspace_context.get("active_tabular_surface")
        return surface if isinstance(surface, dict) else {}

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

        # Component 4 — explainable replay: until the recipe has earned enough
        # human-confirmed replays, ASK before running the learned shortcut. This
        # is the human-in-the-loop guard exactly where misfire risk is highest
        # (a similar-but-different request). After trust is earned, runs silently.
        needs_confirmation = (recipe.confirmed_replays or 0) < recipes_module._TRUST_AFTER_CONFIRMED
        if needs_confirmation:
            steps_text = " → ".join(
                f"{s.get('capability')}.{s.get('action') or 'call'}" for s in (recipe.steps or [])
            )
            prompt = (
                f"Задача похожа на ранее выученную «{recipe.name}».\n"
                f"Выполнить по выученному рецепту (шаги: {steps_text})?\n"
                f"Да — выполню сразу; Нет — разберу запрос заново."
            )
            approved = await self._executor.request_confirmation(
                prompt, {"recipe_id": str(recipe.id), "recipe_name": recipe.name}
            )
            if not approved:
                await self._outer_send(
                    {
                        "type": "orchestrator.status",
                        "content": "Секретарь: разбираю запрос обычным путём.",
                        "plan_source": "recipe_declined",
                        "degraded": False,
                    }
                )
                return False

        self._workspace_before = _workspace_updated_at_snapshot()
        await self._outer_send(
            {
                "type": "orchestrator.status",
                "content": f"Секретарь: задача знакома — выполняю выученный рецепт «{recipe.name}».",
                "plan_source": "recipe",
                "degraded": False,
                "recipe_id": str(recipe.id),
            }
        )
        await self._outer_send(
            {
                "type": "worker.assigned",
                "content": f"Исполнитель: {recipe.role} (replay рецепта, без LLM-планирования).",
                "role": recipe.role,
                "skills": [f"{s.get('capability')}" for s in (recipe.steps or [])][:5],
            }
        )

        ok = await recipes_module.replay(recipe, slots, config, on_event=self._send_from_executor)
        if not ok:
            await self._outer_send(
                {
                    "type": "orchestrator.status",
                    "content": "Секретарь: рецепт не сработал, решаю задачу обычным путём.",
                    "plan_source": "recipe_fallback",
                    "degraded": True,
                }
            )
            return False

        # Component 3 — deterministic post-check: a recipe that "ran" but produced
        # nothing (no workspace change AND no result message) very likely matched
        # the wrong task. Treat as a miss → count a failure (feeds the fail-rate
        # retire logic) and fall back to the worker rather than show an empty win.
        produced_message = any(
            isinstance(r.get("result"), dict) and r["result"].get("message")
            for r in self._trace.tool_results
        )
        workspace_changed = bool(self._trace.workspace_events) and await self._verify_workspace(
            OrchestratorPlan(
                goal=content,
                intent="recipe_replay",
                worker=WorkerAssignment(role=recipe.role, task=content),
                workspace=WorkspaceOutputSpec(required=True),
            )
        )
        if not produced_message and not workspace_changed:
            await recipes_module.record_outcome(recipe.id, success=False)
            logger.info("recipe_replay_empty_result", recipe=str(recipe.id))
            await self._outer_send(
                {
                    "type": "orchestrator.status",
                    "content": "Секретарь: рецепт дал пустой результат, решаю задачу обычным путём.",
                    "plan_source": "recipe_fallback",
                    "degraded": True,
                }
            )
            return False

        # Component 4 — replay succeeded with a real result: build trust so the
        # recipe eventually replays without asking.
        if needs_confirmation:
            await recipes_module.record_confirmed_replay(recipe.id)

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
        # Remember the replayed recipe so a follow-up correction can demote it.
        self._last_recipe_id = str(recipe.id)
        return True

    async def _penalise_recipe_on_correction(self, content: str) -> None:
        """A recipe whose result the user corrects is not trustworthy — record a
        failure so the fail-rate retire logic demotes it. No-op otherwise."""
        if not (self._last_recipe_id and is_correction(content)):
            return
        try:
            from app.ai import recipes as _recipes_pen

            await _recipes_pen.record_outcome(self._last_recipe_id, success=False)
            logger.info("recipe_penalised_by_correction", recipe=self._last_recipe_id)
        except Exception as exc:
            log_degraded("orchestrator.recipe_penalty", exc)
        self._last_recipe_id = ""

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
            # The audit's copy is unwrapped; the client still gets the event as is.
            traced = _unwrap_succeeded_tool_result(data)
            self._trace.tool_results.append(traced)
            result = traced.get("result")
            if isinstance(result, dict) and result.get("canvas_id"):
                self._trace.workspace_events.append(
                    {
                        "type": "workspace.updated",
                        "canvas_id": result.get("canvas_id"),
                    }
                )
        elif msg_type == "error":
            self._trace.errors.append(str(data.get("content") or ""))
        elif msg_type == "tools.parallel":
            self._trace.parallel_used = True
            return  # internal observability marker — don't forward to the client
        await self._outer_send(data)

    def _derive_action_chips(self, plan: OrchestratorPlan, content: str) -> list[dict]:
        """Return contextual action chips based on the completed turn's plan."""
        return route_table.chips_for(
            plan.intent, content, workspace_required=plan.workspace.required
        )

    async def _announce_plan(self, plan: OrchestratorPlan) -> None:
        if plan.workspace.required:
            await self._outer_send(
                {
                    "type": "workspace.publish_started",
                    "content": "Рабочий стол: готовлю rich-вывод и проверю публикацию.",
                    "canvas_id": plan.workspace.canvas_id,
                }
            )

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

        # What the turn did, not what a router expected: an empty answer,
        # failed tools, a table claiming more rows than it shows.
        issues: list[AuditIssue] = []
        workspace_verified = bool(self._trace.workspace_events)

        # ── Does the published table deliver what its title promises? ──────────
        # A hand-built block can claim any number: live, the agent published 3
        # rows under the title «Каталог поставщика … — 1046 позиций». The user
        # sees three items and believes that is the whole catalog — the worst
        # kind of silent failure, because nothing looks broken.
        for item in self._trace.tool_results:
            result_payload = item.get("result")
            if not isinstance(result_payload, dict) or result_payload.get("status") != "published":
                continue
            args = self._trace.tool_call_args.get(item.get("tool", "")) or {}
            title = str(
                (result_payload.get("spec") or {}).get("title")
                or (args.get("body") or {}).get("title")
                or ""
            )
            claimed = _claimed_total_in_title(title)
            shown = result_payload.get("shown")
            if claimed and isinstance(shown, int) and shown and claimed > shown:
                issues.append(
                    AuditIssue(
                        code=AuditCode.TOTAL_OVERSTATED,
                        message=(
                            f"Заголовок обещает {claimed}, а опубликовано {shown} строк. "
                            "Опубликуй полные данные (spec_table отдаёт весь набор из SQL) "
                            "или назови в заголовке фактическое количество."
                        ),
                        context={"claimed": claimed, "shown": shown, "title": title[:200]},
                    )
                )
                break

        for item in self._trace.tool_results:
            result = item.get("result")
            if not isinstance(result, dict):
                continue
            # Prefer the structured error_code; fall back to legacy string-prefix
            # match only for results that predate structured dispatcher errors.
            is_unknown = result.get("error_code") in ("unknown_skill", "unknown_capability") or (
                "error_code" not in result
                and str(result.get("error") or "").startswith("Unknown skill")
            )
            if is_unknown:
                issues.append(
                    AuditIssue(
                        code=AuditCode.UNKNOWN_SKILL,
                        message=str(
                            result.get("error") or result.get("message") or "unknown skill"
                        ),
                        context={"tool": str(item.get("tool") or "")},
                    )
                )

        # ── Answer-quality checks — apply to ALL turns, including text-only ─────
        # (Previously a text turn had no blocking check at all → a hallucinated
        # chat answer always passed.)
        final_text = self._trace.final_text
        has_workspace = bool(self._trace.workspace_events)
        if not final_text and not has_workspace:
            # Empty turn — no text and nothing on the desktop — is always wrong.
            issues.append(
                AuditIssue(
                    code=AuditCode.EMPTY_ANSWER,
                    message="Ход завершился без ответа: нет текста и нет вывода на Рабочий стол.",
                )
            )
        # Tool errors (recovered or not) — advisory signal for the learning loop.
        for item in self._trace.tool_results:
            result = item.get("result")
            if not isinstance(result, dict):
                continue
            ec = result.get("error_code")
            if (ec and ec not in ("unknown_skill", "unknown_capability")) or (
                result.get("error") and "error_code" not in result
            ):
                issues.append(
                    AuditIssue(
                        code=AuditCode.TOOL_ERROR,
                        severity="advisory",
                        message=str(result.get("error") or result.get("message") or "tool error"),
                        context={"tool": str(item.get("tool") or "")},
                    )
                )
                break

        return AuditReport(
            passed=not _blocking_issues(issues),
            issues=issues,
            workspace_verified=workspace_verified,
            final_channel=plan.workspace.channel,
        )

    def _published_table_brief(self) -> str:
        """Compact snapshot of the published spec-table for the semantic critic:
        source, columns, grouping/filters, row count and a few sample rows — so
        the critic judges the actual artifact, not just the chat text."""
        pub = _last_published_table(self._trace)
        if not pub:
            return ""
        spec = pub.get("spec") or {}
        cols = [c.get("header") or c.get("field") for c in (spec.get("columns") or [])]
        parts: list[str] = [f"источник={spec.get('source')}", f"колонки={cols}"]
        if spec.get("group_by"):
            parts.append(f"группировка={spec.get('group_by')}")
        if spec.get("filters"):
            parts.append(f"фильтры={spec.get('filters')}")
        parts.append(f"строк={pub.get('total')}")
        try:
            block = get_workspace_block(str(pub.get("canvas_id") or ""))
            sample = ((block or {}).get("rows") or [])[:3]
            if sample:
                parts.append(f"примеры={sample}")
        except Exception:
            pass
        return "; ".join(str(p) for p in parts)[:800]

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
        final_text = self._trace.final_text
        # Parametric-answer risk: a factual turn answered with no tool call. Worth
        # a semantic check even on a small model that passed the mechanical audit.
        ungrounded_risk = (
            plan.intent in ("answer_self", "analytical_table", "document_op", "count")
            and not self._trace.tool_calls
            and len(final_text) > 80
        )
        if self._tier < Tier.LARGE and audit.passed and not ungrounded_risk:
            return
        if not final_text:
            return

        # Deterministic short-circuit: every tool call errored → clear failure.
        results = [
            r.get("result") for r in self._trace.tool_results if isinstance(r.get("result"), dict)
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
        table_brief = self._published_table_brief()
        prompt = (
            f"Задача пользователя: {plan.goal[:400]}\n"
            f"Использованные инструменты: {tools_used}\n"
            + (f"Опубликованная таблица: {table_brief}\n" if table_brief else "")
            + f"Ответ агента:\n{final_text[:1500]}\n\n"
            "Решает ли РЕЗУЛЬТАТ (таблица и/или текст) задачу именно так, как "
            "просили — тот источник, нужные колонки/группировка/фильтры, не пустой "
            "и не данные другого запроса? "
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
                            config.auditor_model or config.worker_model or config.model
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
                            confidential=True,  # generative answers may cite data → stay local
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
            await self._outer_send(
                {
                    "type": "answer.revised",
                    "content": revised.strip(),
                    "reason": audit.semantic_reason,
                }
            )
            # The revised answer is now the effective final text.
            self._trace.text_chunks = [revised.strip()]

    async def _emit_semantic_audit(self, audit: AuditReport) -> None:
        await self._outer_send(
            {
                "type": "audit.semantic",
                "content": (
                    "Аудит качества: ответ может не полностью соответствовать запросу — "
                    f"{audit.semantic_reason}"
                    if not audit.semantic_passed
                    else "Аудит качества: ответ соответствует запросу."
                ),
                "semantic_passed": audit.semantic_passed,
                "semantic_reason": audit.semantic_reason,
            }
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
        before = self._workspace_before.get(canvas_id)
        # A canvas absent from the pre-turn snapshot was created this turn — the
        # publish is genuine even if the updated_at string happens to collide.
        if before is None:
            return True
        updated_at = str(block.get("updated_at") or "")
        return bool(updated_at) and updated_at != before

    async def _publish_audit(self, audit: AuditReport) -> None:
        if audit.workspace_verified:
            await self._outer_send(
                {
                    "type": "workspace.publish_verified",
                    "content": "Рабочий стол: публикация подтверждена.",
                    "audit": audit.model_dump(mode="json"),
                }
            )
        if audit.passed:
            await self._outer_send(
                {
                    "type": "audit.passed",
                    "content": "Аудит: результат проверен, канал вывода корректный.",
                    "audit": audit.model_dump(mode="json"),
                    "issue_codes": audit.issue_codes,
                }
            )
        else:
            await self._outer_send(
                {
                    "type": "audit.failed",
                    "content": "Аудит: требуется исправление результата.",
                    "audit": audit.model_dump(mode="json"),
                    "issue_codes": audit.issue_codes,
                }
            )

    # Canvases whose skill has self-sufficient default_args — the orchestrator
    # can run them deterministically (0 worker-LLM) the moment the plan resolves
    # the canvas. spec-table is intentionally excluded: it needs the LLM to
    # choose columns. invoice-pivot etc. carry sensible defaults.
    _PROACTIVE_SAFE_CANVASES = frozenset(
        {
            "agent:invoices",
            "agent:suppliers",
            "agent:documents",
            "agent:invoice-pivot",
            "agent:invoice-items",
            "agent:invoice-items-by-supplier",
            "agent:invoice-items-grouped",
        }
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
        self._last_direct_tool_blocked = False
        approval_gates: set[str] = set(config.approval_gates or [])
        policy = check_tool_execution(
            skill_name=tool_name,
            args=spec["args"],
            config=config,
            approval_gates=approval_gates,
        )
        if not policy.allowed or tool_name in approval_gates:
            self._last_direct_tool_blocked = True
            reason = (
                "требует подтверждения человеком (approval gate)"
                if tool_name in approval_gates
                else policy.reason
            )
            await self._outer_send(
                {
                    "type": "orchestrator.direct_tool_blocked",
                    "content": (
                        f"Оркестратор: инструмент {tool_name!r} заблокирован политикой — {reason}. "
                        "Требуется явное подтверждение через интерфейс."
                    ),
                    "tool": tool_name,
                    "reason": reason,
                    "risk_level": policy.risk_level,
                }
            )
            return False

        self._trace = _TurnTrace()
        self._workspace_before = _workspace_updated_at_snapshot()
        await self._outer_send(
            {
                "type": "orchestrator.direct_tool_started",
                "content": announce,
                "tool": tool_name,
                "args": spec["args"],
            }
        )
        await self._record_orchestrator_tool_event(
            {
                "type": "tool_call",
                "tool": tool_name,
                "args": spec["args"],
            }
        )
        try:
            async with httpx.AsyncClient(timeout=float(config.backend_timeout_seconds)) as client:
                resp = await client.post(
                    f"{config.backend_url.rstrip('/')}{spec['path']}",
                    json=spec["args"],
                    headers=_agent_headers(),
                )
            if resp.status_code >= 400:
                await self._record_orchestrator_tool_event(
                    {
                        "type": "tool_result",
                        "tool": tool_name,
                        "result": {
                            "error": f"HTTP {resp.status_code}",
                            "detail": resp.text[:300],
                        },
                    }
                )
                return False
            result = resp.json()
        except Exception as exc:
            await self._record_orchestrator_tool_event(
                {
                    "type": "tool_result",
                    "tool": tool_name,
                    "result": {"error": str(exc)},
                }
            )
            return False

        await self._record_orchestrator_tool_event(
            {
                "type": "tool_result",
                "tool": tool_name,
                "result": result,
            }
        )
        message = str(result.get("message") or "") if isinstance(result, dict) else ""
        if message:
            await self._record_orchestrator_tool_event(
                {
                    "type": "text",
                    "content": message,
                }
            )
        # Stashed for callers that need the raw result after a successful direct
        # call (e.g. table-patch correction learning) without a second round trip.
        self._last_direct_tool_result = result if isinstance(result, dict) else {}
        return True

    async def _record_orchestrator_tool_event(self, event: dict[str, Any]) -> None:
        await self._send_from_executor(event)


# Keyword heuristics are delegated to the declarative table in
# aiagent/config/routes.yml (see app.ai.route_table) — do not add markers here.
_norm = route_table.normalize
_is_secretary_query = route_table.is_flow_status_query


def _unwrap_succeeded_tool_result(data: dict[str, Any]) -> dict[str, Any]:
    """Give the turn audit the tool's own result, not its ToolResult v1 envelope.

    Every audit check here reads the tool's fields (status "published" or
    "not_found", canvas_id, filters, spec). Under the envelope they sit in
    "data", so a published SQL table registered no workspace event, the audit
    called it unpublished and retried the turn (live 2026-10-07), and the title
    and filter checks silently skipped every result. A failed envelope is kept
    whole: its error_code is already at the top level.
    """
    result = data.get("result")
    if (
        isinstance(result, dict)
        and result.get("version") == 1
        and result.get("status") == "succeeded"
        and isinstance(result.get("data"), dict)
    ):
        return {**data, "result": result["data"]}
    return data


def _workspace_updated_at_snapshot() -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for block in list_workspace_blocks():
        block_id = str(block.get("id") or "")
        updated_at = str(block.get("updated_at") or "")
        if block_id:
            snapshot[block_id] = updated_at
    return snapshot


def _registry_model_name(model_name: str | None) -> str | None:
    """Resolve a config value to a registry KEY usable as preferred_model.

    Config stores the provider_model name (e.g. "gemma4:e2b"), but the AI router
    looks models up by their catalog KEY (e.g. "gemma4_e2b_ollama"). Match by key
    first, then fall back to provider_model so an assigned light model actually
    takes effect instead of silently degrading to the task default.
    """
    if not model_name:
        return None
    models = ai_router.registry.models
    if model_name in models:
        return model_name
    for key, cap in models.items():
        if getattr(cap, "provider_model", None) == model_name:
            return key
    return None


_CELL_REF_RE = re.compile(r"^([A-Z]+)([1-9][0-9]*)$", re.IGNORECASE)
_MERGE_RANGE_RE = re.compile(
    r"(?:объедин\w*|merge)\s+(?P<start>[A-Z]+[1-9][0-9]*)\s*[:\-]\s*(?P<end>[A-Z]+[1-9][0-9]*)",
    re.IGNORECASE,
)
_UNMERGE_CELL_RE = re.compile(
    r"(?:разъедин\w*|сними\s+объедин\w*|unmerge)\s+(?P<cell>[A-Z]+[1-9][0-9]*)",
    re.IGNORECASE,
)
_ADD_SHEET_COLUMN_RE = re.compile(
    r"добав\w*\s+(?:столбец|колонк\w*)\s+(?P<header>.+?)(?:\s+с\s+формулой\s+(?P<formula>=?.+))?$",
    re.IGNORECASE,
)
_SET_SHEET_FORMULA_RE = re.compile(
    r"(?:задай|установи|поставь)\s+формул\w*\s+(?P<formula>=?\S.+?)\s+(?:в|для)\s+(?P<cell>[A-Z]+[1-9][0-9]*|[A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE,
)


def _col_index_to_letter(index: int) -> str:
    n = index
    label = ""
    while True:
        label = chr(65 + (n % 26)) + label
        n = n // 26 - 1
        if n < 0:
            return label


def _cell_ref(cell: str, column_keys: list[str]) -> tuple[int, str] | None:
    m = _CELL_REF_RE.match(cell.strip())
    if not m:
        return None
    letters, row_s = m.groups()
    idx = 0
    for ch in letters.upper():
        idx = idx * 26 + (ord(ch) - 64)
    idx -= 1
    if idx < 0 or idx >= len(column_keys):
        return None
    return int(row_s) - 1, column_keys[idx]


def _sheet_column_key(header: str, existing: list[str]) -> str:
    raw = re.sub(r"[^A-Za-z0-9_]+", "_", header.strip()).strip("_").lower()
    if not raw:
        raw = _col_index_to_letter(len(existing))
    key = raw
    i = 2
    while key in existing:
        key = f"{raw}_{i}"
        i += 1
    return key


def _parse_sheet_edit_command(
    text: str,
    column_keys: list[str],
) -> tuple[str, str, dict[str, Any], str] | None:
    t = " ".join(text.strip().split())
    low = t.lower()
    if re.search(r"добав\w*\s+строк", low):
        return "add_row", "/add-row", {"count": 1}, "Добавил строку в активный лист."

    if m := _MERGE_RANGE_RE.search(t):
        start = _cell_ref(m.group("start"), column_keys)
        end = _cell_ref(m.group("end"), column_keys)
        if not start or not end:
            return None
        return (
            "merge_cells",
            "/merge-cells",
            {
                "start_row": start[0],
                "end_row": end[0],
                "start_col": start[1],
                "end_col": end[1],
            },
            f"Объединил диапазон {m.group('start').upper()}:{m.group('end').upper()} в активном листе.",
        )

    if m := _UNMERGE_CELL_RE.search(t):
        ref = _cell_ref(m.group("cell"), column_keys)
        if not ref:
            return None
        return (
            "unmerge_cells",
            "/unmerge-cells",
            {"row": ref[0], "col": ref[1]},
            f"Снял объединение для {m.group('cell').upper()} в активном листе.",
        )

    if m := _SET_SHEET_FORMULA_RE.search(t):
        formula = m.group("formula")
        target = m.group("cell")
        ref = _cell_ref(target, column_keys)
        if ref:
            body = {"row": ref[0], "column": ref[1], "formula": formula}
        elif target in column_keys:
            body = {"column": target, "formula": formula}
        else:
            return None
        return "set_formula", "/set-formula", body, "Обновил формулу в активном листе."

    if m := _ADD_SHEET_COLUMN_RE.search(t):
        header = m.group("header").strip(" .")
        # Avoid treating "добавь столбец перед суммой" as a literal column named
        # "перед суммой"; spec-table patch can handle positional DB fields.
        if header.lower().startswith(("перед ", "после ")):
            return None
        key = _sheet_column_key(header, column_keys)
        body: dict[str, Any] = {"key": key, "header": header, "type": "text"}
        if m.group("formula"):
            body["formula"] = m.group("formula")
            body["type"] = "number"
        return "add_column", "/add-column", body, f"Добавил столбец «{header}» в активный лист."

    return None


# Vague references that signal the gated action's target is unclear.
_VAGUE_REFS = (
    "ему",
    "им",
    "ей",
    "его",
    "её",
    "это",
    "этот",
    "этому",
    "тому",
    "туда",
    "там",
    "тем",
    "той",
)


# Intents that must yield ≥1 row on the desktop — an empty published table is the
# "опубликовал не то" bug. ``count`` is excluded (0 is a valid count answer).
def _last_published_table(trace: _TurnTrace) -> dict[str, Any] | None:
    """Latest spec-table publish result in the trace (what the user actually sees)."""
    out: dict[str, Any] | None = None
    for item in trace.tool_results:
        r = item.get("result")
        if (
            isinstance(r, dict)
            and r.get("status") == "published"
            and isinstance(r.get("spec"), dict)
        ):
            out = r
    return out


def _employee_plan(content: str) -> OrchestratorPlan:
    """The plan of every model turn: one employee, chat channel, no role.

    The executor reads the request itself and publishes to the desktop when
    the result needs it; the audit judges what the turn actually did.
    """
    return OrchestratorPlan(
        goal=content[:200],
        intent="employee",
        worker=WorkerAssignment(role="employee", task=content[:200]),
        workspace=WorkspaceOutputSpec(),
    )


def _record_feedback_async(
    *,
    content: str,
    plan: OrchestratorPlan,
    trace: _TurnTrace,
    audit: AuditReport,
    retries: int,
    duration_ms: int,
) -> None:
    """Schedule feedback recording as a background task (non-blocking)."""
    import asyncio

    # Sanitise tool names: trace stores them as "skill__name" format
    skills_used = [t.replace("__", ".") for t in trace.tool_calls if not t.startswith("_")]

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


# Capabilities whose calls only produce views/notes — never a change to the
# business data the user asked to change. A turn that asked for an ACTION and
# only touched these did not do what was asked.
# Turn intents whose whole point is a view: the "action" is the publication.
_REPORTING_INTENTS = frozenset(
    {
        "analytical_table",
        "table_edit",
        "count",
        "flow_status",
        "smalltalk",
        "answer_self",
        "invoice_list",
        "invoice_items_analytics",
        "table_build",
    }
)

# Read-only action prefixes shared by every capability (list/get/search/…).
# Plan filter key → the spec-table fields that satisfy it. A spec table
# expresses "only this supplier" as a filter on a real column, not as the
# plan's shorthand argument name.
_PLAN_FILTER_SPEC_FIELDS: dict[str, tuple[str, ...]] = {
    "supplier_query": ("supplier_name", "supplier", "supplier_inn"),
}
# Filtering by the entity's id satisfies the same requirement more precisely
# than its name (the worker often resolves the supplier first, then filters
# by supplier_id — live 2026-08-21).
_PLAN_FILTER_ID_FIELDS: dict[str, tuple[str, ...]] = {
    "supplier_query": ("supplier_id", "party_id"),
}


_CLAIMED_TOTAL_RE = re.compile(
    r"(\d[\d \u00a0]*)\s*(?:позиц|строк|запис|товар|наименован|счет|счёт|шт\b)",
    re.IGNORECASE,
)


def _claimed_total_in_title(title: str) -> int | None:
    """Row count a title promises («— 1046 позиций», «всего 152 строки»).

    Only counts numbers standing next to a unit word: a bare year in
    «Счета за 2024 год» is not a promise about row count.
    """
    best: int | None = None
    for raw in _CLAIMED_TOTAL_RE.findall(title or ""):
        try:
            value = int(raw.replace(" ", "").replace("\u00a0", ""))
        except ValueError:
            continue
        best = value if best is None else max(best, value)
    return best
