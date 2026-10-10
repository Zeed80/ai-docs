"""E24: the effect fence at the recipient's own commit.

A durable attempt checks its lease before it dispatches a tool call, but the
recipient endpoint used to commit without knowing whether that attempt was
still current: a lease transferred, an order canceled or a plan superseded
between the check and the write still produced the effect.

The executor attaches a signed ``X-Work-Effect-Fence`` token (work order,
step, attempt, plan revision, tool operation key) to every durable tool call;
the gateway relays it. ``EffectFenceMiddleware`` verifies it and binds it to
the request, and ``install_effect_fence`` (called by ``get_db``) checks it in
``before_commit`` of the request session, in the same transaction as the
effect: the work order row is locked, the attempt must still own its lease,
the plan must be active at the signed revision and the order must be running
or replanning. It records one ``WorkEffectReceipt`` per operation key there
too, so a replayed token cannot commit a second effect. A rejected check
aborts the commit; the endpoint answers 409 ``effect_fence_rejected``.

Scope: sessions from ``get_db`` only. An endpoint that opens its own session,
an external dispatch (SMTP, browser, MCP) and anything after the response are
not fenced here — see docs/agent-employee-delivery/E24-effect-fence.md.
"""

from __future__ import annotations

import base64
import contextvars
import hashlib
import hmac
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

import structlog
from pydantic import BaseModel, ConfigDict

logger = structlog.get_logger()

EFFECT_FENCE_HEADER = "X-Work-Effect-Fence"
_TTL_SECONDS = 900
# The gateway relays; the effect happens at the recipient it calls.
_GATEWAY_PREFIX = "/api/agent/cap/"
_LIVE_ORDER_STATUSES = frozenset({"running", "replanning"})


class EffectFence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    work_order_id: uuid.UUID
    step_id: uuid.UUID
    attempt_id: uuid.UUID
    plan_id: uuid.UUID
    plan_revision: int
    operation_key: str
    expires_at: int
    version: Literal[1] = 1


class EffectFenceRejected(RuntimeError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _sign(payload: str) -> str:
    from app.config import settings

    return hmac.new(settings.app_secret_key.encode(), payload.encode(), hashlib.sha256).hexdigest()


def sign_effect_fence(fence: EffectFence) -> str:
    payload = base64.urlsafe_b64encode(fence.model_dump_json().encode()).decode()
    return f"{payload}.{_sign(payload)}"


def new_effect_fence(
    *,
    work_order_id: uuid.UUID,
    step_id: uuid.UUID,
    attempt_id: uuid.UUID,
    plan_id: uuid.UUID,
    plan_revision: int,
    operation_key: str,
) -> str:
    return sign_effect_fence(
        EffectFence(
            work_order_id=work_order_id,
            step_id=step_id,
            attempt_id=attempt_id,
            plan_id=plan_id,
            plan_revision=plan_revision,
            operation_key=operation_key,
            expires_at=int(time.time()) + _TTL_SECONDS,
        )
    )


def verify_effect_fence(token: str) -> EffectFence:
    try:
        payload, signature = token.split(".", 1)
        if not hmac.compare_digest(signature, _sign(payload)):
            raise ValueError("invalid signature")
        fence = EffectFence.model_validate_json(base64.urlsafe_b64decode(payload))
        now = int(time.time())
        if fence.expires_at <= now or fence.expires_at > now + _TTL_SECONDS + 5:
            raise ValueError("expired")
        return fence
    except Exception as exc:
        raise EffectFenceRejected("invalid_or_expired_fence") from exc


@dataclass
class _RequestFence:
    fence: EffectFence
    method: str
    path: str
    receipt_written: bool = field(default=False)


_request_fence: contextvars.ContextVar[_RequestFence | None] = contextvars.ContextVar(
    "work_effect_fence", default=None
)


def current_request_fence() -> _RequestFence | None:
    return _request_fence.get()


class EffectFenceMiddleware:
    """Bind a verified fence token to the request (pure ASGI, context-safe)."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers") or []}
        token = headers.get(EFFECT_FENCE_HEADER.lower())
        path = str(scope.get("path") or "")
        if not token or path.startswith(_GATEWAY_PREFIX):
            return await self.app(scope, receive, send)
        from app.config import settings

        service_key = settings.agent_service_key
        try:
            if not service_key or not hmac.compare_digest(
                headers.get("x-api-key", ""), service_key
            ):
                raise EffectFenceRejected("fence_without_service_key")
            fence = verify_effect_fence(token)
        except EffectFenceRejected as exc:
            return await _reject(send, exc.reason)
        reset = _request_fence.set(
            _RequestFence(fence=fence, method=str(scope.get("method") or ""), path=path)
        )
        try:
            return await self.app(scope, receive, send)
        finally:
            _request_fence.reset(reset)


async def _reject(send, reason: str) -> None:
    import json

    body = json.dumps(
        {"detail": {"error_code": "effect_fence_rejected", "reason": reason}}
    ).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 409,
            "headers": [(b"content-type", b"application/json")],
        }
    )
    await send({"type": "http.response.body", "body": body})


def install_effect_fence(session) -> None:
    """Check the request's fence in ``before_commit`` of this session."""
    bound = _request_fence.get()
    if bound is None:
        return
    from sqlalchemy import event

    def _before_commit(sync_session) -> None:
        _check_and_record(sync_session, bound)

    event.listen(session.sync_session, "before_commit", _before_commit)


def _check_and_record(sync_session, bound: _RequestFence) -> None:
    """Runs inside the effect's transaction, before its commit.

    The first commit of the request is the decision point. A later commit
    of the same request is not checked again: by then the handler may have
    dispatched beyond the database (email.send queues the SMTP task between
    its two commits), and refusing the second commit would only erase the
    record of an effect that already left.
    """
    if bound.receipt_written:
        return
    from sqlalchemy import select

    from app.db.models import WorkEffectReceipt, WorkOrder, WorkPlan, WorkStep, WorkStepAttempt

    fence = bound.fence
    order = sync_session.execute(
        select(WorkOrder).where(WorkOrder.id == fence.work_order_id).with_for_update()
    ).scalar_one_or_none()
    # Step and attempt are read, not locked: lease transfer and heartbeat lock
    # step first and then the order, which this transaction now holds.
    step = sync_session.get(WorkStep, fence.step_id, populate_existing=True)
    attempt = sync_session.get(WorkStepAttempt, fence.attempt_id, populate_existing=True)
    plan = sync_session.get(WorkPlan, fence.plan_id, populate_existing=True)
    reason = None
    now = datetime.now(UTC)
    if order is None or order.status not in _LIVE_ORDER_STATUSES:
        reason = "work_order_not_live"
    elif plan is None or plan.status != "active" or plan.revision != fence.plan_revision:
        reason = "plan_superseded"
    elif order.plan_revision != fence.plan_revision:
        reason = "plan_superseded"
    elif (
        step is None
        or attempt is None
        or step.work_order_id != order.id
        or step.plan_id != fence.plan_id
        or attempt.step_id != step.id
        or step.state != "running"
        or attempt.status != "running"
        or attempt.attempt_no != step.attempt_count
        or attempt.worker_id != step.lease_owner
        or step.lease_expires_at is None
        or step.lease_expires_at <= now
    ):
        reason = "attempt_lease_lost"
    if reason is not None:
        logger.warning(
            "effect_fence_rejected",
            reason=reason,
            path=bound.path,
            work_order_id=str(fence.work_order_id),
            attempt_id=str(fence.attempt_id),
        )
        raise EffectFenceRejected(reason)
    # One effect per operation key: a replayed token cannot commit twice.
    # A concurrent replay waits on the order lock and then sees this row;
    # the unique constraint backs it up.
    recorded = sync_session.execute(
        select(WorkEffectReceipt.id).where(WorkEffectReceipt.operation_key == fence.operation_key)
    ).first()
    if recorded is not None:
        logger.warning("effect_fence_rejected", reason="effect_already_recorded")
        raise EffectFenceRejected("effect_already_recorded")
    sync_session.add(
        WorkEffectReceipt(
            operation_key=fence.operation_key,
            work_order_id=fence.work_order_id,
            step_id=fence.step_id,
            attempt_id=fence.attempt_id,
            method=bound.method[:10],
            path=bound.path[:500],
        )
    )
    sync_session.flush()
    bound.receipt_written = True
