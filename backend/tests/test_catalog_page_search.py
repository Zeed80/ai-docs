"""Поиск ПО ДОКУМЕНТУ каталога — то, чего в каталогах не было совсем.

Человек, открывший каталог на 948 страниц, ищет «где это здесь», а не «какая
это строка». Поэтому ответ — страницы, и страницы с совпавшими позициями идут
первыми: страница, которая товар продаёт, полезнее страницы, которая его
упоминает.
"""

from __future__ import annotations

import uuid

import pytest

from app.api.catalogs import search_catalog_pages
from app.db.models import (
    CatalogPage,
    Document,
    DocumentStatus,
    DocumentType,
    Party,
    ToolCatalogEntry,
    ToolSupplier,
    ToolTypeEnum,
)


@pytest.fixture
async def catalog_with_pages(db_session):
    party = Party(name="ООО Поиск", inn="7700000099")
    db_session.add(party)
    await db_session.flush()
    supplier = ToolSupplier(name="ООО Поиск", is_active=True, main_supplier_id=party.id)
    db_session.add(supplier)
    await db_session.flush()

    doc = Document(
        file_name="Каталог-поиск.pdf",
        file_hash=uuid.uuid4().hex,
        file_size=2048,
        mime_type="application/pdf",
        storage_path="tool-catalogs/x/ab/abcd/Каталог-поиск.pdf",
        doc_type=DocumentType.supplier_catalog,
        status=DocumentStatus.ingested,
    )
    db_session.add(doc)
    await db_session.flush()

    db_session.add_all(
        [
            CatalogPage(
                document_id=doc.id,
                page_number=1,
                status="skipped",
                skip_reason="toc",
                text="Содержание. Фрезы концевые ..... 12",
                entries_count=0,
            ),
            CatalogPage(
                document_id=doc.id,
                page_number=12,
                status="parsed",
                text="Фрезы концевые твердосплавные, серия FR, покрытие TiAlN",
                entries_count=2,
            ),
            # Страница без читаемого текстового слоя — как живой INSIZE.
            CatalogPage(
                document_id=doc.id, page_number=13, status="parsed", text="", entries_count=1
            ),
        ]
    )
    db_session.add_all(
        [
            ToolCatalogEntry(
                supplier_id=supplier.id,
                source_document_id=doc.id,
                catalog_page=12,
                part_number="FR-12",
                name="Фреза концевая 12 мм",
                tool_type=ToolTypeEnum.endmill,
                is_active=True,
            ),
            ToolCatalogEntry(
                supplier_id=supplier.id,
                source_document_id=doc.id,
                catalog_page=13,
                part_number="FR-16",
                name="Фреза концевая 16 мм",
                tool_type=ToolTypeEnum.endmill,
                is_active=True,
            ),
        ]
    )
    await db_session.commit()
    return doc


@pytest.mark.asyncio
async def test_a_word_from_the_page_finds_that_page(db_session, catalog_with_pages):
    result = await search_catalog_pages(
        catalog_with_pages.id, q="покрытие", limit=10, db=db_session
    )
    assert [hit.page_number for hit in result.items] == [12]
    assert "TiAlN" in result.items[0].snippet


@pytest.mark.asyncio
async def test_pages_whose_positions_match_come_first(db_session, catalog_with_pages):
    """«Фрезы» стоит и в оглавлении, и на товарной странице. Человеку нужна
    вторая — там товар, а не ссылка на него."""
    result = await search_catalog_pages(
        catalog_with_pages.id, q="фреза концевая", limit=10, db=db_session
    )
    assert result.items, "страницы с позициями обязаны находиться"
    assert result.items[0].matched_entries > 0
    assert result.items[0].page_number in (12, 13)


@pytest.mark.asyncio
async def test_article_code_is_found_although_a_dictionary_would_split_it(
    db_session, catalog_with_pages
):
    """Артикул для словаря полнотекстового поиска — не слово: «FR-16» он режет
    на части и целиком никогда не находит. Ветка ILIKE закрывает именно это."""
    result = await search_catalog_pages(catalog_with_pages.id, q="FR-16", limit=10, db=db_session)
    assert [hit.page_number for hit in result.items] == [13]


@pytest.mark.asyncio
async def test_page_without_readable_text_shows_its_positions_instead(
    db_session, catalog_with_pages
):
    """У страницы 13 текста нет (PDF без шрифтовых карт). Пустая строка вместо
    подсказки — это «нашлось, но непонятно что»; показываем позиции."""
    result = await search_catalog_pages(catalog_with_pages.id, q="FR-16", limit=10, db=db_session)
    assert "FR-16" in result.items[0].snippet


@pytest.mark.asyncio
async def test_catalog_without_any_readable_text_says_so(db_session, catalog_with_pages):
    """Ноль результатов и ноль текста — это разные новости для человека."""
    from sqlalchemy import update

    await db_session.execute(
        update(CatalogPage).where(CatalogPage.document_id == catalog_with_pages.id).values(text="")
    )
    await db_session.commit()

    result = await search_catalog_pages(
        catalog_with_pages.id, q="покрытие", limit=10, db=db_session
    )
    assert result.items == []
    assert result.message and "текстового слоя" in result.message


@pytest.mark.asyncio
async def test_unknown_catalog_is_404_not_an_empty_result(db_session):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await search_catalog_pages(uuid.uuid4(), q="фреза", limit=10, db=db_session)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_static_catalog_routes_are_not_swallowed_by_the_id_route(client):
    """/api/catalogs/visual-status обязан быть маршрутом, а не «документом».

    Найдено на живом стенде: объявленный ПОСЛЕ "/{document_id}" статический
    путь сопоставлялся с параметрическим, и браузер получал 422 «не UUID» —
    в интерфейсе это выглядело как «поиск по фото недоступен».
    """
    response = await client.get("/api/catalogs/visual-status")
    assert response.status_code == 200, response.text
    assert "available" in response.json()
