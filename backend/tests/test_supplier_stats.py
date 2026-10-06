"""SupplierProfile counters are recounted from invoices, not incremented."""

import pytest

from app.db.models import (
    Document,
    DocumentStatus,
    Invoice,
    InvoiceStatus,
    Party,
    PartyRole,
    SupplierProfile,
)
from app.domain.supplier_stats import recompute_supplier_profile


@pytest.mark.asyncio
async def test_recount_replaces_an_inflated_counter_and_is_idempotent(db_session):
    party = Party(name="ООО Счёт", inn="7700000001", role=PartyRole.supplier)
    db_session.add(party)
    await db_session.flush()
    # Live shape: double counting left 166 for a supplier with 33 invoices.
    profile = SupplierProfile(party_id=party.id, total_invoices=166, total_amount=9e9)
    db_session.add(profile)
    for number, (status, amount) in enumerate(
        [
            (InvoiceStatus.approved, 100.0),
            (InvoiceStatus.paid, 50.0),
            (InvoiceStatus.rejected, 1000.0),
            (InvoiceStatus.needs_review, 7.0),
        ]
    ):
        document = Document(
            file_name=f"s{number}.pdf",
            file_hash=f"stats-{number}",
            file_size=1,
            mime_type="application/pdf",
            storage_path=f"t/s{number}.pdf",
            status=DocumentStatus.approved,
        )
        db_session.add(document)
        await db_session.flush()
        db_session.add(
            Invoice(
                document_id=document.id,
                invoice_number=f"N-{number}",
                currency="RUB",
                total_amount=amount,
                status=status,
                supplier_id=party.id,
            )
        )
    await db_session.flush()

    await recompute_supplier_profile(db_session, party.id)
    await recompute_supplier_profile(db_session, party.id)  # approving twice

    assert profile.total_invoices == 2  # approved + paid only
    assert profile.total_amount == 150.0
