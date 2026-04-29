"""Payments API — payment schedules and calendar.

Skills: payment.list_schedule, payment.create_schedule, payment.mark_paid,
        payment.overdue, payment.upcoming
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone, timedelta

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, func, and_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.session import get_db
from app.db.models import PaymentSchedule, Invoice, Party, CalendarEvent, Reminder
from app.audit.service import log_action, add_timeline_event

router = APIRouter()
logger = structlog.get_logger()


# ── Pydantic schemas ─────────────────────────────────────────────────────────


class PaymentScheduleCreate(BaseModel):
    invoice_id: uuid.UUID
    due_date: datetime
    amount: float
    currency: str = "RUB"
    payment_number: int = 1
    payment_method: str | None = None
    notes: str | None = None


class PaymentScheduleUpdate(BaseModel):
    due_date: datetime | None = None
    amount: float | None = None
    payment_method: str | None = None
    notes: str | None = None
    status: str | None = None


class MarkPaidRequest(BaseModel):
    paid_amount: float | None = None
    reference: str | None = None
    paid_at: datetime | None = None


class PaymentScheduleOut(BaseModel):
    id: uuid.UUID
    invoice_id: uuid.UUID
    payment_number: int
    due_date: datetime
    amount: float
    currency: str
    status: str
    payment_method: str | None
    paid_at: datetime | None
    paid_amount: float | None
    reference: str | None
    notes: str | None
    created_at: datetime
    updated_at: datetime
    model_config = {"from_attributes": True}


class PaymentScheduleListResponse(BaseModel):
    items: list[PaymentScheduleOut]
    total: int
    offset: int
    limit: int


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.get("/payment-schedules", response_model=PaymentScheduleListResponse)
async def list_payment_schedules(
    status: str | None = None,
    invoice_id: uuid.UUID | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    """Skill: payment.list_schedule — List payment schedules with filters."""
    q = select(PaymentSchedule)
    if status:
        q = q.where(PaymentSchedule.status == status)
    if invoice_id:
        q = q.where(PaymentSchedule.invoice_id == invoice_id)
    if date_from:
        q = q.where(PaymentSchedule.due_date >= date_from)
    if date_to:
        q = q.where(PaymentSchedule.due_date <= date_to)
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar() or 0
    items = (
        await db.execute(q.order_by(PaymentSchedule.due_date.asc()).offset(offset).limit(limit))
    ).scalars().all()
    return PaymentScheduleListResponse(items=items, total=total, offset=offset, limit=limit)


@router.post("/payment-schedules", response_model=PaymentScheduleOut, status_code=201)
async def create_payment_schedule(
    payload: PaymentScheduleCreate,
    db: AsyncSession = Depends(get_db),
):
    """Skill: payment.create_schedule — Create payment schedule entry."""
    invoice = await db.get(Invoice, payload.invoice_id)
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")

    schedule = PaymentSchedule(**payload.model_dump())
    db.add(schedule)
    await db.flush()

    # Create calendar event for due date
    calendar_event = CalendarEvent(
        title=f"Оплата счёта {invoice.invoice_number or str(invoice.id)[:8]} — {payload.amount} {payload.currency}",
        event_type="payment",
        event_date=payload.due_date,
        entity_type="payment_schedule",
        entity_id=schedule.id,
        source="manual",
    )
    db.add(calendar_event)

    # Create reminder 3 days before
    remind_at = payload.due_date - timedelta(days=3)
    if remind_at > datetime.now(timezone.utc):
        supplier_name = ""
        if invoice.supplier_id:
            supplier = await db.get(Party, invoice.supplier_id)
            supplier_name = f" поставщику {supplier.name}" if supplier else ""
        reminder = Reminder(
            entity_type="payment_schedule",
            entity_id=schedule.id,
            remind_at=remind_at,
            message=f"Платёж {payload.amount} {payload.currency}{supplier_name} через 3 дня",
        )
        db.add(reminder)

    await log_action(db, action="payment.create_schedule", entity_type="payment_schedule",
                     entity_id=schedule.id, details={"invoice_id": str(payload.invoice_id), "amount": payload.amount})
    await db.commit()
    await db.refresh(schedule)
    return schedule


@router.get("/payment-schedules/overdue", response_model=PaymentScheduleListResponse)
async def list_overdue_payments(
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    """Skill: payment.overdue — List overdue payments."""
    now = datetime.now(timezone.utc)
    q = select(PaymentSchedule).where(
        PaymentSchedule.due_date < now,
        PaymentSchedule.status.in_(["scheduled", "partial"]),
    )
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar() or 0
    items = (
        await db.execute(q.order_by(PaymentSchedule.due_date.asc()).offset(offset).limit(limit))
    ).scalars().all()
    return PaymentScheduleListResponse(items=items, total=total, offset=offset, limit=limit)


@router.get("/payment-schedules/upcoming", response_model=PaymentScheduleListResponse)
async def list_upcoming_payments(
    days: int = Query(7, ge=1, le=90),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    """Skill: payment.upcoming — List upcoming payments within N days."""
    now = datetime.now(timezone.utc)
    until = now + timedelta(days=days)
    q = select(PaymentSchedule).where(
        PaymentSchedule.due_date >= now,
        PaymentSchedule.due_date <= until,
        PaymentSchedule.status.in_(["scheduled", "partial"]),
    )
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar() or 0
    items = (
        await db.execute(q.order_by(PaymentSchedule.due_date.asc()).offset(offset).limit(limit))
    ).scalars().all()
    return PaymentScheduleListResponse(items=items, total=total, offset=offset, limit=limit)


@router.get("/payment-schedules/{schedule_id}", response_model=PaymentScheduleOut)
async def get_payment_schedule(
    schedule_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Get payment schedule details."""
    schedule = await db.get(PaymentSchedule, schedule_id)
    if not schedule:
        raise HTTPException(status_code=404, detail="Payment schedule not found")
    return schedule


@router.patch("/payment-schedules/{schedule_id}", response_model=PaymentScheduleOut)
async def update_payment_schedule(
    schedule_id: uuid.UUID,
    payload: PaymentScheduleUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Update payment schedule."""
    schedule = await db.get(PaymentSchedule, schedule_id)
    if not schedule:
        raise HTTPException(status_code=404, detail="Payment schedule not found")
    if schedule.status in ("paid", "cancelled"):
        raise HTTPException(status_code=400, detail=f"Cannot edit schedule in status '{schedule.status}'")
    for k, v in payload.model_dump(exclude_unset=True).items():
        setattr(schedule, k, v)
    await db.commit()
    await db.refresh(schedule)
    return schedule


@router.post("/payment-schedules/{schedule_id}/mark-paid", response_model=PaymentScheduleOut)
async def mark_paid(
    schedule_id: uuid.UUID,
    payload: MarkPaidRequest,
    db: AsyncSession = Depends(get_db),
):
    """Skill: payment.mark_paid — Mark payment as paid (approval gate)."""
    schedule = await db.get(PaymentSchedule, schedule_id)
    if not schedule:
        raise HTTPException(status_code=404, detail="Payment schedule not found")
    if schedule.status == "paid":
        raise HTTPException(status_code=400, detail="Payment already marked as paid")
    if schedule.status == "cancelled":
        raise HTTPException(status_code=400, detail="Payment is cancelled")

    schedule.status = "paid"
    schedule.paid_at = payload.paid_at or datetime.now(timezone.utc)
    schedule.paid_amount = payload.paid_amount or schedule.amount
    if payload.reference:
        schedule.reference = payload.reference

    await log_action(db, action="payment.mark_paid", entity_type="payment_schedule",
                     entity_id=schedule.id,
                     details={"amount": schedule.paid_amount, "reference": schedule.reference})
    await add_timeline_event(db, entity_type="payment_schedule", entity_id=schedule.id,
                             event_type="paid", actor="user",
                             summary=f"Оплачено {schedule.paid_amount} {schedule.currency}"
                                     + (f", п/п {schedule.reference}" if schedule.reference else ""))
    await db.commit()
    await db.refresh(schedule)
    return schedule


# ── Invoice shortcuts ─────────────────────────────────────────────────────────


@router.post("/invoices/{invoice_id}/schedule-payment", response_model=PaymentScheduleOut, status_code=201)
async def schedule_invoice_payment(
    invoice_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Skill: payment.schedule_from_invoice — Create payment schedule from invoice due date and total."""
    invoice = await db.get(Invoice, invoice_id)
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")

    # Check if schedule already exists
    existing = (await db.execute(
        select(PaymentSchedule).where(PaymentSchedule.invoice_id == invoice_id)
    )).scalar_one_or_none()
    if existing:
        return existing

    due_date = invoice.due_date or (invoice.invoice_date + timedelta(days=30) if invoice.invoice_date else datetime.now(timezone.utc) + timedelta(days=30))
    amount = invoice.total_amount or 0.0
    currency = invoice.currency or "RUB"

    schedule = PaymentSchedule(
        invoice_id=invoice_id,
        due_date=due_date,
        amount=amount,
        currency=currency,
    )
    db.add(schedule)
    await db.flush()

    # Calendar event
    calendar_event = CalendarEvent(
        title=f"Оплата счёта {invoice.invoice_number or str(invoice_id)[:8]} — {amount} {currency}",
        event_type="payment",
        event_date=due_date,
        entity_type="payment_schedule",
        entity_id=schedule.id,
        source="manual",
    )
    db.add(calendar_event)

    # Reminder 3 days before
    remind_at = due_date - timedelta(days=3)
    if remind_at > datetime.now(timezone.utc):
        reminder = Reminder(
            entity_type="payment_schedule",
            entity_id=schedule.id,
            remind_at=remind_at,
            message=f"Платёж {amount} {currency} через 3 дня (счёт {invoice.invoice_number or ''})",
        )
        db.add(reminder)

    await log_action(db, action="payment.schedule_from_invoice", entity_type="payment_schedule",
                     entity_id=schedule.id, details={"invoice_id": str(invoice_id), "amount": amount})
    await db.commit()
    await db.refresh(schedule)
    return schedule
