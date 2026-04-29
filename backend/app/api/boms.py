"""BOM API — Bill of Materials (спецификации изделий).

Skills: bom.list, bom.get, bom.create, bom.approve,
        bom.stock_check, bom.create_purchase_request
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.session import get_db
from app.db.models import BOM, BOMLine, InventoryItem, CanonicalItem, PurchaseRequest
from app.audit.service import log_action, add_timeline_event

router = APIRouter()
logger = structlog.get_logger()


# ── Pydantic schemas ─────────────────────────────────────────────────────────


class BOMLineCreate(BaseModel):
    line_number: int
    description: str
    quantity: float
    unit: str
    canonical_item_id: uuid.UUID | None = None
    norm_card_id: uuid.UUID | None = None
    notes: str | None = None


class BOMLineOut(BaseModel):
    id: uuid.UUID
    bom_id: uuid.UUID
    line_number: int
    description: str
    quantity: float
    unit: str
    canonical_item_id: uuid.UUID | None
    norm_card_id: uuid.UUID | None
    notes: str | None
    model_config = {"from_attributes": True}


class BOMCreate(BaseModel):
    product_name: str
    product_code: str | None = None
    version: str = "1.0"
    notes: str | None = None
    lines: list[BOMLineCreate] = []


class BOMUpdate(BaseModel):
    product_name: str | None = None
    product_code: str | None = None
    version: str | None = None
    notes: str | None = None
    status: str | None = None


class BOMOut(BaseModel):
    id: uuid.UUID
    product_name: str
    product_code: str | None
    version: str
    status: str
    document_id: uuid.UUID | None
    approved_by: str | None
    approved_at: datetime | None
    notes: str | None
    lines: list[BOMLineOut] = []
    created_at: datetime
    updated_at: datetime
    model_config = {"from_attributes": True}


class BOMListResponse(BaseModel):
    items: list[BOMOut]
    total: int
    offset: int
    limit: int


class StockCheckLine(BaseModel):
    line_number: int
    description: str
    required_qty: float
    unit: str
    available_qty: float | None
    shortage: float | None
    inventory_item_id: uuid.UUID | None
    canonical_item_id: uuid.UUID | None


class StockCheckResult(BaseModel):
    bom_id: uuid.UUID
    product_name: str
    batch_qty: float
    lines: list[StockCheckLine]
    can_produce: bool
    shortage_count: int


# ── BOM CRUD ─────────────────────────────────────────────────────────────────


@router.get("/boms", response_model=BOMListResponse)
async def list_boms(
    status: str | None = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    """Skill: bom.list — List BOMs (bill of materials)."""
    q = select(BOM).options(selectinload(BOM.lines))
    if status:
        q = q.where(BOM.status == status)
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar() or 0
    items = (await db.execute(q.order_by(BOM.created_at.desc()).offset(offset).limit(limit))).scalars().all()
    return BOMListResponse(items=items, total=total, offset=offset, limit=limit)


@router.post("/boms", response_model=BOMOut, status_code=201)
async def create_bom(
    payload: BOMCreate,
    db: AsyncSession = Depends(get_db),
):
    """Skill: bom.create — Create a new BOM."""
    bom = BOM(
        product_name=payload.product_name,
        product_code=payload.product_code,
        version=payload.version,
        notes=payload.notes,
    )
    db.add(bom)
    await db.flush()

    for line_data in payload.lines:
        line = BOMLine(bom_id=bom.id, **line_data.model_dump())
        db.add(line)

    await log_action(db, action="bom.create", entity_type="bom", entity_id=bom.id,
                     details={"product_name": bom.product_name, "lines": len(payload.lines)})
    await db.commit()

    result = await db.execute(
        select(BOM).where(BOM.id == bom.id).options(selectinload(BOM.lines))
    )
    return result.scalar_one()


@router.get("/boms/{bom_id}", response_model=BOMOut)
async def get_bom(
    bom_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Skill: bom.get — Get BOM with all lines."""
    result = await db.execute(
        select(BOM).where(BOM.id == bom_id).options(selectinload(BOM.lines))
    )
    bom = result.scalar_one_or_none()
    if not bom:
        raise HTTPException(status_code=404, detail="BOM not found")
    return bom


@router.patch("/boms/{bom_id}", response_model=BOMOut)
async def update_bom(
    bom_id: uuid.UUID,
    payload: BOMUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Skill: bom.update — Update BOM metadata."""
    result = await db.execute(
        select(BOM).where(BOM.id == bom_id).options(selectinload(BOM.lines))
    )
    bom = result.scalar_one_or_none()
    if not bom:
        raise HTTPException(status_code=404, detail="BOM not found")
    if bom.status == "approved" and payload.status != "obsolete":
        raise HTTPException(status_code=400, detail="Approved BOM can only be set to obsolete")
    for k, v in payload.model_dump(exclude_unset=True).items():
        setattr(bom, k, v)
    await db.commit()
    await db.refresh(bom)
    return bom


# ── BOM Lines ─────────────────────────────────────────────────────────────────


@router.post("/boms/{bom_id}/lines", response_model=BOMLineOut, status_code=201)
async def add_bom_line(
    bom_id: uuid.UUID,
    payload: BOMLineCreate,
    db: AsyncSession = Depends(get_db),
):
    """Add a line to BOM."""
    bom = await db.get(BOM, bom_id)
    if not bom:
        raise HTTPException(status_code=404, detail="BOM not found")
    if bom.status == "approved":
        raise HTTPException(status_code=400, detail="Cannot modify approved BOM")
    line = BOMLine(bom_id=bom_id, **payload.model_dump())
    db.add(line)
    await db.commit()
    await db.refresh(line)
    return line


@router.delete("/boms/{bom_id}/lines/{line_id}", status_code=200)
async def delete_bom_line(
    bom_id: uuid.UUID,
    line_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Remove a line from BOM."""
    line = await db.get(BOMLine, line_id)
    if not line or line.bom_id != bom_id:
        raise HTTPException(status_code=404, detail="Line not found")
    bom = await db.get(BOM, bom_id)
    if bom and bom.status == "approved":
        raise HTTPException(status_code=400, detail="Cannot modify approved BOM")
    await db.delete(line)
    await db.commit()
    return {"deleted": str(line_id)}


# ── BOM Approve ───────────────────────────────────────────────────────────────


@router.post("/boms/{bom_id}/approve", response_model=BOMOut)
async def approve_bom(
    bom_id: uuid.UUID,
    approved_by: str = "user",
    db: AsyncSession = Depends(get_db),
):
    """Skill: bom.approve — Approve BOM (approval gate)."""
    result = await db.execute(
        select(BOM).where(BOM.id == bom_id).options(selectinload(BOM.lines))
    )
    bom = result.scalar_one_or_none()
    if not bom:
        raise HTTPException(status_code=404, detail="BOM not found")
    if bom.status != "draft":
        raise HTTPException(status_code=400, detail=f"BOM is already {bom.status}")
    if not bom.lines:
        raise HTTPException(status_code=400, detail="Cannot approve BOM without lines")

    bom.status = "approved"
    bom.approved_by = approved_by
    bom.approved_at = datetime.now(timezone.utc)

    await log_action(db, action="bom.approve", entity_type="bom", entity_id=bom.id,
                     details={"approved_by": approved_by})
    await add_timeline_event(db, entity_type="bom", entity_id=bom.id,
                             event_type="approved", actor=approved_by,
                             summary=f"Спецификация {bom.product_name} v{bom.version} утверждена")
    await db.commit()
    await db.refresh(bom)
    return bom


# ── Stock Check ───────────────────────────────────────────────────────────────


@router.get("/boms/{bom_id}/stock-check", response_model=StockCheckResult)
async def bom_stock_check(
    bom_id: uuid.UUID,
    batch_qty: float = Query(1.0, ge=0.001),
    db: AsyncSession = Depends(get_db),
):
    """Skill: bom.stock_check — Check inventory availability for BOM production."""
    result = await db.execute(
        select(BOM).where(BOM.id == bom_id).options(selectinload(BOM.lines))
    )
    bom = result.scalar_one_or_none()
    if not bom:
        raise HTTPException(status_code=404, detail="BOM not found")

    check_lines = []
    shortage_count = 0

    for line in bom.lines:
        required = line.quantity * batch_qty
        available = None
        inv_item_id = None

        if line.canonical_item_id:
            inv_result = await db.execute(
                select(InventoryItem).where(
                    InventoryItem.canonical_item_id == line.canonical_item_id
                )
            )
            inv_item = inv_result.scalar_one_or_none()
            if inv_item:
                available = inv_item.current_qty
                inv_item_id = inv_item.id

        shortage = max(0.0, required - (available or 0.0)) if available is not None else None
        if shortage and shortage > 0:
            shortage_count += 1

        check_lines.append(StockCheckLine(
            line_number=line.line_number,
            description=line.description,
            required_qty=required,
            unit=line.unit,
            available_qty=available,
            shortage=shortage,
            inventory_item_id=inv_item_id,
            canonical_item_id=line.canonical_item_id,
        ))

    can_produce = shortage_count == 0 and all(l.available_qty is not None for l in check_lines)

    return StockCheckResult(
        bom_id=bom_id,
        product_name=bom.product_name,
        batch_qty=batch_qty,
        lines=check_lines,
        can_produce=can_produce,
        shortage_count=shortage_count,
    )


# ── Create Purchase Request from BOM ─────────────────────────────────────────


@router.post("/boms/{bom_id}/create-purchase-request", response_model=dict, status_code=201)
async def create_purchase_request_from_bom(
    bom_id: uuid.UUID,
    batch_qty: float = Query(1.0, ge=0.001),
    db: AsyncSession = Depends(get_db),
):
    """Skill: bom.create_purchase_request — Create purchase request from BOM shortage (approval gate)."""
    result = await db.execute(
        select(BOM).where(BOM.id == bom_id).options(selectinload(BOM.lines))
    )
    bom = result.scalar_one_or_none()
    if not bom:
        raise HTTPException(status_code=404, detail="BOM not found")

    # Determine what's missing
    items_needed = []
    for line in bom.lines:
        required = line.quantity * batch_qty
        available = 0.0

        if line.canonical_item_id:
            inv_result = await db.execute(
                select(InventoryItem).where(
                    InventoryItem.canonical_item_id == line.canonical_item_id
                )
            )
            inv_item = inv_result.scalar_one_or_none()
            if inv_item:
                available = inv_item.current_qty

        shortage = required - available
        if shortage > 0:
            items_needed.append({
                "name": line.description,
                "qty": round(shortage, 3),
                "unit": line.unit,
                "canonical_item_id": str(line.canonical_item_id) if line.canonical_item_id else None,
            })

    if not items_needed:
        return {"message": "Все материалы в наличии, заявка не нужна", "purchase_request_id": None}

    pr = PurchaseRequest(
        title=f"Закупка для {bom.product_name} v{bom.version} (партия {batch_qty})",
        requested_by="sveta",
        items=items_needed,
    )
    db.add(pr)

    await log_action(db, action="bom.create_purchase_request", entity_type="purchase_request",
                     entity_id=pr.id, details={"bom_id": str(bom_id), "items_count": len(items_needed)})
    await add_timeline_event(db, entity_type="bom", entity_id=bom_id,
                             event_type="purchase_request_created", actor="sveta",
                             summary=f"Создана заявка на {len(items_needed)} позиций для партии {batch_qty}")
    await db.commit()

    return {
        "purchase_request_id": str(pr.id),
        "items_count": len(items_needed),
        "message": f"Создана заявка на {len(items_needed)} позиций",
    }
