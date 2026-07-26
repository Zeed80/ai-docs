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
