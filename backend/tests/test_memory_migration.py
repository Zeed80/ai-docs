"""E43: old shared memory moves only on proof; the rest is quarantined."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db.models import ChatSession, MemoryFact
from app.domain.memory_migration import apply_report, build_report


def _fact(kind, scope="project", **meta):
    return MemoryFact(
        scope=scope,
        kind=kind,
        title=f"{kind}-title",
        summary="текст",
        source="test",
        confidence=1.0,
        pinned=False,
        metadata_=meta or None,
    )


@pytest.fixture
async def facts(db_session):
    session = ChatSession(user_key="e43:alice", title="Alice chat")
    db_session.add(session)
    await db_session.flush()
    rows = {
        "alice_turn": _fact("chat_turn", session_id=str(session.id)),
        "lost_turn": _fact("chat_turn", session_id="00000000-0000-0000-0000-000000000001"),
        "rule": _fact("verified_fact", promotion_status="approved"),
        "source": _fact("web_source", url="https://example.com"),
        "state": _fact("idle_reflection_state", last_run_at="x"),
        "insight": _fact("graph_insight", nodes=[{"node_id": "n1"}]),
        "pin": _fact("pinned_fact"),
        "manager_pin": _fact("pinned_fact", owner_key="boss"),
        "proposal": _fact("proposed_fact", owner_key="e43:alice"),
        "already_owned": _fact("chat_turn", scope="owner:e43:alice"),
    }
    db_session.add_all(rows.values())
    await db_session.commit()
    return {k: v.id for k, v in rows.items()}


def _by_id(report):
    return {d.fact_id: d for d in report.decisions}


@pytest.mark.asyncio
async def test_the_report_sorts_by_provenance_and_writes_nothing(db_session, facts):
    report = await build_report(db_session)
    d = _by_id(report)
    assert d[str(facts["alice_turn"])].category == "proven_owner"
    assert d[str(facts["alice_turn"])].target_scope == "owner:e43:alice"
    assert d[str(facts["lost_turn"])].category == "ambiguous"
    assert d[str(facts["lost_turn"])].target_scope == "quarantine:project"
    assert d[str(facts["rule"])].category == "proven_shared"
    assert d[str(facts["source"])].category == "proven_shared"
    assert d[str(facts["state"])].category == "system_state"
    assert d[str(facts["insight"])].category == "derived"
    assert d[str(facts["insight"])].source_ids == ["n1"]
    assert d[str(facts["pin"])].category == "ambiguous"
    assert d[str(facts["manager_pin"])].category == "proven_shared"
    assert d[str(facts["proposal"])].category == "review_queue"
    assert str(facts["already_owned"]) not in d
    scopes = set(await db_session.scalars(select(MemoryFact.scope)))
    assert scopes == {"project", "owner:e43:alice"}  # dry run moved nothing


@pytest.mark.asyncio
async def test_apply_needs_approval_moves_in_batches_and_is_idempotent(db_session, facts):
    report = await build_report(db_session)
    with pytest.raises(ValueError):
        await apply_report(db_session, report, approved_by="")
    first = await apply_report(db_session, report, approved_by="owner", batch=2)
    assert first["moved"] == 2 and first["next_after"]
    rest = await apply_report(
        db_session, report, approved_by="owner", batch=100, after=first["next_after"]
    )
    assert rest["next_after"] is None
    alice = await db_session.get(MemoryFact, facts["alice_turn"])
    await db_session.refresh(alice)
    assert alice.scope == "owner:e43:alice"
    assert alice.metadata_["scope_migration"]["rule"] == "chat_session_owner"
    rule = await db_session.get(MemoryFact, facts["rule"])
    assert rule.scope == "project"
    # A second run finds nothing left to move.
    again = await apply_report(db_session, await build_report(db_session), approved_by="owner")
    assert again["moved"] == 0


@pytest.mark.asyncio
async def test_a_fact_changed_after_the_report_is_not_moved(db_session, facts):
    report = await build_report(db_session)
    lost = await db_session.get(MemoryFact, facts["lost_turn"])
    lost.summary = "изменено после отчёта"
    await db_session.commit()
    result = await apply_report(db_session, report, approved_by="owner")
    assert result["skipped"] == 1
    await db_session.refresh(lost)
    assert lost.scope == "project"
