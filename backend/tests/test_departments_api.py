"""Tests for Admin Departments API and user org-field assignment."""

import pytest
from httpx import AsyncClient

from app.db.models import User


@pytest.mark.asyncio
async def test_department_crud_flow(client: AsyncClient):
    # create
    resp = await client.post("/api/admin/departments", json={"name": "Procurement", "code": "proc"})
    assert resp.status_code == 201, resp.text
    dept = resp.json()
    assert dept["code"] == "proc"

    # child with parent
    resp = await client.post(
        "/api/admin/departments",
        json={"name": "Proc Sub", "code": "proc-sub", "parent_id": dept["id"]},
    )
    assert resp.status_code == 201

    # duplicate code rejected
    resp = await client.post("/api/admin/departments", json={"name": "X", "code": "proc"})
    assert resp.status_code == 409

    # list
    resp = await client.get("/api/admin/departments")
    assert resp.status_code == 200
    assert resp.json()["total"] == 2

    # rename
    resp = await client.patch(f"/api/admin/departments/{dept['id']}", json={"name": "Закупки"})
    assert resp.status_code == 200
    assert resp.json()["name"] == "Закупки"

    # cannot delete while it has a child
    resp = await client.delete(f"/api/admin/departments/{dept['id']}")
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_assign_user_org_fields(client: AsyncClient, db_session):
    user = User(sub="local:emp1", email="e@x", name="Emp", preferred_username="emp", role="buyer")
    db_session.add(user)
    await db_session.commit()

    dept = (await client.post("/api/admin/departments", json={"name": "Eng", "code": "eng"})).json()

    resp = await client.patch(
        "/api/admin/users/local:emp1",
        json={"department_id": dept["id"], "title": "Инженер", "manager_sub": "local:boss"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["department_id"] == dept["id"]
    assert data["title"] == "Инженер"
    assert data["manager_sub"] == "local:boss"

    # clear department with explicit null
    resp = await client.patch("/api/admin/users/local:emp1", json={"department_id": None})
    assert resp.status_code == 200
    assert resp.json()["department_id"] is None


@pytest.mark.asyncio
async def test_assign_unknown_department_404(client: AsyncClient, db_session):
    user = User(sub="local:emp2", email="e2@x", name="Emp2", preferred_username="emp2", role="buyer")
    db_session.add(user)
    await db_session.commit()
    import uuid

    resp = await client.patch(
        "/api/admin/users/local:emp2", json={"department_id": str(uuid.uuid4())}
    )
    assert resp.status_code == 404
