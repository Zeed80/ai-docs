"""Pydantic schemas for Collections domain."""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class CollectionItemOut(BaseModel):
    id: uuid.UUID
    entity_type: str
    entity_id: uuid.UUID
    note: str | None = None
    added_by: str
    created_at: datetime

    model_config = {"from_attributes": True}


class CollectionOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None = None
    user_id: str
    is_closed: bool = False
    closed_at: datetime | None = None
    closure_summary: str | None = None
    items: list[CollectionItemOut] = []
    created_at: datetime

    model_config = {"from_attributes": True}


class CollectionCreate(BaseModel):
    name: str
    description: str | None = None


class CollectionAddItem(BaseModel):
    entity_type: str
    entity_id: uuid.UUID
    note: str | None = None


class CollectionSummaryResponse(BaseModel):
    collection_id: uuid.UUID
    summary: str
    item_count: int
    entity_types: dict[str, int]


class CollectionTimelineEvent(BaseModel):
    timestamp: str
    event_type: str
    entity_type: str
    entity_id: str
    summary: str


class CollectionTimelineResponse(BaseModel):
    collection_id: uuid.UUID
    events: list[CollectionTimelineEvent]
    total: int
