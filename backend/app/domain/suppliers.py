"""Pydantic schemas for Supplier domain."""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


# ── Party / Supplier ───────────────────────────────────────────────────────


class PartyOut(BaseModel):
    id: uuid.UUID
    name: str
    inn: str | None = None
    kpp: str | None = None
    ogrn: str | None = None
    address: str | None = None
    role: str
    bank_name: str | None = None
    bank_bik: str | None = None
    bank_account: str | None = None
    corr_account: str | None = None
    contact_email: str | None = None
    contact_phone: str | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class SupplierProfileOut(BaseModel):
    id: uuid.UUID
    party_id: uuid.UUID
    total_invoices: int = 0
    total_amount: float = 0.0
    avg_processing_days: float | None = None
    last_invoice_date: datetime | None = None
    trust_score: float | None = None
    notes: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class SupplierFullOut(PartyOut):
    """Party + aggregated profile data."""
    profile: SupplierProfileOut | None = None
    recent_invoices_count: int = 0
    open_invoices_amount: float = 0.0


class SupplierSearchRequest(BaseModel):
    query: str
    limit: int = Field(10, le=50)


class SupplierSearchResponse(BaseModel):
    results: list[PartyOut]
    total: int


# ── Price History ──────────────────────────────────────────────────────────


class PriceHistoryPoint(BaseModel):
    date: str
    price: float
    invoice_number: str | None = None
    invoice_id: str | None = None


class PriceHistoryItem(BaseModel):
    description: str
    canonical_item_id: str | None = None
    points: list[PriceHistoryPoint]
    current_price: float | None = None
    min_price: float | None = None
    max_price: float | None = None
    avg_price: float | None = None
    trend: str | None = None  # up, down, stable


class SupplierPriceHistoryResponse(BaseModel):
    supplier_id: uuid.UUID
    supplier_name: str
    items: list[PriceHistoryItem]
    total_items: int


# ── Requisites Check ──────────────────────────────────────────────────────


class RequisiteCheck(BaseModel):
    field: str
    status: str  # ok, warning, error, missing
    message: str | None = None


class RequisiteCheckResponse(BaseModel):
    supplier_id: uuid.UUID
    is_valid: bool
    checks: list[RequisiteCheck]


# ── Trust Score ───────────────────────────────────────────────────────────


class TrustScoreBreakdown(BaseModel):
    factor: str
    weight: float
    score: float
    detail: str | None = None


class TrustScoreResponse(BaseModel):
    supplier_id: uuid.UUID
    trust_score: float
    breakdown: list[TrustScoreBreakdown]
    recommendation: str | None = None


# ── Alerts ────────────────────────────────────────────────────────────────


class SupplierAlert(BaseModel):
    id: str
    alert_type: str  # price_increase, missing_docs, overdue, requisite_change
    severity: str  # info, warning, error
    message: str
    created_at: str
    entity_id: str | None = None


class SupplierAlertsResponse(BaseModel):
    supplier_id: uuid.UUID
    alerts: list[SupplierAlert]
    total: int


# ── Update ────────────────────────────────────────────────────────────────


class SupplierUpdate(BaseModel):
    name: str | None = None
    inn: str | None = None
    kpp: str | None = None
    address: str | None = None
    contact_email: str | None = None
    contact_phone: str | None = None
    bank_name: str | None = None
    bank_bik: str | None = None
    bank_account: str | None = None
    corr_account: str | None = None
    notes: str | None = None
