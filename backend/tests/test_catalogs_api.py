"""Browsing and searching catalogs: what the user actually sees.

Pins the behaviours the previous UI could not offer at all — telling two
catalogs of one supplier apart, opening a page, and a search that ranks an
exact article first instead of sorting everything by name.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.db.models import (
    CatalogPage,
    Document,
    DocumentLink,
    DocumentStatus,
    DocumentType,
    Party,
    ToolCatalogEntry,
    ToolSupplier,
    ToolTypeEnum,
)


async def _catalog(db, supplier, *, name: str, pages: int = 3) -> Document:
    doc = Document(
        file_name=name,
        file_hash=uuid.uuid4().hex,
        file_size=1024,
        mime_type="application/pdf",
        storage_path=f"tool-catalogs/{supplier.id}/ab/abcd/{name}",
        doc_type=DocumentType.supplier_catalog,
        status=DocumentStatus.ingested,
        metadata_={"tool_supplier_id": str(supplier.id), "supplier_name": supplier.name},
    )
    db.add(doc)
    await db.flush()
    db.add(
        DocumentLink(
            document_id=doc.id,
            linked_entity_type="tool_supplier",
            linked_entity_id=supplier.id,
            link_type="supplier_catalog",
        )
    )
    for number in range(1, pages + 1):
        db.add(
            CatalogPage(
                document_id=doc.id,
                page_number=number,
                status="parsed" if number > 1 else "skipped",
                skip_reason="cover" if number == 1 else None,
                entries_count=1 if number > 1 else 0,
                image_width=1241,
                image_height=1670,
                thumb_path=f"tool-catalogs/x/pages/{number:04d}_thumb.webp",
            )
        )
    await db.commit()
    return doc


@pytest.fixture
async def supplier_with_catalogs(db_session):
    party = Party(name="ООО Два Каталога", inn="7700000042")
    db_session.add(party)
    await db_session.flush()
    supplier = ToolSupplier(name="ООО Два Каталога", is_active=True, main_supplier_id=party.id)
    db_session.add(supplier)
    await db_session.commit()

    first = await _catalog(db_session, supplier, name="Каталог-фрезы.pdf", pages=3)
    second = await _catalog(db_session, supplier, name="Каталог-свёрла.pdf", pages=2)

    page = (
        await db_session.execute(
            select(CatalogPage).where(
                CatalogPage.document_id == first.id, CatalogPage.page_number == 2
            )
        )
    ).scalar_one()

    db_session.add_all(
        [
            ToolCatalogEntry(
                supplier_id=supplier.id,
                source_document_id=first.id,
                catalog_page_id=page.id,
                catalog_page=2,
                part_number="MT190-016C04",
                tool_type=ToolTypeEnum.endmill,
                name="Фреза концевая 90° Ø16",
                price_value=3450.0,
                image_path="tool-catalogs/x/crops/0002_r0.webp",
                image_thumb_path="tool-catalogs/x/crops/0002_r0_thumb.webp",
                image_kind="crop",
                image_confidence=0.8,
            ),
            ToolCatalogEntry(
                supplier_id=supplier.id,
                source_document_id=second.id,
                catalog_page=1,
                part_number="DR-6-5",
                tool_type=ToolTypeEnum.drill,
                name="Сверло спиральное Ø6.5",
                image_kind="page",
                image_thumb_path="tool-catalogs/x/pages/0001_thumb.webp",
            ),
        ]
    )
    await db_session.commit()
    return {"party": party, "supplier": supplier, "first": first, "second": second}


@pytest.mark.asyncio
async def test_catalogs_are_listed_separately_with_progress(
    client: AsyncClient, supplier_with_catalogs
):
    party = supplier_with_catalogs["party"]
    resp = await client.get(f"/api/catalogs?party_id={party.id}")
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert {item["file_name"] for item in items} == {
        "Каталог-фрезы.pdf",
        "Каталог-свёрла.pdf",
    }
    by_name = {item["file_name"]: item for item in items}
    assert by_name["Каталог-фрезы.pdf"]["page_count"] == 3
    assert by_name["Каталог-фрезы.pdf"]["entries_count"] == 1
    assert by_name["Каталог-фрезы.pdf"]["entries_with_image"] == 1
    assert by_name["Каталог-фрезы.pdf"]["cover_url"].endswith("size=thumb")
    assert by_name["Каталог-фрезы.pdf"]["download_url"].startswith("/api/documents/")


@pytest.mark.asyncio
async def test_pages_expose_status_and_skip_reason(client: AsyncClient, supplier_with_catalogs):
    """A skipped cover must look different from a page that yielded nothing."""
    doc = supplier_with_catalogs["first"]
    resp = await client.get(f"/api/catalogs/{doc.id}/pages")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["page_count"] == 3
    first_page = body["items"][0]
    assert first_page["status"] == "skipped"
    assert first_page["skip_reason"] == "cover"
    assert first_page["thumb_url"].endswith("size=thumb")


@pytest.mark.asyncio
async def test_search_ranks_the_exact_article_first(client: AsyncClient, supplier_with_catalogs):
    party = supplier_with_catalogs["party"]
    resp = await client.post(
        "/api/catalogs/search",
        json={"query": "MT190-016C04", "party_id": str(party.id)},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["items"], body
    assert body["items"][0]["part_number"] == "MT190-016C04"
    assert body["diagnostics"]["branches"]["exact"] >= 1


@pytest.mark.asyncio
async def test_search_scoped_to_one_catalog_excludes_the_other(
    client: AsyncClient, supplier_with_catalogs
):
    party = supplier_with_catalogs["party"]
    second = supplier_with_catalogs["second"]
    resp = await client.post(
        "/api/catalogs/search",
        json={"party_id": str(party.id), "catalog_document_id": str(second.id)},
    )
    assert resp.status_code == 200, resp.text
    names = [item["name"] for item in resp.json()["items"]]
    assert names == ["Сверло спиральное Ø6.5"]


@pytest.mark.asyncio
async def test_facets_are_counted_over_the_whole_result_not_the_page(
    client: AsyncClient, supplier_with_catalogs
):
    party = supplier_with_catalogs["party"]
    resp = await client.post(
        "/api/catalogs/search",
        json={"party_id": str(party.id), "page_size": 1, "include_facets": True},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["items"]) == 1, "page size honoured"
    facets = body["facets"]
    assert sum(item["count"] for item in facets["catalogs"]) == 2, "facets cover both catalogs"
    assert facets["with_image"] == 1
    assert facets["with_price"] == 1


@pytest.mark.asyncio
async def test_entries_carry_their_catalog_page_and_image_kind(
    client: AsyncClient, supplier_with_catalogs
):
    party = supplier_with_catalogs["party"]
    resp = await client.post("/api/catalogs/search", json={"party_id": str(party.id)})
    items = {item["part_number"]: item for item in resp.json()["items"]}
    assert items["MT190-016C04"]["page_number"] == 2
    assert items["MT190-016C04"]["image_kind"] == "crop"
    assert items["MT190-016C04"]["catalog_name"] == "Каталог-фрезы.pdf"
    # A page preview is offered, but never disguised as a product photo.
    assert items["DR-6-5"]["image_kind"] == "page"
    assert items["DR-6-5"]["thumb_url"].endswith("size=thumb")


@pytest.mark.asyncio
async def test_missing_page_image_is_404_not_500(client: AsyncClient, supplier_with_catalogs):
    doc = supplier_with_catalogs["first"]
    resp = await client.get(f"/api/catalogs/{doc.id}/pages/999/image")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_pause_stops_parsing_and_keeps_what_is_done(
    client: AsyncClient, db_session, supplier_with_catalogs
):
    """Parsing a big catalog holds the GPU for hours — it must be stoppable
    without losing the pages already parsed, and it must STAY stopped (the
    resume watchdog would otherwise restart it minutes later)."""
    from app.tasks.catalog_pages import _is_paused

    doc = supplier_with_catalogs["first"]
    resp = await client.post(f"/api/catalogs/{doc.id}/pause")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["paused"] is True
    assert body["pages_done"] == 3, "already parsed pages stay parsed"
    assert "остановлен" in body["message"]

    await db_session.refresh(doc)
    assert await _is_paused(db_session, doc.id) is True

    with __import__("unittest.mock", fromlist=["patch"]).patch(
        "app.tasks.catalog_pages.render_catalog_page_batch.delay", lambda *a, **k: None
    ):
        again = await client.post(f"/api/catalogs/{doc.id}/pause?resume=true")
    assert again.status_code == 200, again.text
    assert again.json()["paused"] is False
    await db_session.refresh(doc)
    assert await _is_paused(db_session, doc.id) is False


@pytest.mark.asyncio
async def test_paused_catalog_is_not_reported_as_running(
    client: AsyncClient, db_session, supplier_with_catalogs
):
    """A paused run must not look active to the UI.

    It did, and the supplier card kept polling every three seconds forever —
    the page looked like it was refreshing in a loop (user report).
    """
    from unittest.mock import patch

    from app.db.models import DocumentProcessingJob

    doc = supplier_with_catalogs["first"]
    party = supplier_with_catalogs["party"]
    db_session.add(
        DocumentProcessingJob(
            document_id=doc.id,
            status="running",
            pipeline_steps=[{"key": "parse", "label": "Разбор", "status": "running"}],
            current_step="parse",
        )
    )
    await db_session.commit()

    resp = await client.post(f"/api/catalogs/{doc.id}/pause")
    assert resp.status_code == 200, resp.text

    listing = await client.get(f"/api/catalogs?party_id={party.id}")
    item = next(
        row for row in listing.json()["items"] if row["document_id"] == str(doc.id)
    )
    assert item["paused"] is True
    assert item["status"] == "paused", "status must not stay 'running'"

    with patch(
        "app.tasks.catalog_pages.render_catalog_page_batch.delay", lambda *a, **k: None
    ):
        resumed = await client.post(f"/api/catalogs/{doc.id}/pause?resume=true")
    assert resumed.status_code == 200
    again = await client.get(f"/api/catalogs?party_id={party.id}")
    item = next(
        row for row in again.json()["items"] if row["document_id"] == str(doc.id)
    )
    assert item["paused"] is False
    assert item["status"] == "running"


@pytest.mark.asyncio
async def test_positions_without_a_catalog_are_shown_honestly(
    client: AsyncClient, db_session, supplier_with_catalogs
):
    """Positions imported before page-wise parsing have no file behind them.

    Hiding them would make a supplier's catalog look smaller than it is; the
    list carries them as one pseudo-catalog with no document id, so the UI can
    offer a re-parse instead of a broken "open".
    """
    supplier = supplier_with_catalogs["supplier"]
    party = supplier_with_catalogs["party"]
    db_session.add(
        ToolCatalogEntry(
            supplier_id=supplier.id,
            part_number="LEGACY-1",
            tool_type=ToolTypeEnum.other,
            name="Старая позиция без каталога",
            metadata_={"legacy_import": True},
        )
    )
    await db_session.commit()

    resp = await client.get(f"/api/catalogs?party_id={party.id}")
    assert resp.status_code == 200, resp.text
    legacy = [item for item in resp.json()["items"] if item["document_id"] is None]
    assert len(legacy) == 1
    assert legacy[0]["entries_count"] == 1
    assert legacy[0]["legacy"] is True
    assert legacy[0]["download_url"] is None, "there is no file to download"


@pytest.mark.asyncio
async def test_old_search_endpoint_uses_the_same_ranking(
    client: AsyncClient, supplier_with_catalogs
):
    """Two searches over the same data that disagree is a bug waiting to be
    reported; the legacy endpoint now delegates to the hybrid one."""
    supplier = supplier_with_catalogs["supplier"]
    resp = await client.get(
        f"/api/tool-catalog/search?query=MT190-016C04&supplier_id={supplier.id}"
    )
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert items and items[0]["part_number"] == "MT190-016C04"


@pytest.mark.asyncio
async def test_page_image_is_rendered_on_demand_and_cached(
    client: AsyncClient, db_session, supplier_with_catalogs
):
    """Only the first pages are rendered eagerly — a 948-page catalog would cost
    far more storage than the saved time is worth. Opening any other page must
    render it, serve webp, and remember the result for next time."""
    from unittest.mock import patch

    doc = supplier_with_catalogs["first"]
    page = (
        await db_session.execute(
            select(CatalogPage).where(
                CatalogPage.document_id == doc.id, CatalogPage.page_number == 3
            )
        )
    ).scalar_one()
    page.image_path = None  # not rendered eagerly
    page.thumb_path = None
    await db_session.commit()

    uploaded: list[str] = []
    fake_png = b"fake-webp-bytes"

    with (
        patch("app.storage.download_file", lambda *a, **k: b"%PDF-1.4 fake"),
        patch(
            "app.tasks.catalog_pages._render_page",
            lambda pdf, index, dpi=150: (fake_png, 1241, 1670),
        ),
        patch("app.storage.upload_file", lambda data, path, *a, **k: uploaded.append(path)),
        patch("fitz.open", lambda *a, **k: _FakePdf()),
    ):
        resp = await client.get(f"/api/catalogs/{doc.id}/pages/3/image")

    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "image/webp"
    assert resp.headers.get("etag"), "an ETag lets the browser skip re-downloading"
    assert uploaded, "the rendered page must be cached, not re-rendered every open"

    await db_session.refresh(page)
    assert page.image_path, "the cached path is remembered on the page row"

    # A repeat request with the ETag must be answered 304, not with the bytes.
    again = await client.get(
        f"/api/catalogs/{doc.id}/pages/3/image",
        headers={"if-none-match": resp.headers["etag"]},
    )
    assert again.status_code == 304


class _FakePdf:
    """Minimal stand-in for a PyMuPDF document."""

    page_count = 5

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.mark.asyncio
async def test_delete_data_keeps_the_file_for_a_re_parse(
    client: AsyncClient, db_session, supplier_with_catalogs
):
    """Three honest options instead of one all-or-nothing button: "данные" frees
    the catalog for a clean re-parse without losing the file."""
    from unittest.mock import patch

    doc = supplier_with_catalogs["first"]
    with (
        patch("app.storage.delete_prefix", lambda *a, **k: 0),
        patch("app.vector.qdrant_store.delete_tool_catalog_entry", lambda *a, **k: None),
    ):
        resp = await client.request("DELETE", f"/api/catalogs/{doc.id}?mode=data")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["entries"] == 1
    assert body["pages"] == 3
    assert "Файл сохранён" in body["message"]

    assert await db_session.get(Document, doc.id) is not None, "the file record stays"
    left = (
        await db_session.execute(
            select(CatalogPage).where(CatalogPage.document_id == doc.id)
        )
    ).scalars().all()
    assert left == [], "the page registry is cleared with the data"


@pytest.mark.asyncio
async def test_delete_file_keeps_positions(
    client: AsyncClient, db_session, supplier_with_catalogs
):
    """Reclaiming storage must not cost the prices already extracted."""
    from unittest.mock import patch

    doc = supplier_with_catalogs["first"]
    with patch("app.storage.delete_prefix", lambda *a, **k: 7):
        resp = await client.request("DELETE", f"/api/catalogs/{doc.id}?mode=file")
    assert resp.status_code == 200, resp.text
    assert resp.json()["images"] == 7

    entries = (
        await db_session.execute(
            select(ToolCatalogEntry).where(ToolCatalogEntry.source_document_id == doc.id)
        )
    ).scalars().all()
    assert len(entries) == 1, "positions survive"
    await db_session.refresh(doc)
    assert (doc.metadata_ or {}).get("file_removed") is True, "and the card says so"


@pytest.mark.asyncio
async def test_delete_all_removes_pages_too(
    client: AsyncClient, db_session, supplier_with_catalogs
):
    """Page rows carry a foreign key on the document — without deleting them the
    whole delete failed once page-wise parsing landed."""
    from unittest.mock import patch

    doc = supplier_with_catalogs["second"]
    with (
        patch("app.storage.delete_prefix", lambda *a, **k: 3),
        patch("app.vector.qdrant_store.delete_tool_catalog_entry", lambda *a, **k: None),
    ):
        resp = await client.request("DELETE", f"/api/catalogs/{doc.id}?mode=all")
    assert resp.status_code == 200, resp.text
    assert await db_session.get(Document, doc.id) is None


@pytest.mark.asyncio
async def test_search_across_several_selected_catalogs(
    client: AsyncClient, supplier_with_catalogs
):
    """"Искать в этих двух каталогах" — a one-at-a-time filter could not say it."""
    first = supplier_with_catalogs["first"]
    second = supplier_with_catalogs["second"]

    both = await client.post(
        "/api/catalogs/search",
        json={"catalog_document_ids": [str(first.id), str(second.id)]},
    )
    assert both.status_code == 200, both.text
    assert both.json()["total"] == 2

    one = await client.post(
        "/api/catalogs/search", json={"catalog_document_ids": [str(second.id)]}
    )
    assert [item["name"] for item in one.json()["items"]] == ["Сверло спиральное Ø6.5"]


@pytest.mark.asyncio
async def test_search_returns_a_publishable_report(client: AsyncClient, supplier_with_catalogs):
    """The agent must not re-derive a table from prose."""
    party = supplier_with_catalogs["party"]
    resp = await client.post(
        "/api/catalogs/search", json={"query": "фреза", "party_id": str(party.id)}
    )
    assert resp.status_code == 200, resp.text
    report = resp.json()["report"]
    assert report["rows"], report
    page_column = next(c for c in report["columns"] if c["key"] == "page")
    assert page_column["type"] == "link", "the page reference must be clickable"
    assert any(row["image"] in {"товар", "страница"} for row in report["rows"])


@pytest.mark.asyncio
async def test_progress_shows_the_stage_that_is_actually_running():
    """Карточка обязана показывать разбор, а не обогнавший его рендер.

    На стенде рендер уходил на сотни страниц вперёд, и карточка писала
    «488 из 488», когда разобрано было 228: человек читал это как «готово».
    """
    from app.api.catalogs import _progress_from_job

    class _Job:
        pipeline_steps = [
            {"key": "pages", "progress": {"done": 488, "total": 488}},
            {"key": "parse", "progress": {"done": 228, "total": 488}},
        ]

    assert _progress_from_job(_Job()) == (228, 488)


@pytest.mark.asyncio
async def test_progress_falls_back_to_the_last_finished_stage():
    from app.api.catalogs import _progress_from_job

    class _Job:
        pipeline_steps = [
            {"key": "pages", "progress": {"done": 11, "total": 11}},
            {"key": "parse", "progress": {"done": 11, "total": 11}},
        ]

    assert _progress_from_job(_Job()) == (11, 11)
