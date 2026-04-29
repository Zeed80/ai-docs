"""Pydantic schemas for Calendar & Reminders domain."""

import uuid
from datetime import datetime

from pydantic import BaseModel


class CalendarEventCreate(BaseModel):
    title: str
    event_date: datetime
    event_type: str  # due_date, payment, delivery, meeting
    entity_type: str | None = None
    entity_id: uuid.UUID | None = None
    source: str = "manual"


class CalendarEventOut(BaseModel):
    id: uuid.UUID
    title: str
    event_date: datetime
    event_type: str
    entity_type: str | None = None
    entity_id: uuid.UUID | None = None
    source: str
    user_id: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class ReminderCreate(BaseModel):
    entity_type: str
    entity_id: uuid.UUID
    remind_at: datetime
    message: str
    calendar_event_id: uuid.UUID | None = None


class ReminderOut(BaseModel):
    id: uuid.UUID
    calendar_event_id: uuid.UUID | None = None
    entity_type: str
    entity_id: uuid.UUID
    remind_at: datetime
    message: str
    is_sent: bool
    sent_at: datetime | None = None
    user_id: str
    created_at: datetime

    model_config = {"from_attributes": True}


class ExtractDatesRequest(BaseModel):
    invoice_id: uuid.UUID


class ExtractedDate(BaseModel):
    date: datetime
    event_type: str
    source_field: str


class ExtractDatesResponse(BaseModel):
    invoice_id: uuid.UUID
    dates: list[ExtractedDate]
    events_created: int


class UpcomingResponse(BaseModel):
    events: list[CalendarEventOut]
    reminders: list[ReminderOut]
