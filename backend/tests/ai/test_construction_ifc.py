import pytest

ifcopenshell = pytest.importorskip("ifcopenshell")

from app.ai.construction_emg import ConstructionModel, compile_construction_ifc


def test_construction_ifc_is_reopenable_and_deterministic():
    model = ConstructionModel.model_validate({
        "site_name": "Test site",
        "building_name": "Test building",
        "storeys": [{"id": "l1", "name": "Level 1", "elevation_mm": 0}],
        "elements": [
            {
                "id": "w1",
                "kind": "wall",
                "name": "Wall",
                "storey_id": "l1",
                "material": "Concrete",
                "box": {
                    "x_mm": 0,
                    "y_mm": 0,
                    "z_mm": 0,
                    "width_mm": 5000,
                    "depth_mm": 200,
                    "height_mm": 3000,
                },
            },
            {
                "id": "o1",
                "kind": "opening",
                "name": "Door opening",
                "storey_id": "l1",
                "host_id": "w1",
                "box": {
                    "x_mm": 1000,
                    "y_mm": 0,
                    "z_mm": 0,
                    "width_mm": 900,
                    "depth_mm": 200,
                    "height_mm": 2100,
                },
            },
        ],
    })

    first, first_report = compile_construction_ifc(model)
    second, second_report = compile_construction_ifc(model)

    assert first == second
    assert first_report["ifc_sha256"] == second_report["ifc_sha256"]
    assert first_report["valid"] is True
    assert first_report["geometry_failures"] == []
    assert first_report["product_class_counts"] == {
        "IfcOpeningElement": 1,
        "IfcWall": 1,
    }
    assert first_report["opening_relation_count"] == 1


def test_multi_storey_ifc_relationship_members_have_canonical_order():
    elements = []
    for index, storey_id in enumerate(("l1", "l2")):
        elements.extend([
            {
                "id": f"slab-{index}",
                "kind": "slab",
                "name": f"Slab {index}",
                "storey_id": storey_id,
                "material": "Concrete",
                "box": {
                    "x_mm": 0,
                    "y_mm": 0,
                    "z_mm": 0,
                    "width_mm": 8000,
                    "depth_mm": 6000,
                    "height_mm": 250,
                },
            },
            {
                "id": f"column-{index}",
                "kind": "column",
                "name": f"Column {index}",
                "storey_id": storey_id,
                "material": "Concrete",
                "box": {
                    "x_mm": 500,
                    "y_mm": 500,
                    "z_mm": 250,
                    "width_mm": 400,
                    "depth_mm": 400,
                    "height_mm": 3350,
                },
            },
        ])
    model = ConstructionModel.model_validate({
        "site_name": "Test site",
        "building_name": "Two storey frame",
        "storeys": [
            {"id": "l1", "name": "Level 1", "elevation_mm": 0},
            {"id": "l2", "name": "Level 2", "elevation_mm": 3600},
        ],
        "elements": elements,
    })

    artifacts = [compile_construction_ifc(model)[0] for _ in range(3)]

    assert artifacts[0] == artifacts[1] == artifacts[2]
