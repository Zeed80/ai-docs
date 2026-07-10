"""Line-class hypothesis resolution + symbol detection (Ф4.3)."""

from __future__ import annotations

from app.ai.cad_hypothesis import apply_line_hypotheses, resolve_line_hypotheses
from app.ai.cad_ir import CadIR, SourceInfo
from app.ai.cad_ir.schema import Circle, Point, Polyline, Segment
from app.ai.vlm_dimensions import _parse_line_response


def _ir(entities) -> CadIR:
    return CadIR(source=SourceInfo(image_width=400, image_height=300), entities=entities)


# ── _parse_line_response ─────────────────────────────────────────────────────


def test_parse_line_response_basic():
    raw = '{"line_readings": [{"line_class": "axis", "confidence": 0.8}], "symbol": null}'
    out = _parse_line_response(raw)
    assert out["line_readings"] == [{"line_class": "axis", "confidence": 0.8}]
    assert out["symbol"] is None


def test_parse_line_response_sorts_by_confidence_and_keeps_symbol():
    raw = """{"line_readings": [
        {"line_class": "hidden", "confidence": 0.3},
        {"line_class": "axis", "confidence": 0.7}
    ], "symbol": {"kind": "roughness", "text": "Ra 1.6", "confidence": 0.9}}"""
    out = _parse_line_response(raw)
    assert [r["line_class"] for r in out["line_readings"]] == ["axis", "hidden"]
    assert out["symbol"] == {"kind": "roughness", "text": "Ra 1.6", "confidence": 0.9}


def test_parse_line_response_ignores_none_kind_symbol():
    raw = '{"line_readings": [], "symbol": {"kind": "none", "confidence": 0.0}}'
    out = _parse_line_response(raw)
    assert out["symbol"] is None


def test_parse_line_response_malformed_returns_empty():
    assert _parse_line_response("not json") == {"line_readings": [], "symbol": None}


# ── apply_line_hypotheses ────────────────────────────────────────────────────


def test_apply_line_hypotheses_sets_leading_and_geometric_alternatives():
    seg = Segment(p1=Point(x=0, y=0), p2=Point(x=100, y=0), line_class="contour", confidence=0.5)
    apply_line_hypotheses(seg, {
        "line_readings": [
            {"line_class": "axis", "confidence": 0.65},
            {"line_class": "hidden", "confidence": 0.35},
        ],
        "symbol": {"kind": "thread", "text": "M12", "confidence": 0.8},
    })
    assert seg.line_class == "axis"
    assert seg.origin == "vlm"
    assert len(seg.alternatives) == 1
    assert seg.alternatives[0].entity == {"line_class": "hidden"}
    assert any("vlm_symbol:thread=M12" in e for e in seg.evidence)


# ── resolve_line_hypotheses ──────────────────────────────────────────────────


def test_axis_through_circle_center_is_promoted():
    circle = Circle(center=Point(x=200, y=150), radius=50)
    axis = Segment(
        p1=Point(x=140, y=150), p2=Point(x=260, y=150),  # passes exactly through center
        line_class="axis", confidence=0.55,
        alternatives=[__import__("app.ai.cad_ir.schema", fromlist=["Alternative"]).Alternative(
            entity={"line_class": "hidden"}, p=0.45
        )],
    )
    ir = _ir([circle, axis])
    resolve_line_hypotheses(ir)
    assert axis.assurance == "constraint_validated"


def test_axis_reading_without_nearby_circle_stays_ambiguous():
    from app.ai.cad_ir.schema import Alternative

    seg = Segment(
        p1=Point(x=10, y=10), p2=Point(x=100, y=10), line_class="axis", confidence=0.55,
        alternatives=[Alternative(entity={"line_class": "hidden"}, p=0.45)],
    )
    ir = _ir([seg])  # no circle anywhere -> no geometric confirmation for "axis"
    resolve_line_hypotheses(ir)
    assert seg.assurance != "constraint_validated"
    pending = [r for r in ir.review if not r.resolved]
    assert any(r.entity_id == seg.id and r.reason == "unresolved_hypothesis" for r in pending)


def test_entities_without_geometric_alternatives_are_skipped():
    seg = Segment(p1=Point(x=0, y=0), p2=Point(x=10, y=0))  # no alternatives at all
    ir = _ir([seg])
    resolve_line_hypotheses(ir)  # must not raise
    assert seg.assurance == "constraint_validated" or seg.assurance == "inferred"  # untouched either way
    assert not any(r.entity_id == seg.id for r in ir.review)


def test_polyline_with_geometric_alternatives_handled():
    from app.ai.cad_ir.schema import Alternative

    pln = Polyline(
        points=[Point(x=0, y=0), Point(x=10, y=0), Point(x=10, y=10)],
        line_class="thin", confidence=0.9,
        alternatives=[Alternative(entity={"line_class": "hatch"}, p=0.1)],
    )
    ir = _ir([pln])
    resolve_line_hypotheses(ir)  # no axis-bonus path applies to polylines; just must not crash
    assert pln.assurance == "constraint_validated"  # 0.9 vs 0.1 is decisive on raw confidence alone
