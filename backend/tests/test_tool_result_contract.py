"""Pure unit matrix for the version=1 ToolResult contract."""

import pytest
from pydantic import ValidationError

from app.ai.tool_result import (
    LegacyToolResultContract,
    ToolResult,
    classify_tool_result,
    normalize_http_one_db_commit_response,
    normalize_tool_result,
    result_failed,
)
from app.ai.tool_transport import unknown_outcome


@pytest.mark.parametrize(
    "status,invocation_succeeded",
    [
        ("succeeded", True),
        ("failed", False),
        ("partial", False),
        ("waiting_approval", False),
        ("outcome_unknown", False),
    ],
)
def test_each_status_has_explicit_non_terminal_semantics(status, invocation_succeeded):
    result = ToolResult(status=status, error_code=None)

    unaccepted = classify_tool_result(result)
    accepted = classify_tool_result(result, accepted=True)

    assert unaccepted.invocation_succeeded is invocation_succeeded
    assert unaccepted.work_completed is False
    assert accepted.work_completed is invocation_succeeded
    assert unaccepted.safely_retryable is False
    assert unaccepted.result.status == status


def test_retry_is_separate_and_only_failed_invocations_may_be_retryable():
    failed = ToolResult(status="failed", error_code="read_timeout", retryable=True)
    assert classify_tool_result(failed).safely_retryable is True

    for status in ("succeeded", "partial", "waiting_approval", "outcome_unknown"):
        with pytest.raises(ValidationError, match="only a failed result"):
            ToolResult(status=status, retryable=True)


@pytest.mark.parametrize(
    "marker,expected_code",
    [
        ({"error": "bad input"}, "domain_error"),
        ({"error_code": "invalid_widget"}, "invalid_widget"),
        ({"errors": ["bad input"]}, "domain_error"),
        ({"built": False}, "not_built"),
    ],
)
def test_succeeded_with_direct_error_marker_is_never_success(marker, expected_code):
    normalized = normalize_tool_result({"version": 1, "status": "succeeded", **marker})

    assert normalized.status == "failed"
    assert normalized.error_code == expected_code
    assert classify_tool_result(normalized, accepted=True).work_completed is False


def test_succeeded_with_nested_domain_error_is_never_success():
    payload = {
        "version": 1,
        "status": "succeeded",
        "data": {"result": {"domain": {"error_code": "validation_failed"}}},
    }

    normalized = normalize_tool_result(payload)

    assert normalized.status == "failed"
    assert normalized.error_code == "validation_failed"
    assert normalized.data == payload


def test_succeeded_with_list_data_markers_is_not_misclassified_as_domain_failure():
    payload = {
        "version": 1,
        "status": "succeeded",
        "data": [{"status": "running", "error": "historical", "built": False}],
    }

    normalized = normalize_tool_result(payload)

    assert normalized.status == "succeeded"
    assert normalized.data == payload["data"]


def test_succeeded_with_metadata_status_is_not_misclassified_as_domain_failure():
    payload = {
        "version": 1,
        "status": "succeeded",
        "data": {"metadata": {"status": "running", "error": "historical", "built": False}},
    }

    normalized = normalize_tool_result(payload)

    assert normalized.status == "succeeded"
    assert normalized.data == payload["data"]


@pytest.mark.parametrize("status", ["queued", "running"])
def test_succeeded_with_domain_progress_status_remains_successful_read_data(status):
    payload = {
        "version": 1,
        "status": "succeeded",
        "data": {"result": {"job_id": "job-1", "status": status}},
    }

    normalized = normalize_tool_result(payload)

    assert normalized.status == "succeeded"
    assert normalized.data == payload["data"]


@pytest.mark.parametrize(
    "nested_status,expected_code",
    [
        ("failed", "domain_error"),
        ("error", "domain_error"),
        ("partial", "domain_partial"),
        ("waiting_approval", "domain_waiting_approval"),
        ("outcome_unknown", "domain_outcome_unknown"),
    ],
)
def test_succeeded_with_nested_non_success_status_is_never_complete(nested_status, expected_code):
    normalized = normalize_tool_result(
        {
            "version": 1,
            "status": "succeeded",
            "data": {"result": {"status": nested_status}},
        }
    )

    assert normalized.status == "failed"
    assert normalized.error_code == expected_code
    classification = classify_tool_result(normalized, accepted=True)
    assert classification.invocation_succeeded is False
    assert classification.work_completed is False


@pytest.mark.parametrize("payload", [None, [], ["ok"], "ok", 1, True])
def test_unknown_unversioned_non_mapping_result_is_not_guessed_as_success(payload):
    normalized = normalize_tool_result(payload)

    assert normalized.status == "failed"
    assert normalized.error_code == "unrecognized_tool_result"
    assert normalized.data == payload


def test_unversioned_mapping_requires_an_explicit_legacy_contract():
    unknown = normalize_tool_result({"items": []})

    assert unknown.status == "failed"
    assert unknown.evidence == {"reason": "legacy_contract_required"}


def test_unknown_legacy_contract_does_not_guess_success():
    normalized = normalize_tool_result({"items": []}, legacy_contract="future_adapter")

    assert normalized.status == "failed"
    assert normalized.evidence == {"reason": "unknown_legacy_contract"}


def test_unknown_version_does_not_fall_back_to_legacy_contract():
    normalized = normalize_tool_result(
        {"version": 2, "status": "succeeded", "data": {"id": "x"}},
        legacy_contract=LegacyToolResultContract.TOOL_TRANSPORT_UNKNOWN_OUTCOME_V0,
    )

    assert normalized.status == "failed"
    assert normalized.error_code == "unrecognized_tool_result"
    assert normalized.evidence == {"reason": "unknown_tool_result_version"}


@pytest.mark.parametrize("data", [None, [1, "two"], "plain text"])
def test_version_one_data_preserves_null_list_and_string(data):
    normalized = normalize_tool_result({"version": 1, "status": "succeeded", "data": data})

    assert normalized.status == "succeeded"
    assert normalized.data == data


@pytest.mark.parametrize("status", ["partial", "waiting_approval", "outcome_unknown"])
def test_non_terminal_status_and_reason_are_preserved(status):
    normalized = normalize_tool_result(
        {
            "version": 1,
            "status": status,
            "error_code": f"reason_for_{status}",
            "evidence": {"detail": "retained"},
            "checkpoint": {"cursor": "next"},
        }
    )

    assert normalized.status == status
    assert normalized.error_code == f"reason_for_{status}"
    assert normalized.evidence == {"detail": "retained"}
    assert normalized.checkpoint == {"cursor": "next"}
    assert classify_tool_result(normalized, accepted=True).work_completed is False


def test_invalid_v1_shape_is_an_explicit_failed_result():
    normalized = normalize_tool_result(
        {"version": 1, "status": "succeeded", "data": {}, "unexpected": True}
    )

    assert normalized.status == "failed"
    assert normalized.error_code == "invalid_tool_result_contract"
    assert normalized.evidence["validation_errors"]
    normalized.model_dump_json()


@pytest.mark.parametrize(
    "version",
    [
        True,
        1.0,
    ],
)
def test_version_requires_exact_json_integer(version):
    normalized = normalize_tool_result({"version": version, "status": "succeeded"})

    assert normalized.status == "failed"
    assert normalized.evidence == {"reason": "unknown_tool_result_version"}
    with pytest.raises(ValidationError, match="version must be the integer 1"):
        ToolResult(version=version, status="succeeded")


def test_tool_result_is_immutable_and_copied_instances_are_revalidated():
    result = ToolResult(status="succeeded", data={"id": "x"})
    with pytest.raises(ValidationError, match="frozen"):
        result.status = "failed"

    bypassed = result.model_copy(update={"error_code": "copied_without_validation"})
    normalized = normalize_tool_result(bypassed)

    assert normalized.status == "failed"
    assert normalized.error_code == "copied_without_validation"

    nested_mutation = ToolResult(status="succeeded", data={"result": {}})
    nested_mutation.data["result"]["error"] = "mutated after validation"
    classified = classify_tool_result(nested_mutation, accepted=True)
    assert classified.invocation_succeeded is False
    assert classified.work_completed is False


def test_acceptance_decision_requires_a_real_boolean():
    with pytest.raises(TypeError, match="accepted must be a boolean"):
        classify_tool_result(ToolResult(status="succeeded"), accepted="true")


def test_real_transport_unknown_outcome_keeps_reason_and_is_never_retryable():
    payload = unknown_outcome("HTTP 503")

    normalized = normalize_tool_result(
        payload,
        legacy_contract=LegacyToolResultContract.TOOL_TRANSPORT_UNKNOWN_OUTCOME_V0,
    )

    assert normalized.status == "outcome_unknown"
    assert normalized.error_code == "tool_outcome_unknown"
    assert normalized.retryable is False
    assert normalized.data == payload
    assert classify_tool_result(normalized).safely_retryable is False


def test_legacy_unknown_outcome_cannot_enable_retry_with_contradictory_flag():
    payload = {**unknown_outcome("dispatch timed out"), "retryable": True}

    normalized = normalize_tool_result(
        payload,
        legacy_contract=LegacyToolResultContract.TOOL_TRANSPORT_UNKNOWN_OUTCOME_V0,
    )

    assert normalized.status == "outcome_unknown"
    assert normalized.retryable is False
    assert normalized.evidence["contract_error"] == "outcome_unknown_cannot_be_retryable"


def test_narrow_legacy_contract_rejects_unrelated_success_mapping():
    normalized = normalize_tool_result(
        {"items": []},
        legacy_contract=LegacyToolResultContract.TOOL_TRANSPORT_UNKNOWN_OUTCOME_V0,
    )

    assert normalized.status == "failed"
    assert normalized.evidence == {"reason": "legacy_contract_mismatch"}


@pytest.mark.parametrize(
    "payload,failed",
    [
        ({"items": []}, False),
        ({"status": "succeeded", "data": "ok"}, False),
        ({"error": "boom"}, True),
        ({"error_code": "bad"}, True),
        ({"errors": ["bad"]}, True),
        ({"built": False}, True),
        ({"status": "partial"}, True),
        ({"status": "waiting_approval"}, True),
        ({"status": "outcome_unknown"}, True),
        ({"status": []}, False),
        (None, False),
        ([], False),
        ("ok", False),
    ],
)
def test_result_failed_preserves_legacy_consumer_compatibility_without_false_success(
    payload, failed
):
    assert result_failed(payload) is failed


@pytest.mark.parametrize("recipient_status", ["proposed", "approved"])
def test_one_db_commit_recipient_status_is_raw_data(recipient_status):
    payload = {"id": "record-1", "status": recipient_status}

    normalized = normalize_http_one_db_commit_response(payload, operation="warehouse.create_item")

    assert normalized.status == "succeeded"
    assert normalized.data == payload


def test_one_db_commit_invalid_v1_is_failed_without_double_wrapping():
    payload = {"version": 1, "status": "succeeded", "unexpected": True}

    normalized = normalize_http_one_db_commit_response(payload, operation="warehouse.create_item")

    assert normalized.status == "failed"
    assert normalized.error_code == "invalid_tool_result_contract"
    assert normalized.data == payload


@pytest.mark.parametrize("status", ["partial", "waiting_approval", "outcome_unknown"])
def test_one_db_commit_legacy_nonterminal_status_is_preserved(status):
    payload = {
        "status": status,
        "error_code": f"recipient_{status}",
        "reason": "recipient needs reconciliation",
        "retryable": True,
        "checkpoint": {"cursor": "next"},
        "evidence": {"recipient_revision": 7},
    }

    normalized = normalize_http_one_db_commit_response(payload, operation="warehouse.create_item")

    assert normalized.status == status
    assert normalized.data == payload
    assert normalized.error_code == f"recipient_{status}"
    assert normalized.retryable is False
    assert normalized.checkpoint == {"cursor": "next"}
    assert normalized.evidence["reason"] == "recipient needs reconciliation"
    assert normalized.evidence["recipient_evidence"] == {"recipient_revision": 7}
    assert result_failed(payload) is True


def test_one_db_commit_legacy_nonterminal_without_code_keeps_reason_and_status():
    payload = {"status": "partial", "error": "only some rows committed"}

    normalized = normalize_http_one_db_commit_response(
        payload, operation="analytics.table_create_view"
    )

    assert normalized.status == "partial"
    assert normalized.error_code == "domain_partial"
    assert normalized.evidence["reason"] == "only some rows committed"
    assert normalized.data == payload
