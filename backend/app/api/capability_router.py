"""Capability dispatcher — unified /api/agent/cap/{capability} endpoint.

Routes agent capability calls to the appropriate backend endpoints.
Each capability accepts an `action` field plus context parameters.
"""

from __future__ import annotations

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

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
        "invoice_table":             ("POST", "/api/workspace/agent/invoices/table",               []),
        "invoice_items_table":       ("POST", "/api/workspace/agent/invoices/items-table",         []),
        "invoice_items_grouped":     ("POST", "/api/workspace/agent/invoices/items-grouped-table", []),
        "invoice_items_by_supplier": ("POST", "/api/workspace/agent/invoices/items-by-supplier-table", []),
        "general":                   ("POST", "/api/workspace/agent/generated/general",            []),
        "supplier_lookup":           ("POST", "/api/workspace/agent/generated/supplier_lookup",    []),
        "verify":                    ("POST", "/api/workspace/agent/verify-block",                 []),
    },
    "search": {
        "hybrid":     ("POST", "/api/search/hybrid",       []),
        "nl":         ("POST", "/api/search/nl",           []),
        "nl_to_query": ("POST", "/api/search/nl-to-query", []),
        "explain":    ("POST", "/api/memory/explain",      []),
    },
    "memory": {
        "search":           ("POST", "/api/memory/search",                   []),
        "explain":          ("POST", "/api/memory/explain",                  []),
        "reindex":          ("POST", "/api/memory/reindex",                  []),
        "embeddings_stats": ("GET",  "/api/memory/embeddings/stats",         []),
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
        "compare_decide":        ("POST",  "/api/compare/{entity_id}/decide",  ["entity_id"]),
        "compare_summary":       ("GET",   "/api/compare/{entity_id}/summary", ["entity_id"]),
        "collection_list":       ("GET",   "/api/collections",                 []),
        "collection_create":     ("POST",  "/api/collections",                 []),
        "collection_get":        ("GET",   "/api/collections/{entity_id}",     ["entity_id"]),
        "collection_summarize":  ("POST",  "/api/collections/{entity_id}/summarize", ["entity_id"]),
        "calendar_events":       ("GET",   "/api/calendar/events",             []),
        "calendar_upcoming":     ("GET",   "/api/calendar/upcoming",           []),
        "calendar_create_reminder": ("POST", "/api/calendar/reminders",        []),
        "calendar_extract_dates": ("POST", "/api/calendar/extract-dates",      []),
    },
    "agent_control": {
        "task_create":         ("POST", "/api/agent/tasks",                    []),
        "capability_propose":  ("POST", "/api/agent/capabilities/propose",     []),
        "capability_status":   ("GET",  "/api/agent/capabilities/status",      []),
        "approval_list":       ("GET",  "/api/approvals/pending",              []),
        "approval_status":     ("GET",  "/api/approvals/{entity_id}",          ["entity_id"]),
        "config_status":       ("GET",  "/api/agent/control-plane/status",     []),
    },
}


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
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            if method == "GET":
                resp = await client.get(url, params=query)
            elif method == "POST":
                resp = await client.post(url, json=payload)
            elif method == "PATCH":
                resp = await client.patch(url, json=payload)
            elif method == "DELETE":
                resp = await client.delete(url)
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


@router.post("/cap/{capability_name}")
async def dispatch_capability(capability_name: str, request: Request) -> JSONResponse:
    """Route a capability call to the appropriate backend endpoint."""
    actions = _DISPATCH.get(capability_name)
    if actions is None:
        raise HTTPException(status_code=404, detail=f"Unknown capability: {capability_name}")

    try:
        body: dict = await request.json()
    except Exception:
        body = {}

    action = body.pop("action", None)
    if not action:
        raise HTTPException(status_code=400, detail="'action' field is required")

    route = actions.get(action)
    if route is None:
        available = sorted(actions.keys())
        raise HTTPException(
            status_code=400,
            detail=f"Unknown action '{action}' for capability '{capability_name}'. "
                   f"Available: {available}",
        )

    method, path_tpl, path_params = route
    from app.ai.gateway_config import gateway_config
    base_url = gateway_config.backend_url

    # Flatten nested 'filters' and 'body' into top-level args for proxying
    if "filters" in body and isinstance(body["filters"], dict):
        body.update(body.pop("filters"))
    if "body" in body and isinstance(body["body"], dict):
        body.update(body.pop("body"))

    result = await _proxy(method, path_tpl, path_params, body, base_url)
    return JSONResponse(content=result)
