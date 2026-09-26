"""Паз, которого на листе нет: ни капсулы на главном виде, ни следа секущей
на его пролёте — снимается (живые turned_multiaxis-0/1: ридер выдал за паз
числа лыски и ступени, «паз 33…48 × 8» держал сборку)."""

from __future__ import annotations

from types import SimpleNamespace

from app.ai.cad_recognize.verifiers.keyway import NOT_FOUND_REASON
from app.ai.cad_recognize.verifiers.reconcile import apply_reconciliation, reconcile
from app.ai.cad_recognize.verifiers.stage import _keyways_without_trace

FRAME = SimpleNamespace(mm_per_px=0.34)
PROFILE = SimpleNamespace(line_px=6.0)


def _body() -> dict:
    return {
        "outer": [
            {"diameter_mm": 45.0, "length_mm": 18.0},
            {"diameter_mm": 25.0, "length_mm": 15.0},
            {"diameter_mm": 40.0, "length_mm": 35.0},
        ],  # fmt: skip
        "placed_features": [{"kind": "pocket", "origin_mm": [14.1421, 14.1421, 53.5]}],
        "keyways": [
            {"axial_start_mm": 33.0, "length_mm": 15.0, "width_mm": 8.0, "depth_mm": 1.6},
        ],
    }


def _report(flat_status: str = "confirmed", keyway_reason: str = NOT_FOUND_REASON) -> dict:
    return {
        "items": [
            {
                "kind": "placed_feature",
                "path": "main_view.placed_features[0]",
                "status": flat_status,
            },
            {
                "kind": "keyway",
                "path": "main_view.keyways[0]",
                "status": "unmeasurable",
                "measured": {},
                "reason": keyway_reason,
            },
        ],
        "notes": [],
    }


def test_a_keyway_with_no_contour_and_no_free_trace_is_refuted_and_dropped():
    body, report = _body(), _report()

    _keyways_without_trace(FRAME, PROFILE, body, report, [53.37])

    item = report["items"][1]
    assert item["status"] == "refuted" and item.get("absent"), item
    spec = {
        "main_view": body,
        "unresolved": ["шпоночный паз 0: глубина 1.6 мм при Ø25 и ничем не подтверждена"],
        "provenance": {"main_view.keyways[0].width_mm": {"origin": "reader"}},
    }
    decisions = [d for d in reconcile(spec, report) if d["action"] == "drop"]
    fixed, fixed_report = apply_reconciliation(spec, report, decisions)

    assert fixed["main_view"]["keyways"] == []
    assert not any(note.startswith("шпоночный паз 0:") for note in fixed["unresolved"])
    assert any("снят" in note for note in fixed_report["notes"])
    assert not any("снят" in note for note in fixed["unresolved"])
    assert "main_view.keyways[0].width_mm" not in fixed["provenance"]


def test_a_free_trace_on_the_keyway_span_keeps_it():
    """Сечение на пролёте паза — его сечение: капсулу мог скрыть размер."""
    body, report = _body(), _report()

    _keyways_without_trace(FRAME, PROFILE, body, report, [40.0, 53.37])

    assert report["items"][1]["status"] == "unmeasurable"


def test_without_a_found_element_the_trace_detector_is_not_trusted():
    body, report = _body(), _report(flat_status="unmeasurable")

    _keyways_without_trace(FRAME, PROFILE, body, report, [53.37])

    assert report["items"][1]["status"] == "unmeasurable"


def test_a_coarse_sheet_keyway_is_never_dropped():
    body, report = _body(), _report(keyway_reason="лист слишком грубый: основная линия 3.0 px")

    _keyways_without_trace(FRAME, PROFILE, body, report, [53.37])

    assert report["items"][1]["status"] == "unmeasurable"
