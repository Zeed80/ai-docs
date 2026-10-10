"""E41: document rights inside the vector index (real Qdrant).

A temporary documents__ collection holds Alice's and Bob's chunks and one
point written before acl existed. A restricted search sees only what its
user may; the unstamped point stays out until it is stamped.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.auth.models import UserInfo, UserRole
from app.db.models import Department, Document, DocumentType, User
from app.vector import acl as vector_acl


def _user(sub: str, role: UserRole = UserRole.buyer) -> UserInfo:
    return UserInfo(sub=sub, email=f"{sub}@x", name=sub, preferred_username=sub, roles=[role])


@pytest.fixture
def qdrant():
    from qdrant_client.models import Distance, PointStruct, VectorParams

    from app.vector.qdrant_store import get_client

    try:
        client = get_client()
        client.get_collections()
    except Exception:  # noqa: BLE001
        pytest.skip("Qdrant is not reachable")
    name = f"documents__acltest_{uuid.uuid4().hex[:8]}"
    client.create_collection(name, vectors_config=VectorParams(size=3, distance=Distance.COSINE))

    def put(doc_id, n, extra=None):
        client.upsert(
            name,
            points=[
                PointStruct(
                    id=str(uuid.uuid4()),
                    vector=[1.0, 0.1 * n, 0.0],
                    payload={"document_id": str(doc_id), "n": n, **(extra or {})},
                )
            ],
        )

    yield client, name, put
    client.delete_collection(name)


@pytest.fixture
async def docs(db_session):
    dept_a, dept_b = Department(name="A", code="va-a"), Department(name="B", code="va-b")
    db_session.add_all([dept_a, dept_b])
    await db_session.flush()
    db_session.add_all(
        [
            User(sub="va:alice", email="a@x", name="A", role="buyer", department_id=dept_a.id),
            User(sub="va:bob", email="b@x", name="B", role="buyer", department_id=dept_b.id),
        ]
    )

    def doc(name, **kw):
        return Document(
            file_name=name,
            file_hash=uuid.uuid4().hex,
            file_size=1,
            mime_type="application/pdf",
            storage_path=f"/{name}",
            **kw,
        )

    rows = {
        "alice": doc("a.pdf", owner_sub="va:alice", department_id=dept_a.id),
        "bob": doc("b.pdf", owner_sub="va:bob", department_id=dept_b.id),
        "catalog": doc("c.pdf", owner_sub="va:bob", doc_type=DocumentType.supplier_catalog),
    }
    db_session.add_all(rows.values())
    await db_session.commit()
    return rows


def _search(client, name, acl):
    from app.vector.qdrant_store import search_similar

    hits = search_similar([1.0, 0.0, 0.0], limit=20, collection_name=name, acl=acl)
    return sorted(h["payload"]["n"] for h in hits)


@pytest.mark.asyncio
async def test_a_restricted_search_sees_only_its_documents(qdrant, docs, db_session, monkeypatch):
    client, name, put = qdrant
    monkeypatch.setattr(vector_acl, "_document_collections", lambda _c: [name])
    for n, key in enumerate(("alice", "bob", "catalog"), start=1):
        put(docs[key].id, n)
    put(uuid.uuid4(), 9)  # a point of a document the index knows nothing about

    # Before stamping: quarantined for a restricted user, all for a manager.
    assert _search(client, name, await vector_acl.acl_filter(db_session, _user("va:alice"))) == []
    assert await vector_acl.acl_filter(db_session, _user("m", UserRole.manager)) is None

    rows = [(d.id, d.owner_sub, d.department_id, d.doc_type) for d in docs.values()]
    assert vector_acl.stamp_documents(rows) == 3

    alice = await vector_acl.acl_filter(db_session, _user("va:alice"))
    assert _search(client, name, alice) == [1, 3]  # her own and the shared catalog
    bob = await vector_acl.acl_filter(db_session, _user("va:bob"))
    assert _search(client, name, bob) == [2, 3]


@pytest.mark.asyncio
async def test_changing_an_owner_enqueues_a_restamp(docs, db_session, monkeypatch):
    sent: list = []

    class _Task:
        @staticmethod
        def delay(ids):
            sent.append(ids)

    monkeypatch.setattr("app.tasks.vector_acl.sync_document_acl_task", _Task)
    vector_acl.install_change_capture()
    try:
        doc = await db_session.scalar(select(Document).where(Document.id == docs["bob"].id))
        doc.owner_sub = "va:alice"
        await db_session.commit()
        assert sent == [[str(docs["bob"].id)]]

        doc.file_name = "renamed.pdf"  # not a rights change
        await db_session.commit()
        assert len(sent) == 1
    finally:
        vector_acl.uninstall_change_capture()


def test_deleting_a_document_removes_its_points_everywhere(qdrant, monkeypatch):
    from app.vector.qdrant_store import delete_document

    client, name, put = qdrant
    monkeypatch.setattr(vector_acl, "_document_collections", lambda _c: [name])
    gone, kept = uuid.uuid4(), uuid.uuid4()
    put(gone, 1)
    put(gone, 2)
    put(kept, 3)
    delete_document(str(gone))
    assert _search(client, name, None) == [3]


def test_a_full_sync_purges_points_of_vanished_documents(qdrant, monkeypatch):
    client, name, put = qdrant
    monkeypatch.setattr(vector_acl, "_document_collections", lambda _c: [name])
    live, vanished = uuid.uuid4(), uuid.uuid4()
    put(live, 1)
    put(vanished, 2)
    client.upsert(  # a point that belongs to no document is left alone
        name,
        points=[
            __import__("qdrant_client").models.PointStruct(
                id=str(uuid.uuid4()), vector=[1.0, 0.3, 0.0], payload={"n": 3}
            )
        ],
    )
    assert vector_acl.purge_orphans({str(live)}) == 1
    assert _search(client, name, None) == [1, 3]
