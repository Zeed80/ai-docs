"""Tests for Admin API — user management, audit logs, API keys, system status."""

import hashlib
import uuid

import pytest
from httpx import AsyncClient

from app.db.models import ApiKey, AuditLog, User


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
async def admin_user(db_session):
    user = User(
        sub="local:test-admin-001",
        email="admin@example.com",
        name="Test Admin",
        preferred_username="testadmin",
        role="admin",
        is_active=True,
    )
    db_session.add(user)
    await db_session.commit()
    return user


@pytest.fixture
async def viewer_user(db_session):
    user = User(
        sub="local:test-viewer-001",
        email="viewer@example.com",
        name="Test Viewer",
        preferred_username="testviewer",
        role="viewer",
        is_active=True,
    )
    db_session.add(user)
    await db_session.commit()
    return user


@pytest.fixture
async def api_key(db_session):
    raw = "test-raw-key-12345"
    key = ApiKey(
        key_hash=hashlib.sha256(raw.encode()).hexdigest(),
        name="Test Integration Key",
        user_sub="dev-user",
        scopes=["invoices.read", "documents.read"],
        is_active=True,
    )
    db_session.add(key)
    await db_session.commit()
    return key


@pytest.fixture
async def audit_entry(db_session):
    log = AuditLog(
        user_id="dev-user",
        action="invoice.approve",
        entity_type="invoice",
        details={"invoice_id": str(uuid.uuid4())},
    )
    db_session.add(log)
    await db_session.commit()
    return log


# ── Users: list ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_users_empty(client: AsyncClient):
    resp = await client.get("/api/admin/users")
    assert resp.status_code == 200
    data = resp.json()
    assert "items" in data
    assert "total" in data
    assert isinstance(data["items"], list)


@pytest.mark.asyncio
async def test_list_users(client: AsyncClient, admin_user, viewer_user):
    resp = await client.get("/api/admin/users")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 2
    emails = [u["email"] for u in data["items"]]
    assert "admin@example.com" in emails
    assert "viewer@example.com" in emails


@pytest.mark.asyncio
async def test_list_users_filter_by_role(client: AsyncClient, admin_user, viewer_user):
    resp = await client.get("/api/admin/users", params={"role": "viewer"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1
    for u in data["items"]:
        assert u["role"] == "viewer"


@pytest.mark.asyncio
async def test_list_users_filter_by_active(client: AsyncClient, admin_user):
    resp = await client.get("/api/admin/users", params={"is_active": True})
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1


@pytest.mark.asyncio
async def test_list_users_search(client: AsyncClient, admin_user):
    resp = await client.get("/api/admin/users", params={"q": "Test Admin"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1
    assert any("Test Admin" in u["name"] for u in data["items"])


# ── Users: create ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_user(client: AsyncClient):
    resp = await client.post("/api/admin/users", json={
        "name": "New Employee",
        "email": "new.employee@factory.ru",
        "role": "accountant",
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["email"] == "new.employee@factory.ru"
    assert data["name"] == "New Employee"
    assert data["role"] == "accountant"
    assert data["is_active"] is True
    assert "sub" in data


@pytest.mark.asyncio
async def test_create_user_invalid_role(client: AsyncClient):
    resp = await client.post("/api/admin/users", json={
        "name": "Bad Role",
        "email": "badrole@factory.ru",
        "role": "superuser",
    })
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_user_duplicate_email(client: AsyncClient, admin_user):
    resp = await client.post("/api/admin/users", json={
        "name": "Duplicate",
        "email": "admin@example.com",
        "role": "viewer",
    })
    assert resp.status_code == 409


# ── Users: get ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_user(client: AsyncClient, viewer_user):
    resp = await client.get(f"/api/admin/users/{viewer_user.sub}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["sub"] == viewer_user.sub
    assert data["email"] == "viewer@example.com"


@pytest.mark.asyncio
async def test_get_user_not_found(client: AsyncClient):
    resp = await client.get("/api/admin/users/local:nonexistent-000")
    assert resp.status_code == 404


# ── Users: update ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_update_user_role(client: AsyncClient, viewer_user):
    resp = await client.patch(f"/api/admin/users/{viewer_user.sub}", json={
        "role": "accountant",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["role"] == "accountant"


@pytest.mark.asyncio
async def test_update_user_preferences(client: AsyncClient, viewer_user):
    resp = await client.patch(f"/api/admin/users/{viewer_user.sub}", json={
        "preferences": {"theme": "dark", "locale": "ru"},
    })
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_update_user_not_found(client: AsyncClient):
    resp = await client.patch("/api/admin/users/local:ghost-000", json={"role": "viewer"})
    assert resp.status_code == 404


# ── Users: deactivate ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_deactivate_user(client: AsyncClient, viewer_user):
    resp = await client.post(f"/api/admin/users/{viewer_user.sub}/deactivate")
    assert resp.status_code == 200
    data = resp.json()
    assert data["is_active"] is False


@pytest.mark.asyncio
async def test_deactivate_self_forbidden(client: AsyncClient, db_session):
    dev = User(
        sub="dev-user",
        email="dev@localhost",
        name="Dev User",
        preferred_username="dev",
        role="admin",
        is_active=True,
    )
    db_session.add(dev)
    await db_session.commit()

    resp = await client.post("/api/admin/users/dev-user/deactivate")
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_deactivate_user_not_found(client: AsyncClient):
    resp = await client.post("/api/admin/users/local:ghost-000/deactivate")
    assert resp.status_code == 404


# ── Permissions ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_permission_matrix(client: AsyncClient):
    resp = await client.get("/api/admin/permissions")
    assert resp.status_code == 200
    data = resp.json()
    assert "matrix" in data
    assert isinstance(data["matrix"], dict)
    assert "admin" in data["matrix"]
    assert "viewer" in data["matrix"]
    assert isinstance(data["matrix"]["viewer"], list)


# ── Audit logs ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_audit_logs_empty(client: AsyncClient):
    resp = await client.get("/api/admin/audit-logs")
    assert resp.status_code == 200
    data = resp.json()
    assert "items" in data
    assert "total" in data


@pytest.mark.asyncio
async def test_list_audit_logs(client: AsyncClient, audit_entry):
    resp = await client.get("/api/admin/audit-logs")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1
    actions = [e["action"] for e in data["items"]]
    assert "invoice.approve" in actions


@pytest.mark.asyncio
async def test_list_audit_logs_filter_by_user(client: AsyncClient, audit_entry):
    resp = await client.get("/api/admin/audit-logs", params={"user_id": "dev-user"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1
    for entry in data["items"]:
        assert entry["user_id"] == "dev-user"


@pytest.mark.asyncio
async def test_list_audit_logs_filter_by_action(client: AsyncClient, audit_entry):
    resp = await client.get("/api/admin/audit-logs", params={"action": "invoice.approve"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1


@pytest.mark.asyncio
async def test_list_audit_logs_filter_by_entity_type(client: AsyncClient, audit_entry):
    resp = await client.get("/api/admin/audit-logs", params={"entity_type": "invoice"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1


# ── API keys ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_api_keys_empty(client: AsyncClient):
    resp = await client.get("/api/admin/api-keys")
    assert resp.status_code == 200
    data = resp.json()
    assert "items" in data
    assert "total" in data


@pytest.mark.asyncio
async def test_list_api_keys(client: AsyncClient, api_key):
    resp = await client.get("/api/admin/api-keys")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1
    names = [k["name"] for k in data["items"]]
    assert "Test Integration Key" in names


@pytest.mark.asyncio
async def test_create_api_key(client: AsyncClient):
    resp = await client.post("/api/admin/api-keys", json={
        "name": "CI/CD Pipeline Key",
        "scopes": ["documents.read", "invoices.read"],
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "CI/CD Pipeline Key"
    assert "raw_key" in data
    assert len(data["raw_key"]) > 20
    assert "id" in data


@pytest.mark.asyncio
async def test_revoke_api_key(client: AsyncClient, api_key):
    resp = await client.delete(f"/api/admin/api-keys/{api_key.id}")
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_revoke_api_key_not_found(client: AsyncClient):
    resp = await client.delete(f"/api/admin/api-keys/{uuid.uuid4()}")
    assert resp.status_code == 404


# ── System status ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_system_status(client: AsyncClient):
    resp = await client.get("/api/admin/system-status")
    assert resp.status_code == 200
    data = resp.json()
    assert "db" in data
    assert "redis" in data
    assert "celery" in data
    assert "ai_providers" in data
    assert "active_users_count" in data
    assert "pending_approvals_count" in data
    assert data["db"] == "ok"
    assert isinstance(data["active_users_count"], int)
    assert isinstance(data["pending_approvals_count"], int)
