"""P0 of the email-client refactor: the mailbox actually syncs and failures show.

Root causes fixed here:
  * Celery beat only polled three hard-coded names — a mailbox added through the
    UI with any other name was never polled (looked "not configured").
  * IMAP errors were swallowed into an empty list — a login failure looked like
    an empty inbox instead of a sync error.
  * last_sync_at / sync_error were never written — the mailbox always looked dead.
"""

from unittest.mock import MagicMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import create_engine, delete
from sqlalchemy.orm import Session

from app.db.models import MailboxConfig


def _row(name: str, *, sweep: bool = True, active: bool = True) -> MailboxConfig:
    return MailboxConfig(
        name=name,
        imap_host="imap.example.com",
        imap_port=993,
        imap_user=f"{name}@example.com",
        imap_password_encrypted="x",
        imap_ssl=True,
        imap_folder="INBOX",
        is_active=active,
        sweep_enabled=sweep,
        mailbox_type="shared",
    )


@pytest.fixture
def sync_db(test_engine, monkeypatch):
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


def test_dispatch_fans_out_every_active_mailbox_by_name(sync_db):
    from app.tasks import email_triage

    with Session(sync_db) as db:
        db.add(_row("procurement"))
        db.add(_row("sales-team-2024"))  # arbitrary name — used to be ignored
        db.add(_row("archived", active=False))  # inactive — skipped
        db.commit()

    sent: list[str] = []
    fake = MagicMock()
    fake.apply_async = lambda args, queue=None: sent.append(args[0])
    with patch("app.tasks.ingest.poll_imap_mailbox", fake):
        result = email_triage.dispatch_mailbox_polls()

    assert result["status"] == "ok"
    assert set(sent) == {"procurement", "sales-team-2024"}


def test_list_active_mailboxes_all_vs_sweep(sync_db):
    from app.tasks.email_triage import list_active_mailboxes

    with Session(sync_db) as db:
        db.add(_row("shared-inbox", sweep=True))
        db.add(_row("private-no-consent", sweep=False))
        db.commit()

    assert set(list_active_mailboxes(require_sweep=False)) == {
        "shared-inbox",
        "private-no-consent",
    }
    assert list_active_mailboxes(require_sweep=True) == ["shared-inbox"]


def test_fetch_unseen_raises_on_connection_failure_instead_of_returning_empty():
    from app.tasks.imap_client import MailboxConfig as ImapCfg
    from app.tasks.imap_client import fetch_unseen_from_mailbox

    cfg = ImapCfg(
        name="procurement",
        host="imap.invalid.example",
        port=993,
        user="u",
        password="p",
    )
    with patch("imaplib.IMAP4_SSL", side_effect=OSError("connection refused")):
        with pytest.raises(OSError):
            fetch_unseen_from_mailbox(cfg)


def test_poll_records_sync_error_on_imap_failure(sync_db, monkeypatch):
    from app.tasks import ingest

    monkeypatch.setattr(ingest, "_get_sync_session", lambda: Session(sync_db))

    with Session(sync_db) as db:
        db.add(_row("procurement"))
        db.commit()

    from app.tasks.imap_client import MailboxConfig as ImapCfg

    with (
        patch(
            "app.tasks.imap_client.get_mailbox_configs",
            return_value=[ImapCfg(name="procurement", host="h", port=993, user="u", password="p")],
        ),
        patch(
            "app.tasks.imap_client.fetch_unseen_from_mailbox",
            side_effect=RuntimeError("AUTHENTICATIONFAILED"),
        ),
    ):
        with pytest.raises(Exception):
            ingest.poll_imap_mailbox("procurement")

    with Session(sync_db) as db:
        row = db.query(MailboxConfig).filter_by(name="procurement").one()
        assert row.sync_error and "AUTHENTICATIONFAILED" in row.sync_error


def test_poll_clears_sync_error_and_stamps_last_sync_on_success(sync_db, monkeypatch):
    from app.tasks import ingest
    from app.tasks.imap_client import MailboxConfig as ImapCfg

    monkeypatch.setattr(ingest, "_get_sync_session", lambda: Session(sync_db))
    with Session(sync_db) as db:
        r = _row("procurement")
        r.sync_error = "old failure"
        db.add(r)
        db.commit()

    with (
        patch(
            "app.tasks.imap_client.get_mailbox_configs",
            return_value=[ImapCfg(name="procurement", host="h", port=993, user="u", password="p")],
        ),
        patch("app.tasks.imap_client.fetch_unseen_from_mailbox", return_value=[]),
    ):
        out = ingest.poll_imap_mailbox("procurement")

    assert out["fetched"] == 0
    with Session(sync_db) as db:
        row = db.query(MailboxConfig).filter_by(name="procurement").one()
        assert row.sync_error is None
        assert row.last_sync_at is not None


async def test_email_mailboxes_endpoint_reports_sync_status(client: AsyncClient, db_session):
    db_session.add(_row("procurement"))
    bad = _row("accounting")
    bad.sync_error = "IMAP: [AUTHENTICATIONFAILED] Invalid credentials"
    db_session.add(bad)
    await db_session.commit()

    resp = await client.get("/api/email/mailboxes")
    assert resp.status_code == 200, resp.text
    by_name = {m["name"]: m for m in resp.json()}
    assert "procurement" in by_name and "accounting" in by_name
    assert by_name["accounting"]["sync_error"].startswith("IMAP:")
    assert by_name["procurement"]["sync_error"] is None


async def test_a_stricter_csp_set_by_an_endpoint_survives_the_middleware(monkeypatch):
    """Найдено при приёмочном прогоне (Правило 0).

    Выдача вложений ставит `default-src 'none'; sandbox`, а middleware
    безопасности перезаписывал заголовок общеприложенческой политикой со
    `script-src 'self'` — более строгая политика молча исчезала. Ослаблять то,
    что эндпоинт ужесточил осознанно, нельзя; выставлять общую там, где своей
    нет, — нужно.
    """
    from starlette.requests import Request
    from starlette.responses import Response

    from app.config import settings
    from app.middleware.security import SecurityHeadersMiddleware

    monkeypatch.setattr(settings, "csp_enabled", True, raising=False)
    mw = SecurityHeadersMiddleware(app=None)

    def _request() -> Request:
        return Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/api/email/x",
                "headers": [],
                "query_string": b"",
                "client": ("1.2.3.4", 0),
            }
        )

    async def _strict(_req):
        return Response(b"", headers={"Content-Security-Policy": "default-src 'none'; sandbox"})

    async def _plain(_req):
        return Response(b"")

    strict = await mw.dispatch(_request(), _strict)
    assert strict.headers["content-security-policy"] == "default-src 'none'; sandbox"

    plain = await mw.dispatch(_request(), _plain)
    assert "script-src 'self'" in plain.headers["content-security-policy"]
