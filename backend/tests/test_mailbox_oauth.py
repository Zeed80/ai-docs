"""Mailbox auth: provider presets and the OAuth2 (XOAUTH2) path.

Gmail and most Microsoft 365 tenants stopped accepting the account password
for IMAP/SMTP, so a mailbox can now authenticate with an OAuth2 grant instead
(app/domain/oauth_mail.py, app/api/oauth.py). These cover the parts that are
easy to get subtly wrong and impossible to notice until real mail stops
flowing: the SASL payload's exact shape, the refresh/caching rules, who is
allowed to rebind a mailbox to a different account, and the cleanup that has
to happen when a connected mailbox is deleted.
"""

import base64
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

from app.auth.jwt import get_current_user
from app.auth.models import UserInfo, UserRole
from app.db.models import MailboxConfig, OAuthAppConfig
from app.domain import oauth_mail
from app.main import app
from app.utils.crypto import encrypt_password


def _mailbox(**kw) -> MailboxConfig:
    defaults = dict(
        name="procurement",
        imap_host="imap.gmail.com",
        imap_port=993,
        imap_user="buy@example.com",
        imap_password_encrypted=encrypt_password(""),
        smtp_host="smtp.gmail.com",
        smtp_port=587,
        smtp_user="buy@example.com",
        smtp_use_tls=True,
        auth_method="oauth2",
        oauth_provider="google",
        oauth_refresh_token_encrypted=encrypt_password("refresh-token"),
    )
    defaults.update(kw)
    return MailboxConfig(**defaults)


async def _google_app(db) -> OAuthAppConfig:
    from app.ai.secret_box import encrypt as encrypt_secret

    row = OAuthAppConfig(
        provider="google",
        client_id="client-id",
        client_secret_encrypted=encrypt_secret("client-secret"),
        redirect_uri="https://app.example.com/api/oauth/callback/google",
    )
    db.add(row)
    await db.commit()
    return row


# ── SASL XOAUTH2 wire format ────────────────────────────────────────────────


def test_xoauth2_payload_matches_the_sasl_spec():
    raw = oauth_mail.xoauth2_raw("user@example.com", "tok")
    assert raw == "user=user@example.com\x01auth=Bearer tok\x01\x01"
    assert base64.b64decode(oauth_mail.xoauth2_base64("user@example.com", "tok")).decode() == raw


def test_imap_authobject_answers_the_failure_continuation_with_an_empty_string():
    """A rejected XOAUTH2 bind gets a second continuation carrying a JSON error.

    The client must close it out with an empty response; resending the same
    credentials there is a protocol violation some servers never answer, which
    would hang the IMAP poll instead of failing it.
    """
    authobject = oauth_mail.imap_xoauth2_authobject("user@example.com", "tok")

    first = authobject(b"")
    second = authobject(b'{"status":"400","schemes":"Bearer"}')

    assert first == b"user=user@example.com\x01auth=Bearer tok\x01\x01"
    assert second == b""


# ── Token caching / refresh ─────────────────────────────────────────────────


async def test_valid_token_is_reused_without_calling_the_provider(db_session):
    mb = _mailbox(
        oauth_access_token_encrypted=encrypt_password("cached-token"),
        oauth_token_expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
    )
    db_session.add(mb)
    await db_session.commit()

    with patch.object(oauth_mail, "refresh_access_token_async", new=AsyncMock()) as refresh:
        token = await oauth_mail.get_valid_access_token(db_session, mb)

    assert token == "cached-token"
    refresh.assert_not_awaited()


async def test_expiring_token_is_refreshed_and_persisted(db_session):
    await _google_app(db_session)
    mb = _mailbox(
        oauth_access_token_encrypted=encrypt_password("old-token"),
        # Inside the 60s safety margin — treated as already gone.
        oauth_token_expires_at=datetime.now(timezone.utc) + timedelta(seconds=5),
    )
    db_session.add(mb)
    await db_session.commit()

    fresh = oauth_mail.TokenResult(
        access_token="new-token",
        refresh_token=None,  # Google omits it on refresh — the old one must survive
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        email=None,
        scope=None,
    )
    with patch.object(oauth_mail, "refresh_access_token_async", new=AsyncMock(return_value=fresh)):
        token = await oauth_mail.get_valid_access_token(db_session, mb)

    assert token == "new-token"
    from app.utils.crypto import decrypt_password

    assert decrypt_password(mb.oauth_access_token_encrypted) == "new-token"
    assert decrypt_password(mb.oauth_refresh_token_encrypted) == "refresh-token"


async def test_concurrent_callers_refresh_the_token_only_once(db_session):
    """The IMAP poll and an SMTP send can hit an expired token together.

    Without the single-flight lock both would refresh; with Microsoft (whose
    refresh token rotates) the second exchange invalidates the token the first
    one just stored, and the mailbox silently stops authenticating.
    """
    import asyncio

    await _google_app(db_session)
    mb = _mailbox(
        name="shared-race",
        oauth_access_token_encrypted=encrypt_password("stale"),
        oauth_token_expires_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )
    db_session.add(mb)
    await db_session.commit()

    calls = {"n": 0}

    async def counting_refresh(*args, **kwargs):
        calls["n"] += 1
        await asyncio.sleep(0.05)  # hold the lock long enough for the other to queue
        return oauth_mail.TokenResult(
            access_token=f"token-{calls['n']}",
            refresh_token=None,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            email=None,
            scope=None,
        )

    with patch.object(oauth_mail, "refresh_access_token_async", new=counting_refresh):
        tokens = await asyncio.gather(
            oauth_mail.get_valid_access_token(db_session, mb),
            oauth_mail.get_valid_access_token(db_session, mb),
        )

    assert calls["n"] == 1, "оба вызова обновили токен — лок не сработал"
    assert tokens[0] == tokens[1] == "token-1"

async def test_password_mailbox_is_never_treated_as_oauth_connected(db_session):
    mb = _mailbox(auth_method="password", oauth_provider=None,
                  oauth_refresh_token_encrypted=None)
    db_session.add(mb)
    await db_session.commit()

    with pytest.raises(oauth_mail.MailboxNotOAuthConnected):
        await oauth_mail.get_valid_access_token(db_session, mb)


async def test_refresh_without_a_configured_app_fails_loudly(db_session):
    """No Client ID/Secret → a named error, not a confusing HTTP failure."""
    mb = _mailbox(oauth_token_expires_at=datetime.now(timezone.utc) - timedelta(minutes=1))
    db_session.add(mb)
    await db_session.commit()

    with pytest.raises(oauth_mail.OAuthAppNotConfigured):
        await oauth_mail.get_valid_access_token(db_session, mb)


# ── Presets ─────────────────────────────────────────────────────────────────


async def test_presets_expose_provider_defaults_and_hints(client: AsyncClient):
    resp = await client.get("/api/mailbox/presets")
    assert resp.status_code == 200
    by_id = {p["id"]: p for p in resp.json()}

    gmail = by_id["gmail"]
    assert gmail["imap_host"] == "imap.gmail.com"
    assert gmail["smtp_port"] == 587 and gmail["smtp_use_tls"] is True
    assert "oauth2" in gmail["auth_methods"] and gmail["oauth_provider"] == "google"
    assert gmail["hint"]

    # Mail.ru's SMTP is implicit TLS on 465 — sending STARTTLS there fails.
    assert by_id["mail_ru"]["smtp_port"] == 465
    assert by_id["mail_ru"]["smtp_use_tls"] is False


async def test_preset_reports_oauth_unconfigured_until_an_admin_sets_it_up(
    client: AsyncClient, db_session
):
    before = {p["id"]: p for p in (await client.get("/api/mailbox/presets")).json()}
    assert before["gmail"]["oauth_configured"] is False

    await _google_app(db_session)

    after = {p["id"]: p for p in (await client.get("/api/mailbox/presets")).json()}
    assert after["gmail"]["oauth_configured"] is True


# ── Authorisation ───────────────────────────────────────────────────────────


def _as_user(sub: str, *roles: UserRole):
    """Replace the dev admin identity for one test."""
    user = UserInfo(
        sub=sub, email=f"{sub}@example.com", name=sub,
        preferred_username=sub, roles=list(roles), groups=[],
    )
    app.dependency_overrides[get_current_user] = lambda: user
    return user


@pytest.fixture(autouse=True)
def _restore_identity():
    yield
    app.dependency_overrides.pop(get_current_user, None)


async def test_non_admin_cannot_read_or_change_shared_mailboxes(
    client: AsyncClient, db_session
):
    """A shared mailbox decides which address the company sends from — editing
    it is an admin act, not something any logged-in viewer may do."""
    mb = _mailbox(auth_method="password", oauth_provider=None,
                  oauth_refresh_token_encrypted=None)
    db_session.add(mb)
    await db_session.commit()

    _as_user("viewer-sub", UserRole.viewer)

    assert (await client.get("/api/mailbox/configs")).status_code == 403
    assert (await client.patch(f"/api/mailbox/configs/{mb.id}",
                               json={"imap_user": "attacker@evil.test"})).status_code == 403
    assert (await client.delete(f"/api/mailbox/configs/{mb.id}")).status_code == 403


async def test_owner_may_connect_their_own_personal_mailbox_but_not_a_shared_one(
    client: AsyncClient, db_session
):
    await _google_app(db_session)
    personal = _mailbox(name="ivan@example.com", mailbox_type="personal",
                        owner_sub="ivan-sub")
    shared = _mailbox(name="accounting")
    db_session.add_all([personal, shared])
    await db_session.commit()

    _as_user("ivan-sub", UserRole.accountant)

    own = await client.post("/api/oauth/start",
                            json={"provider": "google", "mailbox_id": str(personal.id)})
    assert own.status_code == 200
    assert own.json()["authorize_url"].startswith("https://accounts.google.com/")

    other = await client.post("/api/oauth/start",
                              json={"provider": "google", "mailbox_id": str(shared.id)})
    assert other.status_code == 403


# ── Switching away from OAuth / deleting a connected mailbox ────────────────


async def test_reverting_to_password_auth_clears_the_stale_grant(
    client: AsyncClient, db_session
):
    mb = _mailbox(oauth_email="buy@example.com",
                  oauth_access_token_encrypted=encrypt_password("tok"))
    db_session.add(mb)
    await db_session.commit()

    resp = await client.patch(f"/api/mailbox/configs/{mb.id}",
                              json={"auth_method": "password", "imap_password": "app-password"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["auth_method"] == "password"
    assert body["oauth_connected"] is False
    assert body["oauth_provider"] is None

    await db_session.refresh(mb)
    assert mb.oauth_refresh_token_encrypted is None
    assert mb.oauth_access_token_encrypted is None


async def test_deleting_a_connected_mailbox_revokes_the_grant(
    client: AsyncClient, db_session
):
    """Otherwise the refresh token stays valid on the provider's side with no
    local record left of it for anyone to revoke."""
    mb = _mailbox()
    db_session.add(mb)
    await db_session.commit()

    with patch.object(oauth_mail, "revoke_tokens", new=AsyncMock(return_value=None)) as revoke:
        resp = await client.delete(f"/api/mailbox/configs/{mb.id}")

    assert resp.status_code == 204
    revoke.assert_awaited_once()


async def test_delete_still_succeeds_when_revocation_fails(
    client: AsyncClient, db_session
):
    """A provider outage must not leave an un-deletable mailbox behind."""
    mb = _mailbox(name="general")
    db_session.add(mb)
    await db_session.commit()

    with patch.object(oauth_mail, "revoke_tokens",
                      new=AsyncMock(return_value="HTTP 503")):
        resp = await client.delete(f"/api/mailbox/configs/{mb.id}")

    assert resp.status_code == 204
    assert await db_session.get(MailboxConfig, mb.id) is None


async def test_revoke_is_a_noop_for_providers_without_an_endpoint(db_session):
    """Microsoft withdraws consent in the account portal, not via an API."""
    mb = _mailbox(oauth_provider="microsoft")
    reason = await oauth_mail.revoke_tokens(db_session, mb)
    assert reason == "provider has no revocation endpoint"
