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
        if status in {"queued", "running"}:
            return "domain_work_incomplete"
    return None


def _domain_failure(value: Any) -> str | None:
    """Find explicit domain failure markers, including nested adapter wrappers."""

    if isinstance(value, Mapping):
        direct = _direct_domain_failure(value)
        if direct is not None:
            return direct
        for nested in value.values():
            failure = _domain_failure(nested)
            if failure is not None:
                return failure
    elif isinstance(value, list):
        for item in value:
            failure = _domain_failure(item)
            if failure is not None:
                return failure
    return None


def _meaningful(value: Any) -> bool:
    return value not in (None, "", False, [], {})


def _unrecognized(payload: Any, reason: str) -> ToolResult:
    return ToolResult(
        status="failed",
        data=payload,
        error_code="unrecognized_tool_result",
        evidence={"reason": reason},
    )
