"""Pydantic schemas for Compare КП (commercial offers) domain."""

import uuid
from datetime import datetime

from pydantic import BaseModel


class CompareCreateRequest(BaseModel):
    name: str
    invoice_ids: list[uuid.UUID]


class AlignedItem(BaseModel):
    canonical_name: str
    items: dict[str, dict | None]  # supplier_id → {description, qty, unit_price, amount}


class CompareAlignResponse(BaseModel):
    session_id: uuid.UUID
    items: list[AlignedItem]


class CompareDecideRequest(BaseModel):
    chosen_supplier_id: uuid.UUID
    reasoning: str | None = None


class CompareSessionOut(BaseModel):
    id: uuid.UUID
    name: str
    status: str
    invoice_ids: list[str]
    alignment: dict | None = None
    decision: dict | None = None
    decided_by: str | None = None
    decided_at: datetime | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class CompareSummaryResponse(BaseModel):
    session_id: uuid.UUID
    total_items: int
    suppliers: list[dict]
    cheapest_total: dict | None = None
    recommendation: str | None = None
