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
