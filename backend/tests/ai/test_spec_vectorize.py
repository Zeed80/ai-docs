"""Model 2: parametric drafter (spec -> clean CadIR), no VLM needed."""

from __future__ import annotations

from app.ai.cad_recognize.spec_vectorize import (
    _num,
    _parse_spec_json,
    draft_rotation_body,
)


def test_num_reads_values_from_messy_fields():
    assert _num(30) == 30.0
    assert _num("Ø30h6") == 30.0
    assert _num("±0,0095") == 0.0095
    assert _num(None) is None
    assert _num("no number") is None


def test_parse_spec_strips_think_and_fences():
    raw = '<think>reasoning</think>\n```json\n{"part":"Вал"}\n```'
    assert _parse_spec_json(raw) == {"part": "Вал"}
    assert _parse_spec_json("garbage") == {}


def test_draft_rotation_body_builds_clean_stepped_profile():
    spec = {
        "main_view": {
            "type": "тело вращения (вал)",
            "features": [
                {"kind": "cylinder", "diameter_mm": 50, "length_mm": 150},
                {"kind": "cylinder", "diameter_mm": 80, "length_mm": 200},
                {"kind": "cylinder", "diameter_mm": 30, "length_mm": 100},
            ],
        }
    }
    ir = draft_rotation_body(spec)
    assert ir is not None
    segs = [e for e in ir.entities if e.type == "segment"]
    # Clean, not fragmented: a handful of edges, all spec-origin/validated.
    assert 6 <= len(segs) <= 20
    assert all(s.origin == "spec" and s.assurance == "constraint_validated" for s in segs)
    assert any(s.line_class == "axis" for s in segs)  # centreline
    assert ir.recognizer_used == "spec-drafter-rotation"


def test_draft_rotation_body_declines_when_no_sections():
    assert draft_rotation_body({"main_view": {"features": [{"kind": "hole", "diameter_mm": 10}]}}) is None
