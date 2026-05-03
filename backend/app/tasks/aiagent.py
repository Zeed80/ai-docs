from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from backend.app.domain.models import AgentAction, ApprovalGate, Document, Invoice
from backend.app.domain.services import (
    add_agent_scenario_completed_audit,
    add_agent_scenario_started_audit,
    create_approval_gate,
    create_task_job,
    record_agent_action,
)
from backend.app.domain.schemas import AgentScenarioRunRequest


AIAGENT_ROOT = Path("aiagent")
REGISTRY_PATH = AIAGENT_ROOT / "skills" / "registry.json"
SCENARIOS_DIR = AIAGENT_ROOT / "scenarios"


def load_tool_registry() -> dict[str, dict[str, Any]]:
    raw = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    return {tool["name"]: tool for tool in raw["tools"]}


def load_scenario(name: str) -> dict[str, Any]:
    path = SCENARIOS_DIR / f"{name}.json"
    if not path.exists():
        raise KeyError(name)
    return json.loads(path.read_text(encoding="utf-8"))


def run_aiagent_scenario(
    db: Session,
    *,
    scenario_name: str,
    payload: AgentScenarioRunRequest,
) -> tuple[list[AgentAction], list[ApprovalGate], list[str], int]:
    registry = load_tool_registry()
    scenario = load_scenario(scenario_name)
    max_steps = int(scenario.get("max_steps", 1))
    requested_tools = payload.requested_tools or scenario.get("steps", [])
    actions: list[AgentAction] = []
    gates: list[ApprovalGate] = []
    warnings: list[str] = []

    add_agent_scenario_started_audit(
        db,
        scenario=scenario_name,
        case_id=payload.case_id,
        requested_tools=list(requested_tools),
        max_steps=max_steps,
    )

    if len(requested_tools) > max_steps:
        warnings.append(f"Requested {len(requested_tools)} steps; truncated to max_steps={max_steps}")
        requested_tools = requested_tools[:max_steps]

    for step_no, tool_name in enumerate(requested_tools, start=1):
        tool = registry.get(tool_name)
        if tool is None:
            actions.append(
                record_agent_action(
                    db,
                    scenario=scenario_name,
                    tool_name=tool_name,
                    step_no=step_no,
                    case_id=payload.case_id,
                    status="denied_unknown_tool",
                    payload=payload.model_dump(exclude_none=True),
                    result={"reason": "Tool is not allowlisted"},
                )
            )
            warnings.append(f"Denied unknown tool: {tool_name}")
            continue

        if tool.get("approval_required"):
            action = record_agent_action(
                db,
                scenario=scenario_name,
                tool_name=tool_name,
                step_no=step_no,
                case_id=payload.case_id,
                status="blocked_for_approval",
                payload=payload.model_dump(exclude_none=True),
                result={"approval_required": True},
            )
            actions.append(action)
            gates.append(
                create_approval_gate(
                    db,
                    gate_type=tool_name,
                    reason=f"Tool {tool_name} requires human approval",
                    case_id=payload.case_id,
                    action_id=action.id,
                    payload=payload.model_dump(exclude_none=True),
                )
            )
            continue

        action_status, result = _simulate_safe_tool(tool_name, payload, db)
        action = record_agent_action(
            db,
            scenario=scenario_name,
            tool_name=tool_name,
            step_no=step_no,
            case_id=payload.case_id,
            status=action_status,
            payload=payload.model_dump(exclude_none=True),
            result=result,
        )
        actions.append(action)
        if action_status == "planned" and _tool_can_be_queued(tool_name) and result.get("queue", True):
            task = create_task_job(
                db,
                task_type=tool_name,
                case_id=payload.case_id,
                document_id=payload.document_id if tool_name.startswith("document.") else None,
                agent_action_id=action.id,
                payload=payload.model_dump(exclude_none=True),
            )
            result["task_id"] = task.id

    add_agent_scenario_completed_audit(
        db,
        scenario=scenario_name,
        case_id=payload.case_id,
        status="completed_with_gates" if gates else "completed",
        action_count=len(actions),
        approval_gate_count=len(gates),
        warnings=warnings,
    )
    return actions, gates, warnings, max_steps


def _simulate_safe_tool(
    tool_name: str,
    payload: AgentScenarioRunRequest,
    db: Session,
) -> tuple[str, dict[str, Any]]:
    if tool_name == "document.process" and not payload.document_id:
        return "skipped_missing_input", {"missing": "document_id"}
    if tool_name in {"document.invoice_extraction", "document.drawing_analysis"} and not payload.document_id:
        return "skipped_missing_input", {"missing": "document_id"}
    if tool_name.startswith("document.") and payload.document_id and db.get(Document, payload.document_id) is None:
        return "planned", {
            "note": "Tool is allowlisted, but execution was not queued because document_id is not present locally",
            "queue": False,
        }
    if tool_name == "email.draft" and not (payload.case_id or payload.draft_id):
        return "skipped_missing_input", {"missing": "case_id_or_draft_id"}
    if tool_name.startswith("invoice.") and not payload.invoice_id:
        return "skipped_missing_input", {"missing": "invoice_id"}
    if tool_name.startswith("invoice.") and payload.invoice_id and db.get(Invoice, payload.invoice_id) is None:
        return "planned", {
            "note": "Tool is allowlisted, but execution was not queued because invoice_id is not present locally",
            "queue": False,
        }
    return "planned", {"note": "Tool is allowlisted and safe; execution is delegated to explicit API endpoint"}


def _tool_can_be_queued(tool_name: str) -> bool:
    return tool_name in {
        "document.process",
        "document.invoice_extraction",
        "document.drawing_analysis",
        "email.draft",
        "invoice.export.xlsx",
    }
