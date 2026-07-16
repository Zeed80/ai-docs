"""AiAgent scenario runner — executes YAML workflow pipelines.

Scenarios are declared in aiagent/scenarios/*.yml and registered in gateway.yml.
Each scenario is a sequence of skill calls with optional for_each, conditions,
and error handling. The runner is intentionally minimal and dependency-free.

Usage:
    from app.ai.scenario_runner import scenario_runner
    result = await scenario_runner.run("email_triage", trigger={"mailbox": "all"})
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from typing import Any

import structlog

from app.ai.gateway_config import gateway_config
from app.core import metrics

logger = structlog.get_logger()


async def _persist_trace(trace_data: dict) -> None:
    """Write scenario trace to DB — fire-and-forget, never raises."""
    try:
        from app.db.session import _get_session_factory
        from app.db.models import ScenarioTrace

        async with _get_session_factory()() as db:
            trace = ScenarioTrace(
                scenario_name=trace_data["scenario_name"],
                status=trace_data["status"],
                trigger=trace_data.get("trigger"),
                steps_total=trace_data["steps_total"],
                steps_done=trace_data["steps_done"],
                step_traces=trace_data.get("step_traces", []),
                error=trace_data.get("error"),
                duration_ms=trace_data.get("duration_ms"),
                started_at=trace_data["started_at"],
                finished_at=trace_data.get("finished_at"),
                triggered_by=trace_data.get("triggered_by", "system"),
            )
            db.add(trace)
            await db.commit()
    except Exception as exc:
        logger.warning("scenario_trace_persist_failed", error=str(exc))


# ── Template engine ───────────────────────────────────────────────────────────

class _Context:
    """Resolves {{ expr }} templates against accumulated step results."""

    def __init__(self, trigger: dict | None = None) -> None:
        self._data: dict[str, Any] = {"trigger": trigger or {}}

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value

    def resolve(self, template: Any) -> Any:
        """Resolve a value — strings with {{ }} get substituted. Recurses into
        lists and dicts so templates inside nested structures (e.g.
        ``source_document_ids: ["{{trigger.document_id}}"]`` or a ``body:``
        object) render too, not only top-level strings."""
        if isinstance(template, list):
            return [self.resolve(item) for item in template]
        if isinstance(template, dict):
            return {k: self.resolve(v) for k, v in template.items()}
        if not isinstance(template, str):
            return template
        # Single whole-string template → return actual typed value
        single = re.fullmatch(r"\{\{\s*(.+?)\s*\}\}", template.strip())
        if single:
            return self._get(single.group(1).strip())
        # Inline substitutions
        return re.sub(
            r"\{\{(.+?)\}\}",
            lambda m: str(self._get(m.group(1).strip()) or ""),
            template,
        )

    def resolve_dict(self, d: dict) -> dict:
        return {k: self.resolve(v) for k, v in d.items()}

    def evaluate(self, expr: str) -> bool:
        """Evaluate a simple boolean condition string."""
        inner = re.sub(r"^\{\{\s*|\s*\}\}$", "", expr.strip())
        for op in ("==", "!=", ">=", "<=", ">", "<"):
            if op in inner:
                lhs_expr, rhs_expr = inner.split(op, 1)
                lhs = self._get(lhs_expr.strip())
                rhs = rhs_expr.strip().strip("'\"")
                if op == "==":
                    return str(lhs) == rhs
                if op == "!=":
                    return str(lhs) != rhs
                try:
                    if op == ">":
                        return float(lhs) > float(rhs)
                    if op == "<":
                        return float(lhs) < float(rhs)
                    if op == ">=":
                        return float(lhs) >= float(rhs)
                    if op == "<=":
                        return float(lhs) <= float(rhs)
                except (TypeError, ValueError):
                    return False
        val = self.resolve(expr)
        return bool(val) and val is not None and str(val) not in ("None", "False", "")

    def _get(self, path: str) -> Any:
        """Get value by dotted path, e.g. 'step1.results[0].id'."""
        val: Any = self._data
        for part in re.split(r"[.\[\]]", path):
            if not part:
                continue
            if isinstance(val, dict):
                val = val.get(part)
            elif isinstance(val, list):
                try:
                    val = val[int(part)]
                except (ValueError, IndexError):
                    return None
            else:
                return None
        return val


# ── Skill caller ──────────────────────────────────────────────────────────────

def _skill_catalog() -> dict[str, dict]:
    """Skills visible to scenarios: the active registry PLUS the capability
    catalog. Scenarios must resolve capability names (image_studio,
    engineering, …) regardless of which skills_mode the chat agent runs in —
    a scenario written against capabilities must not break when the agent is
    flipped to legacy registry mode."""
    from app.ai.agent_loop import _load_registry, _sanitize_name

    _, skill_map = _load_registry(expose_filter=None)  # all skills, no filter
    catalog = dict(skill_map)
    try:
        import yaml as _yaml

        from app.ai.gateway_config import gateway_config as _gw

        caps_path = _gw.capabilities_path
        if caps_path.exists():
            data = _yaml.safe_load(caps_path.read_text()) or {}
            for cap in data.get("capabilities", []) or []:
                name = _sanitize_name(cap.get("name", ""))
                if name and name not in catalog:
                    catalog[name] = cap
    except Exception as exc:  # noqa: BLE001 — capability merge is additive
        logger.warning("scenario_capability_catalog_failed", error=str(exc))
    return catalog


async def _call_skill(skill_name: str, params: dict) -> dict:
    """Execute a skill by name against the FastAPI backend."""
    from app.ai.agent_loop import _execute_skill, _sanitize_name

    skill = _skill_catalog().get(_sanitize_name(skill_name))
    if not skill:
        return {"error": f"Skill not found: {skill_name}"}
    return await _execute_skill(skill, params)


# ── Step executor ─────────────────────────────────────────────────────────────

class _StopScenario(Exception):
    """Raised by a step with action: complete to halt the scenario early."""


async def _run_step(step: dict, ctx: _Context) -> Any:
    """Execute one scenario step and return its result."""
    import asyncio

    skill_name: str = step.get("skill", "")
    on_error: str = step.get("on_error", "continue")
    condition: str | None = step.get("condition")
    for_each: str | None = step.get("for_each")
    params: dict = step.get("params", {})
    action: str | None = step.get("action")
    # G1: polling — repeat the skill call until `until` evaluates truthy over
    # the LAST result (exposed as {{ last.* }}). For async work (digitize)
    # where the scenario must wait for a queued job to finish.
    until: str | None = step.get("until")
    poll_interval_s: float = float(step.get("poll_interval_s", 5))
    max_polls: int = int(step.get("max_polls", 60))

    if condition and not ctx.evaluate(condition):
        return {"skipped": True, "reason": "condition_false"}

    # action: complete — stop scenario execution immediately (clean exit)
    if action == "complete":
        raise _StopScenario("early_exit")

    resolved_params = ctx.resolve_dict(params)

    try:
        if until:
            result: Any = None
            for attempt in range(max_polls):
                result = await _call_skill(skill_name, ctx.resolve_dict(params))
                ctx.set("last", result)
                if ctx.evaluate(until):
                    return {**(result if isinstance(result, dict) else {"value": result}),
                            "_polls": attempt + 1}
                await asyncio.sleep(poll_interval_s)
            return {"error": f"until-условие не выполнено за {max_polls} попыток",
                    "last": result, "_polls": max_polls}
        if for_each:
            items = ctx.resolve(for_each)
            if not isinstance(items, list):
                items = [items] if items is not None else []
            results = []
            for item in items:
                ctx.set("item", item)
                item_params = ctx.resolve_dict(params)
                result = await _call_skill(skill_name, item_params)
                results.append(result)
            return {"results": results, "count": len(results)}
        else:
            return await _call_skill(skill_name, resolved_params)
    except Exception as exc:
        logger.warning("scenario_step_error", skill=skill_name, error=str(exc))
        if on_error == "abort":
            raise
        return {"error": str(exc)}


# ── Scenario runner ───────────────────────────────────────────────────────────

class ScenarioRunner:
    """Execute AiAgent YAML scenarios."""

    async def run(
        self,
        scenario_name: str,
        trigger: dict | None = None,
        triggered_by: str = "system",
    ) -> dict:
        """Run a named scenario and return the final context dict.

        Args:
            scenario_name: name as registered in gateway.yml (e.g. 'email_triage')
            trigger: initial event data passed as {{ trigger.* }} in templates
            triggered_by: user sub or "system" (stored in trace)
        Returns:
            dict with all step results keyed by step id
        """
        scenario = gateway_config.load_scenario(scenario_name)
        if not scenario:
            raise ValueError(f"Scenario not found: {scenario_name!r}")

        ctx = _Context(trigger)
        steps: list[dict] = scenario.get("steps", [])
        timeout: int = scenario.get("timeout", 300)
        step_traces: list[dict] = []
        started_at = datetime.now(timezone.utc)
        t0 = time.monotonic()
        status = "ok"
        error_msg: str | None = None

        logger.info(
            "scenario_start",
            name=scenario_name,
            steps=len(steps),
            trigger=trigger,
        )

        import asyncio
        try:
            async with asyncio.timeout(timeout):
                for idx, step in enumerate(steps):
                    step_id = step.get("id", f"step_{idx}")
                    on_error = step.get("on_error", "continue")
                    step_t0 = time.monotonic()
                    step_status = "ok"
                    step_error: str | None = None
                    result: Any = None
                    try:
                        result = await _run_step(step, ctx)
                        ctx.set(step_id, result)
                        logger.debug(
                            "scenario_step_done",
                            scenario=scenario_name,
                            step=step_id,
                        )
                    except _StopScenario:
                        # action: complete — clean early exit, not an error
                        step_status = "ok"
                        ctx.set(step_id, {"skipped": True, "reason": "action_complete"})
                        logger.info("scenario_early_exit", scenario=scenario_name, step=step_id)
                        break
                    except Exception as exc:
                        step_status = "error"
                        step_error = str(exc)
                        logger.error(
                            "scenario_step_abort",
                            scenario=scenario_name,
                            step=step_id,
                            error=str(exc),
                        )
                        if on_error == "abort":
                            status = "error"
                            error_msg = f"Step {step_id}: {exc}"
                            raise
                    finally:
                        step_traces.append({
                            "step_id": step_id,
                            "skill": step.get("skill", ""),
                            "status": step_status,
                            "duration_ms": int((time.monotonic() - step_t0) * 1000),
                            "error": step_error,
                            "result_keys": list(result.keys()) if isinstance(result, dict) else None,
                        })
        except TimeoutError:
            status = "timeout"
            error_msg = f"Scenario timed out after {timeout}s"
            logger.error("scenario_timeout", name=scenario_name, timeout=timeout)
            ctx.set("_timeout", True)
        except Exception as exc:
            if status != "error":
                status = "error"
                error_msg = str(exc)

        finished_at = datetime.now(timezone.utc)
        duration_ms = int((time.monotonic() - t0) * 1000)
        logger.info("scenario_done", name=scenario_name, status=status, duration_ms=duration_ms)
        metrics.scenario_runs_total.labels(scenario=scenario_name).inc()
        metrics.scenario_duration_seconds.labels(scenario=scenario_name).observe(duration_ms / 1000)
        if status in ("error", "timeout"):
            metrics.scenario_errors_total.labels(scenario=scenario_name, reason=status).inc()

        import asyncio as _asyncio
        _asyncio.create_task(_persist_trace({
            "scenario_name": scenario_name,
            "status": status,
            "trigger": trigger,
            "steps_total": len(steps),
            "steps_done": len(step_traces),
            "step_traces": step_traces,
            "error": error_msg,
            "duration_ms": duration_ms,
            "started_at": started_at,
            "finished_at": finished_at,
            "triggered_by": triggered_by,
        }))

        return dict(ctx._data)

    def dry_run(self, scenario_name: str, trigger: dict | None = None) -> dict:
        """G1: plan a scenario WITHOUT executing anything.

        Resolves every step against the skill registry (missing skills become
        an explicit gap report, not a runtime surprise), templates the
        parameters as far as the trigger allows, and flags the steps whose
        capability action is approval-gated — so a human can see exactly what
        the agent WOULD do, and where it would have to stop and ask, before
        anything runs."""
        from app.ai.agent_loop import _sanitize_name

        scenario = gateway_config.load_scenario(scenario_name)
        if not scenario:
            raise ValueError(f"Scenario not found: {scenario_name!r}")
        skill_map = _skill_catalog()
        ctx = _Context(trigger)
        planned: list[dict] = []
        missing: list[str] = []
        gated: list[str] = []
        for idx, step in enumerate(scenario.get("steps", [])):
            step_id = step.get("id", f"step_{idx}")
            skill_name = step.get("skill", "")
            skill = skill_map.get(_sanitize_name(skill_name)) if skill_name else None
            if skill_name and skill is None:
                missing.append(skill_name)
            try:
                resolved = ctx.resolve_dict(step.get("params", {}))
            except Exception:  # noqa: BLE001 — later-step templates can't resolve yet
                resolved = step.get("params", {})
            gate_actions = set((skill or {}).get("gate_actions") or [])
            requires_approval = bool(gate_actions and resolved.get("action") in gate_actions)
            if requires_approval:
                gated.append(step_id)
            planned.append({
                "step_id": step_id,
                "name": step.get("name"),
                "skill": skill_name or None,
                "skill_found": skill is not None if skill_name else None,
                "params": resolved,
                "condition": step.get("condition"),
                "until": step.get("until"),
                "for_each": step.get("for_each"),
                "on_error": step.get("on_error", "continue"),
                "requires_approval": requires_approval,
            })
        plan = {
            "scenario": scenario_name,
            "description": scenario.get("description"),
            "steps": planned,
            "missing_skills": sorted(set(missing)),
            "approval_gated_steps": gated,
            "declared_gates": [g.get("id") for g in scenario.get("approval_gates", [])],
            "executable": not missing,
        }
        import asyncio as _asyncio

        started = datetime.now(timezone.utc)
        _asyncio.create_task(_persist_trace({
            "scenario_name": scenario_name,
            "status": "dry_run",
            "trigger": trigger,
            "steps_total": len(planned),
            "steps_done": 0,
            "step_traces": [
                {"step_id": p["step_id"], "skill": p["skill"] or "",
                 "status": "planned", "duration_ms": 0, "error": None,
                 "result_keys": None}
                for p in planned
            ],
            "error": None,
            "duration_ms": 0,
            "started_at": started,
            "finished_at": started,
            "triggered_by": "dry_run",
        }))
        return plan

    def list_scenarios(self) -> list[dict]:
        """Return scenario metadata from gateway.yml."""
        return [
            {
                "name": s.get("name"),
                "trigger": s.get("trigger", {}),
                "path": s.get("path"),
            }
            for s in gateway_config.scenario_definitions
        ]


scenario_runner = ScenarioRunner()
