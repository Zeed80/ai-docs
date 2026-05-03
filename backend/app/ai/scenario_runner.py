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
from typing import Any

import structlog

from app.ai.gateway_config import gateway_config

logger = structlog.get_logger()


# ── Template engine ───────────────────────────────────────────────────────────

class _Context:
    """Resolves {{ expr }} templates against accumulated step results."""

    def __init__(self, trigger: dict | None = None) -> None:
        self._data: dict[str, Any] = {"trigger": trigger or {}}

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value

    def resolve(self, template: Any) -> Any:
        """Resolve a value — strings with {{ }} get substituted."""
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

async def _call_skill(skill_name: str, params: dict) -> dict:
    """Execute a skill by name against the FastAPI backend."""
    from app.ai.agent_loop import _execute_skill, _load_registry, _sanitize_name

    _, skill_map = _load_registry(expose_filter=None)  # all skills, no filter
    sanitized = _sanitize_name(skill_name)
    skill = skill_map.get(sanitized)
    if not skill:
        return {"error": f"Skill not found: {skill_name}"}
    return await _execute_skill(skill, params)


# ── Step executor ─────────────────────────────────────────────────────────────

async def _run_step(step: dict, ctx: _Context) -> Any:
    """Execute one scenario step and return its result."""
    skill_name: str = step.get("skill", "")
    on_error: str = step.get("on_error", "continue")
    condition: str | None = step.get("condition")
    for_each: str | None = step.get("for_each")
    params: dict = step.get("params", {})

    if condition and not ctx.evaluate(condition):
        return {"skipped": True, "reason": "condition_false"}

    resolved_params = ctx.resolve_dict(params)

    try:
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
    ) -> dict:
        """Run a named scenario and return the final context dict.

        Args:
            scenario_name: name as registered in gateway.yml (e.g. 'email_triage')
            trigger: initial event data passed as {{ trigger.* }} in templates
        Returns:
            dict with all step results keyed by step id
        """
        scenario = gateway_config.load_scenario(scenario_name)
        if not scenario:
            raise ValueError(f"Scenario not found: {scenario_name!r}")

        ctx = _Context(trigger)
        steps: list[dict] = scenario.get("steps", [])
        timeout: int = scenario.get("timeout", 300)

        logger.info(
            "scenario_start",
            name=scenario_name,
            steps=len(steps),
            trigger=trigger,
        )

        import asyncio
        try:
            async with asyncio.timeout(timeout):
                for step in steps:
                    step_id = step.get("id", f"step_{steps.index(step)}")
                    on_error = step.get("on_error", "continue")
                    try:
                        result = await _run_step(step, ctx)
                        ctx.set(step_id, result)
                        logger.debug(
                            "scenario_step_done",
                            scenario=scenario_name,
                            step=step_id,
                        )
                    except Exception as exc:
                        logger.error(
                            "scenario_step_abort",
                            scenario=scenario_name,
                            step=step_id,
                            error=str(exc),
                        )
                        if on_error == "abort":
                            raise
        except TimeoutError:
            logger.error("scenario_timeout", name=scenario_name, timeout=timeout)
            ctx.set("_timeout", True)

        logger.info("scenario_done", name=scenario_name)
        return dict(ctx._data)

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
