"""Extraction API tests — doc.classify, doc.extract, doc.correct_field"""

import sys
import types
import uuid
from unittest.mock import MagicMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Document,
    DocumentExtraction,
    DocumentStatus,
    ExtractionField,
    ConfidenceReason,
)

# Ensure app.tasks.extraction module exists for patching (Celery autodiscover may fail in test env)
if "app.tasks.extraction" not in sys.modules:
    _mock_mod = types.ModuleType("app.tasks.extraction")
    _mock_mod.classify_document = MagicMock()
    _mock_mod.process_document = MagicMock()
    _mock_mod.extract_invoice = MagicMock()
    sys.modules["app.tasks.extraction"] = _mock_mod


async def _create_doc(db: AsyncSession, status=DocumentStatus.ingested) -> uuid.UUID:
    doc = Document(
        file_name="extract_test.pdf",
        file_hash="extracthash123",
        file_size=2048,
        mime_type="application/pdf",
        storage_path="documents/ex/tr/extracthash123",
        status=status,
    )
    db.add(doc)
    await db.commit()
    return doc.id


@pytest.mark.asyncio
async def test_classify_document(client: AsyncClient, db_session: AsyncSession):
    """doc.classify — triggers Celery task."""
    doc_id = await _create_doc(db_session)

    mock_task = MagicMock()
    mock_task.id = "test-task-id-123"

    with patch("app.tasks.extraction.classify_document.delay", return_value=mock_task):
        resp = await client.post(f"/api/documents/{doc_id}/classify")

    assert resp.status_code == 200
    data = resp.json()
    assert data["task_id"] == "test-task-id-123"
    assert data["status"] == "queued"


@pytest.mark.asyncio
async def test_classify_wrong_status(client: AsyncClient, db_session: AsyncSession):
    """doc.classify — cannot classify in extracting status."""
    doc_id = await _create_doc(db_session, status=DocumentStatus.extracting)

    resp = await client.post(f"/api/documents/{doc_id}/classify")
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_extract_document(client: AsyncClient, db_session: AsyncSession):
    """doc.extract — triggers full pipeline."""
    doc_id = await _create_doc(db_session)

    mock_task = MagicMock()
    mock_task.id = "extract-task-456"

    with patch("app.tasks.extraction.process_document.delay", return_value=mock_task):
        resp = await client.post(f"/api/documents/{doc_id}/extract")

    assert resp.status_code == 200
    data = resp.json()
    assert data["task_id"] == "extract-task-456"


@pytest.mark.asyncio
async def test_correct_field(client: AsyncClient, db_session: AsyncSession):
    """doc.correct_field — human correction of extracted field."""
    doc_id = await _create_doc(db_session)

    # Create extraction + field
    extraction = DocumentExtraction(
        document_id=doc_id,
        model_name="gemma4:e4b",
        overall_confidence=0.85,
    )
    db_session.add(extraction)
    await db_session.flush()

    field = ExtractionField(
        extraction_id=extraction.id,
        field_name="invoice_number",
        field_value="INV-001",
        confidence=0.9,
        confidence_reason=ConfidenceReason.high_quality_ocr,
    )
    db_session.add(field)
    await db_session.commit()

    resp = await client.post(
        f"/api/documents/{doc_id}/correct-field",
        json={"field_name": "invoice_number", "corrected_value": "INV-001A"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["old_value"] == "INV-001"
    assert data["corrected_value"] == "INV-001A"


@pytest.mark.asyncio
async def test_correct_field_not_found(client: AsyncClient, db_session: AsyncSession):
    """doc.correct_field — 404 when no extraction exists."""
    doc_id = await _create_doc(db_session)

    resp = await client.post(
        f"/api/documents/{doc_id}/correct-field",
        json={"field_name": "invoice_number", "corrected_value": "X"},
    )
    assert resp.status_code == 404
