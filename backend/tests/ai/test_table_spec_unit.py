"""TableSpec engine: field resolution, NL edit commands, patch semantics (pure)."""

from __future__ import annotations

import pytest

from app.domain import table_spec as ts


def _spec() -> ts.TableSpec:
    return ts.TableSpec(
        source="invoices",
        columns=[
            ts.ColumnSpec(field="supplier_name"),
            ts.ColumnSpec(field="invoice_number"),
            ts.ColumnSpec(field="invoice_date"),
            ts.ColumnSpec(field="items_list"),
            ts.ColumnSpec(field="total_amount"),
        ],
    )


# ── Field resolution (declension-tolerant) ─────────────────────────────────────


@pytest.mark.parametrize(
    "token,expected",
    [
        ("ндс", "tax_amount"),
        ("НДС", "tax_amount"),
        ("сумма", "total_amount"),
        ("суммой", "total_amount"),
        ("сумме", "total_amount"),
        ("поставщик", "supplier_name"),
        ("поставщику", "supplier_name"),
        ("дата счета", "invoice_date"),
        ("перечень товаров", "items_list"),
        ("номенклатура", "items_list"),
        ("номер", "invoice_number"),
    ],
)
def test_resolve_field_invoices(token, expected):
    fd = ts.resolve_field(ts.SOURCES["invoices"], token)
    assert fd is not None and fd.key == expected


def test_resolve_field_rejects_unknown_and_collisions():
    src = ts.SOURCES["invoices"]
    assert ts.resolve_field(src, "погода") is None
    # «номер» must not collide with «номенклатура» and vice versa.
    assert ts.resolve_field(src, "номер").key == "invoice_number"
    assert not ts._words_match("номер", "номенклатура")


# ── NL command parser ──────────────────────────────────────────────────────────


def test_parse_add_column_with_position():
    cmd = ts.parse_patch_command("добавь столбец с ндс перед суммой", _spec())
    assert cmd is not None
    op = cmd.ops[0]
    assert op.op == "add_column" and op.field == "tax_amount"
    assert op.before == "total_amount"


def test_parse_add_column_after():
    cmd = ts.parse_patch_command("добавь колонку валюта после суммы", _spec())
    assert cmd is not None
    op = cmd.ops[0]
    assert op.op == "add_column" and op.field == "currency" and op.after == "total_amount"


def test_parse_remove_and_sort():
    cmd = ts.parse_patch_command("убери столбец перечень товаров", _spec())
    assert cmd.ops[0].op == "remove_column" and cmd.ops[0].field == "items_list"

    cmd = ts.parse_patch_command("отсортируй по сумме по убыванию", _spec())
    assert cmd.ops[0].op == "set_sort"
    assert cmd.ops[0].field == "total_amount" and cmd.ops[0].dir == "desc"

    cmd = ts.parse_patch_command("отсортируй по дате", _spec())
    assert cmd.ops[0].field == "invoice_date" and cmd.ops[0].dir == "asc"


def test_parse_only_filter():
    cmd = ts.parse_patch_command("покажи только фрезы диаметра 5", _spec())
    assert [op.op for op in cmd.ops] == ["clear_filters", "add_filter"]
    flt = cmd.ops[1].filter
    assert flt.op == "smart" and "фрезы" in str(flt.value)


def test_parse_returns_none_for_non_commands():
    assert ts.parse_patch_command("как дела?", _spec()) is None
    assert ts.parse_patch_command("добавь столбец с погодой", _spec()) is None


# ── Patch semantics ────────────────────────────────────────────────────────────


def test_apply_patch_insert_before_and_idempotent():
    spec = _spec()
    op = ts.PatchOp(op="add_column", field="tax_amount", before="total_amount")
    out = ts.apply_patch(spec, [op])
    fields = [c.field for c in out.columns]
    assert fields.index("tax_amount") == fields.index("total_amount") - 1
    # Re-adding the same column is a no-op, not a duplicate.
    again = ts.apply_patch(out, [op])
    assert [c.field for c in again.columns] == fields


def test_apply_patch_move_and_remove():
    spec = _spec()
    out = ts.apply_patch(
        spec, [ts.PatchOp(op="move_column", field="total_amount", before="supplier_name")]
    )
    assert out.columns[0].field == "total_amount"
    out = ts.apply_patch(out, [ts.PatchOp(op="remove_column", field="items_list")])
    assert "items_list" not in [c.field for c in out.columns]
    with pytest.raises(ValueError):
        ts.apply_patch(out, [ts.PatchOp(op="remove_column", field="items_list")])


def test_apply_patch_rejects_unknown_field():
    with pytest.raises(ValueError):
        ts.apply_patch(_spec(), [ts.PatchOp(op="add_column", field="nope")])


# ── Spec validation & smart tokens ─────────────────────────────────────────────


def test_validate_spec_reports_unknown_fields():
    spec = ts.TableSpec(source="invoices", columns=[ts.ColumnSpec(field="nope")])
    assert any("nope" in p for p in ts.validate_spec(spec))
    assert ts.validate_spec(_spec()) == []
    assert any("unknown source" in p for p in ts.validate_spec(ts.TableSpec(source="x")))


def test_smart_tokens_stem_words_and_keep_numbers():
    tokens = ts._smart_tokens("фрезы для диаметра 5")
    assert "5" in tokens
    assert any(tok.startswith("фрез") for tok in tokens)
    # Qualifier words («для», «диаметра») are dropped.
    assert all(not tok.startswith("диам") and tok != "для" for tok in tokens)
