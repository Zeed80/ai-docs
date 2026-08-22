"""Discovery must be anchored on the supplier's OWN site.

Live failure this pins: a supplier whose card had no website (but whose 1046
existing catalog entries all came from one host) got 20 candidates back — work
gloves, an exhibition guide and another company's panel benders. Nothing on the
list belonged to them, and the agent, given no usable result and no instruction,
published a table of invoice items instead of attaching anything.
"""

from __future__ import annotations

import pytest

from app.api.tool_catalog import (
    _discovery_message,
    _mentions_supplier,
    _name_tokens,
)


def test_name_tokens_drop_legal_form():
    assert "ооо" not in _name_tokens("ООО Мир Станочника")
    assert "станочника" in _name_tokens("ООО Мир Станочника")


def test_foreign_host_without_supplier_name_is_not_a_candidate():
    assert not _mentions_supplier(
        "https://mirstandart.ru/mprices/download/85/khb-s-lateksnym.xlsx",
        "ХБ с латексным покрытием",
        "ООО Мир Станочника",
    )


def test_link_carrying_the_supplier_name_survives_the_filter():
    assert _mentions_supplier(
        "https://cdn.example.com/files/mirstan-katalog-2026.pdf",
        "Каталог Мир Станочника 2026",
        "ООО Мир Станочника",
    )


def test_message_without_website_tells_the_agent_to_ask_not_to_report():
    message = _discovery_message("ООО Пример", None, [], [])
    assert "СПРОСИ" in message
    assert "не публикуй" in message.lower()


def test_message_with_files_names_the_next_call():
    class _C:
        kind = "pdf"

    message = _discovery_message("ООО Пример", "https://example.ru", [_C(), _C()], [_C(), _C()])
    assert "attach_web_catalog" in message


def test_message_with_only_pages_offers_the_crawl():
    class _C:
        kind = "page"

    message = _discovery_message("ООО Пример", "https://example.ru", [_C()], [])
    assert "crawl_site" in message


@pytest.mark.asyncio
async def test_website_is_learned_from_existing_entries(db_session):
    """The system already knew the site — it just never wrote it down."""
    from app.api.tool_catalog import _ensure_supplier_website
    from app.db.models import ToolCatalogEntry, ToolSupplier, ToolTypeEnum

    supplier = ToolSupplier(name="ООО Сайт Из Записей", is_active=True)
    db_session.add(supplier)
    await db_session.commit()

    for index in range(3):
        db_session.add(
            ToolCatalogEntry(
                supplier_id=supplier.id,
                part_number=f"P-{index}",
                tool_type=ToolTypeEnum.drill,
                name=f"Сверло {index}",
                metadata_={"source_url": "https://www.example-tools.ru/catalog/page"},
            )
        )
    await db_session.commit()

    website, diagnostics = await _ensure_supplier_website(db_session, supplier)
    assert website == "https://example-tools.ru"
    assert any("site_from_existing_entries" in d for d in diagnostics)
    assert supplier.website == "https://example-tools.ru", "site must be remembered"


@pytest.mark.asyncio
async def test_attach_without_urls_discovers_them(db_session, client):
    """"Найди и загрузи" must be one call — the model may not stop halfway."""
    from unittest.mock import AsyncMock, patch

    from app.db.models import ToolSupplier
    from app.domain.tool_catalog import CatalogCandidate, DiscoverCatalogsResult

    supplier = ToolSupplier(
        name="ООО Один Вызов", website="https://example-tools.ru", is_active=True
    )
    db_session.add(supplier)
    await db_session.commit()

    discovered = DiscoverCatalogsResult(
        tool_supplier_id=supplier.id,
        supplier_name=supplier.name,
        website="https://example-tools.ru",
        candidates=[
            CatalogCandidate(
                url="https://example-tools.ru/katalog.pdf",
                title="Каталог",
                kind="pdf",
                found_via="site_scan",
                on_supplier_site=True,
            )
        ],
    )

    class _Task:
        id = "task-web"

    with (
        patch("app.api.tool_catalog.discover_catalogs", new=AsyncMock(return_value=discovered)),
        patch(
            "app.tasks.drawing_analysis.ingest_web_catalog_sources.delay",
            lambda *a, **k: _Task(),
        ),
    ):
        resp = await client.post(
            "/api/tool-catalog/attach-web-catalog",
            json={"supplier_name": "ООО Один Вызов"},
        )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "queued"
    assert data["sources"], "the discovered file must have been queued for ingestion"


def test_anchor_variants_collapse_to_one_candidate():
    from app.api.tool_catalog import _normalize_candidate_url

    base = "https://example-tools.ru/catalog/"
    assert _normalize_candidate_url(base + "#") == base
    assert _normalize_candidate_url(base + "#content") == base
    assert _normalize_candidate_url(base) == base


def test_business_directories_are_not_catalogs():
    from app.api.tool_catalog import _looks_like_catalog_link

    assert not _looks_like_catalog_link(
        "https://bizorg.su/moskva-rg/c241484-mir-stanochnika-ooo", "Мир Станочника, ООО"
    )
    assert not _looks_like_catalog_link("javascript:void(0)", "Каталог")
    assert _looks_like_catalog_link("https://example-tools.ru/katalog.pdf", "Каталог")


def test_file_urls_go_to_the_document_pipeline_pages_do_not():
    """A PDF catalog must not be ingested as "web text".

    Live: a 200 000-character supplier PDF went through the page-text path,
    which OCRs a handful of pages, and produced rows=0 with a success status.
    """
    from app.tasks.catalog_ingest import looks_like_catalog_file_url

    assert looks_like_catalog_file_url("https://example-tools.ru/files/Каталог-2026.pdf")
    assert looks_like_catalog_file_url("https://example-tools.ru/price/all.xlsx")
    assert looks_like_catalog_file_url("https://example-tools.ru/price/all.zip")
    assert not looks_like_catalog_file_url("https://example-tools.ru/catalog/")
    assert not looks_like_catalog_file_url("https://example-tools.ru/katalog/frezy")
