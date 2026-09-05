"""Ф3 web-sourced supplier catalog ingestion (AGENT_AUTONOMY_ROADMAP.md) —
the bridge from Ф2's web_discover output to ToolCatalogEntry rows, built on
top of the *existing* file-upload catalog pipeline
(app.tasks.drawing_analysis._ingest_catalog_async) rather than a parallel one:
_create_catalog_entries_from_rows is the shared entry-creation/embed/graph
loop both paths now call.

No mocking of embed_text/Qdrant/graph — matches the existing convention in
test_tool_catalog.py (those tests hit the real services too); only the new
LLM text-extraction call is mocked, since its output needs to be deterministic
for the row assertions here.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.db.models import AnomalyCard, ToolCatalogEntry, ToolSupplier, ToolTypeEnum
from app.tasks.drawing_analysis import (
    _create_catalog_entries_from_rows,
    _parse_catalog_text_via_llm,
    ingest_web_catalog_source,
)


@pytest.fixture
async def supplier(db_session):
    s = ToolSupplier(name="ACME Tools", website="https://acme.example", is_active=True)
    db_session.add(s)
    await db_session.commit()
    return s


async def _make_catalog_document(db, name: str = "каталог.pdf"):
    """A real Document row — source_document_id carries a foreign key."""
    import uuid as _uuid

    from app.db.models import Document, DocumentStatus, DocumentType

    doc = Document(
        file_name=name,
        file_hash=_uuid.uuid4().hex,
        file_size=100,
        mime_type="application/pdf",
        storage_path=f"tool-catalogs/test/{name}",
        doc_type=DocumentType.supplier_catalog,
        status=DocumentStatus.ingested,
    )
    db.add(doc)
    await db.flush()
    return doc.id


def _row(**overrides) -> dict:
    row = {
        "part_number": "DRL-8MM",
        "name": "Сверло Ø8мм HSS",
        "tool_type": "drill",
        "diameter_mm": 8.0,
        "price": 350.0,
        "currency": "RUB",
    }
    row.update(overrides)
    return row


# ── _create_catalog_entries_from_rows ───────────────────────────────────────


@pytest.mark.asyncio
async def test_manual_upload_path_leaves_metadata_unset(db_session, supplier):
    """No provenance argument (the file-upload caller's default) must behave
    exactly as before Ф3 — no review_status gating at all."""
    result = await _create_catalog_entries_from_rows(db_session, supplier.id, [_row()])
    await db_session.commit()

    assert result["created"] == 1
    assert result["conflicted"] == 0
    entry = (
        await db_session.execute(
            select(ToolCatalogEntry).where(ToolCatalogEntry.supplier_id == supplier.id)
        )
    ).scalar_one()
    assert entry.metadata_ is None


@pytest.mark.asyncio
async def test_web_sourced_entry_gets_ingested_review_status(db_session, supplier):
    result = await _create_catalog_entries_from_rows(
        db_session,
        supplier.id,
        [_row()],
        provenance={
            "discovery_method": "web_discover",
            "source_url": "https://acme.example/catalog",
            "fetched_at": "2026-08-20T12:00:00+00:00",
            "title": "ACME Catalog",
        },
    )
    await db_session.commit()

    assert result["created"] == 1
    entry = (
        await db_session.execute(
            select(ToolCatalogEntry).where(ToolCatalogEntry.supplier_id == supplier.id)
        )
    ).scalar_one()
    assert entry.metadata_["review_status"] == "ingested"
    assert entry.metadata_["source_url"] == "https://acme.example/catalog"
    assert entry.metadata_["title"] == "ACME Catalog"


@pytest.mark.asyncio
async def test_conflicting_web_sourced_entry_creates_anomaly_and_leaves_existing_untouched(
    db_session, supplier
):
    """A web-discovered row for a part_number that already exists with a
    materially different price must not silently overwrite — the existing
    entry stays exactly as it was, the new one is flagged needs_review, and
    an AnomalyCard links them."""
    existing = ToolCatalogEntry(
        supplier_id=supplier.id,
        part_number="DRL-8MM",
        tool_type=ToolTypeEnum.drill,
        name="Сверло Ø8мм HSS",
        price_value=350.0,
        price_currency="RUB",
    )
    db_session.add(existing)
    await db_session.commit()
    existing_price = existing.price_value

    result = await _create_catalog_entries_from_rows(
        db_session,
        supplier.id,
        [_row(price=999.0)],  # same part_number, very different price
        provenance={"discovery_method": "web_discover", "source_url": "https://acme.example/x"},
    )
    await db_session.commit()

    assert result["created"] == 1
    assert result["conflicted"] == 1
    assert len(result["anomaly_ids"]) == 1

    await db_session.refresh(existing)
    assert existing.price_value == existing_price  # untouched

    new_entries = (
        (
            await db_session.execute(
                select(ToolCatalogEntry).where(
                    ToolCatalogEntry.supplier_id == supplier.id,
                    ToolCatalogEntry.id != existing.id,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(new_entries) == 1
    assert new_entries[0].metadata_["review_status"] == "needs_review"
    assert new_entries[0].metadata_["conflicts_with_entry_id"] == str(existing.id)

    anomaly = (
        await db_session.execute(
            select(AnomalyCard).where(AnomalyCard.entity_id == new_entries[0].id)
        )
    ).scalar_one()
    assert anomaly.entity_type == "tool_catalog_entry"
    assert anomaly.details["existing_entry_id"] == str(existing.id)


@pytest.mark.asyncio
async def test_matching_part_number_same_price_and_name_is_not_a_conflict(db_session, supplier):
    """Re-discovering the same, unchanged listing must not spuriously flag a
    conflict every time an exploratory task re-crawls a known supplier."""
    db_session.add(
        ToolCatalogEntry(
            supplier_id=supplier.id,
            part_number="DRL-8MM",
            tool_type=ToolTypeEnum.drill,
            name="Сверло Ø8мм HSS",
            price_value=350.0,
            price_currency="RUB",
        )
    )
    await db_session.commit()

    result = await _create_catalog_entries_from_rows(
        db_session,
        supplier.id,
        [_row(price=350.0)],
        provenance={"discovery_method": "web_discover", "source_url": "https://acme.example/x"},
    )
    await db_session.commit()

    assert result["conflicted"] == 0


@pytest.mark.asyncio
async def test_row_without_tool_type_is_kept_not_skipped(db_session, supplier):
    """Changed deliberately 2026-08-21: requiring `tool_type` per row dropped
    every line of a normal price list (measured live: created=0, skipped=2 on a
    two-row CSV). The type is now derived from the name, falling back to
    "other" — only a row without a NAME is skipped."""
    result = await _create_catalog_entries_from_rows(
        db_session,
        supplier.id,
        [
            {"name": "Фреза концевая Ø8"},  # type inferred
            {"name": "Ящик инструментальный"},  # unrecognised → other
            {"part_number": "NO-NAME"},  # no name → skipped
        ],
    )
    assert result["created"] == 2
    assert result["skipped"] == 1
    assert result["skipped_by_reason"] == {"no_name": 1}


# ── _parse_catalog_text_via_llm ─────────────────────────────────────────────


class _FakeModel:
    model = "m"
    provider = "ollama"


@pytest.mark.asyncio
async def test_parse_catalog_text_returns_rows_from_well_formed_response():
    fake_response = {"rows": [_row()]}
    with (
        patch("app.ai.ollama_client.generate_json", new=AsyncMock(return_value=fake_response)),
        patch("app.ai.model_resolver.get_reasoning_model", return_value=_FakeModel()),
    ):
        rows = await _parse_catalog_text_via_llm("страница с прайс-листом сверл")
    assert rows == [_row()]


@pytest.mark.asyncio
async def test_parse_catalog_text_returns_empty_for_non_catalog_page():
    with (
        patch("app.ai.ollama_client.generate_json", new=AsyncMock(return_value={"rows": []})),
        patch("app.ai.model_resolver.get_reasoning_model", return_value=_FakeModel()),
    ):
        rows = await _parse_catalog_text_via_llm("страница контактов компании")
    assert rows == []


@pytest.mark.asyncio
async def test_parse_catalog_text_never_raises_on_malformed_response():
    with (
        patch(
            "app.ai.ollama_client.generate_json",
            new=AsyncMock(return_value={"unexpected": "shape"}),
        ),
        patch("app.ai.model_resolver.get_reasoning_model", return_value=_FakeModel()),
    ):
        rows = await _parse_catalog_text_via_llm("текст")
    assert rows == []


@pytest.mark.asyncio
async def test_parse_catalog_text_never_raises_when_llm_call_fails():
    with (
        patch(
            "app.ai.ollama_client.generate_json",
            new=AsyncMock(side_effect=RuntimeError("model down")),
        ),
        patch("app.ai.model_resolver.get_reasoning_model", return_value=_FakeModel()),
    ):
        rows = await _parse_catalog_text_via_llm("текст")
    assert rows == []


# ── ingest_web_catalog_source (integration) ─────────────────────────────────


@pytest.mark.asyncio
async def test_ingest_web_catalog_source_creates_entries_with_provenance(db_session, supplier):
    """Ф3 finding: this must take db_session directly (not open its own via
    _get_session_factory()) — it's called synchronously from an HTTP handler
    that already has a request-scoped session, not a detached Celery task."""
    with (
        patch(
            "app.ai.ollama_client.generate_json",
            new=AsyncMock(return_value={"rows": [_row()]}),
        ),
        patch("app.ai.model_resolver.get_reasoning_model", return_value=_FakeModel()),
        patch("app.storage.upload_file"),  # provenance storage is best-effort, not asserted here
    ):
        result = await ingest_web_catalog_source(
            db_session,
            str(supplier.id),
            url="https://acme.example/catalog",
            title="ACME Catalog",
            text="ACME Tools — прайс-лист свёрл...",
        )

    assert result["entries_created"] == 1
    assert result["source_url"] == "https://acme.example/catalog"
    entry = (
        await db_session.execute(
            select(ToolCatalogEntry).where(ToolCatalogEntry.supplier_id == supplier.id)
        )
    ).scalar_one()
    assert entry.metadata_["review_status"] == "ingested"
    assert entry.metadata_["source_url"] == "https://acme.example/catalog"


@pytest.mark.asyncio
async def test_ingest_web_catalog_source_unknown_supplier_reports_error(db_session):
    with (
        patch("app.ai.ollama_client.generate_json", new=AsyncMock(return_value={"rows": []})),
        patch("app.ai.model_resolver.get_reasoning_model", return_value=_FakeModel()),
        patch("app.storage.upload_file"),
    ):
        result = await ingest_web_catalog_source(
            db_session,
            "00000000-0000-0000-0000-000000000000",
            url="https://nowhere.example",
            title=None,
            text="x",
        )
    assert "error" in result


# ── API endpoints ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ingest_web_source_endpoint(client: AsyncClient, supplier):
    with (
        patch(
            "app.ai.ollama_client.generate_json",
            new=AsyncMock(return_value={"rows": [_row()]}),
        ),
        patch("app.ai.model_resolver.get_reasoning_model", return_value=_FakeModel()),
        patch("app.storage.upload_file"),
    ):
        resp = await client.post(
            f"/api/tool-catalog/suppliers/{supplier.id}/ingest-web-source",
            json={"url": "https://acme.example/catalog", "title": "ACME", "text": "прайс-лист"},
        )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["entries_created"] == 1
    assert data["source_url"] == "https://acme.example/catalog"


@pytest.mark.asyncio
async def test_ingest_web_source_unknown_supplier_404(client: AsyncClient):
    import uuid

    resp = await client.post(
        f"/api/tool-catalog/suppliers/{uuid.uuid4()}/ingest-web-source",
        json={"url": "https://x.example", "text": "y"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_approve_entry_clears_review_status(client: AsyncClient, db_session, supplier):
    entry = ToolCatalogEntry(
        supplier_id=supplier.id,
        part_number="DRL-8MM",
        tool_type=ToolTypeEnum.drill,
        name="Сверло Ø8мм HSS",
        metadata_={"review_status": "ingested", "source_url": "https://acme.example/x"},
    )
    db_session.add(entry)
    await db_session.commit()

    resp = await client.post(f"/api/tool-catalog/entries/{entry.id}/approve")
    assert resp.status_code == 200
    data = resp.json()
    assert "review_status" not in (data.get("metadata") or {})
    assert data["metadata"]["source_url"] == "https://acme.example/x"  # other metadata untouched


@pytest.mark.asyncio
async def test_approve_entry_is_idempotent_for_never_gated_entries(
    client: AsyncClient, db_session, supplier
):
    """A manually-created entry (no review_status at all) — approving it is a
    harmless no-op, not an error."""
    entry = ToolCatalogEntry(
        supplier_id=supplier.id,
        tool_type=ToolTypeEnum.drill,
        name="Ручная запись",
    )
    db_session.add(entry)
    await db_session.commit()

    resp = await client.post(f"/api/tool-catalog/entries/{entry.id}/approve")
    assert resp.status_code == 200


# ── Supplier resolution + one-call attach (name → Party → ToolSupplier) ──────


@pytest.fixture
async def party(db_session):
    from app.db.models import Party, PartyRole

    p = Party(name="ООО Мир Станочника", inn="7701234567", role=PartyRole.supplier)
    db_session.add(p)
    await db_session.commit()
    return p


@pytest.mark.asyncio
async def test_resolve_supplier_by_name_creates_linked_tool_supplier(
    client: AsyncClient, db_session, party
):
    """The agent has a NAME, not a UUID — and the legal-form prefix must not
    matter ("Мир Станочника" == "ООО Мир Станочника")."""
    resp = await client.post(
        "/api/tool-catalog/resolve-supplier", json={"supplier_name": "Мир Станочника"}
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["resolved"] is True
    assert data["party_id"] == str(party.id)

    linked = (
        await db_session.execute(
            select(ToolSupplier).where(ToolSupplier.main_supplier_id == party.id)
        )
    ).scalar_one()
    assert str(linked.id) == data["tool_supplier_id"]


@pytest.mark.asyncio
async def test_resolve_supplier_ambiguous_returns_candidates(client: AsyncClient, db_session):
    from app.db.models import Party, PartyRole

    db_session.add_all(
        [
            Party(name="ООО Инструмент Плюс", role=PartyRole.supplier),
            Party(name="АО Инструмент Плюс Сервис", role=PartyRole.supplier),
        ]
    )
    await db_session.commit()

    resp = await client.post(
        "/api/tool-catalog/resolve-supplier", json={"supplier_name": "Инструмент Плюс"}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["resolved"] is False
    assert len(data["candidates"]) == 2  # → the agent asks which one


@pytest.mark.asyncio
async def test_resolve_supplier_unknown_name_is_answerable_not_an_error(client: AsyncClient):
    resp = await client.post(
        "/api/tool-catalog/resolve-supplier", json={"supplier_name": "Нет Такого Поставщика"}
    )
    assert resp.status_code == 200
    assert resp.json()["resolved"] is False


def _fetched(text: str, title: str = "Каталог"):
    from app.api.web_search import WebFetchResponse

    return WebFetchResponse(
        url="https://mirstan.example/catalog.pdf",
        final_url="https://mirstan.example/catalog.pdf",
        status=200,
        title=title,
        text=text,
    )


@pytest.mark.asyncio
async def test_attach_web_catalog_by_supplier_name_creates_entries(
    client: AsyncClient, db_session, party
):
    """The whole live-failed turn in one call: name + url → catalog entries."""
    with (
        patch(
            "app.api.web_search.fetch_page",
            new=AsyncMock(return_value=_fetched("прайс-лист " * 60)),
        ),
        patch(
            "app.ai.ollama_client.generate_json",
            new=AsyncMock(return_value={"rows": [_row()]}),
        ),
        patch("app.ai.model_resolver.get_reasoning_model", return_value=_FakeModel()),
        patch("app.storage.upload_file"),
    ):
        resp = await client.post(
            "/api/tool-catalog/attach-web-catalog",
            json={
                "supplier_name": "ООО Мир Станочника",
                "url": "https://mirstan.example/catalog.pdf",
                "wait": True,
            },
        )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["entries_created"] == 1
    assert data["party_id"] == str(party.id)

    entries = (
        (
            await db_session.execute(
                select(ToolCatalogEntry).where(
                    ToolCatalogEntry.supplier_id == uuid.UUID(data["tool_supplier_id"])
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(entries) == 1
    # Draft-first, exactly like the Ф3 web path it reuses.
    assert entries[0].metadata_["review_status"] == "ingested"


@pytest.mark.asyncio
async def test_attach_web_catalog_empty_page_fails_honestly(client: AsyncClient, party):
    """A JS-only catalog page must not report a successful attach of 0 items."""
    with patch(
        "app.api.web_search.fetch_page", new=AsyncMock(return_value=_fetched("Свяжитесь с нами"))
    ):
        resp = await client.post(
            "/api/tool-catalog/attach-web-catalog",
            json={
                "supplier_name": "ООО Мир Станочника",
                "url": "https://mirstan.example/catalog/",
                "wait": True,
            },
        )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error_code"] == "catalog_source_empty"
    # …and it says which source came back empty, not just that "something" failed.
    assert detail["sources"][0]["status"] == "empty"


@pytest.mark.asyncio
async def test_attach_web_catalog_ambiguous_supplier_asks_instead_of_guessing(
    client: AsyncClient, db_session
):
    from app.db.models import Party, PartyRole

    db_session.add_all(
        [
            Party(name="ООО Резец", role=PartyRole.supplier),
            Party(name="ООО Резец-Инструмент", role=PartyRole.supplier),
        ]
    )
    await db_session.commit()

    resp = await client.post(
        "/api/tool-catalog/attach-web-catalog",
        json={"supplier_name": "Резец", "url": "https://x.example/catalog", "wait": True},
    )
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error_code"] == "supplier_ambiguous"
    assert len(detail["candidates"]) == 2


# ── Discovery + multi-source attach + report block ──────────────────────────


def _search_response(*pairs):
    from app.api.web_search import WebSearchResponse, WebSearchResult

    return WebSearchResponse(
        query="q",
        provider="searxng",
        results=[WebSearchResult(title=title, url=url) for url, title in pairs],
    )


def _page(url: str, *, links=(), text: str = "страница каталога", title: str = "Каталог"):
    from app.api.web_search import PageLink, WebFetchResponse

    return WebFetchResponse(
        url=url,
        final_url=url,
        status=200,
        title=title,
        text=text,
        links=[PageLink(url=u, text=t) for u, t in links],
    )


@pytest.mark.asyncio
async def test_discover_catalogs_combines_search_and_site_scan(client: AsyncClient, party):
    """Neither pass alone is enough: the search misses files only linked from
    the site, the scan misses catalogs hosted elsewhere."""
    site_page = _page(
        "https://mirstan.example/catalog/",
        links=[
            ("https://mirstan.example/files/katalog-frezy.pdf", "Скачать каталог фрез"),
            ("https://mirstan.example/about/", "О компании"),  # not a catalog
        ],
    )
    with (
        patch(
            "app.api.web_search.execute_web_search",
            new=AsyncMock(
                return_value=_search_response(
                    ("https://mirstan.example/files/price-2026.pdf", "Прайс-лист 2026"),
                    ("https://example.org/news", "Новости отрасли"),  # filtered out
                )
            ),
        ),
        patch("app.api.web_search.fetch_page", new=AsyncMock(return_value=site_page)),
    ):
        resp = await client.post(
            "/api/tool-catalog/discover-catalogs",
            json={"supplier_name": "ООО Мир Станочника", "website": "https://mirstan.example"},
        )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    urls = [c["url"] for c in data["candidates"]]
    assert "https://mirstan.example/files/price-2026.pdf" in urls  # from search
    assert "https://mirstan.example/files/katalog-frezy.pdf" in urls  # from site scan
    assert "https://example.org/news" not in urls
    assert "https://mirstan.example/about/" not in urls
    # Files rank above pages — those are what attach cleanly.
    assert data["candidates"][0]["kind"] in {"pdf", "spreadsheet"}
    # "Найди каталоги" (without attaching) also comes with a publishable table
    # whose link column is clickable.
    link_column = next(c for c in data["report"]["columns"] if c["key"] == "url")
    assert link_column["type"] == "link"
    assert data["report"]["rows"][0]["url"]["href"] == data["candidates"][0]["url"]


@pytest.mark.asyncio
async def test_discover_catalogs_reports_nothing_found_honestly(client: AsyncClient, party):
    with (
        patch(
            "app.api.web_search.execute_web_search", new=AsyncMock(return_value=_search_response())
        ),
        patch(
            "app.api.web_search.fetch_page", new=AsyncMock(return_value=_page("https://x.example"))
        ),
    ):
        resp = await client.post(
            "/api/tool-catalog/discover-catalogs",
            json={"supplier_name": "ООО Мир Станочника"},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["candidates"] == []
    # Changed 2026-08-21: "не найдено" left the agent with nothing to do, and it
    # published a table of invoice items instead. With no verified site the
    # answer now says what blocks it and what to ask.
    message = data["message"]
    assert "сайт" in message.lower()
    assert "спроси" in message.lower() or "уточните" in message.lower()


@pytest.mark.asyncio
async def test_attach_multiple_urls_reports_each_source(client: AsyncClient, party):
    """One dead link must not sink the rest, and the caller must see which is which."""
    from fastapi import HTTPException as _HTTPException

    async def _fetch(payload):
        if "broken" in payload.url:
            raise _HTTPException(status_code=502, detail={"message": "unreachable"})
        if "js-only" in payload.url:
            return _page(payload.url, text="Свяжитесь с нами")
        return _page(payload.url, text="прайс-лист " * 60)

    with (
        patch("app.api.web_search.fetch_page", new=AsyncMock(side_effect=_fetch)),
        patch("app.ai.ollama_client.generate_json", new=AsyncMock(return_value={"rows": [_row()]})),
        patch("app.ai.model_resolver.get_reasoning_model", return_value=_FakeModel()),
        patch("app.storage.upload_file"),
    ):
        resp = await client.post(
            "/api/tool-catalog/attach-web-catalog",
            json={
                "supplier_name": "ООО Мир Станочника",
                "wait": True,
                "urls": [
                    "https://mirstan.example/ok.pdf",
                    "https://mirstan.example/js-only/",
                    "https://mirstan.example/broken.pdf",
                ],
            },
        )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    statuses = {s["url"]: s["status"] for s in data["sources"]}
    assert statuses["https://mirstan.example/ok.pdf"] == "attached"
    assert statuses["https://mirstan.example/js-only/"] == "empty"
    assert statuses["https://mirstan.example/broken.pdf"] == "error"
    assert data["entries_created"] == 1


@pytest.mark.asyncio
async def test_attach_report_block_has_clickable_link_column(client: AsyncClient, party):
    """The live session published the URL as a plain text column — not clickable.
    The report the endpoint hands back types it as `link` (grid renders an <a>)."""
    with (
        patch(
            "app.api.web_search.fetch_page",
            new=AsyncMock(
                return_value=_page("https://mirstan.example/ok.pdf", text="прайс " * 100)
            ),
        ),
        patch("app.ai.ollama_client.generate_json", new=AsyncMock(return_value={"rows": [_row()]})),
        patch("app.ai.model_resolver.get_reasoning_model", return_value=_FakeModel()),
        patch("app.storage.upload_file"),
    ):
        resp = await client.post(
            "/api/tool-catalog/attach-web-catalog",
            json={
                "supplier_name": "ООО Мир Станочника",
                "url": "https://mirstan.example/ok.pdf",
                "wait": True,
            },
        )
    report = resp.json()["report"]
    link_column = next(c for c in report["columns"] if c["key"] == "url")
    assert link_column["type"] == "link"
    assert report["rows"][0]["url"]["href"] == "https://mirstan.example/ok.pdf"
    assert report["rows"][0]["entries"] == 1


# ── Chunk selection: budget goes to the catalog, not to the cover page ──────


def test_catalog_density_scores_articles_over_front_matter():
    from app.tasks.drawing_analysis import _catalog_density

    assert _catalog_density("MT245-040G16R03ON05 фреза Ø 40 мм, 12 500 руб") > 0
    assert _catalog_density("Содержание. Система обозначения инструмента ..... 6") == 0


@pytest.mark.asyncio
async def test_parse_catalog_text_spends_budget_on_dense_chunks():
    """A real PDF catalog opens with a cover + contents; a first-N window burns
    the whole LLM budget there and returns nothing (measured live)."""
    from app.tasks import drawing_analysis as da

    front_matter = "Содержание раздела каталога инструмента и оснастки. " * 200
    dense = "MT245-040G16R03ON05 фреза Ø40 мм 12 500 руб. " * 120
    text = front_matter + dense

    seen: list[str] = []

    async def fake_generate_json(prompt, **kwargs):
        seen.append(prompt)
        return {"rows": [_row()]}

    with (
        patch("app.ai.ollama_client.generate_json", new=fake_generate_json),
        patch("app.ai.model_resolver.get_reasoning_model", return_value=_FakeModel()),
    ):
        rows = await da._parse_catalog_text_via_llm(text, max_chunks=2)

    assert rows, "dense catalog text must yield rows"
    assert len(seen) == 2
    assert all("MT245" in prompt for prompt in seen), "budget spent on front matter"


# ── Background ingestion (default path) + progress ──────────────────────────


@pytest.mark.asyncio
async def test_attach_queues_by_default_and_answers_immediately(client: AsyncClient, party):
    """Parsing a real catalog is minutes per fragment — the chat turn must not
    hold the connection (or the GPU) for it."""
    from app.domain.catalog_ingest_status import clear_source_statuses

    files_sent: list[str] = []
    pages_sent: list[list[str]] = []

    class _Task:
        id = "task-123"

    def fake_page_delay(supplier_id, urls, max_pages, max_chunks):
        pages_sent.append(urls)
        return _Task()

    def fake_file_delay(supplier_id, url, *args, **kwargs):
        files_sent.append(url)
        return _Task()

    with (
        patch("app.tasks.drawing_analysis.ingest_web_catalog_sources.delay", fake_page_delay),
        patch("app.tasks.catalog_ingest.ingest_catalog_url.delay", fake_file_delay),
    ):
        resp = await client.post(
            "/api/tool-catalog/attach-web-catalog",
            json={
                "supplier_name": "ООО Мир Станочника",
                "urls": [
                    "https://mirstan.example/a.pdf",
                    "https://mirstan.example/b.pdf",
                    "https://mirstan.example/katalog/",
                ],
            },
        )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "queued"
    assert data["task_id"] == "task-123"
    # A downloadable FILE goes through the document pipeline (page-by-page table
    # extraction, OCR when the text layer is unreadable); a PAGE keeps the
    # text/LLM path. Sending a 44 MB PDF through the page path read a fraction
    # of it and reported success — measured live.
    assert files_sent == ["https://mirstan.example/a.pdf", "https://mirstan.example/b.pdf"]
    assert pages_sent == [["https://mirstan.example/katalog/"]]
    assert [s["status"] for s in data["sources"]] == ["queued", "queued", "queued"]
    # The report is publishable right away — with clickable links.
    assert next(c for c in data["report"]["columns"] if c["key"] == "url")["type"] == "link"

    # …and the same state is readable back as progress.
    status = await client.post(
        "/api/tool-catalog/ingest-status", json={"supplier_name": "ООО Мир Станочника"}
    )
    assert status.status_code == 200
    status_data = status.json()
    assert status_data["in_progress"] is True
    assert len(status_data["sources"]) == 3
    clear_source_statuses(status_data["tool_supplier_id"])


@pytest.mark.asyncio
async def test_ingest_status_reports_finished_totals(client: AsyncClient, party):
    from app.domain.catalog_ingest_status import (
        clear_source_statuses,
        record_source_status,
    )

    resolved = await client.post(
        "/api/tool-catalog/resolve-supplier", json={"supplier_name": "ООО Мир Станочника"}
    )
    supplier_id = resolved.json()["tool_supplier_id"]
    clear_source_statuses(supplier_id)
    record_source_status(
        supplier_id,
        "https://mirstan.example/a.pdf",
        status="attached",
        entries_created=38,
        message="Добавлено позиций: 38",
    )
    record_source_status(
        supplier_id,
        "https://mirstan.example/b.pdf",
        status="empty",
        message="Позиций каталога в тексте не найдено.",
    )

    resp = await client.post(
        "/api/tool-catalog/ingest-status", json={"supplier_name": "ООО Мир Станочника"}
    )
    data = resp.json()
    assert data["in_progress"] is False
    assert data["entries_created"] == 38
    assert "завершена" in data["message"]
    clear_source_statuses(supplier_id)


def test_report_source_name_is_readable():
    """A direct PDF link carries a percent-encoded filename; the report table
    the user reads must not show "%D0%9A%D0%B0%D1%82..."."""
    from app.api.tool_catalog import _readable_source_name

    assert (
        _readable_source_name(
            "https://mirstan.example/uploads/%D0%9A%D0%B0%D1%82%D0%B0%D0%BB%D0%BE%D0%B3.pdf"
        )
        == "Каталог.pdf"
    )
    assert _readable_source_name("https://x.example/a.pdf", "Каталог фрез 2026") == (
        "Каталог фрез 2026"
    )


# ── anomalies: only what a person can act on ────────────────────────────────


@pytest.mark.asyncio
async def test_same_article_on_several_pages_of_one_catalog_is_not_an_anomaly(db_session, supplier):
    """Live result of the old rule: 469 open cards, all of them one article
    printed on several pages of the SAME catalog. Nobody could act on them and
    real anomalies drowned in the noise."""
    from app.db.models import AnomalyCard, ToolCatalogEntry, ToolTypeEnum

    document_id = await _make_catalog_document(db_session)
    db_session.add(
        ToolCatalogEntry(
            supplier_id=supplier.id,
            source_document_id=document_id,
            part_number="1A1-150",
            tool_type=ToolTypeEnum.grinder,
            name="Круг шлифовальный 150",
            price_value=1000.0,
        )
    )
    await db_session.commit()

    result = await _create_catalog_entries_from_rows(
        db_session,
        supplier.id,
        [{"part_number": "1A1-150", "name": "Круг шлифовальный 150 мм", "price": 1200.0}],
        source_document_id=document_id,
        provenance={"discovery_method": "web_discover"},
    )
    await db_session.commit()

    assert result["anomaly_ids"] == []
    cards = (
        (
            await db_session.execute(
                select(AnomalyCard).where(AnomalyCard.entity_type == "tool_catalog_entry")
            )
        )
        .scalars()
        .all()
    )
    assert cards == []


@pytest.mark.asyncio
async def test_material_price_gap_between_two_catalogs_is_reported_once(db_session, supplier):
    """A real event: the same article costs different money in two sources."""
    from app.db.models import AnomalyCard, AnomalyType, ToolCatalogEntry, ToolTypeEnum

    old_catalog = await _make_catalog_document(db_session, "старый.pdf")
    new_catalog = await _make_catalog_document(db_session, "новый.pdf")
    db_session.add(
        ToolCatalogEntry(
            supplier_id=supplier.id,
            source_document_id=old_catalog,
            part_number="DR-8",
            tool_type=ToolTypeEnum.drill,
            name="Сверло Ø8",
            price_value=1000.0,
        )
    )
    await db_session.commit()

    first = await _create_catalog_entries_from_rows(
        db_session,
        supplier.id,
        [{"part_number": "DR-8", "name": "Сверло Ø8", "price": 1400.0}],
        source_document_id=new_catalog,
        provenance={"discovery_method": "web_discover"},
    )
    await db_session.commit()
    assert len(first["anomaly_ids"]) == 1

    card = (
        await db_session.execute(
            select(AnomalyCard).where(AnomalyCard.id == first["anomaly_ids"][0])
        )
    ).scalar_one()
    assert card.anomaly_type == AnomalyType.price_spike
    assert "40%" in card.title, card.title
    assert card.details["old_price"] == 1000.0
    assert card.details["new_price"] == 1400.0

    # Re-parsing the same catalog must not pile a second card on the same article.
    second = await _create_catalog_entries_from_rows(
        db_session,
        supplier.id,
        [{"part_number": "DR-8", "name": "Сверло Ø8", "price": 1400.0}],
        source_document_id=new_catalog,
        provenance={"discovery_method": "web_discover"},
    )
    await db_session.commit()
    assert second["anomaly_ids"] == []


@pytest.mark.asyncio
async def test_rounding_sized_price_difference_is_not_an_anomaly(db_session, supplier):
    """1 % was the old threshold — rounding, VAT and pack sizes tripped it."""
    from app.db.models import ToolCatalogEntry, ToolTypeEnum

    old_catalog = await _make_catalog_document(db_session, "прайс-старый.pdf")
    new_catalog = await _make_catalog_document(db_session, "прайс-новый.pdf")
    db_session.add(
        ToolCatalogEntry(
            supplier_id=supplier.id,
            source_document_id=old_catalog,
            part_number="TP-M8",
            tool_type=ToolTypeEnum.tap,
            name="Метчик М8",
            price_value=1000.0,
        )
    )
    await db_session.commit()

    result = await _create_catalog_entries_from_rows(
        db_session,
        supplier.id,
        [{"part_number": "TP-M8", "name": "Метчик М8", "price": 1020.0}],
        source_document_id=new_catalog,
        provenance={"discovery_method": "web_discover"},
    )
    await db_session.commit()
    assert result["anomaly_ids"] == []


def _age_status(supplier_id: str, url: str, *, hours: int) -> None:
    """Отодвинуть отметку времени записи назад — как будто она давняя."""
    import json
    from datetime import UTC, datetime, timedelta

    from app.domain.catalog_ingest_status import _FALLBACK, _KEY_PREFIX, _redis

    older = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
    client = _redis()
    if client is not None:
        raw = client.hget(f"{_KEY_PREFIX}{supplier_id}", url)
        if raw:
            record = json.loads(raw)
            record["updated_at"] = older
            client.hset(f"{_KEY_PREFIX}{supplier_id}", url, json.dumps(record, ensure_ascii=False))
            return
    record = _FALLBACK.get(supplier_id, {}).get(url)
    if record:
        record["updated_at"] = older


@pytest.mark.asyncio
async def test_ingest_status_does_not_report_catalogs_that_were_deleted(
    client: AsyncClient, db_session
):
    """«Загружено: 312 позиций» про удалённый каталог — ложь, которую агент
    повторит как факт.

    Записи о загрузке живут неделю, а каталог могли удалить за это время.
    Найдено на живом стенде: после полной очистки каталогов ingest_status
    по-прежнему рапортовал о позавчерашних загрузках.
    """
    from app.db.models import Party, ToolSupplier
    from app.domain.catalog_ingest_status import clear_source_statuses, record_source_status

    party = Party(name="ООО Устаревший Статус", inn="7700000123")
    db_session.add(party)
    await db_session.flush()
    supplier = ToolSupplier(name="ООО Устаревший Статус", is_active=True, main_supplier_id=party.id)
    db_session.add(supplier)
    await db_session.commit()

    clear_source_statuses(str(supplier.id))
    record_source_status(
        str(supplier.id),
        "https://example.test/catalog-which-is-gone.pdf",
        status="attached",
        entries_created=312,
        message="Добавлено позиций: 312",
    )
    # Состариваем запись: свежая законно опережает появление документа, и
    # правило её не трогает — устаревает только то, что давно должно было
    # материализоваться в каталог.
    _age_status(str(supplier.id), "https://example.test/catalog-which-is-gone.pdf", hours=5)
    try:
        response = await client.post(
            "/api/tool-catalog/ingest-status", json={"supplier_name": supplier.name}
        )
        assert response.status_code == 200, response.text
        data = response.json()

        assert data["entries_created"] == 0, "позиции удалённого каталога не считаются"
        assert data["sources"][0]["status"] == "stale"
        assert "устарела" in data["sources"][0]["message"]
    finally:
        clear_source_statuses(str(supplier.id))
