"""The generic invoice pivot is the data-driven path for ANY grouped/aggregated
items table — it must stay wired so the agent never falls back to hand-building
tables in the LLM (which silently drops rows/groups)."""

from app.api.capability_router import _DISPATCH


def test_invoice_pivot_capability_registered():
    assert _DISPATCH["workspace"]["invoice_pivot"][1] == (
        "/api/workspace/agent/invoices/pivot-table"
    )


def test_pivot_supports_all_dimensions():
    from app.api.workspace import _PIVOT_DIMENSIONS

    for dim in ("supplier", "invoice", "item", "month", "currency", "status"):
        assert dim in _PIVOT_DIMENSIONS
