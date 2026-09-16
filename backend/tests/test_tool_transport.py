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
        assert result["version"] == 1
        assert result["status"] == "failed"
        assert result["error_code"] == "http_500"
        assert result["data"] == {"detail": "Unavailable"}
    else:
        assert client.post.await_count == 2
        assert result["version"] == 1
        assert result["status"] == "succeeded"
        assert result["data"] == {"items": []}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response,expected_data",
    [
        (
            httpx.Response(200, json={"artifact_id": "artifact-1", "items": []}),
            {"artifact_id": "artifact-1", "items": []},
        ),
        (httpx.Response(200, text="plain read response"), "plain read response"),
    ],
)
async def test_catalog_read_success_is_serialized_without_losing_raw_payload(
    monkeypatch, response, expected_data
):
    from app.ai import agent_loop

    client = AsyncMock()
    client.__aenter__.return_value = client
    client.post.return_value = response
    monkeypatch.setattr(agent_loop.httpx, "AsyncClient", MagicMock(return_value=client))
    monkeypatch.setattr(agent_loop, "internal_headers", lambda: {})

    result = await execute_skill(
        {"method": "POST", "path": "/api/agent/cap/documents"},
        {"action": "list"},
        BuiltinAgentConfig(),
    )

    assert result["version"] == 1
    assert result["status"] == "succeeded"
    assert result["data"] == expected_data


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload,error_code",
    [
        ({"error": "bad input", "artifact_id": "artifact-1"}, "domain_error"),
        ({"error_code": "invalid_filter"}, "invalid_filter"),
        ({"errors": ["bad input"]}, "domain_error"),
        ({"built": False}, "not_built"),
        ({"result": {"errors": ["bad input"]}}, "domain_error"),
        ({"domain": {"built": False}}, "not_built"),
    ],
)
async def test_catalog_read_domain_failures_are_never_serialized_as_success(
    monkeypatch, payload, error_code
):
    from app.ai import agent_loop

    client = AsyncMock()
    client.__aenter__.return_value = client
    client.post.return_value = httpx.Response(200, json=payload)
    monkeypatch.setattr(agent_loop.httpx, "AsyncClient", MagicMock(return_value=client))
    monkeypatch.setattr(agent_loop, "internal_headers", lambda: {})

    result = await execute_skill(
        {"method": "POST", "path": "/api/agent/cap/documents"},
        {"action": "list"},
        BuiltinAgentConfig(),
    )

    assert result["status"] == "failed"
    assert result["error_code"] == error_code
    assert result["data"] == payload


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"job_id": "job-1", "status": "queued"},
        {"job_id": "job-2", "status": "running"},
    ],
)
async def test_catalog_read_progress_response_is_successful_raw_data(monkeypatch, payload):
    from app.ai import agent_loop

    client = AsyncMock()
    client.__aenter__.return_value = client
    client.post.return_value = httpx.Response(200, json=payload)
    monkeypatch.setattr(agent_loop.httpx, "AsyncClient", MagicMock(return_value=client))
    monkeypatch.setattr(agent_loop, "internal_headers", lambda: {})

    result = await execute_skill(
        {"method": "POST", "path": "/api/agent/cap/documents"},
        {"action": "list"},
        BuiltinAgentConfig(),
    )

    assert result["status"] == "succeeded"
    assert result["data"] == payload


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"items": [{"status": "running", "error": "historical", "built": False}]},
        {
            "history": [{"status": "queued", "error": "historical", "built": False}],
            "metadata": {"status": "running"},
        },
    ],
)
async def test_catalog_read_data_markers_do_not_override_response_success(monkeypatch, payload):
    from app.ai import agent_loop

    client = AsyncMock()
    client.__aenter__.return_value = client
    client.post.return_value = httpx.Response(200, json=payload)
    monkeypatch.setattr(agent_loop.httpx, "AsyncClient", MagicMock(return_value=client))
    monkeypatch.setattr(agent_loop, "internal_headers", lambda: {})

    result = await execute_skill(
        {"method": "POST", "path": "/api/agent/cap/documents"},
        {"action": "list"},
        BuiltinAgentConfig(),
    )

    assert result["status"] == "succeeded"
    assert result["data"] == payload


@pytest.mark.asyncio
async def test_catalog_read_timeout_after_bounded_retries_is_explicit_failed_result(monkeypatch):
    from app.ai import agent_loop

    client = AsyncMock()
    client.__aenter__.return_value = client
    client.post.side_effect = [httpx.ReadTimeout("private")] * 3
    monkeypatch.setattr(agent_loop.httpx, "AsyncClient", MagicMock(return_value=client))
    monkeypatch.setattr(agent_loop, "internal_headers", lambda: {})
    sleep = AsyncMock()
    monkeypatch.setattr(agent_loop.asyncio, "sleep", sleep)

    result = await execute_skill(
        {"method": "POST", "path": "/api/agent/cap/documents"},
        {"action": "list"},
        BuiltinAgentConfig(),
    )

    assert client.post.await_count == 3
    assert sleep.await_count == 2
    assert result["status"] == "failed"
    assert result["error_code"] == "read_transport_failed"
    assert result["retryable"] is False
    assert result["evidence"]["effect_ambiguity"] == "no_side_effect_expected"
    assert result["evidence"]["retry_policy"] == "internal_retry_budget_exhausted"
    assert "private" not in str(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["completed", "succeeded"])
async def test_catalog_read_completed_job_status_is_not_rejected_by_job_id(monkeypatch, status):
    from app.ai import agent_loop

    payload = {"job_id": "job-1", "status": status, "artifact_id": "artifact-1"}
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.post.return_value = httpx.Response(200, json=payload)
    monkeypatch.setattr(agent_loop.httpx, "AsyncClient", MagicMock(return_value=client))
    monkeypatch.setattr(agent_loop, "internal_headers", lambda: {})

    result = await execute_skill(
        {"method": "POST", "path": "/api/agent/cap/documents"},
        {"action": "list"},
        BuiltinAgentConfig(),
    )

    assert result["status"] == "succeeded"
    assert result["data"] == payload


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload,expected_status,expected_data",
    [
        (
            {"version": 1, "status": "succeeded", "data": {"artifact_id": "artifact-1"}},
            "succeeded",
            {"artifact_id": "artifact-1"},
        ),
        (
            {"version": 1, "status": "succeeded", "error": "contradiction"},
            "failed",
            {"version": 1, "status": "succeeded", "error": "contradiction"},
        ),
    ],
)
async def test_catalog_read_versioned_result_is_not_double_wrapped(
    monkeypatch, payload, expected_status, expected_data
):
    from app.ai import agent_loop

    client = AsyncMock()
    client.__aenter__.return_value = client
    client.post.return_value = httpx.Response(200, json=payload)
    monkeypatch.setattr(agent_loop.httpx, "AsyncClient", MagicMock(return_value=client))
    monkeypatch.setattr(agent_loop, "internal_headers", lambda: {})

    result = await execute_skill(
        {"method": "POST", "path": "/api/agent/cap/documents"},
        {"action": "list"},
        BuiltinAgentConfig(),
    )

    assert result["version"] == 1
    assert result["status"] == expected_status
    assert result["data"] == expected_data
    if expected_status == "succeeded":
        assert result["data"].get("version") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["partial", "waiting_approval", "outcome_unknown"])
async def test_catalog_read_preserves_versioned_nonterminal_status(monkeypatch, status):
    from app.ai import agent_loop

    payload = {
        "version": 1,
        "status": status,
        "data": {"job_id": "job-1"},
        "error_code": f"reason_for_{status}",
        "evidence": {"source": "recipient"},
        "checkpoint": {"cursor": "next"},
    }
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.post.return_value = httpx.Response(200, json=payload)
    monkeypatch.setattr(agent_loop.httpx, "AsyncClient", MagicMock(return_value=client))
    monkeypatch.setattr(agent_loop, "internal_headers", lambda: {})

    result = await execute_skill(
        {"method": "POST", "path": "/api/agent/cap/documents"},
        {"action": "list"},
        BuiltinAgentConfig(),
    )

    assert result["status"] == status
    assert result["status"] != "succeeded"
    assert result["data"] == payload["data"]
    assert result["error_code"] == payload["error_code"]
    assert result["evidence"] == payload["evidence"]
    assert result["checkpoint"] == payload["checkpoint"]


@pytest.mark.asyncio
async def test_catalog_read_checks_and_dispatch_receive_original_arguments(monkeypatch):
    from app.ai import agent_loop, tool_transport

    client = AsyncMock()
    client.__aenter__.return_value = client
    client.post.return_value = httpx.Response(200, json={"items": []})
    monkeypatch.setattr(agent_loop.httpx, "AsyncClient", MagicMock(return_value=client))
    monkeypatch.setattr(agent_loop, "internal_headers", lambda: {})
    seen_args = []

    def retry_safe_with_identity(skill, received_args):
        seen_args.append(received_args)
        return True

    monkeypatch.setattr(tool_transport, "retry_safe", retry_safe_with_identity)
    args = {"action": "list", "filters": {"owner": "operator"}}
    original_args = {"action": "list", "filters": {"owner": "operator"}}

    await execute_skill(
        {"method": "POST", "path": "/api/agent/cap/documents"},
        args,
        BuiltinAgentConfig(),
    )

    assert seen_args == [args]
    assert seen_args[0] is args
    assert args == original_args
    assert client.post.call_args.kwargs["json"] == original_args


@pytest.mark.asyncio
async def test_unknown_handler_failure_is_explicit_and_not_retried():
    handler = AsyncMock(side_effect=TimeoutError("private"))
    result = await execute_skill({"_method": "mcp", "_handler": handler}, {}, BuiltinAgentConfig())
    handler.assert_awaited_once()
    assert result["status"] == "outcome_unknown"


@pytest.mark.asyncio
async def test_successful_mcp_handler_result_remains_unchanged():
    payload = {"job_id": "job-1", "status": "running"}
    handler = AsyncMock(return_value=payload)

    result = await execute_skill({"_method": "mcp", "_handler": handler}, {}, BuiltinAgentConfig())

    handler.assert_awaited_once_with({})
    assert result is payload


@pytest.mark.asyncio
async def test_logical_key_is_transport_metadata_not_model_arguments(monkeypatch):
    from app.ai import agent_loop

    client = AsyncMock()
    client.__aenter__.return_value = client
    client.post.return_value = httpx.Response(200, json={"status": "proposed"})
    monkeypatch.setattr(agent_loop.httpx, "AsyncClient", MagicMock(return_value=client))
    monkeypatch.setattr(agent_loop, "internal_headers", lambda: {"Authorization": "test-context"})
    args = {"action": "task_propose", "objective": "Test"}
    result = await execute_skill(
        {"method": "POST", "path": "/api/agent/cap/agent_control"},
        args,
        BuiltinAgentConfig(),
        idempotency_key="logical-action:attempt",
    )
    sent = client.post.call_args.kwargs
    assert sent["headers"]["X-Agent-Idempotency-Key"] == "logical-action:attempt"
    assert sent["headers"]["Authorization"] == "test-context"
    assert sent["json"] == args
    assert result == {"status": "proposed"}
