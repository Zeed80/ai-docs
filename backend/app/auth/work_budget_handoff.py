"""Signed, single-recipient handoff for durable HTTP model work."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

WORKSPACE_SQL_TABLE_PATH = "/api/workspace/agent/generated/sql-table"
WORK_BUDGET_HANDOFF_HEADER = "X-Work-Budget-Handoff"


def canonical_body_digest(body: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            body,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            default=str,
        ).encode()
    ).hexdigest()


class WorkBudgetHandoff(BaseModel):
    """Immutable authority carried only between the parent and fixed recipient."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    audience: Literal["workspace.sql_table"] = "workspace.sql_table"
    method: Literal["POST"] = "POST"
    path: Literal[WORKSPACE_SQL_TABLE_PATH] = WORKSPACE_SQL_TABLE_PATH
    body_digest: str
    actor: str
    work_order_id: uuid.UUID
    step_id: uuid.UUID
    attempt_id: uuid.UUID
    ledger_id: uuid.UUID
    plan_id: uuid.UUID
    plan_revision: int
    tool_operation_key: str
    tool_request_digest: str
    tool_binding_digest: str
    expires_at: int
    version: Literal[1] = 1


def _payload(value: WorkBudgetHandoff) -> str:
    raw = value.model_dump_json().encode()
    return base64.urlsafe_b64encode(raw).decode()


def sign_work_budget_handoff(handoff: WorkBudgetHandoff) -> str:
    from app.config import settings

    payload = _payload(handoff)
    signature = hmac.new(
        settings.app_secret_key.encode(), payload.encode(), hashlib.sha256
    ).hexdigest()
    return f"{payload}.{signature}"


def verify_work_budget_handoff(token: str) -> WorkBudgetHandoff:
    from app.config import settings

    try:
        payload, signature = token.split(".", 1)
        expected = hmac.new(
            settings.app_secret_key.encode(), payload.encode(), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("invalid signature")
        handoff = WorkBudgetHandoff.model_validate_json(base64.urlsafe_b64decode(payload))
        now = int(time.time())
        if (
            handoff.expires_at <= now
            or handoff.expires_at > now + 65
            or handoff.actor in {"", "anonymous", "agent-service"}
        ):
            raise ValueError("invalid handoff")
        return handoff
    except Exception as exc:
        raise ValueError("Invalid or expired work budget handoff") from exc


async def resolve_sql_recipient_context(request):
    """Authenticate the fixed recipient independently of the dev auth shortcut."""
    from fastapi import HTTPException

    from app.ai.work_budget_context import RecipientWorkBudgetContext
    from app.auth.execution_context import resolve_execution_actor, verify_execution_context
    from app.config import settings
    from app.db.session import _get_session_factory

    headers = request.headers
    internal = any(
        headers.get(key) is not None
        for key in (
            WORK_BUDGET_HANDOFF_HEADER,
            "X-API-Key",
            "X-Internal-Agent",
            "X-Execution-Context",
            "X-Acting-User",
        )
    )
    if not internal:
        return None
    if not settings.agent_service_key or not hmac.compare_digest(
        headers.get("X-API-Key", ""), settings.agent_service_key
    ):
        raise HTTPException(401, "SQL recipient requires the internal service key")
    execution = verify_execution_context(headers.get("X-Execution-Context", ""))
    await resolve_execution_actor(execution.actor)
    try:
        handoff = verify_work_budget_handoff(headers.get(WORK_BUDGET_HANDOFF_HEADER, ""))
        if (
            request.method != handoff.method
            or request.url.path != handoff.path
            or execution.actor != handoff.actor
            or canonical_body_digest(await request.json()) != handoff.body_digest
        ):
            raise ValueError("recipient request does not match the signed handoff")
    except ValueError as exc:
        raise HTTPException(403, str(exc)) from exc
    return RecipientWorkBudgetContext(
        work_order_id=handoff.work_order_id,
        step_id=handoff.step_id,
        attempt_id=handoff.attempt_id,
        owner_key=handoff.actor,
        ledger_id=handoff.ledger_id,
        plan_id=handoff.plan_id,
        plan_revision=handoff.plan_revision,
        tool_operation_key=handoff.tool_operation_key,
        tool_request_digest=handoff.tool_request_digest,
        tool_binding_digest=handoff.tool_binding_digest,
        body_digest=handoff.body_digest,
        session_factory=_get_session_factory(),
    )
