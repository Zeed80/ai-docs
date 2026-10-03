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


def test_an_unscaled_sketch_takes_diameters_by_their_order():
    """Втулка 793539cc_p015 нарисована не в масштабе (Ø13 — как Ø10,7): Ø
    площадок — надписи по порядку величины, расточка — наименьшие."""
    from app.ai.cad_views.pipeline import _ordinal_diameters

    outer = [
        {"r": 10.8, "z": 0.0},
        {"r": 10.8, "z": 2.0},
        {"r": 9.1, "z": 2.0},
        {"r": 9.1, "z": 9.0},
    ]
    bore = [{"r": 5.35, "z": -0.05}, {"r": 5.35, "z": 9.05}]
    new_outer, new_bore = _ordinal_diameters(outer, bore, [22.0, 13.0, 18.0], [])
    assert [p["r"] for p in new_outer] == [11.0, 11.0, 9.0, 9.0]
    assert [p["r"] for p in new_bore] == [6.5, 6.5]
    # Надписей больше, чем площадок: наружным — наибольшие, если пропорции
    # замера с ними согласны (лишняя — элемент не на силуэте) …
    surplus = _ordinal_diameters(outer, [], [22.0, 13.0, 18.0], [])
    assert surplus is not None and [p["r"] for p in surplus[0]] == [11.0, 11.0, 9.0, 9.0]
    # … иначе не угадывается.
    narrow = [{**p, "r": 3.0} if p["r"] < 10 else p for p in outer]
    assert _ordinal_diameters(narrow, [], [22.0, 13.0, 18.0], []) is None


def test_a_whole_nominal_wins_over_a_groove_diameter_in_tolerance():
    """Реальный p007: Ø дна канавки «Ø49,5» с выносного элемента ближе к
    замеру 49,6, чем номинал ступени Ø50, — ступень привязывалась к канавке."""
    from app.ai.cad_views.nominals import snap_diameter

    assert snap_diameter(49.6, [50.0, 49.5, 25.0]) == 50.0
    assert snap_diameter(24.6, [24.5, 25.0]) == 25.0
    # Единственный кандидат — как раньше, и мелкие номиналы не трогаются.
    assert snap_diameter(6.48, [6.0, 6.5]) == 6.5
    assert snap_diameter(49.6, [49.5]) == 49.5


def test_an_unlabelled_spike_over_a_plateau_is_flattened():
    from app.ai.cad_views.nominals import _flatten_spikes

    # z4-r4: след контура паза над Ø25 — не ступень Ø26,24.
    points = [
        {"z": 15.0, "r": 12.5},
        {"z": 19.0, "r": 12.5},
        {"z": 19.0, "r": 13.12},
        {"z": 25.0, "r": 12.5},
        {"z": 31.0, "r": 12.5},
        {"z": 31.0, "r": 17.5},
        {"z": 60.0, "r": 17.5},
    ]
    flat = _flatten_spikes(points, [25.0, 35.0])
    assert {p["r"] for p in flat} == {12.5, 17.5}
    # Надписанный Ø между равными соседями — настоящая ступень (бурт).
    collar = [dict(p) for p in points]
    collar[2]["r"] = 13.0
    assert 13.0 in {p["r"] for p in _flatten_spikes(collar, [25.0, 26.0, 35.0])}


def test_a_measured_keyway_follows_the_station_mapping():
    from app.ai.cad_views.nominals import remap_station

    # z4-r4: уступы 66,06 → 67 и 93,61 → 93; паз в замере 69,24…91,45.
    mapping = {0.0: 0.0, 66.06: 67.0, 93.61: 93.0, 185.0: 185.0}
    assert abs(remap_station(69.24, mapping) - 70.0) < 0.05
    assert abs(remap_station(91.45, mapping) - 90.97) < 0.05
    # Вне привязанных станций — как есть.
    assert remap_station(190.0, mapping) == 190.0


def test_a_long_end_plateau_with_a_thread_label_is_the_thread():
    from app.ai.cad_views.nominals import _thread_ends

    # z4-r4: M18 нарисована как 16,2 и привязалась к Ø15,7 проточки.
    points = [
        {"z": 0.0, "r": 7.85},
        {"z": 13.0, "r": 7.85},
        {"z": 13.0, "r": 12.5},
        {"z": 160.0, "r": 12.5},
        {"z": 160.0, "r": 11.0},
        {"z": 185.0, "r": 11.0},
    ]
    out = _thread_ends(points, [18.0, 24.0])
    assert out[0]["r"] == out[1]["r"] == 9.0
    # Целый надписанный Ø22 на другом конце — номинал ступени, не M24.
    assert out[-1]["r"] == 11.0


def test_an_unlabelled_largest_plateau_is_the_overall_diameter():
    from app.ai.cad_views.nominals import _overall_diameter

    # Колесо part_06: венец мерился 39,48 при Ø46 по вершинам зубьев.
    points = [
        {"z": 0.0, "r": 19.74},
        {"z": 6.0, "r": 19.74},
        {"z": 6.0, "r": 7.0},
        {"z": 15.0, "r": 7.0},
    ]
    out = _overall_diameter(points, [46.0, 38.0, 16.0, 14.0, 7.0])
    assert out[0]["r"] == out[1]["r"] == 23.0
    # Надписанная наибольшая площадка (Ø38) — не трогается.
    labelled = [dict(p, r=19.0) if p["r"] > 10 else p for p in points]
    assert _overall_diameter(labelled, [46.0, 38.0, 14.0])[0]["r"] == 19.0
