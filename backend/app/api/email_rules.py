"""Email filter rules — CRUD, dry-run test, apply-to-existing.

Shared rules (owner_sub is None): admin only. A personal rule belongs to the
mailbox owner. The engine itself is app.domain.email_rules (runs on ingest).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.acting import get_effective_user
from app.auth.models import UserInfo, UserRole
from app.db.models import EmailAttachment, EmailMessage, EmailRule, EmailRuleLog
from app.db.session import get_db
from app.domain.email_access import mailbox_filter, may_write_mailbox
from app.domain.email_rules import evaluate_conditions, known_domains

router = APIRouter()
logger = structlog.get_logger()


class RuleCondition(BaseModel):
    field: str
    op: str
    value: object = ""


class RuleGroup(BaseModel):
    match: Literal["all", "any"] = "all"
    rules: list[RuleCondition] = []


class RuleAction(BaseModel):
    type: str
    label_id: uuid.UUID | None = None
    template_id: uuid.UUID | None = None
    folder: str | None = None
    role: str | None = None
    address: str | None = None
    prompt: str | None = None


class EmailRuleCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    mailbox: str | None = None
    conditions: RuleGroup
    actions: list[RuleAction] = []
    priority: int = 100
    stop_processing: bool = False
    is_active: bool = True


class EmailRuleUpdate(BaseModel):
    name: str | None = None
    mailbox: str | None = None
    conditions: RuleGroup | None = None
    actions: list[RuleAction] | None = None
    priority: int | None = None
    stop_processing: bool | None = None
    is_active: bool | None = None


class EmailRuleOut(BaseModel):
    id: uuid.UUID
    name: str
    mailbox: str | None
    owner_sub: str | None
    is_active: bool
    priority: int
    stop_processing: bool
    conditions: dict
    actions: list
    run_count: int
    last_run_at: datetime | None

    model_config = {"from_attributes": True}


class RuleTestRequest(BaseModel):
    last_n: int = 20


class RuleTestResult(BaseModel):
    matched: int
    total: int
    sample_subjects: list[str] = []


def _is_admin(user: UserInfo) -> bool:
    return UserRole.admin in (user.roles or [])


async def _owned(db: AsyncSession, rule_id: uuid.UUID, user: UserInfo) -> EmailRule:
    rule = await db.get(EmailRule, rule_id)
    if not rule:
        raise HTTPException(404, "Rule not found")
    if rule.owner_sub is None and not _is_admin(user):
        raise HTTPException(403, "Общие правила меняет администратор")
    if rule.owner_sub not in (None, user.sub):
        raise HTTPException(403, "Не ваше правило")
    return rule


@router.get("", response_model=list[EmailRuleOut])
async def list_rules(
    user: UserInfo = Depends(get_effective_user),
    db: AsyncSession = Depends(get_db),
):
    """Skill: email.rules_list — Filter rules visible to the caller."""
    rows = (
        (
            await db.execute(
                select(EmailRule)
                .where((EmailRule.owner_sub.is_(None)) | (EmailRule.owner_sub == user.sub))
                .order_by(EmailRule.priority.asc())
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


@router.post("", response_model=EmailRuleOut, status_code=201)
async def create_rule(
    payload: EmailRuleCreate,
    user: UserInfo = Depends(get_effective_user),
    db: AsyncSession = Depends(get_db),
):
    """Skill: email.rules_create — Create a filter rule.

    Ф0.5. ``mailbox=None`` means "every mailbox in the company" and is an
    admin-only privilege: the engine matches rules by mailbox, so before this
    check any employee could create an all-mailbox rule that ran against
    colleagues' private mail — labelling it, marking it read, drafting replies
    from it, or forwarding it to the agent.
    """
    await _assert_may_target(db, user, payload.mailbox)
    owner_sub = None if _is_admin(user) else user.sub
    rule = EmailRule(
        name=payload.name,
        mailbox=payload.mailbox,
        owner_sub=owner_sub,
        is_active=payload.is_active,
        priority=payload.priority,
        stop_processing=payload.stop_processing,
        conditions=payload.conditions.model_dump(),
        actions=[a.model_dump(exclude_none=True) for a in payload.actions],
    )
    db.add(rule)
    await db.commit()
    await db.refresh(rule)
    return rule


async def _assert_may_target(db: AsyncSession, user: UserInfo, mailbox: str | None) -> None:
    """A rule may only target a mailbox its author may actually act in."""
    if mailbox is None:
        if not _is_admin(user):
            raise HTTPException(
                403,
                "Правило без указания ящика применяется ко всем ящикам компании — "
                "это может сделать только администратор. Укажите свой ящик.",
            )
        return
    if not await may_write_mailbox(db, user, mailbox):
        raise HTTPException(403, "Нет доступа к этому ящику")


@router.patch("/{rule_id}", response_model=EmailRuleOut)
async def update_rule(
    rule_id: uuid.UUID,
    payload: EmailRuleUpdate,
    user: UserInfo = Depends(get_effective_user),
    db: AsyncSession = Depends(get_db),
):
    rule = await _owned(db, rule_id, user)
    if "mailbox" in payload.model_fields_set:
        await _assert_may_target(db, user, payload.mailbox)
    data = payload.model_dump(exclude_none=True)
    if "conditions" in data:
        rule.conditions = payload.conditions.model_dump()
        data.pop("conditions")
    if "actions" in data:
        rule.actions = [a.model_dump(exclude_none=True) for a in payload.actions]
        data.pop("actions")
    for k, v in data.items():
        setattr(rule, k, v)
    await db.commit()
    await db.refresh(rule)
    return rule


@router.delete("/{rule_id}", status_code=204)
async def delete_rule(
    rule_id: uuid.UUID,
    user: UserInfo = Depends(get_effective_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    rule = await _owned(db, rule_id, user)
    await db.delete(rule)
    await db.commit()


@router.post("/{rule_id}/test", response_model=RuleTestResult)
async def test_rule(
    rule_id: uuid.UUID,
    payload: RuleTestRequest,
    user: UserInfo = Depends(get_effective_user),
    db: AsyncSession = Depends(get_db),
):
    """Dry-run: how many of the last N messages would this rule match?

    Two things this got wrong before Ф0.5 and both made the preview lie:
    it scanned every mailbox (returning subject lines out of colleagues'
    private mail in ``sample_subjects``), and it passed an empty attachment
    list, so every attachment-based condition — the ones this product exists
    for — evaluated false here and true in production.
    """
    rule = await _owned(db, rule_id, user)
    q = select(EmailMessage).where(EmailMessage.is_inbound == True)  # noqa: E712
    if rule.mailbox:
        q = q.where(EmailMessage.mailbox == rule.mailbox)
    scope = await mailbox_filter(db, user, mailbox_col=EmailMessage.mailbox)
    if scope is not None:
        q = q.where(scope)
    msgs = (
        (await db.execute(q.order_by(EmailMessage.received_at.desc()).limit(payload.last_n)))
        .scalars()
        .all()
    )
    ksd = await known_domains(db)
    attachments: dict = {}
    if msgs:
        rows = (
            (
                await db.execute(
                    select(EmailAttachment).where(
                        EmailAttachment.message_id.in_([m.id for m in msgs])
                    )
                )
            )
            .scalars()
            .all()
        )
        for att in rows:
            attachments.setdefault(att.message_id, []).append(att)
    matched = [
        m
        for m in msgs
        if evaluate_conditions(
            m, attachments.get(m.id, []), rule.conditions, known_supplier_domains=ksd
        )
    ]
    return RuleTestResult(
        matched=len(matched),
        total=len(msgs),
        sample_subjects=[m.subject or "" for m in matched[:5]],
    )


class RuleLogEntry(BaseModel):
    at: datetime
    message_subject: str | None = None
    message_from: str | None = None
    actions_applied: list = []


@router.get("/{rule_id}/log", response_model=list[RuleLogEntry])
async def rule_log(
    rule_id: uuid.UUID,
    limit: int = 20,
    user: UserInfo = Depends(get_effective_user),
    db: AsyncSession = Depends(get_db),
):
    """What this rule actually did, most recent first.

    ``EmailRuleLog`` has been written on every application since the feature
    shipped and surfaced nowhere, so "работает ли правило?" could only be
    answered by reading the database — and a no-op action (``assign_role``
    before Ф3) looked exactly like a working one.
    """
    rule = await _owned(db, rule_id, user)
    rows = (
        await db.execute(
            select(EmailRuleLog, EmailMessage.subject, EmailMessage.from_address)
            .outerjoin(EmailMessage, EmailRuleLog.message_id == EmailMessage.id)
            .where(EmailRuleLog.rule_id == rule.id)
            .order_by(EmailRuleLog.at.desc())
            .limit(min(limit, 100))
        )
    ).all()
    return [
        RuleLogEntry(
            at=log.at,
            message_subject=subject,
            message_from=from_address,
            actions_applied=log.actions_applied or [],
        )
        for log, subject, from_address in rows
    ]


@router.post("/{rule_id}/run")
async def run_rule_now(
    rule_id: uuid.UUID,
    user: UserInfo = Depends(get_effective_user),
    db: AsyncSession = Depends(get_db),
):
    """Apply the rule to existing inbound messages (async, best-effort)."""
    rule = await _owned(db, rule_id, user)
    from app.tasks.email_triage import apply_rule_to_backlog

    task = apply_rule_to_backlog.delay(str(rule.id))
    return {"task_id": task.id}
