"""H4: performance locks for large sheets — the full derive chain (validate,
PNG/SVG/DXF render, PDF) must stay interactive on a dense drawing. Budgets
are ~3× the measured времена on the reference container, so a real
regression trips them while normal jitter does not."""

from __future__ import annotations

import math
import time

import pytest

from app.ai.cad_ir import CadIR, SourceInfo
from app.ai.cad_ir.schema import Circle, Point, Segment, TextEntity


def _dense_ir(n_segments: int = 3000, n_circles: int = 200, n_texts: int = 100) -> CadIR:
    """A dense but realistic sheet: a grid of contour segments, bore circles
    and labels — the shape of a digitized assembly drawing."""
    entities = []
    cols = 60
    for i in range(n_segments):
        row, col = divmod(i, cols)
        x0, y0 = 20 + col * 30, 20 + row * 15
        entities.append(
            Segment(p1=Point(x=x0, y=y0), p2=Point(x=x0 + 25, y=y0 + (5 if i % 3 else 0)))
        )
    for i in range(n_circles):
        entities.append(
            Circle(center=Point(x=100 + (i % 20) * 90, y=100 + (i // 20) * 80), radius=8 + i % 15)
        )
    for i in range(n_texts):
        entities.append(
            TextEntity(position=Point(x=50 + (i % 10) * 180, y=40 + (i // 10) * 70), text=f"Ø{10 + i}", height=12)
        )
    return CadIR(
        source=SourceInfo(image_width=2000, image_height=1400, kind="blank"),
        scale=0.5, scale_source="manual", entities=entities,
    )


def test_validate_large_sheet_under_budget():
    from app.ai.cad_validate import validate_ir

    ir = _dense_ir()
    t0 = time.monotonic()
    validate_ir(ir)
    elapsed = time.monotonic() - t0
    assert elapsed < 15, f"validate_ir({len(ir.entities)} entities) took {elapsed:.1f}s"


def test_render_chain_large_sheet_under_budget():
    from app.ai.cad_ir.dxf_render import render_dxf_to_pdf, render_ir_to_dxf
    from app.ai.cad_ir.svg_render import render_ir_to_svg

    ir = _dense_ir()
    t0 = time.monotonic()
    dxf = render_ir_to_dxf(ir)
    t_dxf = time.monotonic() - t0

    t0 = time.monotonic()
    svg = render_ir_to_svg(ir)
    t_svg = time.monotonic() - t0

    t0 = time.monotonic()
    pdf = render_dxf_to_pdf(dxf)
    t_pdf = time.monotonic() - t0

    assert len(dxf) > 10_000 and len(svg) > 10_000 and pdf[:5] == b"%PDF-"
    assert t_dxf < 10, f"DXF render took {t_dxf:.1f}s"
    assert t_svg < 5, f"SVG render took {t_svg:.1f}s"
    assert t_pdf < 60, f"PDF render took {t_pdf:.1f}s"


def test_entity_lookup_scales():
    """entity_by_id on a dense sheet — the editor's hottest path (every PATCH
    op resolves ids). Must be far below interactive thresholds even when
    called thousands of times."""
    ir = _dense_ir()
    ids = [e.id for e in ir.entities[:: max(len(ir.entities) // 500, 1)]]
    t0 = time.monotonic()
    for entity_id in ids * 10:
        assert ir.entity_by_id(entity_id) is not None
    elapsed = time.monotonic() - t0
    assert elapsed < 10, f"{len(ids) * 10} lookups took {elapsed:.1f}s"


def test_parallel_analysis_solvers():
    """H4: solver jobs run concurrently without shared-state corruption —
    every result must equal its own sequential answer."""
    import concurrent.futures

    from types import SimpleNamespace

    from app.domain.analysis_solvers import solve_bending

    steel = SimpleNamespace(yield_strength_mpa=300.0, elastic_modulus_mpa=200_000.0,
                            thermal_expansion_1_k=1.2e-5)

    def run(i: int) -> float:
        out = solve_bending(
            {"force_n": 100 + i, "length_mm": 100, "diameter_mm": 20}, steel
        )
        return out.results["stress_mpa"]

    sequential = [run(i) for i in range(50)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        parallel = list(pool.map(run, range(50)))
    assert parallel == sequential
    w = math.pi * 20**3 / 32
    assert parallel[0] == pytest.approx((100 * 100) / w)
