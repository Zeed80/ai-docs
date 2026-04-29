from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

REQUIRED_ROLE_IDS = {
    "technologist_process_plan",
    "designer_drawing_ntd_review",
    "norm_setter_learning",
    "warehouse_keeper_receipt",
    "buyer_procurement_rfq",
}

ALLOWED_MEMORY_MODES = {"sql", "sql_vector", "sql_vector_rerank", "graph", "hybrid"}


def validate_agent_role_manifest(
    manifest_path: Path,
    registry_path: Path,
) -> list[str]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    tools = _load_registry_tools(registry_path)
    errors: list[str] = []

    if manifest.get("privacy") != "synthetic_no_customer_documents":
        errors.append("privacy must be synthetic_no_customer_documents")

    memory_mode = manifest.get("default_memory_mode")
    if memory_mode not in ALLOWED_MEMORY_MODES:
        errors.append(f"default_memory_mode must be one of {sorted(ALLOWED_MEMORY_MODES)}")

    roles = manifest.get("roles") or []
    role_ids = {role.get("id") for role in roles}
    missing_roles = sorted(REQUIRED_ROLE_IDS - role_ids)
    if missing_roles:
        errors.append(f"missing required role cases: {', '.join(missing_roles)}")

    for role in roles:
        errors.extend(_validate_role(role, tools))

    return errors


def _load_registry_tools(registry_path: Path) -> dict[str, dict[str, Any]]:
    raw = yaml.safe_load(registry_path.read_text(encoding="utf-8")) or {}
    tool_items = raw.get("tools") or raw.get("skills") or []
    return {tool["name"]: tool for tool in tool_items if "name" in tool}


def _validate_role(role: dict[str, Any], tools: dict[str, dict[str, Any]]) -> list[str]:
    role_id = role.get("id") or "<missing-id>"
    errors: list[str] = []

    for field in ("profession", "user_request"):
        if not role.get(field):
            errors.append(f"{role_id}: {field} must not be empty")

    expected_tools = role.get("expected_tool_sequence") or []
    if len(expected_tools) < 3:
        errors.append(f"{role_id}: expected_tool_sequence must contain at least 3 tools")

    for tool_name in expected_tools:
        if tool_name not in tools:
            errors.append(f"{role_id}: unknown tool in expected sequence: {tool_name}")

    approval_gates = role.get("expected_approval_gates") or []
    for tool_name in approval_gates:
        tool = tools.get(tool_name)
        if tool_name not in expected_tools:
            errors.append(f"{role_id}: approval gate is not in expected sequence: {tool_name}")
        if not tool:
            continue
        if tool.get("approval_required") is not True:
            errors.append(f"{role_id}: tool must be approval_required: {tool_name}")

    terms = role.get("expected_terms") or []
    if len(terms) < 4:
        errors.append(f"{role_id}: expected_terms must contain at least 4 terms")

    return errors
