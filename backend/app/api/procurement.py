"""Procurement API — purchase requests and supplier contracts.

Skills: procurement.list_requests, procurement.create_request, procurement.update_request,
        procurement.send_rfq, procurement.list_contracts, procurement.get_contract
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
from app.db.models import PurchaseRequest, SupplierContract, Party, DraftEmail
from app.audit.service import log_action, add_timeline_event

router = APIRouter()
logger = structlog.get_logger()


# ── Pydantic schemas ─────────────────────────────────────────────────────────


class PurchaseRequestItem(BaseModel):
    name: str
    qty: float
    unit: str
    target_price: float | None = None
    canonical_item_id: str | None = None


class PurchaseRequestCreate(BaseModel):
    title: str
    items: list[PurchaseRequestItem]
    deadline: datetime | None = None
    notes: str | None = None
    requested_by: str = "user"


class PurchaseRequestUpdate(BaseModel):
    title: str | None = None
    items: list[dict] | None = None
    deadline: datetime | None = None
    notes: str | None = None
    status: str | None = None


class PurchaseRequestOut(BaseModel):
    id: uuid.UUID
    title: str
    requested_by: str
    status: str
    items: list[Any]
    deadline: datetime | None
    notes: str | None
    compare_session_id: uuid.UUID | None
    approval_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime
    model_config = {"from_attributes": True}


class PurchaseRequestListResponse(BaseModel):
    items: list[PurchaseRequestOut]
    total: int
    offset: int
    limit: int


class SupplierContractCreate(BaseModel):
    supplier_id: uuid.UUID
    contract_number: str | None = None
    start_date: datetime | None = None
    end_date: datetime | None = None
    payment_terms: str | None = None
    delivery_terms: str | None = None
    credit_limit: float | None = None
    currency: str = "RUB"
    notes: str | None = None


class SupplierContractUpdate(BaseModel):
    contract_number: str | None = None
    start_date: datetime | None = None
    end_date: datetime | None = None
    status: str | None = None
    payment_terms: str | None = None
    delivery_terms: str | None = None
    credit_limit: float | None = None
    notes: str | None = None


class SupplierContractOut(BaseModel):
    id: uuid.UUID
    supplier_id: uuid.UUID
    document_id: uuid.UUID | None
    contract_number: str | None
    start_date: datetime | None
    end_date: datetime | None
    status: str
    payment_terms: str | None
    delivery_terms: str | None
    credit_limit: float | None
    currency: str
    notes: str | None
    created_at: datetime
    updated_at: datetime
    model_config = {"from_attributes": True}


class SupplierContractListResponse(BaseModel):
    items: list[SupplierContractOut]
    total: int
    offset: int
    limit: int


# ── Purchase Requests ─────────────────────────────────────────────────────────


@router.get("/purchase-requests", response_model=PurchaseRequestListResponse)
async def list_purchase_requests(
    status: str | None = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    """Skill: procurement.list_requests — List purchase requests."""
    q = select(PurchaseRequest)
    if status:
        q = q.where(PurchaseRequest.status == status)
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar() or 0
    items = (
        await db.execute(q.order_by(PurchaseRequest.created_at.desc()).offset(offset).limit(limit))
    ).scalars().all()
    return PurchaseRequestListResponse(items=items, total=total, offset=offset, limit=limit)


@router.post("/purchase-requests", response_model=PurchaseRequestOut, status_code=201)
async def create_purchase_request(
    payload: PurchaseRequestCreate,
    db: AsyncSession = Depends(get_db),
):
    """Skill: procurement.create_request — Create a purchase request."""
    req = PurchaseRequest(
        title=payload.title,
        requested_by=payload.requested_by,
        items=[i.model_dump() for i in payload.items],
        deadline=payload.deadline,
        notes=payload.notes,
    )
    db.add(req)
    await db.commit()
    await db.refresh(req)
    await log_action(db, action="procurement.create_request", entity_type="purchase_request",
                     entity_id=req.id, details={"title": req.title})
    return req


@router.get("/purchase-requests/{req_id}", response_model=PurchaseRequestOut)
async def get_purchase_request(
    req_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Skill: procurement.get_request — Get purchase request details."""
    req = await db.get(PurchaseRequest, req_id)
    if not req:
        raise HTTPException(status_code=404, detail="Purchase request not found")
    return req


@router.patch("/purchase-requests/{req_id}", response_model=PurchaseRequestOut)
async def update_purchase_request(
    req_id: uuid.UUID,
    payload: PurchaseRequestUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Skill: procurement.update_request — Update purchase request."""
    req = await db.get(PurchaseRequest, req_id)
    if not req:
        raise HTTPException(status_code=404, detail="Purchase request not found")
    for k, v in payload.model_dump(exclude_unset=True).items():
        setattr(req, k, v)
    await db.commit()
    await db.refresh(req)
    return req


@router.delete("/purchase-requests/{req_id}", status_code=200)
async def cancel_purchase_request(
    req_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Cancel a purchase request."""
    req = await db.get(PurchaseRequest, req_id)
    if not req:
        raise HTTPException(status_code=404, detail="Purchase request not found")
    if req.status not in ("draft", "approved"):
        raise HTTPException(status_code=400, detail=f"Cannot cancel request in status '{req.status}'")
    req.status = "cancelled"
    await db.commit()
    return {"status": "cancelled", "id": str(req_id)}


@router.post("/purchase-requests/{req_id}/send-rfq", response_model=dict, status_code=201)
async def send_rfq(
    req_id: uuid.UUID,
    supplier_ids: list[uuid.UUID],
    db: AsyncSession = Depends(get_db),
):
    """Skill: procurement.send_rfq — Generate RFQ draft emails to suppliers (approval gate)."""
    req = await db.get(PurchaseRequest, req_id)
    if not req:
        raise HTTPException(status_code=404, detail="Purchase request not found")

    # Load suppliers
    result = await db.execute(
        select(Party).where(Party.id.in_(supplier_ids))
    )
    suppliers = result.scalars().all()
    if not suppliers:
        raise HTTPException(status_code=400, detail="No valid suppliers found")

    items_text = "\n".join(
        f"- {i.get('name', '?')}: {i.get('qty', 0)} {i.get('unit', 'шт')}"
        + (f" (целевая цена: {i.get('target_price')} руб)" if i.get("target_price") else "")
        for i in req.items
    )
    deadline_str = req.deadline.strftime("%d.%m.%Y") if req.deadline else "по согласованию"

    draft_ids = []
    for supplier in suppliers:
        subject = f"Запрос коммерческого предложения: {req.title}"
        body = (
            f"Уважаемые коллеги,\n\n"
            f"Просим предоставить коммерческое предложение на следующие позиции:\n\n"
            f"{items_text}\n\n"
            f"Срок подачи предложения: {deadline_str}\n\n"
            f"С уважением"
        )
        draft = DraftEmail(
            related_entity_type="purchase_request",
            related_entity_id=req.id,
            to_addresses=[supplier.email] if supplier.email else [],
            subject=subject,
            body_text=body,
            status="draft",
            generated_by="sveta",
        )
        db.add(draft)
        await db.flush()
        draft_ids.append(str(draft.id))

    req.status = "rfq_sent"
    await log_action(db, action="procurement.send_rfq", entity_type="purchase_request",
                     entity_id=req.id, details={"suppliers": len(suppliers), "drafts": len(draft_ids)})
    await add_timeline_event(db, entity_type="purchase_request", entity_id=req.id,
                             event_type="rfq_prepared", actor="sveta",
                             summary=f"Подготовлено {len(draft_ids)} черновиков КП для {len(suppliers)} поставщиков")
    await db.commit()
    return {"draft_email_ids": draft_ids, "supplier_count": len(suppliers)}


# ── Supplier Contracts ────────────────────────────────────────────────────────


@router.get("/supplier-contracts", response_model=SupplierContractListResponse)
async def list_supplier_contracts(
    supplier_id: uuid.UUID | None = None,
    status: str | None = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    """Skill: procurement.list_contracts — List supplier contracts."""
    q = select(SupplierContract)
    if supplier_id:
        q = q.where(SupplierContract.supplier_id == supplier_id)
    if status:
        q = q.where(SupplierContract.status == status)
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar() or 0
    items = (
        await db.execute(q.order_by(SupplierContract.created_at.desc()).offset(offset).limit(limit))
    ).scalars().all()
    return SupplierContractListResponse(items=items, total=total, offset=offset, limit=limit)


@router.post("/supplier-contracts", response_model=SupplierContractOut, status_code=201)
async def create_supplier_contract(
    payload: SupplierContractCreate,
    db: AsyncSession = Depends(get_db),
):
    """Skill: procurement.create_contract — Create supplier contract."""
    supplier = await db.get(Party, payload.supplier_id)
    if not supplier:
        raise HTTPException(status_code=404, detail="Supplier not found")
    contract = SupplierContract(**payload.model_dump())
    db.add(contract)
    await db.commit()
    await db.refresh(contract)
    await log_action(db, action="procurement.create_contract", entity_type="supplier_contract",
                     entity_id=contract.id, details={"supplier_id": str(payload.supplier_id)})
    return contract


@router.get("/supplier-contracts/{contract_id}", response_model=SupplierContractOut)
async def get_supplier_contract(
    contract_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Skill: procurement.get_contract — Get contract details."""
    contract = await db.get(SupplierContract, contract_id)
    if not contract:
        raise HTTPException(status_code=404, detail="Contract not found")
    return contract


@router.patch("/supplier-contracts/{contract_id}", response_model=SupplierContractOut)
async def update_supplier_contract(
    contract_id: uuid.UUID,
    payload: SupplierContractUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Skill: procurement.update_contract — Update contract details."""
    contract = await db.get(SupplierContract, contract_id)
    if not contract:
        raise HTTPException(status_code=404, detail="Contract not found")
    for k, v in payload.model_dump(exclude_unset=True).items():
        setattr(contract, k, v)
    await db.commit()
    await db.refresh(contract)
    return contract
