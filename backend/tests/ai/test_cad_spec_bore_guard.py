"""The bore is checked against the sheet, exactly like the outer contour.

Until now the outer chain earned four guards on the way in while the bore —
read by its own separate question — went straight into the part. A cavity the
reader invented, or read off the wrong view, became a hole through a real
shaft: Ø18 where the sheet says Ø80H7.
"""

from __future__ import annotations

from app.ai.cad_recognize.spec_fragments import _checked_bore

_OUTER = [
    {"diameter_mm": 80.0, "length_mm": 150.0},
    {"diameter_mm": 102.0, "length_mm": 200.0},
]
_CALLOUTS = {
    "dimensions": [
        {"value": "Ø102"}, {"value": "Ø80js6"}, {"value": "Ø60H7"},
        {"value": "470"}, {"value": "150"},
    ]
}


def test_a_bore_the_sheet_carries_is_kept():
    bore, problem = _checked_bore(
        [{"diameter_mm": 60.0, "length_mm": 300.0}], _OUTER, _CALLOUTS
    )
    assert problem is None
    assert bore == [{"diameter_mm": 60.0, "length_mm": 300.0}]


def test_a_bore_no_callout_supports_is_refused():
    """Ø18 appears nowhere on a sheet whose bore callout says Ø60H7."""
    bore, problem = _checked_bore(
        [{"diameter_mm": 18.0, "length_mm": 300.0}], _OUTER, _CALLOUTS
    )
    assert bore == []
    assert problem is not None and "выносок" in problem


def test_a_bore_wider_than_the_part_is_refused():
    bore, problem = _checked_bore(
        [{"diameter_mm": 120.0, "length_mm": 100.0}], _OUTER, {"dimensions": [{"value": "Ø120"}]}
    )
    assert bore == []
    assert problem is not None and "не меньше наружного" in problem


def test_a_bore_longer_than_the_part_is_refused():
    bore, problem = _checked_bore(
        [{"diameter_mm": 60.0, "length_mm": 700.0}], _OUTER, _CALLOUTS
    )
    assert bore == []
    assert problem is not None and "длиннее детали" in problem


def test_no_bore_is_not_a_problem():
    """A solid shaft is a legitimate reading, not a failed one."""
    assert _checked_bore([], _OUTER, _CALLOUTS) == ([], None)


def test_a_sheet_with_no_callouts_read_does_not_veto_the_bore():
    """Silence about the callouts is not evidence against the geometry."""
    bore, problem = _checked_bore(
        [{"diameter_mm": 60.0, "length_mm": 300.0}], _OUTER, {}
    )
    assert bore and problem is None
