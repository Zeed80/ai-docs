"""SupplierProfile counters recomputed from invoices, never incremented.

Both invoice approval (api/invoices) and approved-document processing
(tasks/extraction) did ``total_invoices += 1``; reprocessing added more.
Live 2026-10-06 the profile said 166 invoices for a supplier with 33, and
the agent quoted the inflated figure. A recount is idempotent and always
matches the invoices table.
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select

from app.db.models import Invoice, InvoiceStatus, SupplierProfile

_COUNTED = (InvoiceStatus.approved, InvoiceStatus.paid)


def _stats_query(party_id: uuid.UUID):
    return select(
        func.count(Invoice.id),
        func.coalesce(func.sum(Invoice.total_amount), 0.0),
        func.max(func.coalesce(Invoice.invoice_date, Invoice.created_at)),
    ).where(Invoice.supplier_id == party_id, Invoice.status.in_(_COUNTED))


def _apply(profile: SupplierProfile, row) -> None:
    count, amount, last = row
    profile.total_invoices = int(count or 0)
    profile.total_amount = float(amount or 0.0)
    if last is not None:
        profile.last_invoice_date = last


async def recompute_supplier_profile(db, party_id: uuid.UUID) -> SupplierProfile | None:
    profile = await db.scalar(select(SupplierProfile).where(SupplierProfile.party_id == party_id))
    if profile is None:
        return None
    await db.flush()
    _apply(profile, (await db.execute(_stats_query(party_id))).one())
    return profile


def recompute_supplier_profile_sync(db, party_id: uuid.UUID) -> SupplierProfile | None:
    profile = db.execute(
        select(SupplierProfile).where(SupplierProfile.party_id == party_id)
    ).scalar_one_or_none()
    if profile is None:
        return None
    db.flush()
    _apply(profile, db.execute(_stats_query(party_id)).one())
    return profile
