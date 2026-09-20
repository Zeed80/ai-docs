"""Versioned, fail-closed outcomes for tool and domain adapter execution.

``ToolResult`` describes one invocation. It deliberately does not claim that
the parent work item is complete: that requires a separate acceptance decision.
The transition rules for version 1 are:

==================  =====================  ==================  =================
status              invocation succeeded  work complete       repeat same call
==================  =====================  ==================  =================
succeeded           yes                    only if accepted    no
failed              no                     no                  iff retryable
partial             no                     no                  no
waiting_approval    no                     no                  no
outcome_unknown     no                     no                  no
==================  =====================  ==================  =================

Legacy payloads are ambiguous by definition. They may only be normalized when
the caller names a known adapter contract; otherwise they become an explicit
``unrecognized_result`` failure rather than an inferred success.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    ValidationError,
    field_validator,
    model_validator,
)

ToolResultStatus = Literal[
    "succeeded",
    "failed",
    "partial",
    "waiting_approval",
    "outcome_unknown",
]


# Each reviewed asynchronous operation has one recipient-issued identity in
# addition to its Celery task id.  Keeping this contract next to the result
# normalizer makes the response shape explicit without creating per-route
# transport branches.
ASYNC_JOB_IDENTITY_FIELDS: dict[str, str] = {
    "documents.classify": "document_id",
    "documents.extract": "document_id",
    "documents.reprocess": "document_id",
    "tech.generate_tp_from_drawing": "plan_id",
}


class ToolResult(BaseModel):
    """The canonical version=1 result envelope for one tool invocation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1] = 1
    status: ToolResultStatus
    data: Any = None
    error_code: str | None = None
    retryable: StrictBool = False
    evidence: dict[str, Any] = Field(default_factory=dict)
    checkpoint: dict[str, Any] | None = None

    @field_validator("version", mode="before")
    @classmethod
    def require_exact_version_one(cls, value: Any) -> Any:
        # bool and float compare equal to 1 in Python and pass Literal[1]
        # validation unless the JSON type is checked explicitly.
        if type(value) is not int or value != 1:
            raise ValueError("version must be the integer 1")
        return value

    @model_validator(mode="after")
    def validate_decision_fields(self) -> ToolResult:
        if self.status == "succeeded" and self.error_code is not None:
            raise ValueError("a succeeded result cannot have error_code")
        if self.status == "succeeded" and _domain_failure(self.data) is not None:
            raise ValueError("a succeeded result cannot contain a domain error")
        if self.retryable and self.status != "failed":
            raise ValueError("only a failed result may repeat the same invocation")
        return self


@dataclass(frozen=True, slots=True)
class ToolResultClassification:
    """Three independent decisions derived from a validated invocation result."""

    result: ToolResult
    invocation_succeeded: bool
    work_completed: bool
    safely_retryable: bool


class LegacyToolResultContract(str, Enum):
    """Legacy shapes whose semantics have been explicitly reviewed.

    The transport's pre-v1 unknown-outcome shape is narrow and deterministic.
    Successful domain response contracts remain adapter-specific work for E05;
    there is intentionally no generic "mapping means success" normalizer here.
    """

    TOOL_TRANSPORT_UNKNOWN_OUTCOME_V0 = "tool_transport_unknown_outcome_v0"


class CriterionVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")
    criterion_id: str
    ok: StrictBool
    reason: str
    checks: list[str] = Field(default_factory=list)


class VerifierResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    verdicts: list[CriterionVerdict]


def classify_tool_result(result: ToolResult, *, accepted: bool = False) -> ToolResultClassification:
    """Keep invocation success, whole-work acceptance and retry safety separate."""

    if type(accepted) is not bool:
        raise TypeError("accepted must be a boolean acceptance decision")
    result = normalize_tool_result(result)
    invocation_succeeded = result.status == "succeeded"
    return ToolResultClassification(
        result=result,
        invocation_succeeded=invocation_succeeded,
        work_completed=invocation_succeeded and accepted,
        safely_retryable=result.status == "failed" and result.retryable,
    )


def normalize_tool_result(
    payload: Any,
    *,
    legacy_contract: LegacyToolResultContract | str | None = None,
) -> ToolResult:
    """Return a canonical result without guessing the meaning of unknown payloads.

    Versioned payloads are always interpreted as versioned contracts. Therefore
    an unknown version cannot fall back to legacy handling, even when a legacy
    contract was supplied.
    """

    if isinstance(payload, ToolResult):
        # ``model_copy(update=...)`` does not validate updates in Pydantic.
        # Re-enter through the serialized envelope rather than trusting an
        # apparently typed instance supplied across a boundary.
        payload = payload.model_dump(mode="python")
    if isinstance(payload, Mapping) and "version" in payload:
        return _normalize_versioned(payload)
    if legacy_contract is None:
        return _unrecognized(payload, "legacy_contract_required")
    try:
        contract = LegacyToolResultContract(legacy_contract)
    except (TypeError, ValueError):
        return _unrecognized(payload, "unknown_legacy_contract")
    if contract is LegacyToolResultContract.TOOL_TRANSPORT_UNKNOWN_OUTCOME_V0:
        return _normalize_transport_unknown_outcome_v0(payload)
    return _unrecognized(payload, "unknown_legacy_contract")


def normalize_http_read_response(payload: Any) -> ToolResult:
    """Normalize a reviewed HTTP read response at the agent boundary.

    This is deliberately an adapter-specific contract rather than a general
    legacy normalizer. A successful HTTP response is a successful invocation
    when it does not carry an explicit domain failure. Domain progress such as
    ``queued`` or ``running`` remains raw read data; command-acceptance semantics
    belong to the concrete async adapter. Versioned payloads keep their existing
    envelope, which avoids wrapping a valid ``ToolResult`` inside another
    result's ``data``.
    """

    if isinstance(payload, ToolResult) or (isinstance(payload, Mapping) and "version" in payload):
        normalized = normalize_tool_result(payload)
        if normalized.status != "succeeded":
            return normalized
        failure = _domain_failure(normalized.data)
        if failure is None:
            return normalized
        return ToolResult(
            status="failed",
            data=(
                payload.model_dump(mode="json")
                if isinstance(payload, ToolResult)
                else dict(payload)
            ),
            error_code=failure,
            evidence={"adapter_contract": "http_read_response_v1"},
        )

    failure = _domain_failure(payload)
    if failure is not None:
        return ToolResult(
            status="failed",
            data=payload,
            error_code=failure,
            evidence={"adapter_contract": "http_read_response_v1"},
        )
    return ToolResult(
        status="succeeded",
        data=payload,
        evidence={"adapter_contract": "http_read_response_v1"},
    )


def normalize_http_one_db_commit_response(payload: Any, *, operation: str) -> ToolResult:
    """Normalize a reviewed E03 one-DB-commit response at the agent boundary.

    A 2xx response confirms the recipient invocation, but explicit domain error
    markers still win over HTTP success. Recipient lifecycle words such as
    ``proposed`` and ``approved`` are ordinary response data, not ToolResult
    statuses. Versioned envelopes are revalidated and never wrapped twice.
    """

    if isinstance(payload, ToolResult) or (isinstance(payload, Mapping) and "version" in payload):
        normalized = normalize_tool_result(payload)
        if normalized.status != "succeeded":
            return normalized
        failure = _domain_failure(normalized.data)
        if failure is None:
            return normalized
        return ToolResult(
            status="failed",
            data=(
                payload.model_dump(mode="json")
                if isinstance(payload, ToolResult)
                else dict(payload)
            ),
            error_code=failure,
            evidence={
                "adapter_contract": "http_one_db_commit_response_v1",
                "operation": operation,
            },
        )

    nonterminal = _normalize_one_db_commit_nonterminal(payload, operation=operation)
    if nonterminal is not None:
        return nonterminal

    failure = _domain_failure(payload)
    if failure is not None:
        return ToolResult(
            status="failed",
            data=payload,
            error_code=failure,
            evidence={
                "adapter_contract": "http_one_db_commit_response_v1",
                "operation": operation,
            },
        )
    return ToolResult(
        status="succeeded",
        data=payload,
        evidence={
            "adapter_contract": "http_one_db_commit_response_v1",
            "operation": operation,
        },
    )


def normalize_http_async_job_response(payload: Any, *, operation: str) -> ToolResult:
    """Normalize acceptance of one reviewed asynchronous recipient job.

    The HTTP call is complete when the recipient accepts the job, but the work
    itself is not.  Only the exact legacy ``TaskResponse`` shape is accepted;
    a versioned ``succeeded`` envelope containing queued work is contradictory
    and must not turn queue acceptance into completion.
    """

    raw: Any
    if isinstance(payload, ToolResult):
        raw = payload.model_dump(mode="json")
    elif isinstance(payload, Mapping):
        raw = dict(payload)
    else:
        raw = payload

    identity_field = ASYNC_JOB_IDENTITY_FIELDS.get(operation)
    contract_error: str | None = None
    recipient_error_code: str | None = None
    if identity_field is None:
        contract_error = "unknown_async_job_operation"
    elif not isinstance(raw, Mapping):
        contract_error = "response_not_mapping"
    elif "version" in raw:
        normalized = normalize_tool_result(raw)
        if raw.get("status") != "succeeded":
            return normalized
        data = raw.get("data")
        contract_error = (
            "versioned_succeeded_contains_queued_job"
            if isinstance(data, Mapping) and data.get("status") == "queued"
            else "versioned_succeeded_not_async_acceptance"
        )
    else:
        recipient_error_code = _domain_failure(raw)
        if recipient_error_code is not None:
            contract_error = "recipient_domain_failure"
        elif not _nonempty_string(raw.get("task_id")):
            contract_error = "missing_task_id"
        elif not _nonempty_string(raw.get(identity_field)):
            contract_error = f"missing_{identity_field}"
        elif raw.get("status") != "queued":
            contract_error = "status_not_queued"

    evidence: dict[str, Any] = {
        "adapter_contract": "http_async_job_response_v1",
        "operation": operation,
    }
    if contract_error is not None:
        evidence["contract_error"] = contract_error
        if recipient_error_code is not None:
            evidence["recipient_error_code"] = recipient_error_code
        return ToolResult(
            status="failed",
            data=raw,
            error_code="invalid_async_job_contract",
            retryable=False,
            evidence=evidence,
        )

    task_id = raw["task_id"]
    identity = raw[identity_field]
    evidence.update(
        {
            "task_id": task_id,
            "recipient_outcome": "accepted",
        }
    )
    return ToolResult(
        status="partial",
        data=raw,
        error_code="job_queued",
        retryable=False,
        evidence=evidence,
        checkpoint={
            "task_id": task_id,
            identity_field: identity,
            "status": "queued",
        },
    )


def _normalize_one_db_commit_nonterminal(payload: Any, *, operation: str) -> ToolResult | None:
    """Preserve reviewed legacy nonterminal outcomes without calling them failed."""

    if not isinstance(payload, Mapping):
        return None
    status = payload.get("status")
    if status not in {"partial", "waiting_approval", "outcome_unknown"}:
        return None

    raw = dict(payload)
    explicit_error_code = raw.get("error_code")
    error_code = (
        explicit_error_code
        if isinstance(explicit_error_code, str) and explicit_error_code
        else f"domain_{status}"
    )
    evidence: dict[str, Any] = {
        "adapter_contract": "http_one_db_commit_response_v1",
        "operation": operation,
        "legacy_status": status,
    }
    recipient_evidence = raw.get("evidence")
    if isinstance(recipient_evidence, Mapping):
        evidence["recipient_evidence"] = dict(recipient_evidence)
    reason = raw.get("reason")
    if not isinstance(reason, str) or not reason:
        error = raw.get("error")
        reason = error if isinstance(error, str) and error else None
    if reason is not None:
        evidence["reason"] = reason

    checkpoint = raw.get("checkpoint")
    return ToolResult(
        status=status,
        data=raw,
        error_code=error_code,
        retryable=False,
        evidence=evidence,
        checkpoint=dict(checkpoint) if isinstance(checkpoint, Mapping) else None,
    )


def result_failed(result: Any) -> bool:
    """Compatibility predicate for the legacy work-order consumer.

    This boolean necessarily loses the non-terminal reason, so new consumers
    must use ``normalize_tool_result`` and inspect ``status``. It intentionally
    retains the old non-mapping behavior because its caller accesses ``.get``
    only after this predicate; adapter migration belongs to E05.
    """

    if not isinstance(result, dict):
        return False
    status = result.get("status")
    return bool(
        _domain_failure(result)
        or (
            isinstance(status, str)
            and status
            in {"error", "failed", "stub", "partial", "waiting_approval", "outcome_unknown"}
        )
    )


def _normalize_versioned(payload: Mapping[str, Any]) -> ToolResult:
    if type(payload.get("version")) is not int or payload.get("version") != 1:
        return _unrecognized(payload, "unknown_tool_result_version")

    failure = _domain_failure(payload.get("data"))
    if payload.get("status") == "succeeded" and failure is not None:
        return ToolResult(
            status="failed",
            data=dict(payload),
            error_code=failure,
            evidence={"contract_error": "succeeded_result_contains_domain_error"},
        )

    # Direct legacy error markers are forbidden extras in a v1 envelope, but
    # classify the contradiction explicitly instead of preserving "succeeded".
    direct_failure = _direct_domain_failure(payload)
    if payload.get("status") == "succeeded" and direct_failure is not None:
        return ToolResult(
            status="failed",
            data=dict(payload),
            error_code=direct_failure,
            evidence={"contract_error": "succeeded_result_contains_error_marker"},
        )

    try:
        return ToolResult.model_validate(payload)
    except ValidationError as exc:
        return ToolResult(
            status="failed",
            data=dict(payload),
            error_code="invalid_tool_result_contract",
            evidence={
                "validation_errors": exc.errors(
                    include_url=False,
                    include_context=False,
                    include_input=False,
                )
            },
        )


def _normalize_transport_unknown_outcome_v0(payload: Any) -> ToolResult:
    if not isinstance(payload, Mapping):
        return _unrecognized(payload, "unrecognized_legacy_result")

    raw = dict(payload)
    if (
        raw.get("status") != "outcome_unknown"
        or raw.get("error_code") != "tool_outcome_unknown"
        or not isinstance(raw.get("error"), str)
        or not raw["error"]
    ):
        return _unrecognized(raw, "legacy_contract_mismatch")

    return ToolResult(
        status="outcome_unknown",
        data=raw,
        error_code="tool_outcome_unknown",
        retryable=False,
        evidence={
            "legacy_contract": (LegacyToolResultContract.TOOL_TRANSPORT_UNKNOWN_OUTCOME_V0.value),
            **(
                {"contract_error": "outcome_unknown_cannot_be_retryable"}
                if raw.get("retryable") is True
                else {}
            ),
        },
        checkpoint=(raw.get("checkpoint") if isinstance(raw.get("checkpoint"), dict) else None),
    )


def _direct_domain_failure(value: Mapping[str, Any]) -> str | None:
    error_code = value.get("error_code")
    if isinstance(error_code, str) and error_code:
        return error_code
    if _meaningful(value.get("error")) or _meaningful(value.get("errors")):
        return "domain_error"
    if value.get("built") is False:
        return "not_built"
    status = value.get("status")
    if isinstance(status, str):
        if status in {"error", "failed", "stub"}:
            return "domain_error"
        if status in {"partial", "waiting_approval", "outcome_unknown"}:
            return f"domain_{status}"
    return None


def _domain_failure(value: Any) -> str | None:
    """Find markers only in an explicit response-envelope chain.

    Read payloads often contain records with historical ``error`` fields or a
    current ``status``. Those records are data, not a response contract. Only
    the top-level response and reviewed ``result``/``domain`` wrappers may
    therefore influence invocation success.
    """

    if isinstance(value, Mapping):
        direct = _direct_domain_failure(value)
        if direct is not None:
            return direct
        for wrapper_key in ("result", "domain"):
            nested = value.get(wrapper_key)
            if isinstance(nested, Mapping):
                failure = _domain_failure(nested)
                if failure is not None:
                    return failure
    return None


def _meaningful(value: Any) -> bool:
    return value not in (None, "", False, [], {})


def _nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _unrecognized(payload: Any, reason: str) -> ToolResult:
    return ToolResult(
        status="failed",
        data=payload,
        error_code="unrecognized_tool_result",
        evidence={"reason": reason},
    )
