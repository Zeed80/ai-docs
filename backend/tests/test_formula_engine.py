"""Safe formula engine: computed columns, cell/range refs, guards."""

from __future__ import annotations

from app.domain.formula_engine import evaluate_sheet


def _cols(*specs):
    out = []
    for s in specs:
        out.append(s if isinstance(s, dict) else {"key": s, "header": s, "type": "text"})
    return out


def test_computed_column_by_key():
    cols = _cols("quantity", "unit_price", {"key": "amount", "formula": "quantity * unit_price"})
    rows = [{"quantity": 10, "unit_price": 800}, {"quantity": "2", "unit_price": "2 500"}]
    out = evaluate_sheet(cols, rows)
    assert out[0]["amount"] == 8000
    assert out[1]["amount"] == 5000  # "2 500" parsed as 2500


def test_cell_formula_and_ranges():
    cols = _cols("A", "B")
    rows = [
        {"A": 1, "B": "=A1*10"},
        {"A": 2, "B": "=A1+A2"},
        {"A": 3, "B": "=SUM(A1:A3)"},
    ]
    out = evaluate_sheet(cols, rows)
    assert out[0]["B"] == 10
    assert out[1]["B"] == 3
    assert out[2]["B"] == 6


def test_functions_round_if_average():
    cols = _cols("x", {"key": "r", "formula": "ROUND(x/3, 2)"})
    rows = [{"x": 10}]
    out = evaluate_sheet(cols, rows)
    assert out[0]["r"] == 3.33

    cols2 = _cols("v", {"key": "flag", "formula": "IF(v>100, 1, 0)"})
    out2 = evaluate_sheet(cols2, [{"v": 150}, {"v": 50}])
    assert out2[0]["flag"] == 1 and out2[1]["flag"] == 0


def test_div_zero_and_errors_are_contained():
    cols = _cols("a", {"key": "d", "formula": "a / 0"})
    out = evaluate_sheet(cols, [{"a": 5}])
    assert out[0]["d"] == "#DIV/0"


def test_no_code_execution():
    # Attribute access / calls to non-whitelisted names must not execute.
    cols = _cols({"key": "evil", "formula": "__import__('os')"})
    out = evaluate_sheet(cols, [{}])
    assert out[0]["evil"] == "#ERR"


def test_plain_values_pass_through():
    cols = _cols("name", "qty")
    rows = [{"name": "Болт", "qty": 100}]
    out = evaluate_sheet(cols, rows)
    assert out[0] == {"name": "Болт", "qty": 100}
