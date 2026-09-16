"""Transport failures must not replay an unconfirmed side effect."""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.ai.agent_config import BuiltinAgentConfig
from app.ai.agent_loop import capability_args_digest, execute_skill
from app.ai.tool_transport import one_db_commit_operation, retry_safe


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


@pytest.mark.parametrize(
    "capability,action,expected",
    [
        ("analytics", "calendar_create_reminder", "analytics.calendar_create_reminder"),
        ("analytics", "collection_add_item", "analytics.collection_add_item"),
        ("analytics", "collection_close", "analytics.collection_close"),
        ("analytics", "collection_create", "analytics.collection_create"),
        ("analytics", "compare_align", "analytics.compare_align"),
        ("analytics", "compare_create", "analytics.compare_create"),
        ("analytics", "table_create_view", "analytics.table_create_view"),
        ("analytics", "table_inline_edit", "analytics.table_inline_edit"),
        ("warehouse", "create_item", "warehouse.create_item"),
    ],
)
def test_one_db_commit_resolution_uses_route_and_original_action_only(capability, action, expected):
    operation = one_db_commit_operation(
        {"method": "POST", "path": f"/api/agent/cap/{capability}"},
        {"action": action, "unchanged": {"nested": True}},
    )
    assert operation is not None
    assert operation.name == expected


@pytest.mark.parametrize(
    "skill,args,expected",
    [
        (
            {"method": "POST", "path": "/api/warehouse/inventory/{item_id}/adjust"},
            {"item_id": "item-1", "quantity": 2},
            "warehouse.adjust_stock",
        ),
        (
            {"method": "POST", "path": "/api/warehouse/receipts"},
            {"invoice_id": "invoice-1"},
            "warehouse.create_receipt",
        ),
        (
            {"method": "PATCH", "path": "/api/warehouse/inventory/{item_id}"},
            {"item_id": "item-1", "name": "Updated item"},
            "warehouse.update_item",
        ),
    ],
)
def test_e05_2_3_direct_routes_resolve_to_exact_catalog_operations(skill, args, expected):
    operation = one_db_commit_operation(skill, args)
    assert operation is not None
    assert operation.name == expected


def test_one_db_commit_resolution_fails_closed_for_other_operations_and_routes():
    assert (
        one_db_commit_operation(
            {"method": "POST", "path": "/api/agent/cap/warehouse"},
            {"action": "bulk_confirm", "receipt_ids": ["r-1"]},
        )
        is None
    )
    assert (
        one_db_commit_operation(
            {
                "name": "warehouse.create_item",
                "method": "POST",
                "path": "/api/agent/cap/documents",
            },
            {"action": "ingest"},
        )
        is None
    )
    # Two catalog aliases own this direct route. Without the capability action,
    # the exact operation is ambiguous and must retain legacy behavior.
    assert (
        one_db_commit_operation(
            {"method": "DELETE", "path": "/api/email-templates/{template_id}"},
            {"template_id": "t-1"},
        )
        is None
    )
    # analytics.compare_create is selected, but this direct route also belongs
    # to procurement.create_contract. Without the capability/action identity,
    # transport must not guess which operation the caller intended.
    assert (
        one_db_commit_operation(
            {"method": "POST", "path": "/api/compare"},
            {},
        )
        is None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action,operation",
    [
        ("collection_add_item", "analytics.collection_add_item"),
        ("collection_close", "analytics.collection_close"),
        ("compare_create", "analytics.compare_create"),
        ("compare_align", "analytics.compare_align"),
        ("table_inline_edit", "analytics.table_inline_edit"),
    ],
)
async def test_e05_2_2_operations_preserve_raw_success_payload(monkeypatch, action, operation):
    from app.ai import agent_loop

    payload = {
        "record_id": f"{action}-1",
        "status": "accepted",
        "details": {"source": "recipient"},
    }
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.post.return_value = httpx.Response(200, json=payload)
    monkeypatch.setattr(agent_loop.httpx, "AsyncClient", MagicMock(return_value=client))
    monkeypatch.setattr(agent_loop, "internal_headers", lambda: {})

    args = {"action": action, "entity_id": "entity-1", "value": "unchanged"}
    result = await execute_skill(
        {"method": "POST", "path": "/api/agent/cap/analytics"},
        args,
        BuiltinAgentConfig(),
    )

    assert result["version"] == 1
    assert result["status"] == "succeeded"
    assert result["data"] == payload
    assert result["evidence"]["operation"] == operation
    assert client.post.call_args.kwargs["json"] == args


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "skill,args,operation,method",
    [
        (
            {"method": "POST", "path": "/api/warehouse/inventory/{item_id}/adjust"},
            {"item_id": "item-1", "quantity": 2, "reason": "count correction"},
            "warehouse.adjust_stock",
            "post",
        ),
        (
            {"method": "POST", "path": "/api/warehouse/receipts"},
            {"invoice_id": "invoice-1", "notes": "raw recipient payload"},
            "warehouse.create_receipt",
            "post",
        ),
        (
            {"method": "PATCH", "path": "/api/warehouse/inventory/{item_id}"},
            {"item_id": "item-1", "name": "Updated item"},
            "warehouse.update_item",
            "patch",
        ),
    ],
)
async def test_e05_2_3_operations_preserve_raw_success_payload(
    monkeypatch, skill, args, operation, method
):
    from app.ai import agent_loop

    payload = {"record_id": f"{operation}-1", "status": "accepted"}
    client = AsyncMock()
    client.__aenter__.return_value = client
    getattr(client, method).return_value = httpx.Response(200, json=payload)
    monkeypatch.setattr(agent_loop.httpx, "AsyncClient", MagicMock(return_value=client))
    monkeypatch.setattr(agent_loop, "internal_headers", lambda: {})

    result = await execute_skill(skill, args, BuiltinAgentConfig())

    assert result["status"] == "succeeded"
    assert result["data"] == payload
    assert result["evidence"]["operation"] == operation


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "domain_marker,error_code",
    [
        ({"error": "bad"}, "domain_error"),
        ({"error_code": "invalid_item"}, "invalid_item"),
        ({"errors": ["bad"]}, "domain_error"),
        ({"built": False}, "not_built"),
        ({"status": "failed", "reason": "rejected"}, "domain_error"),
        ({"status": "error"}, "domain_error"),
    ],
)
async def test_one_db_commit_200_domain_failure_is_failed(monkeypatch, domain_marker, error_code):
    from app.ai import agent_loop

    payload = {"record_id": "item-1", **domain_marker}
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.post.return_value = httpx.Response(200, json=payload)
    monkeypatch.setattr(agent_loop.httpx, "AsyncClient", MagicMock(return_value=client))
    monkeypatch.setattr(agent_loop, "internal_headers", lambda: {})

    result = await execute_skill(
        {"method": "POST", "path": "/api/agent/cap/warehouse"},
        {"action": "create_item", "sku": "A-1"},
        BuiltinAgentConfig(),
    )

    assert result["version"] == 1
    assert result["status"] == "failed"
    assert result["error_code"] == error_code
    assert result["data"] == payload
    client.post.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("recipient_status", ["proposed", "approved"])
async def test_one_db_commit_success_preserves_raw_ids_and_recipient_status(
    monkeypatch, recipient_status
):
    from app.ai import agent_loop

    payload = {
        "record_id": "item-1",
        "receipt_id": "receipt-1",
        "status": recipient_status,
        "evidence": {"revision": 7},
    }
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.post.return_value = httpx.Response(201, json=payload)
    monkeypatch.setattr(agent_loop.httpx, "AsyncClient", MagicMock(return_value=client))
    monkeypatch.setattr(agent_loop, "internal_headers", lambda: {})

    result = await execute_skill(
        {"method": "POST", "path": "/api/agent/cap/warehouse"},
        {"action": "create_item", "sku": "A-1"},
        BuiltinAgentConfig(),
    )

    assert result["version"] == 1
    assert result["status"] == "succeeded"
    assert result["data"] == payload
    assert result["evidence"]["operation"] == "warehouse.create_item"


@pytest.mark.asyncio
async def test_one_db_commit_valid_v1_is_not_double_wrapped(monkeypatch):
    from app.ai import agent_loop

    payload = {
        "version": 1,
        "status": "succeeded",
        "data": {"record_id": "item-1"},
        "evidence": {"recipient": "warehouse"},
    }
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.post.return_value = httpx.Response(200, json=payload)
    monkeypatch.setattr(agent_loop.httpx, "AsyncClient", MagicMock(return_value=client))
    monkeypatch.setattr(agent_loop, "internal_headers", lambda: {})

    result = await execute_skill(
        {"method": "POST", "path": "/api/agent/cap/warehouse"},
        {"action": "create_item", "sku": "A-1"},
        BuiltinAgentConfig(),
    )

    assert result["status"] == "succeeded"
    assert result["data"] == {"record_id": "item-1"}
    assert result["evidence"] == {"recipient": "warehouse"}


@pytest.mark.asyncio
async def test_one_db_commit_4xx_preserves_structured_rejection(monkeypatch):
    from app.ai import agent_loop

    payload = {"detail": {"error_code": "duplicate_sku", "sku": "A-1"}}
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.post.return_value = httpx.Response(409, json=payload)
    monkeypatch.setattr(agent_loop.httpx, "AsyncClient", MagicMock(return_value=client))
    monkeypatch.setattr(agent_loop, "internal_headers", lambda: {})

    result = await execute_skill(
        {"method": "POST", "path": "/api/agent/cap/warehouse"},
        {"action": "create_item", "sku": "A-1"},
        BuiltinAgentConfig(),
    )

    assert result["status"] == "failed"
    assert result["error_code"] == "http_409"
    assert result["retryable"] is False
    assert result["data"] == payload
    assert result["evidence"]["http_status"] == 409
    assert result["evidence"]["effect_confirmed"] is False
    client.post.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure,expected_reason",
    [
        (httpx.Response(503, json={"detail": "recipient unavailable"}), "http_503"),
        (httpx.ReadTimeout("sensitive timeout detail"), "transport_ReadTimeout"),
        (httpx.RemoteProtocolError("sensitive disconnect detail"), "transport_RemoteProtocolError"),
        (RuntimeError("sensitive exception detail"), "exception_RuntimeError"),
    ],
)
async def test_one_db_commit_ambiguous_failure_is_unknown_and_never_retried(
    monkeypatch, failure, expected_reason
):
    from app.ai import agent_loop

    client = AsyncMock()
    client.__aenter__.return_value = client
    client.post.side_effect = [failure, httpx.Response(200, json={"record_id": "duplicate"})]
    monkeypatch.setattr(agent_loop.httpx, "AsyncClient", MagicMock(return_value=client))
    monkeypatch.setattr(agent_loop, "internal_headers", lambda: {})
    sleep = AsyncMock()
    monkeypatch.setattr(agent_loop.asyncio, "sleep", sleep)

    result = await execute_skill(
        {"method": "POST", "path": "/api/agent/cap/warehouse"},
        {"action": "create_item", "sku": "A-1"},
        BuiltinAgentConfig(),
    )

    assert result["version"] == 1
    assert result["status"] == "outcome_unknown"
    assert result["retryable"] is False
    assert result["evidence"]["reason"] == expected_reason
    assert result["evidence"]["recipient_outcome"] == "unconfirmed"
    if isinstance(failure, httpx.Response):
        assert result["data"] == {"detail": "recipient unavailable"}
        assert result["evidence"]["http_status"] == 503
    assert "sensitive" not in str(result)
    client.post.assert_awaited_once()
    sleep.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_point", ["headers", "client_enter"])
async def test_one_db_commit_pre_dispatch_exception_is_versioned_failed(monkeypatch, failure_point):
    from app.ai import agent_loop

    client = AsyncMock()
    client.__aenter__.return_value = client
    if failure_point == "headers":
        monkeypatch.setattr(
            agent_loop,
            "internal_headers",
            MagicMock(side_effect=RuntimeError("sensitive header detail")),
        )
    else:
        monkeypatch.setattr(agent_loop, "internal_headers", lambda: {})
        client.__aenter__.side_effect = RuntimeError("sensitive client detail")
    monkeypatch.setattr(agent_loop.httpx, "AsyncClient", MagicMock(return_value=client))

    result = await execute_skill(
        {"method": "POST", "path": "/api/agent/cap/warehouse"},
        {"action": "create_item", "sku": "A-1"},
        BuiltinAgentConfig(),
    )

    assert result["version"] == 1
    assert result["status"] == "failed"
    assert result["error_code"] == "write_dispatch_failed"
    assert result["retryable"] is False
    assert result["evidence"]["dispatch_attempted"] is False
    assert result["evidence"]["reason"] == "exception_RuntimeError"
    assert "sensitive" not in str(result)
    client.post.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["partial", "waiting_approval", "outcome_unknown"])
async def test_one_db_commit_2xx_legacy_nonterminal_status_is_preserved(monkeypatch, status):
    from app.ai import agent_loop

    payload = {
        "status": status,
        "error_code": f"recipient_{status}",
        "reason": "reconciliation required",
        "checkpoint": {"cursor": "next"},
        "evidence": {"revision": 3},
    }
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.post.return_value = httpx.Response(200, json=payload)
    monkeypatch.setattr(agent_loop.httpx, "AsyncClient", MagicMock(return_value=client))
    monkeypatch.setattr(agent_loop, "internal_headers", lambda: {})

    result = await execute_skill(
        {"method": "POST", "path": "/api/agent/cap/warehouse"},
        {"action": "create_item", "sku": "A-1"},
        BuiltinAgentConfig(),
    )

    assert result["status"] == status
    assert result["data"] == payload
    assert result["error_code"] == f"recipient_{status}"
    assert result["retryable"] is False
    assert result["checkpoint"] == {"cursor": "next"}
    assert result["evidence"]["reason"] == "reconciliation required"
    assert result["evidence"]["recipient_evidence"] == {"revision": 3}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action",
    [
        "ingest",  # E03 db-async-enqueue
        "send",  # E03 external-dispatch
        "bulk_confirm",  # E03 unknown
        "confirm_receipt",  # approval-gated write outside this reviewed slice
    ],
)
async def test_non_one_commit_groups_keep_legacy_success_contract(monkeypatch, action):
    from app.ai import agent_loop

    capability = "documents" if action == "ingest" else "email" if action == "send" else "warehouse"
    payload = {"job_id": "job-1", "status": "queued"}
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.post.return_value = httpx.Response(200, json=payload)
    monkeypatch.setattr(agent_loop.httpx, "AsyncClient", MagicMock(return_value=client))
    monkeypatch.setattr(agent_loop, "internal_headers", lambda: {})

    result = await execute_skill(
        {"method": "POST", "path": f"/api/agent/cap/{capability}"},
        {"action": action},
        BuiltinAgentConfig(),
    )

    assert result == payload
    assert "version" not in result


@pytest.mark.asyncio
async def test_one_db_commit_checks_headers_and_dispatch_keep_original_args(monkeypatch):
    from app.ai import agent_loop, tool_transport

    client = AsyncMock()
    client.__aenter__.return_value = client
    client.post.return_value = httpx.Response(200, json={"record_id": "item-1"})
    monkeypatch.setattr(agent_loop.httpx, "AsyncClient", MagicMock(return_value=client))
    monkeypatch.setattr(agent_loop, "internal_headers", lambda: {"Authorization": "test-context"})
    seen_args = []
    real_resolver = tool_transport.one_db_commit_operation
    real_retry_safe = tool_transport.retry_safe

    def resolver_with_identity(skill, received_args):
        seen_args.append(("resolver", received_args))
        return real_resolver(skill, received_args)

    def retry_safe_with_identity(skill, received_args):
        seen_args.append(("retry", received_args))
        return real_retry_safe(skill, received_args)

    monkeypatch.setattr(tool_transport, "one_db_commit_operation", resolver_with_identity)
    monkeypatch.setattr(tool_transport, "retry_safe", retry_safe_with_identity)
    args = {"action": "create_item", "sku": "A-1", "metadata": {"source": "agent"}}
    original_args = {"action": "create_item", "sku": "A-1", "metadata": {"source": "agent"}}

    await execute_skill(
        {"method": "POST", "path": "/api/agent/cap/warehouse"},
        args,
        BuiltinAgentConfig(),
        approval_granted=True,
        idempotency_key="logical-action:attempt",
    )

    sent = client.post.call_args.kwargs
    assert seen_args == [("resolver", args), ("retry", args)]
    assert seen_args[0][1] is args
    assert seen_args[1][1] is args
    assert args == original_args
    assert sent["json"] == original_args
    assert sent["headers"]["X-Agent-Idempotency-Key"] == "logical-action:attempt"
    assert sent["headers"]["Authorization"] == "test-context"
    assert sent["headers"]["X-Agent-Approval"] == "granted"
    assert sent["headers"]["X-Agent-Approval-Digest"] == capability_args_digest(original_args)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "skill,args,expected_body,method",
    [
        (
            {"method": "POST", "path": "/api/warehouse/inventory/{item_id}/adjust"},
            {"item_id": "item-1", "quantity": 2, "reason": "count correction"},
            {"quantity": 2, "reason": "count correction"},
            "post",
        ),
        (
            {"method": "POST", "path": "/api/warehouse/receipts"},
            {"invoice_id": "invoice-1", "notes": "original payload"},
            {"invoice_id": "invoice-1", "notes": "original payload"},
            "post",
        ),
        (
            {"method": "PATCH", "path": "/api/warehouse/inventory/{item_id}"},
            {"item_id": "item-1", "name": "Updated item"},
            {"name": "Updated item"},
            "patch",
        ),
    ],
)
async def test_e05_2_3_uses_original_args_without_approval_envelope(
    monkeypatch, skill, args, expected_body, method
):
    from app.ai import agent_loop, tool_transport

    client = AsyncMock()
    client.__aenter__.return_value = client
    getattr(client, method).return_value = httpx.Response(200, json={"record_id": "record-1"})
    monkeypatch.setattr(agent_loop.httpx, "AsyncClient", MagicMock(return_value=client))
    monkeypatch.setattr(agent_loop, "internal_headers", lambda: {"Authorization": "test-context"})
    seen_args = []
    real_resolver = tool_transport.one_db_commit_operation
    real_retry_safe = tool_transport.retry_safe

    def resolver_with_identity(received_skill, received_args):
        seen_args.append(("resolver", received_args))
        return real_resolver(received_skill, received_args)

    def retry_safe_with_identity(received_skill, received_args):
        seen_args.append(("retry", received_args))
        return real_retry_safe(received_skill, received_args)

    monkeypatch.setattr(tool_transport, "one_db_commit_operation", resolver_with_identity)
    monkeypatch.setattr(tool_transport, "retry_safe", retry_safe_with_identity)
    original_args = args.copy()

    await execute_skill(skill, args, BuiltinAgentConfig(), idempotency_key="logical-action:attempt")

    sent = getattr(client, method).call_args.kwargs
    assert seen_args == [("resolver", args), ("retry", args)]
    assert seen_args[0][1] is args
    assert seen_args[1][1] is args
    assert args == original_args
    assert sent["json"] == expected_body
    assert sent["headers"]["X-Agent-Idempotency-Key"] == "logical-action:attempt"
    assert sent["headers"]["Authorization"] == "test-context"
    assert "X-Agent-Approval" not in sent["headers"]
