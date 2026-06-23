"""Recipe skills: parameterization and slot resolution (pure functions)."""

from __future__ import annotations

from app.ai import recipes


def test_extract_entities_supplier_and_date():
    entities = recipes.extract_entities(
        'выведи счета поставщика «Ромашка» с 01.05.2026 в таблицу'
    )
    assert entities.get("supplier_name") == "Ромашка"
    assert entities.get("date_1") == "01.05.2026"


def test_parameterize_steps_replaces_literals_with_slots():
    steps = [
        {
            "capability": "invoices",
            "action": "list",
            "args_template": {"action": "list", "filters": {"supplier_query": "Ромашка"}},
        },
        {
            "capability": "workspace",
            "action": "publish",
            "args_template": {"action": "publish", "canvas_id": "agent:invoices"},
        },
    ]
    templated, slots = recipes.parameterize_steps(
        steps, 'покажи счета поставщика «Ромашка»'
    )
    assert (
        templated[0]["args_template"]["filters"]["supplier_query"]
        == "{{user.supplier_name}}"
    )
    assert "supplier_name" in slots
    assert slots["supplier_name"]["example"] == "Ромашка"
    # Untouched literals survive.
    assert templated[1]["args_template"]["canvas_id"] == "agent:invoices"


def test_resolve_slots_roundtrip():
    param_slots = {"supplier_name": {"source": "supplier_name", "example": "Ромашка"}}
    resolved = recipes.resolve_slots(param_slots, 'счета поставщика «Лютик» в таблицу')
    assert resolved == {"supplier_name": "Лютик"}
    # Unresolvable slot → None (recipe must not replay with missing params).
    assert recipes.resolve_slots(param_slots, "выведи все счета") is None
    # No declared slots → empty mapping, replayable as-is.
    assert recipes.resolve_slots(None, "что угодно") == {}


def test_render_args_substitutes_nested():
    args = recipes.render_args(
        {"filters": {"supplier_query": "{{user.supplier_name}}", "limit": 50}},
        {"supplier_name": "Лютик"},
    )
    assert args == {"filters": {"supplier_query": "Лютик", "limit": 50}}


def test_render_args_keeps_unknown_slots_intact():
    args = recipes.render_args({"q": "{{user.unknown}}"}, {})
    assert args == {"q": "{{user.unknown}}"}


# ── Table macros: which table actions may enter recipes ──────────────────────


def test_table_macro_gate_contract():
    """Spreadsheet/table chains are recordable; the writeback edit is gated."""
    gates = recipes._gate_actions_map()
    # The approval-gated cell edit must never enter a recipe.
    assert "spec_table_cell_edit" in gates.get("workspace", set())
    # Sheet building blocks are recordable; only delete is gated (out of recipes).
    assert gates.get("sheets", set()) == {"delete"}
    assert "create" not in gates.get("sheets", set())
    assert "patch_cells" not in gates.get("sheets", set())
    # Read/build/export table actions are not gated either.
    assert "table_query" not in gates.get("analytics", set())
    assert "table_export_excel" not in gates.get("analytics", set())


def test_sheet_macro_chain_dataflow_sheet_id():
    """A create→patch sheet macro links sheet_id as a step reference (reproducible)."""
    steps = [
        {"capability": "sheets", "action": "create",
         "args_template": {"action": "create", "title": "Отчёт"}},
        {"capability": "sheets", "action": "patch_cells",
         "args_template": {"action": "patch_cells", "sheet_id": "SID",
                           "edits": [{"row": 0, "col": "A", "value": 1}]}},
    ]
    step_results = [{"sheet_id": "SID"}, {"status": "patched"}]
    templated, _slots = recipes.parameterize_steps(
        steps, "сделай лист отчёта", step_results
    )
    # The runtime sheet_id became a data-flow reference, not an orphan literal.
    assert templated[1]["args_template"]["sheet_id"] == "{{step.0.sheet_id}}"
    assert recipes.is_reproducible(templated, "сделай лист отчёта")
