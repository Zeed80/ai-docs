"""E44: deletion and revocation reach every derived store."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.auth.models import UserInfo, UserRole
from app.db.models import AuditLog, Document, KnowledgeNode
from app.domain.graph_access import GraphAccess


def _user(sub, role=UserRole.buyer):
    return UserInfo(sub=sub, email=f"{sub}@x", name=sub, preferred_username=sub, roles=[role])


def _doc(name, **kw):
    return Document(
        file_name=name,
        file_hash=uuid.uuid4().hex,
        file_size=1,
        mime_type="application/pdf",
        storage_path=f"/{name}",
        **kw,
    )


@pytest.mark.asyncio
async def test_a_regex_node_outliving_its_documents_is_nobodys(db_session):
    """Its title is text from documents that are gone: it must not turn public."""
    leftover = KnowledgeNode(
        node_type="inn", title="7700000099", metadata_={"method": "deterministic_regex"}
    )
    manual = KnowledgeNode(node_type="supplier", title="ООО Ручной")
    db_session.add_all([leftover, manual])
    await db_session.commit()
    visible = await GraphAccess(db_session, _user("anyone")).visible_nodes([leftover, manual])
    assert visible == {manual.id}


@pytest.mark.asyncio
async def test_deleting_twice_is_safe_and_audited_without_text(client, db_session, monkeypatch):
    erased: list = []
    monkeypatch.setattr(
        "app.domain.document_deletion._delete_storage_paths",
        lambda paths: erased.append(("files", tuple(paths))) or len(paths),
    )
    monkeypatch.setattr(
        "app.domain.document_deletion._delete_qdrant_document",
        lambda doc_id: erased.append(("vectors", str(doc_id))),
    )
    doc = _doc("secret-contract.pdf")
    db_session.add(doc)
    await db_session.commit()

    first = await client.delete(f"/api/documents/{doc.id}")
    assert first.status_code == 200
    assert ("vectors", str(doc.id)) in erased
    assert (await client.delete(f"/api/documents/{doc.id}")).status_code == 404

    entry = await db_session.scalar(
        select(AuditLog).where(AuditLog.action == "doc.delete", AuditLog.entity_id == doc.id)
    )
    assert entry is not None
    assert "secret-contract" not in str(entry.details)


@pytest.mark.asyncio
async def test_files_and_vectors_are_erased_only_after_the_deletion_commits(
    test_engine, monkeypatch
):
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.domain.document_deletion import hard_delete_document

    erased: list = []
    monkeypatch.setattr(
        "app.domain.document_deletion._delete_storage_paths",
        lambda paths: erased.append("files") or 0,
    )
    monkeypatch.setattr(
        "app.domain.document_deletion._delete_qdrant_document",
        lambda doc_id: erased.append("vectors"),
    )
    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        doc = _doc("kept.pdf")
        db.add(doc)
        await db.commit()
        doc_id = doc.id

        await hard_delete_document(db, doc_id)
        await db.rollback()
        assert erased == []
        assert await db.get(Document, doc_id) is not None

        await hard_delete_document(db, doc_id)
        assert erased == []  # not before the commit
        await db.commit()
        assert sorted(erased) == ["files", "vectors"]
        assert await db.get(Document, doc_id) is None


@pytest.mark.asyncio
async def test_a_vector_hit_on_a_deleted_document_is_dropped_even_if_the_index_lags(
    db_session, monkeypatch
):
    """Qdrant was down when the document was deleted: the read still fails closed."""
    import app.domain.table_spec as ts

    async def fake_embed(text, task_type="passage"):
        return [0.1]

    def fake_search(vector, **kw):
        return [
            {
                "doc_id": str(uuid.uuid4()),
                "score": 0.9,
                "file_name": "gone.pdf",
                "payload": {"text": "секрет удалённого документа"},
            }
        ]

    monkeypatch.setattr("app.ai.embeddings.embed_text", fake_embed)
    monkeypatch.setattr("app.vector.qdrant_store.search_similar", fake_search)
    spec = ts.TableSpec(
        source="vector_search",
        columns=[ts.ColumnSpec(field="file_name")],
        filters=[ts.FilterSpec(field="query", op="contains", value="секрет")],
    )
    manager = _user("m", UserRole.manager)
    assert (await ts.execute_spec(db_session, spec, viewer=manager)).total == 0
