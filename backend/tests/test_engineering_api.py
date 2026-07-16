"""Engineering-project API: immutable revisions and traceable projections."""

import uuid

import pytest
from httpx import AsyncClient
from app.db.models import CadIrRevision, Drawing, DrawingStatus, ImageGeneration


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
