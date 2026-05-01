"""Warehouse API — inventory, receipts, stock movements.

Skills: warehouse.list_inventory, warehouse.get_item, warehouse.create_receipt,
        warehouse.confirm_receipt, warehouse.low_stock
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Literal

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, func, delete as sa_delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.session import get_db
from app.db.models import (
    InventoryItem,
    WarehouseReceipt,
    WarehouseReceiptLine,
    StockMovement,
    Invoice,
    InvoiceLine,
    Party,
)
from app.audit.service import log_action, add_timeline_event

router = APIRouter()
logger = structlog.get_logger()


# ── Pydantic schemas ─────────────────────────────────────────────────────────


class InventoryItemOut(BaseModel):
    id: uuid.UUID
    sku: str | None
    name: str
    unit: str
    current_qty: float
    min_qty: float | None
    location: str | None
    is_low_stock: bool = False
    created_at: datetime
    updated_at: datetime
    model_config = {"from_attributes": True}


class InventoryItemCreate(BaseModel):
    name: str
    unit: str
    sku: str | None = None
    min_qty: float | None = None
    location: str | None = None
    current_qty: float = 0.0


class InventoryListResponse(BaseModel):
    items: list[InventoryItemOut]
    total: int
    offset: int
    limit: int


class StockMovementOut(BaseModel):
    id: uuid.UUID
    movement_type: str
    quantity: float
    balance_after: float
    reference_type: str | None
    performed_by: str
    performed_at: datetime
    notes: str | None
    model_config = {"from_attributes": True}


class ReceiptLineOut(BaseModel):
    id: uuid.UUID
    description: str
    quantity_expected: float
    quantity_received: float
    unit: str
    discrepancy_note: str | None
    inventory_item_id: uuid.UUID | None
    invoice_line_id: uuid.UUID | None
    model_config = {"from_attributes": True}


class ReceiptLineUpdate(BaseModel):
    quantity_received: float | None = None
    discrepancy_note: str | None = None
    inventory_item_id: uuid.UUID | None = None


class ReceiptOut(BaseModel):
    id: uuid.UUID
    receipt_number: str | None
    status: str
    received_at: datetime
    received_by: str | None
    notes: str | None
    invoice_id: uuid.UUID | None
    supplier_id: uuid.UUID | None
    lines: list[ReceiptLineOut] = []
    created_at: datetime
    updated_at: datetime
    model_config = {"from_attributes": True}


class ReceiptListResponse(BaseModel):
    items: list[ReceiptOut]
    total: int
    offset: int
    limit: int


class ReceiptCreateRequest(BaseModel):
    invoice_id: uuid.UUID
    received_by: str | None = None
    notes: str | None = None


class ReceiptStatusUpdate(BaseModel):
    status: Literal["expected", "partial", "received", "issued", "cancelled"]
    notes: str | None = None


# Allowed status transitions
_RECEIPT_TRANSITIONS: dict[str, set[str]] = {
    "draft":    {"expected", "received", "cancelled"},
    "expected": {"partial", "received", "cancelled"},
    "partial":  {"received", "cancelled"},
    "received": {"issued"},
    "issued":   set(),
    "cancelled": set(),
}

RECEIPT_STATUS_LABELS = {
    "draft":    "Черновик",
    "expected": "Ожидается",
    "partial":  "Частично получен",
    "received": "Получен",
    "issued":   "Выдан",
    "cancelled": "Отменён",
}


# ── Inventory ────────────────────────────────────────────────────────────────


@router.get("/inventory", response_model=InventoryListResponse)
async def list_inventory(
    low_stock: bool = False,
    location: str | None = None,
    search: str | None = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    """Skill: warehouse.list_inventory — List inventory items."""
    q = select(InventoryItem)
    if low_stock:
        q = q.where(
            InventoryItem.min_qty.isnot(None),
            InventoryItem.current_qty < InventoryItem.min_qty,
        )
    if location:
        q = q.where(InventoryItem.location.ilike(f"%{location}%"))
    if search:
        q = q.where(InventoryItem.name.ilike(f"%{search}%"))

    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar() or 0
    items = (await db.execute(q.order_by(InventoryItem.name).offset(offset).limit(limit))).scalars().all()

    result = []
    for item in items:
        out = InventoryItemOut.model_validate(item)
        out.is_low_stock = bool(item.min_qty and item.current_qty < item.min_qty)
        result.append(out)

    return InventoryListResponse(items=result, total=total, offset=offset, limit=limit)


@router.post("/inventory", response_model=InventoryItemOut, status_code=201)
async def create_inventory_item(
    payload: InventoryItemCreate,
    db: AsyncSession = Depends(get_db),
):
    """Skill: warehouse.create_item — Create inventory position."""
    item = InventoryItem(**payload.model_dump())
    db.add(item)
    await db.commit()
    await db.refresh(item)
    return item


@router.get("/inventory/low-stock", response_model=InventoryListResponse)
async def low_stock_items(
    offset: int = 0,
    limit: int = Query(50, le=200),
    db: AsyncSession = Depends(get_db),
):
    """Skill: warehouse.low_stock — Items below minimum quantity."""
    q = select(InventoryItem).where(
        InventoryItem.min_qty.isnot(None),
        InventoryItem.current_qty < InventoryItem.min_qty,
    )
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar() or 0
    items = (await db.execute(q.order_by(InventoryItem.current_qty).offset(offset).limit(limit))).scalars().all()
    result = [InventoryItemOut.model_validate(i) for i in items]
    for r in result:
        r.is_low_stock = True
    return InventoryListResponse(items=result, total=total, offset=offset, limit=limit)


@router.get("/inventory/{item_id}", response_model=InventoryItemOut)
async def get_inventory_item(
    item_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Skill: warehouse.get_item — Get item card with recent movements."""
    item = await db.get(InventoryItem, item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    out = InventoryItemOut.model_validate(item)
    out.is_low_stock = bool(item.min_qty and item.current_qty < item.min_qty)
    return out


@router.get("/inventory/{item_id}/movements", response_model=list[StockMovementOut])
async def get_item_movements(
    item_id: uuid.UUID,
    limit: int = Query(50, le=200),
    db: AsyncSession = Depends(get_db),
):
    """Get stock movement history for an inventory item."""
    result = await db.execute(
        select(StockMovement)
        .where(StockMovement.inventory_item_id == item_id)
        .order_by(StockMovement.performed_at.desc())
        .limit(limit)
    )
    return result.scalars().all()


# ── Receipts ─────────────────────────────────────────────────────────────────


@router.get("/receipts", response_model=ReceiptListResponse)
async def list_receipts(
    status: str | None = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    """Skill: warehouse.list_receipts — List warehouse receipts."""
    q = select(WarehouseReceipt).options(selectinload(WarehouseReceipt.lines))
    if status:
        q = q.where(WarehouseReceipt.status == status)
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar() or 0
    items = (await db.execute(q.order_by(WarehouseReceipt.created_at.desc()).offset(offset).limit(limit))).scalars().all()
    return ReceiptListResponse(items=items, total=total, offset=offset, limit=limit)


@router.post("/receipts", response_model=ReceiptOut, status_code=201)
async def create_receipt(
    payload: ReceiptCreateRequest,
    db: AsyncSession = Depends(get_db),
):
    """Skill: warehouse.create_receipt — Create receipt from invoice lines."""
    invoice = await db.execute(
        select(Invoice).where(Invoice.id == payload.invoice_id).options(selectinload(Invoice.lines))
    )
    invoice = invoice.scalar_one_or_none()
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")

    # Auto-generate receipt number
    count = (await db.execute(select(func.count()).select_from(WarehouseReceipt))).scalar() or 0
    receipt_number = f"ПО-{count + 1:04d}"

    receipt = WarehouseReceipt(
        invoice_id=invoice.id,
        document_id=invoice.document_id,
        supplier_id=invoice.supplier_id,
        receipt_number=receipt_number,
        received_by=payload.received_by or "user",
        notes=payload.notes,
        status="draft",
    )
    db.add(receipt)
    await db.flush()

    # Build lines from invoice lines, try to match InventoryItem by SKU
    for il in invoice.lines:
        inv_item_id = None
        if il.sku:
            existing = (await db.execute(
                select(InventoryItem).where(InventoryItem.sku == il.sku)
            )).scalar_one_or_none()
            inv_item_id = existing.id if existing else None

        line = WarehouseReceiptLine(
            receipt_id=receipt.id,
            inventory_item_id=inv_item_id,
            invoice_line_id=il.id,
            description=il.description or "",
            quantity_expected=il.quantity or 0,
            quantity_received=0,
            unit=il.unit or "шт",
        )
        db.add(line)

    await log_action(db, action="warehouse.create_receipt", entity_type="warehouse_receipt",
                     entity_id=receipt.id, details={"invoice_id": str(invoice.id)})
    await add_timeline_event(db, entity_type="invoice", entity_id=invoice.id,
                             event_type="receipt_created", actor="user",
                             summary=f"Создан приходный ордер {receipt_number}")
    await db.commit()
    await db.refresh(receipt)

    result = await db.execute(
        select(WarehouseReceipt).where(WarehouseReceipt.id == receipt.id)
        .options(selectinload(WarehouseReceipt.lines))
    )
    return result.scalar_one()


@router.get("/receipts/{receipt_id}", response_model=ReceiptOut)
async def get_receipt(
    receipt_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Skill: warehouse.get_receipt — Get receipt with lines."""
    result = await db.execute(
        select(WarehouseReceipt).where(WarehouseReceipt.id == receipt_id)
        .options(selectinload(WarehouseReceipt.lines))
    )
    receipt = result.scalar_one_or_none()
    if not receipt:
        raise HTTPException(status_code=404, detail="Receipt not found")
    return receipt


@router.patch("/receipts/{receipt_id}/lines/{line_id}", response_model=ReceiptLineOut)
async def update_receipt_line(
    receipt_id: uuid.UUID,
    line_id: uuid.UUID,
    payload: ReceiptLineUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Update received quantity or discrepancy note on a receipt line."""
    line = await db.get(WarehouseReceiptLine, line_id)
    if not line or line.receipt_id != receipt_id:
        raise HTTPException(status_code=404, detail="Line not found")

    for k, v in payload.model_dump(exclude_unset=True).items():
        setattr(line, k, v)

    await db.commit()
    await db.refresh(line)
    return line


@router.post("/receipts/{receipt_id}/confirm", response_model=ReceiptOut)
async def confirm_receipt(
    receipt_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Skill: warehouse.confirm_receipt — Confirm receipt, update stock (approval gate)."""
    result = await db.execute(
        select(WarehouseReceipt).where(WarehouseReceipt.id == receipt_id)
        .options(selectinload(WarehouseReceipt.lines).selectinload(WarehouseReceiptLine.inventory_item))
    )
    receipt = result.scalar_one_or_none()
    if not receipt:
        raise HTTPException(status_code=404, detail="Receipt not found")
    if receipt.status not in {"draft", "expected", "partial"}:
        raise HTTPException(status_code=400, detail=f"Cannot confirm receipt with status '{receipt.status}'")

    # Atomic: update stock for each line that has an inventory_item
    for line in receipt.lines:
        qty = line.quantity_received
        if qty <= 0:
            continue

        item = line.inventory_item
        if not item:
            # Try to find or auto-create item
            item_result = await db.execute(
                select(InventoryItem).where(InventoryItem.name == line.description)
            )
            item = item_result.scalar_one_or_none()
            if not item:
                item = InventoryItem(
                    name=line.description,
                    unit=line.unit,
                    current_qty=0.0,
                )
                db.add(item)
                await db.flush()
            line.inventory_item_id = item.id

        new_balance = item.current_qty + qty
        item.current_qty = new_balance

        movement = StockMovement(
            inventory_item_id=item.id,
            movement_type="receipt",
            quantity=qty,
            balance_after=new_balance,
            reference_type="warehouse_receipt",
            reference_id=receipt.id,
            performed_by=receipt.received_by or "user",
            performed_at=datetime.now(timezone.utc),
        )
        db.add(movement)

    receipt.status = "received"

    await log_action(db, action="warehouse.confirm_receipt", entity_type="warehouse_receipt",
                     entity_id=receipt.id, details={"lines": len(receipt.lines)})
    await add_timeline_event(db, entity_type="warehouse_receipt", entity_id=receipt.id,
                             event_type="confirmed", actor="user",
                             summary=f"Приходный ордер {receipt.receipt_number} подтверждён, остатки обновлены")
    await db.commit()

    result = await db.execute(
        select(WarehouseReceipt).where(WarehouseReceipt.id == receipt_id)
        .options(selectinload(WarehouseReceipt.lines))
    )
    return result.scalar_one()


@router.patch("/receipts/{receipt_id}/status")
async def update_receipt_status(
    receipt_id: uuid.UUID,
    payload: ReceiptStatusUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Skill: warehouse.update_status — Transition receipt status."""
    receipt = await db.get(WarehouseReceipt, receipt_id)
    if not receipt:
        raise HTTPException(status_code=404, detail="Receipt not found")

    allowed = _RECEIPT_TRANSITIONS.get(receipt.status, set())
    if payload.status not in allowed:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Cannot transition from '{receipt.status}' to '{payload.status}'. "
                f"Allowed: {sorted(allowed) or 'none (terminal state)'}"
            ),
        )

    old_status = receipt.status
    receipt.status = payload.status
    if payload.notes:
        receipt.notes = (receipt.notes or "") + f"\n[{payload.status}] {payload.notes}"

    await log_action(db, action="warehouse.status_change", entity_type="warehouse_receipt",
                     entity_id=receipt.id, details={"from": old_status, "to": payload.status})
    await db.commit()
    await db.refresh(receipt)
    return {
        "id": str(receipt.id),
        "receipt_number": receipt.receipt_number,
        "status": receipt.status,
        "status_label": RECEIPT_STATUS_LABELS.get(receipt.status, receipt.status),
    }


@router.delete("/receipts/{receipt_id}", status_code=200)
async def cancel_receipt(
    receipt_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Cancel receipt (any non-terminal status)."""
    receipt = await db.get(WarehouseReceipt, receipt_id)
    if not receipt:
        raise HTTPException(status_code=404, detail="Receipt not found")
    if receipt.status in {"received", "issued", "cancelled"}:
        raise HTTPException(status_code=400, detail=f"Cannot cancel receipt with status '{receipt.status}'")
    receipt.status = "cancelled"
    await db.commit()
    return {"status": "cancelled", "receipt_id": str(receipt_id)}
