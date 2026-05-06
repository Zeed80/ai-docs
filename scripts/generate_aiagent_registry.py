from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
BACKEND_ROOT = ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.main import app


TOOL_MAP: dict[tuple[str, str], dict[str, Any]] = {
    ("POST", "/api/documents/ingest"): {
        "name": "doc.ingest",
        "approval_required": False,
        "description": "Accept a document, store it, and create a Document record.",
    },
    ("POST", "/api/documents/{document_id}/classify"): {
        "name": "doc.classify",
        "approval_required": False,
        "description": "Classify document type through the AI router.",
    },
    ("POST", "/api/documents/{document_id}/extract"): {
        "name": "doc.extract",
        "approval_required": False,
        "description": "Run structured extraction for a document.",
    },
    ("GET", "/api/documents/{document_id}"): {
        "name": "doc.get",
        "approval_required": False,
        "description": "Read a document with extraction evidence.",
    },
    ("GET", "/api/documents"): {
        "name": "doc.list",
        "approval_required": False,
        "description": "List documents with filters.",
    },
    ("POST", "/api/documents/{document_id}/summarize"): {
        "name": "doc.summarize",
        "approval_required": False,
        "description": "Summarize a document without external action.",
    },
    ("POST", "/api/invoices/{invoice_id}/approve"): {
        "name": "invoice.approve",
        "approval_required": True,
        "description": "Approve an invoice after human confirmation.",
    },
    ("POST", "/api/invoices/{invoice_id}/reject"): {
        "name": "invoice.reject",
        "approval_required": True,
        "description": "Reject an invoice after human confirmation.",
    },
    ("POST", "/api/invoices/{invoice_id}/validate"): {
        "name": "invoice.validate",
        "approval_required": False,
        "description": "Run deterministic invoice checks.",
    },
    ("GET", "/api/invoices/{invoice_id}/price-check"): {
        "name": "invoice.compare_prices",
        "approval_required": False,
        "description": "Compare invoice lines with supplier price history.",
    },
    ("POST", "/api/email/drafts"): {
        "name": "email.draft",
        "approval_required": False,
        "description": "Create a draft email with risk-check.",
    },
    ("POST", "/api/email/drafts/{draft_id}/risk-check"): {
        "name": "email.risk_check",
        "approval_required": False,
        "description": "Check draft email risks before send.",
    },
    ("POST", "/api/email/drafts/{draft_id}/send"): {
        "name": "email.send",
        "approval_required": True,
        "description": "Send a draft email only after approval.",
    },
    ("POST", "/api/invoices/{invoice_id}/export"): {
        "name": "invoice.export.excel",
        "approval_required": False,
        "description": "Create local Excel artifact for an invoice.",
    },
    ("POST", "/api/invoices/{invoice_id}/export-1c"): {
        "name": "invoice.export.1c.prepare",
        "approval_required": True,
        "description": "Prepare 1C export payload; no external send.",
    },
    ("POST", "/api/graph/nodes"): {
        "name": "graph.node_create",
        "approval_required": False,
        "description": "Create a graph memory node.",
    },
    ("POST", "/api/graph/edges"): {
        "name": "graph.edge_create",
        "approval_required": False,
        "description": "Create a graph relationship edge.",
    },
    ("GET", "/api/graph/nodes/{node_id}/neighborhood"): {
        "name": "graph.neighborhood",
        "approval_required": False,
        "description": "Read connected graph memory around a node.",
    },
    ("GET", "/api/graph/path"): {
        "name": "graph.path",
        "approval_required": False,
        "description": "Find a relationship path between two graph nodes.",
    },
    ("POST", "/api/graph/chunks"): {
        "name": "graph.chunk_create",
        "approval_required": False,
        "description": "Create a document memory chunk.",
    },
    ("POST", "/api/graph/evidence"): {
        "name": "graph.evidence_create",
        "approval_required": False,
        "description": "Create a source evidence span.",
    },
    ("POST", "/api/graph/mentions"): {
        "name": "graph.mention_create",
        "approval_required": False,
        "description": "Create an entity mention from a document.",
    },
    ("GET", "/api/graph/review"): {
        "name": "graph.review_list",
        "approval_required": False,
        "description": "List graph memory suggestions that need review.",
    },
    ("POST", "/api/graph/review/{item_id}/decide"): {
        "name": "graph.review_decide",
        "approval_required": False,
        "description": "Approve or reject a graph memory suggestion.",
    },
    ("POST", "/api/memory/search"): {
        "name": "memory.search",
        "approval_required": False,
        "description": "Search graph and document memory with source evidence.",
    },
    ("POST", "/api/memory/explain"): {
        "name": "memory.explain",
        "approval_required": False,
        "description": "Search memory and return evidence with graph context.",
    },
    ("POST", "/api/memory/reindex"): {
        "name": "memory.reindex",
        "approval_required": False,
        "description": "Rebuild graph memory for existing documents.",
    },
    ("POST", "/api/memory/embeddings/rebuild"): {
        "name": "memory.embeddings_rebuild",
        "approval_required": False,
        "description": "Prepare chunk and evidence embeddings for Qdrant.",
    },
    ("GET", "/api/memory/embeddings/stats"): {
        "name": "memory.embeddings_stats",
        "approval_required": False,
        "description": "Show active embedding profile and record statuses.",
    },
    ("POST", "/api/memory/embeddings/rebuild-active"): {
        "name": "memory.embeddings_rebuild_active",
        "approval_required": False,
        "description": "Rebuild records for the active embedding profile.",
    },
    ("POST", "/api/memory/embeddings/index-active"): {
        "name": "memory.embeddings_index_active",
        "approval_required": False,
        "description": "Index queued/stale memory embedding records into Qdrant.",
    },
    ("POST", "/api/agent/config/propose"): {
        "name": "config.propose",
        "approval_required": False,
        "description": "Propose an agent configuration change for review.",
    },
    ("POST", "/api/agent/capabilities/propose"): {
        "name": "capability.propose",
        "approval_required": False,
        "description": "Propose a missing capability draft.",
    },
    ("GET", "/api/agent/capabilities/{proposal_id}/status"): {
        "name": "capability.status",
        "approval_required": False,
        "description": "Read capability proposal lifecycle state.",
    },
    ("POST", "/api/agent/capabilities/{proposal_id}/sandbox-apply"): {
        "name": "capability.sandbox_apply",
        "approval_required": False,
        "description": "Prepare sandbox validation for a draft capability.",
    },
    ("POST", "/api/agent/tasks/create"): {
        "name": "task.create",
        "approval_required": False,
        "description": "Create an agent work item.",
    },
    ("GET", "/api/settings/ntd-control"): {
        "name": "ntd.control_settings_get",
        "approval_required": False,
        "description": "Read NTD norm-control mode.",
    },
    ("PATCH", "/api/settings/ntd-control"): {
        "name": "ntd.control_settings_update",
        "approval_required": False,
        "description": "Set manual or automatic NTD norm-control mode.",
    },
    ("GET", "/api/ntd/documents"): {
        "name": "ntd.document_list",
        "approval_required": False,
        "description": "List normative documents.",
    },
    ("POST", "/api/ntd/documents"): {
        "name": "ntd.document_create",
        "approval_required": False,
        "description": "Create a normative document record.",
    },
    ("POST", "/api/ntd/documents/from-source"): {
        "name": "ntd.document_create_from_source",
        "approval_required": False,
        "description": "Create and optionally index NTD from an uploaded document.",
    },
    ("POST", "/api/ntd/documents/{normative_document_id}/index"): {
        "name": "ntd.document_index",
        "approval_required": False,
        "description": "Parse source document text into NTD clauses and requirements.",
    },
    ("POST", "/api/ntd/clauses"): {
        "name": "ntd.clause_create",
        "approval_required": False,
        "description": "Create a normative document clause.",
    },
    ("POST", "/api/ntd/requirements"): {
        "name": "ntd.requirement_create",
        "approval_required": False,
        "description": "Create a normative requirement.",
    },
    ("GET", "/api/ntd/requirements/search"): {
        "name": "ntd.requirement_search",
        "approval_required": False,
        "description": "Search SQL-first NTD requirements.",
    },
    ("POST", "/api/documents/{document_id}/ntd-check"): {
        "name": "ntd.norm_control_run",
        "approval_required": False,
        "description": "Check one document against applicable NTD.",
    },
    ("GET", "/api/documents/{document_id}/ntd-check/availability"): {
        "name": "ntd.check_availability",
        "approval_required": False,
        "description": "Explain whether NTD check can run for a document.",
    },
    ("POST", "/api/ntd/checks/run"): {
        "name": "ntd.norm_control_run_payload",
        "approval_required": False,
        "description": "Check a document against applicable NTD.",
    },
    ("GET", "/api/documents/{document_id}/ntd-checks"): {
        "name": "ntd.check_list",
        "approval_required": False,
        "description": "List NTD checks for a document.",
    },
    ("GET", "/api/ntd/checks/{check_id}"): {
        "name": "ntd.check_get",
        "approval_required": False,
        "description": "Get NTD check details and findings.",
    },
    ("POST", "/api/ntd/checks/{check_id}/findings/{finding_id}/decide"): {
        "name": "ntd.finding_decide",
        "approval_required": False,
        "description": "Record a human decision for an NTD finding.",
    },
    ("GET", "/api/technology/resources"): {
        "name": "tech.resource_list",
        "approval_required": False,
        "description": "List machines, tools, fixtures, and equipment.",
    },
    ("POST", "/api/technology/resources"): {
        "name": "tech.resource_create",
        "approval_required": False,
        "description": "Create a manufacturing resource.",
    },
    ("GET", "/api/technology/operation-templates"): {
        "name": "tech.operation_template_list",
        "approval_required": False,
        "description": "List technology operation templates.",
    },
    ("POST", "/api/technology/operation-templates"): {
        "name": "tech.operation_template_create",
        "approval_required": False,
        "description": "Create a technology operation template.",
    },
    ("GET", "/api/technology/process-plans"): {
        "name": "tech.process_plan_list",
        "approval_required": False,
        "description": "List manufacturing process plans.",
    },
    ("POST", "/api/technology/process-plans"): {
        "name": "tech.process_plan_create",
        "approval_required": False,
        "description": "Create a manufacturing process plan.",
    },
    ("POST", "/api/technology/process-plans/draft-from-document"): {
        "name": "tech.process_plan_draft_from_document",
        "approval_required": False,
        "description": "Draft process plan from document graph memory.",
    },
    ("GET", "/api/technology/process-plans/{plan_id}"): {
        "name": "tech.process_plan_get",
        "approval_required": False,
        "description": "Get process plan with operations and norms.",
    },
    ("POST", "/api/technology/process-plans/{plan_id}/approve"): {
        "name": "tech.process_plan_approve",
        "approval_required": True,
        "description": "Approve a manufacturing process plan after review.",
    },
    ("POST", "/api/technology/process-plans/{plan_id}/validate"): {
        "name": "tech.process_plan_validate",
        "approval_required": False,
        "description": "Validate manufacturability and completeness.",
    },
    ("POST", "/api/technology/process-plans/{plan_id}/estimate-norms"): {
        "name": "tech.norm_estimate_suggest",
        "approval_required": False,
        "description": "Suggest operation time and cutting parameters.",
    },
    ("POST", "/api/technology/process-plans/{plan_id}/operations"): {
        "name": "tech.operation_add",
        "approval_required": False,
        "description": "Add manufacturing operation and graph links.",
    },
    ("POST", "/api/technology/process-plans/{plan_id}/norm-estimates"): {
        "name": "tech.norm_estimate_create",
        "approval_required": False,
        "description": "Create labor and machine time estimate.",
    },
    ("POST", "/api/technology/norm-estimates/{estimate_id}/approve"): {
        "name": "tech.norm_estimate_approve",
        "approval_required": True,
        "description": "Approve labor and machine time estimate.",
    },
    ("POST", "/api/technology/corrections"): {
        "name": "tech.correction_record",
        "approval_required": False,
        "description": "Record a human correction for learning.",
    },
    ("GET", "/api/technology/learning-suggestions"): {
        "name": "tech.learning_suggest",
        "approval_required": False,
        "description": "Suggest rules from repeated corrections.",
    },
    ("GET", "/api/technology/learning-rules"): {
        "name": "tech.learning_rule_list",
        "approval_required": False,
        "description": "List proposed and active learning rules.",
    },
    ("POST", "/api/technology/learning-rules"): {
        "name": "tech.learning_rule_create",
        "approval_required": False,
        "description": "Save a proposed learning rule.",
    },
    ("POST", "/api/technology/learning-rules/{rule_id}/activate"): {
        "name": "tech.learning_rule_activate",
        "approval_required": True,
        "description": "Activate a proposed learning rule.",
    },
}


def build_registry() -> dict[str, Any]:
    openapi = app.openapi()
    tools: list[dict[str, Any]] = []
    for path, operations in openapi["paths"].items():
        for method, operation in operations.items():
            key = (method.upper(), path)
            mapped = TOOL_MAP.get(key)
            if mapped is None:
                continue
            tools.append(
                {
                    **mapped,
                    "method": key[0],
                    "path": key[1],
                    "operation_id": operation.get("operationId"),
                    "request_schema": _schema_ref(operation.get("requestBody")),
                    "response_schema": _schema_ref(
                        operation.get("responses", {}).get("200")
                        or operation.get("responses", {}).get("201")
                    ),
                }
            )
    missing = {tool["name"] for tool in TOOL_MAP.values()} - {tool["name"] for tool in tools}
    if missing:
        raise RuntimeError(f"OpenAPI is missing mapped tools: {sorted(missing)}")
    return {
        "version": 1,
        "source": "fastapi_openapi",
        "policy": {
            "default": "deny",
            "unknown_tools": "deny",
            "confidential_default": True,
            "external_actions_require_approval": True,
        },
        "tools": sorted(tools, key=lambda tool: tool["name"]),
    }


def _schema_ref(container: dict[str, Any] | None) -> str | None:
    if not container:
        return None
    content = container.get("content", {})
    for media_type in (
        "application/json",
        "multipart/form-data",
        "application/x-www-form-urlencoded",
    ):
        schema = content.get(media_type, {}).get("schema")
        if schema:
            return schema.get("$ref") or schema.get("title")
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate AiAgent tool registry from FastAPI OpenAPI.")
    parser.add_argument(
        "--output",
        default="aiagent/skills/registry.json",
        help="Path to write the generated registry JSON.",
    )
    args = parser.parse_args()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    registry = build_registry()
    if output.suffix.lower() in {".yml", ".yaml"}:
        output.write_text(
            yaml.safe_dump(registry, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
    else:
        output.write_text(
            json.dumps(registry, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
