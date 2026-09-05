"""The TechDraw sheet client: what it sends, and how it degrades."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.ai.cad_solid import feature_tree_from_spec
from app.services.cad_kernel import (
    CadKernelRejected,
    CadKernelUnavailable,
    draw_candidate_sheet,
)

_SPEC = {
    "part": "Вал",
    "main_view": {
        "type": "тело вращения",
        "outer": [
            {"diameter_mm": 60.0, "length_mm": 40.0},
            {"diameter_mm": 40.0, "length_mm": 80.0},
        ],
        "bore": [{"diameter_mm": 20.0, "length_mm": 120.0}],
    },
}


def _candidate():
    candidate = feature_tree_from_spec(_SPEC)
    assert candidate is not None
    return candidate


def _response(status: int, payload: dict | None = None) -> httpx.Response:
    return httpx.Response(
        status,
        json=payload if payload is not None else {},
        request=httpx.Request("POST", "http://kernel/drawing"),
    )


def _call(response: httpx.Response, **kwargs):
    with patch("httpx.AsyncClient.post", AsyncMock(return_value=response)):
        return asyncio.run(
            draw_candidate_sheet(
                _candidate(),
                views=[{"kind": "front"}, {"kind": "section", "label": "А-А"}],
                **kwargs,
            )
        )


def test_sends_the_requested_views_and_scale():
    captured: dict = {}

    async def _post(self, url, json=None, **_kw):  # noqa: ANN001
        captured["url"] = url
        captured["json"] = json
        return _response(200, {"views": [], "scale": 0.5})

    with patch("httpx.AsyncClient.post", _post):
        asyncio.run(
            draw_candidate_sheet(
                _candidate(),
                views=[{"kind": "front"}, {"kind": "section", "label": "А-А"}],
                scale=0.5,
            )
        )

    assert captured["url"].endswith("/drawing")
    assert captured["json"]["scale"] == 0.5
    assert [v["kind"] for v in captured["json"]["views"]] == ["front", "section"]
    # Assumptions are confirmed: a section of a part whose bore was not read
    # would otherwise never be produced at all.
    assert captured["json"]["confirm_assumptions"] is True


def test_older_kernel_without_the_endpoint_degrades_to_none():
    """A deployment mid-upgrade must lose the TechDraw sheet, not the drawing."""
    assert _call(_response(404)) is None


def test_a_rejected_request_is_reported_as_rejection_not_an_outage():
    with pytest.raises(CadKernelRejected):
        _call(_response(422, {"detail": "a section needs a base view"}))


def test_a_broken_kernel_is_reported_as_unavailable():
    with pytest.raises(CadKernelUnavailable):
        _call(_response(500))


def test_hatch_outlines_become_regions_on_the_sheet():
    """A section is recognizable by its hatching; the preview drew none.

    The kernel returns the outline of the cut MATERIAL — holes already
    excluded, so the bore stays white — and those become hatch regions placed
    with the view they belong to.
    """
    from app.ai.cad_projection import place_views

    views = {
        "front": {
            "bounds_mm": {"u_min": -10.0, "u_max": 10.0, "v_min": -5.0, "v_max": 5.0},
            "visible": [{"type": "line", "points": [[-10.0, -5.0], [10.0, -5.0]]}],
            "hidden": [],
            "hatch": [[[-10.0, -5.0], [10.0, -5.0], [10.0, 5.0], [-10.0, 5.0]]],
        }
    }

    entities, placements = place_views(views, px_per_mm=2.0)

    hatches = [e for e in entities if e.type == "hatch"]
    assert len(hatches) == 1, entities
    assert len(hatches[0].boundary) == 4
    # Placed with its view, and y is flipped like every other entity: the
    # projector works y-up and the canvas is y-down.
    assert hatches[0].boundary[0].x == (placements["front"]["offset_u"] - 10.0) * 2.0


def test_measured_dimensions_carry_the_kernel_value():
    """The value is measured off the solid, so it cannot disagree with it."""
    from app.ai.cad_projection import dimensions_from_kernel

    entities = dimensions_from_kernel(
        [
            {
                "view_index": 0,
                "anchors_mm": [[2.5, -20.0], [2.5, 20.0]],
                "value_mm": 80.0,
                "label": "Ø",
            }
        ],
        {"front": {"offset_u": 100.0, "offset_v": 50.0}},
        ["front"],
        px_per_mm=2.0,
    )

    # One dimension is drawn as ГОСТ 2.307 wants it: two witness lines, the
    # dimension line, two arrowheads, the value, and the semantic entity that
    # carries the measurement into the DXF export.
    kinds = {e.type for e in entities}
    assert kinds == {"segment", "polyline", "text", "dimension"}
    dimension = next(e for e in entities if e.type == "dimension")
    assert dimension.value_mm == 80.0
    assert dimension.text == "Ø80"
    assert dimension.p1.x == (100.0 + 2.5) * 2.0
    assert [e.text for e in entities if e.type == "text"] == ["Ø80"]
    # Arrowheads are closed slivers, not open strokes.
    assert all(e.closed for e in entities if e.type == "polyline")


@pytest.mark.parametrize(
    ("label", "value", "expected"),
    [
        ("17", 17.0, "17"),
        ("Ø56.55", 56.55, "Ø56.55"),
        ("M75x1,5", 75.0, "M75x1,5"),
        ("Ø", 90.0, "Ø90"),
    ],
)
def test_dimension_label_does_not_duplicate_a_value_already_in_label(
    label: str, value: float, expected: str
):
    from app.ai.cad_projection import dimensions_from_kernel

    entities = dimensions_from_kernel(
        [{"view_index": 0, "anchors_mm": [[0, 0], [10, 0]], "value_mm": value, "label": label}],
        {"front": {"offset_u": 0.0, "offset_v": 0.0}},
        ["front"],
        px_per_mm=1.0,
    )
    dimension = next(entity for entity in entities if entity.type == "dimension")
    assert dimension.text == expected


@pytest.mark.parametrize(
    ("label", "value", "fit", "deviation", "thread"),
    [
        ("Ø80g6", 80.0, "g6", None, None),
        ("Ø50-0,02", 50.0, None, "-0,02", None),
        ("M75x1,5", 75.0, None, None, "M75x1.5"),
    ],
)
def test_dimension_keeps_structured_semantics_bound_to_its_geometry(
    label: str,
    value: float,
    fit: str | None,
    deviation: str | None,
    thread: str | None,
):
    from app.ai.cad_projection import dimensions_from_kernel

    entities = dimensions_from_kernel(
        [
            {
                "view_index": 0,
                "anchors_mm": [[0, 0], [10, 0]],
                "value_mm": value,
                "label": label,
            }
        ],
        {"front": {"offset_u": 0.0, "offset_v": 0.0}},
        ["front"],
        px_per_mm=1.0,
    )

    dimension = next(entity for entity in entities if entity.type == "dimension")
    assert dimension.fit == fit
    assert dimension.deviation == deviation
    assert dimension.thread == thread
    assert dimension.tolerance == (fit or deviation)
