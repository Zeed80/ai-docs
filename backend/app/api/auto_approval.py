"""Auto-Approval Rules API — configure conditions for automatic document/invoice approval."""

import uuid
from datetime import datetime, timezone

import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AutoApprovalRule, Invoice, SupplierProfile
from app.db.session import get_db

router = APIRouter()
logger = structlog.get_logger()


class AutoApprovalRuleCreate(BaseModel):
    name: str
    supplier_id: str | None = None
    doc_type: str | None = None
    max_amount: float | None = None
    currency: str | None = None
    min_trust_score: float | None = None
    approval_role: str = "auto"


class AutoApprovalRuleUpdate(BaseModel):
    name: str | None = None
    is_active: bool | None = None
    supplier_id: str | None = None
    doc_type: str | None = None
    max_amount: float | None = None
    currency: str | None = None
    min_trust_score: float | None = None
    approval_role: str | None = None


class AutoApprovalRuleOut(BaseModel):
    id: uuid.UUID
    name: str
    is_active: bool
    supplier_id: str | None
    doc_type: str | None
    max_amount: float | None
    currency: str | None
    min_trust_score: float | None
    approval_role: str
    created_by: str | None
    apply_count: int
    last_applied_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}


class CheckRequest(BaseModel):
    invoice_id: uuid.UUID


class CheckResult(BaseModel):
    matched: bool
    rule_id: uuid.UUID | None = None
    rule_name: str | None = None
    reason: str


@router.get("", response_model=list[AutoApprovalRuleOut])
async def list_rules(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(AutoApprovalRule).order_by(AutoApprovalRule.created_at.desc())
    )
    return result.scalars().all()


@router.post("", response_model=AutoApprovalRuleOut, status_code=201)
async def create_rule(
    payload: AutoApprovalRuleCreate,
    db: AsyncSession = Depends(get_db),
):
    rule = AutoApprovalRule(
        name=payload.name,
        supplier_id=payload.supplier_id,
        doc_type=payload.doc_type,
        max_amount=payload.max_amount,
        currency=payload.currency,
        min_trust_score=payload.min_trust_score,
        approval_role=payload.approval_role,
        created_by="system",
    )
    db.add(rule)
    await db.commit()
    await db.refresh(rule)
    logger.info("auto_approval_rule_created", rule_id=str(rule.id), name=rule.name)
    return rule


@router.patch("/{rule_id}", response_model=AutoApprovalRuleOut)
async def update_rule(
    rule_id: uuid.UUID,
    payload: AutoApprovalRuleUpdate,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(AutoApprovalRule).where(AutoApprovalRule.id == rule_id))
    rule = result.scalar_one_or_none()
    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")
    for field, value in payload.model_dump(exclude_none=True).items():
        setattr(rule, field, value)
    await db.commit()
    await db.refresh(rule)
    return rule


@router.delete("/{rule_id}", status_code=204)
async def delete_rule(
    rule_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(AutoApprovalRule).where(AutoApprovalRule.id == rule_id))
    rule = result.scalar_one_or_none()
    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")
    await db.delete(rule)
    await db.commit()


@router.post("/check", response_model=CheckResult)
async def check_invoice(
    payload: CheckRequest,
    db: AsyncSession = Depends(get_db),
):
    """Check if an invoice matches any active auto-approval rule."""
    inv_result = await db.execute(select(Invoice).where(Invoice.id == payload.invoice_id))
    invoice = inv_result.scalar_one_or_none()
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")

    rules_result = await db.execute(
        select(AutoApprovalRule)
        .where(AutoApprovalRule.is_active == True)  # noqa: E712
        .order_by(AutoApprovalRule.created_at.asc())
    )
    rules = rules_result.scalars().all()

    for rule in rules:
        # Check supplier
        if rule.supplier_id and str(invoice.supplier_id) != rule.supplier_id:
            continue
        # Check amount
        if rule.max_amount is not None and invoice.total_amount is not None:
            if invoice.total_amount > rule.max_amount:
                continue
        # Check currency
        if rule.currency and invoice.currency != rule.currency:
            continue
        # Check trust score
        if rule.min_trust_score is not None and invoice.supplier_id:
            profile_res = await db.execute(
                select(SupplierProfile).where(SupplierProfile.party_id == invoice.supplier_id)
            )
            profile = profile_res.scalar_one_or_none()
            if not profile or (profile.trust_score or 0) < rule.min_trust_score:
                continue

        # Matched — increment counter
        rule.apply_count += 1
        rule.last_applied_at = datetime.now(timezone.utc)
        await db.commit()
        return CheckResult(
            matched=True,
            rule_id=rule.id,
            rule_name=rule.name,
            reason=f"Совпало правило «{rule.name}»",
        )

    return CheckResult(matched=False, reason="Ни одно правило не подошло")
