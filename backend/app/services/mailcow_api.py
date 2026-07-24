"""Mailcow REST API client — personal mailbox provisioning.

Mirrors the style of app.services.authentik_api: thin async functions, config
resolved lazily via app.services.integration_config.get_mail_server_config()
(DB-backed, api_key encrypted with app.ai.secret_box). See docs.mailcow.email
REST API (X-API-Key header, /api/v1/{add,edit,delete,get}/{object}).
"""

from __future__ import annotations

import structlog

logger = structlog.get_logger()


class MailServerNotConfigured(RuntimeError):
    """Raised when an admin has not yet set the Mailcow connection config."""


async def _client_and_base():
    import httpx
    from app.services.integration_config import get_mail_server_config

    cfg = await get_mail_server_config()
    if not cfg.configured:
        raise MailServerNotConfigured("Mail server API is not configured (see /api/admin/mail-server)")
    headers = {"X-API-Key": cfg.api_key, "Content-Type": "application/json"}
    return httpx.AsyncClient(timeout=15.0, headers=headers), cfg.api_url.rstrip("/"), cfg


async def get_mailbox(full_address: str) -> dict | None:
    """Return the Mailcow mailbox object for full_address, or None if absent."""
    client, base, _cfg = await _client_and_base()
    async with client:
        r = await client.get(f"{base}/api/v1/get/mailbox/{full_address}")
        r.raise_for_status()
        data = r.json()
        if not data or (isinstance(data, dict) and data.get("type") == "error"):
            return None
        return data


async def check_local_part_available(local_part: str, domain: str) -> bool:
    existing = await get_mailbox(f"{local_part}@{domain}")
    return existing is None


async def create_mailbox(
    *, local_part: str, domain: str, password: str, full_name: str, quota_mb: int = 1024
) -> dict:
    """Create a mailbox via POST /api/v1/add/mailbox. Raises on API-reported errors."""
    client, base, _cfg = await _client_and_base()
    async with client:
        r = await client.post(
            f"{base}/api/v1/add/mailbox",
            json={
                "local_part": local_part,
                "domain": domain,
                "name": full_name,
                "password": password,
                "password2": password,
                "quota": str(quota_mb),
                "active": "1",
                "force_pw_update": "0",
            },
        )
        r.raise_for_status()
        data = r.json()
        results = data if isinstance(data, list) else [data]
        for item in results:
            if isinstance(item, dict) and item.get("type") == "error":
                raise ValueError(f"Mailcow API error creating {local_part}@{domain}: {item.get('msg')}")
        logger.info("mailcow_mailbox_created", address=f"{local_part}@{domain}")
        return data


async def edit_mailbox_password(full_address: str, new_password: str) -> None:
    client, base, _cfg = await _client_and_base()
    async with client:
        r = await client.post(
            f"{base}/api/v1/edit/mailbox",
            json={"items": [full_address], "attr": {"password": new_password, "password2": new_password}},
        )
        r.raise_for_status()
        logger.info("mailcow_mailbox_password_reset", address=full_address)


async def delete_mailbox(full_address: str) -> None:
    client, base, _cfg = await _client_and_base()
    async with client:
        r = await client.post(f"{base}/api/v1/delete/mailbox", json=[full_address])
        r.raise_for_status()
        logger.info("mailcow_mailbox_deleted", address=full_address)


async def test_connection() -> tuple[bool, str]:
    """Lightweight connectivity probe for the admin UI "Проверить подключение" button."""
    try:
        client, base, cfg = await _client_and_base()
    except MailServerNotConfigured as exc:
        return False, str(exc)
    async with client:
        try:
            r = await client.get(f"{base}/api/v1/get/status/containers")
            r.raise_for_status()
            return True, "ok"
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)
