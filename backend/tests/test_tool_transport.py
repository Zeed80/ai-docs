"""Transport failures must not replay an unconfirmed side effect."""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.ai.agent_config import BuiltinAgentConfig
from app.ai.agent_loop import capability_args_digest, execute_skill
from app.ai.tool_transport import (
    ASYNC_JOB_OPERATIONS,
    async_job_operation,
    email_send_queue_operation,
    one_db_commit_operation,
    retry_safe,
)


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


@pytest.mark.parametrize(
    "skill,args",
    [
        (
            {"method": "POST", "path": "/api/email-templates/{template_id}/render"},
            {"template_id": "template-1"},
        ),
        (
            {"method": "POST", "path": "/api/agent/cap/email"},
            {"action": "render_template", "template_id": "template-1"},
        ),
        (
            {"method": "POST", "path": "/api/agent/cap/email"},
            {"action": "templates.render", "template_id": "template-1"},
        ),
    ],
)
def test_persistent_template_rendering_never_receives_read_retry(skill, args):
    """Direct calls and both catalog aliases fail closed on retry safety."""
    assert retry_safe(skill, args) is False
    assert one_db_commit_operation(skill, args) is None


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
async def test_legacy_mcp_handler_is_rejected_before_dispatch():
    handler = AsyncMock(side_effect=TimeoutError("private"))
    result = await execute_skill({"_method": "mcp", "_handler": handler}, {}, BuiltinAgentConfig())
    handler.assert_not_awaited()
    assert result["version"] == 1
    assert result["status"] == "failed"
    assert result["error_code"] == "direct_mcp_handler_disabled"
    assert result["retryable"] is False


@pytest.mark.asyncio
async def test_legacy_mcp_success_payload_cannot_bypass_gateway():
    payload = {"job_id": "job-1", "status": "running"}
    handler = AsyncMock(return_value=payload)

    result = await execute_skill({"_method": "mcp", "_handler": handler}, {}, BuiltinAgentConfig())

    handler.assert_not_awaited()
    assert result["version"] == 1
    assert result["status"] == "failed"
    assert result["evidence"] == {"reason": "mcp_capability_gateway_required"}


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
        ("documents", "link", "documents.link"),
        ("email", "draft", "email.draft"),
        ("invoices", "update", "invoices.update"),
        ("normalization", "create_norm_card", "normalization.create_norm_card"),
        ("normalization", "update_canonical_item", "normalization.update_canonical_item"),
        ("normalization", "update_norm_card", "normalization.update_norm_card"),
        ("payments", "create_schedule", "payments.create_schedule"),
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


@pytest.mark.parametrize(
    "skill,args,expected",
    [
        (
            {"method": "PATCH", "path": "/api/suppliers/{supplier_id}"},
            {"supplier_id": "supplier-1", "user_notes": "Updated by operator"},
            "suppliers.update",
        ),
        (
            {"method": "POST", "path": "/api/email-templates/"},
            {"name": "Payment reminder", "subject": "Reminder", "body_html": "<p>Pay</p>"},
            "email.templates.create",
        ),
        (
            {"method": "PATCH", "path": "/api/email-templates/{template_id}"},
            {"template_id": "template-1", "subject": "Updated reminder"},
            "email.templates.update",
        ),
    ],
)
def test_e05_2_4_direct_routes_resolve_to_exact_catalog_operations(skill, args, expected):
    operation = one_db_commit_operation(skill, args)
    assert operation is not None
    assert operation.name == expected


def test_e05_2_5_direct_route_resolves_to_procurement_create_request():
    operation = one_db_commit_operation(
        {"method": "POST", "path": "/api/purchase-requests"},
        {
            "title": "Fasteners M8",
            "items": [{"name": "Bolt M8", "qty": 100, "unit": "pcs"}],
            "notes": "Original recipient payload",
        },
    )

    assert operation is not None
    assert operation.name == "procurement.create_request"


@pytest.mark.parametrize(
    "skill,args,expected",
    [
        (
            {"method": "POST", "path": "/api/documents/{document_id}/links"},
            {
                "document_id": "document-1",
                "linked_entity_type": "invoice",
                "linked_entity_id": "invoice-1",
                "link_type": "source",
            },
            "documents.link",
        ),
        (
            {"method": "POST", "path": "/api/email/drafts"},
            {"to_addresses": ["supplier@example.test"], "subject": "Draft"},
            "email.draft",
        ),
        (
            {"method": "POST", "path": "/api/payment-schedules"},
            {
                "invoice_id": "invoice-1",
                "due_date": "2026-10-01T00:00:00Z",
                "amount": 1000,
            },
            "payments.create_schedule",
        ),
    ],
)
def test_e05_2_6_direct_routes_resolve_to_exact_catalog_operations(skill, args, expected):
    operation = one_db_commit_operation(skill, args)
    assert operation is not None
    assert operation.name == expected


@pytest.mark.parametrize(
    "skill,args,expected",
    [
        (
            {"method": "POST", "path": "/api/normalization/norm-cards"},
            {"canonical_item_id": "item-1", "name": "Bolt M8"},
            "normalization.create_norm_card",
        ),
        (
            {"method": "PATCH", "path": "/api/normalization/norm-cards/{card_id}"},
            {"card_id": "card-1", "name": "Updated Bolt M8"},
            "normalization.update_norm_card",
        ),
        (
            {
                "method": "PATCH",
                "path": "/api/normalization/canonical-items/{item_id}",
            },
            {"item_id": "item-1", "okpd2_code": "25.94.11"},
            "normalization.update_canonical_item",
        ),
    ],
)
def test_e05_2_7_direct_routes_resolve_to_exact_catalog_operations(skill, args, expected):
    operation = one_db_commit_operation(skill, args)
    assert operation is not None
    assert operation.name == expected


@pytest.mark.parametrize(
    "skill,args,expected",
    [
        (
            {"method": "PATCH", "path": "/api/invoices/{invoice_id}"},
            {"invoice_id": "invoice-1", "notes": "Corrected by operator"},
            "invoices.update",
        ),
        (
            {"method": "POST", "path": "/api/tool-catalog/suppliers"},
            {"name": "Tool supplier", "website": "https://supplier.example.test"},
            "tool_catalog.create_supplier",
        ),
    ],
)
def test_e05_2_8_direct_routes_resolve_to_exact_catalog_operations(skill, args, expected):
    operation = one_db_commit_operation(skill, args)
    assert operation is not None
    assert operation.name == expected


@pytest.mark.parametrize(
    "skill,args,expected",
    [
        (
            {"method": "POST", "path": "/api/invoices/{invoice_id}/validate"},
            {"invoice_id": "invoice-1"},
            "invoices.validate",
        ),
        (
            {"method": "POST", "path": "/api/memory/sources/propose"},
            {
                "title": "Supplier catalog",
                "url": "https://supplier.example.test/catalog",
                "source_type": "supplier_catalog",
            },
            "memory.source_propose",
        ),
    ],
)
def test_e05_2_9_direct_routes_resolve_to_exact_catalog_operations(skill, args, expected):
    operation = one_db_commit_operation(skill, args)
    assert operation is not None
    assert operation.name == expected


@pytest.mark.parametrize(
    "skill,args,expected",
    [
        (
            {"method": "POST", "path": "/api/technology/corrections"},
            {
                "entity_type": "manufacturing_operation",
                "entity_id": "operation-1",
                "field_name": "setup_time_min",
                "old_value": "10",
                "new_value": "12",
                "corrected_by": "operator",
            },
            "tech.correction_record",
        ),
        (
            {"method": "POST", "path": "/api/technology/operation-templates"},
            {"operation_type": "turning", "name": "Finish turning"},
            "tech.operation_template_create",
        ),
    ],
)
def test_e05_2_10_direct_routes_resolve_to_exact_catalog_operations(skill, args, expected):
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
    # Creating a draft is reviewed, but generation/reply actions may invoke AI
    # or carry thread content and remain fail-closed.
    assert (
        one_db_commit_operation(
            {"method": "POST", "path": "/api/email/compose/generate"},
            {"intent": "Write a reply"},
        )
        is None
    )
    assert (
        one_db_commit_operation(
            {"method": "POST", "path": "/api/email/threads/{thread_id}/reply-draft"},
            {"thread_id": "thread-1", "intent": "Reply"},
        )
        is None
    )
    # Workspace sheet creation publishes on the chat bus after the DB commit,
    # so it is not a DB-only E05.2 adapter.
    assert (
        one_db_commit_operation(
            {"method": "POST", "path": "/api/workspace/sheets/create"},
            {"title": "Scratch"},
        )
        is None
    )
    # Auto-approval rule creation is admin-only, checking a rule has a runtime
    # 0/1 commit boundary, and marking a payment paid requires approval.
    assert (
        one_db_commit_operation(
            {"method": "POST", "path": "/api/agent/cap/analytics"},
            {"action": "auto_approval_create", "name": "Small invoices"},
        )
        is None
    )
    assert (
        one_db_commit_operation(
            {"method": "POST", "path": "/api/agent/cap/analytics"},
            {"action": "auto_approval_check", "invoice_id": "invoice-1"},
        )
        is None
    )
    assert (
        one_db_commit_operation(
            {"method": "POST", "path": "/api/agent/cap/payments"},
            {"action": "mark_paid", "schedule_id": "schedule-1"},
        )
        is None
    )
    # Approval/status transitions and source discovery have a different
    # effect boundary, so E05.2.9 must not widen the two reviewed operations.
    assert (
        one_db_commit_operation(
            {"method": "POST", "path": "/api/invoices/{invoice_id}/approve"},
            {"invoice_id": "invoice-1"},
        )
        is None
    )
    assert (
        one_db_commit_operation(
            {"method": "POST", "path": "/api/invoices/{invoice_id}/receive"},
            {"invoice_id": "invoice-1"},
        )
        is None
    )
    assert (
        one_db_commit_operation(
            {"method": "POST", "path": "/api/memory/sources/discover"},
            {"query": "supplier catalogs"},
        )
        is None
    )
    # These superficially local writes do not meet this slice's recipient
    # boundary: normalization can commit 0/1 times, sheets publish to the
    # chat bus, resource creation accepts status, and rule activation is gated.
    assert (
        one_db_commit_operation(
            {"method": "POST", "path": "/api/normalization/suggest"},
            {"min_corrections": 3},
        )
        is None
    )
    assert (
        one_db_commit_operation(
            {"method": "POST", "path": "/api/normalization/apply"},
            {"document_id": "document-1"},
        )
        is None
    )
    assert (
        one_db_commit_operation(
            {"method": "POST", "path": "/api/workspace/sheets/{sheet_id}/add-row"},
            {"sheet_id": "sheet-1", "count": 1},
        )
        is None
    )
    assert (
        one_db_commit_operation(
            {"method": "POST", "path": "/api/technology/resources"},
            {"resource_type": "lathe", "name": "1K62", "status": "active"},
        )
        is None
    )
    assert (
        one_db_commit_operation(
            {"method": "POST", "path": "/api/technology/learning-rules/{rule_id}/activate"},
            {"rule_id": "rule-1", "activated_by": "operator"},
        )
        is None
    )
    # Notification delivery preferences and AI settings are not active
    # catalog operations. Direct local settings routes stay legacy by default.
    assert (
        one_db_commit_operation(
            {"method": "PUT", "path": "/api/notifications/delivery"},
            {"timezone": "Europe/Moscow"},
        )
        is None
    )
    assert (
        one_db_commit_operation(
            {"method": "PATCH", "path": "/api/ai/config"},
            {"exposed_skills": ["payments"], "actor": "admin"},
        )
        is None
    )
    # Both updates accept a status field, so they can make a status decision
    # and remain outside this narrow create-only slice.
    assert (
        one_db_commit_operation(
            {"method": "PATCH", "path": "/api/purchase-requests/{request_id}"},
            {"request_id": "request-1", "status": "approved"},
        )
        is None
    )
    assert (
        one_db_commit_operation(
            {"method": "PATCH", "path": "/api/supplier-contracts/{contract_id}"},
            {"contract_id": "contract-1", "status": "active"},
        )
        is None
    )
    # This catalog GET writes a calculated trust score (E03); it is neither a
    # reviewed write adapter nor retry-safe.
    trust_score = {
        "method": "GET",
        "path": "/api/suppliers/{supplier_id}/trust-score",
    }
    assert one_db_commit_operation(trust_score, {"supplier_id": "supplier-1"}) is None
    assert retry_safe(trust_score, {"supplier_id": "supplier-1"}) is False
    assert (
        retry_safe(
            {"method": "POST", "path": "/api/agent/cap/suppliers"},
            {"action": "trust_score", "supplier_id": "supplier-1"},
        )
        is False
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
    "skill,args,operation,method",
    [
        (
            {"method": "PATCH", "path": "/api/suppliers/{supplier_id}"},
            {"supplier_id": "supplier-1", "user_notes": "Updated by operator"},
            "suppliers.update",
            "patch",
        ),
        (
            {"method": "POST", "path": "/api/email-templates/"},
            {"name": "Payment reminder", "subject": "Reminder", "body_html": "<p>Pay</p>"},
            "email.templates.create",
            "post",
        ),
        (
            {"method": "PATCH", "path": "/api/email-templates/{template_id}"},
            {"template_id": "template-1", "subject": "Updated reminder"},
            "email.templates.update",
            "patch",
        ),
    ],
)
async def test_e05_2_4_operations_preserve_raw_success_payload(
    monkeypatch, skill, args, operation, method
):
    from app.ai import agent_loop

    payload = {"record_id": f"{operation}-1", "status": "draft"}
    client = AsyncMock()
    client.__aenter__.return_value = client
    getattr(client, method).return_value = httpx.Response(200, json=payload)
    monkeypatch.setattr(agent_loop.httpx, "AsyncClient", MagicMock(return_value=client))
    monkeypatch.setattr(agent_loop, "internal_headers", lambda: {})

    result = await execute_skill(skill, args, BuiltinAgentConfig())

    assert result["status"] == "succeeded"
    assert result["data"] == payload
    assert result["evidence"]["operation"] == operation
    getattr(client, method).assert_awaited_once()
    assert getattr(client, method).call_args.kwargs["json"] == {
        key: value for key, value in args.items() if not key.endswith("_id")
    }


@pytest.mark.asyncio
async def test_e05_2_5_create_request_preserves_raw_success_and_body_without_path_params(
    monkeypatch,
):
    from app.ai import agent_loop

    args = {
        "title": "Fasteners M8",
        "items": [{"name": "Bolt M8", "qty": 100, "unit": "pcs"}],
        "deadline": "2026-10-01T12:00:00Z",
        "notes": "Original recipient payload",
        "requested_by": "buyer",
    }
    payload = {"id": "request-1", "status": "draft", **args}
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.post.return_value = httpx.Response(201, json=payload)
    monkeypatch.setattr(agent_loop.httpx, "AsyncClient", MagicMock(return_value=client))
    monkeypatch.setattr(agent_loop, "internal_headers", lambda: {})

    result = await execute_skill(
        {"method": "POST", "path": "/api/purchase-requests"},
        args,
        BuiltinAgentConfig(),
    )

    assert result["status"] == "succeeded"
    assert result["data"] == payload
    assert result["evidence"]["operation"] == "procurement.create_request"
    client.post.assert_awaited_once()
    assert client.post.call_args.kwargs["json"] == args


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
@pytest.mark.parametrize(
    "skill,args,expected_body,raw_response,operation",
    [
        (
            {"method": "POST", "path": "/api/documents/{document_id}/links"},
            {
                "document_id": "document-1",
                "linked_entity_type": "invoice",
                "linked_entity_id": "invoice-1",
                "link_type": "source",
            },
            {
                "linked_entity_type": "invoice",
                "linked_entity_id": "invoice-1",
                "link_type": "source",
            },
            {"id": "link-1", "linked_entity_id": "invoice-1"},
            "documents.link",
        ),
        (
            {"method": "POST", "path": "/api/email/drafts"},
            {"to_addresses": ["supplier@example.test"], "subject": "Draft"},
            {"to_addresses": ["supplier@example.test"], "subject": "Draft"},
            {"draft_id": "draft-1", "status": "draft"},
            "email.draft",
        ),
        (
            {"method": "POST", "path": "/api/payment-schedules"},
            {
                "invoice_id": "invoice-1",
                "due_date": "2026-10-01T00:00:00Z",
                "amount": 1000,
            },
            {
                "invoice_id": "invoice-1",
                "due_date": "2026-10-01T00:00:00Z",
                "amount": 1000,
            },
            {"id": "schedule-1", "status": "scheduled"},
            "payments.create_schedule",
        ),
    ],
)
async def test_e05_2_6_preserves_raw_success_response_and_original_body(
    monkeypatch, skill, args, expected_body, raw_response, operation
):
    from app.ai import agent_loop

    client = AsyncMock()
    client.__aenter__.return_value = client
    client.post.return_value = httpx.Response(201, json=raw_response)
    monkeypatch.setattr(agent_loop.httpx, "AsyncClient", MagicMock(return_value=client))
    monkeypatch.setattr(agent_loop, "internal_headers", lambda: {})

    result = await execute_skill(skill, args, BuiltinAgentConfig())

    assert result["status"] == "succeeded"
    assert result["data"] == raw_response
    assert result["evidence"]["operation"] == operation
    assert client.post.call_args.kwargs["json"] == expected_body


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "skill,args,expected_body,raw_response,operation,method",
    [
        (
            {"method": "POST", "path": "/api/normalization/norm-cards"},
            {
                "canonical_item_id": "item-1",
                "name": "Bolt M8",
                "specification": {"diameter": 8},
            },
            {
                "canonical_item_id": "item-1",
                "name": "Bolt M8",
                "specification": {"diameter": 8},
            },
            {"id": "card-1", "name": "Bolt M8"},
            "normalization.create_norm_card",
            "post",
        ),
        (
            {"method": "PATCH", "path": "/api/normalization/norm-cards/{card_id}"},
            {"card_id": "card-1", "name": "Updated Bolt M8"},
            {"name": "Updated Bolt M8"},
            {"id": "card-1", "name": "Updated Bolt M8"},
            "normalization.update_norm_card",
            "patch",
        ),
        (
            {
                "method": "PATCH",
                "path": "/api/normalization/canonical-items/{item_id}",
            },
            {"item_id": "item-1", "okpd2_code": "25.94.11", "gost_code": "7798-70"},
            {"okpd2_code": "25.94.11", "gost_code": "7798-70"},
            {"id": "item-1", "okpd2_code": "25.94.11", "gost_code": "7798-70"},
            "normalization.update_canonical_item",
            "patch",
        ),
    ],
)
async def test_e05_2_7_preserves_raw_success_response_and_original_body(
    monkeypatch, skill, args, expected_body, raw_response, operation, method
):
    from app.ai import agent_loop

    client = AsyncMock()
    client.__aenter__.return_value = client
    getattr(client, method).return_value = httpx.Response(200, json=raw_response)
    monkeypatch.setattr(agent_loop.httpx, "AsyncClient", MagicMock(return_value=client))
    monkeypatch.setattr(agent_loop, "internal_headers", lambda: {})

    result = await execute_skill(skill, args, BuiltinAgentConfig())

    assert result["status"] == "succeeded"
    assert result["data"] == raw_response
    assert result["evidence"]["operation"] == operation
    getattr(client, method).assert_awaited_once()
    assert getattr(client, method).call_args.kwargs["json"] == expected_body


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "skill,args,expected_body,raw_response,operation,method",
    [
        (
            {"method": "PATCH", "path": "/api/invoices/{invoice_id}"},
            {
                "invoice_id": "invoice-1",
                "invoice_number": "INV-2026-002",
                "notes": "Corrected by operator",
            },
            {"invoice_number": "INV-2026-002", "notes": "Corrected by operator"},
            {"id": "invoice-1", "invoice_number": "INV-2026-002"},
            "invoices.update",
            "patch",
        ),
        (
            {"method": "POST", "path": "/api/tool-catalog/suppliers"},
            {"name": "Tool supplier", "website": "https://supplier.example.test"},
            {"name": "Tool supplier", "website": "https://supplier.example.test"},
            {"id": "supplier-1", "name": "Tool supplier"},
            "tool_catalog.create_supplier",
            "post",
        ),
    ],
)
async def test_e05_2_8_preserves_raw_success_response_and_original_body(
    monkeypatch, skill, args, expected_body, raw_response, operation, method
):
    from app.ai import agent_loop

    client = AsyncMock()
    client.__aenter__.return_value = client
    getattr(client, method).return_value = httpx.Response(200, json=raw_response)
    monkeypatch.setattr(agent_loop.httpx, "AsyncClient", MagicMock(return_value=client))
    monkeypatch.setattr(agent_loop, "internal_headers", lambda: {})

    result = await execute_skill(skill, args, BuiltinAgentConfig())

    assert result["status"] == "succeeded"
    assert result["data"] == raw_response
    assert result["evidence"]["operation"] == operation
    getattr(client, method).assert_awaited_once()
    assert getattr(client, method).call_args.kwargs["json"] == expected_body


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "skill,args,expected_body,raw_response,operation",
    [
        (
            {"method": "POST", "path": "/api/invoices/{invoice_id}/validate"},
            {"invoice_id": "invoice-1"},
            {},
            {
                "invoice_id": "invoice-1",
                "is_valid": True,
                "errors": [],
                "overall_confidence": 0.98,
            },
            "invoices.validate",
        ),
        (
            {"method": "POST", "path": "/api/memory/sources/propose"},
            {
                "title": "Supplier catalog",
                "url": "https://supplier.example.test/catalog",
                "source_type": "supplier_catalog",
            },
            {
                "title": "Supplier catalog",
                "url": "https://supplier.example.test/catalog",
                "source_type": "supplier_catalog",
            },
            {
                "id": "source-1",
                "title": "Supplier catalog",
                "kind": "web_source",
            },
            "memory.source_propose",
        ),
    ],
)
async def test_e05_2_9_preserves_raw_success_response_and_original_body(
    monkeypatch, skill, args, expected_body, raw_response, operation
):
    from app.ai import agent_loop

    client = AsyncMock()
    client.__aenter__.return_value = client
    client.post.return_value = httpx.Response(200, json=raw_response)
    monkeypatch.setattr(agent_loop.httpx, "AsyncClient", MagicMock(return_value=client))
    monkeypatch.setattr(agent_loop, "internal_headers", lambda: {})

    result = await execute_skill(skill, args, BuiltinAgentConfig())

    assert result["status"] == "succeeded"
    assert result["data"] == raw_response
    assert result["evidence"]["operation"] == operation
    client.post.assert_awaited_once()
    assert client.post.call_args.kwargs["json"] == expected_body


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "skill,args,expected_body,raw_response,operation",
    [
        (
            {"method": "POST", "path": "/api/technology/corrections"},
            {
                "entity_type": "manufacturing_operation",
                "entity_id": "operation-1",
                "field_name": "setup_time_min",
                "old_value": "10",
                "new_value": "12",
                "corrected_by": "operator",
            },
            {
                "entity_type": "manufacturing_operation",
                "entity_id": "operation-1",
                "field_name": "setup_time_min",
                "old_value": "10",
                "new_value": "12",
                "corrected_by": "operator",
            },
            {"id": "correction-1", "field_name": "setup_time_min", "new_value": "12"},
            "tech.correction_record",
        ),
        (
            {"method": "POST", "path": "/api/technology/operation-templates"},
            {"operation_type": "turning", "name": "Finish turning"},
            {"operation_type": "turning", "name": "Finish turning"},
            {"id": "template-1", "operation_type": "turning", "name": "Finish turning"},
            "tech.operation_template_create",
        ),
    ],
)
async def test_e05_2_10_preserves_raw_success_response_and_original_body(
    monkeypatch, skill, args, expected_body, raw_response, operation
):
    from app.ai import agent_loop

    client = AsyncMock()
    client.__aenter__.return_value = client
    client.post.return_value = httpx.Response(200, json=raw_response)
    monkeypatch.setattr(agent_loop.httpx, "AsyncClient", MagicMock(return_value=client))
    monkeypatch.setattr(agent_loop, "internal_headers", lambda: {})

    result = await execute_skill(skill, args, BuiltinAgentConfig())

    assert result["status"] == "succeeded"
    assert result["data"] == raw_response
    assert result["evidence"]["operation"] == operation
    client.post.assert_awaited_once()
    assert client.post.call_args.kwargs["json"] == expected_body


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
        "bulk_confirm",  # E03 unknown
        "confirm_receipt",  # approval-gated write outside this reviewed slice
    ],
)
async def test_non_one_commit_groups_keep_legacy_success_contract(monkeypatch, action):
    from app.ai import agent_loop

    capability = "documents" if action == "ingest" else "warehouse"
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


@pytest.mark.parametrize(
    "action,operation",
    [
        ("classify", "documents.classify"),
        ("extract", "documents.extract"),
        ("reprocess", "documents.reprocess"),
    ],
)
def test_async_job_resolution_uses_exact_documents_capability_action(action, operation):
    resolved = async_job_operation(
        {"method": "POST", "path": "/api/agent/cap/documents"},
        {"action": action, "document_id": "document-1", "force": True},
    )

    assert resolved is not None
    assert resolved.name == operation


def test_async_job_allowlist_is_exact_reviewed_e05_3_subset():
    assert ASYNC_JOB_OPERATIONS == frozenset(
        {
            "documents.classify",
            "documents.extract",
            "documents.reprocess",
            "tech.generate_tp_from_drawing",
        }
    )


def test_async_job_resolution_uses_exact_tech_capability_action():
    resolved = async_job_operation(
        {"method": "POST", "path": "/api/agent/cap/tech"},
        {"action": "generate_tp_from_drawing", "drawing_id": "drawing-1"},
    )

    assert resolved is not None
    assert resolved.name == "tech.generate_tp_from_drawing"


@pytest.mark.parametrize(
    "skill,args",
    [
        (
            {"method": "POST", "path": "/api/documents/{document_id}/classify"},
            {"document_id": "document-1", "force": True},
        ),
        (
            {"method": "POST", "path": "/api/documents/{document_id}/extract"},
            {"document_id": "document-1", "force": True},
        ),
        (
            {"method": "POST", "path": "/api/agent/cap/documents"},
            {"action": "ingest", "document_id": "document-1"},
        ),
        (
            {"method": "POST", "path": "/api/technology/process-plans/generate-from-drawing"},
            {"drawing_id": "drawing-1"},
        ),
        (
            {"method": "POST", "path": "/api/agent/cap/tech"},
            {"action": "process_plan_list"},
        ),
    ],
)
def test_async_job_resolution_fails_closed_for_direct_routes_and_other_actions(skill, args):
    assert async_job_operation(skill, args) is None


@pytest.mark.parametrize(
    "skill,args,selected",
    [
        (
            {"method": "POST", "path": "/api/agent/cap/email"},
            {"action": "send", "draft_id": "draft-1"},
            True,
        ),
        (
            {"method": "POST", "path": "/api/email/drafts/{draft_id}/send"},
            {"draft_id": "draft-1"},
            False,
        ),
        (
            {"method": "POST", "path": "/api/agent/cap/email"},
            {"action": "send", "draft_id": "draft-1", "alias": True},
            True,
        ),
        (
            {"method": "POST", "path": "/api/agent/cap/email"},
            {"action": "fetch_new", "draft_id": "draft-1"},
            False,
        ),
        (
            {"method": "POST", "path": "/api/agent/cap/email/"},
            {"action": "send", "draft_id": "draft-1"},
            False,
        ),
    ],
)
def test_email_send_queue_resolution_requires_exact_capability_identity(skill, args, selected):
    resolved = email_send_queue_operation(skill, args)

    assert (resolved is not None) is selected
    if selected:
        assert resolved.name == "email.send"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action,operation",
    [
        ("classify", "documents.classify"),
        ("extract", "documents.extract"),
        ("reprocess", "documents.reprocess"),
    ],
)
async def test_async_job_queue_acceptance_preserves_raw_response_args_and_headers(
    monkeypatch, action, operation
):
    from app.ai import agent_loop, tool_transport

    payload = {
        "task_id": f"task-{action}",
        "document_id": "document-1",
        "status": "queued",
        "recipient_metadata": {"queue": "gpu"},
    }
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.post.return_value = httpx.Response(202, json=payload)
    monkeypatch.setattr(agent_loop.httpx, "AsyncClient", MagicMock(return_value=client))
    monkeypatch.setattr(agent_loop, "internal_headers", lambda: {"Authorization": "test-context"})
    seen_args = []
    real_resolver = tool_transport.async_job_operation

    def resolver_with_identity(received_skill, received_args):
        seen_args.append(received_args)
        return real_resolver(received_skill, received_args)

    monkeypatch.setattr(tool_transport, "async_job_operation", resolver_with_identity)
    args = {"action": action, "document_id": "document-1", "force": True}
    original_args = args.copy()

    result = await execute_skill(
        {"method": "POST", "path": "/api/agent/cap/documents"},
        args,
        BuiltinAgentConfig(),
        idempotency_key="logical-action:attempt",
    )

    assert result["version"] == 1
    assert result["status"] == "partial"
    assert result["error_code"] == "job_queued"
    assert result["retryable"] is False
    assert result["data"] == payload
    assert result["evidence"]["operation"] == operation
    assert result["evidence"]["recipient_outcome"] == "accepted"
    assert result["checkpoint"] == {
        "task_id": f"task-{action}",
        "document_id": "document-1",
        "status": "queued",
    }
    assert seen_args == [args]
    assert seen_args[0] is args
    assert args == original_args
    assert client.post.call_args.kwargs["json"] == original_args
    assert client.post.call_args.kwargs["headers"] == {
        "Authorization": "test-context",
        "X-Agent-Idempotency-Key": "logical-action:attempt",
    }


@pytest.mark.asyncio
async def test_tech_async_job_queue_acceptance_preserves_plan_id_args_and_headers(monkeypatch):
    from app.ai import agent_loop, tool_transport

    payload = {"task_id": "task-tp", "plan_id": "plan-1", "status": "queued"}
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.post.return_value = httpx.Response(202, json=payload)
    monkeypatch.setattr(agent_loop.httpx, "AsyncClient", MagicMock(return_value=client))
    monkeypatch.setattr(agent_loop, "internal_headers", lambda: {"Authorization": "test-context"})
    real_resolver = tool_transport.async_job_operation
    seen_args = []

    def resolver_with_identity(received_skill, received_args):
        seen_args.append(received_args)
        return real_resolver(received_skill, received_args)

    monkeypatch.setattr(tool_transport, "async_job_operation", resolver_with_identity)
    args = {"action": "generate_tp_from_drawing", "drawing_id": "drawing-1"}
    original_args = args.copy()

    result = await execute_skill(
        {"method": "POST", "path": "/api/agent/cap/tech"},
        args,
        BuiltinAgentConfig(),
        idempotency_key="logical-action:attempt",
    )

    assert result["status"] == "partial"
    assert result["error_code"] == "job_queued"
    assert result["data"] == payload
    assert result["evidence"]["operation"] == "tech.generate_tp_from_drawing"
    assert result["checkpoint"] == {"task_id": "task-tp", "plan_id": "plan-1", "status": "queued"}
    assert seen_args == [args]
    assert seen_args[0] is args
    assert args == original_args
    assert client.post.call_args.kwargs["json"] == original_args
    assert client.post.call_args.kwargs["headers"] == {
        "Authorization": "test-context",
        "X-Agent-Idempotency-Key": "logical-action:attempt",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [400, 404, 422])
@pytest.mark.parametrize(
    "skill,args",
    [
        (
            {"method": "POST", "path": "/api/agent/cap/documents"},
            {"action": "extract", "document_id": "document-1"},
        ),
        (
            {"method": "POST", "path": "/api/agent/cap/tech"},
            {"action": "generate_tp_from_drawing", "drawing_id": "drawing-1"},
        ),
    ],
)
async def test_async_job_4xx_is_failed_without_retry(monkeypatch, status_code, skill, args):
    from app.ai import agent_loop

    payload = {"detail": {"error_code": "rejected"}}
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.post.side_effect = [
        httpx.Response(status_code, json=payload),
        httpx.Response(202, json={"task_id": "duplicate"}),
    ]
    monkeypatch.setattr(agent_loop.httpx, "AsyncClient", MagicMock(return_value=client))
    monkeypatch.setattr(agent_loop, "internal_headers", lambda: {})

    result = await execute_skill(
        skill,
        args,
        BuiltinAgentConfig(),
    )

    assert result["status"] == "failed"
    assert result["error_code"] == f"http_{status_code}"
    assert result["retryable"] is False
    assert result["data"] == payload
    assert result["evidence"]["recipient_outcome"] == "rejected"
    client.post.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure,reason",
    [
        (httpx.Response(302, json={"detail": "redirect"}), "http_302"),
        (httpx.Response(503, json={"detail": "unavailable"}), "http_503"),
        (httpx.ReadTimeout("private timeout"), "transport_ReadTimeout"),
        (RuntimeError("private exception"), "exception_RuntimeError"),
    ],
)
@pytest.mark.parametrize(
    "skill,args",
    [
        (
            {"method": "POST", "path": "/api/agent/cap/documents"},
            {"action": "classify", "document_id": "document-1"},
        ),
        (
            {"method": "POST", "path": "/api/agent/cap/tech"},
            {"action": "generate_tp_from_drawing", "drawing_id": "drawing-1"},
        ),
    ],
)
async def test_async_job_post_dispatch_ambiguity_is_unknown_without_retry(
    monkeypatch, failure, reason, skill, args
):
    from app.ai import agent_loop

    client = AsyncMock()
    client.__aenter__.return_value = client
    client.post.side_effect = [failure, httpx.Response(202, json={"task_id": "duplicate"})]
    monkeypatch.setattr(agent_loop.httpx, "AsyncClient", MagicMock(return_value=client))
    monkeypatch.setattr(agent_loop, "internal_headers", lambda: {})
    sleep = AsyncMock()
    monkeypatch.setattr(agent_loop.asyncio, "sleep", sleep)

    result = await execute_skill(
        skill,
        args,
        BuiltinAgentConfig(),
    )

    assert result["status"] == "outcome_unknown"
    assert result["retryable"] is False
    assert result["evidence"]["reason"] == reason
    assert result["evidence"]["recipient_outcome"] == "unconfirmed"
    assert "private" not in str(result)
    client.post.assert_awaited_once()
    sleep.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "skill,args",
    [
        (
            {"method": "POST", "path": "/api/agent/cap/documents"},
            {"action": "reprocess", "document_id": "document-1"},
        ),
        (
            {"method": "POST", "path": "/api/agent/cap/tech"},
            {"action": "generate_tp_from_drawing", "drawing_id": "drawing-1"},
        ),
    ],
)
async def test_async_job_pre_dispatch_failure_is_failed(monkeypatch, skill, args):
    from app.ai import agent_loop

    client = AsyncMock()
    client.__aenter__.return_value = client
    monkeypatch.setattr(agent_loop.httpx, "AsyncClient", MagicMock(return_value=client))
    monkeypatch.setattr(
        agent_loop,
        "internal_headers",
        MagicMock(side_effect=RuntimeError("private header detail")),
    )

    result = await execute_skill(
        skill,
        args,
        BuiltinAgentConfig(),
    )

    assert result["status"] == "failed"
    assert result["error_code"] == "async_dispatch_failed"
    assert result["retryable"] is False
    assert result["evidence"]["dispatch_attempted"] is False
    assert "private" not in str(result)
    client.post.assert_not_awaited()


@pytest.mark.asyncio
async def test_email_send_queue_acceptance_is_one_attempt_and_binds_requested_draft(monkeypatch):
    from app.ai import agent_loop

    payload = {"status": "queued", "task_id": "task-email", "draft_id": "draft-1"}
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.post.return_value = httpx.Response(202, json=payload)
    monkeypatch.setattr(agent_loop.httpx, "AsyncClient", MagicMock(return_value=client))
    monkeypatch.setattr(agent_loop, "internal_headers", lambda: {"Authorization": "test-context"})
    args = {"action": "send", "draft_id": "draft-1", "expected_digest": "digest-1"}

    result = await execute_skill(
        {"method": "POST", "path": "/api/agent/cap/email"},
        args,
        BuiltinAgentConfig(),
        approval_granted=True,
    )

    assert result["status"] == "partial"
    assert result["error_code"] == "job_queued"
    assert result["retryable"] is False
    assert result["checkpoint"] == {
        "task_id": "task-email",
        "draft_id": "draft-1",
        "status": "queued",
    }
    assert result["evidence"]["smtp_delivery"] == "not_confirmed"
    client.post.assert_awaited_once()
    sent = client.post.await_args.kwargs
    assert sent["json"] == args
    assert sent["headers"]["X-Agent-Approval-Digest"] == capability_args_digest(args)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response,expected_status,expected_code",
    [
        (
            httpx.Response(
                202, json={"status": "queued", "task_id": "task-1", "draft_id": "other"}
            ),
            "failed",
            "invalid_email_send_queue_contract",
        ),
        (
            httpx.Response(400, json={"detail": {"error_code": "blocked_by_risk"}}),
            "failed",
            "http_400",
        ),
        (
            httpx.Response(503, json={"detail": "unavailable"}),
            "outcome_unknown",
            "tool_outcome_unknown",
        ),
        (httpx.ReadTimeout("timeout"), "outcome_unknown", "tool_outcome_unknown"),
    ],
)
async def test_email_send_queue_rejection_or_ambiguity_never_retries(
    monkeypatch, response, expected_status, expected_code
):
    from app.ai import agent_loop

    client = AsyncMock()
    client.__aenter__.return_value = client
    client.post.side_effect = [response, httpx.Response(202, json={"status": "queued"})]
    monkeypatch.setattr(agent_loop.httpx, "AsyncClient", MagicMock(return_value=client))
    monkeypatch.setattr(agent_loop, "internal_headers", lambda: {})
    sleep = AsyncMock()
    monkeypatch.setattr(agent_loop.asyncio, "sleep", sleep)

    result = await execute_skill(
        {"method": "POST", "path": "/api/agent/cap/email"},
        {"action": "send", "draft_id": "draft-1"},
        BuiltinAgentConfig(),
    )

    assert result["status"] == expected_status
    assert result["error_code"] == expected_code
    assert result["retryable"] is False
    assert result["evidence"]["smtp_delivery"] == "not_confirmed"
    client.post.assert_awaited_once()
    sleep.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "skill,args",
    [
        (
            {"method": "POST", "path": "/api/email/drafts/{draft_id}/send"},
            {"draft_id": "draft-1"},
        ),
        (
            {"method": "POST", "path": "/api/agent/cap/email"},
            {"action": "fetch_new"},
        ),
    ],
)
async def test_unselected_email_routes_keep_legacy_payload(monkeypatch, skill, args):
    """Only the exact gateway identity receives the external queue adapter."""
    from app.ai import agent_loop

    payload = {"status": "queued", "job_id": "legacy-job"}
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.post.return_value = httpx.Response(200, json=payload)
    monkeypatch.setattr(agent_loop.httpx, "AsyncClient", MagicMock(return_value=client))
    monkeypatch.setattr(agent_loop, "internal_headers", lambda: {})

    result = await execute_skill(skill, args, BuiltinAgentConfig())

    assert result == payload
    client.post.assert_awaited_once()
