"""Large ToolResult v1 results keep their rows and count in the vault envelope."""

from unittest.mock import AsyncMock

import pytest

from app.ai.actor_context import set_acting_user
from app.ai.turn_vault import make_vault_envelope, vault_get, vault_store


def _tool_result(n):
    return {
        "version": 1,
        "status": "succeeded",
        "data": {"items": [{"id": i, "name": f"Поставщик {i}"} for i in range(n)], "total": n},
        "evidence": {"adapter_contract": "http_read_response_v1"},
        "checkpoint": None,
    }


def test_envelope_keeps_status_count_and_a_preview():
    compact = make_vault_envelope(_tool_result(39), "vault:v2:" + "a" * 32)

    assert compact["status"] == "succeeded"
    assert compact["evidence"] == {"adapter_contract": "http_read_response_v1"}
    assert compact["data"]["total"] == 39
    assert [row["id"] for row in compact["data"]["items"]] == [0, 1, 2]
    assert compact["data"]["_vault_note"].startswith("[39 записей.")


def test_unknown_count_is_not_reported_as_zero():
    compact = make_vault_envelope({"summary": "x" * 7000}, "vault:v2:" + "b" * 32)
    assert "total" not in compact
    assert compact["_vault_note"].startswith("[Полный результат сохранён.")


def test_legacy_list_result_unchanged():
    compact = make_vault_envelope({"items": [1, 2, 3, 4], "total": 4}, "vault:v2:" + "c" * 32)
    assert compact["total"] == 4 and compact["items"] == [1, 2, 3]


@pytest.mark.asyncio
async def test_paging_reads_rows_inside_the_envelope(monkeypatch):
    values = {}

    async def save(key, value, **kwargs):
        values[key] = value

    redis = AsyncMock()
    redis.set.side_effect = save
    redis.get.side_effect = lambda key: values.get(key)
    monkeypatch.setattr("app.utils.redis_client.get_async_redis", lambda: redis)
    set_acting_user("alice")
    try:
        ref = await vault_store("session", _tool_result(39))
        page = await vault_get(ref, offset=30, limit=5)
    finally:
        set_acting_user(None)

    assert page["status"] == "succeeded"
    assert [row["id"] for row in page["data"]["items"]] == [30, 31, 32, 33, 34]
    assert page["data"]["total_stored"] == 39 and page["data"]["has_more"] is True
