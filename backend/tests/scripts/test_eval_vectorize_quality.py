"""eval_vectorize B4 geometry-quality metrics: fragmentation, degenerate/
duplicate rates and open-endpoint rate are self-referential (no GT alignment),
so they can score photos too."""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "scripts"))

from app.ai.cad_ir.schema import Point, Segment  # noqa: E402
from eval_vectorize import _geometry_quality  # noqa: E402


def _seg(x1, y1, x2, y2):
    return Segment(p1=Point(x=x1, y=y1), p2=Point(x=x2, y=y2))


def test_closed_square_has_no_open_endpoints() -> None:
    # four segments meeting corner-to-corner: every endpoint has a neighbour.
    q = _geometry_quality(
        [_seg(0, 0, 100, 0), _seg(100, 0, 100, 100), _seg(100, 100, 0, 100), _seg(0, 100, 0, 0)]
    )
    assert q["n_segments"] == 4
    assert q["open_endpoint_rate"] == 0.0
    assert q["degenerate_rate"] == 0.0


def test_floating_segments_are_all_open() -> None:
    # two disjoint far-apart segments — every endpoint floats free.
    q = _geometry_quality([_seg(0, 0, 50, 0), _seg(500, 500, 550, 500)])
    assert q["open_endpoint_rate"] == 1.0


def test_degenerate_and_duplicate_rates() -> None:
    q = _geometry_quality(
        [
            _seg(0, 0, 100, 0),
            _seg(0, 0, 100, 0),  # duplicate
            _seg(10, 10, 11, 10),  # 1px → degenerate
        ]
    )
    assert q["duplicate_rate"] > 0
    assert q["degenerate_rate"] > 0


def test_empty_input() -> None:
    assert _geometry_quality([]) == {"n_segments": 0}


def test_dxf_roundtrip_reports_eskd_errors() -> None:
    # H1: the downstream chain (IR → ЕСКД validate → DXF → independent parse)
    # must report reopen success and a non-negative blocking-error count.
    from eval_vectorize import _dxf_roundtrip

    out = _dxf_roundtrip([_seg(0, 0, 100, 0), _seg(100, 0, 100, 80)], 400, 300)
    assert out["dxf_reopens"] is True
    assert out["dxf_entities"] >= 2
    assert out["eskd_errors"] >= 0
