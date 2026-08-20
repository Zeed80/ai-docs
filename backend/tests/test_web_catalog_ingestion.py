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
        await db_session.execute(select(ToolCatalogEntry).where(ToolCatalogEntry.supplier_id == supplier.id))
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
        await db_session.execute(select(ToolCatalogEntry).where(ToolCatalogEntry.supplier_id == supplier.id))
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
        await db_session.execute(
            select(ToolCatalogEntry).where(
                ToolCatalogEntry.supplier_id == supplier.id,
                ToolCatalogEntry.id != existing.id,
            )
        )
    ).scalars().all()
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
async def test_row_missing_required_fields_is_skipped_not_created(db_session, supplier):
    result = await _create_catalog_entries_from_rows(db_session, supplier.id, [{"name": "Без типа"}])
    assert result["created"] == 0
    assert result["skipped"] == 1


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
        patch("app.ai.ollama_client.generate_json", new=AsyncMock(return_value={"unexpected": "shape"})),
        patch("app.ai.model_resolver.get_reasoning_model", return_value=_FakeModel()),
    ):
        rows = await _parse_catalog_text_via_llm("текст")
    assert rows == []


@pytest.mark.asyncio
async def test_parse_catalog_text_never_raises_when_llm_call_fails():
    with (
        patch("app.ai.ollama_client.generate_json", new=AsyncMock(side_effect=RuntimeError("model down"))),
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
        await db_session.execute(select(ToolCatalogEntry).where(ToolCatalogEntry.supplier_id == supplier.id))
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
async def test_approve_entry_is_idempotent_for_never_gated_entries(client: AsyncClient, db_session, supplier):
    """A manually-created entry (no review_status at all) — approving it is a
    harmless no-op, not an error."""
    entry = ToolCatalogEntry(
        supplier_id=supplier.id, tool_type=ToolTypeEnum.drill, name="Ручная запись",
    )
    db_session.add(entry)
    await db_session.commit()

    resp = await client.post(f"/api/tool-catalog/entries/{entry.id}/approve")
    assert resp.status_code == 200
