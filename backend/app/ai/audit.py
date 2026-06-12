"""Typed audit issues for the orchestrator's post-turn verification.

The mechanical audit used to report free-form Russian strings, and downstream
control flow (retry, direct tool repair, capability-gap reporting) matched
substrings of those messages — rewording a message silently disabled the
behaviour behind it. Issues now carry a stable :class:`AuditCode`; the human
text in ``message`` is presentation only and MUST NOT be matched against.

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
    # Workspace publication
    WORKSPACE_NOT_PUBLISHED = "workspace_not_published"  # rich output requested, none verified
    WRONG_CANVAS = "wrong_canvas"                        # published to a canvas other than planned
    CHAT_TABLE_LEAK = "chat_table_leak"                  # markdown table dumped into chat
    # Tool usage
    FILTER_MISSING = "filter_missing"                    # required filter not passed to the tool
    FILTER_MISMATCH = "filter_mismatch"                  # filter passed/reported with a wrong value
    UNKNOWN_SKILL = "unknown_skill"                      # executor called a non-existent skill
    TOOL_ERROR = "tool_error"                            # tool call returned an error
    TOOL_OFF_PLAN = "tool_off_plan"                      # executor picked a tool outside the plan
    # Answer quality
    EMPTY_ANSWER = "empty_answer"                        # no text and no workspace output
    SEMANTIC_SUSPECT = "semantic_suspect"                # semantic audit doubts the answer


Severity = Literal["blocking", "advisory"]


class AuditIssue(BaseModel):
    code: AuditCode
    severity: Severity = "blocking"
    # Human-readable Russian text for the UI/logs. Presentation only —
    # control flow must branch on `code`, never on this string.
    message: str
    context: dict[str, Any] = Field(default_factory=dict)


# Issues the executor can plausibly fix on a retry with a corrected request.
# UNKNOWN_SKILL is deliberately absent: retrying cannot invent a missing tool.
RETRYABLE: frozenset[AuditCode] = frozenset({
    AuditCode.WORKSPACE_NOT_PUBLISHED,
    AuditCode.WRONG_CANVAS,
    AuditCode.CHAT_TABLE_LEAK,
    AuditCode.FILTER_MISSING,
    AuditCode.FILTER_MISMATCH,
})

# Issues that signal a genuinely missing capability (feed the builder flow).
CAPABILITY_GAP_CODES: frozenset[AuditCode] = frozenset({
    AuditCode.UNKNOWN_SKILL,
    AuditCode.WRONG_CANVAS,
    AuditCode.TOOL_OFF_PLAN,
})


def blocking(issues: list[AuditIssue]) -> list[AuditIssue]:
    return [issue for issue in issues if issue.severity == "blocking"]


def has_code(issues: list[AuditIssue], *codes: AuditCode) -> bool:
    wanted = set(codes)
    return any(issue.code in wanted for issue in issues)


def retryable(issues: list[AuditIssue]) -> bool:
    return any(issue.code in RETRYABLE for issue in issues)


def messages(issues: list[AuditIssue]) -> list[str]:
    return [issue.message for issue in issues]


def codes(issues: list[AuditIssue]) -> list[str]:
    return [issue.code.value for issue in issues]
