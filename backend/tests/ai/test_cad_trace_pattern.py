"""Tests for app.tasks.cad_trace._pattern_offsets (Ф8 нового CAD-редактора).

Pure arithmetic, no DB/kernel/Celery involved — the one genuinely new
nontrivial piece of Фаза 8 (the persist/build/rollback wiring around it is
the already-proven Ф6 shape, reused verbatim).
"""

from __future__ import annotations

import math

import pytest

from app.tasks.cad_trace import (
    _editor_graph_base,
    _pattern_offsets,
    _persist_editor_candidate,
)


@pytest.mark.asyncio
async def test_editor_graph_base_prefers_design_branch(monkeypatch):
    source = object()
    design = object()

    async def latest(_db, graph_id, *, lock=False):
        assert lock is True
        return design if graph_id.endswith(":design") else source

    monkeypatch.setattr("app.services.engineering_model_graph.latest_graph_revision", latest)
    graph_id, row, needs_fork = await _editor_graph_base(object(), "generation-1", lock=True)
    assert graph_id == "image-generation:generation-1:design"
    assert row is design
    assert needs_fork is False


@pytest.mark.asyncio
async def test_first_editor_mutation_forks_then_applies_human_patch(monkeypatch):
    class Row:
        def __init__(self, graph_id, revision, sha):
            self.graph_id = graph_id
            self.revision = revision
            self.canonical_sha256 = sha

    source = Row("image-generation:generation-1", 4, "source-sha")
    fork = Row("image-generation:generation-1:design", 0, "fork-sha")
    revised = Row("image-generation:generation-1:design", 1, "revised-sha")
    calls = []

    async def persist(_db, **kwargs):
        calls.append(kwargs)
        return fork if len(calls) == 1 else revised

    monkeypatch.setattr(
        "app.services.engineering_model_graph.persist_feature_tree_revision",
        persist,
    )
    result = await _persist_editor_candidate(
        object(),
        generation_id="generation-1",
        spec={"part": "plate"},
        base_candidate=object(),
        updated_candidate=object(),
        base_row=source,
        pass_id="human-add-feature:boss",
        idempotency_key="editor-change-1",
        source_sha256="sheet-sha",
        source_uri="minio://sheet",
        decision_note="Добавить опору",
        actor_sub="engineer-1",
        fork_design_branch=True,
    )
    assert result is revised
    assert [call["producer"] for call in calls] == ["system", "human"]
    assert all(call["graph_id"] == "image-generation:generation-1:design" for call in calls)
    assert calls[1]["expected_base_revision"] == 0
    assert calls[1]["expected_base_sha256"] == "fork-sha"
    assert calls[1]["decision_note"] == "Добавить опору"


def test_linear_pattern_first_instance_is_exactly_the_original():
    offsets = _pattern_offsets(
        {"kind": "linear", "count": 3, "dx_mm": 10.0, "dy_mm": 0.0}, base_x=5.0, base_y=2.0
    )
    assert offsets[0] == (5.0, 2.0)


def test_linear_pattern_steps_evenly_in_both_axes():
    offsets = _pattern_offsets(
        {"kind": "linear", "count": 4, "dx_mm": 10.0, "dy_mm": -3.0}, base_x=0.0, base_y=0.0
    )
    assert offsets == [(0.0, 0.0), (10.0, -3.0), (20.0, -6.0), (30.0, -9.0)]


def test_circular_pattern_first_instance_is_exactly_the_original():
    offsets = _pattern_offsets(
        {
            "kind": "circular",
            "count": 4,
            "center_x_mm": 0.0,
            "center_y_mm": 0.0,
            "total_angle_deg": 360.0,
        },
        base_x=20.0,
        base_y=0.0,
    )
    assert offsets[0] == pytest.approx((20.0, 0.0))


def test_circular_pattern_full_circle_is_evenly_spaced_around_the_centre():
    # 4 instances at radius 10 around the origin, full 360 deg -> 0/90/180/270.
    offsets = _pattern_offsets(
        {
            "kind": "circular",
            "count": 4,
            "center_x_mm": 0.0,
            "center_y_mm": 0.0,
            "total_angle_deg": 360.0,
        },
        base_x=10.0,
        base_y=0.0,
    )
    expected = [
        (10.0, 0.0),
        (0.0, 10.0),
        (-10.0, 0.0),
        (0.0, -10.0),
    ]
    for (ax, ay), (ex, ey) in zip(offsets, expected, strict=True):
        assert ax == pytest.approx(ex, abs=1e-9)
        assert ay == pytest.approx(ey, abs=1e-9)


def test_circular_pattern_preserves_radius_around_an_offset_centre():
    # The feature being patterned isn't necessarily centred at the pattern's
    # own centre -- every instance must stay the same distance from
    # (center_x_mm, center_y_mm) as the original was.
    cx, cy = 5.0, 5.0
    base_x, base_y = 15.0, 5.0  # radius 10 from (5, 5)
    offsets = _pattern_offsets(
        {
            "kind": "circular",
            "count": 6,
            "center_x_mm": cx,
            "center_y_mm": cy,
            "total_angle_deg": 360.0,
        },
        base_x=base_x,
        base_y=base_y,
    )
    for x, y in offsets:
        radius = math.hypot(x - cx, y - cy)
        assert radius == pytest.approx(10.0, abs=1e-9)


def test_circular_pattern_partial_angle_does_not_wrap_past_it():
    offsets = _pattern_offsets(
        {
            "kind": "circular",
            "count": 3,
            "center_x_mm": 0.0,
            "center_y_mm": 0.0,
            "total_angle_deg": 180.0,
        },
        base_x=10.0,
        base_y=0.0,
    )
    # step = 180/3 = 60 deg -> angles 0, 60, 120 (never reaches 180 itself,
    # since 180 would be the START of a 4th, un-requested instance).
    assert offsets[0] == pytest.approx((10.0, 0.0))
    assert offsets[1] == pytest.approx(
        (10.0 * math.cos(math.radians(60)), 10.0 * math.sin(math.radians(60)))
    )
    assert offsets[2] == pytest.approx(
        (10.0 * math.cos(math.radians(120)), 10.0 * math.sin(math.radians(120)))
    )


def test_unknown_pattern_kind_is_rejected():
    with pytest.raises(ValueError, match="неизвестный вид массива"):
        _pattern_offsets({"kind": "spiral", "count": 3}, base_x=0.0, base_y=0.0)
