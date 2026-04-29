from __future__ import annotations


def test_auth_me_uses_local_dev_bypass(client, monkeypatch) -> None:
    monkeypatch.setenv("AUTH_LOCAL_BYPASS", "true")

    response = client.get("/api/auth/me")

    assert response.status_code == 200
    payload = response.json()
    assert payload["subject"] == "local-dev"
    assert payload["auth_mode"] == "local_bypass"
    assert "admin" in payload["roles"]


def test_auth_me_requires_bearer_when_local_bypass_is_disabled(client, monkeypatch) -> None:
    monkeypatch.setenv("AUTH_LOCAL_BYPASS", "false")

    response = client.get("/api/auth/me")

    assert response.status_code == 401
    assert response.json()["detail"] == "Bearer token is required"


def test_auth_permissions_returns_local_admin_permissions(client, monkeypatch) -> None:
    monkeypatch.setenv("AUTH_LOCAL_BYPASS", "true")

    response = client.get("/api/auth/permissions")

    assert response.status_code == 200
    assert response.json() == {"roles": ["admin"], "permissions": ["*"]}
