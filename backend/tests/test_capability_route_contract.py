"""Every capability action must reach a real endpoint — and its declared
parameters must be accepted somewhere.

The catalog test next door checks capabilities.yml ↔ _DISPATCH (that the
advertised ACTIONS are routable). It says nothing about whether the routes
themselves exist, and that gap was real: 20 actions pointed at paths no
router serves (documents.search → /api/documents/search, normalization's whole
card/canonical family, all three table exports, …). The agent calling any of
them got a 404 — a silent dead end that looks like the model misbehaving.

Same failure shape as the dropped `supplier_query` (see
test_workspace_blocks.py::test_all_invoice_tables_accept_supplier_query):
a mismatch between what the agent is told it can do and what the backend
actually accepts, invisible until someone reads a live transcript.
"""

from __future__ import annotations

import re

from app.ai.capability_manifest import load_capability_manifest
from app.api.capability_router import _DISPATCH
from app.main import app

# Wrapper keys the dispatcher itself consumes or flattens before proxying.
_ENVELOPE_PARAMS = {"action", "reason", "idempotency_key", "filters", "body", "arguments"}


def _shape(path: str) -> str:
    """Path with parameter names erased: the dispatcher may call a path param
    entity_id where the endpoint calls it task_id — same route either way."""
    return re.sub(r"\{[^}]+\}", "{}", path or "")


def _routes() -> set[tuple[str, str]]:
    return {
        (method, _shape(getattr(route, "path", "")))
        for route in app.routes
        for method in (getattr(route, "methods", None) or [])
    }


def _accepted_fields(method: str, path_tpl: str) -> set[str] | None:
    want = _shape(path_tpl)
    for route in app.routes:
        if _shape(getattr(route, "path", "")) != want:
            continue
        if method not in (getattr(route, "methods", None) or []):
            continue
        fields: set[str] = set(getattr(route, "param_convertors", {}) or {})
        dependant = getattr(route, "dependant", None)
        if dependant is None:
            return fields
        for param in list(dependant.query_params) + list(dependant.path_params):
            # The wire name is the alias when one is declared (a parameter may
            # be renamed internally to avoid shadowing, as `query` → query_text).
            fields.add(param.name)
            alias = getattr(param, "alias", None)
            if alias:
                fields.add(alias)
        for param in dependant.body_params:
            # FastAPI's ModelField exposes the request model through
            # field_info.annotation (older builds used .type_).
            model = getattr(getattr(param, "field_info", None), "annotation", None) or getattr(
                param, "type_", None
            )
            if hasattr(model, "model_fields"):
                fields |= set(model.model_fields)
            else:
                fields.add(param.name)
        return fields
    return None


def test_every_capability_action_reaches_a_real_endpoint():
    routes = _routes()
    broken = [
        f"{capability}.{action} → {method} {path}"
        for capability, actions in _DISPATCH.items()
        for action, (method, path, _params) in actions.items()
        if (method, _shape(path)) not in routes
    ]
    assert not broken, "actions routed at non-existent endpoints:\n" + "\n".join(broken)


def test_every_declared_parameter_is_accepted_by_some_endpoint():
    """A parameter no endpoint accepts is dropped in silence — the exact way a
    per-supplier request ended up publishing every supplier's data."""
    manifest = load_capability_manifest()
    orphans: list[str] = []
    for capability, actions in _DISPATCH.items():
        declared = (
            set(
                (getattr(manifest.by_name.get(capability), "parameters", {}) or {}).get(
                    "properties", {}
                )
            )
            - _ENVELOPE_PARAMS
        )
        if not declared:
            continue
        accepted: set[str] = set()
        for action, (method, path, path_params) in actions.items():
            # Path params are interpolated into the URL by the dispatcher, so
            # they count as accepted even when the endpoint spells them
            # differently ({entity_id} in the template vs task_id in the route).
            accepted |= set(path_params)
            fields = _accepted_fields(method, path)
            if fields:
                accepted |= fields
        for name in sorted(declared - accepted):
            orphans.append(f"{capability}.{name}")
    assert not orphans, (
        "capability parameters no endpoint accepts (they vanish silently):\n" + "\n".join(orphans)
    )


def test_search_and_normalization_accept_the_capability_parameter_names():
    """Endpoints must accept the names the capability actually sends.

    Both live failures had the same shape: `documents.search` sent `query`
    while /api/search/documents required `q` (422), and
    `normalization.list_canonical_items` sent `query` while the endpoint read
    only `search` (whole catalog returned instead of a search). The contract
    test above cannot catch this on its own — it checks the capability's whole
    parameter set against the union of its endpoints — so pin the two names
    that a real turn depends on.
    """
    documents_search = _accepted_fields("POST", "/api/search/documents") or set()
    assert {"q", "query"} <= documents_search

    canonical = _accepted_fields("GET", "/api/normalization/canonical-items") or set()
    assert {"search", "query"} <= canonical


def test_query_only_parameters_are_routed_to_the_query_string():
    """POST arguments must land where the endpoint reads them.

    The dispatcher sent every non-path argument of a POST as JSON, so an
    endpoint declaring query-only scalars never saw them: documents.search
    arrived with an empty query (422), and force/received_by/batch_qty flags
    were silently ignored on classify/receive/bom_approve.
    """
    from app.api.capability_router import _route_query_params

    assert "q" in _route_query_params("POST", "/api/search/documents")
    assert "force" in _route_query_params("POST", "/api/documents/{document_id}/classify")
    assert "received_by" in _route_query_params("POST", "/api/invoices/{invoice_id}/receive")
    # A body-only endpoint must NOT be reclassified.
    assert _route_query_params("POST", "/api/tool-catalog/attach-web-catalog") == frozenset()
