"""Runtime-configurable integration settings (Authentik, mail server).

Lets an admin set the Authentik API token / external URL from the project admin UI
without editing infra/.env. Falls back to the env-provided settings when unset.
The token is never returned to clients — only a "set" flag and a masked hint.
"""

from __future__ import annotations

from dataclasses import dataclass

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


# ── Mail server (Mailcow) — durable, encrypted in Postgres ─────────────────
# Unlike the Authentik token above, the Mailcow API key lives in the
# mail_server_config table (single row, api_key_encrypted via app.ai.secret_box)
# rather than Redis: it's a lower-traffic, higher-durability secret and doesn't
# need the hot-path Redis lookup Authentik gets on every request.

_MAIL_SERVER_SINGLETON = "default"


@dataclass
class MailServerConfig:
    api_url: str | None
    api_key: str
    mail_domain: str | None
    webmail_url: str | None
    imap_host: str | None
    imap_port: int
    smtp_host: str | None
    smtp_port: int
    default_quota_mb: int = 1024

    @property
    def configured(self) -> bool:
        return bool(self.api_url and self.api_key and self.mail_domain)


async def get_mail_server_config() -> MailServerConfig:
    """Load the Mailcow connection config, decrypting the API key."""
    from sqlalchemy import select

    from app.ai.secret_box import decrypt
    from app.db.models import MailServerConfig as MailServerConfigRow
    from app.db.session import _get_session_factory

    factory = _get_session_factory()
    async with factory() as db:
        row = (
            await db.execute(
                select(MailServerConfigRow).where(
                    MailServerConfigRow.singleton_key == _MAIL_SERVER_SINGLETON
                )
            )
        ).scalar_one_or_none()

    if row is None:
        return MailServerConfig(
            api_url=None,
            api_key="",
            mail_domain=None,
            webmail_url=None,
            imap_host=None,
            imap_port=993,
            smtp_host=None,
            smtp_port=465,
            default_quota_mb=1024,
        )
    return MailServerConfig(
        api_url=row.api_url,
        api_key=decrypt(row.api_key_encrypted),
        mail_domain=row.mail_domain,
        webmail_url=row.webmail_url,
        imap_host=row.imap_host,
        imap_port=row.imap_port,
        smtp_host=row.smtp_host,
        smtp_port=row.smtp_port,
        default_quota_mb=row.default_quota_mb,
    )
