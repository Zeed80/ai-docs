"""OAuth2 (XOAUTH2) for mailbox IMAP/SMTP — Gmail and Microsoft 365.

Plain account passwords no longer work for these two providers in most
setups (see mailbox_presets.py hints); the alternative to an app password is
this OAuth2 flow. It trades a one-time browser consent for a refresh token
that mints short-lived access tokens on demand — nothing password-shaped is
ever stored, and access can be revoked by the user at any time on the
provider's side without touching this app.

Flow (app/api/oauth.py drives it):
  1. build_authorize_url() sends the browser to the provider's consent screen.
  2. exchange_code() trades the returned ``code`` for tokens + the account's
     email (Google via userinfo, Microsoft via its id_token claims — Microsoft
     doesn't expose a plain userinfo endpoint for IMAP/SMTP-only scopes).
  3. get_valid_access_token{,_sync}() is what callers (imap_client.py,
     email_sender.py, mailbox.py's connection test) actually use: returns a
     cached access token if still fresh, else refreshes it via the stored
     refresh token and persists the new one.
"""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
import structlog

logger = structlog.get_logger()

PROVIDERS: dict[str, dict] = {
    "google": {
        "authorize_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        "userinfo_url": "https://openidconnect.googleapis.com/v1/userinfo",
        "scope": "https://mail.google.com/ openid email",
        # access_type=offline is what makes Google issue a refresh_token at
        # all; prompt=consent forces it even on a second connect from the same
        # account (Google otherwise silently omits it after the first grant).
        "extra_authorize_params": {"access_type": "offline", "prompt": "consent"},
    },
    "microsoft": {
        "authorize_url": "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        "token_url": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        "userinfo_url": None,
        "scope": (
            "https://outlook.office365.com/IMAP.AccessAsUser.All "
            "https://outlook.office365.com/SMTP.Send offline_access openid email"
        ),
        "extra_authorize_params": {"prompt": "select_account"},
    },
}


@dataclass
class TokenResult:
    access_token: str
    refresh_token: str | None
    expires_at: datetime
    email: str | None
    scope: str | None


def build_authorize_url(provider: str, client_id: str, redirect_uri: str, state: str) -> str:
    cfg = PROVIDERS[provider]
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": cfg["scope"],
        "state": state,
        **cfg["extra_authorize_params"],
    }
    return f"{cfg['authorize_url']}?{urlencode(params)}"


def _email_from_id_token(id_token: str) -> str | None:
    """Best-effort email extraction from an unverified id_token payload.

    Display only — never used to authenticate. Microsoft's IMAP/SMTP scopes
    don't carry a Graph-callable access token, so there's no userinfo
    endpoint to hit; the id_token's own claims are the only source left.
    """
    try:
        payload_b64 = id_token.split(".")[1]
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
        return payload.get("email") or payload.get("preferred_username")
    except Exception:
        return None


def _to_result(provider: str, body: dict, fallback_refresh_token: str | None, email: str | None) -> TokenResult:
    expires_in = int(body.get("expires_in", 3600))
    return TokenResult(
        access_token=body["access_token"],
        refresh_token=body.get("refresh_token") or fallback_refresh_token,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=expires_in),
        email=email,
        scope=body.get("scope"),
    )


async def exchange_code(
    provider: str, client_id: str, client_secret: str, redirect_uri: str, code: str,
) -> TokenResult:
    """First leg of the flow: authorization code -> tokens + account email."""
    cfg = PROVIDERS[provider]
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(cfg["token_url"], data={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        })
        resp.raise_for_status()
        body = resp.json()

        email = None
        if cfg["userinfo_url"]:
            r = await client.get(
                cfg["userinfo_url"], headers={"Authorization": f"Bearer {body['access_token']}"}
            )
            if r.status_code == 200:
                email = r.json().get("email")
        elif body.get("id_token"):
            email = _email_from_id_token(body["id_token"])

    return _to_result(provider, body, fallback_refresh_token=None, email=email)


async def refresh_access_token_async(
    provider: str, client_id: str, client_secret: str, refresh_token: str,
) -> TokenResult:
    cfg = PROVIDERS[provider]
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(cfg["token_url"], data={
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        })
    resp.raise_for_status()
    # Providers commonly omit refresh_token on a refresh response (it doesn't
    # rotate) — keep using the one we already had in that case.
    return _to_result(provider, resp.json(), fallback_refresh_token=refresh_token, email=None)


def refresh_access_token_sync(
    provider: str, client_id: str, client_secret: str, refresh_token: str,
) -> TokenResult:
    cfg = PROVIDERS[provider]
    with httpx.Client(timeout=15.0) as client:
        resp = client.post(cfg["token_url"], data={
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        })
    resp.raise_for_status()
    return _to_result(provider, resp.json(), fallback_refresh_token=refresh_token, email=None)


def xoauth2_raw(user: str, access_token: str) -> str:
    """The SASL XOAUTH2 payload, unencoded.

    imaplib base64-encodes whatever its authobject callback returns, so IMAP
    callers must pass this raw string. SMTP has no such helper — smtplib
    callers need ``xoauth2_base64`` instead for a manual ``AUTH`` command.
    """
    return f"user={user}\x01auth=Bearer {access_token}\x01\x01"


def xoauth2_base64(user: str, access_token: str) -> str:
    return base64.b64encode(xoauth2_raw(user, access_token).encode("utf-8")).decode("ascii")


def imap_xoauth2_authobject(user: str, access_token: str):
    """``authobject`` for ``imaplib.IMAP4.authenticate("XOAUTH2", ...)``.

    XOAUTH2 sends the credentials on the first server continuation; on
    failure the server issues a *second* continuation carrying a JSON error
    that the client must close out with an empty response — resending the
    same credentials there is a protocol violation some servers hang on.
    imaplib calls this with one positional arg (the decoded challenge) each
    time, so a plain lambda can't tell first call from second — this closure
    tracks it.
    """
    sent = False

    def _cb(_challenge: bytes) -> bytes:
        nonlocal sent
        if sent:
            return b""
        sent = True
        return xoauth2_raw(user, access_token).encode("utf-8")

    return _cb


class MailboxNotOAuthConnected(RuntimeError):
    pass


class OAuthAppNotConfigured(RuntimeError):
    pass


async def get_valid_access_token(db, mailbox) -> str:
    """Async version — FastAPI (mailbox connection test) and the Celery send
    task (which already runs its own asyncio loop, see email_sender.py)."""
    from sqlalchemy import select
    from app.ai.secret_box import decrypt as decrypt_client_secret
    from app.db.models import OAuthAppConfig
    from app.utils.crypto import decrypt_password, encrypt_password

    if mailbox.auth_method != "oauth2" or not mailbox.oauth_refresh_token_encrypted:
        raise MailboxNotOAuthConnected(f"Ящик «{mailbox.name}» не подключён через OAuth2")

    now = datetime.now(timezone.utc)
    if (
        mailbox.oauth_access_token_encrypted
        and mailbox.oauth_token_expires_at
        and mailbox.oauth_token_expires_at > now + timedelta(seconds=60)
    ):
        return decrypt_password(mailbox.oauth_access_token_encrypted)

    app_cfg = (
        await db.execute(select(OAuthAppConfig).where(OAuthAppConfig.provider == mailbox.oauth_provider))
    ).scalar_one_or_none()
    if not app_cfg or not app_cfg.client_id or not app_cfg.client_secret_encrypted:
        raise OAuthAppNotConfigured(
            f"OAuth-приложение «{mailbox.oauth_provider}» не настроено (см. /admin/integrations)"
        )

    result = await refresh_access_token_async(
        mailbox.oauth_provider,
        app_cfg.client_id,
        decrypt_client_secret(app_cfg.client_secret_encrypted),
        decrypt_password(mailbox.oauth_refresh_token_encrypted),
    )
    mailbox.oauth_access_token_encrypted = encrypt_password(result.access_token)
    mailbox.oauth_token_expires_at = result.expires_at
    if result.refresh_token:
        mailbox.oauth_refresh_token_encrypted = encrypt_password(result.refresh_token)
    await db.commit()
    return result.access_token


def get_valid_access_token_sync(db, mailbox) -> str:
    """Sync version — Celery's IMAP poll task (app/tasks/imap_client.py) runs
    on a plain sync SQLAlchemy session, no event loop available."""
    from sqlalchemy import select
    from app.ai.secret_box import decrypt as decrypt_client_secret
    from app.db.models import OAuthAppConfig
    from app.utils.crypto import decrypt_password, encrypt_password

    if mailbox.auth_method != "oauth2" or not mailbox.oauth_refresh_token_encrypted:
        raise MailboxNotOAuthConnected(f"Ящик «{mailbox.name}» не подключён через OAuth2")

    now = datetime.now(timezone.utc)
    if (
        mailbox.oauth_access_token_encrypted
        and mailbox.oauth_token_expires_at
        and mailbox.oauth_token_expires_at > now + timedelta(seconds=60)
    ):
        return decrypt_password(mailbox.oauth_access_token_encrypted)

    app_cfg = db.execute(
        select(OAuthAppConfig).where(OAuthAppConfig.provider == mailbox.oauth_provider)
    ).scalar_one_or_none()
    if not app_cfg or not app_cfg.client_id or not app_cfg.client_secret_encrypted:
        raise OAuthAppNotConfigured(
            f"OAuth-приложение «{mailbox.oauth_provider}» не настроено (см. /admin/integrations)"
        )

    result = refresh_access_token_sync(
        mailbox.oauth_provider,
        app_cfg.client_id,
        decrypt_client_secret(app_cfg.client_secret_encrypted),
        decrypt_password(mailbox.oauth_refresh_token_encrypted),
    )
    mailbox.oauth_access_token_encrypted = encrypt_password(result.access_token)
    mailbox.oauth_token_expires_at = result.expires_at
    if result.refresh_token:
        mailbox.oauth_refresh_token_encrypted = encrypt_password(result.refresh_token)
    db.commit()
    return result.access_token
