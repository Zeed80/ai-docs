"""Tests for app.ai.ifc_reader (Ф5.1 — IFC reader promoted to a service).

Round-trips through the project's own compile_construction_ifc so no
external fixture .ifc file is needed: build a ConstructionModel -> compile
to real IFC bytes -> read those bytes back with ifc_to_construction_model
-> assert the geometry survives exactly (unrotated boxes, so the read-back
AABB must match the original box fields bit-for-bit up to float rounding).
"""

import pathlib

import pytest

ifcopenshell = pytest.importorskip("ifcopenshell")

from app.ai.construction_emg import ConstructionModel, compile_construction_ifc
from app.ai.ifc_reader import ifc_to_construction_model, project_ifc


def _wall_opening_model() -> ConstructionModel:
    return ConstructionModel.model_validate(
        {
            "site_name": "Тестовая площадка",
            "building_name": "Тестовое здание",
            "storeys": [{"id": "l1", "name": "Этаж 1", "elevation_mm": 0}],
            "elements": [
                {
                    "id": "w1",
                    "kind": "wall",
                    "name": "Стена",
                    "storey_id": "l1",
                    "material": "Бетон",
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
                    "name": "Дверной проём",
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
        }
    )


def test_round_trips_wall_and_opening_geometry_exactly(tmp_path: pathlib.Path):
    original = _wall_opening_model()
    ifc_bytes, compile_report = compile_construction_ifc(original)
    assert compile_report["valid"] is True

    ifc_path = tmp_path / "roundtrip.ifc"
    ifc_path.write_bytes(ifc_bytes)

    read_back, report = ifc_to_construction_model(ifc_path)

    assert read_back is not None, report
    assert report["skipped"] == []
    assert report["mapped_elements"] == 2
    assert report["storeys"] == 1
    assert "blocked" not in report

    # The reader deliberately keys elements by IFC GlobalId (the universally
    # stable identifier any authoring tool produces), not by the original
    # "w1"/"o1" ConstructionModel ids compile_construction_ifc happened to
    # stash in Tag/Description -- so look elements up by kind/name instead.
    by_kind = {item.kind: item for item in read_back.elements}
    wall = by_kind["wall"]
    opening = by_kind["opening"]

    assert wall.name == "Стена"
    assert wall.box.width_mm == pytest.approx(5000, abs=1e-6)
    assert wall.box.depth_mm == pytest.approx(200, abs=1e-6)
    assert wall.box.height_mm == pytest.approx(3000, abs=1e-6)
    assert wall.box.x_mm == pytest.approx(0, abs=1e-6)

    assert opening.name == "Дверной проём"
    assert opening.host_id == wall.id
    assert opening.box.x_mm == pytest.approx(1000, abs=1e-6)
    assert opening.box.width_mm == pytest.approx(900, abs=1e-6)
    assert opening.box.height_mm == pytest.approx(2100, abs=1e-6)

    assert read_back.storeys[0].name == "Этаж 1"
    assert read_back.storeys[0].elevation_mm == pytest.approx(0, abs=1e-6)


def test_round_trips_slab_column_space(tmp_path: pathlib.Path):
    model = ConstructionModel.model_validate(
        {
            "site_name": "Площадка",
            "building_name": "Здание",
            "storeys": [{"id": "l1", "name": "Этаж 1", "elevation_mm": 0}],
            "elements": [
                {
                    "id": "s1",
                    "kind": "slab",
                    "name": "Плита",
                    "storey_id": "l1",
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
                    "id": "c1",
                    "kind": "column",
                    "name": "Колонна",
                    "storey_id": "l1",
                    "box": {
                        "x_mm": 500,
                        "y_mm": 500,
                        "z_mm": 250,
                        "width_mm": 400,
                        "depth_mm": 400,
                        "height_mm": 3350,
                    },
                },
                {
                    "id": "sp1",
                    "kind": "space",
                    "name": "Помещение",
                    "storey_id": "l1",
                    "box": {
                        "x_mm": 0,
                        "y_mm": 0,
                        "z_mm": 0,
                        "width_mm": 8000,
                        "depth_mm": 6000,
                        "height_mm": 3000,
                    },
                },
            ],
        }
    )
    ifc_bytes, compile_report = compile_construction_ifc(model)
    assert compile_report["valid"] is True
    ifc_path = tmp_path / "mixed.ifc"
    ifc_path.write_bytes(ifc_bytes)

    read_back, report = ifc_to_construction_model(ifc_path)

    assert read_back is not None, report
    assert report["mapped_elements"] == 3
    kinds = sorted(item.kind for item in read_back.elements)
    assert kinds == ["column", "slab", "space"]


def test_site_and_building_names_read_from_ifc_when_not_overridden(tmp_path: pathlib.Path):
    model = _wall_opening_model()
    ifc_bytes, _ = compile_construction_ifc(model)
    ifc_path = tmp_path / "names.ifc"
    ifc_path.write_bytes(ifc_bytes)

    read_back, _ = ifc_to_construction_model(ifc_path)

    assert read_back.site_name == "Тестовая площадка"
    assert read_back.building_name == "Тестовое здание"


def test_explicit_names_override_ifc_content(tmp_path: pathlib.Path):
    model = _wall_opening_model()
    ifc_bytes, _ = compile_construction_ifc(model)
    ifc_path = tmp_path / "override.ifc"
    ifc_path.write_bytes(ifc_bytes)

    read_back, _ = ifc_to_construction_model(
        ifc_path, site_name="Другая площадка", building_name="Другое здание"
    )

    assert read_back.site_name == "Другая площадка"
    assert read_back.building_name == "Другое здание"


def test_empty_ifc_file_is_blocked_not_guessed(tmp_path: pathlib.Path):
    import ifcopenshell.api.project
    import ifcopenshell.api.unit

    empty = ifcopenshell.api.project.create_file(version="IFC4")
    ifcopenshell.api.root.create_entity(empty, ifc_class="IfcProject", name="Empty")
    ifcopenshell.api.unit.assign_unit(empty)
    ifc_path = tmp_path / "empty.ifc"
    ifc_path.write_text(empty.to_string())

    model, report = ifc_to_construction_model(ifc_path)

    assert model is None
    assert report["blocked"] is True
    assert report["blocked_reason"] == "no_storeys"


def test_project_ifc_still_works_after_relocation(tmp_path: pathlib.Path):
    """project_ifc (moved verbatim from scripts/project_ifc_views.py) still projects views."""
    model = _wall_opening_model()
    ifc_bytes, _ = compile_construction_ifc(model)
    ifc_path = tmp_path / "views.ifc"
    ifc_path.write_bytes(ifc_bytes)

    payload = project_ifc(ifc_path)

    assert payload["ifc_schema"] == "IFC4"
    assert payload["elements"]
    assert payload["geometry_failures"] == []
    assert set(payload["views"].keys()) == {"plan", "front", "side", "plan_section"}
