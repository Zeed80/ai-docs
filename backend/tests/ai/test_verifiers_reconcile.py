"""Согласование: когда замер по листу принимается сам, а когда — решение человеку."""

from __future__ import annotations

from app.ai.cad_recognize.verifiers.reconcile import (
    apply_reconciliation,
    reconcile,
    sheet_numbers,
)


def _spec(dimensions: list[str]) -> dict:
    return {
        "main_view": {
            "outer": [
                {"diameter_mm": 25.0, "length_mm": 12.0, "id": "s0"},
                {"diameter_mm": 40.0, "length_mm": 30.0, "id": "s1"},
                {"diameter_mm": 35.0, "length_mm": 98.0, "id": "s2"},
            ]
        },
        "dimensions": [{"value": text} for text in dimensions],
    }


def _report(items: list[dict]) -> dict:
    return {"items": items, "notes": []}


def _step(index: int, read: dict, measured: dict, status: str = "refuted") -> dict:
    return {
        "kind": "shaft_step",
        "path": f"main_view.outer[{index}]",
        "feature_id": f"s{index}",
        "read": read,
        "measured": measured,
        "status": status,
        "reason": "",
        "tolerance_mm": {"diameter": 0.3, "length": 0.5},
    }


def test_sheet_numbers_are_parsed_from_the_callouts_the_reader_wrote():
    numbers = sheet_numbers(
        _spec(["Ø80js6", "M24×1,5", "2 отв. Ø5.5", "1. Ø120 — наружный диаметр 999", "R5"])
    )

    assert numbers == [1.5, 5.0, 5.5, 24.0, 80.0, 120.0]


def test_a_measurement_matching_a_callout_is_adopted_when_the_read_value_is_not_on_the_sheet():
    """Живой shaft-1: длина 98 (на листе её нет) → замер 80,035 ≈ «80» на листе."""
    spec = _spec(["12", "30", "80", "Ø25", "Ø40", "Ø35"])
    report = _report(
        [
            _step(
                2,
                {"diameter_mm": 35.0, "length_mm": 98.0},
                {"diameter_mm": 35.0, "length_mm": 80.035},
            )
        ]
    )
    decisions = reconcile(spec, report)

    assert [(d["action"], d["field"], d["value"]) for d in decisions] == [
        ("adopt", "length_mm", 80.0)
    ]
    fixed, updated = apply_reconciliation(spec, report, decisions)
    assert fixed["main_view"]["outer"][2]["length_mm"] == 80.0
    assert spec["main_view"]["outer"][2]["length_mm"] == 98.0  # исходный спек не тронут
    assert fixed["provenance"]["main_view.outer[2].length_mm"]["origin"] == "sheet_measurement"
    assert fixed["provenance"]["main_view.outer[2].length_mm"]["read_mm"] == 98.0
    item = updated["items"][0]
    assert item["status"] == "confirmed" and item["reconciled"]["length_mm"]["read"] == 98.0
    assert item["read"]["length_mm"] == 80.0


def test_an_ambiguous_case_goes_to_a_person():
    """Прочитанное Ø40 тоже есть на листе (у соседней ступени), замер 27,9 без надписи."""
    spec = _spec(["12", "30", "Ø25", "Ø40", "Ø35"])
    report = _report(
        [
            _step(
                1,
                {"diameter_mm": 40.0, "length_mm": 30.0},
                {"diameter_mm": 27.917, "length_mm": 29.95},
            )
        ]
    )
    decisions = reconcile(spec, report)

    assert [d["action"] for d in decisions] == ["ask_human"]
    assert "тоже есть на листе" in decisions[0]["reason"]
    fixed, updated = apply_reconciliation(spec, report, decisions)
    assert fixed["main_view"]["outer"][1]["diameter_mm"] == 40.0
    assert updated["items"][0]["status"] == "refuted"


def test_positions_and_confirmed_items_are_never_adopted():
    spec = _spec(["12", "30", "80"])
    keyway = {
        "kind": "keyway",
        "path": "main_view.keyways[0]",
        "read": {"axial_start_mm": 16.0, "length_mm": 19.0, "width_mm": 8.0},
        "measured": {"axial_start_mm": 12.0, "length_mm": 19.0, "width_mm": 8.0},
        "status": "refuted",
        "tolerance_mm": {"length": 0.5, "width": 0.3},
    }
    confirmed = _step(
        0,
        {"diameter_mm": 25.0, "length_mm": 12.0},
        {"diameter_mm": 25.0, "length_mm": 12.0},
        "confirmed",
    )

    assert reconcile(spec, _report([keyway, confirmed])) == []
