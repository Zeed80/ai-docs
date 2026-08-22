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
