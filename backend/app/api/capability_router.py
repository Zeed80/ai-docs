"""Capability dispatcher — unified /api/agent/cap/{capability} endpoint.

Routes agent capability calls to the appropriate backend endpoints.
Each capability accepts an `action` field plus context parameters.
"""

from __future__ import annotations

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from app.ai.agent_config import get_builtin_agent_config
from app.ai.capability_manifest import load_capability_manifest
from app.ai.policy_engine import check_tool_execution, classify_capability_action_risk
from app.config import settings

router = APIRouter(tags=["capabilities"])

# Maps capability → action → (method, path_template, path_params)
# path_params: list of arg keys that get interpolated into the URL
_DISPATCH: dict[str, dict[str, tuple[str, str, list[str]]]] = {
    "documents": {
        "list":         ("GET",    "/api/documents",                          []),
        "get":          ("GET",    "/api/documents/{document_id}",            ["document_id"]),
        "search":       ("POST",   "/api/documents/search",                   []),
        "ingest":       ("POST",   "/api/documents/ingest",                   []),
        "classify":     ("POST",   "/api/documents/{document_id}/classify",   ["document_id"]),
        "extract":      ("POST",   "/api/documents/{document_id}/extract",    ["document_id"]),
        "summarize":    ("POST",   "/api/documents/{document_id}/summarize",  ["document_id"]),
        "update":       ("PATCH",  "/api/documents/{document_id}",            ["document_id"]),
        "delete":       ("DELETE", "/api/documents/{document_id}",            ["document_id"]),
        "bulk_delete":  ("POST",   "/api/documents/bulk-delete",              []),
        "link":         ("POST",   "/api/documents/{document_id}/link",       ["document_id"]),
        "dependencies": ("GET",    "/api/documents/{document_id}/dependencies", ["document_id"]),
        # Processing-queue visibility & control (sequential GPU pipeline)
        "queue":           ("GET",  "/api/documents/workspace",                  []),
        "processing_status": ("GET", "/api/documents/{document_id}/management",   ["document_id"]),
        "reprocess":       ("POST", "/api/documents/{document_id}/classify",      ["document_id"]),
    },
    "invoices": {
        "list":           ("GET",    "/api/invoices",                               []),
        "get":            ("GET",    "/api/invoices/{invoice_id}",                  ["invoice_id"]),
        "validate":       ("POST",   "/api/invoices/{invoice_id}/validate",         ["invoice_id"]),
        "approve":        ("POST",   "/api/invoices/{invoice_id}/approve",          ["invoice_id"]),
        "reject":         ("POST",   "/api/invoices/{invoice_id}/reject",           ["invoice_id"]),
        "compare_prices": ("GET",    "/api/invoices/{invoice_id}/price-check",      ["invoice_id"]),
        "export_excel":   ("POST",   "/api/invoices/{invoice_id}/export",           ["invoice_id"]),
        "export_1c":      ("POST",   "/api/invoices/{invoice_id}/export-1c",        ["invoice_id"]),
        "update":         ("PATCH",  "/api/invoices/{invoice_id}",                  ["invoice_id"]),
        "delete":         ("DELETE", "/api/invoices/{invoice_id}",                  ["invoice_id"]),
        "bulk_delete":    ("POST",   "/api/invoices/bulk-delete",                   []),
        "bulk_approve":   ("POST",   "/api/invoices/bulk-approve",                  []),
        "bulk_reject":    ("POST",   "/api/invoices/bulk-reject",                   []),
        "receive":        ("POST",   "/api/invoices/{invoice_id}/receive",          ["invoice_id"]),
    },
    "suppliers": {
        "list":             ("GET",   "/api/suppliers",                               []),
        "get":              ("GET",   "/api/suppliers/{supplier_id}",                 ["supplier_id"]),
        "search":           ("POST",  "/api/suppliers/search",                        []),
        "price_history":    ("GET",   "/api/suppliers/{supplier_id}/price-history",   ["supplier_id"]),
        "trust_score":      ("GET",   "/api/suppliers/{supplier_id}/trust-score",     ["supplier_id"]),
        "alerts":           ("GET",   "/api/suppliers/{supplier_id}/alerts",          ["supplier_id"]),
        "update":           ("PATCH", "/api/suppliers/{supplier_id}",                 ["supplier_id"]),
        "check_requisites": ("POST",  "/api/suppliers/{supplier_id}/check-requisites", ["supplier_id"]),
    },
    "warehouse": {
        "list_inventory":   ("GET",    "/api/warehouse/inventory",                               []),
        "get_item":         ("GET",    "/api/warehouse/inventory/{item_id}",                     ["item_id"]),
        "low_stock":        ("GET",    "/api/warehouse/inventory/low-stock",                     []),
        "create_receipt":   ("POST",   "/api/warehouse/receipts",                               []),
        "confirm_receipt":  ("POST",   "/api/warehouse/receipts/{receipt_id}/confirm",          ["receipt_id"]),
        "issue_stock":      ("POST",   "/api/warehouse/inventory/{item_id}/issue",              ["item_id"]),
        "adjust_stock":     ("POST",   "/api/warehouse/inventory/{item_id}/adjust",             ["item_id"]),
        "list_receipts":    ("GET",    "/api/warehouse/receipts",                               []),
        "get_receipt":      ("GET",    "/api/warehouse/receipts/{receipt_id}",                  ["receipt_id"]),
        "list_movements":   ("GET",    "/api/warehouse/movements",                              []),
        "create_item":      ("POST",   "/api/warehouse/inventory",                              []),
        "update_item":      ("PATCH",  "/api/warehouse/inventory/{item_id}",                    ["item_id"]),
        "delete_item":      ("DELETE", "/api/warehouse/inventory/{item_id}",                    ["item_id"]),
        "bulk_confirm":     ("POST",   "/api/warehouse/receipts/bulk-confirm",                  []),
        "update_status":    ("PATCH",  "/api/warehouse/receipts/{receipt_id}/status",           ["receipt_id"]),
    },
    "email": {
        "list":              ("GET",   "/api/email/threads",                           []),
        "search":            ("POST",  "/api/email/search",                            []),
        "get":               ("GET",   "/api/email/threads/{thread_id}",               ["thread_id"]),
        "draft":             ("POST",  "/api/email/drafts",                            []),
        "send":              ("POST",  "/api/email/drafts/{draft_id}/send",            ["draft_id"]),
        "fetch_new":         ("POST",  "/api/email/fetch",                             []),
        "risk_check":        ("POST",  "/api/email/drafts/{draft_id}/risk-check",      ["draft_id"]),
        "list_templates":    ("GET",   "/api/email/templates",                         []),
        "get_template":      ("GET",   "/api/email/templates/{template_id}",           ["template_id"]),
        "render_template":   ("POST",  "/api/email/templates/{template_id}/render",    ["template_id"]),
        "suggest_template":  ("POST",  "/api/email/templates/suggest",                 []),
        "style_match":       ("POST",  "/api/email/style-match",                       []),
        "delete_template":   ("DELETE", "/api/email/templates/{template_id}",          ["template_id"]),
    },
    "procurement": {
        "list_requests":   ("GET",   "/api/purchase-requests",                        []),
        "create_request":  ("POST",  "/api/purchase-requests",                        []),
        "get_request":     ("GET",   "/api/purchase-requests/{request_id}",           ["request_id"]),
        "update_request":  ("PATCH", "/api/purchase-requests/{request_id}",           ["request_id"]),
        "send_rfq":        ("POST",  "/api/purchase-requests/{request_id}/send-rfq",  ["request_id"]),
        "list_contracts":  ("GET",   "/api/compare",                                  []),
        "create_contract": ("POST",  "/api/compare",                                  []),
        "get_contract":    ("GET",   "/api/compare/{contract_id}",                    ["contract_id"]),
        "update_contract": ("PATCH", "/api/compare/{contract_id}",                    ["contract_id"]),
    },
    "payments": {
        "list_schedule":         ("GET",   "/api/payment-schedules",                          []),
        "overdue":               ("GET",   "/api/payment-schedules/overdue",                  []),
        "upcoming":              ("GET",   "/api/payment-schedules/upcoming",                 []),
        "mark_paid":             ("POST",  "/api/payment-schedules/{schedule_id}/mark-paid",  ["schedule_id"]),
        "create_schedule":       ("POST",  "/api/payment-schedules",                          []),
        "calendar":              ("GET",   "/api/calendar/upcoming",                          []),
    },
    "anomalies": {
        "list":        ("GET",   "/api/anomalies",                   []),
        "check_all":   ("POST",  "/api/anomalies/check",             []),
        "explain":     ("POST",  "/api/anomalies/{anomaly_id}/explain", ["anomaly_id"]),
        "resolve":     ("POST",  "/api/anomalies/{anomaly_id}/resolve", ["anomaly_id"]),
        "create_card": ("POST",  "/api/anomalies",                   []),
    },
    "normalization": {
        "list_rules":            ("GET",   "/api/normalization/rules",                    []),
        "suggest_rule":          ("POST",  "/api/normalization/rules/suggest",            []),
        "activate_rule":         ("POST",  "/api/normalization/rules/{rule_id}/activate", ["rule_id"]),
        "apply_rules":           ("POST",  "/api/normalization/apply",                    []),
        "list_norm_cards":       ("GET",   "/api/normalization/cards",                    []),
        "create_norm_card":      ("POST",  "/api/normalization/cards",                    []),
        "update_norm_card":      ("PATCH", "/api/normalization/cards/{item_id}",          ["item_id"]),
        "list_canonical_items":  ("GET",   "/api/normalization/canonical",                []),
        "get_canonical_item":    ("GET",   "/api/normalization/canonical/{item_id}",      ["item_id"]),
        "update_canonical_item": ("PATCH", "/api/normalization/canonical/{item_id}",      ["item_id"]),
    },
    "workspace": {
        # Spec-driven tables: LLM supplies only the spec, data flows SQL→table.
        "spec_table":           ("POST", "/api/workspace/agent/spec-table",           []),
        "spec_table_patch":     ("POST", "/api/workspace/agent/spec-table/patch",     []),
        "spec_table_catalog":   ("GET",  "/api/workspace/agent/spec-table/catalog",   []),
        # Approval-gated: queues a DraftAction, never writes the DB directly.
        "spec_table_cell_edit": ("POST", "/api/workspace/agent/spec-table/cell-edit", []),
        "invoice_table":             ("POST", "/api/workspace/agent/invoices/table",               []),
        "invoice_items_table":       ("POST", "/api/workspace/agent/invoices/items-table",         []),
        "invoice_items_grouped":     ("POST", "/api/workspace/agent/invoices/items-grouped-table", []),
        "invoice_items_by_supplier": ("POST", "/api/workspace/agent/invoices/items-by-supplier-table", []),
        "invoice_pivot":             ("POST", "/api/workspace/agent/invoices/pivot-table",            []),
        "general":                   ("POST", "/api/workspace/agent/generated/general",            []),
        # Guarded free SQL (SELECT-only, validated) when no spec source fits.
        "sql_table":                 ("POST", "/api/workspace/agent/generated/sql-table",          []),
        "compare_table_data":        ("POST", "/api/workspace/agent/compare-table-data",           []),
        "supplier_lookup":           ("POST", "/api/workspace/agent/generated/supplier_lookup",    []),
        "verify":                    ("POST", "/api/workspace/agent/verify-block",                 []),
        "get_block":                 ("GET",  "/api/workspace/blocks/{canvas_id}",                 ["canvas_id"]),
    },
    "sheets": {
        # Ad-hoc editable spreadsheets ("листы") — never touch production data.
        "create":      ("POST",   "/api/workspace/sheets/create",                    []),
        "list":        ("GET",    "/api/workspace/sheets",                           []),
        "get":         ("GET",    "/api/workspace/sheets/{sheet_id}",                ["sheet_id"]),
        "patch_cells": ("POST",   "/api/workspace/sheets/{sheet_id}/patch-cells",    ["sheet_id"]),
        "add_row":     ("POST",   "/api/workspace/sheets/{sheet_id}/add-row",        ["sheet_id"]),
        "add_column":  ("POST",   "/api/workspace/sheets/{sheet_id}/add-column",     ["sheet_id"]),
        "set_formula":   ("POST",   "/api/workspace/sheets/{sheet_id}/set-formula",   ["sheet_id"]),
        "rename_column": ("POST",   "/api/workspace/sheets/{sheet_id}/rename-column", ["sheet_id"]),
        "merge_cells":   ("POST",   "/api/workspace/sheets/{sheet_id}/merge-cells",   ["sheet_id"]),
        "unmerge_cells": ("POST",   "/api/workspace/sheets/{sheet_id}/unmerge-cells", ["sheet_id"]),
        "delete":        ("DELETE", "/api/workspace/sheets/{sheet_id}",               ["sheet_id"]),
        "from_spec":     ("POST",   "/api/workspace/sheets/from-spec",                []),
        "from_template": ("POST",   "/api/workspace/sheets/from-template",            []),
        "templates":     ("GET",    "/api/workspace/sheets/templates/list",           []),
    },
    "search": {
        "hybrid":        ("POST", "/api/search/hybrid",                             []),
        "nl":            ("POST", "/api/search/nl",                                 []),
        "nl_to_query":   ("POST", "/api/search/nl-to-query",                        []),
        "web":           ("POST", "/api/web-search/query",                          []),
        "explain":       ("POST", "/api/memory/explain",                            []),
        "similar":       ("GET",  "/api/search/similar/{entity_type}/{entity_id}",  ["entity_type", "entity_id"]),
        "saved_queries": ("GET",  "/api/search/saved-queries",                      []),
    },
    "memory": {
        "query":            ("POST", "/api/memory/query",                    []),
        "search":           ("POST", "/api/memory/search",                   []),
        "explain":          ("POST", "/api/memory/explain",                  []),
        "promote":          ("POST", "/api/memory/promotions",               []),
        "promotion_list":    ("GET",  "/api/memory/promotions",               []),
        "promotion_evaluate": ("POST", "/api/memory/promotions/{entity_id}/evaluate", ["entity_id"]),
        "promotion_decide":  ("POST", "/api/memory/promotions/{entity_id}/decide", ["entity_id"]),
        "source_propose":   ("POST", "/api/memory/sources/propose",          []),
        "source_list":      ("GET",  "/api/memory/sources",                  []),
        "source_discover":  ("POST", "/api/memory/sources/discover",         []),
        "source_decide":    ("POST", "/api/memory/sources/{entity_id}/decide", ["entity_id"]),
        "reindex":          ("POST", "/api/memory/reindex",                  []),
        "embeddings_stats": ("GET",  "/api/memory/embeddings/stats",         []),
        # Multi-hop graph traversal — relational questions ("что связано с
        # этим поставщиком", "цепочка согласования по счёту") need this
        # instead of lexical/vector memory.search.
        "neighborhood":     ("GET",  "/api/graph/nodes/{node_id}/neighborhood", ["node_id"]),
        "path":             ("GET",  "/api/graph/path",                      []),
    },
    "tech": {
        "process_plan_list":             ("GET",   "/api/technology/process-plans",                                    []),
        "process_plan_get":              ("GET",   "/api/technology/process-plans/{entity_id}",                        ["entity_id"]),
        "process_plan_create":           ("POST",  "/api/technology/process-plans",                                    []),
        "process_plan_validate":         ("POST",  "/api/technology/process-plans/{entity_id}/validate",               ["entity_id"]),
        "process_plan_approve":          ("POST",  "/api/technology/process-plans/{entity_id}/approve",                ["entity_id"]),
        "operation_add":                 ("POST",  "/api/technology/process-plans/{entity_id}/operations",             ["entity_id"]),
        "operation_template_list":       ("GET",   "/api/technology/operation-templates",                              []),
        "norm_estimate_create":          ("POST",  "/api/technology/process-plans/{entity_id}/norm-estimates",         ["entity_id"]),
        "norm_estimate_suggest":         ("POST",  "/api/technology/process-plans/{entity_id}/estimate-norms",         ["entity_id"]),
        "norm_estimate_approve":         ("POST",  "/api/technology/norm-estimates/{entity_id}/approve",               ["entity_id"]),
        "resource_list":                 ("GET",   "/api/technology/resources",                                        []),
        "resource_create":               ("POST",  "/api/technology/resources",                                        []),
        "bom_list":                      ("GET",   "/api/boms",                                                        []),
        "bom_get":                       ("GET",   "/api/boms/{entity_id}",                                            ["entity_id"]),
        "bom_create":                    ("POST",  "/api/boms",                                                        []),
        "bom_update":                    ("PATCH", "/api/boms/{entity_id}",                                            ["entity_id"]),
        "bom_approve":                   ("POST",  "/api/boms/{entity_id}/approve",                                    ["entity_id"]),
        "bom_stock_check":               ("GET",   "/api/boms/{entity_id}/stock-check",                                ["entity_id"]),
        "bom_purchase_request":          ("POST",  "/api/boms/{entity_id}/purchase-request",                           ["entity_id"]),
        "ntd_list":                      ("GET",   "/api/ntd/documents",                                               []),
        "ntd_get":                       ("GET",   "/api/ntd/checks/{entity_id}",                                      ["entity_id"]),
        "ntd_run_check":                 ("POST",  "/api/ntd/checks/run",                                              []),
        "ntd_findings":                  ("GET",   "/api/ntd/checks/{entity_id}/findings" if False else "/api/ntd/requirements/search", []),
        "learning_suggest":              ("GET",   "/api/technology/learning-suggestions",                             []),
        "learning_rule_list":            ("GET",   "/api/technology/learning-rules",                                   []),
        "learning_rule_create":          ("POST",  "/api/technology/learning-rules",                                   []),
        "learning_rule_activate":        ("POST",  "/api/technology/learning-rules/{entity_id}/activate",              ["entity_id"]),
        "learning_rule_reject":          ("POST",  "/api/technology/learning-rules/{entity_id}/reject",                ["entity_id"]),
        "correction_record":             ("POST",  "/api/technology/corrections",                                      []),
        "process_plan_draft_from_document": ("POST", "/api/technology/process-plans/draft-from-document",             []),
    },
    "analytics": {
        "dashboard_today":       ("GET",   "/api/dashboard/today",            []),
        "table_query":           ("POST",  "/api/tables/query",                []),
        "table_export_excel":    ("POST",  "/api/tables/export/excel",         []),
        "table_export_1c":       ("POST",  "/api/tables/export/1c",            []),
        "table_import_excel":    ("POST",  "/api/tables/import/excel",         []),
        "table_apply_diff":      ("POST",  "/api/tables/apply-diff",           []),
        "table_inline_edit":     ("POST",  "/api/tables/inline-edit",          []),
        "table_list_views":      ("GET",   "/api/tables/views",                []),
        "table_create_view":     ("POST",  "/api/tables/views",                []),
        "compare_list":          ("GET",   "/api/compare",                     []),
        "compare_create":        ("POST",  "/api/compare",                     []),
        "compare_get":           ("GET",   "/api/compare/{entity_id}",         ["entity_id"]),
        "compare_align":         ("POST",  "/api/compare/{entity_id}/align",   ["entity_id"]),
        "compare_decide":        ("POST",  "/api/compare/{entity_id}/decide",  ["entity_id"]),
        "compare_summary":       ("GET",   "/api/compare/{entity_id}/summary", ["entity_id"]),
        "collection_list":       ("GET",   "/api/collections",                 []),
        "collection_create":     ("POST",  "/api/collections",                 []),
        "collection_get":        ("GET",   "/api/collections/{entity_id}",     ["entity_id"]),
        "collection_summarize":  ("POST",  "/api/collections/{entity_id}/summarize", ["entity_id"]),
        "collection_search":     ("GET",   "/api/collections/{entity_id}/search", ["entity_id"]),
        "collection_add_item":   ("POST",  "/api/collections/{entity_id}/items", ["entity_id"]),
        "collection_suggest":    ("GET",   "/api/collections/{entity_id}/suggest", ["entity_id"]),
        "collection_timeline":   ("GET",   "/api/collections/{entity_id}/timeline", ["entity_id"]),
        "collection_close":      ("POST",  "/api/collections/{entity_id}/close", ["entity_id"]),
        "calendar_events":       ("GET",   "/api/calendar/events",             []),
        "calendar_upcoming":     ("GET",   "/api/calendar/upcoming",           []),
        "calendar_create_reminder": ("POST", "/api/calendar/reminders",        []),
        "calendar_extract_dates": ("POST", "/api/calendar/extract-dates",      []),
        "calendar_generate_followup": ("POST", "/api/calendar/reminders/{entity_id}/generate-followup", ["entity_id"]),
        "auto_approval_list":    ("GET",   "/api/auto-approval-rules",         []),
        "auto_approval_create":  ("POST",  "/api/auto-approval-rules",         []),
        "auto_approval_check":   ("POST",  "/api/auto-approval-rules/check",   []),
    },
    "agent_control": {
        "task_create":         ("POST", "/api/agent/tasks",                    []),
        "task_propose":        ("POST", "/api/agent/tasks/propose",            []),
        "task_decide":         ("POST", "/api/agent/tasks/{entity_id}/decide", ["entity_id"]),
        "task_run":            ("POST", "/api/agent/tasks/{entity_id}/run",    ["entity_id"]),
        "capability_propose":  ("POST", "/api/agent/capabilities/propose",     []),
        "capability_status":   ("GET",  "/api/agent/capabilities/status",      []),
        "approval_list":       ("GET",  "/api/approvals/pending",              []),
        "approval_status":     ("GET",  "/api/approvals/{entity_id}",          ["entity_id"]),
        "config_status":       ("GET",  "/api/agent/control-plane/status",     []),
        # AI / auto-approval settings: read freely; changing them is gated.
        "ai_config_get":       ("GET",   "/api/ai/config",                     []),
        "ai_config_set":       ("PATCH", "/api/ai/config",                     []),
    },
    "image_studio": {
        "generate":       ("POST",  "/api/image-gen/generate",                  []),
        "list":           ("GET",   "/api/image-gen",                           []),
        "get":            ("GET",   "/api/image-gen/{generation_id}",           ["generation_id"]),
        "accept":         ("POST",  "/api/image-gen/{generation_id}/accept",    ["generation_id"]),
        "iterate":        ("POST",  "/api/image-gen/{generation_id}/iterate",   ["generation_id"]),
        "prompt_help":    ("POST",  "/api/image-gen/prompt-help",               []),
        "list_workflows": ("GET",   "/api/image-gen/workflows/list",            []),
    },
}


def capability_action_map() -> dict[str, list[str]]:
    """Action enum per capability — the single source of truth for tool schemas.

    The agent's tool catalog injects these as JSON-schema ``enum`` on the
    ``action`` field, so the model can only emit a valid action and the dispatcher
    never has to reject a guessed string. Drift is structurally impossible because
    both the schema and the routing read this same ``_DISPATCH`` table.
    """
    return {cap: sorted(actions.keys()) for cap, actions in _DISPATCH.items()}


# Capabilities handled by dedicated routes outside the generic _DISPATCH table.
_SPECIAL_CAPABILITIES = {"vault"}


def validate_capability_catalog() -> list[str]:
    """Fail-closed consistency check: capabilities.yml ↔ _DISPATCH.

    Returns a list of human-readable problems (empty = consistent). Run as a
    test and optionally at startup so the hand-curated manifest can never drift
    from the dispatcher's real routing table.
    """
    manifest = load_capability_manifest()
    problems: list[str] = []
    declared = {c.name for c in manifest.capabilities}

    # Every declared capability must be routable (or a known special route).
    for name in declared:
        if name not in _DISPATCH and name not in _SPECIAL_CAPABILITIES:
            problems.append(f"capability '{name}' declared in manifest but absent from _DISPATCH")

    # Every routable capability should be declared so the model can see it.
    for name in _DISPATCH:
        if name not in declared:
            problems.append(f"capability '{name}' in _DISPATCH but not declared in manifest")

    # Gate actions must reference real actions of their capability.
    for cap in manifest.capabilities:
        if cap.name in _SPECIAL_CAPABILITIES:
            continue
        actions = set(_DISPATCH.get(cap.name, {}).keys())
        for gate in cap.gate_actions:
            if gate not in actions:
                problems.append(
                    f"gate_action '{cap.name}.{gate}' has no matching action in _DISPATCH"
                )
    return problems


def _service_headers() -> dict:
    """Auth headers for internal service-to-service calls."""
    from app.config import settings
    if settings.agent_service_key:
        return {"X-API-Key": settings.agent_service_key}
    return {}


async def _proxy(
    method: str,
    path: str,
    path_params: list[str],
    body: dict,
    base_url: str,
) -> dict:
    """Interpolate path params, split remaining args into query/body, proxy request."""
    query: dict = {}
    payload: dict = {}

    for k, v in body.items():
        if k in path_params:
            path = path.replace(f"{{{k}}}", str(v))
        elif method == "GET":
            query[k] = v
        else:
            payload[k] = v

    url = base_url.rstrip("/") + path
    headers = _service_headers()
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            if method == "GET":
                resp = await client.get(url, params=query, headers=headers)
            elif method == "POST":
                resp = await client.post(url, json=payload, headers=headers)
            elif method == "PATCH":
                resp = await client.patch(url, json=payload, headers=headers)
            elif method == "DELETE":
                resp = await client.delete(url, headers=headers)
            else:
                return {"error": f"Unsupported method: {method}"}

        if resp.status_code < 400:
            try:
                data = resp.json()
                # Normalise bare list responses to {"items": [...], "total": N}
                if isinstance(data, list):
                    return {"items": data, "total": len(data)}
                return data
            except Exception:
                return {"text": resp.text[:2000]}
        return {"error": f"HTTP {resp.status_code}", "detail": resp.text[:300]}
    except Exception as exc:
        return {"error": str(exc)}


def _validate_capability_contract(
    capability_name: str,
    action: str,
    path_params: list[str],
    body: dict,
) -> None:
    """Fail closed when dispatcher and the reviewed capability contract drift."""
    try:
        manifest = load_capability_manifest()
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Capability contract unavailable: {exc}",
        ) from exc

    capability = manifest.by_name.get(capability_name)
    if capability is None:
        raise HTTPException(
            status_code=503,
            detail={
                "error_code": "contract_unavailable",
                "message": f"Capability '{capability_name}' is not declared in the active manifest",
            },
        )

    if (
        classify_capability_action_risk(action) == "high"
        and action not in capability.gate_actions
    ):
        raise HTTPException(
            status_code=503,
            detail={
                "error_code": "gate_missing",
                "message": (
                    f"Risky action '{capability_name}.{action}' is blocked because "
                    "it is missing from gate_actions"
                ),
            },
        )

    missing = [
        name
        for name in path_params
        if name not in body or body[name] is None or str(body[name]).strip() == ""
    ]
    if missing:
        raise HTTPException(
            status_code=422,
            detail={
                "error_code": "missing_args",
                "message": f"Missing required path parameters: {missing}",
                "missing": missing,
            },
        )


def _capability_gate_actions(capability_name: str) -> set[str]:
    """Return approval-gated actions from the reviewed manifest."""
    manifest = load_capability_manifest()
    capability = manifest.by_name.get(capability_name)
    if capability is None:
        return set()
    return set(capability.gate_actions or [])


def _request_has_internal_approval(request: Request) -> bool:
    """Accept approval proof only from the internal agent transport.

    In production the service key is the trust boundary. The internal marker is
    kept for local/dev environments where AGENT_SERVICE_KEY may be empty.
    """
    if request.headers.get("X-Agent-Approval") != "granted":
        return False
    if settings.agent_service_key:
        return request.headers.get("X-API-Key") == settings.agent_service_key
    return request.headers.get("X-Internal-Agent") == "1"


def _enforce_capability_policy(
    capability_name: str,
    action: str,
    body: dict,
    request: Request,
) -> None:
    """Apply the same risk/approval policy at the HTTP dispatcher boundary."""
    config = get_builtin_agent_config()
    gate_actions = _capability_gate_actions(capability_name)
    approval_gates = set(config.approval_gates)

    if action in gate_actions:
        approval_gates.add(capability_name)
        if not _request_has_internal_approval(request):
            raise HTTPException(
                status_code=423,
                detail={
                    "error_code": "approval_required",
                    "message": (
                        f"Action '{capability_name}.{action}' requires an "
                        "approved internal agent execution."
                    ),
                    "capability": capability_name,
                    "action": action,
                    "required_approval": True,
                },
            )

    decision = check_tool_execution(
        skill_name=capability_name,
        args={"action": action, **body},
        config=config,
        approval_gates=approval_gates,
    )
    if not decision.allowed:
        raise HTTPException(
            status_code=403,
            detail={
                "error_code": "policy_blocked",
                "message": decision.reason,
                "capability": capability_name,
                "action": action,
                "risk_level": decision.risk_level,
                "required_approval": decision.required_approval,
            },
        )


@router.post("/cap/vault")
async def dispatch_vault(request: Request) -> JSONResponse:
    """Vault capability: retrieve paginated data from a previous large tool result.

    The agent calls this when it needs data beyond the 3-item preview in the
    compact envelope. Prefer workspace.* for display — vault is for iterating.
    """
    from app.ai.turn_vault import vault_get
    try:
        body: dict = await request.json()
    except Exception:
        body = {}
    vault_ref = body.get("vault_ref") or ""
    if not vault_ref:
        raise HTTPException(status_code=400, detail="'vault_ref' is required")
    offset = int(body.get("offset") or 0)
    limit = min(int(body.get("limit") or 20), 100)
    result = await vault_get(vault_ref, offset=offset, limit=limit)
    if result is None:
        raise HTTPException(status_code=404, detail="Vault ref expired or not found (TTL 15 min)")
    return JSONResponse(content=result)


@router.post("/cap/{capability_name}")
async def dispatch_capability(capability_name: str, request: Request) -> JSONResponse:
    """Route a capability call to the appropriate backend endpoint."""
    actions = _DISPATCH.get(capability_name)
    if actions is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error_code": "unknown_capability",
                "message": f"Unknown capability: {capability_name}",
                "available": sorted(_DISPATCH.keys()),
            },
        )

    try:
        body: dict = await request.json()
    except Exception:
        body = {}

    action = body.pop("action", None)
    if not action:
        raise HTTPException(
            status_code=400,
            detail={
                "error_code": "missing_action",
                "message": "'action' field is required",
                "available": sorted(actions.keys()),
            },
        )

    route = actions.get(action)
    if route is None:
        raise HTTPException(
            status_code=400,
            detail={
                "error_code": "unknown_action",
                "message": f"Unknown action '{action}' for capability '{capability_name}'.",
                "available": sorted(actions.keys()),
            },
        )

    method, path_tpl, path_params = route
    _validate_capability_contract(capability_name, action, path_params, body)
    from app.ai.gateway_config import gateway_config
    base_url = gateway_config.backend_url

    # Flatten nested 'filters' and 'body' into top-level args for proxying
    if "filters" in body and isinstance(body["filters"], dict):
        body.update(body.pop("filters"))
    if "body" in body and isinstance(body["body"], dict):
        body.update(body.pop("body"))

    _enforce_capability_policy(capability_name, action, body, request)

    result = await _proxy(method, path_tpl, path_params, body, base_url)
    return JSONResponse(content=result)
