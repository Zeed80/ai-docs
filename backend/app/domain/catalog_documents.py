"""Supplier catalog files as first-class Documents.

Before this, a catalog was an anonymous MinIO object: no dedup, no antivirus,
no size history, no progress, and no way to answer "which file did this price
come from" (ToolCatalogEntry.source_document_id existed but was never written).
Registering the upload as a Document with two DocumentLinks (party and
tool_supplier) gets all of that from machinery that already exists, and gives
the ingestion task a DocumentProcessingJob to report stages into.
"""

from __future__ import annotations

import hashlib
import mimetypes
import uuid
from dataclasses import dataclass

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Document,
    DocumentLink,
    DocumentProcessingJob,
    DocumentStatus,
    DocumentType,
    ToolSupplier,
)
from app.domain.pipeline import CATALOG_PIPELINE_STEP_DEFINITIONS

logger = structlog.get_logger()

CATALOG_LINK_TYPE = "supplier_catalog"
ARCHIVE_SUFFIXES = {".zip", ".7z", ".rar", ".tar", ".gz", ".tgz", ".bz2", ".xz"}
ARCHIVE_MEMBER_LINK_TYPE = "archive_member"


@dataclass
class RegisteredCatalog:
    document: Document
    job: DocumentProcessingJob
    is_duplicate: bool


def catalog_storage_path(supplier_id: str | uuid.UUID, digest: str, filename: str) -> str:
    return f"tool-catalogs/{supplier_id}/{digest[:2]}/{digest}/{filename}"


def _catalog_job_steps(*, store_done: bool = True) -> list[dict]:
    steps = [
        {"key": key, "label": label, "status": "pending"}
        for key, label in CATALOG_PIPELINE_STEP_DEFINITIONS
    ]
    if store_done:
        steps[0]["status"] = "done"
    return steps


async def find_catalog_document(
    db: AsyncSession, supplier_id: uuid.UUID, file_hash: str
) -> Document | None:
    """A previously uploaded catalog with the same bytes, for the same supplier."""
    result = await db.execute(
        select(Document)
        .join(DocumentLink, DocumentLink.document_id == Document.id)
        .where(
            Document.file_hash == file_hash,
            DocumentLink.linked_entity_type == "tool_supplier",
            DocumentLink.linked_entity_id == supplier_id,
            DocumentLink.link_type == CATALOG_LINK_TYPE,
        )
        .limit(1)
    )
    return result.scalar_one_or_none()


async def register_catalog_document(
    db: AsyncSession,
    *,
    supplier: ToolSupplier,
    file_bytes: bytes,
    filename: str,
    party_id: uuid.UUID | None = None,
    source_channel: str = "upload",
    owner_sub: str | None = None,
    parent_document_id: uuid.UUID | None = None,
    metadata: dict | None = None,
    reuse_duplicate: bool = True,
) -> RegisteredCatalog:
    """Store the file, create/reuse the Document, link it, and open a job.

    Storage upload happens here (not in the task) so the caller can fail the
    request loudly instead of promising processing for a file that never landed.
    """
    from app import storage

    digest = hashlib.sha256(file_bytes).hexdigest()
    existing = await find_catalog_document(db, supplier.id, digest)
    if existing is not None and reuse_duplicate:
        job = await _open_job(db, existing, restart=True)
        return RegisteredCatalog(document=existing, job=job, is_duplicate=True)

    path = catalog_storage_path(supplier.id, digest, filename)
    mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    storage.upload_file(file_bytes, path, content_type=mime)

    doc = Document(
        file_name=filename,
        file_hash=digest,
        file_size=len(file_bytes),
        mime_type=mime,
        storage_path=path,
        doc_type=DocumentType.supplier_catalog,
        status=DocumentStatus.ingested,
        source_channel=source_channel,
        owner_sub=owner_sub,
        metadata_={
            "supplier_catalog": True,
            "tool_supplier_id": str(supplier.id),
            "supplier_name": supplier.name,
            **({"parent_document_id": str(parent_document_id)} if parent_document_id else {}),
            **(metadata or {}),
        },
    )
    db.add(doc)
    await db.flush()

    db.add(
        DocumentLink(
            document_id=doc.id,
            linked_entity_type="tool_supplier",
            linked_entity_id=supplier.id,
            link_type=CATALOG_LINK_TYPE,
        )
    )
    resolved_party = party_id or supplier.main_supplier_id
    if resolved_party:
        db.add(
            DocumentLink(
                document_id=doc.id,
                linked_entity_type="party",
                linked_entity_id=resolved_party,
                link_type=CATALOG_LINK_TYPE,
            )
        )
    if parent_document_id:
        db.add(
            DocumentLink(
                document_id=doc.id,
                linked_entity_type="document",
                linked_entity_id=parent_document_id,
                link_type=ARCHIVE_MEMBER_LINK_TYPE,
            )
        )

    job = await _open_job(db, doc, restart=False)
    return RegisteredCatalog(document=doc, job=job, is_duplicate=False)


async def _open_job(db: AsyncSession, doc: Document, *, restart: bool) -> DocumentProcessingJob:
    if restart:
        result = await db.execute(
            select(DocumentProcessingJob)
            .where(DocumentProcessingJob.document_id == doc.id)
            .order_by(DocumentProcessingJob.created_at.desc())
            .limit(1)
        )
        job = result.scalar_one_or_none()
        if job is not None and job.status not in {"done", "failed"}:
            return job
    job = DocumentProcessingJob(
        document_id=doc.id,
        status="queued",
        pipeline_steps=_catalog_job_steps(),
        current_step="parse",
    )
    db.add(job)
    await db.flush()
    return job


async def catalog_documents_for_supplier(
    db: AsyncSession, supplier_ids: list[uuid.UUID]
) -> list[Document]:
    if not supplier_ids:
        return []
    result = await db.execute(
        select(Document)
        .join(DocumentLink, DocumentLink.document_id == Document.id)
        .where(
            DocumentLink.linked_entity_type == "tool_supplier",
            DocumentLink.linked_entity_id.in_(supplier_ids),
            DocumentLink.link_type == CATALOG_LINK_TYPE,
        )
        .order_by(Document.created_at.desc())
    )
    return list(result.scalars().unique().all())
