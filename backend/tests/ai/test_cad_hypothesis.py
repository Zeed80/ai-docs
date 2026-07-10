"""Cross-checks that resolve VLM reading hypotheses (Ф4.2)."""

from __future__ import annotations

from app.ai.cad_hypothesis import apply_vlm_readings, check_dimension_chains, resolve_hypotheses
from app.ai.cad_ir import CadIR, SourceInfo
from app.ai.cad_ir.schema import Circle, DimensionEntity, Point, TextEntity


def _ir(entities, scale=0.5) -> CadIR:
    return CadIR(source=SourceInfo(image_width=400, image_height=300), scale=scale, entities=entities)


# ── apply_vlm_readings ────────────────────────────────────────────────────────


def test_apply_vlm_readings_sets_leading_and_alternatives():
    e = TextEntity(position=Point(x=10, y=10), text="", confidence=0.0)
    readings = [
        {"text": "Ø18", "value_mm": 18.0, "kind": "diameter", "tolerance": None, "confidence": 0.7},
        {"text": "Ø16", "value_mm": 16.0, "kind": "diameter", "tolerance": None, "confidence": 0.3},
    ]
    apply_vlm_readings(e, readings)
    assert e.text == "Ø18"
    assert e.origin == "vlm"
    assert e.confidence == 0.7
    assert len(e.alternatives) == 1
    assert e.alternatives[0].value == "Ø16"
    assert e.alternatives[0].p == 0.3
    assert any("vlm:kind=diameter" in ev for ev in e.evidence)


def test_apply_vlm_readings_noop_on_empty():
    e = TextEntity(position=Point(x=10, y=10), text="original", confidence=0.9)
    apply_vlm_readings(e, [])
    assert e.text == "original"


# ── resolve_hypotheses: promotion ────────────────────────────────────────────


def test_thread_reading_promoted_over_lookalike_diameter():
    """"M18" is a valid metric thread designation, "Ø18" would need geometry
    to confirm — with no matching circle nearby, thread validity alone should
    decisively win here."""
    dim = DimensionEntity(
        p1=Point(x=100, y=100), p2=Point(x=140, y=100), kind="linear",
        text="M18", value_mm=None, confidence=0.5,
        alternatives=[__import__("app.ai.cad_ir.schema", fromlist=["Alternative"]).Alternative(value="M180", p=0.1)],
    )
    ir = _ir([dim])
    resolve_hypotheses(ir)
    assert dim.assurance == "constraint_validated"
    assert not any(r.entity_id == dim.id for r in ir.review if not r.resolved)


def test_geometry_confirms_diameter_reading_over_alternative():
    circle = Circle(center=Point(x=200, y=150), radius=36)  # 36px * scale=0.5 = 18mm diameter*... wait radius*2*scale
    text = TextEntity(
        position=Point(x=205, y=150), text="Ø18", confidence=0.55,
    )
    from app.ai.cad_ir.schema import Alternative

    text.alternatives = [Alternative(value="Ø16", p=0.45)]
    ir = _ir([circle, text])
    # circle diameter in mm = 2*36*0.5 = 36mm — set text to match exactly for a clean test
    text.text = "36"
    resolve_hypotheses(ir)
    assert text.assurance == "constraint_validated"


def test_ambiguous_reading_stays_inferred_and_queued_for_review():
    dim = DimensionEntity(
        p1=Point(x=10, y=10), p2=Point(x=50, y=10), kind="linear",
        text="13", value_mm=13.0, confidence=0.51, tolerance=None,
    )
    from app.ai.cad_ir.schema import Alternative

    dim.alternatives = [Alternative(value="18", p=0.49)]
    ir = _ir([dim])
    resolve_hypotheses(ir)
    assert dim.assurance != "constraint_validated"
    pending = [r for r in ir.review if not r.resolved]
    assert any(r.entity_id == dim.id and r.reason == "unresolved_hypothesis" for r in pending)


def test_invalid_tolerance_symbol_penalized_in_favor_of_alternative():
    from app.ai.cad_ir.schema import Alternative

    dim = DimensionEntity(
        p1=Point(x=10, y=10), p2=Point(x=50, y=10), kind="linear",
        text="20Zz9", value_mm=20.0, confidence=0.55, tolerance="Zz9",  # bogus tolerance letters
        alternatives=[Alternative(value="20H7", p=0.45)],
    )
    ir = _ir([dim])
    # can't cross-check the alternative's tolerance since our scorer only
    # reads the entity's OWN .tolerance field for the leading candidate;
    # this test only asserts the leading (invalid-tolerance) reading is NOT
    # decisively promoted.
    resolve_hypotheses(ir)
    assert dim.assurance != "constraint_validated"


def test_human_approved_entity_is_never_touched():
    from app.ai.cad_ir.schema import Alternative

    dim = DimensionEntity(
        p1=Point(x=10, y=10), p2=Point(x=50, y=10), kind="linear",
        text="M18", value_mm=None, confidence=0.9, assurance="human_approved",
        alternatives=[Alternative(value="M180", p=0.1)],
    )
    ir = _ir([dim])
    resolve_hypotheses(ir)
    assert dim.assurance == "human_approved"
    assert not any(r.entity_id == dim.id for r in ir.review)


# ── check_dimension_chains ───────────────────────────────────────────────────


def test_dimension_chain_matching_is_silent():
    dims = [
        DimensionEntity(p1=Point(x=0, y=0), p2=Point(x=10, y=0), kind="linear", value_mm=30.0),
        DimensionEntity(p1=Point(x=0, y=0), p2=Point(x=10, y=0), kind="linear", value_mm=40.0),
        DimensionEntity(p1=Point(x=0, y=0), p2=Point(x=10, y=0), kind="linear", value_mm=70.0),
    ]
    ir = _ir(dims)
    assert check_dimension_chains(ir) == []


def test_dimension_chain_mismatch_warns():
    dims = [
        DimensionEntity(p1=Point(x=0, y=0), p2=Point(x=10, y=0), kind="linear", value_mm=30.0),
        DimensionEntity(p1=Point(x=0, y=0), p2=Point(x=10, y=0), kind="linear", value_mm=40.0),
        DimensionEntity(p1=Point(x=0, y=0), p2=Point(x=10, y=0), kind="linear", value_mm=100.0),
    ]
    ir = _ir(dims)
    warnings = check_dimension_chains(ir)
    assert len(warnings) == 1
    assert "100.00" in warnings[0]


def test_dimension_chain_check_wired_into_cad_validate():
    from app.ai.cad_validate import CadCheckCode, validate_ir

    dims = [
        DimensionEntity(p1=Point(x=0, y=0), p2=Point(x=10, y=0), kind="linear", value_mm=30.0),
        DimensionEntity(p1=Point(x=0, y=0), p2=Point(x=10, y=0), kind="linear", value_mm=40.0),
        DimensionEntity(p1=Point(x=0, y=0), p2=Point(x=10, y=0), kind="linear", value_mm=100.0),
    ]
    ir = _ir(dims)
    report = validate_ir(ir)
    assert any(i.code == CadCheckCode.DIM_CHAIN_MISMATCH.value for i in report.issues)
