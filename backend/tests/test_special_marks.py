"""Tests for notes/special_marks field separation.

notes  — user-only field, never written by AI extraction.
special_marks — AI-extracted free text (delivery conditions, payment terms, etc.).

Verifications:
- Invoice model accepts both fields independently
- PATCH /api/invoices/{id} sets notes without touching special_marks
- Re-patching notes does not clear special_marks
- AI extraction mapping writes special_marks, not notes
- auto_process=False job shows embedding step (not immediately done)
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Document,
    DocumentProcessingJob,
    DocumentStatus,
    Invoice,
    InvoiceStatus,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _create_invoice(
    db: AsyncSession,
    *,
    notes: str | None = None,
    special_marks: str | None = None,
) -> tuple[uuid.UUID, uuid.UUID]:
    doc = Document(
        file_name="sm_test.pdf",
        file_hash=f"sm{uuid.uuid4().hex}",
        file_size=512,
        mime_type="application/pdf",
        storage_path="documents/sm/test",
        status=DocumentStatus.needs_review,
    )
    db.add(doc)
    await db.flush()

    invoice = Invoice(
        document_id=doc.id,
        invoice_number="SM-001",
        currency="RUB",
        total_amount=5000.0,
        status=InvoiceStatus.needs_review,
        notes=notes,
        special_marks=special_marks,
    )
    db.add(invoice)
    await db.commit()
    return doc.id, invoice.id


# ---------------------------------------------------------------------------
# DB-level field isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_notes_and_special_marks_are_independent(db_session: AsyncSession):
    """notes and special_marks can be set/read independently."""
    _, inv_id = await _create_invoice(
        db_session,
        notes="Пользовательское примечание",
        special_marks="Доставка в течение 5 дней",
    )

    from sqlalchemy import select

    result = await db_session.execute(select(Invoice).where(Invoice.id == inv_id))
    inv = result.scalar_one()
    assert inv.notes == "Пользовательское примечание"
    assert inv.special_marks == "Доставка в течение 5 дней"


@pytest.mark.asyncio
async def test_invoice_can_have_special_marks_without_notes(db_session: AsyncSession):
    """AI sets special_marks; notes stays None until user writes."""
    _, inv_id = await _create_invoice(
        db_session,
        notes=None,
        special_marks="Особые условия: оплата в течение 30 дней",
    )

    from sqlalchemy import select

    result = await db_session.execute(select(Invoice).where(Invoice.id == inv_id))
    inv = result.scalar_one()
    assert inv.notes is None
    assert inv.special_marks is not None


# ---------------------------------------------------------------------------
# API: PATCH notes does not touch special_marks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_patch_notes_preserves_special_marks(
    client: AsyncClient, db_session: AsyncSession
):
    """PATCH /api/invoices/{id} with notes must not overwrite special_marks."""
    _, inv_id = await _create_invoice(
        db_session,
        notes=None,
        special_marks="Срочная поставка",
    )

    resp = await client.patch(
        f"/api/invoices/{inv_id}",
        json={"notes": "Мой комментарий"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["notes"] == "Мой комментарий"
    assert data["special_marks"] == "Срочная поставка", (
        "PATCH notes must not erase special_marks"
    )


@pytest.mark.asyncio
async def test_patch_notes_twice_preserves_special_marks(
    client: AsyncClient, db_session: AsyncSession
):
    """Re-patching notes still keeps special_marks intact."""
    _, inv_id = await _create_invoice(
        db_session,
        notes="первое",
        special_marks="особые условия из счёта",
    )

    resp = await client.patch(f"/api/invoices/{inv_id}", json={"notes": "второе"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["notes"] == "второе"
    assert data["special_marks"] == "особые условия из счёта"


@pytest.mark.asyncio
async def test_patch_special_marks_does_not_set_notes(
    client: AsyncClient, db_session: AsyncSession
):
    """PATCH special_marks should not accidentally set notes."""
    _, inv_id = await _create_invoice(db_session, notes=None, special_marks=None)

    resp = await client.patch(
        f"/api/invoices/{inv_id}",
        json={"special_marks": "Условия доставки: EXW"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["special_marks"] == "Условия доставки: EXW"
    assert data["notes"] is None, "notes must remain None when only special_marks is patched"


# ---------------------------------------------------------------------------
# Extraction mapping: AI uses special_marks, not notes
# ---------------------------------------------------------------------------


def test_extraction_mapping_uses_special_marks_not_notes():
    """AI extraction output maps to special_marks, never to notes."""
    from app.ai.extraction_prompts import EXTRACT_INVOICE_PROMPT

    assert "special_marks" in EXTRACT_INVOICE_PROMPT, (
        "Extraction prompt must contain 'special_marks' field"
    )
    assert '"notes"' not in EXTRACT_INVOICE_PROMPT or "user" in EXTRACT_INVOICE_PROMPT.lower(), (
        "Extraction prompt must not instruct AI to write 'notes' without 'user' context"
    )


def test_extraction_task_maps_special_marks(monkeypatch):
    """extraction.py writes extracted['special_marks'] to Invoice.special_marks (not notes)."""
    import inspect
    import app.tasks.extraction as ext_module

    src = inspect.getsource(ext_module)
    assert "special_marks=extracted.get(\"special_marks\")" in src, (
        "extraction.py must map extracted['special_marks'] → Invoice.special_marks"
    )
    assert "notes=extracted.get(" not in src, (
        "extraction.py must not map any extracted field to Invoice.notes"
    )


# ---------------------------------------------------------------------------
# Pipeline: auto_process=False creates embedding job, not immediately done
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auto_process_false_creates_embedding_job(
    client: AsyncClient, db_session: AsyncSession
):
    """auto_process=False must create a job targeting the embedding step."""
    from sqlalchemy import select

    resp = await client.post(
        "/api/documents/ingest?auto_process=false",
        files={"file": ("sm_no_proc.pdf", b"special marks pipeline test", "application/pdf")},
    )
    assert resp.status_code == 200
    doc_id = uuid.UUID(resp.json()["id"])

    result = await db_session.execute(
        select(DocumentProcessingJob).where(
            DocumentProcessingJob.document_id == doc_id
        )
    )
    job = result.scalar_one_or_none()
    assert job is not None

    steps: dict[str, str] = {s["key"]: s["status"] for s in (job.pipeline_steps or [])}

    assert steps.get("store") == "done", "store step must always be done"
    assert steps.get("embedding") in ("queued", "running", "done"), (
        "embedding step must be active when auto_process=False"
    )
    for skip_key in ("classification", "extraction", "sql_records", "memory_graph"):
        assert steps.get(skip_key) == "skipped", (
            f"Step '{skip_key}' must be skipped when auto_process=False"
        )
