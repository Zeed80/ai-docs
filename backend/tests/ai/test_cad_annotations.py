"""Structured ЕСКД annotations (C4): text, rendering, validation."""

from __future__ import annotations

import pytest

from app.ai.cad_ir import CadIR, SourceInfo
from app.ai.cad_ir.annotations import annotation_text, validate_annotation
from app.ai.cad_ir.schema import AnnotationEntity, Point
from app.ai.cad_validate import validate_ir


def _ann(kind, **kw):
    return AnnotationEntity(position=Point(x=50, y=50), kind=kind, **kw)


def _ir(entities, scale=0.5):
    return CadIR(
        source=SourceInfo(image_width=400, image_height=300),
        scale=scale,
        scale_source="manual",
        entities=entities,
    )


# ── canonical text ───────────────────────────────────────────────────────────


def test_roughness_text_adds_ra_prefix():
    assert annotation_text("roughness", value="3.2") == "Ra 3.2"
    assert annotation_text("roughness", value="Ra 3.2") == "Ra 3.2"


def test_tolerance_text_uses_symbol_glyph_and_datums():
    txt = annotation_text("tolerance", value="0.05", symbol="perpendicularity", datum_refs=["A"])
    assert "⊥" in txt
    assert "0.05" in txt
    assert txt.endswith("A")


def test_thread_text_is_designation():
    assert annotation_text("thread", value="M20×1.5") == "M20×1.5"


# ── validation ───────────────────────────────────────────────────────────────


def test_roughness_out_of_series_flagged():
    ok, msg = validate_annotation(_ann("roughness", value="3.0"))
    assert not ok and "2789" in msg


def test_roughness_in_series_ok():
    ok, _ = validate_annotation(_ann("roughness", value="3.2"))
    assert ok


def test_unparseable_thread_flagged():
    ok, msg = validate_annotation(_ann("thread", value="не резьба"))
    assert not ok and "8724" in msg


def test_valid_thread_ok():
    ok, _ = validate_annotation(_ann("thread", value="M20×1.5"))
    assert ok


def test_unknown_tolerance_symbol_flagged():
    ok, msg = validate_annotation(_ann("tolerance", value="0.05", symbol="bogus"))
    assert not ok


def test_multichar_datum_flagged():
    ok, msg = validate_annotation(_ann("datum", symbol="AB"))
    assert not ok


def test_single_letter_datum_ok():
    ok, _ = validate_annotation(_ann("datum", symbol="A"))
    assert ok


# ── through validate_ir (profile-backed issue) ───────────────────────────────


def test_invalid_annotation_becomes_profile_issue():
    report = validate_ir(_ir([_ann("roughness", value="3.0")]))
    issue = next(i for i in report.issues if i.code == "ESKD_ANNOTATION_INVALID")
    assert issue.rule_id == "ESKD.2.308.annotation"
    assert issue.fix_hint
    assert issue.entity_ids


def test_valid_annotations_produce_no_issue():
    report = validate_ir(_ir([
        _ann("roughness", value="3.2"),
        _ann("thread", value="M20×1.5"),
        _ann("tolerance", value="0.05", symbol="flatness"),
        _ann("datum", symbol="A"),
    ]))
    assert "ESKD_ANNOTATION_INVALID" not in {i.code for i in report.issues}


# ── rendering ────────────────────────────────────────────────────────────────


def test_svg_renders_annotation_text():
    from app.ai.cad_ir.svg_render import render_ir_to_svg

    svg = render_ir_to_svg(_ir([_ann("roughness", value="3.2")])).decode()
    assert "Ra 3.2" in svg


def test_dxf_renders_annotation_and_tolerance_frame():
    pytest.importorskip("ezdxf")
    import ezdxf
    import io

    from app.ai.cad_ir.dxf_render import render_ir_to_dxf

    dxf = render_ir_to_dxf(_ir([
        _ann("tolerance", value="0.05", symbol="perpendicularity", datum_refs=["A"]),
    ]))
    doc = ezdxf.read(io.StringIO(dxf.decode()))
    types = {e.dxftype() for e in doc.modelspace()}
    assert "TEXT" in types
    assert "LWPOLYLINE" in types  # the boxed tolerance frame


def test_annotation_not_counted_as_stroke_geometry():
    # rasterize_entities (coverage) skips annotations like text.
    from app.ai.cad_ir.png_render import rasterize_entities
    import numpy as np

    canvas = rasterize_entities([_ann("roughness", value="3.2")], 400, 300)
    assert (np.asarray(canvas) < 128).sum() == 0
