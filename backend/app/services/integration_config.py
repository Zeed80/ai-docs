"""Runtime-configurable integration settings (Authentik), stored in Redis.

Lets an admin set the Authentik API token / external URL from the project admin UI
without editing infra/.env. Falls back to the env-provided settings when unset.
The token is never returned to clients — only a "set" flag and a masked hint.
"""
from __future__ import annotations

from app.config import settings
from app.utils.redis_client import get_sync_redis

_TOKEN_KEY = "integration:authentik_api_token"
_URL_KEY = "integration:authentik_external_url"


def _get(key: str) -> str | None:
    try:
        return get_sync_redis().get(key)
    except Exception:  # pragma: no cover - Redis optional/transient
        return None


def get_authentik_token() -> str:
    """Effective Authentik API token: runtime override (Redis) → env fallback."""
    return _get(_TOKEN_KEY) or settings.authentik_api_token


def get_authentik_external_url() -> str:
    """Effective Authentik external URL: runtime override → env fallback."""
    return _get(_URL_KEY) or settings.authentik_external_url


def set_authentik_token(token: str | None) -> None:
    r = get_sync_redis()
    if token:
        r.set(_TOKEN_KEY, token)
    else:
        r.delete(_TOKEN_KEY)


def set_authentik_external_url(url: str | None) -> None:
    r = get_sync_redis()
    if url:
        r.set(_URL_KEY, url.rstrip("/"))
    else:
        r.delete(_URL_KEY)


def mask_token(token: str) -> str:
    """Return a non-reversible hint like '••••cdef' for display."""
    if not token:
        return ""
    return "••••" + token[-4:] if len(token) >= 4 else "••••"
