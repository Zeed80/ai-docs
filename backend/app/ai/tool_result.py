"""Versioned execution outcomes shared by chat, durable work and scripts."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool


class ToolResult(BaseModel):
    version: int = 1
    status: Literal["succeeded", "failed", "partial", "waiting_approval", "outcome_unknown"]
    data: Any = None
    error_code: str | None = None
    retryable: bool = False
    evidence: dict = Field(default_factory=dict)
    checkpoint: dict | None = None


class CriterionVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")
    criterion_id: str
    ok: StrictBool
    reason: str
    checks: list[str] = Field(default_factory=list)


class VerifierResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    verdicts: list[CriterionVerdict]


def result_failed(result: Any) -> bool:
    return isinstance(result, dict) and bool(
        result.get("error")
        or result.get("error_code")
        or result.get("errors")
        or result.get("status") in {"error", "failed", "stub", "outcome_unknown"}
        or result.get("built") is False
    )
