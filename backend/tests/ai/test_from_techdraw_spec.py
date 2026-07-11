"""ShaftSpec -> CAD IR adapter (Ф6.1): geometric equivalence against the
legacy bespoke DXF renderer (techdraw.render_spec_to_dxf), and a valid
independent render through the shared IR pipeline."""

from __future__ import annotations

import io

import ezdxf
import pytest

from app.ai.cad_ir.adapters.from_techdraw_spec import shaft_spec_to_ir
from app.ai.cad_ir.dxf_render import render_ir_to_dxf
from app.ai.techdraw import ShaftSegment, ShaftSpec, render_spec_to_dxf


def _sample_spec() -> ShaftSpec:
    return ShaftSpec(
        segments=[
            ShaftSegment(diameter=30, length=40, tolerance="h6"),
            ShaftSegment(diameter=20, length=60, tolerance="k6", bore_diameter=10),
            ShaftSegment(diameter=25, length=30, thread="M20x1.5"),
        ]
    )


def _dxf_lines_by_layer(dxf_bytes: bytes) -> dict[str, int]:
    doc = ezdxf.read(io.StringIO(dxf_bytes.decode("utf-8")))
    counts: dict[str, int] = {}
    for e in doc.modelspace():
        if e.dxftype() == "LINE":
            counts[e.dxf.layer] = counts.get(e.dxf.layer, 0) + 1
    return counts


def test_shaft_ir_object_line_count_matches_legacy_dxf() -> None:
    """Same profile geometry -> same number of OBJECT-layer strokes: 2
    top/bottom lines per segment + a step-transition line wherever the
    diameter changes + one closing line at the free end."""
    spec = _sample_spec()
    legacy = _dxf_lines_by_layer(render_spec_to_dxf(spec.model_dump()))
    ir = shaft_spec_to_ir(spec)
    new = _dxf_lines_by_layer(render_ir_to_dxf(ir))
    assert new["OBJECT"] == legacy["OBJECT"]


def test_shaft_ir_center_line_count_matches_legacy_dxf() -> None:
    """CENTER layer: shaft axis + bore centerlines + thread minor-diameter
    centerlines — same count on both paths for a spec using both features."""
    spec = _sample_spec()
    legacy = _dxf_lines_by_layer(render_spec_to_dxf(spec.model_dump()))
    ir = shaft_spec_to_ir(spec)
    new = _dxf_lines_by_layer(render_ir_to_dxf(ir))
    assert new["CENTER"] == legacy["CENTER"]


def test_shaft_ir_total_length_matches_sum_of_segments() -> None:
    spec = _sample_spec()
    ir = shaft_spec_to_ir(spec)
    total = sum(seg.length for seg in spec.segments)
    dims = [e for e in ir.entities if e.type == "dimension" and e.kind == "linear"]
    assert any(d.value_mm == pytest.approx(total) for d in dims)


def test_shaft_ir_diameter_dimensions_carry_tolerance() -> None:
    spec = _sample_spec()
    ir = shaft_spec_to_ir(spec)
    dia_dims = [e for e in ir.entities if e.type == "dimension" and e.kind == "diameter"]
    assert any(d.value_mm == pytest.approx(30) and d.tolerance == "h6" for d in dia_dims)
    assert any(d.value_mm == pytest.approx(20) and d.tolerance == "k6" for d in dia_dims)


def test_shaft_ir_entities_are_spec_origin_and_constraint_validated() -> None:
    ir = shaft_spec_to_ir(_sample_spec())
    assert all(e.origin == "spec" for e in ir.entities)
    assert all(e.assurance == "constraint_validated" for e in ir.entities)


def test_shaft_ir_dxf_reads_back_valid() -> None:
    """The new path's own output must be independently valid, not just
    line-count-compatible with the legacy one."""
    ir = shaft_spec_to_ir(_sample_spec())
    dxf_bytes = render_ir_to_dxf(ir)
    doc = ezdxf.read(io.StringIO(dxf_bytes.decode("utf-8")))
    assert doc.modelspace() is not None


def test_shaft_ir_single_segment_shaft() -> None:
    """Edge case: a single uniform cylinder — no step-transition lines at
    all beyond the two end caps."""
    spec = ShaftSpec(segments=[ShaftSegment(diameter=15, length=50)])
    ir = shaft_spec_to_ir(spec)
    segments = [e for e in ir.entities if e.type == "segment" and e.line_class == "contour"]
    # top, bottom, start cap, end cap
    assert len(segments) == 4


def test_shaft_ir_carries_roughness_marks_legacy_dxf_also_draws() -> None:
    """Regression: the adapter used to silently drop seg.roughness even
    though the legacy DXF path draws a real ГОСТ 2.309 roughness mark for
    it (on a "ROUGHNESS" layer) — verify both paths actually have SOME
    roughness-related output for a spec that sets it, not just that the IR
    path doesn't crash."""
    spec = ShaftSpec(segments=[ShaftSegment(diameter=30, length=40, roughness=3.2)])

    legacy_doc = ezdxf.read(io.StringIO(render_spec_to_dxf(spec.model_dump()).decode("utf-8")))
    legacy_roughness_text = [
        e.dxf.text for e in legacy_doc.modelspace()
        if e.dxftype() == "TEXT" and e.dxf.layer == "ROUGHNESS"
    ]
    assert any("3.2" in t for t in legacy_roughness_text), "legacy path should draw the Ra callout"

    ir = shaft_spec_to_ir(spec)
    ir_roughness_texts = [e.text for e in ir.entities if e.type == "text" and "Ra" in (e.text or "")]
    assert any("3.2" in t for t in ir_roughness_texts), "adapter must not silently drop seg.roughness"


def test_shaft_ir_no_roughness_marks_when_not_specified() -> None:
    spec = ShaftSpec(segments=[ShaftSegment(diameter=30, length=40)])
    ir = shaft_spec_to_ir(spec)
    assert not [e for e in ir.entities if e.type == "text" and "Ra" in (e.text or "")]
