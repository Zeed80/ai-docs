"""Personal mailbox lifecycle: consent to sweep, re-provisioning, safe revoke.

Covers the rules that make a personal mailbox different from a company inbox:
  * the agent does not read it until its owner says so (sweep_enabled);
  * a DB outage in the sweep is reported, not disguised as a normal run;
  * revoking is reversible by default and never a one-way door for the user;
  * destroying mail requires echoing the address.

Two dependencies deliberately live outside the per-test transaction and are
substituted here: the Celery sweep uses a synchronous session of its own
(app.db.sync_session), and the Mailcow connection config is read through its own
async session (app.services.integration_config) — neither can see rows held in
the rolled-back test transaction.
"""

from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import create_engine, delete
from sqlalchemy.orm import Session

from app.db.models import MailboxConfig, User
from app.services.integration_config import MailServerConfig

OWNER_SUB = "employee-sub"
ADDRESS = "ivan.petrov@example.com"

MAIL_CFG = MailServerConfig(
    api_url="https://mail.example.com",
    api_key="test-key",
    mail_domain="example.com",
    webmail_url="https://mail.example.com",
    imap_host="mail.example.com",
    imap_port=993,
    smtp_host="mail.example.com",
    smtp_port=465,
    default_quota_mb=2048,
)


@pytest.fixture(autouse=True)
def mail_server_configured():
    """Pretend the admin has connected a Mailcow instance."""
    with patch(
        "app.services.integration_config.get_mail_server_config",
        new=AsyncMock(return_value=MAIL_CFG),
    ):
        yield


@pytest.fixture
async def employee(db_session):
    db_session.add(
        User(
            sub=OWNER_SUB,
            email="ivan@example.com",
            name="Иван Петров",
            preferred_username="ivan",
            role="buyer",
            is_active=True,
        )
    )
    await db_session.commit()


def _personal_row(**overrides) -> MailboxConfig:
    values = dict(
        name=ADDRESS,
        display_name="Иван Петров — личная почта",
        owner_sub=OWNER_SUB,
        mailbox_type="personal",
        imap_host="mail.example.com",
        imap_port=993,
        imap_user=ADDRESS,
        imap_password_encrypted="x",
        imap_ssl=True,
        is_active=True,
        sweep_enabled=False,
        quota_mb=2048,
    )
    values.update(overrides)
    return MailboxConfig(**values)


@pytest.fixture
async def personal_mailbox(db_session, employee):
    cfg = _personal_row()
    db_session.add(cfg)
    await db_session.commit()
    return cfg


# ── Sweep consent (Celery path, synchronous session) ────────────────────────


@pytest.fixture
def sync_db(test_engine, monkeypatch):
    """Point app.db.sync_session at the test database.

    The sweep runs in a Celery worker with its own engine built from settings —
    without this it would query the deployment database instead of the test one.
    """
    # render_as_string(hide_password=False): str(url) masks the password with ***
    sync_url = test_engine.url.render_as_string(hide_password=False).replace(
        "postgresql+asyncpg://", "postgresql://"
    )
    engine = create_engine(sync_url, pool_pre_ping=True)

    import app.db.sync_session as sync_module

    monkeypatch.setattr(sync_module, "sync_session", lambda: Session(engine))

    with Session(engine) as db:
        db.execute(delete(MailboxConfig))
        db.commit()
    try:
        yield engine
    finally:
        with Session(engine) as db:
            db.execute(delete(MailboxConfig))
            db.commit()
        engine.dispose()


def test_triage_sweeps_only_mailboxes_with_consent(sync_db):
    from app.tasks.email_triage import _sweepable_mailbox_names

    with Session(sync_db) as db:
        db.add(_personal_row())  # personal, sweep_enabled=False
        db.add(
            MailboxConfig(
                name="procurement",
                display_name="Закупки",
                mailbox_type="shared",
                imap_host="imap.example.com",
                imap_port=993,
                imap_user="procurement",
                imap_password_encrypted="x",
                imap_ssl=True,
                is_active=True,
                sweep_enabled=True,
            )
        )
        db.commit()

    names = _sweepable_mailbox_names()
    assert "procurement" in names
    assert ADDRESS not in names, "личный ящик метётся без согласия владельца"

    with Session(sync_db) as db:
        row = db.query(MailboxConfig).filter_by(name=ADDRESS).one()
        row.sweep_enabled = True
        db.commit()

    assert ADDRESS in _sweepable_mailbox_names()


def test_triage_reports_db_failure_instead_of_faking_defaults(monkeypatch):
    """A DB outage must not look like a successful run over legacy mailbox names."""
    import app.db.sync_session as sync_module
    import app.tasks.email_triage as triage

    def broken_session():
        raise RuntimeError("db down")

    monkeypatch.setattr(sync_module, "sync_session", broken_session)

    result = triage.run_triage(None)
    assert result["status"] == "error"
    assert "mailbox list unavailable" in result["error"]
    assert result["emails"] == 0


# ── Self-service consent ────────────────────────────────────────────────────


async def test_owner_can_toggle_sweep_for_own_mailbox(client: AsyncClient, db_session):
    db_session.add(
        _personal_row(name="dev@example.com", owner_sub="dev-user", imap_user="dev@example.com")
    )
    await db_session.commit()

    resp = await client.patch("/api/mailbox/me/sweep", json={"sweep_enabled": True})
    assert resp.status_code == 200, resp.text
    assert resp.json()["sweep_enabled"] is True

    resp = await client.get("/api/mailbox/me")
    assert resp.json()["sweep_enabled"] is True


# ── Revoke / re-provision ───────────────────────────────────────────────────


async def test_deactivate_then_provision_again(client: AsyncClient, db_session, personal_mailbox):
    """Revoke must not lock the user out of ever having a mailbox again."""
    with patch("app.services.mailcow_api.set_mailbox_active", new=AsyncMock()):
        resp = await client.post(
            f"/api/admin/users/{OWNER_SUB}/mailbox/revoke",
            json={"delete_on_server": False},
        )
    assert resp.status_code == 204, resp.text

    await db_session.refresh(personal_mailbox)
    assert personal_mailbox.is_active is False
    assert personal_mailbox.sweep_enabled is False

    with (
        patch(
            "app.services.mailcow_api.check_local_part_available", new=AsyncMock(return_value=True)
        ),
        patch("app.services.mailcow_api.create_mailbox", new=AsyncMock(return_value={})),
    ):
        resp = await client.post(
            f"/api/admin/users/{OWNER_SUB}/mailbox", json={"local_part": "ivan.petrov"}
        )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["address"] == ADDRESS
    assert body["generated_password"]


async def test_new_mailbox_starts_without_ai_access(client: AsyncClient, employee):
    with (
        patch(
            "app.services.mailcow_api.check_local_part_available", new=AsyncMock(return_value=True)
        ),
        patch("app.services.mailcow_api.create_mailbox", new=AsyncMock(return_value={})),
    ):
        resp = await client.post(f"/api/admin/users/{OWNER_SUB}/mailbox", json={})
    assert resp.status_code == 201, resp.text

    resp = await client.get(f"/api/admin/users/{OWNER_SUB}/mailbox")
    assert resp.json()["sweep_enabled"] is False


async def test_destructive_delete_requires_address_confirmation(
    client: AsyncClient, personal_mailbox
):
    delete_mock = AsyncMock()
    with patch("app.services.mailcow_api.delete_mailbox", new=delete_mock):
        resp = await client.post(
            f"/api/admin/users/{OWNER_SUB}/mailbox/revoke",
            json={"delete_on_server": True, "confirm_address": "wrong@example.com"},
        )
    assert resp.status_code == 400
    delete_mock.assert_not_awaited()

    with patch("app.services.mailcow_api.delete_mailbox", new=delete_mock):
        resp = await client.post(
            f"/api/admin/users/{OWNER_SUB}/mailbox/revoke",
            json={"delete_on_server": True, "confirm_address": ADDRESS},
        )
    assert resp.status_code == 204, resp.text
    delete_mock.assert_awaited_once()


async def test_provision_uses_configured_default_quota(client: AsyncClient, employee):
    create_mock = AsyncMock(return_value={})
    with (
        patch(
            "app.services.mailcow_api.check_local_part_available", new=AsyncMock(return_value=True)
        ),
        patch("app.services.mailcow_api.create_mailbox", new=create_mock),
    ):
        resp = await client.post(f"/api/admin/users/{OWNER_SUB}/mailbox", json={})
    assert resp.status_code == 201, resp.text
    assert create_mock.await_args.kwargs["quota_mb"] == 2048


# ── Integration config validation ───────────────────────────────────────────


async def test_api_url_is_validated(client: AsyncClient):
    resp = await client.put(
        "/api/admin/integrations/mail-server",
        json={"api_url": "mail.example.com"},  # no scheme
    )
    assert resp.status_code == 422

    resp = await client.put(
        "/api/admin/integrations/mail-server",
        json={"api_url": "https://mail.example.com/api/v1/"},  # path included
    )
    assert resp.status_code == 422
