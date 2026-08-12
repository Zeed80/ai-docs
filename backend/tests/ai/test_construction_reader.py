"""Tests for app.ai.construction_reader (Ф5.2 — 2D architectural reader).

construction_read_as_model is pure/deterministic (no VLM call) and gets the
thorough coverage; read_construction_drawing's VLM-facing half is only
smoke-tested against a fake router, since no real floor-plan corpus exists
in this repository (see the module's own docstring).
"""

from __future__ import annotations

import pytest

from app.ai.construction_reader import (
    ConstructionSheetRead,
    construction_read_as_model,
    read_construction_drawing,
)


def _sheet(**overrides) -> ConstructionSheetRead:
    base = {
        "storey": {"name": "1 этаж", "elevation_mm": 0.0, "default_wall_height_mm": 3000.0},
        "walls": [
            {
                "id": "w1",
                "start_x_mm": 0,
                "start_y_mm": 0,
                "end_x_mm": 6000,
                "end_y_mm": 0,
                "thickness_mm": 200,
                "load_bearing": True,
                "material": "кирпич",
            },
            {
                "id": "w2",
                "start_x_mm": 0,
                "start_y_mm": 0,
                "end_x_mm": 0,
                "end_y_mm": 4000,
                "thickness_mm": 200,
            },
        ],
        "openings": [
            {
                "id": "o1",
                "host_wall_id": "w1",
                "kind": "door",
                "offset_mm": 1000,
                "width_mm": 900,
                "height_mm": 2100,
            },
        ],
    }
    base.update(overrides)
    return ConstructionSheetRead.model_validate(base)


def test_orthogonal_walls_and_opening_build_correct_boxes():
    model, report = construction_read_as_model(_sheet())
    assert model is not None, report
    assert report["walls_built"] == 2
    assert report["openings_built"] == 1
    assert report["skipped"] == []

    wall1 = next(item for item in model.elements if item.id == "w1")
    assert wall1.box.x_mm == pytest.approx(0)
    assert wall1.box.y_mm == pytest.approx(-100)  # centred: start_y - thickness/2
    assert wall1.box.width_mm == pytest.approx(6000)
    assert wall1.box.depth_mm == pytest.approx(200)
    assert wall1.box.height_mm == pytest.approx(3000)  # from default_wall_height_mm

    wall2 = next(item for item in model.elements if item.id == "w2")
    assert wall2.box.width_mm == pytest.approx(200)
    assert wall2.box.depth_mm == pytest.approx(4000)

    opening = next(item for item in model.elements if item.id == "o1")
    assert opening.box.x_mm == pytest.approx(1000)
    assert opening.box.width_mm == pytest.approx(900)
    assert opening.host_id == "w1"


def test_non_orthogonal_wall_is_excluded_not_approximated():
    sheet = _sheet(
        walls=[
            {
                "id": "w1",
                "start_x_mm": 0,
                "start_y_mm": 0,
                "end_x_mm": 6000,
                "end_y_mm": 0,
                "thickness_mm": 200,
            },
            {
                "id": "w_diag",
                "start_x_mm": 0,
                "start_y_mm": 0,
                "end_x_mm": 3000,
                "end_y_mm": 3000,
                "thickness_mm": 200,
            },
        ],
        openings=[],
    )
    model, report = construction_read_as_model(sheet)
    assert model is not None, report
    assert report["walls_built"] == 1
    assert {"id": "w_diag", "kind": "wall", "reason": "not_orthogonal_or_zero_length"} in report[
        "skipped"
    ]


def test_wall_with_no_readable_height_is_excluded():
    sheet = _sheet()
    sheet.storey.default_wall_height_mm = None
    sheet.walls[0].height_mm = None
    sheet.walls[1].height_mm = None
    model, report = construction_read_as_model(sheet)
    assert model is None
    assert report["blocked"] is True
    assert report["blocked_reason"] == "no_orthogonal_walls_with_known_height"
    assert {"id": "w1", "kind": "wall", "reason": "no_height"} in report["skipped"]


def test_opening_outside_host_bounds_is_excluded_individually():
    sheet = _sheet(
        openings=[
            {
                "id": "o1",
                "host_wall_id": "w1",
                "kind": "door",
                # Wall w1 is only 6000mm long; this opening runs off the end.
                "offset_mm": 5900,
                "width_mm": 900,
                "height_mm": 2100,
            },
        ],
    )
    model, report = construction_read_as_model(sheet)
    assert model is not None, report
    assert report["openings_built"] == 0
    assert {"id": "o1", "kind": "opening", "reason": "outside_host_bounds"} in report["skipped"]


def test_opening_on_unknown_host_is_excluded():
    sheet = _sheet(
        openings=[
            {
                "id": "o1",
                "host_wall_id": "does-not-exist",
                "kind": "window",
                "offset_mm": 0,
                "width_mm": 900,
                "height_mm": 1200,
            },
        ],
    )
    model, report = construction_read_as_model(sheet)
    assert model is not None, report
    assert {"id": "o1", "kind": "opening", "reason": "unknown_or_excluded_host"} in report[
        "skipped"
    ]


def test_zero_walls_blocks_with_reason():
    sheet = _sheet(walls=[], openings=[])
    model, report = construction_read_as_model(sheet)
    assert model is None
    assert report["blocked"] is True
    assert report["blocked_reason"] == "no_orthogonal_walls_with_known_height"


class _FakeResponse:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeRouter:
    def __init__(self, text: str) -> None:
        self._text = text
        self.requests: list = []

    async def run(self, request):
        self.requests.append(request)
        return _FakeResponse(self._text)


@pytest.mark.asyncio
async def test_read_construction_drawing_wires_vlm_json_through_to_a_model():
    payload = """{
      "storey": {"name": "1 этаж", "elevation_mm": 0, "default_wall_height_mm": 3000},
      "walls": [
        {"id": "w1", "start_x_mm": 0, "start_y_mm": 0, "end_x_mm": 5000, "end_y_mm": 0,
         "thickness_mm": 200}
      ],
      "openings": []
    }"""
    router = _FakeRouter(payload)
    model, report = await read_construction_drawing(b"fake-image-bytes", router=router)
    assert model is not None, report
    assert len(model.elements) == 1
    assert router.requests, "the VLM router was never called"


@pytest.mark.asyncio
async def test_read_construction_drawing_fails_closed_on_garbage_response():
    router = _FakeRouter("не могу прочитать это изображение")
    model, report = await read_construction_drawing(b"fake-image-bytes", router=router)
    assert model is None
    assert report == {"read_failed": True}
