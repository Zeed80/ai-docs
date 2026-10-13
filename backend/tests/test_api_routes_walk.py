"""Route introspection sees every route, flat or lazily included (FastAPI 0.141)."""

import uuid
from types import SimpleNamespace

from fastapi import APIRouter

from app.api_routes import _walk, api_routes


def test_a_lazily_included_router_is_walked_with_its_prefix():
    inner = APIRouter()

    @inner.post("/{invoice_id}/approve")
    async def approve(invoice_id: uuid.UUID):  # pragma: no cover - never called
        return None

    lazy = SimpleNamespace(
        original_router=inner, include_context=SimpleNamespace(prefix="/api/invoices")
    )
    paths = [path for path, _route in _walk([lazy])]
    assert paths == ["/api/invoices/{invoice_id}/approve"]


def test_the_live_app_shows_its_routes_and_their_types():
    """The walk that silently found 6 of ~1000 routes in the 0.141 image."""
    routes = dict(api_routes())
    assert len(routes) > 500
    approve = routes["/api/invoices/{invoice_id}/approve"]
    assert [p.field_info.annotation for p in approve.dependant.path_params] == [uuid.UUID]


def test_query_only_arguments_of_a_post_are_known():
    from app.ai.output_shapes import action_uuid_path_params
    from app.api.capability_router import _route_query_params

    assert action_uuid_path_params()["invoices.approve"] == frozenset({"invoice_id"})
    # documents.search posts q as a query parameter; sent as JSON it was
    # dropped and the search answered 422 "Укажите строку поиска" (E52).
    assert {"q", "limit"} <= _route_query_params("POST", "/api/search/documents")
    assert "force" in _route_query_params("POST", "/api/documents/{document_id}/extract")
