"""E40: a walk through the graph never reaches what its user may not see.

Alice (department A) and Bob (department B) each have a document. Both
documents link to one shared supplier node; Bob's document also mentions an
INN, which the regex builder turned into a node of its own with no source.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

from app.auth.acting import get_effective_user
from app.auth.models import UserInfo, UserRole
from app.db.models import (
    Department,
    Document,
    EntityMention,
    EvidenceSpan,
    KnowledgeEdge,
    KnowledgeNode,
    User,
)


def _doc(name: str, **kw) -> Document:
    return Document(
        file_name=name,
        file_hash=uuid.uuid4().hex,
        file_size=1,
        mime_type="application/pdf",
        storage_path=f"/{name}",
        **kw,
    )


def _user(sub: str, role: UserRole = UserRole.buyer) -> UserInfo:
    return UserInfo(sub=sub, email=f"{sub}@x", name=sub, preferred_username=sub, roles=[role])


@pytest.fixture
async def graph(db_session):
    dept_a, dept_b = Department(name="A", code="ga"), Department(name="B", code="gb")
    db_session.add_all([dept_a, dept_b])
    await db_session.flush()
    db_session.add_all(
        [
            User(sub="g:alice", email="a@x", name="A", role="buyer", department_id=dept_a.id),
            User(sub="g:bob", email="b@x", name="B", role="buyer", department_id=dept_b.id),
        ]
    )
    doc_a = _doc("alice.pdf", owner_sub="g:alice", department_id=dept_a.id)
    doc_b = _doc("bob.pdf", owner_sub="g:bob", department_id=dept_b.id)
    db_session.add_all([doc_a, doc_b])
    await db_session.flush()

    def node(title, **kw):
        return KnowledgeNode(node_type=kw.pop("node_type", "entity"), title=title, **kw)

    alice_node = node("Alice doc", node_type="document", source_document_id=doc_a.id)
    bob_node = node("Bob doc", node_type="document", source_document_id=doc_b.id)
    supplier = node("ООО Общий", node_type="supplier")  # company data
    inn = node("7700000001", node_type="inn")  # mention-only, from Bob's text
    db_session.add_all([alice_node, bob_node, supplier, inn])
    await db_session.flush()
    span_b = EvidenceSpan(document_id=doc_b.id, text="ИНН 7700000001")
    db_session.add(span_b)
    await db_session.flush()
    db_session.add(
        EntityMention(
            document_id=doc_b.id, node_id=inn.id, mention_text="7700000001", entity_type="inn"
        )
    )
    edges = {
        "alice_supplier": KnowledgeEdge(
            source_node_id=alice_node.id,
            target_node_id=supplier.id,
            edge_type="from_supplier",
            source_document_id=doc_a.id,
        ),
        "bob_supplier": KnowledgeEdge(
            source_node_id=bob_node.id,
            target_node_id=supplier.id,
            edge_type="from_supplier",
            source_document_id=doc_b.id,
        ),
        "bob_inn": KnowledgeEdge(
            source_node_id=bob_node.id,
            target_node_id=inn.id,
            edge_type="mentions",
            source_document_id=doc_b.id,
            evidence_span_id=span_b.id,
        ),
        # A link between two visible nodes whose only evidence is Bob's text.
        "alice_supplier_by_bob": KnowledgeEdge(
            source_node_id=alice_node.id,
            target_node_id=supplier.id,
            edge_type="same_bank",
            evidence_span_id=span_b.id,
        ),
    }
    db_session.add_all(edges.values())
    await db_session.commit()
    return {
        "alice": alice_node,
        "bob": bob_node,
        "supplier": supplier,
        "inn": inn,
        "edges": edges,
        "doc_b": doc_b,
    }


@pytest.fixture
def as_user(client: AsyncClient):
    from app.main import app

    def bind(user: UserInfo):
        app.dependency_overrides[get_effective_user] = lambda: user

    yield bind
    app.dependency_overrides.pop(get_effective_user, None)


@pytest.mark.asyncio
async def test_alice_through_the_shared_supplier_does_not_reach_bob(client, graph, as_user):
    as_user(_user("g:alice"))
    resp = await client.get(f"/api/graph/nodes/{graph['alice'].id}/neighborhood?depth=3")
    assert resp.status_code == 200, resp.text
    titles = {n["title"] for n in resp.json()["nodes"]}
    assert titles == {"Alice doc", "ООО Общий"}
    edge_types = sorted(e["edge_type"] for e in resp.json()["edges"])
    # The same_bank link exists between two nodes she sees, but its evidence
    # is Bob's text.
    assert edge_types == ["from_supplier"]


@pytest.mark.asyncio
async def test_bobs_nodes_answer_like_missing_ones(client, graph, as_user):
    as_user(_user("g:alice"))
    for node in ("bob", "inn"):
        resp = await client.get(f"/api/graph/nodes/{graph[node].id}")
        assert resp.status_code == 404
    resp = await client.get(
        "/api/graph/path",
        params={"source_node_id": str(graph["alice"].id), "target_node_id": str(graph["bob"].id)},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_the_mention_only_node_is_bobs_and_the_managers(client, graph, as_user):
    as_user(_user("g:bob"))
    assert (await client.get(f"/api/graph/nodes/{graph['inn'].id}")).status_code == 200
    as_user(_user("g:boss", UserRole.manager))
    resp = await client.get(f"/api/graph/nodes/{graph['bob'].id}/neighborhood?depth=2")
    assert {n["title"] for n in resp.json()["nodes"]} == {
        "Bob doc",
        "ООО Общий",
        "7700000001",
        "Alice doc",
    }


@pytest.mark.asyncio
async def test_a_path_does_not_run_through_a_hidden_node(client, graph, as_user, db_session):
    """Alice's node and the supplier are both hers to see, but the only route
    between a second visible node and the supplier goes through Bob's."""
    other = KnowledgeNode(node_type="entity", title="Тупик")
    db_session.add(other)
    await db_session.flush()
    db_session.add(
        KnowledgeEdge(
            source_node_id=other.id,
            target_node_id=graph["bob"].id,
            edge_type="near",
        )
    )
    await db_session.commit()
    as_user(_user("g:alice"))
    resp = await client.get(
        "/api/graph/path",
        params={"source_node_id": str(other.id), "target_node_id": str(graph["supplier"].id)},
    )
    assert resp.status_code == 200
    assert resp.json()["found"] is False


@pytest.mark.asyncio
async def test_a_deleted_source_hides_its_node(client, graph, as_user, db_session):
    from sqlalchemy import update

    # A dangling reference (the source row is gone) must not read as legacy.
    node_id = graph["alice"].id
    await db_session.execute(
        update(KnowledgeNode)
        .where(KnowledgeNode.id == node_id)
        .values(source_document_id=None, entity_type="document", entity_id=uuid.uuid4())
    )
    await db_session.commit()
    db_session.expire_all()
    as_user(_user("g:alice"))
    assert (await client.get(f"/api/graph/nodes/{node_id}")).status_code == 404


@pytest.mark.asyncio
async def test_alice_cannot_link_to_bobs_evidence(client, graph, as_user):
    as_user(_user("g:alice"))
    resp = await client.post(
        "/api/graph/edges",
        json={
            "source_node_id": str(graph["alice"].id),
            "target_node_id": str(graph["supplier"].id),
            "edge_type": "related",
            "source_document_id": str(graph["doc_b"].id),
        },
    )
    assert resp.status_code == 404
