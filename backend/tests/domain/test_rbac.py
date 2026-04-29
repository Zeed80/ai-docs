from __future__ import annotations

from backend.app.auth import has_permission, permissions_for_roles


def test_rbac_role_permissions() -> None:
    assert has_permission(["admin"], "agent:run") is True
    assert has_permission(["technologist"], "agent:run") is True
    assert has_permission(["accountant"], "agent:run") is False
    assert has_permission(["accountant"], "invoice:export") is True


def test_rbac_permissions_for_roles() -> None:
    permissions = permissions_for_roles(["technologist", "accountant"])

    assert "drawing:analyze" in permissions
    assert "invoice:export" in permissions
    assert "*" not in permissions
