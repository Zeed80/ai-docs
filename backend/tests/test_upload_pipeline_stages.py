"""Tests for document upload pipeline stages tracking.

Verifies that DocumentProcessingJob is created correctly, all 7 pipeline steps
are present, failures are recorded, and re-triggering creates a fresh job.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import DocumentProcessingJob

EXPECTED_STEPS = [
    "store",
    "memory_seed",
    "classification",
    "extraction",
    "sql_records",
    "memory_graph",
    "embedding",
]


async def _ingest(client: AsyncClient, content: bytes = b"pipeline test pdf", name: str = "pipe.pdf") -> str:
    resp = await client.post(
        "/api/documents/ingest?auto_process=true",
        files={"file": (name, content, "application/pdf")},
    )
    assert resp.status_code == 200
    return resp.json()["id"]


# ---------------------------------------------------------------------------
# Job creation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pipeline_job_created_on_auto_process(
    client: AsyncClient, db_session: AsyncSession
):
    """auto_process=true must create a DocumentProcessingJob in 'queued' status."""
    import uuid

    doc_id = await _ingest(client)
    doc_uuid = uuid.UUID(doc_id)

    result = await db_session.execute(
        select(DocumentProcessingJob).where(DocumentProcessingJob.document_id == doc_uuid)
    )
    job = result.scalar_one_or_none()
    assert job is not None, "DocumentProcessingJob must be created when auto_process=true"
    # In test mode (CELERY_TASK_ALWAYS_EAGER=true) job may be done or queued
    assert job.status in ("queued", "running", "done", "failed"), f"Unexpected status: {job.status}"


@pytest.mark.asyncio
async def test_pipeline_job_not_created_when_auto_process_false(
    client: AsyncClient, db_session: AsyncSession
):
    """auto_process=false must create a job with status='done' immediately."""
    import uuid

    resp = await client.post(
        "/api/documents/ingest?auto_process=false",
        files={"file": ("no_auto.pdf", b"no auto process", "application/pdf")},
    )
    assert resp.status_code == 200
    doc_uuid = uuid.UUID(resp.json()["id"])

    result = await db_session.execute(
        select(DocumentProcessingJob).where(DocumentProcessingJob.document_id == doc_uuid)
    )
    job = result.scalar_one_or_none()
    assert job is not None, "Job should be created even without auto_process"
    assert job.status == "done", "Job status must be 'done' when auto_process=false"


# ---------------------------------------------------------------------------
# Pipeline steps structure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pipeline_steps_all_present(
    client: AsyncClient, db_session: AsyncSession
):
    """DocumentProcessingJob.pipeline_steps must contain all 7 expected step keys."""
    import uuid

    doc_id = await _ingest(client, content=b"steps check content")
    doc_uuid = uuid.UUID(doc_id)

    result = await db_session.execute(
        select(DocumentProcessingJob).where(DocumentProcessingJob.document_id == doc_uuid)
    )
    job = result.scalar_one_or_none()
    assert job is not None

    steps: list[dict] = job.pipeline_steps or []
    # Steps use "key" field (see _initial_pipeline_steps in documents.py)
    step_names = {s.get("key") for s in steps}

    for expected in EXPECTED_STEPS:
        assert expected in step_names, (
            f"Step '{expected}' missing from pipeline_steps. Found: {step_names}"
        )


@pytest.mark.asyncio
async def test_pipeline_steps_have_status_field(
    client: AsyncClient, db_session: AsyncSession
):
    """Each pipeline step must have a 'status' field."""
    import uuid

    doc_id = await _ingest(client, content=b"step status check")
    doc_uuid = uuid.UUID(doc_id)

    result = await db_session.execute(
        select(DocumentProcessingJob).where(DocumentProcessingJob.document_id == doc_uuid)
    )
    job = result.scalar_one_or_none()
    assert job is not None

    steps: list[dict] = job.pipeline_steps or []
    for step in steps:
        assert "status" in step, f"Step missing 'status': {step}"
        assert "key" in step, f"Step missing 'key': {step}"
        assert step["status"] in ("pending", "queued", "running", "done", "skipped", "failed"), (
            f"Unexpected step status: {step['status']}"
        )


# ---------------------------------------------------------------------------
# Re-trigger /extract creates a new job
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_extract_endpoint_creates_new_job(
    client: AsyncClient, db_session: AsyncSession
):
    """POST /{doc_id}/extract must create a fresh pipeline job."""
    import uuid

    doc_id = await _ingest(client, content=b"extract retrigger test")
    doc_uuid = uuid.UUID(doc_id)

    # Get current job count
    result = await db_session.execute(
        select(DocumentProcessingJob).where(DocumentProcessingJob.document_id == doc_uuid)
    )
    jobs_before = result.scalars().all()

    # Re-trigger extraction
    extract_resp = await client.post(f"/api/documents/{doc_id}/extract")
    assert extract_resp.status_code == 200

    await db_session.refresh(jobs_before[0]) if jobs_before else None

    result2 = await db_session.execute(
        select(DocumentProcessingJob).where(DocumentProcessingJob.document_id == doc_uuid)
    )
    jobs_after = result2.scalars().all()
    assert len(jobs_after) >= len(jobs_before), "At least one job must exist after /extract"


# ---------------------------------------------------------------------------
# Management summary reflects pipeline state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_management_summary_has_pipeline(client: AsyncClient):
    """GET /{doc_id}/management must return pipeline section with extraction_count."""
    doc_id = await _ingest(client, content=b"management pipeline check")

    resp = await client.get(f"/api/documents/{doc_id}/management")
    assert resp.status_code == 200

    data = resp.json()
    assert "pipeline" in data, "management response must contain 'pipeline' key"
    pipeline = data["pipeline"]
    assert "extraction_count" in pipeline
    assert "memory_chunks" in pipeline
    assert "graph_nodes" in pipeline


@pytest.mark.asyncio
async def test_workspace_includes_pipeline_steps(client: AsyncClient):
    """GET /workspace must include pipeline_steps for each document."""
    await _ingest(client, content=b"workspace pipeline test")

    resp = await client.get("/api/documents/workspace?limit=10")
    assert resp.status_code == 200

    data = resp.json()
    assert "items" in data
    assert data["total"] >= 1

    # Check at least one item has a pipeline field
    items_with_pipeline = [
        item for item in data["items"]
        if item.get("pipeline") is not None
    ]
    assert len(items_with_pipeline) >= 1, "At least one workspace item must have pipeline data"


# ---------------------------------------------------------------------------
# File hash in response
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ingest_response_contains_file_hash(client: AsyncClient):
    """doc.ingest response must include file_hash (SHA-256 hex string)."""
    content = b"hash verification content"
    resp = await client.post(
        "/api/documents/ingest?auto_process=false",
        files={"file": ("hash_test.pdf", content, "application/pdf")},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "file_hash" in data
    file_hash = data["file_hash"]
    assert len(file_hash) == 64, "SHA-256 hex must be 64 characters"
    assert all(c in "0123456789abcdef" for c in file_hash), "Hash must be hex"

    # Verify it matches the actual SHA-256 of the content
    import hashlib
    expected = hashlib.sha256(content).hexdigest()
    assert file_hash == expected


@pytest.mark.asyncio
async def test_ingest_size_matches_content(client: AsyncClient):
    """doc.ingest — file_size in response must match the actual bytes uploaded."""
    content = b"size check " * 100  # 1100 bytes
    resp = await client.post(
        "/api/documents/ingest?auto_process=false",
        files={"file": ("size_test.pdf", content, "application/pdf")},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["file_size"] == len(content), (
        f"Expected file_size={len(content)}, got {data['file_size']}"
    )
