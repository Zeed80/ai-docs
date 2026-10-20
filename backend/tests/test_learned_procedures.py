"""Procedures the agent proposes and the owner approves (hermes-agent's skills).

A proposal never reaches the prompt before the owner decides; an approved
version replaces the live one; unused learned procedures go stale, then to the
archive, unless pinned.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.ai import procedures
from app.db.models import AgentProcedure

TEXT = "1. Найди просроченные счета.\n2. Письмо — с копией бухгалтеру."


@pytest.fixture(autouse=True)
def _fresh_cache():
    procedures._learned = {}
    yield
    procedures._learned = {}


async def _propose(client: AsyncClient, **overrides):
    body = {
        "name": "overdue-letter",
        "description": "Письмо о просрочке оплаты",
        "text": TEXT,
        "reason": "Владелец: «запомни, всегда копия бухгалтеру»",
        **overrides,
    }
    return await client.post("/api/agent/procedures-proposals", json=body)


@pytest.mark.asyncio
async def test_a_proposal_waits_for_the_owner(client: AsyncClient, db_session):
    resp = await _propose(client)
    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "proposed"

    await procedures.refresh_learned(db_session)
    assert procedures.read_procedure("overdue-letter") is None
    assert "overdue-letter" not in procedures.procedures_index()


@pytest.mark.asyncio
async def test_an_approved_procedure_joins_the_index_and_is_read(client: AsyncClient, db_session):
    row_id = (await _propose(client)).json()["id"]
    decided = await client.post(
        f"/api/agent/procedures-registry/{row_id}/decide", json={"approve": True}
    )
    assert decided.status_code == 200, decided.text
    assert decided.json()["status"] == "active"

    assert await procedures.refresh_learned(db_session) is True
    assert "- `overdue-letter` — Письмо о просрочке оплаты" in procedures.procedures_index()
    read = await client.get("/api/agent/procedures/overdue-letter")
    assert read.json()["text"] == TEXT
    db_session.expire_all()
    row = await db_session.get(AgentProcedure, uuid.UUID(row_id))
    assert row.uses == 1


@pytest.mark.asyncio
async def test_a_new_version_supersedes_the_live_one(client: AsyncClient, db_session):
    first = (await _propose(client)).json()["id"]
    await client.post(f"/api/agent/procedures-registry/{first}/decide", json={"approve": True})
    second = (await _propose(client, text=TEXT + "\n3. Срок ответа — 3 дня.")).json()
    assert second["current_text"] == TEXT
    edited = await client.post(
        f"/api/agent/procedures-registry/{second['id']}/decide",
        json={"approve": True, "text": TEXT + "\n3. Срок ответа — 5 дней."},
    )
    assert edited.json()["text"].endswith("5 дней.")
    rows = (
        await db_session.scalars(
            select(AgentProcedure.status).where(AgentProcedure.name == "overdue-letter")
        )
    ).all()
    assert sorted(rows) == ["active", "superseded"]


@pytest.mark.asyncio
async def test_a_rejected_proposal_never_reaches_the_prompt(client: AsyncClient, db_session):
    row_id = (await _propose(client)).json()["id"]
    resp = await client.post(
        f"/api/agent/procedures-registry/{row_id}/decide", json={"approve": False}
    )
    assert resp.json()["status"] == "rejected"
    await procedures.refresh_learned(db_session)
    assert procedures.read_procedure("overdue-letter") is None
    again = await client.post(
        f"/api/agent/procedures-registry/{row_id}/decide", json={"approve": True}
    )
    assert again.status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad",
    [{"name": "Письмо"}, {"name": "../base"}, {"description": "x" * 151}],
)
async def test_a_malformed_proposal_is_refused(client: AsyncClient, bad):
    assert (await _propose(client, **bad)).status_code == 422


@pytest.mark.asyncio
async def test_the_curator_stales_then_archives_unpinned_procedures(db_session):
    now = datetime.now(UTC)

    def row(name, days, pinned=False, status="active"):
        return AgentProcedure(
            name=name,
            description="d",
            text="t",
            status=status,
            origin="agent",
            proposed_by="agent:auto",
            pinned=pinned,
            last_used_at=now - timedelta(days=days),
        )

    db_session.add_all(
        [row("fresh", 3), row("unused", 20), row("forgotten", 40), row("kept", 40, pinned=True)]
    )
    await db_session.commit()

    counts = await procedures.curate(db_session, now=now)
    await db_session.commit()

    statuses = dict(
        (await db_session.execute(select(AgentProcedure.name, AgentProcedure.status))).all()
    )
    assert statuses == {
        "fresh": "active",
        "unused": "stale",
        "forgotten": "archived",
        "kept": "active",
    }
    assert counts == {"stale": 1, "archived": 1}

    await procedures.refresh_learned(db_session)
    assert "- `unused` — d (давно не читалась)" in procedures.procedures_index()
    assert "forgotten" not in procedures.procedures_index()


@pytest.mark.asyncio
async def test_a_read_brings_a_stale_procedure_back(client: AsyncClient, db_session):
    db_session.add(
        AgentProcedure(
            name="unused",
            description="d",
            text="t",
            status="stale",
            origin="agent",
            proposed_by="agent:auto",
        )
    )
    await db_session.commit()
    assert (await client.get("/api/agent/procedures/unused")).status_code == 200
    db_session.expire_all()
    status = await db_session.scalar(
        select(AgentProcedure.status).where(AgentProcedure.name == "unused")
    )
    assert status == "active"


@pytest.mark.asyncio
async def test_archive_and_restore(client: AsyncClient):
    row_id = (await _propose(client)).json()["id"]
    await client.post(f"/api/agent/procedures-registry/{row_id}/decide", json={"approve": True})
    archived = await client.post(f"/api/agent/procedures-registry/{row_id}/archive")
    assert archived.json()["status"] == "archived"
    restored = await client.post(f"/api/agent/procedures-registry/{row_id}/restore")
    assert restored.json()["status"] == "active"
    pinned = await client.post(
        f"/api/agent/procedures-registry/{row_id}/pin", json={"pinned": True}
    )
    assert pinned.json()["pinned"] is True


def test_propose_is_a_routed_write_action():
    from app.ai.tool_catalog import get_tool

    tool = get_tool("procedures", "propose")
    assert tool is not None and tool.effect == "write"


@pytest.mark.asyncio
async def test_autoproposal_is_off_by_default(monkeypatch):
    from app.ai.agent_config import BuiltinAgentConfig
    from app.domain import procedure_learning

    monkeypatch.setattr(
        "app.ai.agent_config.get_builtin_agent_config", lambda: BuiltinAgentConfig()
    )
    assert BuiltinAgentConfig().procedure_autopropose_enabled is False
    assert await procedure_learning.suggest_procedure(uuid.uuid4()) is None


@pytest.mark.asyncio
async def test_autoproposal_after_long_work_proposes_and_waits(test_engine, monkeypatch):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.ai import ollama_client
    from app.ai.agent_config import BuiltinAgentConfig
    from app.domain import procedure_learning

    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    monkeypatch.setattr(
        "app.ai.agent_config.get_builtin_agent_config",
        lambda: BuiltinAgentConfig(procedure_autopropose_enabled=True),
    )
    calls = [{"tool": f"invoices.list{i}", "arguments": "{}"} for i in range(4)]
    traces = {"short": calls[:3], "long": calls}

    async def fake_trace(db, work_order_id):
        return {
            "order": None,
            "request": "Письма по просрочкам",
            "calls": traces[state["kind"]],
            "answer": "Готово",
        }

    seen = []

    async def fake_generate_json(prompt, **kwargs):
        seen.append(prompt)
        return {
            "propose": True,
            "name": "overdue-letters",
            "description": "Письма поставщикам о просрочке",
            "text": TEXT,
            "reason": "Многошаговая работа по просрочкам",
        }

    state = {"kind": "short"}
    monkeypatch.setattr(procedure_learning, "_trace", fake_trace)
    monkeypatch.setattr(ollama_client, "generate_json", fake_generate_json)

    order_id = uuid.uuid4()
    assert await procedure_learning.suggest_procedure(order_id, session_factory=factory) is None
    assert seen == []

    state["kind"] = "long"
    name = await procedure_learning.suggest_procedure(order_id, session_factory=factory)
    assert name == "overdue-letters"
    async with factory() as db:
        row = await db.scalar(select(AgentProcedure).where(AgentProcedure.name == name))
        assert row.status == "proposed" and row.source_work_order_id == order_id
