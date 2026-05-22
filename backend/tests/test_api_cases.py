"""Cases (Cockpit) API tests."""

import uuid

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_create_case(client: AsyncClient):
    resp = await client.post(
        "/api/cases",
        json={"title": "Тест кейс", "customer": "Иванов", "task_description": "Проверка"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["title"] == "Тест кейс"
    assert data["customer"] == "Иванов"
    assert data["status"] == "open"
    assert "id" in data


@pytest.mark.asyncio
async def test_list_cases(client: AsyncClient):
    await client.post("/api/cases", json={"title": "Кейс для листинга"})
    resp = await client.get("/api/cases")
    assert resp.status_code == 200
    data = resp.json()
    assert "items" in data
    assert data["total"] >= 1


@pytest.mark.asyncio
async def test_list_cases_filter_by_status(client: AsyncClient):
    await client.post("/api/cases", json={"title": "Открытый кейс"})
    resp = await client.get("/api/cases?status=open")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert all(i["status"] == "open" for i in items)


@pytest.mark.asyncio
async def test_get_case_detail(client: AsyncClient):
    created = (await client.post("/api/cases", json={"title": "Детальный кейс"})).json()
    case_id = created["id"]

    resp = await client.get(f"/api/cases/{case_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == case_id
    assert "documents" in data
    assert "timeline" in data
    assert "approval_gates" in data


@pytest.mark.asyncio
async def test_get_case_not_found(client: AsyncClient):
    resp = await client.get(f"/api/cases/{uuid.uuid4()}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_update_case(client: AsyncClient):
    created = (await client.post("/api/cases", json={"title": "До обновления"})).json()
    case_id = created["id"]

    resp = await client.patch(f"/api/cases/{case_id}", json={"status": "closed", "title": "После обновления"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "closed"
    assert data["title"] == "После обновления"


@pytest.mark.asyncio
async def test_case_creation_recorded_in_timeline(client: AsyncClient):
    created = (await client.post("/api/cases", json={"title": "Аудит кейс"})).json()
    case_id = created["id"]

    resp = await client.get(f"/api/cases/{case_id}/audit")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1
    event_types = [e["event_type"] for e in data["items"]]
    assert "case_created" in event_types


@pytest.mark.asyncio
async def test_add_document_to_case(client: AsyncClient):
    created = (await client.post("/api/cases", json={"title": "Кейс с документом"})).json()
    case_id = created["id"]

    # Ingest a document first
    import io
    doc_resp = await client.post(
        "/api/documents/ingest",
        files={"file": ("test.txt", io.BytesIO(b"invoice content"), "text/plain")},
    )
    assert doc_resp.status_code in (200, 201)
    doc_id = doc_resp.json().get("id") or doc_resp.json().get("document_id")

    # Add to case
    resp = await client.post(
        f"/api/cases/{case_id}/documents",
        json={"document_id": doc_id},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["ok"] is True
    assert data["document_id"] == doc_id


@pytest.mark.asyncio
async def test_add_duplicate_document_to_case(client: AsyncClient):
    created = (await client.post("/api/cases", json={"title": "Кейс дублей"})).json()
    case_id = created["id"]

    import io
    doc_resp = await client.post(
        "/api/documents/ingest",
        files={"file": ("dup.txt", io.BytesIO(b"dup"), "text/plain")},
    )
    doc_id = doc_resp.json().get("id") or doc_resp.json().get("document_id")

    await client.post(f"/api/cases/{case_id}/documents", json={"document_id": doc_id})
    # Second attempt must 409
    resp = await client.post(f"/api/cases/{case_id}/documents", json={"document_id": doc_id})
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_list_case_documents(client: AsyncClient):
    created = (await client.post("/api/cases", json={"title": "Список документов"})).json()
    case_id = created["id"]

    import io
    doc_resp = await client.post(
        "/api/documents/ingest",
        files={"file": ("listed.txt", io.BytesIO(b"content"), "text/plain")},
    )
    doc_id = doc_resp.json().get("id") or doc_resp.json().get("document_id")
    await client.post(f"/api/cases/{case_id}/documents", json={"document_id": doc_id})

    resp = await client.get(f"/api/cases/{case_id}/documents")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["items"][0]["id"] == doc_id


@pytest.mark.asyncio
async def test_approval_decide_approve(client: AsyncClient):
    from app.db.models import Approval, ApprovalActionType, ApprovalStatus
    from sqlalchemy.ext.asyncio import AsyncSession

    created = (await client.post("/api/cases", json={"title": "Кейс с апрувом"})).json()
    case_id = created["id"]

    # Create an approval gate directly via /api/approvals
    approval_resp = await client.post(
        "/api/approvals",
        json={
            "action_type": "email.send",
            "entity_type": "case",
            "entity_id": case_id,
            "requested_by": "agent",
        },
    )
    assert approval_resp.status_code == 201
    approval_id = approval_resp.json()["id"]

    resp = await client.post(
        f"/api/cases/{case_id}/approvals/{approval_id}/decide",
        json={"approved": True, "decided_by": "tester"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["status"] == "approved"


@pytest.mark.asyncio
async def test_approval_decide_already_decided(client: AsyncClient):
    created = (await client.post("/api/cases", json={"title": "Уже решён"})).json()
    case_id = created["id"]

    approval_resp = await client.post(
        "/api/approvals",
        json={
            "action_type": "invoice.approve",
            "entity_type": "case",
            "entity_id": case_id,
        },
    )
    approval_id = approval_resp.json()["id"]

    await client.post(
        f"/api/cases/{case_id}/approvals/{approval_id}/decide",
        json={"approved": True},
    )
    # Second decide must 422
    resp = await client.post(
        f"/api/cases/{case_id}/approvals/{approval_id}/decide",
        json={"approved": False},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_scenario_run_creates_approval_gate(client: AsyncClient):
    created = (await client.post("/api/cases", json={"title": "Кейс сценарий"})).json()
    case_id = created["id"]

    resp = await client.post(
        "/api/scenarios/draft_email/run",
        json={
            "case_id": case_id,
            "draft_id": "test-draft",
            "requested_tools": ["email.send.request_approval"],
        },
    )
    # Scenario might not exist in test DB — 404 is fine if name unknown
    if resp.status_code == 404:
        pytest.skip("draft_email scenario not configured in test env")
    assert resp.status_code == 200
    data = resp.json()
    assert "approval_gates" in data
    assert len(data["approval_gates"]) == 1
    assert data["approval_gates"][0]["action_type"] == "email.send.request_approval"
    assert data["approval_gates"][0]["status"] == "pending"
