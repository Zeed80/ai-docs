"""Calendar & Reminders API — skills: calendar.extract_dates, calendar.upcoming,
calendar.create_event, calendar.create_reminder, calendar.mark_sent"""

import uuid
from datetime import datetime, timedelta, timezone

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.db.models import CalendarEvent, Invoice, Reminder
from app.domain.calendar import (
    CalendarEventCreate,
    CalendarEventOut,
    ExtractDatesRequest,
    ExtractDatesResponse,
    ExtractedDate,
    ReminderCreate,
    ReminderOut,
    UpcomingResponse,
)

router = APIRouter()
logger = structlog.get_logger()


# ── calendar.create_event ─────────────────────────────────────────────────


@router.post("/events", response_model=CalendarEventOut)
async def create_event(
    payload: CalendarEventCreate,
    db: AsyncSession = Depends(get_db),
):
    """Skill: calendar.create_event — Create a calendar event."""
    event = CalendarEvent(
        title=payload.title,
        event_date=payload.event_date,
        event_type=payload.event_type,
        entity_type=payload.entity_type,
        entity_id=payload.entity_id,
        source=payload.source,
    )
    db.add(event)
    await db.commit()
    await db.refresh(event)
    return event


# ── calendar.list_events ──────────────────────────────────────────────────


@router.get("/events", response_model=list[CalendarEventOut])
async def list_events(
    event_type: str | None = None,
    from_date: datetime | None = None,
    to_date: datetime | None = None,
    entity_id: uuid.UUID | None = None,
    limit: int = Query(50, le=200),
    db: AsyncSession = Depends(get_db),
):
    """Skill: calendar.list_events — List calendar events with filters."""
    query = select(CalendarEvent)
    if event_type:
        query = query.where(CalendarEvent.event_type == event_type)
    if from_date:
        query = query.where(CalendarEvent.event_date >= from_date)
    if to_date:
        query = query.where(CalendarEvent.event_date <= to_date)
    if entity_id:
        query = query.where(CalendarEvent.entity_id == entity_id)
    query = query.order_by(CalendarEvent.event_date.asc()).limit(limit)
    result = await db.execute(query)
    return result.scalars().all()


# ── calendar.extract_dates ────────────────────────────────────────────────


@router.post("/extract-dates", response_model=ExtractDatesResponse)
async def extract_dates(
    payload: ExtractDatesRequest,
    db: AsyncSession = Depends(get_db),
):
    """Skill: calendar.extract_dates — Extract dates from invoice and create events."""
    result = await db.execute(
        select(Invoice).where(Invoice.id == payload.invoice_id)
    )
    invoice = result.scalar_one_or_none()
    if not invoice:
        raise HTTPException(404, "Invoice not found")

    extracted: list[ExtractedDate] = []
    events_created = 0

    # Extract invoice_date
    if invoice.invoice_date:
        extracted.append(ExtractedDate(
            date=invoice.invoice_date if isinstance(invoice.invoice_date, datetime)
            else datetime.combine(invoice.invoice_date, datetime.min.time()).replace(tzinfo=timezone.utc),
            event_type="invoice_date",
            source_field="invoice_date",
        ))

    # Extract due_date from metadata
    meta = invoice.metadata_ or {}
    if meta.get("due_date"):
        try:
            due = datetime.fromisoformat(str(meta["due_date"]))
            if due.tzinfo is None:
                due = due.replace(tzinfo=timezone.utc)
            extracted.append(ExtractedDate(
                date=due,
                event_type="due_date",
                source_field="metadata.due_date",
            ))
        except (ValueError, TypeError):
            pass

    if meta.get("delivery_date"):
        try:
            delivery = datetime.fromisoformat(str(meta["delivery_date"]))
            if delivery.tzinfo is None:
                delivery = delivery.replace(tzinfo=timezone.utc)
            extracted.append(ExtractedDate(
                date=delivery,
                event_type="delivery",
                source_field="metadata.delivery_date",
            ))
        except (ValueError, TypeError):
            pass

    if meta.get("payment_date"):
        try:
            payment = datetime.fromisoformat(str(meta["payment_date"]))
            if payment.tzinfo is None:
                payment = payment.replace(tzinfo=timezone.utc)
            extracted.append(ExtractedDate(
                date=payment,
                event_type="payment",
                source_field="metadata.payment_date",
            ))
        except (ValueError, TypeError):
            pass

    # Create calendar events for extracted dates
    inv_number = invoice.invoice_number or str(invoice.id)[:8]
    for ed in extracted:
        # Check if event already exists
        existing = await db.execute(
            select(CalendarEvent).where(
                CalendarEvent.entity_id == invoice.id,
                CalendarEvent.event_type == ed.event_type,
            )
        )
        if existing.scalar_one_or_none():
            continue

        event = CalendarEvent(
            title=f"{ed.event_type}: счёт {inv_number}",
            event_date=ed.date,
            event_type=ed.event_type,
            entity_type="invoice",
            entity_id=invoice.id,
            source="extraction",
        )
        db.add(event)
        events_created += 1

    if events_created:
        await db.commit()

    logger.info(
        "dates_extracted",
        invoice_id=str(invoice.id),
        found=len(extracted),
        created=events_created,
    )

    return ExtractDatesResponse(
        invoice_id=invoice.id,
        dates=extracted,
        events_created=events_created,
    )


# ── calendar.upcoming ────────────────────────────────────────────────────


@router.get("/upcoming", response_model=UpcomingResponse)
async def upcoming(
    days: int = Query(7, le=90),
    db: AsyncSession = Depends(get_db),
):
    """Skill: calendar.upcoming — Get upcoming events and pending reminders."""
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(days=days)

    events_result = await db.execute(
        select(CalendarEvent)
        .where(
            CalendarEvent.event_date >= now,
            CalendarEvent.event_date <= horizon,
        )
        .order_by(CalendarEvent.event_date.asc())
        .limit(50)
    )
    events = events_result.scalars().all()

    reminders_result = await db.execute(
        select(Reminder)
        .where(
            Reminder.is_sent == False,
            Reminder.remind_at <= horizon,
        )
        .order_by(Reminder.remind_at.asc())
        .limit(50)
    )
    reminders = reminders_result.scalars().all()

    return UpcomingResponse(events=events, reminders=reminders)


# ── calendar.create_reminder ─────────────────────────────────────────────


@router.post("/reminders", response_model=ReminderOut)
async def create_reminder(
    payload: ReminderCreate,
    db: AsyncSession = Depends(get_db),
):
    """Skill: calendar.create_reminder — Create a reminder."""
    reminder = Reminder(
        calendar_event_id=payload.calendar_event_id,
        entity_type=payload.entity_type,
        entity_id=payload.entity_id,
        remind_at=payload.remind_at,
        message=payload.message,
    )
    db.add(reminder)
    await db.commit()
    await db.refresh(reminder)
    return reminder


# ── calendar.list_reminders ──────────────────────────────────────────────


@router.get("/reminders", response_model=list[ReminderOut])
async def list_reminders(
    is_sent: bool | None = None,
    entity_id: uuid.UUID | None = None,
    limit: int = Query(50, le=200),
    db: AsyncSession = Depends(get_db),
):
    """Skill: calendar.list_reminders — List reminders."""
    query = select(Reminder)
    if is_sent is not None:
        query = query.where(Reminder.is_sent == is_sent)
    if entity_id:
        query = query.where(Reminder.entity_id == entity_id)
    query = query.order_by(Reminder.remind_at.asc()).limit(limit)
    result = await db.execute(query)
    return result.scalars().all()


# ── calendar.mark_sent ───────────────────────────────────────────────────


@router.post("/reminders/{reminder_id}/mark-sent", response_model=ReminderOut)
async def mark_sent(
    reminder_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Skill: calendar.mark_sent — Mark a reminder as sent."""
    result = await db.execute(
        select(Reminder).where(Reminder.id == reminder_id)
    )
    reminder = result.scalar_one_or_none()
    if not reminder:
        raise HTTPException(404, "Reminder not found")
    if reminder.is_sent:
        raise HTTPException(400, "Already sent")

    reminder.is_sent = True
    reminder.sent_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(reminder)
    return reminder
