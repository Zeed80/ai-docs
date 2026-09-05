"""Ф6.1 — an emailed invoice must actually reach recognition.

The core defect this covers: ``_store_attachment`` created a Document from the
attachment and stopped. ``process_document`` was reachable only through a
``run_extraction`` filter rule, a manual API call, or ``run_triage`` — which is
not in the beat schedule. So "счёт пришёл письмом → он в системе" never
happened on its own, while the API docstring claimed it did.
"""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, delete
from sqlalchemy.orm import Session

from app.db.models import Document, DocumentStatus, MailboxConfig


def _attachment(filename="Счёт УТ-2562.pdf", content=b"%PDF-1.4 invoice", ctype="application/pdf"):
    import hashlib

    return SimpleNamespace(
        filename=filename,
        content=content,
        content_type=ctype,
        size=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        content_id=None,
        is_inline=False,
    )


def _mailbox(name, **kw) -> MailboxConfig:
    defaults = dict(
        display_name=name,
        imap_host="mail.example.com",
        imap_port=993,
        imap_user=name,
        imap_password_encrypted="x",
        imap_ssl=True,
        is_active=True,
    )
    defaults.update(kw)
    return MailboxConfig(name=name, **defaults)


def _cleanup_since(engine, started):
    """Remove exactly what a test created, children first.

    Hand-ordered because these rows are committed outside the suite's per-test
    transaction: a leftover child (DocumentLink, EmailAttachment) makes the
    NEXT fixture's cleanup die on a foreign key, which surfaces as an
    unrelated test erroring at setup.
    """
    from sqlalchemy import select as _select

    from app.db.models import (
        Document,
        DocumentExtraction,
        DocumentLink,
        EmailAttachment,
        EmailMessage,
        EmailThread,
        Invoice,
        MailboxConfig,
        Party,
        QuarantineEntry,
    )

    with Session(engine) as db:
        msg_ids = _select(EmailMessage.id).where(EmailMessage.created_at >= started)
        doc_ids = _select(Document.id).where(
            (Document.source_email_id.in_(msg_ids)) | (Document.created_at >= started)
        )
        db.execute(delete(DocumentLink).where(DocumentLink.document_id.in_(doc_ids)))
        db.execute(delete(QuarantineEntry).where(QuarantineEntry.document_id.in_(doc_ids)))
        db.execute(delete(EmailAttachment).where(EmailAttachment.message_id.in_(msg_ids)))
        db.execute(delete(EmailAttachment).where(EmailAttachment.created_at >= started))
        db.execute(delete(Invoice).where(Invoice.document_id.in_(doc_ids)))
        db.execute(delete(DocumentExtraction).where(DocumentExtraction.document_id.in_(doc_ids)))
        db.execute(delete(Document).where(Document.id.in_(doc_ids)))
        for model in (EmailMessage, EmailThread, MailboxConfig, Party):
            db.execute(delete(model).where(model.created_at >= started))
        db.commit()


@pytest.fixture
def sync_db(test_engine, monkeypatch):
    """Sync session that really commits — the ingest path is sync code.

    Only rows created by this test are removed afterwards. Wiping
    ``MailboxConfig`` wholesale (the pattern in test_email_client_p0.py) reaches
    outside the per-test transaction the rest of the suite uses and deletes
    mailboxes other tests are relying on.
    """
    sync_url = test_engine.url.render_as_string(hide_password=False).replace(
        "postgresql+asyncpg://", "postgresql://"
    )
    engine = create_engine(sync_url, pool_pre_ping=True)
    import app.db.sync_session as sync_module

    monkeypatch.setattr(sync_module, "sync_session", lambda: Session(engine))

    started = datetime.now(UTC)
    try:
        with Session(engine) as db:
            yield db
    finally:
        _cleanup_since(engine, started)
        engine.dispose()


def test_shared_mailbox_processes_attachments_but_holds_for_review(sync_db):
    from app.tasks.ingest import _mailbox_automation

    sync_db.add(_mailbox("procurement"))
    sync_db.commit()

    process, auto_approve, triage_mode = _mailbox_automation(sync_db, "procurement")
    assert process is True  # recognition runs…
    assert auto_approve is False  # …but a human still approves (product decision)
    assert triage_mode == "classify"


def test_personal_mailbox_needs_owner_consent(sync_db):
    from app.tasks.ingest import _mailbox_automation

    sync_db.add(
        _mailbox(
            "employee@example.com",
            mailbox_type="personal",
            owner_sub="emp",
            sweep_enabled=False,
            auto_process_attachments=True,
        )
    )
    sync_db.commit()

    # Recognising the contents of private mail — and reading it at all — is
    # exactly what sweep_enabled covers, so triage is off too.
    assert _mailbox_automation(sync_db, "employee@example.com") == (False, False, "off")

    cfg = sync_db.query(MailboxConfig).filter_by(name="employee@example.com").one()
    cfg.sweep_enabled = True
    sync_db.commit()
    assert _mailbox_automation(sync_db, "employee@example.com") == (True, False, "classify")


def test_mailbox_may_opt_into_auto_approval(sync_db):
    from app.tasks.ingest import _mailbox_automation

    sync_db.add(_mailbox("accounting", auto_approve_invoices=True))
    sync_db.commit()
    assert _mailbox_automation(sync_db, "accounting") == (True, True, "classify")


def test_unknown_mailbox_defaults_to_processing_without_auto_approval(sync_db):
    from app.tasks.ingest import _mailbox_automation

    assert _mailbox_automation(sync_db, "never-configured") == (True, False, "classify")


def test_stored_attachment_carries_the_auto_verify_override(sync_db, monkeypatch):
    """The per-document flag is what extraction.py reads; without it an emailed
    invoice would follow the GLOBAL auto_verify setting and could be approved
    with no human in the loop."""
    import app.storage as storage
    from app.db.models import EmailMessage, EmailThread
    from app.tasks.ingest import _store_attachment

    monkeypatch.setattr(storage, "upload_file", lambda *a, **k: None)

    thread = EmailThread(subject="Счёт", mailbox="procurement", message_count=1)
    msg = EmailMessage(
        thread=thread,
        mailbox="procurement",
        subject="Счёт",
        from_address="supplier@example.com",
        to_addresses=["procurement@example.com"],
        received_at=datetime.now(UTC),
        message_id_header=f"<{uuid.uuid4()}@example.com>",
    )
    sync_db.add_all([thread, msg])
    sync_db.flush()

    doc = _store_attachment(sync_db, _attachment(), msg.id, "procurement", auto_approve=False)
    assert doc is not None
    assert doc.metadata_["auto_verify"] is False
    assert doc.source_channel == "email"
    assert doc.source_email_id == msg.id
    assert doc.status == DocumentStatus.ingested

    doc2 = _store_attachment(
        sync_db,
        _attachment(filename="Счёт-2.pdf", content=b"%PDF other"),
        msg.id,
        "procurement",
        auto_approve=True,
    )
    assert doc2.metadata_["auto_verify"] is True


def test_inline_logo_never_becomes_a_document(sync_db, monkeypatch):
    import app.storage as storage
    from app.db.models import EmailMessage, EmailThread
    from app.tasks.ingest import _store_attachment

    monkeypatch.setattr(storage, "upload_file", lambda *a, **k: None)
    thread = EmailThread(subject="Подпись", mailbox="procurement", message_count=1)
    msg = EmailMessage(
        thread=thread,
        mailbox="procurement",
        subject="Подпись",
        from_address="x@y.z",
        to_addresses=["procurement@example.com"],
        received_at=datetime.now(UTC),
        message_id_header=f"<{uuid.uuid4()}@example.com>",
    )
    sync_db.add_all([thread, msg])
    sync_db.flush()

    logo = _attachment(filename="logo.png", content=b"\x89PNG", ctype="image/png")
    logo.is_inline = True
    logo.content_id = "logo@firm"
    assert _store_attachment(sync_db, logo, msg.id, "procurement") is None


def test_failed_storage_upload_does_not_leave_a_dangling_path(sync_db, monkeypatch):
    """Ф1.5: the row used to keep a storage_path pointing at bytes that were
    never written, so every later download answered 502."""
    import app.storage as storage
    from app.db.models import EmailAttachment, EmailMessage, EmailThread
    from app.tasks.ingest import _store_attachment

    def _boom(*a, **k):
        raise RuntimeError("MinIO down")

    monkeypatch.setattr(storage, "upload_file", _boom)
    thread = EmailThread(subject="Счёт", mailbox="procurement", message_count=1)
    msg = EmailMessage(
        thread=thread,
        mailbox="procurement",
        subject="Счёт",
        from_address="x@y.z",
        to_addresses=["procurement@example.com"],
        received_at=datetime.now(UTC),
        message_id_header=f"<{uuid.uuid4()}@example.com>",
    )
    sync_db.add_all([thread, msg])
    sync_db.flush()

    assert _store_attachment(sync_db, _attachment(), msg.id, "procurement") is None
    sync_db.flush()
    att = sync_db.query(EmailAttachment).filter_by(message_id=msg.id).one()
    assert att.storage_path is None


def test_polling_a_mailbox_queues_recognition_for_its_attachments(sync_db, monkeypatch):
    """The whole point of Ф6.1, end to end through poll_imap_mailbox."""
    import app.storage as storage
    import app.tasks.imap_client as imap_client
    import app.tasks.ingest as ingest
    from app.tasks.extraction import process_document

    monkeypatch.setattr(storage, "upload_file", lambda *a, **k: None)
    sync_db.add(_mailbox("procurement"))
    sync_db.commit()

    parsed = SimpleNamespace(
        message_id=f"<{uuid.uuid4()}@supplier.example>",
        in_reply_to=None,
        from_address="supplier@example.com",
        to_addresses=["procurement@example.com"],
        cc_addresses=[],
        subject="Счёт УТ-2562",
        body_text="во вложении счёт",
        body_html="",
        sent_at=datetime.now(UTC),
        has_attachments=True,
        attachments=[_attachment()],
        body_text_derived=False,
        references=None,
        reply_to=None,
        headers_meta={},
    )
    monkeypatch.setattr(
        imap_client,
        "get_mailbox_configs",
        lambda: [
            imap_client.MailboxConfig(
                name="procurement",
                host="mail.example.com",
                port=993,
                user="procurement",
                password="x",
            )
        ],
    )
    monkeypatch.setattr(imap_client, "fetch_unseen_from_mailbox", lambda cfg: [parsed])
    monkeypatch.setattr(ingest, "_record_sync_result", lambda *a, **k: None)
    monkeypatch.setattr(ingest, "_notify_new_email", lambda *a, **k: None)

    queued: list[str] = []
    monkeypatch.setattr(
        process_document,
        "apply_async",
        lambda args=None, **kw: queued.append(args[0]),
    )

    result = ingest.poll_imap_mailbox("procurement")

    assert result["fetched"] == 1
    assert len(result["documents"]) == 1
    # Before Ф6.1 this list was empty: the Document existed and nothing ever
    # asked for it to be recognised.
    assert result["queued_for_extraction"] == 1
    assert queued == result["documents"]

    doc = sync_db.get(Document, uuid.UUID(result["documents"][0]))
    assert doc.metadata_["auto_verify"] is False  # holds at Needs Review


def test_mailbox_with_automation_off_stores_but_does_not_recognise(sync_db, monkeypatch):
    import app.storage as storage
    import app.tasks.imap_client as imap_client
    import app.tasks.ingest as ingest
    from app.tasks.extraction import process_document

    monkeypatch.setattr(storage, "upload_file", lambda *a, **k: None)
    sync_db.add(_mailbox("archive-box", auto_process_attachments=False))
    sync_db.commit()

    parsed = SimpleNamespace(
        message_id=f"<{uuid.uuid4()}@x.example>",
        in_reply_to=None,
        from_address="x@example.com",
        to_addresses=["archive-box@example.com"],
        cc_addresses=[],
        subject="Счёт",
        body_text="текст",
        body_html="",
        sent_at=datetime.now(UTC),
        has_attachments=True,
        # Distinct bytes: an identical attachment would be de-duplicated onto
        # the Document created by the previous test and never re-queued (which
        # is correct behaviour, but not what this test is about).
        attachments=[_attachment(filename="Другой счёт.pdf", content=b"%PDF-1.4 other")],
        body_text_derived=False,
        references=None,
        reply_to=None,
        headers_meta={},
    )
    monkeypatch.setattr(
        imap_client,
        "get_mailbox_configs",
        lambda: [
            imap_client.MailboxConfig(
                name="archive-box",
                host="m.example.com",
                port=993,
                user="archive-box",
                password="x",
            )
        ],
    )
    monkeypatch.setattr(imap_client, "fetch_unseen_from_mailbox", lambda cfg: [parsed])
    monkeypatch.setattr(ingest, "_record_sync_result", lambda *a, **k: None)
    monkeypatch.setattr(ingest, "_notify_new_email", lambda *a, **k: None)

    queued: list[str] = []
    monkeypatch.setattr(
        process_document, "apply_async", lambda args=None, **kw: queued.append(args[0])
    )

    result = ingest.poll_imap_mailbox("archive-box")
    assert len(result["documents"]) == 1  # still stored and searchable
    assert result["queued_for_extraction"] == 0
    assert queued == []


def test_emailed_pdf_is_accepted_when_the_allowlist_table_is_empty(sync_db):
    """Live-stand finding: `file_extension_allowlist` is empty in production.

    The upload endpoint falls back to a built-in default set; the e-mail path
    did not, so the SAME pdf was accepted when a person uploaded it and
    quarantined when it arrived as an attachment — which silently defeated the
    whole Ф6.1 automation.
    """
    from sqlalchemy import delete as _delete

    from app.db.models import FileExtensionAllowlist
    from app.tasks.ingest import _is_extension_allowed

    sync_db.execute(_delete(FileExtensionAllowlist))
    sync_db.commit()

    assert _is_extension_allowed(sync_db, "Счёт № 102111 от 30 октября 2024 г..pdf") is True
    assert _is_extension_allowed(sync_db, "чертёж.dxf") is True
    assert _is_extension_allowed(sync_db, "script.exe") is False
    assert _is_extension_allowed(sync_db, "no-extension") is False


def test_an_explicit_deny_row_still_wins_over_the_default(sync_db):
    from app.db.models import FileExtensionAllowlist
    from app.tasks.ingest import _is_extension_allowed

    sync_db.add(FileExtensionAllowlist(extension=".pdf", is_allowed=False, added_by="test"))
    sync_db.commit()
    try:
        assert _is_extension_allowed(sync_db, "счёт.pdf") is False
    finally:
        from sqlalchemy import delete as _delete

        sync_db.execute(
            _delete(FileExtensionAllowlist).where(FileExtensionAllowlist.extension == ".pdf")
        )
        sync_db.commit()


def test_the_poll_reads_every_folder_mapped_to_the_inbox(sync_db, monkeypatch):
    """Ф2 — находка с живого стенда: сервер сам раскладывает входящие по
    подпапкам (mail.ru → INBOX/ToMyself), а опрашивалась только настроенная
    imap_folder. Письмо есть на сервере, здесь его нет, ошибки нигде нет."""
    from app.db.models import MailboxConfig, MailboxFolder
    from app.tasks import ingest as ingest_module

    sync_db.add(
        MailboxConfig(
            name="multibox",
            imap_host="m.example.com",
            imap_port=993,
            imap_user="multibox",
            imap_password_encrypted="x",
            imap_ssl=True,
            imap_folder="INBOX",
            is_active=True,
        )
    )
    sync_db.add_all(
        [
            MailboxFolder(
                mailbox="multibox", remote_name="INBOX", local_folder="inbox", sync_enabled=True
            ),
            MailboxFolder(
                mailbox="multibox",
                remote_name="INBOX/ToMyself",
                local_folder="inbox",
                sync_enabled=True,
                last_seen_uid=7,
            ),
            # Отправленные во входящие не подмешиваются.
            MailboxFolder(
                mailbox="multibox", remote_name="Sent", local_folder="sent", sync_enabled=True
            ),
            # Выключенную папку опрашивать нельзя: это решение человека.
            MailboxFolder(
                mailbox="multibox",
                remote_name="INBOX/News",
                local_folder="inbox",
                sync_enabled=False,
            ),
        ]
    )
    sync_db.commit()

    assert ingest_module._inbound_folders("multibox", "INBOX") == [
        "INBOX",
        "INBOX/ToMyself",
    ]

    from app.tasks.imap_client import folder_last_seen_uid

    # Водяной знак — на папке: UID нумеруются внутри папки, и общий на ящик
    # знак, поднятый подпапкой, заставил бы пропустить письма в INBOX.
    assert folder_last_seen_uid("multibox", "INBOX/ToMyself") == 7
    assert folder_last_seen_uid("multibox", "INBOX") == 0
