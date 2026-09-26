"""Элемент по сечению без следа секущей плоскости — снимается, но только при
полном детекторе следов на этом листе (живой turned_multiaxis-1)."""

from __future__ import annotations

from types import SimpleNamespace

import app.ai.cad_recognize.verifiers.plate_frame as plate_frame
import app.ai.cad_recognize.verifiers.section_outline as section_outline
from app.ai.cad_recognize.verifiers.reconcile import apply_reconciliation, reconcile
from app.ai.cad_recognize.verifiers.stage import _absent_without_trace

BODY = {
    "outer": [{"diameter_mm": 45.0, "length_mm": 18.0}, {"diameter_mm": 40.0, "length_mm": 50.0}],
    "placed_features": [
        {"kind": "pocket", "origin_mm": [14.1421, 14.1421, 53.5]},
        {"kind": "hole", "origin_mm": [20.0, 0.0, 43.7]},
    ],
}
FRAME = SimpleNamespace(mm_per_px=0.34, bbox_px=(0, 0, 100, 100))
PROFILE = SimpleNamespace(line_px=6.0)


def _items(first_status: str = "confirmed") -> list[dict]:
    return [
        {"kind": "placed_feature", "path": "main_view.placed_features[0]", "status": first_status},
        {
            "kind": "placed_feature",
            "path": "main_view.placed_features[1]",
            "status": "unmeasurable",
            "reason": "Ø на сечении не измерить",
        },
    ]


def _run(monkeypatch, found: list[float], items: list[dict], sections: int = 1) -> dict:
    monkeypatch.setattr(plate_frame, "_ink", lambda gray: gray)
    monkeypatch.setattr(
        section_outline,
        "locate_sections",
        lambda ink, bbox, mpp, diameters: [{"step_diameter_mm": 40.0}] * sections,
    )
    report: dict = {"items": items}
    _absent_without_trace(None, FRAME, PROFILE, BODY, items, report, traces=found)
    return report


def test_a_feature_off_every_trace_is_refuted_and_dropped(monkeypatch):
    report = _run(monkeypatch, [53.4], _items())

    hole = report["items"][1]
    assert hole["status"] == "refuted" and hole["absent"], hole
    spec = {"main_view": {"placed_features": [dict(f) for f in BODY["placed_features"]]}}
    decisions = reconcile(spec, report)
    assert [(d["field"], d["action"]) for d in decisions] == [("exists", "drop")]
    spec2, _report = apply_reconciliation(spec, report, decisions)
    assert [f["kind"] for f in spec2["main_view"]["placed_features"]] == ["pocket"]
    assert any("снят" in note for note in spec2["unresolved"])


def test_an_incomplete_trace_detector_does_not_drop_anything(monkeypatch):
    """Найденный на сечении элемент не стоит на найденном следе — детектор
    неполон, и отсутствие следа уликой не считается."""
    report = _run(monkeypatch, [120.0], _items())

    assert report["items"][1]["status"] == "unmeasurable"


def test_a_spare_section_of_the_step_keeps_the_feature(monkeypatch):
    """Эталон 30 валов: 3 настоящих элемента снимались — их следы детектор
    пропустил. Сечение ступени сверх занятых найденными — у элемента может
    быть своё: не снимать."""
    report = _run(monkeypatch, [53.4], _items(), sections=2)

    assert report["items"][1]["status"] == "unmeasurable"


def test_no_section_of_the_step_found_is_no_evidence(monkeypatch):
    report = _run(monkeypatch, [53.4], _items(), sections=0)

    assert report["items"][1]["status"] == "unmeasurable"
