"""Parallel execution is authorized by reviewed tool effects, never name prefixes."""

from __future__ import annotations

import json

from app.ai.tool_catalog import TOOLS, get_tool


def _is_parallel_safe(tool_name: str, arguments: dict | None = None) -> bool:
    name = tool_name.replace("__", ".")
    definition = TOOLS.get(name) or get_tool(name, str((arguments or {}).get("action", "")))
    return definition is not None and definition.parallel_safe


def should_parallelize(tool_calls: list[dict]) -> bool:
    if len(tool_calls) < 2:
        return False
    for call in tool_calls:
        function = call.get("function", {})
        arguments = function.get("arguments", {})
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except (TypeError, ValueError):
                return False
        if not isinstance(arguments, dict) or not _is_parallel_safe(
            function.get("name", ""), arguments
        ):
            return False
    return True
