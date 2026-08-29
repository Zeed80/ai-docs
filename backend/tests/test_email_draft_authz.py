"""Ф0.1 — ownership on the /api/email/drafts* surface.

Before this, none of create/list/read/patch/send checked who was asking, which
made the personal-mailbox privacy in app.domain.email_access decorative: any
authenticated user could list every draft in the system (including ones
composed inside a colleague's private mailbox), rewrite its recipients and
send it from that colleague's SMTP account.

The rule under test is app.domain.email_access.may_access_draft: the creator
always may; otherwise it is exactly "may you send from this draft's mailbox".
"""

import uuid
from typing import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.auth.models import UserInfo, UserRole
from app.db.models import DraftAction, MailboxConfig

OWNER_SUB = "dev-user"          # whoever the default test client authenticates as
COLLEAGUE_SUB = "colleague-sub"


def _mailbox(name: str, *, owner_sub: str | None, mailbox_type: str) -> MailboxConfig:
    return MailboxConfig(
        name=name,
        display_name=name,
        owner_sub=owner_sub,
        mailbox_type=mailbox_type,
        imap_host="mail.example.com",
        imap_port=993,
        imap_user=name,
        imap_password_encrypted="x",
        imap_ssl=True,
        is_active=True,
    )


def _draft(*, mailbox: str | None, created_by_sub: str | None) -> DraftAction:
    return DraftAction(
        action_type="email.send",
        entity_type="email",
        draft_data={
            "to_addresses": ["supplier@example.com"],
            "cc_addresses": [],
            "bcc_addresses": [],
            "subject": "Черновик",
            "body_html": "<p>текст</p>",
            "body_text": "текст",
            "mailbox": mailbox,
            "attachment_ids": [],
            "status": "draft",
            "risk_flags": [],
            "created_by_sub": created_by_sub,
        },
    )


@pytest_asyncio.fixture
async def colleague_client(db_session) -> AsyncIterator[AsyncClient]:
    """A second, unrelated employee — not an admin, owns their own mailbox.

    The identity is chosen per REQUEST (header ``X-Test-Sub``), not by a global
    dependency override: the default ``client`` fixture and this one share the
    single FastAPI app object, so a global override would silently re-identify
    both clients and quietly make the test prove nothing.
    """
    from fastapi import Request

    from app.auth.acting import get_effective_user
    from app.config import settings
    from app.db.session import get_db
    from app.main import app

    settings.rate_limit_api_per_minute = 0
    people = {
        COLLEAGUE_SUB: UserInfo(
            sub=COLLEAGUE_SUB, email="colleague@example.com", name="Коллега",
            preferred_username="colleague", roles=[UserRole.viewer],
        ),
        OWNER_SUB: UserInfo(
            sub=OWNER_SUB, email="dev@example.com", name="Владелец",
            preferred_username="dev", roles=[UserRole.admin],
        ),
    }

    async def override_get_db():
        yield db_session

    def override_effective_user(request: Request) -> UserInfo:
        return people[(request.headers.get("x-test-sub") or OWNER_SUB)]

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_effective_user] = override_effective_user
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", headers={"X-Test-Sub": COLLEAGUE_SUB}
    ) as c:
        yield c
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def mailboxes(db_session):
    db_session.add_all([
        _mailbox("procurement", owner_sub=None, mailbox_type="shared"),
        _mailbox("me@example.com", owner_sub=OWNER_SUB, mailbox_type="personal"),
        _mailbox("colleague@example.com", owner_sub=COLLEAGUE_SUB, mailbox_type="personal"),
    ])
    await db_session.commit()


async def test_colleague_cannot_list_a_private_draft(
    colleague_client: AsyncClient, db_session, mailboxes
):
    """A draft inside someone's personal mailbox is invisible to everyone else."""
    private = _draft(mailbox="me@example.com", created_by_sub=OWNER_SUB)
    db_session.add(private)
    await db_session.commit()

    mine = await colleague_client.get("/api/email/drafts", headers={"X-Test-Sub": OWNER_SUB})
    assert mine.status_code == 200
    assert str(private.id) in {d["id"] for d in mine.json()}

    theirs = await colleague_client.get("/api/email/drafts")
    assert theirs.status_code == 200
    assert str(private.id) not in {d["id"] for d in theirs.json()}


async def test_colleague_cannot_read_edit_or_send_a_private_draft(
    colleague_client: AsyncClient, db_session, mailboxes
):
    private = _draft(mailbox="me@example.com", created_by_sub=OWNER_SUB)
    db_session.add(private)
    await db_session.commit()
    draft_id = private.id

    assert (await colleague_client.get(f"/api/email/drafts/{draft_id}")).status_code == 404
    patched = await colleague_client.patch(
        f"/api/email/drafts/{draft_id}", json={"to_addresses": ["attacker@evil.example"]}
    )
    assert patched.status_code == 404
    assert (
        await colleague_client.post(f"/api/email/drafts/{draft_id}/risk-check")
    ).status_code == 404
    assert (
        await colleague_client.post(f"/api/email/drafts/{draft_id}/send")
    ).status_code == 404

    await db_session.refresh(private)
    assert private.draft_data["to_addresses"] == ["supplier@example.com"]
    assert not private.executed


async def test_shared_mailbox_draft_stays_visible_to_the_team(
    colleague_client: AsyncClient, db_session, mailboxes
):
    """Regression guard: a company inbox is shared on purpose — the fix must
    not turn shared mailboxes into private ones."""
    shared = _draft(mailbox="procurement", created_by_sub=OWNER_SUB)
    db_session.add(shared)
    await db_session.commit()

    listed = await colleague_client.get("/api/email/drafts")
    assert str(shared.id) in {d["id"] for d in listed.json()}
    assert (await colleague_client.get(f"/api/email/drafts/{shared.id}")).status_code == 200


async def test_cannot_create_a_draft_in_someone_elses_mailbox(colleague_client: AsyncClient, mailboxes):
    resp = await colleague_client.post(
        "/api/email/drafts",
        json={
            "to_addresses": ["supplier@example.com"],
            "subject": "Не моё",
            "body_html": "<p>x</p>",
            "mailbox": "me@example.com",
        },
    )
    assert resp.status_code == 403


async def test_created_draft_records_its_owner(colleague_client: AsyncClient, mailboxes):
    resp = await colleague_client.post(
        "/api/email/drafts",
        json={
            "to_addresses": ["supplier@example.com"],
            "subject": "Моё",
            "body_html": "<p>x</p>",
            "mailbox": "procurement",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["mailbox"] == "procurement"


async def test_legacy_ownerless_draft_is_not_reachable(
    colleague_client: AsyncClient, db_session, mailboxes
):
    """Rows written before Ф0.1 carry neither owner nor mailbox: unreachable
    rather than world-readable."""
    legacy = _draft(mailbox=None, created_by_sub=None)
    db_session.add(legacy)
    await db_session.commit()

    listed = await colleague_client.get("/api/email/drafts")
    assert str(legacy.id) not in {d["id"] for d in listed.json()}
    assert (await colleague_client.get(f"/api/email/drafts/{legacy.id}")).status_code == 404


async def test_staged_compose_attachment_can_be_uploaded(
    colleague_client: AsyncClient, db_session, monkeypatch, mailboxes
):
    """Regression: ``email_attachments.message_id`` was NOT NULL while
    ``POST /attachments/upload`` stages a file with no message yet, so
    attaching anything to an outbound mail failed with a 500. Found in Ф0.1."""
    import app.storage as storage

    monkeypatch.setattr(storage, "upload_file", lambda *a, **k: None)

    resp = await colleague_client.post(
        "/api/email/attachments/upload",
        files={"file": ("счёт.pdf", b"%PDF-1.4 test", "application/pdf")},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["filename"] == "счёт.pdf"

    from app.db.models import EmailAttachment

    att = await db_session.get(EmailAttachment, uuid.UUID(body["id"]))
    assert att.message_id is None
    assert att.uploaded_by_sub == COLLEAGUE_SUB


async def test_draft_cannot_reference_someone_elses_attachment(
    colleague_client: AsyncClient, db_session, mailboxes
):
    from app.db.models import EmailAttachment

    foreign = EmailAttachment(
        message_id=None,
        filename="чужой-договор.pdf",
        content_type="application/pdf",
        size=10,
        storage_path="email-attachments/aa/aaaa",
        sha256="a" * 64,
        uploaded_by_sub=OWNER_SUB,
    )
    db_session.add(foreign)
    await db_session.commit()

    resp = await colleague_client.post(
        "/api/email/drafts",
        json={
            "to_addresses": ["outside@example.com"],
            "subject": "Утечка",
            "body_html": "<p>см. вложение</p>",
            "mailbox": "procurement",
            "attachment_ids": [str(foreign.id)],
        },
    )
    assert resp.status_code == 403


# ── Ф0.2: the approval gate binds to the letter, not to the draft id ────────


async def test_send_rejects_content_changed_after_risk_check(
    colleague_client: AsyncClient, db_session, mailboxes
):
    """draft → risk_check → (content rewritten) → send must refuse."""
    draft = _draft(mailbox="procurement", created_by_sub=COLLEAGUE_SUB)
    from app.domain.email_send import draft_content_digest

    draft.draft_data["content_digest"] = draft_content_digest(draft.draft_data)
    db_session.add(draft)
    await db_session.commit()

    checked = await colleague_client.post(f"/api/email/drafts/{draft.id}/risk-check")
    assert checked.status_code == 200

    await db_session.refresh(draft)
    approved_digest = draft.draft_data["content_digest"]
    assert draft.draft_data["risk_checked_digest"] == approved_digest

    # Somebody rewrites the recipient behind the approved id.
    data = dict(draft.draft_data)
    data["to_addresses"] = ["attacker@evil.example"]
    data["content_digest"] = draft_content_digest(data)
    draft.draft_data = data
    from sqlalchemy.orm.attributes import flag_modified

    flag_modified(draft, "draft_data")
    await db_session.commit()

    resp = await colleague_client.post(
        f"/api/email/drafts/{draft.id}/send", json={"expected_digest": approved_digest}
    )
    assert resp.status_code == 409
    await db_session.refresh(draft)
    assert not draft.executed


async def test_send_accepts_the_digest_it_was_approved_with(
    colleague_client: AsyncClient, db_session, monkeypatch, mailboxes
):
    import app.tasks.email_sender as sender

    class _Task:
        id = "task-1"

    monkeypatch.setattr(sender.send_email_draft, "delay", lambda *a, **k: _Task())

    draft = _draft(mailbox="procurement", created_by_sub=COLLEAGUE_SUB)
    from app.domain.email_send import draft_content_digest

    draft.draft_data["content_digest"] = draft_content_digest(draft.draft_data)
    db_session.add(draft)
    await db_session.commit()

    await colleague_client.post(f"/api/email/drafts/{draft.id}/risk-check")
    await db_session.refresh(draft)
    digest = draft.draft_data["content_digest"]

    resp = await colleague_client.post(
        f"/api/email/drafts/{draft.id}/send", json={"expected_digest": digest}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "queued"


async def test_editing_a_draft_changes_its_digest(colleague_client: AsyncClient, mailboxes):
    created = await colleague_client.post(
        "/api/email/drafts",
        json={
            "to_addresses": ["supplier@example.com"],
            "subject": "Тема",
            "body_html": "<p>раз</p>",
            "mailbox": "procurement",
        },
    )
    assert created.status_code == 200, created.text
    first = created.json()["content_digest"]
    assert first

    patched = await colleague_client.patch(
        f"/api/email/drafts/{created.json()['id']}", json={"body_html": "<p>два</p>"}
    )
    assert patched.status_code == 200
    assert patched.json()["content_digest"] != first


# ── Ф0.3: an emailed attachment must never render in our origin ─────────────


@pytest_asyncio.fixture
async def message_with_attachments(db_session, mailboxes):
    from datetime import datetime, timezone

    from app.db.models import EmailAttachment, EmailMessage, EmailThread

    thread = EmailThread(subject="Со вложениями", mailbox="procurement", message_count=1)
    msg = EmailMessage(
        thread=thread,
        mailbox="procurement",
        subject="Со вложениями",
        from_address="sender@example.com",
        to_addresses=["procurement@example.com"],
        body_text="см. вложения",
        received_at=datetime.now(timezone.utc),
        message_id_header=f"<{uuid.uuid4()}@example.com>",
        has_attachments=True,
    )
    db_session.add_all([thread, msg])
    await db_session.flush()
    db_session.add_all([
        EmailAttachment(
            message_id=msg.id, filename="payload.html", content_type="text/html",
            size=10, storage_path="documents/aa/bb/hash1", sha256="1" * 64,
        ),
        EmailAttachment(
            message_id=msg.id, filename="Счёт №123.pdf", content_type="application/pdf",
            size=10, storage_path="documents/aa/bb/hash2", sha256="2" * 64,
        ),
    ])
    await db_session.commit()
    return msg


async def test_html_attachment_is_downloaded_not_rendered(
    colleague_client: AsyncClient, message_with_attachments, monkeypatch
):
    import app.storage as storage

    monkeypatch.setattr(storage, "download_file", lambda path: b"<script>alert(1)</script>")

    resp = await colleague_client.get(
        f"/api/email/messages/{message_with_attachments.id}/attachments/payload.html/content"
        "?disposition=inline"
    )
    assert resp.status_code == 200
    assert resp.headers["content-disposition"].startswith("attachment;")
    assert resp.headers["content-type"].startswith("application/octet-stream")
    assert resp.headers["x-content-type-options"] == "nosniff"


async def test_pdf_may_be_previewed_and_cyrillic_name_survives(
    colleague_client: AsyncClient, message_with_attachments, monkeypatch
):
    import app.storage as storage

    monkeypatch.setattr(storage, "download_file", lambda path: b"%PDF-1.4")

    from urllib.parse import quote

    name = "Счёт №123.pdf"
    resp = await colleague_client.get(
        f"/api/email/messages/{message_with_attachments.id}/attachments/{quote(name)}/content"
        "?disposition=inline"
    )
    assert resp.status_code == 200, resp.text
    cd = resp.headers["content-disposition"]
    assert cd.startswith("inline;")
    # RFC 5987 form, so a Cyrillic filename neither breaks the header nor is lost
    assert "filename*=UTF-8''" in cd
    assert quote(name) in cd

    default = await colleague_client.get(
        f"/api/email/messages/{message_with_attachments.id}/attachments/{quote(name)}/content"
    )
    assert default.headers["content-disposition"].startswith("attachment;")


async def test_oversized_upload_is_rejected_while_streaming(
    colleague_client: AsyncClient, db_session, monkeypatch, mailboxes
):
    """Ф0.4: the cap must apply during the read, not after the whole file is
    already resident in the worker's memory."""
    import app.storage as storage
    from app.db.models import MailServerConfig

    monkeypatch.setattr(storage, "upload_file", lambda *a, **k: None)
    db_session.add(MailServerConfig(singleton_key="default", max_attachment_mb=1))
    await db_session.commit()

    resp = await colleague_client.post(
        "/api/email/attachments/upload",
        files={"file": ("big.bin", b"x" * (2 * 1024 * 1024), "application/octet-stream")},
    )
    assert resp.status_code == 413
    assert "1 МБ" in resp.json()["detail"]

    ok = await colleague_client.post(
        "/api/email/attachments/upload",
        files={"file": ("small.bin", b"x" * 1024, "application/octet-stream")},
    )
    assert ok.status_code == 200
