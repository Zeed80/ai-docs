"""Canonical Items API — skills: canonical.list, canonical.create, canonical.suggest_mapping, canonical.confirm_mapping"""

import uuid
from datetime import datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, func, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.db.models import CanonicalItem, InvoiceLine, PriceHistoryEntry

router = APIRouter()
logger = structlog.get_logger()


# ── Schemas ────────────────────────────────────────────────────────────────────

class CanonicalItemOut(BaseModel):
    id: uuid.UUID
    name: str
    category: str | None = None
    unit: str | None = None
    description: str | None = None
    aliases: list[str] | None = None
    is_confirmed: bool
    okpd2_code: str | None = None
    gost: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class CanonicalItemCreate(BaseModel):
    name: str
    category: str | None = None
    unit: str | None = None
    description: str | None = None
    aliases: list[str] | None = None
    okpd2_code: str | None = None
    gost: str | None = None


class CanonicalItemUpdate(BaseModel):
    name: str | None = None
    category: str | None = None
    unit: str | None = None
    description: str | None = None
    aliases: list[str] | None = None
    okpd2_code: str | None = None
    gost: str | None = None
    is_confirmed: bool | None = None


class SuggestMappingRequest(BaseModel):
    invoice_line_id: uuid.UUID | None = None
    description: str | None = None
    limit: int = 5


class SuggestMappingMatch(BaseModel):
    canonical_item_id: uuid.UUID
    canonical_name: str
    score: float
    reason: str


class SuggestMappingResponse(BaseModel):
    query: str
    matches: list[SuggestMappingMatch]


class ConfirmMappingRequest(BaseModel):
    invoice_line_id: uuid.UUID
    canonical_item_id: uuid.UUID


class PriceHistoryPoint(BaseModel):
    recorded_at: datetime
    price: float
    currency: str
    supplier_id: uuid.UUID | None = None
    invoice_id: uuid.UUID | None = None


# ── Endpoints ──────────────────────────────────────────────────────────────────

@router.get("", response_model=list[CanonicalItemOut])
async def list_canonical_items(
    q: str | None = Query(None),
    category: str | None = Query(None),
    confirmed_only: bool = Query(False),
    limit: int = Query(50, le=200),
    offset: int = Query(0),
    db: AsyncSession = Depends(get_db),
):
    """Skill: canonical.list — List canonical items with optional filters."""
    query = select(CanonicalItem)
    if q:
        query = query.where(
            or_(
                CanonicalItem.name.ilike(f"%{q}%"),
                CanonicalItem.description.ilike(f"%{q}%"),
            )
        )
    if category:
        query = query.where(CanonicalItem.category == category)
    if confirmed_only:
        query = query.where(CanonicalItem.is_confirmed.is_(True))
    query = query.order_by(CanonicalItem.name).offset(offset).limit(limit)
    result = await db.execute(query)
    return result.scalars().all()


@router.post("", response_model=CanonicalItemOut, status_code=201)
async def create_canonical_item(
    payload: CanonicalItemCreate,
    db: AsyncSession = Depends(get_db),
):
    """Skill: canonical.create — Create a new canonical item."""
    item = CanonicalItem(
        name=payload.name,
        category=payload.category,
        unit=payload.unit,
        description=payload.description,
        aliases=payload.aliases,
        okpd2_code=payload.okpd2_code,
        gost=payload.gost,
        is_confirmed=True,
    )
    db.add(item)
    await db.commit()
    await db.refresh(item)
    logger.info("canonical_item_created", item_id=str(item.id), name=item.name)
    return item


@router.get("/{item_id}", response_model=CanonicalItemOut)
async def get_canonical_item(item_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(CanonicalItem).where(CanonicalItem.id == item_id))
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Not found")
    return item


@router.patch("/{item_id}", response_model=CanonicalItemOut)
async def update_canonical_item(
    item_id: uuid.UUID,
    payload: CanonicalItemUpdate,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(CanonicalItem).where(CanonicalItem.id == item_id))
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Not found")
    for field, value in payload.model_dump(exclude_none=True).items():
        setattr(item, field, value)
    await db.commit()
    await db.refresh(item)
    return item


@router.delete("/{item_id}", status_code=204)
async def delete_canonical_item(item_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(CanonicalItem).where(CanonicalItem.id == item_id))
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Not found")
    await db.delete(item)
    await db.commit()


@router.get("/{item_id}/price-history", response_model=list[PriceHistoryPoint])
async def get_price_history(
    item_id: uuid.UUID,
    limit: int = Query(50, le=200),
    db: AsyncSession = Depends(get_db),
):
    """Get price history for a canonical item."""
    result = await db.execute(select(CanonicalItem).where(CanonicalItem.id == item_id))
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Not found")
    rows = await db.execute(
        select(PriceHistoryEntry)
        .where(PriceHistoryEntry.canonical_item_id == item_id)
        .order_by(PriceHistoryEntry.recorded_at.desc())
        .limit(limit)
    )
    return [
        PriceHistoryPoint(
            recorded_at=row.recorded_at,
            price=row.price,
            currency=row.currency,
            supplier_id=row.supplier_id,
            invoice_id=row.invoice_id,
        )
        for row in rows.scalars().all()
    ]


@router.post("/suggest", response_model=SuggestMappingResponse)
async def suggest_mapping(
    payload: SuggestMappingRequest,
    db: AsyncSession = Depends(get_db),
):
    """Skill: canonical.suggest_mapping — Suggest canonical items for an invoice line."""
    query_text = payload.description or ""

    if payload.invoice_line_id and not query_text:
        res = await db.execute(select(InvoiceLine).where(InvoiceLine.id == payload.invoice_line_id))
        line = res.scalar_one_or_none()
        if line:
            query_text = line.description or line.name or ""

    if not query_text:
        return SuggestMappingResponse(query="", matches=[])

    # Text similarity search using ilike across name + aliases
    result = await db.execute(
        select(CanonicalItem)
        .where(
            or_(
                CanonicalItem.name.ilike(f"%{query_text[:50]}%"),
                CanonicalItem.description.ilike(f"%{query_text[:50]}%"),
            )
        )
        .limit(payload.limit)
    )
    candidates = result.scalars().all()

    # Score by overlap
    qwords = set(query_text.lower().split())
    matches = []
    for c in candidates:
        cwords = set(c.name.lower().split())
        overlap = len(qwords & cwords)
        score = overlap / max(len(qwords | cwords), 1)
        matches.append(
            SuggestMappingMatch(
                canonical_item_id=c.id,
                canonical_name=c.name,
                score=round(score, 3),
                reason=f"{overlap} совпадающих слов",
            )
        )

    matches.sort(key=lambda m: m.score, reverse=True)
    return SuggestMappingResponse(query=query_text, matches=matches)


@router.post("/confirm", status_code=200)
async def confirm_mapping(
    payload: ConfirmMappingRequest,
    db: AsyncSession = Depends(get_db),
):
    """Skill: canonical.confirm_mapping — Link an invoice line to a canonical item."""
    res = await db.execute(select(InvoiceLine).where(InvoiceLine.id == payload.invoice_line_id))
    line = res.scalar_one_or_none()
    if not line:
        raise HTTPException(status_code=404, detail="Invoice line not found")

    res2 = await db.execute(select(CanonicalItem).where(CanonicalItem.id == payload.canonical_item_id))
    item = res2.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Canonical item not found")

    line.canonical_item_id = item.id
    line.canonical_key = item.name
    await db.commit()
    logger.info("canonical_mapping_confirmed", line_id=str(line.id), item_id=str(item.id))
    return {"ok": True, "canonical_item_id": str(item.id), "canonical_name": item.name}
