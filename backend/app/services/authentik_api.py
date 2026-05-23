"""Authentik REST API client — user provisioning and password management."""

from __future__ import annotations

import structlog

logger = structlog.get_logger()


def _headers() -> dict[str, str]:
    from app.config import settings
    return {
        "Authorization": f"Bearer {settings.authentik_api_token}",
        "Content-Type": "application/json",
    }


def _base() -> str:
    from app.config import settings
    return f"{settings.authentik_url}/api/v3"


async def find_user_by_email(email: str) -> int | None:
    """Return Authentik PK for the user with this email, or None."""
    import httpx
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(
            f"{_base()}/core/users/",
            params={"search": email},
            headers=_headers(),
        )
        r.raise_for_status()
        results = r.json().get("results", [])
        for u in results:
            if u.get("email", "").lower() == email.lower():
                return u["pk"]
    return None


async def create_user(email: str, username: str, name: str) -> int:
    """Create a user in Authentik and return its PK."""
    import httpx
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.post(
            f"{_base()}/core/users/",
            json={
                "username": username,
                "name": name,
                "email": email,
                "is_active": True,
                "type": "internal",
                "groups": [],
            },
            headers=_headers(),
        )
        if r.status_code == 400:
            data = r.json()
            raise ValueError(f"Authentik user creation failed: {data}")
        r.raise_for_status()
        pk: int = r.json()["pk"]
        logger.info("authentik_user_created", email=email, pk=pk)
        return pk


async def set_password(authentik_pk: int, password: str) -> None:
    """Set password for an Authentik user."""
    import httpx
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.post(
            f"{_base()}/core/users/{authentik_pk}/set_password/",
            json={"password": password},
            headers=_headers(),
        )
        r.raise_for_status()
        logger.info("authentik_password_set", pk=authentik_pk)


async def provision_user(email: str, username: str, name: str, password: str | None = None) -> int:
    """Ensure user exists in Authentik; create if absent. Optionally set password. Returns PK."""
    pk = await find_user_by_email(email)
    if pk is None:
        pk = await create_user(email, username, name)
    if password:
        await set_password(pk, password)
    return pk
