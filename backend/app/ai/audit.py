"""Typed audit issues for the orchestrator's post-turn verification.

Issues carry a stable :class:`AuditCode`; the human text in ``message`` is
presentation only and MUST NOT be matched against. The audit no longer drives
a retry of the turn: its codes are facts for telemetry, the learning loop and
the durable verifier.

Severity semantics:
- ``blocking`` — the turn result is wrong for the user; flips ``passed``.
- ``advisory`` — worth recording (learning loop, telemetry) but the executor's
  result is acceptable; never flips ``passed``.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


class AuditCode(StrEnum):
    # Facts of the turn only: what the executor called and what it published.
    # Codes that judged the request against a plan (wrong canvas, missing
    # filter, action not performed…) went with the turn router.
    UNKNOWN_SKILL = "unknown_skill"  # executor called a non-existent skill
    TOOL_ERROR = "tool_error"  # tool call returned an error
    EMPTY_ANSWER = "empty_answer"  # no text and no workspace output
    TOTAL_OVERSTATED = "total_overstated"  # title claims more rows than were published


Severity = Literal["blocking", "advisory"]


class AuditIssue(BaseModel):
    code: AuditCode
    severity: Severity = "blocking"
    # Human-readable Russian text for the UI/logs. Presentation only —
    # control flow must branch on `code`, never on this string.
    message: str
    context: dict[str, Any] = Field(default_factory=dict)


def blocking(issues: list[AuditIssue]) -> list[AuditIssue]:
    return [issue for issue in issues if issue.severity == "blocking"]


def has_code(issues: list[AuditIssue], *codes: AuditCode) -> bool:
    wanted = set(codes)
    return any(issue.code in wanted for issue in issues)


def messages(issues: list[AuditIssue]) -> list[str]:
    return [issue.message for issue in issues]


def codes(issues: list[AuditIssue]) -> list[str]:
    return [issue.code.value for issue in issues]
