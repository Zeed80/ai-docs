"""Telegram is only a human UI for the normal approval settlement path."""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import UserInfo, UserRole
from app.db.agent_runtime_models import AgentChannelIdentity, TelegramApprovalCallback
from app.db.models import Approval, ApprovalActionType, ApprovalStatus, User


class TelegramApprovalError(ValueError):
    """A safe, user-displayable reason why a Telegram button cannot settle."""


@dataclass(frozen=True)
class TelegramApprovalResult:
    status: str
    message: str


def _action_digest(context: dict) -> str | None:
    try:
        payload = {
            "capability": context["tool_name"],
            "action": context["action"],
            "arguments": context["tool_args"],
        }
    except (KeyError, TypeError):
        return None
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


async def create_telegram_approval_callback(
    db: AsyncSession, *, approval: Approval, binding: AgentChannelIdentity
) -> TelegramApprovalCallback:
    """Create the only button format accepted by the polling bot.

    Buttons are intentionally available only for a named, bound approver and
    durable work-order actions.  Generic approval records have no safe exact
    action binding, so they remain available through the existing HTTP UI.
    """
    context = approval.context or {}
    action_digest = _action_digest(context)
    now = datetime.now(UTC)
    if (
        approval.status != ApprovalStatus.pending
        or approval.action_type != ApprovalActionType.agent_tool_call
        or approval.entity_type != "work_order"
        or not approval.assigned_to
        or approval.assigned_to != binding.owner_key
        or not binding.is_active
        or binding.channel != "telegram"
        or approval.expires_at is None
        or approval.expires_at <= now
        or action_digest is None
        or action_digest != context.get("action_digest")
    ):
        raise TelegramApprovalError("Approval cannot be safely handed to Telegram")
    callback = TelegramApprovalCallback(
        token=secrets.token_urlsafe(12),
        approval_id=approval.id,
        binding_id=binding.id,
        owner_key=binding.owner_key,
        action_digest=action_digest,
        expires_at=approval.expires_at,
        created_at=datetime.now(UTC),
    )
    db.add(callback)
    await db.flush()
    return callback


def callback_data(callback: TelegramApprovalCallback, approved: bool) -> str:
    """Return Telegram-safe opaque data (well below Telegram's 64-byte limit)."""
    return f"ta:{callback.token}:{'a' if approved else 'r'}"


async def settle_telegram_approval_callback(
    db: AsyncSession, *, token: str, approved: bool, telegram_user_id: int
) -> TelegramApprovalResult:
    """Revalidate the current human identity and exact pending action, then settle.

    No service principal is introduced: the actor passed to the existing HTTP
    settlement is reconstructed from the active human user record.
    """
    callback = await db.scalar(
        select(TelegramApprovalCallback)
        .where(TelegramApprovalCallback.token == token)
        .with_for_update()
    )
    if callback is None or callback.consumed_at is not None:
        return TelegramApprovalResult(
            "unavailable", "Это подтверждение уже обработано или устарело."
        )

    binding = await db.scalar(
        select(AgentChannelIdentity)
        .where(
            AgentChannelIdentity.id == callback.binding_id,
            AgentChannelIdentity.channel == "telegram",
            AgentChannelIdentity.external_id == str(telegram_user_id),
            AgentChannelIdentity.owner_key == callback.owner_key,
            AgentChannelIdentity.is_active.is_(True),
        )
        .with_for_update()
    )
    user = await db.scalar(select(User).where(User.sub == callback.owner_key).with_for_update())
    if (
        binding is None
        or user is None
        or not user.is_active
        or user.role not in {"manager", "admin"}
    ):
        return TelegramApprovalResult(
            "unavailable", "У вас больше нет права подтвердить это действие."
        )

    approval = await db.scalar(
        select(Approval).where(Approval.id == callback.approval_id).with_for_update()
    )
    now = datetime.now(UTC)
    if approval is None or approval.status != ApprovalStatus.pending:
        return TelegramApprovalResult(
            "unavailable", "Это подтверждение уже обработано или устарело."
        )
    # An altered deadline changes the decision that was shown to the human.
    # It must not silently extend a previously delivered button.
    if approval.expires_at != callback.expires_at:
        return TelegramApprovalResult(
            "unavailable", "Условия подтверждения изменились; откройте новое согласование."
        )
    if approval.expires_at is not None and approval.expires_at <= now:
        approval.status = ApprovalStatus.expired
        callback.consumed_at = now
        await db.commit()
        return TelegramApprovalResult("expired", "Срок этого подтверждения истёк.")

    context = approval.context or {}
    current_digest = _action_digest(context)
    if (
        approval.action_type != ApprovalActionType.agent_tool_call
        or approval.entity_type != "work_order"
        or approval.assigned_to != callback.owner_key
        or current_digest is None
        or current_digest != context.get("action_digest")
        or current_digest != callback.action_digest
    ):
        return TelegramApprovalResult(
            "unavailable", "Действие изменилось; подтвердите его в веб-интерфейсе заново."
        )

    # The row lock above makes concurrent clicks serialize.  Mark consumed in
    # the same transaction as the canonical settlement; a rollback consumes
    # neither the decision nor its button.
    callback.consumed_at = now
    actor = UserInfo(
        sub=user.sub,
        email=user.email,
        name=user.name,
        preferred_username=user.preferred_username,
        roles=[UserRole(user.role)],
    )
    from app.api.approvals import decide_approval
    from app.domain.approvals import ApprovalDecision

    try:
        await decide_approval(
            approval.id,
            ApprovalDecision(
                status=ApprovalStatus.approved if approved else ApprovalStatus.rejected
            ),
            db,
            actor,
        )
    except Exception as exc:
        await db.rollback()
        raise TelegramApprovalError("Approval can no longer be settled") from exc
    return TelegramApprovalResult("approved" if approved else "rejected", "Решение сохранено.")
