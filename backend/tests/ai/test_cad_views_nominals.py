"""Метод `views`, C2: замеры → номиналы надписей."""

from __future__ import annotations


def test_axis_chain_snaps_to_labels_and_keeps_unexplained():
    from app.ai.cad_views.nominals import snap_axis

    # Планка: габарит 90, отверстие в 10 от правого края, ещё одно в 16,
    # и точка, которую не объясняет ни одна надпись.
    mapping = snap_axis(
        [0.0, 89.75, 79.99, 15.84, 47.3], [90, 10, 64, 16], tolerance=0.8, overall=90
    )
    assert mapping[89.75] == 90
    assert mapping[79.99] == 80
    assert mapping[15.84] == 16
    assert 47.3 not in mapping


def test_revolve_profile_nominals_keep_bore_ends_outside_faces():
    from app.ai.cad_views.nominals import nominal_revolve

    outer = [
        {"r": 9.9, "z": 0.0},
        {"r": 9.9, "z": 29.6},
        {"r": 15.1, "z": 29.6},
        {"r": 15.1, "z": 50.2},
    ]
    bore = [{"r": 4.0, "z": -0.05}, {"r": 4.0, "z": 50.25}]
    new_outer, new_bore, changed = nominal_revolve(
        outer, bore, [30, 50], [20, 30], [8], tolerance=0.6
    )
    assert [p["z"] for p in new_outer] == [0.0, 30, 30, 50]
    assert [p["r"] for p in new_outer] == [10.0, 10.0, 15.0, 15.0]
    assert [p["z"] for p in new_bore] == [-0.05, 50.25]
    assert changed > 0


def test_bore_step_is_not_merged_onto_outer_step():
    from app.ai.cad_views.nominals import nominal_revolve

    # Уступ снаружи на 13,9 и уступ расточки на 13,95: номинал 14 свёл бы их
    # в одну станцию — стенка нулевой толщины, тело из двух частей.
    outer = [
        {"r": 3.8, "z": 0.0},
        {"r": 3.8, "z": 13.9},
        {"r": 5.75, "z": 13.9},
        {"r": 5.75, "z": 29.0},
    ]
    bore = [
        {"r": 2.0, "z": -0.05},
        {"r": 2.0, "z": 13.95},
        {"r": 4.9, "z": 13.95},
        {"r": 4.9, "z": 29.05},
    ]
    new_outer, new_bore, _ = nominal_revolve(outer, bore, [14, 29], [], [], tolerance=0.3)
    outer_step = [p["z"] for p in new_outer][1]
    bore_step = [p["z"] for p in new_bore][1]
    assert outer_step == 14 and bore_step != outer_step


def test_cross_hole_zone_is_a_cylinder_not_the_intersection_arcs():
    from app.ai.cad_views.nominals import bridge_cross_holes

    # Профиль по дугам пересечения отверстия Ø5 с цилиндром Ø11,5.
    outer = [
        {"r": 5.75, "z": 6.5},
        {"r": 5.75, "z": 9.5},
        {"r": 5.4, "z": 11.1},
        {"r": 5.4, "z": 12.3},
        {"r": 5.75, "z": 14.6},
        {"r": 5.75, "z": 16.2},
    ]
    hole = {"kind": "hole", "diameter_mm": 5.0, "origin_mm": [0.0, 7.0, 11.9], "axis": [0, -1, 0]}
    new_outer, _bore, bridged = bridge_cross_holes(outer, [], [hole])
    assert bridged == 1
    inside = [p["r"] for p in new_outer if 8.5 <= p["z"] <= 15.3]
    assert inside and all(r == 5.75 for r in inside)


def test_chamfer_goes_to_the_threaded_end():
    from app.ai.cad_views.nominals import chamfer_threaded_end

    outer = [
        {"r": 5.0, "z": 0.0},
        {"r": 5.0, "z": 3.9},
        {"r": 6.4, "z": 3.9},
        {"r": 6.4, "z": 29.0},
    ]
    new_outer, note = chamfer_threaded_end(outer, [0.5], [10.0])
    assert note and new_outer[0] == {"r": 4.5, "z": 0.0} and new_outer[1]["z"] == 0.5
    same, none = chamfer_threaded_end(outer, [0.5], [])
    assert none is None and same == outer


def test_a_chain_link_joins_neighbouring_stations_and_does_not_skip_one():
    """shaft-6: замеры уступов у канавок на ~1 мм короче; «18» от станции 115
    давало 97 (перескакивая уступ 99,6), и от неверной 97 − 18 = 79 съезжала
    вся цепочка. Звено цепочки соединяет СОСЕДНИЕ станции."""
    from app.ai.cad_views.nominals import snap_axis

    stations = [0.0, 79.19, 79.37, 97.05, 99.57, 115.09, 115.63, 163.79, 164.15, 181.65, 182.01]
    stations.append(195.0)
    mapping = snap_axis(
        stations, [195, 20, 15, 50, 18, 12], tolerance=1.73, overall=195, chain=True
    )
    assert mapping[182.01] == mapping[181.65] == 183
    assert mapping[164.15] == mapping[163.79] == 165
    assert mapping[115.09] == mapping[115.63] == 115
    assert mapping[99.57] == 100
    assert mapping[79.19] == mapping[79.37] == 80
