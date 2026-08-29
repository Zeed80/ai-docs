"""Ф6.2 — the supplier is also identifiable from who sent the letter.

``auto_supplier_task`` carries a five-step matching ladder (INN → e-mail in the
document → phone → LLM name match → create) and was called from nowhere at all.
Meanwhile the strongest signal for an emailed invoice — the sender address —
was not consulted anywhere in the system.
"""

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, delete
from sqlalchemy.orm import Session

from app.db.models import (
    Document,
    DocumentExtraction,
    DocumentStatus,
    EmailMessage,
    EmailThread,
    Invoice,
    InvoiceStatus,
    Party,
    PartyRole,
)


def _cleanup_since(engine, started):
    """Remove exactly what a test created, children first.

    Hand-ordered because these rows are committed outside the suite's per-test
    transaction: a leftover child (DocumentLink, EmailAttachment) makes the
    NEXT fixture's cleanup die on a foreign key, which surfaces as an
    unrelated test erroring at setup.
    """
    from sqlalchemy import select as _select

    from app.db.models import (
        AnomalyCard, Document, DocumentExtraction, DocumentLink, EmailAttachment,
        EmailMessage, EmailThread, Invoice, MailboxConfig, Party, QuarantineEntry,
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
        # Approving an invoice fires check_invoice_anomalies, which in tests
        # runs eagerly and leaves AnomalyCard rows behind — they leaked into
        # test_spec_tables::test_anomalies_source.
        invoice_ids = _select(Invoice.id).where(Invoice.document_id.in_(doc_ids))
        db.execute(delete(AnomalyCard).where(
            AnomalyCard.entity_id.in_(invoice_ids)
        ))
        db.execute(delete(AnomalyCard).where(AnomalyCard.created_at >= started))
        db.execute(delete(Invoice).where(Invoice.document_id.in_(doc_ids)))
        db.execute(delete(DocumentExtraction).where(DocumentExtraction.document_id.in_(doc_ids)))
        db.execute(delete(Document).where(Document.id.in_(doc_ids)))
        for model in (EmailMessage, EmailThread, MailboxConfig, Party):
            db.execute(delete(model).where(model.created_at >= started))
        db.commit()


@pytest.fixture
def sync_db(test_engine, monkeypatch):
    """Sync session that really commits — auto_supplier_task opens its own.

    Teardown deletes ONLY what this test created (rows newer than the fixture's
    start). A blanket ``delete(Party)``/``delete(Document)`` here reaches
    outside the per-test transaction the rest of the suite relies on and takes
    other tests' data with it — which is exactly what it did before this
    comment existed.
    """
    sync_url = test_engine.url.render_as_string(hide_password=False).replace(
        "postgresql+asyncpg://", "postgresql://"
    )
    engine = create_engine(sync_url, pool_pre_ping=True)
    import app.db.sync_session as sync_module

    monkeypatch.setattr(sync_module, "sync_session", lambda: Session(engine))
    monkeypatch.setattr("app.tasks.extraction._get_sync_session", lambda: Session(engine))

    started = datetime.now(timezone.utc)
    try:
        with Session(engine) as db:
            yield db
    finally:
        _cleanup_since(engine, started)
        engine.dispose()


def _emailed_invoice(db, sender: str, *, supplier_data: dict | None = None):
    thread = EmailThread(subject="Счёт", mailbox="procurement", message_count=1)
    msg = EmailMessage(
        thread=thread, mailbox="procurement", subject="Счёт",
        from_address=sender, to_addresses=["procurement@example.com"],
        received_at=datetime.now(timezone.utc),
        message_id_header=f"<{uuid.uuid4()}@example.com>",
    )
    db.add_all([thread, msg])
    db.flush()

    doc = Document(
        file_name="Счёт.pdf", file_hash=uuid.uuid4().hex, mime_type="application/pdf",
        file_size=10, storage_path="documents/x", source_channel="email",
        source_email_id=msg.id, status=DocumentStatus.approved,
    )
    db.add(doc)
    db.flush()
    db.add(DocumentExtraction(
        document_id=doc.id, model_name="test",
        structured_data={"supplier": supplier_data or {}},
    ))
    db.add(Invoice(
        document_id=doc.id, invoice_number="УТ-1", status=InvoiceStatus.approved,
        total_amount=1000,
    ))
    db.commit()
    return doc


def test_supplier_is_found_by_the_sender_address(sync_db):
    from app.tasks.extraction import auto_supplier_task

    party = Party(name="ООО Ромекс", role=PartyRole.supplier,
                  contact_email="sales@romex.example")
    sync_db.add(party)
    sync_db.commit()
    party_id = party.id

    # The scan carries neither name nor INN — exactly the case that used to end
    # with an invoice that had no supplier at all.
    doc = _emailed_invoice(sync_db, "Отдел продаж <sales@romex.example>", supplier_data={})

    result = auto_supplier_task.apply(args=[str(doc.id)]).get()
    assert result["party_id"] == str(party_id)
    assert result["matched_by"] == "email_sender_exact"

    inv = sync_db.execute(
        Invoice.__table__.select().where(Invoice.document_id == doc.id)
    ).first()
    assert inv.supplier_id == party_id


def test_a_unique_sender_domain_also_identifies_the_supplier(sync_db):
    from app.tasks.extraction import auto_supplier_task

    party = Party(name="ООО Ромекс", role=PartyRole.supplier,
                  contact_email="info@romex.example")
    sync_db.add(party)
    sync_db.commit()
    party_id = party.id

    doc = _emailed_invoice(sync_db, "manager@romex.example", supplier_data={})
    result = auto_supplier_task.apply(args=[str(doc.id)]).get()
    assert result["party_id"] == str(party_id)
    assert result["matched_by"] == "email_sender_domain"


def test_a_shared_domain_never_binds_an_invoice_to_a_random_supplier(sync_db):
    """mail.ru/gmail.com identify nobody — matching the first row there would
    attach an invoice to the wrong company."""
    from app.tasks.extraction import auto_supplier_task

    sync_db.add_all([
        Party(name="ООО Первый", role=PartyRole.supplier, contact_email="a@mail.ru"),
        Party(name="ООО Второй", role=PartyRole.supplier, contact_email="b@mail.ru"),
    ])
    sync_db.commit()

    doc = _emailed_invoice(sync_db, "c@mail.ru", supplier_data={})
    result = auto_supplier_task.apply(args=[str(doc.id)]).get()
    assert result.get("error") == "no_supplier_data"


def test_document_inn_still_wins_over_the_envelope(sync_db):
    """The envelope is a fallback, not an override: a letter forwarded by a
    third party must not re-label the invoice as theirs."""
    from app.tasks.extraction import auto_supplier_task

    real = Party(name="ООО Ромекс", role=PartyRole.supplier, inn="7701234567")
    forwarder = Party(name="ООО Посредник", role=PartyRole.supplier,
                      contact_email="buh@middleman.example")
    sync_db.add_all([real, forwarder])
    sync_db.commit()
    real_id = real.id

    doc = _emailed_invoice(
        sync_db, "buh@middleman.example",
        supplier_data={"name": "ООО Ромекс", "inn": "7701234567"},
    )
    result = auto_supplier_task.apply(args=[str(doc.id)]).get()
    assert result["party_id"] == str(real_id)
    assert result["matched_by"] == "email_sender_exact" or result["matched_by"] == "inn"


# ── Ф6.3: письмо ↔ счёт видны в обе стороны ────────────────────────────────


async def test_invoice_reports_the_letter_it_came_from(client, db_session):
    """`Document.source_email_id` existed and was returned by no endpoint, so
    the invoice screen could not say "пришёл письмом от X"."""
    from app.db.models import (
        Document as D, DocumentStatus as DS, EmailMessage as EM,
        EmailThread as ET, Invoice as I, InvoiceStatus as IS,
    )

    thread = ET(subject="Счёт от Ромекс", mailbox="procurement", message_count=1)
    msg = EM(
        thread=thread, mailbox="procurement", subject="Счёт от Ромекс",
        from_address="sales@romex.example", to_addresses=["procurement@example.com"],
        received_at=datetime.now(timezone.utc),
        message_id_header=f"<{uuid.uuid4()}@romex.example>",
        headers_meta={"auth": {"spf": "pass", "dkim": "pass"}},
    )
    db_session.add_all([thread, msg])
    await db_session.flush()
    doc = D(
        file_name="Счёт.pdf", file_hash=uuid.uuid4().hex, mime_type="application/pdf",
        file_size=10, storage_path="documents/x", source_channel="email",
        source_email_id=msg.id, status=DS.approved,
    )
    db_session.add(doc)
    await db_session.flush()
    inv = I(
        document_id=doc.id, invoice_number="УТ-2562", status=IS.approved,
        total_amount=240000, metadata_={"supplier_matched_by": "email_sender_exact"},
    )
    db_session.add(inv)
    await db_session.commit()

    resp = await client.get(f"/api/invoices/{inv.id}")
    assert resp.status_code == 200, resp.text
    src = resp.json()["email_source"]
    assert src["from_address"] == "sales@romex.example"
    assert src["mailbox"] == "procurement"
    assert src["thread_id"] == str(thread.id)
    assert src["spf_dkim_ok"] is True
    assert resp.json()["supplier_matched_by"] == "email_sender_exact"


async def test_thread_shows_what_was_created_from_it(client, db_session):
    from app.db.models import (
        Document as D, DocumentStatus as DS, EmailMessage as EM,
        EmailThread as ET, Invoice as I, InvoiceStatus as IS, MailboxConfig as MC,
    )

    db_session.add(MC(
        name="procurement", display_name="Закупки", imap_host="m.example.com",
        imap_port=993, imap_user="procurement", imap_password_encrypted="x",
        imap_ssl=True, is_active=True,
    ))
    thread = ET(subject="Счёт", mailbox="procurement", message_count=1)
    msg = EM(
        thread=thread, mailbox="procurement", subject="Счёт",
        from_address="sales@romex.example", to_addresses=["procurement@example.com"],
        received_at=datetime.now(timezone.utc),
        message_id_header=f"<{uuid.uuid4()}@romex.example>",
    )
    db_session.add_all([thread, msg])
    await db_session.flush()
    doc = D(
        file_name="Счёт.pdf", file_hash=uuid.uuid4().hex, mime_type="application/pdf",
        file_size=10, storage_path="documents/x", source_channel="email",
        source_email_id=msg.id, status=DS.needs_review,
    )
    db_session.add(doc)
    await db_session.flush()
    db_session.add(I(
        document_id=doc.id, invoice_number="УТ-2562", status=IS.needs_review,
        total_amount=240000, metadata_={"supplier_matched_by": "email_sender_domain"},
    ))
    await db_session.commit()

    resp = await client.get(f"/api/email/threads/{thread.id}")
    assert resp.status_code == 200, resp.text
    derived = resp.json()["messages"][0]["derived_invoices"]
    assert len(derived) == 1
    assert derived[0]["invoice_number"] == "УТ-2562"
    assert derived[0]["total_amount"] == 240000
    assert derived[0]["supplier_matched_by"] == "email_sender_domain"


async def test_unknown_authentication_is_not_reported_as_pass(client, db_session):
    from app.db.models import (
        Document as D, DocumentStatus as DS, EmailMessage as EM,
        EmailThread as ET, Invoice as I, InvoiceStatus as IS,
    )

    thread = ET(subject="Без заголовков", mailbox="procurement", message_count=1)
    msg = EM(
        thread=thread, mailbox="procurement", subject="Без заголовков",
        from_address="x@y.example", to_addresses=["procurement@example.com"],
        received_at=datetime.now(timezone.utc),
        message_id_header=f"<{uuid.uuid4()}@y.example>",
    )
    db_session.add_all([thread, msg])
    await db_session.flush()
    doc = D(
        file_name="a.pdf", file_hash=uuid.uuid4().hex, mime_type="application/pdf",
        file_size=1, storage_path="documents/y", source_channel="email",
        source_email_id=msg.id, status=DS.approved,
    )
    db_session.add(doc)
    await db_session.flush()
    inv = I(document_id=doc.id, invoice_number="Б/Н", status=IS.approved, total_amount=1)
    db_session.add(inv)
    await db_session.commit()

    resp = await client.get(f"/api/invoices/{inv.id}")
    assert resp.json()["email_source"]["spf_dkim_ok"] is None
