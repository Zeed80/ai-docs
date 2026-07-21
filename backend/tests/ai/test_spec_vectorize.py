"""Model 2: parametric drafter (spec -> clean CadIR), no VLM needed."""

from __future__ import annotations

import pytest

from app.ai.cad_recognize.spec_vectorize import (
    _dsl_to_ir,
    _num,
    _parse_spec_json,
    draft_from_spec_async,
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


def test_dsl_to_ir_decodes_all_primitive_kinds():
    ir = _dsl_to_ir({
        "lines": [[0, 0, 100, 0], [100, 0, 100, 50]],
        "circles": [[50, 25, 10]],
        "arcs": [[50, 25, 20, 0, 90]],
        "polylines": [{"pts": [[0, 0], [10, 10], [20, 0]], "closed": 1}],
    })
    assert ir is not None
    kinds = sorted(e.type for e in ir.entities)
    assert kinds == ["arc", "circle", "polyline", "segment", "segment"]
    assert ir.recognizer_used == "spec-drafter-generative"
    assert _dsl_to_ir({"lines": [], "circles": [], "arcs": [], "polylines": []}) is None


class _FakeResp:
    def __init__(self, text):
        self.text = text


class _FakeRouter:
    """Stand-in Model 2: returns a fixed geometry DSL, records the request."""

    def __init__(self, text):
        self._text = text
        self.seen = None

    async def run(self, request):
        self.seen = request
        return _FakeResp(self._text)


@pytest.mark.asyncio
async def test_draft_from_spec_uses_generative_model_when_assigned():
    router = _FakeRouter('{"lines":[[0,0,100,0],[100,0,100,50]],"circles":[],"arcs":[],"polylines":[]}')
    spec = {"main_view": {"type": "тело вращения (вал)", "features": []}}
    ir = await draft_from_spec_async(spec, draft_model="my-lora", router=router)
    assert ir is not None
    assert ir.recognizer_used == "spec-drafter-generative"
    assert router.seen.preferred_model == "my-lora"
    assert router.seen.confidential is True and router.seen.allow_cloud is False


@pytest.mark.asyncio
async def test_draft_from_spec_falls_back_to_deterministic_on_bad_output():
    router = _FakeRouter("not json")
    spec = {
        "main_view": {
            "type": "тело вращения (вал)",
            "features": [
                {"kind": "cylinder", "diameter_mm": 50, "length_mm": 150},
                {"kind": "cylinder", "diameter_mm": 30, "length_mm": 100},
            ],
        }
    }
    ir = await draft_from_spec_async(spec, draft_model="my-lora", router=router)
    assert ir is not None
    # Generative output was unusable → deterministic drafter took over.
    assert ir.recognizer_used == "spec-drafter-rotation"
