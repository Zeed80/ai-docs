"""Э5: crawling a supplier's own site.

The pure helpers (URL normalisation, page priority, crawlability) are what
decide whether a crawl walks the catalog or the news section, and whether a
re-run doubles the catalog — so they are pinned here without network.
"""

from __future__ import annotations

from app.tasks.catalog_crawl import is_crawlable, normalize_url, page_priority


def test_normalize_url_drops_fragment_tracking_and_trailing_slash():
    a = normalize_url("https://Example.RU/catalog/?utm_source=ya&page=2#top")
    b = normalize_url("https://example.ru/catalog?page=2")
    assert a == b, "the same page under tracking params must dedup to one URL"


def test_listing_pages_outrank_news_and_contacts():
    catalog = page_priority("https://example.ru/catalog/frezy")
    news = page_priority("https://example.ru/news/2026")
    contacts = page_priority("https://example.ru/kontakty")
    assert catalog < news
    assert catalog < contacts


def test_assets_are_not_crawled():
    assert not is_crawlable("https://example.ru/img/logo.png")
    assert not is_crawlable("https://example.ru/style.css")
    assert not is_crawlable("mailto:sales@example.ru")
    assert is_crawlable("https://example.ru/catalog/page-2")


def test_deeper_paths_sort_after_shallow_ones_within_same_category():
    shallow = page_priority("https://example.ru/catalog")
    deep = page_priority("https://example.ru/catalog/a/b/c/d")
    assert shallow < deep


# ── Э7: unresolved plan placeholders must never become data ─────────────────


def test_supplier_name_rejects_unresolved_placeholder():
    import pytest

    from app.domain.tool_catalog import ToolSupplierCreate, ToolSupplierUpdate

    with pytest.raises(Exception):
        ToolSupplierCreate(name="${steps.discover_suppliers.output.suppliers[0].name}")
    with pytest.raises(Exception):
        ToolSupplierUpdate(name="${steps.x.output.name}")
    assert ToolSupplierCreate(name="ООО Мир Станочника").name == "ООО Мир Станочника"


def test_cleanup_normalizes_supplier_names_for_matching():
    from app.scripts.cleanup_tool_suppliers import normalize_name

    assert normalize_name("ООО «Мир Станочника»") == normalize_name("Мир станочника")
    assert normalize_name("АО Инструмент-Сервис") == normalize_name("инструмент сервис")
