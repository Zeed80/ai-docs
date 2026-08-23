"""Search by picture: what it must do, and what it must never pretend.

The one behaviour worth pinning above all others: when the embedding sidecar
is unavailable, a photo search must SAY so and return nothing — not quietly
answer from the text index. A person who uploaded a photo and got word matches
back has no way to tell the feature is off.
"""

from __future__ import annotations

import base64
import io
import uuid

import pytest
from sqlalchemy import select

from app.api.catalogs import search_catalog_visually
from app.db.models import Party, ToolCatalogEntry, ToolSupplier, ToolTypeEnum
from app.domain.catalogs import CatalogVisualSearchRequest


def _png(color: tuple[int, int, int] = (200, 30, 30)) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (48, 48), color).save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture
async def indexed_position(db_session):
    party = Party(name="ООО Картинки", inn="7700000077")
    db_session.add(party)
    await db_session.flush()
    supplier = ToolSupplier(name="ООО Картинки", is_active=True, main_supplier_id=party.id)
    db_session.add(supplier)
    await db_session.flush()
    entry = ToolCatalogEntry(
        supplier_id=supplier.id,
        part_number="FR-12",
        name="Фреза концевая 12 мм",
        tool_type=ToolTypeEnum.endmill,
        is_active=True,
        image_path="tool-catalogs/x/crops/0007_1.webp",
        image_kind="crop",
        metadata_={"visual_indexed_at": "2026-08-23T00:00:00Z", "visual_model": "test-model"},
    )
    db_session.add(entry)
    await db_session.commit()
    return entry


@pytest.mark.asyncio
async def test_unavailable_sidecar_says_so_instead_of_answering_from_text(
    db_session, indexed_position, monkeypatch
):
    """A down sidecar must be visible, not papered over."""
    import app.ai.vl_embeddings as vl

    async def _no_info():
        return None

    monkeypatch.setattr(vl, "vl_info", _no_info)

    result = await search_catalog_visually(
        CatalogVisualSearchRequest(image_base64=base64.b64encode(_png()).decode()),
        db_session,
    )

    assert result.available is False
    assert result.items == []
    # And it still reports how much of the catalog WOULD be searchable, so the
    # message can be specific rather than "что-то пошло не так".
    assert result.indexed_positions >= 1
    assert result.report and "недоступен" in result.report["title"].lower()


@pytest.mark.asyncio
async def test_photo_finds_the_position_behind_it(db_session, indexed_position, monkeypatch):
    import app.ai.vl_embeddings as vl
    import app.api.catalogs as catalogs_api

    async def _info():
        return {"model": "test-model", "dim": 4}

    async def _embed_query(text=None, image=None):
        return [0.1, 0.2, 0.3, 0.4]

    def _search(vector, **kwargs):
        return [{"entry_id": str(indexed_position.id), "score": 0.81, "payload": {}}]

    monkeypatch.setattr(vl, "vl_info", _info)
    monkeypatch.setattr(vl, "embed_query", _embed_query)
    monkeypatch.setattr(catalogs_api, "search_visual_catalog", _search, raising=False)
    monkeypatch.setattr(
        "app.vector.qdrant_store.search_visual_catalog", _search, raising=False
    )

    result = await search_catalog_visually(
        CatalogVisualSearchRequest(image_base64=base64.b64encode(_png()).decode()),
        db_session,
    )

    assert result.available is True
    assert result.mode == "image"
    assert [item.part_number for item in result.items] == ["FR-12"]
    assert result.scores[str(indexed_position.id)] == pytest.approx(0.81)


@pytest.mark.asyncio
async def test_similar_by_entry_never_returns_the_position_itself(
    db_session, indexed_position, monkeypatch
):
    """«Похожие на эту» answering with "this one" is the classic own-goal."""
    import app.ai.vl_embeddings as vl

    captured: dict = {}

    async def _info():
        return {"model": "test-model", "dim": 4}

    async def _embed_query(text=None, image=None):
        return [0.1, 0.2, 0.3, 0.4]

    def _search(vector, **kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(vl, "vl_info", _info)
    monkeypatch.setattr(vl, "embed_query", _embed_query)
    monkeypatch.setattr(
        "app.vector.qdrant_store.search_visual_catalog", _search, raising=False
    )

    await search_catalog_visually(
        CatalogVisualSearchRequest(entry_id=indexed_position.id), db_session
    )

    assert captured.get("exclude_entry_id") == str(indexed_position.id)


@pytest.mark.asyncio
async def test_a_request_without_picture_text_or_entry_is_refused(db_session):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await search_catalog_visually(CatalogVisualSearchRequest(), db_session)
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_broken_base64_is_a_clear_422_not_a_500(db_session):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await search_catalog_visually(
            CatalogVisualSearchRequest(image_base64="это не base64!!"), db_session
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["error_code"] == "image_not_base64"


@pytest.mark.asyncio
async def test_indexing_skips_what_is_already_done_for_this_model(db_session, indexed_position):
    """The checkpoint lives in the row, so a re-run resumes instead of
    re-embedding thousands of pictures — and a MODEL change re-indexes."""
    from app.tasks.catalog_visual import INDEXED_KEY, MODEL_KEY

    def _pending(model_name: str):
        from sqlalchemy import or_

        return select(ToolCatalogEntry).where(
            ToolCatalogEntry.is_active.is_(True),
            ToolCatalogEntry.image_path.isnot(None),
            or_(
                ToolCatalogEntry.metadata_.is_(None),
                ToolCatalogEntry.metadata_[INDEXED_KEY].as_string().is_(None),
                ToolCatalogEntry.metadata_[MODEL_KEY].as_string() != model_name,
            ),
        )

    same_model = (await db_session.execute(_pending("test-model"))).scalars().all()
    other_model = (await db_session.execute(_pending("another-model"))).scalars().all()

    assert indexed_position.id not in {row.id for row in same_model}
    assert indexed_position.id in {row.id for row in other_model}


@pytest.mark.asyncio
async def test_reranking_is_off_unless_asked_for(db_session, indexed_position, monkeypatch):
    """Measured on this stand: reranking gave the same top-1 (14/25) for 55x the
    latency, because inside a catalog family every crop is the same picture.
    It stays available, but nobody pays for it by default."""
    import app.ai.vl_embeddings as vl

    called: list[str] = []

    async def _info():
        return {"model": "test-model", "dim": 4}

    async def _embed_query(text=None, image=None):
        return [0.1, 0.2, 0.3, 0.4]

    async def _rerank(**kwargs):
        called.append("rerank")
        return [0.9] * len(kwargs["documents"])

    def _search(vector, **kwargs):
        return [{"entry_id": str(indexed_position.id), "score": 0.7, "payload": {}}]

    monkeypatch.setattr(vl, "vl_info", _info)
    monkeypatch.setattr(vl, "embed_query", _embed_query)
    monkeypatch.setattr(vl, "rerank_candidates", _rerank)
    monkeypatch.setattr(
        "app.vector.qdrant_store.search_visual_catalog", _search, raising=False
    )

    result = await search_catalog_visually(
        CatalogVisualSearchRequest(query="фреза"), db_session
    )
    assert result.reranked is False
    assert called == []


@pytest.mark.asyncio
async def test_deleting_a_catalog_also_drops_its_visual_vectors(
    db_session, indexed_position, monkeypatch
):
    """The visual vectors live in their OWN Qdrant collection, so deleting the
    text ones is half the job: a deleted catalog went on answering photo
    searches with positions that no longer existed."""
    from app.db.models import Document, DocumentStatus, DocumentType

    doc = Document(
        file_name="Каталог-картинки.pdf",
        file_hash=uuid.uuid4().hex,
        file_size=1024,
        mime_type="application/pdf",
        storage_path="tool-catalogs/x/ab/abcd/Каталог-картинки.pdf",
        doc_type=DocumentType.supplier_catalog,
        status=DocumentStatus.ingested,
    )
    db_session.add(doc)
    await db_session.flush()
    indexed_position.source_document_id = doc.id
    await db_session.commit()

    dropped: list[list[str]] = []

    def _drop(entry_ids):
        dropped.append(list(entry_ids))

    monkeypatch.setattr(
        "app.vector.qdrant_store.delete_visual_catalog_entries", _drop, raising=False
    )
    monkeypatch.setattr(
        "app.vector.qdrant_store.delete_tool_catalog_entry", lambda _id: None, raising=False
    )

    from app.api.catalogs import delete_catalog

    await delete_catalog(doc.id, mode="data", db=db_session)

    assert dropped and str(indexed_position.id) in dropped[0]
