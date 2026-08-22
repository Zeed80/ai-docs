"""Э1/Э3: a supplier catalog is a Document with a visible processing job, and
archives fan out into child documents.

These pin the behaviours that were previously invisible: an upload that lands
nowhere, a re-ingest that wipes the supplier's other catalogs, and an archive
that is trusted to declare its own size.
"""

from __future__ import annotations

import io
import zipfile
from unittest.mock import patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.db.models import Document, DocumentLink, DocumentProcessingJob, DocumentType, ToolSupplier
from app.tasks.catalog_archive import ArchiveRejected, extract_catalog_archive


class _Task:
    id = "task-1"


def _zip(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


@pytest.fixture
async def supplier(db_session):
    s = ToolSupplier(name="ООО Загрузки", is_active=True)
    db_session.add(s)
    await db_session.commit()
    return s


@pytest.mark.asyncio
async def test_upload_creates_document_links_and_job(client: AsyncClient, db_session, supplier):
    with (
        patch("app.tasks.catalog_ingest.ingest_catalog_document.delay", lambda *a, **k: _Task()),
        patch("app.storage.upload_file", lambda *a, **k: "tool-catalogs/x"),
    ):
        resp = await client.post(
            f"/api/tool-catalog/suppliers/{supplier.id}/catalog",
            files={"file": ("price.csv", b"name,price\nfreza,100\n", "text/csv")},
        )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["document_id"] and data["job_id"]

    doc = await db_session.get(Document, __import__("uuid").UUID(data["document_id"]))
    assert doc is not None
    assert doc.doc_type == DocumentType.supplier_catalog
    links = (
        await db_session.execute(
            select(DocumentLink).where(DocumentLink.document_id == doc.id)
        )
    ).scalars().all()
    assert {l.linked_entity_type for l in links} >= {"tool_supplier"}

    job = (
        await db_session.execute(
            select(DocumentProcessingJob).where(DocumentProcessingJob.document_id == doc.id)
        )
    ).scalars().first()
    assert job is not None
    keys = [s["key"] for s in job.pipeline_steps]
    # "pages" and "images" joined the pipeline when parsing became page-wise.
    assert keys[:5] == ["store", "unpack", "pages", "parse", "images"]


@pytest.mark.asyncio
async def test_same_file_twice_is_not_duplicated(client: AsyncClient, db_session, supplier):
    payload = b"name,price\nfreza,100\n"
    ids = []
    for _ in range(2):
        with (
            patch("app.tasks.catalog_ingest.ingest_catalog_document.delay", lambda *a, **k: _Task()),
            patch("app.storage.upload_file", lambda *a, **k: "tool-catalogs/x"),
        ):
            resp = await client.post(
                f"/api/tool-catalog/suppliers/{supplier.id}/catalog",
                files={"file": ("price.csv", payload, "text/csv")},
            )
        assert resp.status_code == 200, resp.text
        ids.append(resp.json()["document_id"])
    assert ids[0] == ids[1]


@pytest.mark.asyncio
async def test_two_different_files_are_independent_uploads(
    client: AsyncClient, db_session, supplier
):
    docs = []
    for payload, name in ((b"a,b\n1,2\n", "one.csv"), (b"c,d\n3,4\n", "two.csv")):
        with (
            patch("app.tasks.catalog_ingest.ingest_catalog_document.delay", lambda *a, **k: _Task()),
            patch("app.storage.upload_file", lambda *a, **k: f"tool-catalogs/{name}"),
        ):
            resp = await client.post(
                f"/api/tool-catalog/suppliers/{supplier.id}/catalog",
                files={"file": (name, payload, "text/csv")},
            )
        docs.append(resp.json()["document_id"])
    assert docs[0] != docs[1]


@pytest.mark.asyncio
async def test_uploads_listing_reports_stages(client: AsyncClient, db_session):
    from app.db.models import Party

    party = Party(name="ООО Список", inn="7700000001")
    db_session.add(party)
    await db_session.commit()

    with (
        patch("app.tasks.catalog_ingest.ingest_catalog_document.delay", lambda *a, **k: _Task()),
        patch("app.storage.upload_file", lambda *a, **k: "tool-catalogs/p"),
    ):
        up = await client.post(
            f"/api/tool-catalog/by-supplier/{party.id}/catalog",
            files={"file": ("price.csv", b"name,price\nfreza,100\n", "text/csv")},
        )
    assert up.status_code == 200, up.text

    resp = await client.get(f"/api/tool-catalog/by-supplier/{party.id}/uploads")
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["file_name"] == "price.csv"
    assert [s["key"] for s in items[0]["steps"]][0] == "store"


@pytest.mark.asyncio
async def test_deleting_one_upload_keeps_other_catalog_entries(
    client: AsyncClient, db_session, supplier
):
    from app.db.models import ToolCatalogEntry, ToolTypeEnum

    doc_ids = []
    for payload, name in ((b"a\n1\n", "one.csv"), (b"b\n2\n", "two.csv")):
        with (
            patch("app.tasks.catalog_ingest.ingest_catalog_document.delay", lambda *a, **k: _Task()),
            patch("app.storage.upload_file", lambda *a, **k: f"tool-catalogs/{name}"),
        ):
            resp = await client.post(
                f"/api/tool-catalog/suppliers/{supplier.id}/catalog",
                files={"file": (name, payload, "text/csv")},
            )
        doc_ids.append(__import__("uuid").UUID(resp.json()["document_id"]))

    for doc_id, part in zip(doc_ids, ("P-1", "P-2")):
        db_session.add(
            ToolCatalogEntry(
                supplier_id=supplier.id,
                source_document_id=doc_id,
                part_number=part,
                tool_type=ToolTypeEnum.drill,
                name=f"Сверло {part}",
            )
        )
    await db_session.commit()

    resp = await client.delete(f"/api/tool-catalog/uploads/{doc_ids[0]}")
    assert resp.status_code == 200, resp.text
    # The endpoint now delegates to /api/catalogs, which reports what each
    # deletion mode actually removed instead of one opaque counter.
    assert resp.json()["entries"] == 1

    remaining = (
        await db_session.execute(
            select(ToolCatalogEntry).where(
                ToolCatalogEntry.supplier_id == supplier.id,
                ToolCatalogEntry.is_active.is_(True),
            )
        )
    ).scalars().all()
    assert [e.part_number for e in remaining] == ["P-2"]


# ── archives ────────────────────────────────────────────────────────────────


def test_archive_yields_members_and_ignores_junk():
    data = _zip(
        {
            "price1.csv": b"name,price\nfreza,10\n",
            "price2.xlsx": b"binary-ish",
            "readme.exe": b"nope",
            "subdir/price3.csv": b"name,price\nsverlo,20\n",
        }
    )
    members = extract_catalog_archive(data, "catalogs.zip")
    assert sorted(m.name for m in members) == ["price1.csv", "price2.xlsx", "price3.csv"]


def test_zip_bomb_is_rejected():
    data = _zip({"bomb.csv": b"0" * (5 * 1024 * 1024)})
    with pytest.raises(ArchiveRejected):
        extract_catalog_archive(data, "bomb.zip")


def test_path_traversal_member_is_rejected():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../../etc/price.csv", b"name,price\nx,1\n")
    with pytest.raises(ArchiveRejected):
        extract_catalog_archive(buf.getvalue(), "evil.zip")


def test_archive_without_usable_members_is_rejected():
    data = _zip({"photo.png": b"\x89PNG"})
    with pytest.raises(ArchiveRejected):
        extract_catalog_archive(data, "pics.zip")


@pytest.mark.asyncio
async def test_old_job_gains_new_pipeline_stages_as_pending(db_session, supplier):
    """The catalog pipeline grew "pages" and "images" stages after page-wise
    parsing landed. A job created before that must not break or lose its
    history — the unknown keys simply appear as pending."""
    from app.db.models import Document, DocumentProcessingJob, DocumentStatus, DocumentType
    from app.domain import processing_jobs as pj
    from app.domain.pipeline import CATALOG_PIPELINE_STEP_DEFINITIONS as CATALOG_STEPS

    doc = Document(
        file_name="старый-каталог.pdf",
        file_hash="0" * 64,
        file_size=10,
        mime_type="application/pdf",
        storage_path="tool-catalogs/x/старый-каталог.pdf",
        doc_type=DocumentType.supplier_catalog,
        status=DocumentStatus.ingested,
    )
    db_session.add(doc)
    await db_session.flush()

    old_job = DocumentProcessingJob(
        document_id=doc.id,
        status="done",
        current_step="completed",
        # The stage list as it was before pages/images existed.
        pipeline_steps=[
            {"key": "store", "label": "Файл сохранен", "status": "done"},
            {"key": "parse", "label": "Разбор каталога", "status": "done", "rows_parsed": 42},
            {"key": "entries", "label": "Позиции каталога", "status": "done", "created": 42},
        ],
    )
    db_session.add(old_job)
    await db_session.commit()

    steps = {step["key"]: step for step in pj.ensure_step_entries(old_job, CATALOG_STEPS)}

    assert steps["pages"]["status"] == "pending"
    assert steps["images"]["status"] == "pending"
    # …and nothing that was already recorded is lost.
    assert steps["parse"]["status"] == "done"
    assert steps["parse"]["rows_parsed"] == 42
    assert steps["entries"]["created"] == 42
