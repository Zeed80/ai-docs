"""Tests for Этап 6 collaboration: approval auto-routing and case members."""

import pytest
from httpx import AsyncClient

from app.db.models import Department, User


@pytest.mark.asyncio
async def test_approval_auto_routes_to_department_manager(client: AsyncClient, db_session):
    dept = Department(name="Procurement", code="proc")
    db_session.add(dept)
    await db_session.flush()
    db_session.add_all([
        User(sub="u:boss", email="boss@x", name="Boss", role="manager", department_id=dept.id),
        User(sub="u:req", email="req@x", name="Req", role="buyer", department_id=dept.id),
    ])
    await db_session.commit()

    resp = await client.post(
        "/api/approvals",
        json={
            "action_type": "invoice.approve",
            "entity_type": "invoice",
            "entity_id": "00000000-0000-0000-0000-000000000099",
            "requested_by": "u:req",
        },
    )
    assert resp.status_code == 201, resp.text
    # No explicit assignee → routed to the department manager.
    assert resp.json()["assigned_to"] == "u:boss"


@pytest.mark.asyncio
async def test_approval_keeps_explicit_assignee(client: AsyncClient, db_session):
    resp = await client.post(
        "/api/approvals",
        json={
            "action_type": "email.send",
            "entity_type": "email",
            "entity_id": "00000000-0000-0000-0000-000000000098",
            "requested_by": "u:req",
            "assigned_to": "u:someone",
        },
    )
    assert resp.status_code == 201
    assert resp.json()["assigned_to"] == "u:someone"


@pytest.mark.asyncio
async def test_case_member_add_list_remove(client: AsyncClient):
    case = (await client.post("/api/cases", json={"title": "Совместный кейс"})).json()
    case_id = case["id"]

    # creator is auto-added as owner
    members = (await client.get(f"/api/cases/{case_id}/members")).json()
    assert any(m["role"] == "owner" for m in members)

    resp = await client.post(
        f"/api/cases/{case_id}/members", json={"user_sub": "u:peer", "role": "collaborator"}
    )
    assert resp.status_code == 201
    assert resp.json()["user_sub"] == "u:peer"

    members = (await client.get(f"/api/cases/{case_id}/members")).json()
    assert {m["user_sub"] for m in members} >= {"u:peer"}

    resp = await client.delete(f"/api/cases/{case_id}/members/u:peer")
    assert resp.status_code == 204
