"""Отверстия пластины: глухие и резьбовые сквозь схему, дерево и лист (X1)."""

from app.ai.cad_ir.sheet_from_solid import _hole_cut_and_text
from app.ai.cad_recognize.spec_vectorize import EngineeringDrawingSpec
from app.ai.cad_solid import feature_tree_from_spec


def _spec(holes: list[dict]) -> dict:
    return EngineeringDrawingSpec.model_validate(
        {
            "schema_version": 1,
            "main_view": {
                "type": "пластина",
                "profile": {
                    "shape": "rectangle",
                    "width_mm": 100.0,
                    "height_mm": 60.0,
                    "thickness_mm": 20.0,
                    "holes": holes,
                    "hole_patterns": [],
                    "slots": [],
                },
            },
            "views": [],
            "dimensions": [],
            "annotations": [],
            "title_block": {},
            "unresolved": [],
        }
    ).model_dump(mode="json")


M8 = {"designation": "M8", "nominal_diameter_mm": 8.0, "internal": True, "length_mm": 12.0}


def test_blind_and_tapped_holes_reach_the_kernel_from_the_plan_face():
    spec = _spec(
        [
            {"center_x_mm": -25.0, "center_y_mm": 0.0, "diameter_mm": 10.0, "depth_mm": 12.0},
            {
                "center_x_mm": 25.0,
                "center_y_mm": 0.0,
                "diameter_mm": 8.0,
                "depth_mm": 15.0,
                "thread": M8,
            },
        ]
    )
    tree = feature_tree_from_spec(spec)
    holes = [f.params for f in tree.features if f.kind == "hole"]
    threads = [f.params for f in tree.features if f.kind == "thread"]
    # Глухое — с грани плана (zmin): с zmax на плане оно выходило штриховым.
    assert holes[0] == {
        "diameter_mm": 10.0,
        "center_x_mm": 25.0,  # от угла пластины 100 × 60
        "center_y_mm": 30.0,
        "through": False,
        "depth_mm": 12.0,
        "from_face": "zmin",
    }
    # Резьбовое режется по Ø впадин M8 (8 − 1,0825·1,25), резьба — косметическая.
    assert abs(holes[1]["diameter_mm"] - 6.646835) < 1e-6
    assert threads[0]["spec"] == "M8" and threads[0]["internal"] and threads[0]["length_mm"] == 12.0


def test_the_sheet_labels_holes_by_what_they_are():
    assert _hole_cut_and_text({"diameter_mm": 8.0, "depth_mm": 15.0, "thread": M8}) == (
        6.647,
        "M8 гл.15",
    )
    assert _hole_cut_and_text({"diameter_mm": 10.0, "depth_mm": 12.0}) == (10.0, "Ø10 гл.12")
    assert _hole_cut_and_text({"diameter_mm": 6.0}) == (6.0, "")
