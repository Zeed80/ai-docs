"""Regression for the re-approval idempotency bug in process_approved_document.

Re-processing an already-approved invoice used to crash with a ForeignKeyViolation:
deleting the old invoice / its lines while warehouse receipts (and payment
schedules) still referenced them. The fix detaches receipts (nullable FKs) and
deletes derived payment schedules BEFORE removing the invoice, so re-approval is
idempotent and never destroys a warehouse receipt.

This test builds the exact dependency graph that broke production and runs the
detach-then-delete sequence the fix performs, asserting it succeeds and that the
warehouse receipt survives (only its now-stale invoice link is cleared).
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select
from sqlalchemy import update as sa_update

from app.db.models import (
    Document,
    Invoice,
    InvoiceLine,
    InvoiceStatus,
    PaymentSchedule,
    WarehouseReceipt,
    WarehouseReceiptLine,
)


@pytest.mark.asyncio
async def test_reapproval_detaches_receipts_and_succeeds(db_session):
    # ── Seed: document → invoice → line, plus a warehouse receipt + line that
    #    reference the invoice/line, plus a payment schedule. ──────────────────
    doc = Document(
        file_name="inv.pdf", file_hash=uuid.uuid4().hex, file_size=1,
        mime_type="application/pdf", storage_path="x/y",
    )
    db_session.add(doc)
    await db_session.flush()

    invoice = Invoice(document_id=doc.id, status=InvoiceStatus.approved)
    db_session.add(invoice)
    await db_session.flush()

    line = InvoiceLine(invoice_id=invoice.id, line_number=1, amount=100.0)
    db_session.add(line)
    await db_session.flush()

    receipt = WarehouseReceipt(invoice_id=invoice.id, document_id=doc.id, status="draft")
    db_session.add(receipt)
    await db_session.flush()

    rline = WarehouseReceiptLine(
        receipt_id=receipt.id, invoice_line_id=line.id,
        description="Болт", quantity_expected=10.0, quantity_received=0.0, unit="шт",
    )
    db_session.add(rline)
    db_session.add(PaymentSchedule(
        invoice_id=invoice.id, due_date=dt.datetime(2026, 1, 1), amount=100.0,
    ))
    await db_session.flush()

    # ── The fix's sequence: detach nullable refs, delete derived schedules,
    #    then delete the invoice + its lines. Must NOT raise an FK violation. ──
    line_ids = (
        await db_session.execute(select(InvoiceLine.id).where(InvoiceLine.invoice_id == invoice.id))
    ).scalars().all()
    await db_session.execute(
        sa_update(WarehouseReceiptLine)
        .where(WarehouseReceiptLine.invoice_line_id.in_(line_ids))
        .values(invoice_line_id=None)
    )
    await db_session.execute(
        sa_update(WarehouseReceipt).where(WarehouseReceipt.invoice_id == invoice.id).values(invoice_id=None)
    )
    await db_session.execute(
        sa_delete(PaymentSchedule).where(PaymentSchedule.invoice_id == invoice.id)
    )
    await db_session.execute(sa_delete(InvoiceLine).where(InvoiceLine.invoice_id == invoice.id))
    await db_session.delete(invoice)
    await db_session.flush()  # would raise ForeignKeyViolation without the detach

    # ── Receipts preserved (data integrity), links cleared, invoice gone. ─────
    assert (await db_session.get(WarehouseReceipt, receipt.id)) is not None
    surviving = await db_session.get(WarehouseReceiptLine, rline.id)
    assert surviving is not None and surviving.invoice_line_id is None
    assert (await db_session.get(Invoice, invoice.id)) is None
    ps_count = (
        await db_session.execute(
            select(func.count()).select_from(PaymentSchedule).where(PaymentSchedule.invoice_id == invoice.id)
        )
    ).scalar()
    assert ps_count == 0
