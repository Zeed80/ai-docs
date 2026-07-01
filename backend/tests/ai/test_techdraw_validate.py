"""Tests for deterministic post-validation of LLM-generated techdraw specs."""

from __future__ import annotations

from app.ai.techdraw_validate import blocking, validate_spec


def _codes(spec: dict) -> set[str]:
    return {i.code for i in blocking(validate_spec(spec))}


def test_valid_shaft_has_no_issues():
    spec = {
        "type": "shaft",
        "segments": [{"diameter": 45, "length": 60, "tolerance": "h6", "roughness": 0.8}],
    }
    assert validate_spec(spec) == []


def test_invalid_ra_flagged():
    spec = {"type": "shaft", "segments": [{"diameter": 45, "length": 60, "roughness": 0.9}]}
    assert "RA_INVALID" in _codes(spec)


def test_unknown_tolerance_symbol_flagged():
    spec = {"type": "shaft", "segments": [{"diameter": 45, "length": 60, "tolerance": "zzz"}]}
    assert "TOLERANCE_UNKNOWN" in _codes(spec)


def test_tolerance_out_of_diameter_range_flagged():
    spec = {"type": "shaft", "segments": [{"diameter": 5000, "length": 60, "tolerance": "h6"}]}
    assert "TOLERANCE_OUT_OF_RANGE" in _codes(spec)


def test_unknown_thread_flagged():
    spec = {"type": "shaft", "segments": [{"diameter": 45, "length": 60, "thread": "M13"}]}
    assert "THREAD_UNKNOWN" in _codes(spec)


def test_valid_thread_not_flagged():
    spec = {"type": "shaft", "segments": [{"diameter": 45, "length": 60, "thread": "M20x1.5"}]}
    assert "THREAD_UNKNOWN" not in _codes(spec)


def test_bore_larger_than_shaft_flagged():
    spec = {"type": "shaft", "segments": [{"diameter": 20, "length": 60, "bore_diameter": 25}]}
    assert "BORE_TOO_LARGE" in _codes(spec)


def test_hole_too_large_for_plate_flagged():
    spec = {"type": "plate", "shape": "circle", "diameter": 40,
            "holes": [{"x": 0, "y": 0, "diameter": 50}]}
    assert "HOLE_TOO_LARGE" in _codes(spec)


def test_bolt_circle_too_large_flagged():
    spec = {"type": "plate", "shape": "circle", "diameter": 40,
            "bolt_circle_d": 60, "bolt_circle_n": 6, "bolt_hole_d": 5}
    assert "BOLT_CIRCLE_TOO_LARGE" in _codes(spec)


def test_bolt_hole_larger_than_bolt_circle_flagged():
    spec = {"type": "plate", "shape": "circle", "diameter": 200,
            "bolt_circle_d": 90, "bolt_circle_n": 6, "bolt_hole_d": 100}
    assert "BOLT_HOLE_TOO_LARGE" in _codes(spec)


def test_valid_plate_has_no_issues():
    spec = {
        "type": "plate", "shape": "circle", "diameter": 120, "thickness": 14,
        "holes": [{"x": 0, "y": 0, "diameter": 40, "tolerance": "H7"}],
        "bolt_circle_d": 90, "bolt_circle_n": 6, "bolt_hole_d": 11, "bolt_hole_tol": "H12",
    }
    assert validate_spec(spec) == []


def test_assembly_recurses_into_components():
    spec = {
        "type": "assembly",
        "components": [
            {"ref": "1", "spec": {"type": "shaft", "segments": [
                {"diameter": 45, "length": 60, "roughness": 0.9},
            ]}, "x": 0, "y": 0},
        ],
    }
    assert "RA_INVALID" in _codes(spec)


def test_unknown_type_flagged():
    assert "UNKNOWN_TYPE" in _codes({"type": "spaceship"})
