"""Full SQL read access for every model: the operator's switch and its limits.

Off (default): seven tables, local providers only. On: every non-secret table
for every model, cloud included, read-only, under ``agent_sql_reader_full``;
secrets stay out by DB grants as well as by the text check.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai import data_access, ollama_client, table_sql_pipeline
from app.ai.agent_config import BuiltinAgentConfigUpdate, update_builtin_agent_config
from app.ai.policy_engine import PROTECTED_SETTINGS
from app.ai.table_sql_pipeline import validate_sql
from app.ai.work_budget_context import BudgetExecutionStopped, bind_http_recipient_budget_context
from app.auth.work_budget_handoff import resolve_sql_recipient_context
from tests.test_work_budget_direct_text import _install_http, _install_local_runtime
from tests.test_work_budget_http_recipient import _request, _rows, _setup


def _full(monkeypatch, enabled=True):
    monkeypatch.setattr(data_access, "sql_full_access_enabled", lambda: enabled)


def test_the_switch_is_off_by_default_and_protected():
    assert data_access.sql_full_access_enabled() is False
    assert "sql_full_access" in PROTECTED_SETTINGS


def test_full_mode_text_check_admits_any_table_but_the_secret_ones():
    allowed = frozenset(data_access.readable_tables({"invoices", "email_messages", "api_keys"}))
    assert validate_sql("SELECT subject FROM email_messages", allowed=allowed) is not None
    assert validate_sql("SELECT * FROM api_keys", allowed=allowed) is None
    # Default mode is unchanged: mail stays out.
    assert validate_sql("SELECT subject FROM email_messages") is None


@pytest.mark.asyncio
async def test_full_reader_grants_read_data_and_withhold_secrets(test_engine):
    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        assert await data_access.sync_full_reader_grants(db) > 50
        await db.commit()

    async def as_full_reader(sql):
        async with factory() as db:
            await db.execute(text(f"SET LOCAL ROLE {data_access.FULL_READER_ROLE}"))
            try:
                await db.execute(text(sql))
                return True
            except Exception:  # noqa: BLE001 — permission denied
                return False
            finally:
                await db.rollback()

    assert await as_full_reader("SELECT count(*) FROM invoices")
    assert await as_full_reader("SELECT count(*) FROM email_messages")
    assert await as_full_reader("SELECT id FROM chat_sessions LIMIT 1")
    assert not await as_full_reader("SELECT share_token FROM chat_sessions LIMIT 1")
    for table in sorted(data_access.SECRET_TABLES):
        assert not await as_full_reader(f"SELECT 1 FROM {table} LIMIT 1"), table
    # Read-only even here.
    assert not await as_full_reader("DELETE FROM invoices WHERE false")

    # Switched off, the role keeps nothing.
    async with factory() as db:
        await data_access.revoke_full_reader_grants(db)
        await db.commit()
    assert not await as_full_reader("SELECT count(*) FROM invoices")


@pytest.mark.asyncio
async def test_full_mode_fails_closed_without_its_role(monkeypatch):
    """Under the owner role every secret would be readable: no fallback."""
    _full(monkeypatch)
    monkeypatch.setattr(data_access, "FULL_READER_ROLE", "no_such_reader_role")

    async def schema():
        return "invoices(id uuid)", frozenset({"invoices"})

    monkeypatch.setattr(table_sql_pipeline, "_full_schema", schema)
    with pytest.raises(Exception):  # noqa: B017 — the role error itself
        await table_sql_pipeline.execute_sql("SELECT id FROM invoices")


class _CloudResponse:
    status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return {"choices": [{"message": {"content": "SELECT 1 FROM invoices"}}]}


def _install_cloud(monkeypatch):
    _install_local_runtime(monkeypatch, provider="openai")
    monkeypatch.setattr(
        "app.ai.model_resolver._provider_base_url", lambda _p: "https://cloud.example/v1"
    )
    monkeypatch.setattr("app.ai.model_resolver._provider_api_key", lambda _p: "k")
    return _install_http(monkeypatch, [_CloudResponse()])[0]


@pytest.mark.asyncio
async def test_sql_recipient_refuses_a_cloud_model_while_the_switch_is_off(
    test_engine, monkeypatch
):
    _factory, _run, _parent, headers = await _setup(test_engine, monkeypatch)
    context = await resolve_sql_recipient_context(_request(headers))
    posts = _install_cloud(monkeypatch)

    with bind_http_recipient_budget_context(context), pytest.raises(BudgetExecutionStopped) as stop:
        await ollama_client.reasoning_generate("sql")

    assert stop.value.code == "http_recipient_provider_unsupported"
    assert posts == []


@pytest.mark.asyncio
async def test_sql_recipient_charges_a_cloud_call_once_the_switch_is_on(test_engine, monkeypatch):
    factory, run, _parent, headers = await _setup(test_engine, monkeypatch)
    context = await resolve_sql_recipient_context(_request(headers))
    posts = _install_cloud(monkeypatch)
    _full(monkeypatch)

    with bind_http_recipient_budget_context(context):
        await context.assert_ready()
        result = await ollama_client.reasoning_generate("sql")

    assert result == "SELECT 1 FROM invoices"
    rows = await _rows(factory, run["work_order_id"], "recipient-llm:")
    assert len(posts) == len(rows) == 1
    assert rows[0].state == "charged"
    assert posts[0][0] == "https://cloud.example/v1/chat/completions"


@pytest.mark.asyncio
async def test_api_turns_it_on_only_with_an_explicit_acknowledgement(
    client, db_session, monkeypatch
):
    synced = []

    async def fake_sync(_db):
        synced.append(True)
        return 123

    monkeypatch.setattr(data_access, "sync_full_reader_grants", fake_sync)

    async def fake_revoke(_db):
        synced.append(False)

    monkeypatch.setattr(data_access, "revoke_full_reader_grants", fake_revoke)

    # As on the stand: a durable config written before the switch existed.
    from app.ai.agent_config import get_builtin_agent_config
    from app.ai.model_runtime_store import persist_agent_config

    stale = get_builtin_agent_config().model_dump(mode="json")
    stale.pop("sql_full_access")
    await persist_agent_config(db_session, config=stale)
    await db_session.flush()

    refused = await client.put("/api/providers/policy/data-access", json={"enabled": True})
    assert refused.status_code == 422
    assert data_access.sql_full_access_enabled() is False

    on = await client.put(
        "/api/providers/policy/data-access", json={"enabled": True, "acknowledged": True}
    )
    assert on.status_code == 200, on.text
    assert on.json()["sql_full_access"] is True
    assert on.json()["granted_tables"] == 123
    assert "provider_instances" in on.json()["secret_tables"]
    assert synced == [True]

    # It survives a restart: startup hydrates Redis from the durable copy.
    from app.ai.model_runtime_store import hydrate_runtime_cache

    await hydrate_runtime_cache(db_session)
    assert data_access.sql_full_access_enabled() is True

    off = await client.put("/api/providers/policy/data-access", json={"enabled": False})
    assert off.json()["sql_full_access"] is False
    assert (await client.get("/api/providers/policy/data-access")).json()[
        "sql_full_access"
    ] is False
    update_builtin_agent_config(BuiltinAgentConfigUpdate(sql_full_access=False))
