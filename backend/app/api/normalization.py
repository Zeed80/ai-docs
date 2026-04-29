"""Normalization API — skills: norm.list_rules, norm.suggest_rule, norm.apply_rules, norm.activate_rule,
                               norm.list_norm_cards, norm.create_norm_card, norm.update_canonical_item"""

import re
import uuid
from collections import Counter
from datetime import datetime, timezone
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, func, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.session import get_db
from app.db.models import (
    DocumentExtraction,
    ExtractionField,
    NormalizationRule,
    NormRuleStatus,
    CanonicalItem,
    NormCard,
)
from app.domain.normalization import (
    NormApplyRequest,
    NormApplyResult,
    NormRuleActivateRequest,
    NormRuleCreate,
    NormRuleListResponse,
    NormRuleOut,
    NormRuleSuggestRequest,
    NormRuleSuggestResponse,
)
from app.audit.service import log_action

router = APIRouter()
logger = structlog.get_logger()


# ── norm.list_rules ─────────────────────────────────────────────────────────


@router.get("/rules", response_model=NormRuleListResponse)
async def list_rules(
    status: NormRuleStatus | None = None,
    field_name: str | None = None,
    offset: int = 0,
    limit: int = Query(50, le=200),
    db: AsyncSession = Depends(get_db),
):
    """Skill: norm.list_rules — List normalization rules."""
    query = select(NormalizationRule)
    if status:
        query = query.where(NormalizationRule.status == status)
    if field_name:
        query = query.where(NormalizationRule.field_name == field_name)

    count_q = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_q)).scalar() or 0

    query = query.order_by(NormalizationRule.created_at.desc()).offset(offset).limit(limit)
    result = await db.execute(query)
    items = result.scalars().all()

    return NormRuleListResponse(items=items, total=total)


# ── norm.suggest_rule ───────────────────────────────────────────────────────


@router.post("/suggest", response_model=NormRuleSuggestResponse)
async def suggest_rules(
    payload: NormRuleSuggestRequest,
    db: AsyncSession = Depends(get_db),
):
    """Skill: norm.suggest_rule — Detect repeated human corrections and propose rules."""
    # Find all human-corrected fields
    query = select(ExtractionField).where(ExtractionField.human_corrected.is_(True))
    if payload.field_name:
        query = query.where(ExtractionField.field_name == payload.field_name)

    result = await db.execute(query)
    corrected_fields = result.scalars().all()
    total_analyzed = len(corrected_fields)

    # Group by (field_name, original_value → corrected_value)
    correction_patterns: Counter[tuple[str, str, str]] = Counter()
    for f in corrected_fields:
        if f.field_value and f.corrected_value:
            key = (f.field_name, f.field_value, f.corrected_value)
            correction_patterns[key] += 1

    # Create proposed rules for patterns above threshold
    suggested = []
    for (field_name, pattern, replacement), count in correction_patterns.items():
        if count < payload.min_corrections:
            continue

        # Check if rule already exists
        existing = await db.execute(
            select(NormalizationRule).where(
                NormalizationRule.field_name == field_name,
                NormalizationRule.pattern == pattern,
                NormalizationRule.replacement == replacement,
            )
        )
        if existing.scalar_one_or_none():
            continue

        rule = NormalizationRule(
            field_name=field_name,
            pattern=pattern,
            replacement=replacement,
            is_regex=False,
            status=NormRuleStatus.proposed,
            source_corrections=count,
            suggested_by="system",
            description=f"Auto-suggested: '{pattern}' → '{replacement}' ({count} corrections)",
        )
        db.add(rule)
        await db.flush()
        suggested.append(rule)

    if suggested:
        await log_action(
            db,
            action="norm.suggest_rule",
            entity_type="normalization_rule",
            details={"rules_suggested": len(suggested)},
        )
        await db.commit()
    else:
        await db.rollback()

    return NormRuleSuggestResponse(
        suggested_rules=suggested,
        total_corrections_analyzed=total_analyzed,
    )


# ── norm.activate_rule ──────────────────────────────────────────────────────


@router.post("/rules/{rule_id}/activate", response_model=NormRuleOut)
async def activate_rule(
    rule_id: uuid.UUID,
    payload: NormRuleActivateRequest,
    db: AsyncSession = Depends(get_db),
):
    """Skill: norm.activate_rule — Activate a proposed rule (approval gate)."""
    result = await db.execute(
        select(NormalizationRule).where(NormalizationRule.id == rule_id)
    )
    rule = result.scalar_one_or_none()
    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")

    if rule.status == NormRuleStatus.active:
        raise HTTPException(status_code=400, detail="Rule is already active")

    rule.status = NormRuleStatus.active
    rule.activated_by = payload.activated_by
    rule.activated_at = datetime.now(timezone.utc)

    await log_action(
        db,
        action="norm.activate_rule",
        entity_type="normalization_rule",
        entity_id=rule.id,
        details={"activated_by": payload.activated_by},
    )
    await db.commit()
    await db.refresh(rule)

    logger.info("rule_activated", rule_id=str(rule_id))
    return rule


# ── norm.disable_rule ───────────────────────────────────────────────────────


@router.post("/rules/{rule_id}/disable", response_model=NormRuleOut)
async def disable_rule(
    rule_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Disable a normalization rule (rollback)."""
    result = await db.execute(
        select(NormalizationRule).where(NormalizationRule.id == rule_id)
    )
    rule = result.scalar_one_or_none()
    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")

    rule.status = NormRuleStatus.disabled

    await log_action(
        db,
        action="norm.disable_rule",
        entity_type="normalization_rule",
        entity_id=rule.id,
    )
    await db.commit()
    await db.refresh(rule)
    return rule


# ── norm.apply_rules ────────────────────────────────────────────────────────


@router.post("/apply", response_model=NormApplyResult)
async def apply_rules(
    payload: NormApplyRequest,
    db: AsyncSession = Depends(get_db),
):
    """Skill: norm.apply_rules — Apply active normalization rules to a document's extraction."""
    # Get latest extraction for the document
    result = await db.execute(
        select(DocumentExtraction)
        .where(DocumentExtraction.document_id == payload.document_id)
        .order_by(DocumentExtraction.created_at.desc())
        .limit(1)
    )
    extraction = result.scalar_one_or_none()
    if not extraction:
        raise HTTPException(status_code=404, detail="No extraction found for document")

    # Get extraction fields
    result = await db.execute(
        select(ExtractionField).where(ExtractionField.extraction_id == extraction.id)
    )
    fields = result.scalars().all()

    # Get active rules
    result = await db.execute(
        select(NormalizationRule).where(
            NormalizationRule.status == NormRuleStatus.active
        )
    )
    active_rules = result.scalars().all()

    modifications = []
    rules_applied = 0

    for field in fields:
        if not field.field_value:
            continue

        for rule in active_rules:
            if rule.field_name != field.field_name:
                continue

            old_val = field.field_value
            if rule.is_regex:
                try:
                    new_val = re.sub(rule.pattern, rule.replacement, old_val)
                except re.error:
                    continue
            else:
                if old_val == rule.pattern:
                    new_val = rule.replacement
                else:
                    continue

            if new_val != old_val:
                field.field_value = new_val
                field.confidence_reason = "normalization_applied"
                rule.apply_count += 1
                rule.last_applied_at = datetime.now(timezone.utc)
                rules_applied += 1
                modifications.append({
                    "field_name": field.field_name,
                    "old_value": old_val,
                    "new_value": new_val,
                    "rule_id": str(rule.id),
                })

    if modifications:
        await log_action(
            db,
            action="norm.apply_rules",
            entity_type="document",
            entity_id=payload.document_id,
            details={"rules_applied": rules_applied, "modifications": modifications},
        )
        await db.commit()

    logger.info(
        "normalization_applied",
        document_id=str(payload.document_id),
        rules_applied=rules_applied,
    )
    return NormApplyResult(
        document_id=payload.document_id,
        rules_applied=rules_applied,
        fields_modified=modifications,
    )


# ── norm.create_rule (manual) ───────────────────────────────────────────────


@router.post("/rules", response_model=NormRuleOut, status_code=201)
async def create_rule(
    payload: NormRuleCreate,
    db: AsyncSession = Depends(get_db),
):
    """Create a normalization rule manually."""
    if payload.is_regex:
        try:
            re.compile(payload.pattern)
        except re.error as e:
            raise HTTPException(status_code=400, detail=f"Invalid regex: {e}")

    rule = NormalizationRule(
        field_name=payload.field_name,
        pattern=payload.pattern,
        replacement=payload.replacement,
        is_regex=payload.is_regex,
        status=NormRuleStatus.proposed,
        suggested_by="user",
        description=payload.description,
    )
    db.add(rule)

    await log_action(
        db,
        action="norm.create_rule",
        entity_type="normalization_rule",
        entity_id=rule.id,
    )
    await db.commit()
    await db.refresh(rule)
    return rule


# ── NormCard (Этап 6) ────────────────────────────────────────────────────────


class NormCardCreate(BaseModel):
    canonical_item_id: uuid.UUID
    norm_qty: float
    unit: str
    product_code: str | None = None
    loss_factor: float = 1.0
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    approved_by: str | None = None
    notes: str | None = None


class NormCardUpdate(BaseModel):
    norm_qty: float | None = None
    unit: str | None = None
    product_code: str | None = None
    loss_factor: float | None = None
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    approved_by: str | None = None
    notes: str | None = None


class NormCardOut(BaseModel):
    id: uuid.UUID
    canonical_item_id: uuid.UUID
    product_code: str | None
    norm_qty: float
    unit: str
    loss_factor: float
    valid_from: datetime | None
    valid_to: datetime | None
    approved_by: str | None
    notes: str | None
    created_at: datetime
    updated_at: datetime
    model_config = {"from_attributes": True}


class NormCardListResponse(BaseModel):
    items: list[NormCardOut]
    total: int
    offset: int
    limit: int


class CanonicalItemClassificationUpdate(BaseModel):
    okpd2_code: str | None = None
    gost: str | None = None
    hazard_class: str | None = None


class CanonicalItemOut(BaseModel):
    id: uuid.UUID
    name: str
    category: str | None
    unit: str | None
    description: str | None
    aliases: list[Any] | None
    is_confirmed: bool
    okpd2_code: str | None
    gost: str | None
    hazard_class: str | None
    created_at: datetime
    updated_at: datetime
    model_config = {"from_attributes": True}


@router.get("/norm-cards", response_model=NormCardListResponse)
async def list_norm_cards(
    canonical_item_id: uuid.UUID | None = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    """Skill: norm.list_norm_cards — List norm cards."""
    q = select(NormCard)
    if canonical_item_id:
        q = q.where(NormCard.canonical_item_id == canonical_item_id)
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar() or 0
    items = (await db.execute(q.order_by(NormCard.created_at.desc()).offset(offset).limit(limit))).scalars().all()
    return NormCardListResponse(items=items, total=total, offset=offset, limit=limit)


@router.post("/norm-cards", response_model=NormCardOut, status_code=201)
async def create_norm_card(
    payload: NormCardCreate,
    db: AsyncSession = Depends(get_db),
):
    """Skill: norm.create_norm_card — Create a norm card for a canonical item."""
    item = await db.get(CanonicalItem, payload.canonical_item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Canonical item not found")
    card = NormCard(**payload.model_dump())
    db.add(card)
    await db.commit()
    await db.refresh(card)
    await log_action(db, action="norm.create_norm_card", entity_type="norm_card",
                     entity_id=card.id, details={"canonical_item_id": str(payload.canonical_item_id)})
    return card


@router.get("/norm-cards/{card_id}", response_model=NormCardOut)
async def get_norm_card(
    card_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Get norm card details."""
    card = await db.get(NormCard, card_id)
    if not card:
        raise HTTPException(status_code=404, detail="Norm card not found")
    return card


@router.patch("/norm-cards/{card_id}", response_model=NormCardOut)
async def update_norm_card(
    card_id: uuid.UUID,
    payload: NormCardUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Skill: norm.update_norm_card — Update norm card values."""
    card = await db.get(NormCard, card_id)
    if not card:
        raise HTTPException(status_code=404, detail="Norm card not found")
    for k, v in payload.model_dump(exclude_unset=True).items():
        setattr(card, k, v)
    await db.commit()
    await db.refresh(card)
    return card


@router.delete("/norm-cards/{card_id}", status_code=200)
async def delete_norm_card(
    card_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Delete a norm card."""
    card = await db.get(NormCard, card_id)
    if not card:
        raise HTTPException(status_code=404, detail="Norm card not found")
    await db.delete(card)
    await db.commit()
    return {"deleted": str(card_id)}


# ── CanonicalItem classification fields ──────────────────────────────────────


class CanonicalItemListResponse(BaseModel):
    items: list[CanonicalItemOut]
    total: int
    offset: int
    limit: int


@router.get("/canonical-items", response_model=CanonicalItemListResponse)
async def list_canonical_items(
    search: str | None = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    """Skill: norm.list_canonical_items — List canonical items."""
    q = select(CanonicalItem)
    if search:
        q = q.where(CanonicalItem.name.ilike(f"%{search}%"))
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar() or 0
    items = (await db.execute(q.order_by(CanonicalItem.name).offset(offset).limit(limit))).scalars().all()
    return CanonicalItemListResponse(items=items, total=total, offset=offset, limit=limit)


@router.get("/canonical-items/{item_id}", response_model=CanonicalItemOut)
async def get_canonical_item(
    item_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Skill: norm.get_canonical_item — Get canonical item with classification fields."""
    item = await db.get(CanonicalItem, item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Canonical item not found")
    return item


@router.patch("/canonical-items/{item_id}", response_model=CanonicalItemOut)
async def update_canonical_item_classification(
    item_id: uuid.UUID,
    payload: CanonicalItemClassificationUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Skill: norm.update_canonical_item — Update OKPD2, GOST, hazard class."""
    item = await db.get(CanonicalItem, item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Canonical item not found")
    for k, v in payload.model_dump(exclude_unset=True).items():
        setattr(item, k, v)
    await log_action(db, action="norm.update_canonical_item", entity_type="canonical_item",
                     entity_id=item.id, details=payload.model_dump(exclude_unset=True))
    await db.commit()
    await db.refresh(item)
    return item


@router.get("/canonical-items/{item_id}/norm-cards", response_model=NormCardListResponse)
async def get_canonical_item_norm_cards(
    item_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Skill: norm.get_item_norm_cards — Get all norm cards for a canonical item."""
    item = await db.get(CanonicalItem, item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Canonical item not found")
    q = select(NormCard).where(NormCard.canonical_item_id == item_id)
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar() or 0
    items = (await db.execute(q.order_by(NormCard.created_at.desc()))).scalars().all()
    return NormCardListResponse(items=items, total=total, offset=0, limit=total or 1)
