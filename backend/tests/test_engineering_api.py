"""Engineering-project API: immutable revisions and traceable projections."""

import hashlib
import io
import json
import uuid
import zipfile

import pytest
from httpx import AsyncClient

from app.db.models import (
    CadIrRevision,
    Drawing,
    DrawingAssemblyBOM,
    DrawingStatus,
    EngineeringAssemblyComponent,
    ImageGeneration,
)


@pytest.mark.asyncio
async def test_revision_lifecycle_and_projection(client: AsyncClient, db_session):
    project_response = await client.post("/api/engineering/projects", json={"name": "Корпус редуктора", "code": "ENG-001"})
    assert project_response.status_code == 201
    project_id = project_response.json()["id"]

    first = await client.post(f"/api/engineering/projects/{project_id}/revisions", json={
        "base_revision": None,
        "payload": {"schema_version": 1, "parts": []},
        "validation": {"issues": []},
        "created_by": "engineer",
    })
    assert first.status_code == 201
    revision = first.json()
    assert revision["revision"] == 0
    assert revision["status"] == "validated"

    drawing = Drawing(filename="engineering-detail.dxf", format="dxf", status=DrawingStatus.analyzed)
    db_session.add(drawing)
    await db_session.commit()
    projection = await client.post(f"/api/engineering/revisions/{revision['id']}/projections", json={
        "projection_type": "drawing",
        "entity_type": "drawing",
        "entity_id": str(drawing.id),
    })
    assert projection.status_code == 201

    approved = await client.post(f"/api/engineering/revisions/{revision['id']}/approve", json={"approved_by": "chief-engineer"})
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"

    frozen = await client.post(f"/api/engineering/revisions/{revision['id']}/projections", json={
        "projection_type": "drawing", "entity_type": "drawing", "entity_id": str(drawing.id)
    })
    assert frozen.status_code == 400


@pytest.mark.asyncio
async def test_revision_can_reference_immutable_cad_ir_snapshot(client: AsyncClient, db_session):
    project = (await client.post("/api/engineering/projects", json={"name": "CAD связь"})).json()
    revision = (await client.post(f"/api/engineering/projects/{project['id']}/revisions", json={"base_revision": None})).json()
    generation = ImageGeneration(operation="vectorize")
    db_session.add(generation)
    await db_session.flush()
    cad_revision = CadIrRevision(generation_id=generation.id, revision=0, ir_path="cad/snapshot.json")
    db_session.add(cad_revision)
    await db_session.commit()
    projection = await client.post(f"/api/engineering/revisions/{revision['id']}/projections", json={
        "projection_type": "cad_source",
        "entity_type": "cad_ir_revision",
        "entity_id": str(cad_revision.id),
    })
    assert projection.status_code == 201
    assert projection.json()["entity_type"] == "cad_ir_revision"
    validation = await client.post(f"/api/engineering/revisions/{revision['id']}/validate")
    assert validation.status_code == 200
    assert validation.json()["status"] == "failed"
    assert validation.json()["findings"][0]["code"] == "CAD_IR_NOT_APPROVED"


@pytest.mark.asyncio
async def test_revision_conflict_and_validation_gate(client: AsyncClient):
    project = (await client.post("/api/engineering/projects", json={"name": "Фланец"})).json()
    project_id = project["id"]
    rejected = await client.post(f"/api/engineering/projects/{project_id}/revisions", json={"base_revision": 0})
    assert rejected.status_code == 409

    revision = (await client.post(f"/api/engineering/projects/{project_id}/revisions", json={
        "base_revision": None,
        "validation": {"issues": [{"severity": "error", "code": "SCALE_UNKNOWN"}]},
    })).json()
    assert revision["status"] == "needs_review"
    approval = await client.post(f"/api/engineering/revisions/{revision['id']}/approve", json={"approved_by": "chief-engineer"})
    assert approval.status_code == 400


@pytest.mark.asyncio
async def test_material_assignment_is_revisioned(client: AsyncClient):
    material = (await client.post("/api/engineering/materials", json={
        "designation": "40Х", "standard": "ГОСТ 4543-2016", "density_kg_m3": 7850,
    })).json()
    project = (await client.post("/api/engineering/projects", json={"name": "Шестерня"})).json()
    revision = (await client.post(f"/api/engineering/projects/{project['id']}/revisions", json={"base_revision": None})).json()
    assigned = await client.post(f"/api/engineering/revisions/{revision['id']}/materials", json={
        "material_id": material["id"], "object_key": "part:gear",
    })
    assert assigned.status_code == 201
    assert assigned.json()["material"]["designation"] == "40Х"


@pytest.mark.asyncio
async def test_assembly_reports_aabb_collision(client: AsyncClient):
    project = (await client.post("/api/engineering/projects", json={"name": "Редуктор"})).json()
    revision = (await client.post(f"/api/engineering/projects/{project['id']}/revisions", json={"base_revision": None})).json()
    assembly = (await client.post(f"/api/engineering/revisions/{revision['id']}/assemblies", json={"name": "Главная"})).json()
    for key, bounds in (("housing", {"x_min": 0, "x_max": 10, "y_min": 0, "y_max": 10, "z_min": 0, "z_max": 10}), ("shaft", {"x_min": 9, "x_max": 12, "y_min": 0, "y_max": 2, "z_min": 0, "z_max": 2})):
        response = await client.post(f"/api/engineering/assemblies/{assembly['id']}/components", json={"instance_key": key, "designation": key, "bounds": bounds})
        assert response.status_code == 201
    report = await client.post(f"/api/engineering/assemblies/{assembly['id']}/validate")
    assert report.status_code == 200
    assert report.json()["collisions"] == [["housing", "shaft"]]


async def _digitized_child_drawing(
    client: AsyncClient, db_session, *, drawing_number: str, revision_id: str
) -> Drawing:
    """A Drawing already linked to an EngineeringRevision via /projections."""
    drawing = Drawing(
        filename=f"{drawing_number}.dxf", format="dxf", status=DrawingStatus.analyzed,
        drawing_number=drawing_number,
    )
    db_session.add(drawing)
    await db_session.commit()
    projection = await client.post(f"/api/engineering/revisions/{revision_id}/projections", json={
        "projection_type": "drawing", "entity_type": "drawing", "entity_id": str(drawing.id),
    })
    assert projection.status_code == 201
    await db_session.refresh(drawing)
    return drawing


async def _assembly_drawing_with_bom(db_session, items: list[dict]) -> Drawing:
    drawing = Drawing(filename="sb.pdf", format="pdf", status=DrawingStatus.analyzed)
    db_session.add(drawing)
    await db_session.flush()
    for item in items:
        db_session.add(DrawingAssemblyBOM(drawing_id=drawing.id, **item))
    await db_session.commit()
    await db_session.refresh(drawing)
    return drawing


@pytest.mark.asyncio
async def test_assembly_components_from_bom_matches_digitized_drawing(
    client: AsyncClient, db_session
):
    project = (await client.post("/api/engineering/projects", json={"name": "Редуктор БОМ"})).json()
    assembly_revision = (await client.post(
        f"/api/engineering/projects/{project['id']}/revisions", json={"base_revision": None}
    )).json()
    assembly = (await client.post(
        f"/api/engineering/revisions/{assembly_revision['id']}/assemblies", json={"name": "Главная"}
    )).json()

    child_project = (await client.post("/api/engineering/projects", json={"name": "Вал"})).json()
    child_revision = (await client.post(
        f"/api/engineering/projects/{child_project['id']}/revisions", json={"base_revision": None}
    )).json()
    child_drawing = await _digitized_child_drawing(
        client, db_session, drawing_number="ДП-001", revision_id=child_revision["id"]
    )

    sb_drawing = await _assembly_drawing_with_bom(db_session, [
        {
            "item_no": 1, "designation": "Вал", "quantity": 1.0,
            "drawing_number": "ДП-001", "confidence": 0.9,
        },
        {
            "item_no": 2, "designation": "Подшипник 6205", "quantity": 2.0,
            "drawing_number": "ДП-999", "confidence": 0.8,
        },
    ])

    resp = await client.post(
        f"/api/engineering/assemblies/{assembly['id']}/components/from-bom",
        json={"drawing_id": str(sb_drawing.id)},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["components"]) == 2
    matched = {c["instance_key"]: c for c in body["components"]}
    assert matched["bom-1"]["component_revision_id"] == str(child_drawing.engineering_revision_id)
    assert matched["bom-2"]["component_revision_id"] is None
    assert len(body["unresolved"]) == 1
    assert body["unresolved"][0] == {
        "item_no": 2, "designation": "Подшипник 6205",
        "drawing_number": "ДП-999", "reason": "no_match",
    }


@pytest.mark.asyncio
async def test_assembly_components_from_bom_is_idempotent(client: AsyncClient, db_session):
    project = (await client.post("/api/engineering/projects", json={"name": "Идемпотент"})).json()
    revision = (await client.post(
        f"/api/engineering/projects/{project['id']}/revisions", json={"base_revision": None}
    )).json()
    assembly = (await client.post(
        f"/api/engineering/revisions/{revision['id']}/assemblies", json={"name": "Сборка"}
    )).json()
    sb_drawing = await _assembly_drawing_with_bom(db_session, [
        {
            "item_no": 1, "designation": "Гайка", "quantity": 4.0,
            "drawing_number": None, "confidence": 0.5,
        },
    ])

    first = await client.post(
        f"/api/engineering/assemblies/{assembly['id']}/components/from-bom",
        json={"drawing_id": str(sb_drawing.id)},
    )
    second = await client.post(
        f"/api/engineering/assemblies/{assembly['id']}/components/from-bom",
        json={"drawing_id": str(sb_drawing.id)},
    )
    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["components"][0]["id"] == second.json()["components"][0]["id"]
    assert second.json()["unresolved"][0]["reason"] == "missing_drawing_number"

    listed = await client.post(f"/api/engineering/assemblies/{assembly['id']}/validate")
    assert listed.status_code == 200
    assert len(listed.json()["active_instances"]) == 1


@pytest.mark.asyncio
async def test_assembly_components_from_bom_ambiguous_stays_unresolved(
    client: AsyncClient, db_session
):
    project = (await client.post("/api/engineering/projects", json={"name": "Дубликат"})).json()
    revision = (await client.post(
        f"/api/engineering/projects/{project['id']}/revisions", json={"base_revision": None}
    )).json()
    assembly = (await client.post(
        f"/api/engineering/revisions/{revision['id']}/assemblies", json={"name": "Сборка"}
    )).json()

    for label in ("A", "B"):
        dup_project = (await client.post(
            "/api/engineering/projects", json={"name": f"Дубль {label}"}
        )).json()
        dup_revision = (await client.post(
            f"/api/engineering/projects/{dup_project['id']}/revisions", json={"base_revision": None}
        )).json()
        await _digitized_child_drawing(
            client, db_session, drawing_number="ДП-777", revision_id=dup_revision["id"]
        )

    sb_drawing = await _assembly_drawing_with_bom(db_session, [
        {
            "item_no": 1, "designation": "Втулка", "quantity": 1.0,
            "drawing_number": "дп-777", "confidence": 0.7,
        },
    ])

    resp = await client.post(
        f"/api/engineering/assemblies/{assembly['id']}/components/from-bom",
        json={"drawing_id": str(sb_drawing.id)},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["components"][0]["component_revision_id"] is None
    assert body["unresolved"][0]["reason"] == "ambiguous"


@pytest.mark.asyncio
async def test_assembly_components_from_bom_requires_extraction(client: AsyncClient, db_session):
    project = (await client.post("/api/engineering/projects", json={"name": "Без БОМ"})).json()
    revision = (await client.post(
        f"/api/engineering/projects/{project['id']}/revisions", json={"base_revision": None}
    )).json()
    assembly = (await client.post(
        f"/api/engineering/revisions/{revision['id']}/assemblies", json={"name": "Сборка"}
    )).json()
    empty_drawing = Drawing(filename="empty-sb.pdf", format="pdf", status=DrawingStatus.analyzed)
    db_session.add(empty_drawing)
    await db_session.commit()

    resp = await client.post(
        f"/api/engineering/assemblies/{assembly['id']}/components/from-bom",
        json={"drawing_id": str(empty_drawing.id)},
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_assembly_components_from_bom_blocked_on_approved_revision(
    client: AsyncClient, db_session
):
    project = (await client.post("/api/engineering/projects", json={"name": "Утверждено"})).json()
    revision = (await client.post(
        f"/api/engineering/projects/{project['id']}/revisions", json={"base_revision": None}
    )).json()
    assembly = (await client.post(
        f"/api/engineering/revisions/{revision['id']}/assemblies", json={"name": "Сборка"}
    )).json()
    sb_drawing = await _assembly_drawing_with_bom(db_session, [
        {
            "item_no": 1, "designation": "Болт", "quantity": 4.0,
            "drawing_number": None, "confidence": 0.6,
        },
    ])
    approved = await client.post(
        f"/api/engineering/revisions/{revision['id']}/approve",
        json={"approved_by": "chief-engineer"},
    )
    assert approved.status_code == 200

    resp = await client.post(
        f"/api/engineering/assemblies/{assembly['id']}/components/from-bom",
        json={"drawing_id": str(sb_drawing.id)},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_assembly_model_graph_is_revisioned_through_graph_patch(
    client: AsyncClient,
    db_session,
    monkeypatch,
):
    graph_objects: dict[str, bytes] = {}
    monkeypatch.setattr(
        "app.services.engineering_model_graph.upload_file",
        lambda content, path, _content_type: graph_objects.setdefault(path, content),
    )
    monkeypatch.setattr(
        "app.services.engineering_model_graph.download_file",
        lambda path: graph_objects[path],
    )
    monkeypatch.setattr(
        "app.services.engineering_model_graph.delete_file",
        lambda path: graph_objects.pop(path, None),
    )

    async def exact_interference(components):
        return [], [item.instance_key for item in components], None

    monkeypatch.setattr(
        "app.api.engineering._exact_interference",
        exact_interference,
    )
    project = (await client.post(
        "/api/engineering/projects", json={"name": "EMG сборка"}
    )).json()
    revision = (await client.post(
        f"/api/engineering/projects/{project['id']}/revisions",
        json={"base_revision": None},
    )).json()
    assembly = (await client.post(
        f"/api/engineering/revisions/{revision['id']}/assemblies",
        json={"name": "Вал в корпусе", "designation": "ASM-EMG"},
    )).json()
    component_responses = []
    for key, designation, metadata in (
        ("housing", "Housing", {"grounded": True}),
        ("shaft", "Shaft", {}),
    ):
        component_responses.append(await client.post(
            f"/api/engineering/assemblies/{assembly['id']}/components",
            json={
                "instance_key": key,
                "designation": designation,
                "metadata": {
                    **metadata,
                    "shape": {
                        "kind": "box",
                        "width_mm": 10,
                        "height_mm": 10,
                        "depth_mm": 10,
                    },
                },
            },
        ))
    assert all(response.status_code == 201 for response in component_responses)
    mate = await client.post(
        f"/api/engineering/assemblies/{assembly['id']}/mates",
        json={
            "mate_type": "fixed",
            "first_instance_key": "housing",
            "second_instance_key": "shaft",
        },
    )
    assert mate.status_code == 201

    first = await client.post(
        f"/api/engineering/assemblies/{assembly['id']}/model-graph"
    )
    assert first.status_code == 200
    assert first.json()["revision"] == 0
    assert first.json()["profile"] == "assembly"
    assert first.json()["release_status"] == "blocked"

    step_bytes = b"ISO-10303-21; assembly test; END-ISO-10303-21;"
    step_sha = hashlib.sha256(step_bytes).hexdigest()
    report = {
        "components": [
            {"instance_key": "housing", "solid_count": 1, "edges": []},
            {"instance_key": "shaft", "solid_count": 1, "edges": []},
        ],
        "reopen": {
            "valid": True,
            "solid_count": 2,
            "step_sha256": step_sha,
        },
    }
    archive_buffer = io.BytesIO()
    with zipfile.ZipFile(archive_buffer, "w") as archive:
        archive.writestr("assembly.step", step_bytes)
        archive.writestr("assembly-report.json", json.dumps(report))

    class KernelResponse:
        status_code = 200
        content = archive_buffer.getvalue()
        text = ""

    class KernelClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **_kwargs):
            return KernelResponse()

    monkeypatch.setattr("httpx.AsyncClient", KernelClient)
    monkeypatch.setattr(
        "app.storage.upload_file",
        lambda content, path, _content_type: graph_objects.setdefault(path, content),
    )
    monkeypatch.setattr(
        "app.storage.delete_file",
        lambda path: graph_objects.pop(path, None),
    )
    monkeypatch.setattr(
        "app.storage.download_file",
        lambda path: graph_objects[path],
    )
    built = await client.post(
        f"/api/engineering/assemblies/{assembly['id']}/model-graph/build"
    )
    assert built.status_code == 200
    assert built.json()["revision"] == 1
    assert built.json()["artifact_sha256"] == step_sha
    assert built.json()["solid_count"] == 2
    assert built.json()["production_export_allowed"] is False

    drawing = await client.post(
        f"/api/engineering/assemblies/{assembly['id']}/model-graph/drawing"
    )
    assert drawing.status_code == 200
    assert drawing.json()["revision"] == 2
    assert drawing.json()["views"] == ["assembled", "exploded"]
    assert drawing.json()["production_export_allowed"] is True
    drawing_replay = await client.post(
        f"/api/engineering/assemblies/{assembly['id']}/model-graph/drawing"
    )
    assert drawing_replay.status_code == 200
    assert drawing_replay.json()["revision"] == 2
    assert drawing_replay.json()["idempotent_replay"] is True

    graph_rows = (
        await client.get(
            f"/api/engineering-model-graphs/projects/{project['id']}/graphs"
        )
    ).json()
    assembly_graph = next(
        item for item in graph_rows if item["graph_id"].startswith("assembly:")
    )
    admission = await client.get(
        f"/api/engineering-model-graphs/revisions/{assembly_graph['id']}"
        "/build-admission/production",
        params={"generator": "assembly_step"},
    )
    assert admission.status_code == 200
    assert admission.json()["schema_version"] == "emg-build-admission/1.0"
    assert admission.json()["allowed"] is True
    incompatible = await client.get(
        f"/api/engineering-model-graphs/revisions/{assembly_graph['id']}"
        "/build-admission/production",
        params={"generator": "construction_ifc"},
    )
    assert incompatible.status_code == 200
    assert incompatible.json()["allowed"] is False
    assert "generator_profile_incompatible" in {
        item["code"] for item in incompatible.json()["blockers"]
    }
    drawing_artifact = next(
        item for item in assembly_graph["graph"]["nodes"]
        if item["id"].startswith("artifact:assembly-drawing:")
    )
    artifact_url = (
        f"/api/engineering-model-graphs/revisions/{assembly_graph['id']}"
        f"/artifacts/{drawing_artifact['id']}"
    )
    downloaded = await client.get(artifact_url)
    assert downloaded.status_code == 200
    assert downloaded.headers["content-type"].startswith("image/svg+xml")
    assert (
        downloaded.headers["x-engineering-artifact-sha256"]
        == drawing.json()["artifact_sha256"]
    )
    report_download = await client.get(artifact_url, params={"kind": "report"})
    assert report_download.status_code == 200
    assert report_download.json()["views"] == ["assembled", "exploded"]

    artifact_path = drawing.json()["artifact_path"]
    original_svg = graph_objects[artifact_path]
    graph_objects[artifact_path] = original_svg + b"<!-- tampered -->"
    tampered = await client.get(artifact_url)
    assert tampered.status_code == 409
    graph_objects[artifact_path] = original_svg

    unchanged = await client.post(
        f"/api/engineering/assemblies/{assembly['id']}/model-graph"
    )
    assert unchanged.status_code == 200
    assert unchanged.json()["revision"] == 2
    assert unchanged.json()["release_status"] == "approved"

    shaft_id = component_responses[1].json()["id"]
    shaft = await db_session.get(EngineeringAssemblyComponent, uuid.UUID(shaft_id))
    assert shaft is not None
    shaft.quantity = 4
    await db_session.commit()

    second = await client.post(
        f"/api/engineering/assemblies/{assembly['id']}/model-graph"
    )
    assert second.status_code == 200
    assert second.json()["revision"] == 3
    active_quantities = [
        assertion["value"]["value"]
        for assertion in second.json()["graph"]["assertions"]
        if assertion["state"] == "active"
        and assertion["predicate"] == "component.quantity"
    ]
    assert sorted(active_quantities) == [1, 4]

    replay = await client.post(
        f"/api/engineering/assemblies/{assembly['id']}/model-graph"
    )
    assert replay.status_code == 200
    assert replay.json()["revision"] == 3


@pytest.mark.asyncio
async def test_construction_ifc_parse_endpoint_returns_model_and_report(
    client: AsyncClient, monkeypatch
):
    fake_model = {
        "site_name": "Площадка", "building_name": "Здание",
        "storeys": [{"id": "l1", "name": "Этаж 1", "elevation_mm": 0}],
        "elements": [{
            "id": "w1", "kind": "wall", "name": "Стена", "storey_id": "l1",
            "box": {
                "x_mm": 0, "y_mm": 0, "z_mm": 0,
                "width_mm": 5000, "depth_mm": 200, "height_mm": 3000,
            },
        }],
    }
    fake_report = {"ifc_schema": "IFC4", "mapped_elements": 1, "storeys": 1, "skipped": []}
    captured: dict = {}

    def _fake_reader(path, *, site_name=None, building_name=None):
        captured["site_name"] = site_name
        captured["building_name"] = building_name
        from app.ai.construction_emg import ConstructionModel
        return ConstructionModel.model_validate(fake_model), fake_report

    monkeypatch.setattr("app.ai.ifc_reader.ifc_to_construction_model", _fake_reader)

    resp = await client.post(
        "/api/engineering/construction/ifc/parse",
        params={"site_name": "Override"},
        files={"file": ("model.ifc", b"ISO-10303-21; fake; END-ISO-10303-21;",
                        "application/x-step")},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["construction_model"]["building_name"] == "Здание"
    assert body["report"] == fake_report
    assert captured["site_name"] == "Override"
    assert captured["building_name"] is None


@pytest.mark.asyncio
async def test_construction_ifc_parse_returns_null_model_when_blocked(
    client: AsyncClient, monkeypatch
):
    def _fake_reader(path, *, site_name=None, building_name=None):
        return None, {"blocked": True, "blocked_reason": "no_storeys", "skipped": []}

    monkeypatch.setattr("app.ai.ifc_reader.ifc_to_construction_model", _fake_reader)

    resp = await client.post(
        "/api/engineering/construction/ifc/parse",
        files={"file": ("empty.ifc", b"ISO-10303-21; fake; END-ISO-10303-21;",
                        "application/x-step")},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["construction_model"] is None
    assert body["report"]["blocked"] is True


@pytest.mark.asyncio
async def test_construction_ifc_parse_rejects_empty_upload(client: AsyncClient):
    resp = await client.post(
        "/api/engineering/construction/ifc/parse",
        files={"file": ("empty.ifc", b"", "application/x-step")},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_construction_graph_builds_reopened_ifc_and_replays_idempotently(
    client: AsyncClient,
    monkeypatch,
):
    graph_objects: dict[str, bytes] = {}
    for target in (
        "app.services.engineering_model_graph.upload_file",
        "app.storage.upload_file",
    ):
        monkeypatch.setattr(
            target,
            lambda content, path, _content_type: graph_objects.setdefault(path, content),
        )
    monkeypatch.setattr(
        "app.services.engineering_model_graph.download_file",
        lambda path: graph_objects[path],
    )
    for target in (
        "app.services.engineering_model_graph.delete_file",
        "app.storage.delete_file",
    ):
        monkeypatch.setattr(target, lambda path: graph_objects.pop(path, None))

    project = (await client.post(
        "/api/engineering/projects", json={"name": "IFC building"}
    )).json()
    payload = {
        "construction_model": {
            "site_name": "Site",
            "building_name": "Building",
            "storeys": [{"id": "l1", "name": "Level 1", "elevation_mm": 0}],
            "elements": [{
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
            }],
        },
    }
    revision = (await client.post(
        f"/api/engineering/projects/{project['id']}/revisions",
        json={"base_revision": None, "payload": payload},
    )).json()
    initial = await client.post(
        f"/api/engineering/revisions/{revision['id']}/construction-model-graph"
    )
    assert initial.status_code == 200
    assert initial.json()["profile"] == "construction"
    assert initial.json()["revision"] == 0
    assert initial.json()["release_status"] == "blocked"

    approved = await client.post(
        f"/api/engineering/revisions/{revision['id']}/approve",
        json={"approved_by": "chief-engineer"},
    )
    assert approved.status_code == 200

    ifc_bytes = b"ISO-10303-21; IFC4 test; END-ISO-10303-21;"
    ifc_sha = hashlib.sha256(ifc_bytes).hexdigest()
    report = {
        "valid": True,
        "ifc_sha256": ifc_sha,
        "product_class_counts": {"IfcWall": 1},
        "products": [{
            "source_id": "w1",
            "ifc_class": "IfcWall",
            "global_id": "0testGlobalId0000000000",
            "name": "Wall",
        }],
    }
    monkeypatch.setattr(
        "app.ai.construction_emg.compile_construction_ifc",
        lambda _model: (ifc_bytes, report),
    )

    synced = await client.post(
        f"/api/engineering/revisions/{revision['id']}/construction-model-graph"
    )
    assert synced.status_code == 200
    assert synced.json()["profile"] == "construction"
    assert synced.json()["revision"] == 1

    built = await client.post(
        f"/api/engineering/revisions/{revision['id']}/construction-model-graph/build"
    )
    assert built.status_code == 200
    assert built.json()["revision"] == 2
    assert built.json()["ifc_reopen_valid"] is True
    assert built.json()["production_export_allowed"] is False
    assert built.json()["provisional"] is True

    sheets = await client.post(
        f"/api/engineering/revisions/{revision['id']}/construction-model-graph/sheets"
    )
    assert sheets.status_code == 200
    assert sheets.json()["revision"] == 3
    assert sheets.json()["views"] == ["plan:l1", "section"]
    assert sheets.json()["production_export_allowed"] is True

    replay = await client.post(
        f"/api/engineering/revisions/{revision['id']}/construction-model-graph/sheets"
    )
    assert replay.status_code == 200
    assert replay.json()["revision"] == 3
    assert replay.json()["idempotent_replay"] is True


@pytest.mark.asyncio
async def test_system_graph_connectivity_is_approval_gated_and_idempotent(
    client: AsyncClient,
    monkeypatch,
):
    graph_objects: dict[str, bytes] = {}
    monkeypatch.setattr(
        "app.services.engineering_model_graph.upload_file",
        lambda content, path, _content_type: graph_objects.setdefault(path, content),
    )
    monkeypatch.setattr(
        "app.services.engineering_model_graph.download_file",
        lambda path: graph_objects[path],
    )
    monkeypatch.setattr(
        "app.services.engineering_model_graph.delete_file",
        lambda path: graph_objects.pop(path, None),
    )
    monkeypatch.setattr(
        "app.storage.upload_file",
        lambda content, path, _content_type: graph_objects.setdefault(path, content),
    )
    monkeypatch.setattr(
        "app.storage.delete_file",
        lambda path: graph_objects.pop(path, None),
    )
    project = (await client.post(
        "/api/engineering/projects", json={"name": "Hydraulic system"}
    )).json()
    revision = (await client.post(
        f"/api/engineering/projects/{project['id']}/revisions",
        json={
            "base_revision": None,
            "payload": {"system_model": {
                "profile": "hydraulic",
                "name": "Power unit",
                "system_kind": "hydraulic_power",
                "equipment": [
                    {"id": "pump", "name": "Pump", "equipment_type": "pump"},
                    {"id": "tank", "name": "Tank", "equipment_type": "reservoir"},
                ],
                "ports": [
                    {
                        "id": "pump-out",
                        "equipment_id": "pump",
                        "kind": "pressure",
                        "direction": "out",
                        "medium": "oil",
                    },
                    {
                        "id": "tank-in",
                        "equipment_id": "tank",
                        "kind": "return",
                        "direction": "in",
                        "medium": "oil",
                    },
                ],
                "connections": [{
                    "id": "line-1",
                    "first_port_id": "pump-out",
                    "second_port_id": "tank-in",
                }],
            }},
        },
    )).json()

    initial = await client.post(
        f"/api/engineering/revisions/{revision['id']}/system-model-graph"
    )
    assert initial.status_code == 200
    assert initial.json()["profile"] == "hydraulic"
    assert initial.json()["revision"] == 0
    assert initial.json()["production_export_allowed"] is False

    approved = await client.post(
        f"/api/engineering/revisions/{revision['id']}/approve",
        json={"approved_by": "chief-engineer"},
    )
    assert approved.status_code == 200
    promoted = await client.post(
        f"/api/engineering/revisions/{revision['id']}/system-model-graph"
    )
    assert promoted.status_code == 200
    assert promoted.json()["revision"] == 1
    assert promoted.json()["production_export_allowed"] is False

    diagram = await client.post(
        f"/api/engineering/revisions/{revision['id']}/system-model-graph/diagram"
    )
    assert diagram.status_code == 200
    assert diagram.json()["revision"] == 2
    assert diagram.json()["production_export_allowed"] is True
    diagram_replay = await client.post(
        f"/api/engineering/revisions/{revision['id']}/system-model-graph/diagram"
    )
    assert diagram_replay.status_code == 200
    assert diagram_replay.json()["revision"] == 2
    assert diagram_replay.json()["idempotent_replay"] is True

    replay = await client.post(
        f"/api/engineering/revisions/{revision['id']}/system-model-graph"
    )
    assert replay.status_code == 200
    assert replay.json()["revision"] == 2


@pytest.mark.asyncio
async def test_release_validation_promotes_clean_revision(client: AsyncClient):
    project = (await client.post("/api/engineering/projects", json={"name": "Втулка"})).json()
    revision = (await client.post(f"/api/engineering/projects/{project['id']}/revisions", json={"base_revision": None})).json()
    response = await client.post(f"/api/engineering/revisions/{revision['id']}/validate")
    assert response.status_code == 200
    assert response.json()["status"] == "passed"


@pytest.mark.asyncio
async def test_failed_analysis_case_blocks_release(client: AsyncClient):
    material = (await client.post("/api/engineering/materials", json={
        "designation": "Сталь", "yield_strength_mpa": 100,
    })).json()
    project = (await client.post("/api/engineering/projects", json={"name": "Тяга"})).json()
    revision = (await client.post(f"/api/engineering/projects/{project['id']}/revisions", json={"base_revision": None})).json()
    case = (await client.post(f"/api/engineering/revisions/{revision['id']}/analysis-cases", json={
        "name": "Осевое растяжение", "material_id": material["id"],
        "inputs": {"force_n": 2_000, "area_mm2": 10},
    })).json()
    run = await client.post(f"/api/engineering/analysis-cases/{case['id']}/run")
    assert run.status_code == 200
    assert run.json()["status"] == "failed"
    assert run.json()["results"]["safety_factor"] == 0.5
    validation = await client.post(f"/api/engineering/revisions/{revision['id']}/validate")
    assert validation.status_code == 200
    assert validation.json()["status"] == "failed"


@pytest.mark.asyncio
async def test_change_request_full_lifecycle(client: AsyncClient):
    """E3: create with mandatory reason + auto impact, reviewer signatures
    gate approval, apply mints a new draft revision from the affected one."""
    project = (await client.post("/api/engineering/projects", json={"name": "Изменения"})).json()
    revision = (
        await client.post(
            f"/api/engineering/projects/{project['id']}/revisions",
            json={"base_revision": None, "payload": {"schema_version": 1}},
        )
    ).json()

    created = await client.post(
        f"/api/engineering/projects/{project['id']}/change-requests",
        json={
            "title": "Увеличить диаметр расточки",
            "reason": "Не проходит подшипник 6205 по посадке",
            "affected_revision_id": revision["id"],
            "reviewers": ["chief", "techlead"],
            "created_by": "engineer",
        },
    )
    assert created.status_code == 201
    change = created.json()
    assert change["number"] == 1
    assert change["status"] == "review"
    assert change["impact"]["revision"] == 0

    # apply before approval is refused
    early = await client.post(f"/api/engineering/change-requests/{change['id']}/apply")
    assert early.status_code == 409

    # a non-reviewer cannot sign; a reviewer cannot sign twice
    assert (
        await client.post(
            f"/api/engineering/change-requests/{change['id']}/sign",
            json={"reviewer": "stranger", "decision": "approve"},
        )
    ).status_code == 403
    first = await client.post(
        f"/api/engineering/change-requests/{change['id']}/sign",
        json={"reviewer": "chief", "decision": "approve"},
    )
    assert first.json()["status"] == "review"  # one of two signatures
    assert (
        await client.post(
            f"/api/engineering/change-requests/{change['id']}/sign",
            json={"reviewer": "chief", "decision": "approve"},
        )
    ).status_code == 409
    second = await client.post(
        f"/api/engineering/change-requests/{change['id']}/sign",
        json={"reviewer": "techlead", "decision": "approve"},
    )
    assert second.json()["status"] == "approved"

    applied = await client.post(f"/api/engineering/change-requests/{change['id']}/apply")
    assert applied.status_code == 200
    body = applied.json()
    assert body["status"] == "applied"
    assert body["applied_revision_id"]
    detail = (await client.get(f"/api/engineering/projects/{project['id']}")).json()
    minted = [r for r in detail["revisions"] if r["id"] == body["applied_revision_id"]]
    assert minted and minted[0]["origin"] == "change_order"
    assert minted[0]["base_revision"] == 0
    assert minted[0]["status"] == "draft"


@pytest.mark.asyncio
async def test_change_request_reject_and_supersession(client: AsyncClient):
    project = (await client.post("/api/engineering/projects", json={"name": "Отказ и замена"})).json()
    revision = (
        await client.post(
            f"/api/engineering/projects/{project['id']}/revisions",
            json={"base_revision": None},
        )
    ).json()

    first = (
        await client.post(
            f"/api/engineering/projects/{project['id']}/change-requests",
            json={
                "title": "Вариант 1",
                "reason": "Первый подход",
                "affected_revision_id": revision["id"],
                "reviewers": ["chief"],
            },
        )
    ).json()
    rejected = await client.post(
        f"/api/engineering/change-requests/{first['id']}/sign",
        json={"reviewer": "chief", "decision": "reject", "comment": "не согласован"},
    )
    assert rejected.json()["status"] == "rejected"
    # a rejected request can no longer be signed or applied
    assert (
        await client.post(
            f"/api/engineering/change-requests/{first['id']}/sign",
            json={"reviewer": "chief", "decision": "approve"},
        )
    ).status_code == 409

    second = (
        await client.post(
            f"/api/engineering/projects/{project['id']}/change-requests",
            json={
                "title": "Вариант 2",
                "reason": "Учтены замечания",
                "affected_revision_id": revision["id"],
                "supersedes_id": first["id"],
                "reviewers": [],
            },
        )
    ).json()
    assert second["number"] == 2
    assert second["supersedes_id"] == first["id"]
    listed = (await client.get(f"/api/engineering/projects/{project['id']}/change-requests")).json()
    statuses = {item["id"]: item["status"] for item in listed}
    assert statuses[first["id"]] == "superseded"


@pytest.mark.asyncio
async def test_assembly_exact_interference_degrades_loudly_without_kernel(client: AsyncClient):
    """E5: components with declared occupancy solids go to the kernel; when it
    is unreachable the check degrades to AABB with an explicit note — never
    silently."""
    project = (await client.post("/api/engineering/projects", json={"name": "Точная сборка"})).json()
    revision = (await client.post(f"/api/engineering/projects/{project['id']}/revisions", json={"base_revision": None})).json()
    assembly = (await client.post(f"/api/engineering/revisions/{revision['id']}/assemblies", json={"name": "Сборка"})).json()
    for key in ("a", "b"):
        response = await client.post(
            f"/api/engineering/assemblies/{assembly['id']}/components",
            json={
                "instance_key": key,
                "designation": key,
                "metadata": {"shape": {"kind": "box", "width_mm": 10, "height_mm": 10, "depth_mm": 10}},
            },
        )
        assert response.status_code == 201
    report = (await client.post(f"/api/engineering/assemblies/{assembly['id']}/validate")).json()
    if report["degraded"]:
        # kernel unreachable (host test run): loud degradation, no exact data
        assert report["exact_collisions"] == []
    else:
        # kernel reachable (in-container run): both boxes sit at the origin —
        # full 10³ interpenetration must be reported with its volume
        assert report["exact_checked"] == ["a", "b"]
        [collision] = report["exact_collisions"]
        assert collision["volume_mm3"] == pytest.approx(1000, rel=1e-3)
        assert ["a", "b"] in report["collisions"]


@pytest.mark.asyncio
async def test_assembly_exact_result_overrides_aabb(client: AsyncClient, monkeypatch):
    """E5: for kernel-checked pairs the AABB verdict is discarded — a rotated
    part whose bounding boxes overlap but geometry clears is NOT a collision."""
    from app.api import engineering as engineering_api

    async def fake_exact(components):
        keys = [c.instance_key for c in components if not c.suppressed and c.metadata_.get("shape")]
        return [], keys, None  # kernel says: no interference

    monkeypatch.setattr(engineering_api, "_exact_interference", fake_exact)
    project = (await client.post("/api/engineering/projects", json={"name": "Поворот"})).json()
    revision = (await client.post(f"/api/engineering/projects/{project['id']}/revisions", json={"base_revision": None})).json()
    assembly = (await client.post(f"/api/engineering/revisions/{revision['id']}/assemblies", json={"name": "Сборка"})).json()
    overlapping = {"x_min": 0, "x_max": 10, "y_min": 0, "y_max": 10, "z_min": 0, "z_max": 10}
    for key in ("bar", "cube"):
        await client.post(
            f"/api/engineering/assemblies/{assembly['id']}/components",
            json={
                "instance_key": key, "designation": key,
                "bounds": overlapping,  # AABB says collision
                "metadata": {"shape": {"kind": "box", "width_mm": 10, "height_mm": 10, "depth_mm": 10}},
            },
        )
    report = (await client.post(f"/api/engineering/assemblies/{assembly['id']}/validate")).json()
    assert report["collisions"] == []  # exact verdict wins over the AABB guess
    assert sorted(report["exact_checked"]) == ["bar", "cube"]


async def _new_assembly(client: AsyncClient, name: str) -> str:
    project = (await client.post("/api/engineering/projects", json={"name": name})).json()
    revision = (await client.post(
        f"/api/engineering/projects/{project['id']}/revisions", json={"base_revision": None}
    )).json()
    assembly = (await client.post(
        f"/api/engineering/revisions/{revision['id']}/assemblies", json={"name": "Сборка"}
    )).json()
    return assembly["id"]


@pytest.mark.asyncio
async def test_assembly_solve_preview_requires_grounded_component(client: AsyncClient):
    assembly_id = await _new_assembly(client, "Ф4.2Б без заземления")
    await client.post(
        f"/api/engineering/assemblies/{assembly_id}/components",
        json={"instance_key": "a", "designation": "Деталь A"},
    )
    resp = await client.post(f"/api/engineering/assemblies/{assembly_id}/solve")
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_assembly_solve_preview_requires_active_components(client: AsyncClient):
    assembly_id = await _new_assembly(client, "Ф4.2Б пусто")
    resp = await client.post(f"/api/engineering/assemblies/{assembly_id}/solve")
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_assembly_solve_preview_skips_unsupported_and_missing_frame_mates(
    client: AsyncClient, monkeypatch
):
    from app.api import engineering as engineering_api

    captured: dict = {}

    async def fake_solve(payload):
        captured["payload"] = payload
        return {"solved": True, "status_code": 0, "reason": "solved", "placements": {}}

    monkeypatch.setattr(engineering_api, "_call_kernel_assembly_solve", fake_solve)

    assembly_id = await _new_assembly(client, "Ф4.2Б пропуски")
    await client.post(
        f"/api/engineering/assemblies/{assembly_id}/components",
        json={"instance_key": "base", "designation": "База", "metadata": {"grounded": True}},
    )
    await client.post(
        f"/api/engineering/assemblies/{assembly_id}/components",
        json={"instance_key": "arm", "designation": "Рычаг"},
    )
    # tangent has no kernel joint equivalent -> unsupported_mate_type
    await client.post(
        f"/api/engineering/assemblies/{assembly_id}/mates",
        json={"mate_type": "tangent", "first_instance_key": "base", "second_instance_key": "arm"},
    )
    # concentric is supported, but no first_frame/second_frame -> missing_frames
    await client.post(
        f"/api/engineering/assemblies/{assembly_id}/mates",
        json={
            "mate_type": "concentric",
            "first_instance_key": "base", "second_instance_key": "arm",
        },
    )

    resp = await client.post(f"/api/engineering/assemblies/{assembly_id}/solve")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["skipped_mates"]) == 2
    reasons = sorted(item["reason"] for item in body["skipped_mates"])
    assert reasons == ["missing_frames", "unsupported_mate_type"]
    assert captured["payload"]["joints"] == []  # neither mate reached the kernel
    assert body["grounded_instances"] == ["base"]


@pytest.mark.asyncio
async def test_assembly_solve_preview_maps_mate_type_and_calls_kernel(
    client: AsyncClient, monkeypatch
):
    from app.api import engineering as engineering_api

    captured: dict = {}

    async def fake_solve(payload):
        captured["payload"] = payload
        return {
            "solved": True, "status_code": 0, "reason": "solved",
            "placements": {
                "base": {"position_mm": [0, 0, 0], "axis": [0, 0, 1], "angle_deg": 0},
                "arm": {"position_mm": [5, 5, 10], "axis": [1, 0, 0], "angle_deg": 0},
            },
        }

    monkeypatch.setattr(engineering_api, "_call_kernel_assembly_solve", fake_solve)

    assembly_id = await _new_assembly(client, "Ф4.2Б happy path")
    await client.post(
        f"/api/engineering/assemblies/{assembly_id}/components",
        json={
            "instance_key": "base", "designation": "База",
            "metadata": {"grounded": True},
        },
    )
    await client.post(
        f"/api/engineering/assemblies/{assembly_id}/components",
        json={
            "instance_key": "arm", "designation": "Рычаг",
            "transform": {"translate": [20, 0, 0]},
        },
    )
    await client.post(
        f"/api/engineering/assemblies/{assembly_id}/mates",
        json={
            "mate_type": "concentric",
            "first_instance_key": "base", "second_instance_key": "arm",
            "parameters": {
                "first_frame": {"position_mm": [5, 5, 10], "axis": [1, 0, 0]},
                "second_frame": {"position_mm": [0, 0, 0], "axis": [1, 0, 0]},
            },
        },
    )

    resp = await client.post(f"/api/engineering/assemblies/{assembly_id}/solve")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["solved"] is True
    assert body["skipped_mates"] == []
    assert body["placements"]["arm"]["position_mm"] == [5, 5, 10]

    [joint] = captured["payload"]["joints"]
    assert joint["type"] == "cylindrical"  # concentric -> cylindrical, not a name match
    assert joint["first"]["key"] == "base"
    assert joint["second"]["key"] == "arm"

    [arm_component] = [
        c for c in captured["payload"]["components"] if c["key"] == "arm"
    ]
    assert arm_component["position_mm"] == [20.0, 0.0, 0.0]  # from transform.translate
    assert arm_component["grounded"] is False
    [base_component] = [
        c for c in captured["payload"]["components"] if c["key"] == "base"
    ]
    assert base_component["grounded"] is True


@pytest.mark.asyncio
async def test_analysis_runs_are_immutable_snapshots(client: AsyncClient):
    """F2: each run freezes inputs + material card + solver version; editing
    the live material later does not rewrite past runs; bad input is recorded
    as an invalid_input run before the 422."""
    material = (await client.post("/api/engineering/materials", json={
        "designation": "Сталь F2", "yield_strength_mpa": 100,
    })).json()
    project = (await client.post("/api/engineering/projects", json={"name": "Снапшоты"})).json()
    revision = (await client.post(f"/api/engineering/projects/{project['id']}/revisions", json={"base_revision": None})).json()
    case = (await client.post(f"/api/engineering/revisions/{revision['id']}/analysis-cases", json={
        "name": "Осевое", "material_id": material["id"],
        "inputs": {"force_n": 500, "area_mm2": 10},
    })).json()

    first = await client.post(f"/api/engineering/analysis-cases/{case['id']}/run")
    assert first.status_code == 200
    assert first.json()["status"] == "passed"  # 50 MPa < 100

    runs = (await client.get(f"/api/engineering/analysis-cases/{case['id']}/runs")).json()
    assert len(runs) == 1
    run = runs[0]
    assert run["run_number"] == 1
    assert run["inputs_snapshot"] == {"force_n": 500, "area_mm2": 10}
    assert run["material_snapshot"]["yield_strength_mpa"] == 100
    assert run["solver_name"] == "axial_stress"
    assert run["solver_version"]

    # a second run gets its own number; history is append-only
    second = await client.post(f"/api/engineering/analysis-cases/{case['id']}/run")
    assert second.status_code == 200
    runs = (await client.get(f"/api/engineering/analysis-cases/{case['id']}/runs")).json()
    assert [r["run_number"] for r in runs] == [2, 1]

    # invalid input is a recorded run, then 422
    bad_case = (await client.post(f"/api/engineering/revisions/{revision['id']}/analysis-cases", json={
        "name": "Без площади", "analysis_type": "axial_stress",
        "inputs": {"force_n": 500},
    })).json()
    bad = await client.post(f"/api/engineering/analysis-cases/{bad_case['id']}/run")
    assert bad.status_code == 422
    bad_runs = (await client.get(f"/api/engineering/analysis-cases/{bad_case['id']}/runs")).json()
    assert len(bad_runs) == 1
    assert bad_runs[0]["status"] == "invalid_input"
    assert bad_runs[0]["error"]
