"""Model 2: parametric drafter (spec -> clean CadIR), no VLM needed."""

from __future__ import annotations

import pytest

from app.ai.cad_recognize.spec_vectorize import (
    _dsl_to_ir,
    _num,
    _parse_spec_json,
    choose_standard_scale,
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


def test_choose_standard_scale_reduces_enlarges_and_fits():
    # A big part reduces to a standard reduction that fits the frame.
    assert choose_standard_scale(300, 80, "A4", landscape=True) == (0.5, "1:2")
    # A tiny part enlarges.
    ratio, label = choose_standard_scale(20, 10, "A4", landscape=True)
    assert label.endswith(":1") and ratio > 1
    # Only standard ratios are ever returned.
    assert label in {"2:1", "2.5:1", "4:1", "5:1", "10:1", "20:1", "40:1", "50:1", "100:1"}


def test_draft_rotation_body_lays_out_on_sheet_with_auto_scale():
    spec = {
        "main_view": {
            "type": "тело вращения (вал)",
            "features": [
                {"kind": "cylinder", "diameter_mm": 50, "length_mm": 150},
                {"kind": "cylinder", "diameter_mm": 80, "length_mm": 200},
            ],
        }
    }
    ir = draft_rotation_body(spec, sheet_format="A3", landscape=True)
    assert ir is not None
    assert ir.sheet.format == "A3"
    assert ir.scale_source == "sheet_format"
    # A standard scale label was written to the title block.
    assert ir.sheet.title_block.get("scale") in {"1:1", "1:2", "1:2.5"}
    # Canvas equals the A3 landscape sheet at 4 px/mm (420×297 → 1680×1188).
    assert ir.source.image_width == 1680 and ir.source.image_height == 1188


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
async def test_generative_model_used_first_when_assigned_even_for_rotation():
    # Generative-first: a rotation body ALSO goes through the model (it handles
    # multiple bodies / mis-read cases the single-stack drafter can't).
    router = _FakeRouter('{"lines":[[0,0,100,0],[100,0,100,50],[0,50,100,50]],"circles":[],"arcs":[],"polylines":[]}')
    spec = {
        "main_view": {
            "type": "тело вращения (вал)",
            "features": [
                {"kind": "cylinder", "diameter_mm": 50, "length_mm": 150},
                {"kind": "cylinder", "diameter_mm": 30, "length_mm": 100},
            ],
        }
    }
    ir = await draft_from_spec_async(spec, draft_model="apex", router=router)
    assert ir is not None
    assert ir.recognizer_used == "spec-drafter-generative"
    assert router.seen.preferred_model == "apex"
    assert router.seen.confidential is True and router.seen.allow_cloud is False


@pytest.mark.asyncio
async def test_falls_back_to_deterministic_when_generative_fails():
    # Model returns junk → deterministic parametric drafter is the safety net.
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
    ir = await draft_from_spec_async(spec, draft_model="apex", router=router)
    assert ir is not None
    assert ir.recognizer_used == "spec-drafter-rotation"


@pytest.mark.asyncio
async def test_no_model_assigned_uses_deterministic():
    spec = {
        "main_view": {
            "type": "тело вращения (вал)",
            "features": [
                {"kind": "cylinder", "diameter_mm": 50, "length_mm": 150},
                {"kind": "cylinder", "diameter_mm": 30, "length_mm": 100},
            ],
        }
    }
    ir = await draft_from_spec_async(spec, draft_model=None)
    assert ir is not None and ir.recognizer_used == "spec-drafter-rotation"


def test_layout_on_sheet_scales_generative_geometry():
    from app.ai.cad_recognize.spec_vectorize import _dsl_to_ir, _layout_on_sheet
    ir = _dsl_to_ir({"lines": [[0, 0, 100, 0], [100, 0, 100, 50], [0, 50, 100, 50]],
                     "circles": [], "arcs": [], "polylines": []})
    spec = {"dimensions": [{"value": "300"}, {"value": "150"}]}
    _layout_on_sheet(ir, spec, "A3", True)
    assert ir.sheet.format == "A3"
    assert ir.scale_source == "sheet_format"
    assert ir.source.image_width == 1680 and ir.source.image_height == 1188
    # geometry moved into the sheet frame (positive, within canvas)
    xs = [e.p1.x for e in ir.entities if e.type == "segment"]
    assert all(0 < x < 1680 for x in xs)
