"""Invoice API — skills: invoice.list, invoice.get, invoice.extract,
invoice.validate, invoice.approve, invoice.reject, invoice.update"""

import uuid

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from sqlalchemy import delete as sa_delete
from app.db.session import get_db
from app.db.models import Document, Invoice, InvoiceLine, InvoiceStatus, Party, WarehouseReceipt, WarehouseReceiptLine, InventoryItem
from app.domain.invoices import (
    InvoiceApproveRequest,
    InvoiceDeleteRequest,
    InvoiceFieldUpdate,
    InvoiceListResponse,
    InvoiceOut,
    InvoiceRejectRequest,
    InvoiceValidationResponse,
    InvoiceValidationError,
    PriceCheckResponse,
    PriceComparison,
)
from app.audit.service import log_action, add_timeline_event
from app.auth.jwt import require_role
from app.auth.models import UserInfo, UserRole

router = APIRouter()
logger = structlog.get_logger()


# ── invoice.list ────────────────────────────────────────────────────────────


@router.get("", response_model=InvoiceListResponse)
async def list_invoices(
    status: InvoiceStatus | None = None,
    supplier_id: uuid.UUID | None = None,
    search: str | None = None,
    offset: int = 0,
    limit: int = Query(50, le=200),
    db: AsyncSession = Depends(get_db),
):
    """Skill: invoice.list — List invoices with filters."""
    query = select(Invoice).options(
        selectinload(Invoice.lines),
        selectinload(Invoice.supplier),
        selectinload(Invoice.buyer),
    )

    if status:
        query = query.where(Invoice.status == status)
    if supplier_id:
        query = query.where(Invoice.supplier_id == supplier_id)
    if search:
        query = query.where(Invoice.invoice_number.ilike(f"%{search}%"))

    count_query = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_query)).scalar() or 0

    query = query.order_by(Invoice.created_at.desc()).offset(offset).limit(limit)
    result = await db.execute(query)
    items = result.scalars().all()

    return InvoiceListResponse(items=items, total=total, offset=offset, limit=limit)


# ── invoice.get ─────────────────────────────────────────────────────────────


@router.get("/{invoice_id}", response_model=InvoiceOut)
async def get_invoice(
    invoice_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Skill: invoice.get — Get invoice with lines."""
    result = await db.execute(
        select(Invoice)
        .where(Invoice.id == invoice_id)
        .options(
            selectinload(Invoice.lines),
            selectinload(Invoice.supplier),
            selectinload(Invoice.buyer),
        )
    )
    invoice = result.scalar_one_or_none()
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")
    return invoice


# ── invoice.extract ─────────────────────────────────────────────────────────


@router.post("/{invoice_id}/re-extract")
async def re_extract_invoice(
    invoice_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Skill: invoice.extract — Re-run extraction on invoice's document."""
    result = await db.execute(select(Invoice).where(Invoice.id == invoice_id))
    invoice = result.scalar_one_or_none()
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")

    from app.tasks.extraction import extract_invoice as extract_task

    task = extract_task.delay(str(invoice.document_id))

    await log_action(
        db,
        action="invoice.extract",
        entity_type="invoice",
        entity_id=invoice.id,
        details={"document_id": str(invoice.document_id)},
    )
    await db.commit()

    logger.info("invoice_re_extract", invoice_id=str(invoice_id), task_id=task.id)
    return {"task_id": task.id, "invoice_id": str(invoice_id), "status": "queued"}


# ── invoice.validate ────────────────────────────────────────────────────────


@router.post("/{invoice_id}/validate", response_model=InvoiceValidationResponse)
async def validate_invoice(
    invoice_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Skill: invoice.validate — Run arithmetic and format validation on invoice."""
    result = await db.execute(
        select(Invoice)
        .where(Invoice.id == invoice_id)
        .options(selectinload(Invoice.lines))
    )
    invoice = result.scalar_one_or_none()
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")

    # Build extracted dict from invoice for validation
    extracted = {
        "invoice_number": invoice.invoice_number,
        "invoice_date": invoice.invoice_date.isoformat() if invoice.invoice_date else None,
        "due_date": invoice.due_date.isoformat() if invoice.due_date else None,
        "subtotal": invoice.subtotal,
        "tax_amount": invoice.tax_amount,
        "total_amount": invoice.total_amount,
        "lines": [
            {
                "line_number": line.line_number,
                "quantity": line.quantity,
                "unit_price": line.unit_price,
                "amount": line.amount,
                "tax_rate": line.tax_rate,
                "tax_amount": line.tax_amount,
            }
            for line in invoice.lines
        ],
    }

    from app.ai.confidence import validate_arithmetic

    errors = validate_arithmetic(extracted)
    is_valid = len(errors) == 0

    await log_action(
        db,
        action="invoice.validate",
        entity_type="invoice",
        entity_id=invoice.id,
        details={"is_valid": is_valid, "error_count": len(errors)},
    )
    await db.commit()

    return InvoiceValidationResponse(
        invoice_id=invoice.id,
        is_valid=is_valid,
        errors=[InvoiceValidationError(**e) for e in errors],
        overall_confidence=invoice.overall_confidence,
    )


# ── invoice.update ──────────────────────────────────────────────────────────


@router.patch("/{invoice_id}", response_model=InvoiceOut)
async def update_invoice(
    invoice_id: uuid.UUID,
    payload: InvoiceFieldUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Skill: invoice.update — Update invoice fields after human review."""
    result = await db.execute(
        select(Invoice)
        .where(Invoice.id == invoice_id)
        .options(selectinload(Invoice.lines))
    )
    invoice = result.scalar_one_or_none()
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")

    update_data = payload.model_dump(exclude_unset=True)
    for field_name, value in update_data.items():
        setattr(invoice, field_name, value)

    await log_action(
        db,
        action="invoice.update",
        entity_type="invoice",
        entity_id=invoice.id,
        details=update_data,
    )
    await add_timeline_event(
        db,
        entity_type="invoice",
        entity_id=invoice.id,
        event_type="updated",
        summary=f"Invoice fields updated: {', '.join(update_data.keys())}",
        actor="user",
    )
    await db.commit()
    await db.refresh(invoice)
    return invoice


# ── invoice.approve ─────────────────────────────────────────────────────────


@router.post("/{invoice_id}/approve", response_model=InvoiceOut)
async def approve_invoice(
    invoice_id: uuid.UUID,
    payload: InvoiceApproveRequest,
    db: AsyncSession = Depends(get_db),
    _user: UserInfo = Depends(require_role(UserRole.manager)),
):
    """Skill: invoice.approve — Approve invoice (approval gate)."""
    result = await db.execute(
        select(Invoice)
        .where(Invoice.id == invoice_id)
        .options(selectinload(Invoice.lines))
    )
    invoice = result.scalar_one_or_none()
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")

    if invoice.status not in (InvoiceStatus.needs_review, InvoiceStatus.draft):
        raise HTTPException(
            status_code=400,
            detail=f"Cannot approve invoice in status {invoice.status}",
        )

    invoice.status = InvoiceStatus.approved

    # Record price history for each line with a canonical_item_id
    from app.db.models import PriceHistoryEntry, CanonicalItem
    for line in invoice.lines:
        if line.unit_price is not None and line.canonical_item_id:
            entry = PriceHistoryEntry(
                canonical_item_id=line.canonical_item_id,
                supplier_id=invoice.supplier_id,
                invoice_id=invoice.id,
                invoice_line_id=line.id,
                price=line.unit_price,
                quantity=line.quantity,
                currency=invoice.currency,
            )
            db.add(entry)

    # Update supplier profile stats
    if invoice.supplier_id:
        from app.db.models import SupplierProfile
        profile_result = await db.execute(
            select(SupplierProfile).where(SupplierProfile.party_id == invoice.supplier_id)
        )
        profile = profile_result.scalar_one_or_none()
        if profile:
            profile.total_invoices += 1
            if invoice.total_amount:
                profile.total_amount += invoice.total_amount
            profile.last_invoice_date = invoice.invoice_date or invoice.created_at

    await log_action(
        db,
        action="invoice.approve",
        entity_type="invoice",
        entity_id=invoice.id,
        details={"comment": payload.comment},
    )
    await add_timeline_event(
        db,
        entity_type="invoice",
        entity_id=invoice.id,
        event_type="approved",
        summary="Invoice approved",
        actor="user",
    )
    await db.commit()
    await db.refresh(invoice)
    logger.info("invoice_approved", invoice_id=str(invoice_id))
    return invoice


# ── invoice.reject ──────────────────────────────────────────────────────────


@router.post("/{invoice_id}/reject", response_model=InvoiceOut)
async def reject_invoice(
    invoice_id: uuid.UUID,
    payload: InvoiceRejectRequest,
    db: AsyncSession = Depends(get_db),
    _user: UserInfo = Depends(require_role(UserRole.manager)),
):
    """Skill: invoice.reject — Reject invoice (approval gate)."""
    result = await db.execute(
        select(Invoice)
        .where(Invoice.id == invoice_id)
        .options(selectinload(Invoice.lines))
    )
    invoice = result.scalar_one_or_none()
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")

    if invoice.status not in (InvoiceStatus.needs_review, InvoiceStatus.draft):
        raise HTTPException(
            status_code=400,
            detail=f"Cannot reject invoice in status {invoice.status}",
        )

    invoice.status = InvoiceStatus.rejected
    await log_action(
        db,
        action="invoice.reject",
        entity_type="invoice",
        entity_id=invoice.id,
        details={"reason": payload.reason},
    )
    await add_timeline_event(
        db,
        entity_type="invoice",
        entity_id=invoice.id,
        event_type="rejected",
        summary=f"Invoice rejected: {payload.reason}",
        actor="user",
    )
    await db.commit()
    await db.refresh(invoice)
    return invoice


# ── invoice.compare_prices ──────────────────────────────────────────────────


@router.get("/{invoice_id}/price-check", response_model=PriceCheckResponse)
async def price_check(
    invoice_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Skill: invoice.compare_prices — Compare line prices with previous invoices from same supplier."""
    result = await db.execute(
        select(Invoice)
        .where(Invoice.id == invoice_id)
        .options(selectinload(Invoice.lines))
    )
    invoice = result.scalar_one_or_none()
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")

    supplier_name = None
    if invoice.supplier_id:
        party_result = await db.execute(
            select(Party).where(Party.id == invoice.supplier_id)
        )
        party = party_result.scalar_one_or_none()
        if party:
            supplier_name = party.name

    # Find previous invoices from same supplier
    comparisons: list[PriceComparison] = []
    prev_count = 0

    if invoice.supplier_id:
        prev_result = await db.execute(
            select(Invoice)
            .where(
                Invoice.supplier_id == invoice.supplier_id,
                Invoice.id != invoice.id,
            )
            .options(selectinload(Invoice.lines))
            .order_by(Invoice.created_at.desc())
            .limit(5)
        )
        prev_invoices = prev_result.scalars().all()
        prev_count = len(prev_invoices)

        # Build price history: description → (price, invoice_number)
        price_history: dict[str, tuple[float, str | None]] = {}
        for prev_inv in prev_invoices:
            for line in prev_inv.lines:
                if line.description and line.unit_price is not None:
                    key = line.description.lower().strip()
                    if key not in price_history:
                        price_history[key] = (line.unit_price, prev_inv.invoice_number)

        # Compare current lines
        for line in invoice.lines:
            key = (line.description or "").lower().strip()
            prev_entry = price_history.get(key)
            prev_price = prev_entry[0] if prev_entry else None
            prev_inv_num = prev_entry[1] if prev_entry else None

            change_pct = None
            if prev_price and line.unit_price and prev_price > 0:
                change_pct = round(
                    ((line.unit_price - prev_price) / prev_price) * 100, 1
                )

            comparisons.append(
                PriceComparison(
                    line_number=line.line_number,
                    description=line.description,
                    current_price=line.unit_price,
                    previous_price=prev_price,
                    price_change_pct=change_pct,
                    previous_invoice=prev_inv_num,
                )
            )

    return PriceCheckResponse(
        invoice_id=invoice.id,
        supplier_name=supplier_name,
        comparisons=comparisons,
        total_current=invoice.total_amount,
        previous_invoice_count=prev_count,
    )


# ── invoice.delete (single) ──────────────────────────────────────────────────


@router.delete("/{invoice_id}", status_code=200)
async def delete_invoice(
    invoice_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Skill: invoice.delete — Delete a single invoice and its lines."""
    result = await db.execute(select(Invoice).where(Invoice.id == invoice_id))
    invoice = result.scalar_one_or_none()
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")

    await db.execute(sa_delete(InvoiceLine).where(InvoiceLine.invoice_id == invoice_id))
    await db.delete(invoice)
    await db.flush()

    # Graph cleanup
    try:
        from app.domain.drawing_graph import delete_invoice_graph
        await delete_invoice_graph(invoice_id, db)
    except Exception:
        pass

    await log_action(
        db,
        action="invoice.delete",
        entity_type="invoice",
        entity_id=invoice_id,
        details={"invoice_number": invoice.invoice_number},
    )
    await db.commit()
    return {"deleted": 1, "invoice_id": str(invoice_id)}


# ── invoice.bulk_delete ──────────────────────────────────────────────────────


@router.delete("", status_code=200)
async def bulk_delete_invoices(
    payload: InvoiceDeleteRequest,
    db: AsyncSession = Depends(get_db),
):
    """Skill: invoice.bulk_delete — Bulk delete invoices by ids list, filter, or all."""
    if not payload.delete_all and not payload.ids and not payload.status and not payload.supplier_id:
        raise HTTPException(
            status_code=400,
            detail="Specify ids, a filter (status/supplier_id), or set delete_all=true",
        )

    query = select(Invoice.id)

    if payload.ids:
        query = query.where(Invoice.id.in_(payload.ids))
    else:
        if payload.status:
            query = query.where(Invoice.status == payload.status)
        if payload.supplier_id:
            query = query.where(Invoice.supplier_id == payload.supplier_id)

    ids = (await db.execute(query)).scalars().all()
    if not ids:
        return {"deleted": 0}

    await db.execute(sa_delete(InvoiceLine).where(InvoiceLine.invoice_id.in_(ids)))
    await db.execute(sa_delete(Invoice).where(Invoice.id.in_(ids)))

    # Graph cleanup for all deleted invoices
    try:
        from app.domain.drawing_graph import delete_invoice_graph
        for inv_id in ids:
            await delete_invoice_graph(inv_id, db)
    except Exception:
        pass

    await log_action(
        db,
        action="invoice.bulk_delete",
        entity_type="invoice",
        entity_id=None,
        details={"count": len(ids), "filters": payload.model_dump(exclude_none=True)},
    )
    await db.commit()
    logger.info("invoices_bulk_deleted", count=len(ids))
    return {"deleted": len(ids)}


@router.post("/{invoice_id}/receive", status_code=201)
async def receive_invoice(
    invoice_id: uuid.UUID,
    received_by: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    """Skill: invoice.receive — Create warehouse receipt from this invoice's lines."""
    from sqlalchemy import func as sqlfunc
    invoice = await db.execute(
        select(Invoice).where(Invoice.id == invoice_id).options(selectinload(Invoice.lines))
    )
    invoice = invoice.scalar_one_or_none()
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")

    count = (await db.execute(select(sqlfunc.count()).select_from(WarehouseReceipt))).scalar() or 0
    receipt_number = f"ПО-{count + 1:04d}"

    receipt = WarehouseReceipt(
        invoice_id=invoice.id,
        document_id=invoice.document_id,
        supplier_id=invoice.supplier_id,
        receipt_number=receipt_number,
        received_by=received_by or "user",
        status="expected",
    )
    db.add(receipt)
    await db.flush()

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
    await db.commit()
    return {"receipt_id": str(receipt.id), "receipt_number": receipt_number}
