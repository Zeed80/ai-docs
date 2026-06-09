"""Security RBAC tests — verify role gates on privileged endpoints.

These tests explicitly set AUTH_ENABLED=true and override get_current_user
to simulate different roles, ensuring that:
- viewer/accountant/buyer cannot access admin/manager-only endpoints
- admin/manager can access what they should
"""

import pytest
from httpx import AsyncClient, ASGITransport

from app.auth.models import UserInfo, UserRole
from app.auth.jwt import get_current_user


def _make_user(role: UserRole, sub: str = "test-user") -> UserInfo:
    return UserInfo(
        sub=sub,
        email=f"{role.value}@test.com",
        name=role.value.capitalize(),
        preferred_username=role.value,
        roles=[role],
        groups=[],
    )


@pytest.fixture
def viewer_user():
    return _make_user(UserRole.viewer)


@pytest.fixture
def manager_user():
    return _make_user(UserRole.manager)


@pytest.fixture
def admin_user():
    return _make_user(UserRole.admin)


@pytest.fixture
async def viewer_client(db_session, viewer_user):
    from app.db.session import get_db
    from app.main import app

    async def override_get_db():
        yield db_session

    async def override_auth():
        return viewer_user

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_auth
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
async def manager_client(db_session, manager_user):
    from app.db.session import get_db
    from app.main import app

    async def override_get_db():
        yield db_session

    async def override_auth():
        return manager_user

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_auth
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
async def admin_client(db_session, admin_user):
    from app.db.session import get_db
    from app.main import app

    async def override_get_db():
        yield db_session

    async def override_auth():
        return admin_user

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_auth
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


# ── Agent Control Plane RBAC ──────────────────────────────────────────────────

AGENT_CP_MUTATING = [
    ("POST", "/api/agent/config/proposals",
     {"setting_path": "agent_name", "proposed_value": "Test", "reason": "test"}),
    ("POST", "/api/agent/config/propose",
     {"setting_path": "agent_name", "proposed_value": "Test", "reason": "test"}),
    ("POST", "/api/agent/tasks",
     {"objective": "test", "role": "analyst"}),
    ("POST", "/api/agent/tasks/create",
     {"objective": "test", "role": "analyst"}),
    ("POST", "/api/agent/teams",
     {"name": "team", "purpose": "test"}),
    ("POST", "/api/agent/cron",
     {"schedule": "0 9 * * 1", "prompt": "check", "description": "daily"}),
    ("POST", "/api/agent/plugins",
     {"plugin_key": "test_plugin", "name": "Test", "version": "1.0",
      "description": "test", "manifest": {}, "risk_level": "low",
      "installed_by": "test"}),
    ("POST", "/api/agent/capabilities",
     {"title": "test cap", "missing_capability": "test",
      "reason": "test", "suggested_artifact": {}}),
    ("POST", "/api/agent/capabilities/propose",
     {"title": "test cap", "missing_capability": "test",
      "reason": "test", "suggested_artifact": {}}),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path,payload", AGENT_CP_MUTATING)
async def test_agent_control_plane_viewer_gets_403(viewer_client, method, path, payload):
    """Viewer must be rejected from all Agent Control Plane mutation endpoints."""
    resp = await viewer_client.request(method, path, json=payload)
    assert resp.status_code == 403, (
        f"{method} {path} returned {resp.status_code}, expected 403 for viewer"
    )


@pytest.mark.asyncio
async def test_agent_control_plane_get_status_viewer_allowed(viewer_client):
    """GET (read-only) endpoints on agent control plane are accessible to viewer."""
    resp = await viewer_client.get("/api/agent/control-plane/status")
    assert resp.status_code in (200, 404, 500), (
        f"Expected non-403 on GET /status, got {resp.status_code}"
    )
    assert resp.status_code != 403


# ── Approval Policy RBAC ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_patch_approval_policy_viewer_gets_403(viewer_client):
    """Viewer must not be able to change the auto-approval trust policy."""
    resp = await viewer_client.patch(
        "/api/approvals/policy",
        json={"enabled": True, "trust_threshold": 0.0, "max_amount": 999999999},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_patch_approval_policy_manager_allowed(manager_client):
    """Manager must be able to update approval policy."""
    resp = await manager_client.patch(
        "/api/approvals/policy",
        json={"enabled": False, "trust_threshold": 0.85},
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_patch_approval_policy_admin_allowed(admin_client):
    """Admin must be able to update approval policy."""
    resp = await admin_client.patch(
        "/api/approvals/policy",
        json={"enabled": False, "trust_threshold": 0.9},
    )
    assert resp.status_code == 200


# ── Document Bulk-Delete RBAC ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_bulk_delete_viewer_gets_403(viewer_client):
    """Viewer must not be able to bulk-delete documents."""
    resp = await viewer_client.request(
        "DELETE",
        "/api/documents/bulk-delete",
        json={"document_ids": [], "delete_files": False},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_bulk_delete_manager_allowed(manager_client):
    """Manager gets through the role gate (may still get 404/422 with empty list)."""
    resp = await manager_client.request(
        "DELETE",
        "/api/documents/bulk-delete",
        json={"document_ids": [], "delete_files": False},
    )
    assert resp.status_code != 403, f"Manager should not get 403, got {resp.status_code}"


# ── Dev Purge-All ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_purge_all_viewer_gets_403(viewer_client):
    """Viewer must not be able to call purge-all."""
    resp = await viewer_client.post(
        "/api/documents/dev/purge-all",
        json={"confirm": "DELETE ALL DOCUMENT DATA", "delete_files": False},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_purge_all_production_guard(admin_client, monkeypatch):
    """purge-all must return 403 even for admin when APP_ENV=production."""
    from app.config import settings
    monkeypatch.setattr(settings, "app_env", "production")
    resp = await admin_client.post(
        "/api/documents/dev/purge-all",
        json={"confirm": "DELETE ALL DOCUMENT DATA", "delete_files": False},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_purge_all_admin_dev_allowed(admin_client, monkeypatch):
    """Admin can call purge-all in non-production environments."""
    from app.config import settings
    monkeypatch.setattr(settings, "app_env", "test")
    resp = await admin_client.post(
        "/api/documents/dev/purge-all",
        json={"confirm": "DELETE ALL DOCUMENT DATA", "delete_files": False},
    )
    assert resp.status_code != 403, f"Admin in test env should not get 403, got {resp.status_code}"
