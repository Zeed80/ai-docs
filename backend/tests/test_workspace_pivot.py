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


def test_pivot_columns_are_selectable_and_named():
    from app.api.workspace import _resolve_pivot_columns

    # Caller-chosen columns with custom headers are honoured in order, incl. ИНН.
    spec = [
        {"header": "Поставщик", "expr": "supplier"},
        {"header": "ИНН", "expr": "supplier_inn"},
        {"header": "Товары", "expr": "items"},
        {"header": "Сумма", "expr": "sum"},  # alias → total_amount
    ]
    cols = _resolve_pivot_columns(spec, "Поставщик")
    assert [c[0] for c in cols] == ["Поставщик", "ИНН", "Товары", "Сумма"]
    assert cols[3][3] == "total_amount"  # alias resolved

    # No spec → sensible default set.
    default = _resolve_pivot_columns(None, "Месяц")
    assert default[0][0] == "Месяц" and [c[3] for c in default][1:] == [
        "invoice_count", "items", "total_amount"
    ]

    # Unknown expr is dropped, not crashed.
    only_bad = _resolve_pivot_columns([{"header": "X", "expr": "nope"}], "Поставщик")
    assert only_bad[0][3] == "group"  # fell back to default


def test_pivot_tolerates_messy_model_keys():
    from app.api.workspace import _normalize_pivot_expr, _resolve_pivot_columns

    # SQL-ish forms and synonyms the model tends to produce.
    assert _normalize_pivot_expr("SUM(amount)") == "total_amount"
    assert _normalize_pivot_expr("COUNT(item_id)") == "item_count"
    assert _normalize_pivot_expr("COUNT(invoice_id)") == "invoice_count"
    assert _normalize_pivot_expr("AVG(amount)") == "avg_amount"
    assert _normalize_pivot_expr("item_name") == "items"
    assert _normalize_pivot_expr("ИНН") == "supplier_inn"

    # Imperfect spec still yields a usable, dimension-led table.
    cols = _resolve_pivot_columns(
        [{"header": "ИНН", "expr": "supplier_inn"},
         {"header": "Товар", "expr": "item_name"},
         {"header": "Кол-во", "expr": "quantity"},
         {"header": "Цена", "expr": "price"}],
        "Поставщик",
    )
    assert [c[0] for c in cols] == ["Поставщик", "ИНН", "Товар", "Кол-во", "Цена"]
    assert cols[0][3] == "group"  # dimension guaranteed first
