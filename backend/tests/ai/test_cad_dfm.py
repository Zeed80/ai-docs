"""F4: deterministic DFM checks — drills, radii, walls, bridges, threads."""

import pytest

from app.ai.cad_dfm import check_dfm
from app.ai.cad_ir import CadIR, SourceInfo
from app.ai.cad_ir.schema import AnnotationEntity, Arc, Circle, Point, Segment


def _ir(*entities, scale=1.0):
    return CadIR(
        source=SourceInfo(image_width=1000, image_height=800, kind="blank"),
        scale=scale, scale_source="manual", entities=list(entities),
    )


def _codes(findings):
    return {f.code for f in findings}


def test_requires_confirmed_scale():
    ir = _ir(Circle(center=Point(x=50, y=50), radius=5))
    ir.scale = None
    ir.scale_source = None
    with pytest.raises(ValueError):
        check_dfm(ir)


def test_small_and_nonstandard_holes():
    findings = check_dfm(_ir(
        Circle(center=Point(x=50, y=50), radius=0.3),    # Ø0.6 — unmakeable
        Circle(center=Point(x=200, y=50), radius=3.85),  # Ø7.7 — not in ГОСТ 885
        Circle(center=Point(x=400, y=50), radius=5.0),   # Ø10 — standard, clean
    ))
    assert "DFM_SMALL_HOLE" in _codes(findings)
    assert "DFM_NONSTANDARD_DRILL" in _codes(findings)
    small = next(f for f in findings if f.code == "DFM_SMALL_HOLE")
    assert small.severity == "error"
    nonstd = next(f for f in findings if f.code == "DFM_NONSTANDARD_DRILL")
    assert nonstd.evidence["nearest_standard_mm"] == 7.5


def test_hole_bridge_and_scale_awareness():
    # two Ø10 holes with a 0.5 mm bridge — at scale 0.5 the px distance doubles
    findings = check_dfm(_ir(
        Circle(center=Point(x=100, y=100), radius=10),
        Circle(center=Point(x=121, y=100), radius=10),  # gap 1 px = 0.5 mm
        scale=0.5,
    ))
    assert "DFM_THIN_HOLE_BRIDGE" in _codes(findings)


def test_small_internal_radius_and_thin_wall():
    findings = check_dfm(_ir(
        Arc(center=Point(x=10, y=10), radius=0.3, start_angle=0, end_angle=90),
        Segment(p1=Point(x=0, y=0), p2=Point(x=100, y=0)),
        Segment(p1=Point(x=0, y=1), p2=Point(x=100, y=1)),  # 1 mm wall
    ))
    assert "DFM_SMALL_INTERNAL_RADIUS" in _codes(findings)
    assert "DFM_THIN_WALL" in _codes(findings)


def test_construction_geometry_is_ignored():
    findings = check_dfm(_ir(
        Circle(center=Point(x=50, y=50), radius=0.3, construction=True),
    ))
    assert findings == []


def test_thread_series_checks():
    findings = check_dfm(_ir(
        AnnotationEntity(position=Point(x=10, y=10), kind="thread", value="М11x1.5"),
        AnnotationEntity(position=Point(x=20, y=20), kind="thread", value="M12x2.5"),
        AnnotationEntity(position=Point(x=30, y=30), kind="thread", value="М12x1.75"),
    ))
    assert "DFM_THREAD_NONSTANDARD" in _codes(findings)  # M11 not in the series
    assert "DFM_THREAD_PITCH" in _codes(findings)        # 2.5 > coarse 1.75 for M12
    # M12x1.75 is the coarse pitch — no finding for it
    assert sum(1 for f in findings if "М12x1.75" in f.message) == 0
