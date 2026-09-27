"""Метод `views`, E1: надписи листа против построенного тела."""

from __future__ import annotations


def test_revolve_labels_found_on_body_and_missing_listed():
    from app.ai.cad_views.checks import label_coverage
    from app.ai.cad_views.pipeline import ViewsResult

    result = ViewsResult(
        True,
        profile={
            "outer": [
                {"r": 10.0, "z": 0.0},
                {"r": 10.0, "z": 30.0},
                {"r": 15.0, "z": 30.0},
                {"r": 15.0, "z": 50.0},
            ],
            "bore": [],
        },
    )
    coverage = label_coverage(result, ["Ø20", "Ø30", "30", "50", "Ø12", "Ra 3,2", "Ø20"])
    assert coverage["explained"] == ["Ø20", "Ø30", "30", "50"]
    assert coverage["missing"] == ["Ø12"]
    assert coverage["share"] == 0.8


def test_extrude_positions_and_holes_found_on_body():
    from app.ai.cad_views.checks import label_coverage
    from app.ai.cad_views.pipeline import ViewsResult

    result = ViewsResult(
        True,
        profile={
            "kind": "extrude",
            "thickness_mm": 3.0,
            "sketch": [
                {"kind": "line", "to": [90.0, 0.0]},
                {"kind": "line", "to": [90.0, 100.0]},
                {"kind": "line", "to": [0.0, 100.0]},
                {"kind": "line", "to": [0.0, 0.0]},
            ],
        },
        features=[
            {
                "kind": "hole",
                "params": {"diameter_mm": 10.0, "center_x_mm": 80.0, "center_y_mm": 14.0},
            }
        ],
    )
    coverage = label_coverage(result, ["90", "100", "10", "14", "Ø10", "s3", "Ø16"])
    assert coverage["missing"] == ["Ø16"]
