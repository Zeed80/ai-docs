"""E47: logs carry ids, sizes and hashes, not prompts or secrets."""

from __future__ import annotations

import pytest

from app.core.logging_setup import redact_event


def test_content_and_secrets_are_not_logged(monkeypatch):
    monkeypatch.delenv("LOG_INCLUDE_CONTENT", raising=False)
    event = redact_event(
        None,
        "info",
        {
            "event": "turn",
            "prompt": "Отправь Бобу пароль от банка",
            "content": "секретное содержание счёта",
            "api_key": "sk-123",
            "db_password": "pw",
            "tokens_in": 120,
            "document_id": "d-1",
        },
    )
    assert "Бобу" not in str(event) and "sk-123" not in str(event) and "pw" != event["db_password"]
    assert event["prompt"].startswith("<") and "sha256:" in event["prompt"]
    assert event["api_key"] == "<masked>"
    assert event["tokens_in"] == 120 and event["document_id"] == "d-1"


def test_content_can_be_turned_on_for_debugging_but_secrets_stay_masked(monkeypatch):
    monkeypatch.setenv("LOG_INCLUDE_CONTENT", "true")
    event = redact_event(None, "info", {"prompt": "видно", "token": "t"})
    assert event == {"prompt": "видно", "token": "<masked>"}


@pytest.mark.asyncio
async def test_a_request_gets_an_id_back(client):
    resp = await client.get("/health")
    assert len(resp.headers.get("x-request-id", "")) >= 8
    kept = await client.get("/health", headers={"X-Request-ID": "abc12345-req"})
    assert kept.headers["x-request-id"] == "abc12345-req"
    injected = await client.get("/health", headers={"X-Request-ID": "not an id\\nlog"})
    assert injected.headers["x-request-id"] != "not an id\\nlog"
