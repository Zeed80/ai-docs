"""E39: the agent's SQL sees only the rows its user may see.

Postgres applies the policies (``app.ai.sql_row_security``) to the reader
roles; the pipeline records who the transaction reads for. Alice works in
department A, Bob in department B; Bob has a personal mailbox, a private
memory fact and a chat. The manager sees company documents but not Bob's
private things.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai import data_access, table_sql_pipeline
from app.ai.actor_context import set_acting_user
from app.ai.sql_row_security import COMPANY_WIDE, RULES, bind_actor, install_row_security
from app.db.models import (
    Base,
    ChatSession,
    Department,
    Document,
    EmailMessage,
    Invoice,
    MailboxConfig,
    MemoryFact,
    User,
)

# Columns that tie a row to a person, a document or another private object.
_MARKERS = {
    "owner_sub",
    "owner_key",
    "user_key",
    "user_sub",
    "user_id",
    "created_by",
    "document_id",
    "source_document_id",
    "invoice_id",
    "work_order_id",
    "session_id",
    "mailbox",
    "thread_id",
    "case_id",
    "entity_id",
    "scope",
    "room_id",
    "drawing_id",
    "feature_id",
    "extraction_id",
    "process_plan_id",
    "collection_id",
    "bom_id",
    "receipt_id",
    "source_email_id",
    "step_id",
}
# Marker columns that do not mean ownership in these tables.
_NOT_OWNERSHIP = {
    "audit_logs",  # entity rule covers it; user_id is the actor
    "normative_clauses",
    "normative_requirements",
}


def test_every_table_with_an_owner_has_a_rule_or_a_reason():
    unclassified = []
    for table in Base.metadata.sorted_tables:
        if not (_MARKERS & set(table.columns.keys())):
            continue
        name = table.name
        if name in RULES or name in COMPANY_WIDE or name in data_access.SECRET_TABLES:
            continue
        if name in _NOT_OWNERSHIP:
            continue
        unclassified.append(name)
    assert unclassified == []


def test_rules_name_real_tables_only():
    tables = set(Base.metadata.tables)
    assert sorted(set(RULES) - tables) == []
    assert sorted(set(RULES) & set(COMPANY_WIDE)) == []


@pytest.fixture
def factory(test_engine):
    return async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)


def _doc(name: str, **kw) -> Document:
    return Document(
        file_name=name,
        file_hash=uuid.uuid4().hex,
        file_size=1,
        mime_type="application/pdf",
        storage_path=f"/{name}",
        **kw,
    )


@pytest.fixture
async def world(factory, monkeypatch):
    monkeypatch.setattr("app.db.session._get_session_factory", lambda: factory)
    tag = uuid.uuid4().hex[:8]
    alice, bob, boss = f"alice-{tag}", f"bob-{tag}", f"boss-{tag}"
    async with factory() as db:
        assert await install_row_security(db, force=True)
        await data_access.sync_full_reader_grants(db)
        dept_a = Department(name="A", code=f"a-{tag}")
        dept_b = Department(name="B", code=f"b-{tag}")
        db.add_all([dept_a, dept_b])
        await db.flush()
        db.add_all(
            [
                User(sub=alice, email="a@x", name="A", role="engineer", department_id=dept_a.id),
                User(sub=bob, email="b@x", name="B", role="engineer", department_id=dept_b.id),
                User(sub=boss, email="m@x", name="M", role="manager"),
            ]
        )
        legacy = _doc(f"rls-{tag}-legacy")
        doc_a = _doc(f"rls-{tag}-a", owner_sub=alice, department_id=dept_a.id)
        doc_b = _doc(f"rls-{tag}-b", owner_sub=bob, department_id=dept_b.id)
        # Owned without a department: private to its owner, not "legacy".
        doc_b_private = _doc(f"rls-{tag}-b-private", owner_sub=bob)
        db.add_all([legacy, doc_a, doc_b, doc_b_private])
        await db.flush()
        db.add_all(
            [
                Invoice(document_id=doc_a.id, invoice_number=f"rls-{tag}-inv-a"),
                Invoice(document_id=doc_b.id, invoice_number=f"rls-{tag}-inv-b"),
            ]
        )
        db.add(
            MailboxConfig(
                name=f"bob-box-{tag}",
                imap_host="imap.x",
                imap_user="bob",
                imap_password_encrypted="x",
                mailbox_type="personal",
                owner_sub=bob,
                sweep_enabled=True,
            )
        )
        db.add_all(
            [
                EmailMessage(mailbox=f"bob-box-{tag}", from_address="x@y", subject=f"rls-{tag}-p"),
                EmailMessage(mailbox=f"shared-{tag}", from_address="x@y", subject=f"rls-{tag}-s"),
            ]
        )
        for scope in (f"owner:{bob}", f"department:{dept_b.id}", "project"):
            db.add(
                MemoryFact(
                    scope=scope,
                    kind="fact",
                    title=f"rls-{tag}-{scope}",
                    summary="x",
                    source="test",
                    confidence=1.0,
                    pinned=False,
                )
            )
        db.add(ChatSession(user_key=bob, title=f"rls-{tag}-chat"))
        await db.commit()
    yield {"tag": tag, "alice": alice, "bob": bob, "boss": boss}
    # The rows were committed for the pipeline's own sessions; other tests
    # compare exact sets over the same tables, so take them out again.
    async with factory() as db:
        await _cleanup(db, tag, [alice, bob, boss])
        await db.commit()


async def _cleanup(db, tag: str, subs: list[str]) -> None:
    pattern = f"rls-{tag}-%"
    for sql in (
        "DELETE FROM invoices WHERE invoice_number LIKE :p",
        "DELETE FROM documents WHERE file_name LIKE :p",
        "DELETE FROM email_messages WHERE subject LIKE :p",
        "DELETE FROM memory_facts WHERE title LIKE :p",
        "DELETE FROM chat_sessions WHERE title LIKE :p",
    ):
        await db.execute(text(sql), {"p": pattern})
    await db.execute(text("DELETE FROM mailbox_configs WHERE name = :n"), {"n": f"bob-box-{tag}"})
    await db.execute(text("DELETE FROM users WHERE sub = ANY(:s)"), {"s": subs})
    await db.execute(
        text("DELETE FROM departments WHERE code = ANY(:c)"), {"c": [f"a-{tag}", f"b-{tag}"]}
    )


async def _query(sql: str, actor: str | None, *, full: bool, monkeypatch) -> list:
    monkeypatch.setattr(data_access, "sql_full_access_enabled", lambda: full)
    set_acting_user(actor)
    try:
        rows = await table_sql_pipeline.execute_sql(sql, max_rows=50)
    finally:
        set_acting_user(None)
    return sorted(next(iter(row.values())) for row in rows)


@pytest.mark.asyncio
async def test_documents_follow_the_api_rules(world, monkeypatch):
    t = world["tag"]
    sql = f"SELECT file_name FROM documents WHERE file_name LIKE 'rls-{t}-%'"
    for full in (False, True):
        alice = await _query(sql, world["alice"], full=full, monkeypatch=monkeypatch)
        assert alice == [f"rls-{t}-a", f"rls-{t}-legacy"]
        bob = await _query(sql, world["bob"], full=full, monkeypatch=monkeypatch)
        assert bob == [f"rls-{t}-b", f"rls-{t}-b-private", f"rls-{t}-legacy"]
        boss = await _query(sql, world["boss"], full=full, monkeypatch=monkeypatch)
        assert len(boss) == 4
        # Nobody bound: only rows without an owner.
        assert await _query(sql, None, full=full, monkeypatch=monkeypatch) == [f"rls-{t}-legacy"]


@pytest.mark.asyncio
async def test_invoices_follow_their_document(world, monkeypatch):
    t = world["tag"]
    sql = f"SELECT invoice_number FROM invoices WHERE invoice_number LIKE 'rls-{t}-%'"
    assert await _query(sql, world["alice"], full=False, monkeypatch=monkeypatch) == [
        f"rls-{t}-inv-a"
    ]
    assert len(await _query(sql, world["boss"], full=False, monkeypatch=monkeypatch)) == 2


@pytest.mark.asyncio
async def test_private_mail_memory_and_chats_stay_with_their_owner(world, monkeypatch):
    t, bob = world["tag"], world["bob"]
    mail = f"SELECT subject FROM email_messages WHERE subject LIKE 'rls-{t}-%'"
    assert await _query(mail, world["alice"], full=True, monkeypatch=monkeypatch) == [f"rls-{t}-s"]
    # Not even a manager reads a colleague's personal mailbox.
    assert await _query(mail, world["boss"], full=True, monkeypatch=monkeypatch) == [f"rls-{t}-s"]
    assert await _query(mail, bob, full=True, monkeypatch=monkeypatch) == [
        f"rls-{t}-p",
        f"rls-{t}-s",
    ]

    memory = f"SELECT title FROM memory_facts WHERE title LIKE 'rls-{t}-%'"
    assert await _query(memory, world["alice"], full=True, monkeypatch=monkeypatch) == [
        f"rls-{t}-project"
    ]
    assert len(await _query(memory, bob, full=True, monkeypatch=monkeypatch)) == 3

    chats = f"SELECT title FROM chat_sessions WHERE title LIKE 'rls-{t}-%'"
    assert await _query(chats, world["boss"], full=True, monkeypatch=monkeypatch) == []
    assert await _query(chats, bob, full=True, monkeypatch=monkeypatch) == [f"rls-{t}-chat"]


@pytest.mark.asyncio
async def test_the_reader_cannot_rebind_itself(world, factory):
    """The actor row is written by the owner; the reader can neither write nor
    read it, so SQL written by the model cannot become someone else."""
    for role in ("agent_sql_reader", data_access.FULL_READER_ROLE):
        for sql in (
            "INSERT INTO agent_sql_actors (xid, user_sub, unrestricted) "
            "VALUES (pg_current_xact_id(), 'x', true)",
            "UPDATE agent_sql_actors SET unrestricted = true",
            "SELECT * FROM agent_sql_actors",
        ):
            async with factory() as db:
                await bind_actor(db, world["alice"])
                await db.execute(text(f"SET LOCAL ROLE {role}"))
                with pytest.raises(Exception, match="permission denied"):
                    await db.execute(text(sql))
                await db.rollback()


@pytest.mark.asyncio
async def test_no_reader_role_means_no_query(world, monkeypatch):
    """Under the owner role the policies do not apply: refuse instead."""
    monkeypatch.setattr(table_sql_pipeline, "_READER_ROLE", "no_such_reader_role")
    with pytest.raises(Exception):  # noqa: B017 — the role error itself
        await _query(
            "SELECT id FROM documents", world["alice"], full=False, monkeypatch=monkeypatch
        )


@pytest.mark.asyncio
async def test_install_is_skipped_when_current(world, factory):
    async with factory() as db:
        assert await install_row_security(db) is False
        await db.rollback()


@pytest.mark.asyncio
async def test_a_mention_only_graph_node_follows_the_documents_that_mention_it(
    world, factory, monkeypatch
):
    """E40 in SQL: the INN node from Bob's document is Bob's, not Alice's."""
    from app.db.models import EntityMention, KnowledgeNode

    t = world["tag"]
    async with factory() as db:
        doc_b = await db.scalar(
            text("SELECT id FROM documents WHERE file_name = :n"), {"n": f"rls-{t}-b"}
        )
        node = KnowledgeNode(node_type="inn", title=f"rls-{t}-inn")
        db.add(node)
        await db.flush()
        db.add(
            EntityMention(
                document_id=doc_b, node_id=node.id, mention_text="7700", entity_type="inn"
            )
        )
        await db.commit()
        node_id = node.id
    sql = f"SELECT title FROM knowledge_nodes WHERE title = 'rls-{t}-inn'"
    try:
        assert await _query(sql, world["alice"], full=True, monkeypatch=monkeypatch) == []
        assert await _query(sql, world["bob"], full=True, monkeypatch=monkeypatch) == [
            f"rls-{t}-inn"
        ]
    finally:
        async with factory() as db:
            await db.execute(text("DELETE FROM entity_mentions WHERE node_id = :i"), {"i": node_id})
            await db.execute(text("DELETE FROM knowledge_nodes WHERE id = :i"), {"i": node_id})
            await db.commit()


@pytest.mark.asyncio
async def test_an_email_draft_in_a_personal_mailbox_is_its_owners(world, factory, monkeypatch):
    from app.db.models import DraftAction

    t, bob = world["tag"], world["bob"]
    async with factory() as db:
        draft = DraftAction(
            action_type="email.send",
            entity_type="email",
            draft_data={
                "subject": f"rls-{t}-draft",
                "mailbox": f"bob-box-{t}",
                "created_by_sub": bob,
            },
        )
        db.add(draft)
        await db.commit()
        draft_id = draft.id
    sql = (
        "SELECT draft_data::jsonb ->> 'subject' AS s FROM draft_actions "
        f"WHERE draft_data::jsonb ->> 'subject' = 'rls-{t}-draft'"
    )
    try:
        assert await _query(sql, world["alice"], full=True, monkeypatch=monkeypatch) == []
        assert await _query(sql, world["boss"], full=True, monkeypatch=monkeypatch) == []
        assert await _query(sql, bob, full=True, monkeypatch=monkeypatch) == [f"rls-{t}-draft"]
    finally:
        async with factory() as db:
            await db.execute(text("DELETE FROM draft_actions WHERE id = :i"), {"i": draft_id})
            await db.commit()
