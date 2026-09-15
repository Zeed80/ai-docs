"""Transport failures must not replay an unconfirmed side effect."""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.ai.agent_config import BuiltinAgentConfig
from app.ai.agent_loop import execute_skill
from app.ai.tool_transport import retry_safe


@pytest.mark.parametrize(
    "skill,args,expected",
    [
        ({"method": "POST", "path": "/api/agent/cap/documents"}, {"action": "list"}, True),
        ({"method": "POST", "path": "/api/agent/cap/documents"}, {"action": "delete"}, False),
        ({"method": "POST", "path": "/api/agent/cap/documents"}, {"action": "list_unknown"}, False),
        ({"method": "POST", "path": "/api/agent/cap/mcp"}, {"action": "list"}, False),
        ({"method": "GET", "path": "/api/documents"}, {}, True),
        ({"method": "GET", "path": "/unreviewed"}, {}, False),
        (
            {
                "name": "computer_use.file_read",
                "method": "POST",
                "path": "/api/computer-use/execute",
            },
            {"action": "file_write"},
            False,
        ),
    ],
)
def test_only_reviewed_read_routes_are_retry_safe(skill, args, expected):
    assert retry_safe(skill, args) is expected


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [500, 502, 503, 504, "timeout", "disconnect", "connect"])
@pytest.mark.parametrize("read", [False, True])
async def test_http_retry_policy_preserves_unknown_outcomes(monkeypatch, failure, read):
    from app.ai import agent_loop

    client = AsyncMock()
    manager = MagicMock(return_value=client)
    client.__aenter__.return_value = client
    if isinstance(failure, int):
        first = httpx.Response(failure, json={"detail": "Unavailable"})
    else:
        first = {
            "timeout": httpx.ReadTimeout,
            "disconnect": httpx.RemoteProtocolError,
            "connect": httpx.ConnectError,
        }[failure]("sensitive exception text")
    client.post.side_effect = [first, httpx.Response(200, json={"items": []})]
    monkeypatch.setattr(agent_loop.httpx, "AsyncClient", manager)
    monkeypatch.setattr(agent_loop, "internal_headers", lambda: {})
    sleep = AsyncMock()
    monkeypatch.setattr(agent_loop.asyncio, "sleep", sleep)
    result = await execute_skill(
        {"name": "documents", "method": "POST", "path": "/api/agent/cap/documents"},
        {"action": "list" if read else "delete", "document_id": "doc"},
        BuiltinAgentConfig(),
    )
    if not read:
        assert result["status"] == "outcome_unknown"
        assert result["retryable"] is False
        assert client.post.await_count == 1
        sleep.assert_not_awaited()
        assert "sensitive exception text" not in str(result)
    elif failure == 500:
        assert client.post.await_count == 1
        assert result["error"] == "HTTP 500"
    else:
        assert client.post.await_count == 2
        assert result == {"items": []}


@pytest.mark.asyncio
async def test_unknown_handler_failure_is_explicit_and_not_retried():
    handler = AsyncMock(side_effect=TimeoutError("private"))
    result = await execute_skill({"_method": "mcp", "_handler": handler}, {}, BuiltinAgentConfig())
    handler.assert_awaited_once()
    assert result["status"] == "outcome_unknown"
