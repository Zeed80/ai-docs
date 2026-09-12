"""Канавки и фаски вала на листе (Ф3.0c): ширина и Ø дна канавки, «c×45°» фаски."""

from __future__ import annotations

from app.ai.cad_ir.sheet_from_solid import SheetPlan, _turned_detail_dimensions

# Вал Ø30×30 → Ø20×40 → Ø25×30, главный вид 1:1 от u=10, ось v=50.
SPEC = {
    "main_view": {
        "outer": [
            {"diameter_mm": 30.0, "length_mm": 30.0},
            {"diameter_mm": 20.0, "length_mm": 40.0},
            {"diameter_mm": 25.0, "length_mm": 30.0},
        ],
        # Канавка у уступа 70, на меньшей ступени Ø20: 68…70, глубина 0,5.
        "grooves": [
            {"kind": "relief", "axial_position_mm": 69.0, "width_mm": 2.0, "depth_mm": 0.5}
        ],
        "chamfers": [
            {"size_mm": 1.0, "angle_deg": 45.0, "location": "left_end"},
            {"size_mm": 1.6, "angle_deg": 45.0, "location": "right_end"},
        ],
    }
}


def _plan(ratio: float = 1.0) -> SheetPlan:
    return SheetPlan(
        part_class="solid_rotation",
        views=[{"kind": "front"}],
        sheet_format="A3",
        landscape=True,
        ratio=ratio,
        scale_label="1:1",
        layout_w_mm=400.0,
        layout_h_mm=270.0,
    )


def _drawing() -> dict:
    return {
        "views": [
            {
                "kind": "front",
                "bounds_mm": {"u_min": 10.0, "u_max": 110.0, "v_min": 35.0, "v_max": 65.0},
            }
        ],
        "dimensions": [],
    }


def test_a_groove_gets_its_width_with_the_depth_in_the_label_under_the_view():
    drawing = _drawing()
    _turned_detail_dimensions(drawing, SPEC, _plan())

    grooves = [d for d in drawing["dimensions"] if d["measured_by"] == "groove"]
    # Только размер под видом: вертикальный Ø дна через канавку ломал проверку
    # профиля вала (корпус v8: 96 → 57 %).
    assert len(grooves) == 1
    width = grooves[0]
    assert width["kind"] == "DistanceX" and width["below"] is True
    assert width["value_mm"] == 2.0 and width["label"] == "2×0.5"
    assert width["anchors_mm"] == [[78.0, 40.5], [80.0, 40.5]]  # 68…70 от u=10, дно Ø19


def test_end_chamfers_are_dimensioned_as_size_by_angle():
    drawing = _drawing()
    _turned_detail_dimensions(drawing, SPEC, _plan())

    chamfers = sorted(
        (d for d in drawing["dimensions"] if d["measured_by"] == "chamfer"),
        key=lambda d: d["anchors_mm"][0][0],
    )
    assert [d["label"] for d in chamfers] == ["1×45°", "1.6×45°"]
    assert chamfers[0]["anchors_mm"] == [[10.0, 35.0], [11.0, 35.0]]  # от левого торца
    assert chamfers[1]["anchors_mm"][1][0] == 110.0  # до правого торца


def test_the_scale_of_the_view_applies_to_positions_not_to_values():
    drawing = _drawing()
    _turned_detail_dimensions(drawing, SPEC, _plan(ratio=2.0))

    width = next(
        d
        for d in drawing["dimensions"]
        if d["measured_by"] == "groove" and d["kind"] == "DistanceX"
    )
    assert width["value_mm"] == 2.0
    assert width["anchors_mm"][1][0] - width["anchors_mm"][0][0] == 4.0


def test_a_plate_gets_nothing():
    drawing = _drawing()
    plan = _plan()
    plan.part_class = "plate"
    _turned_detail_dimensions(drawing, SPEC, plan)

    assert drawing["dimensions"] == []
