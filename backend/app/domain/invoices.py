"""Pydantic schemas for Invoice domain."""

import uuid
from datetime import datetime

from pydantic import BaseModel

from app.db.models import InvoiceStatus


class InvoiceLineOut(BaseModel):
    id: uuid.UUID
    line_number: int
    sku: str | None = None
    description: str | None
    quantity: float | None
    unit: str | None
    unit_price: float | None
    amount: float | None
    tax_rate: float | None
    tax_amount: float | None
    weight: float | None = None
    confidence: float | None

    model_config = {"from_attributes": True}


class PartyOut(BaseModel):
    id: uuid.UUID
    name: str
    inn: str | None = None
    kpp: str | None = None
    address: str | None = None
    bank_name: str | None = None
    bank_bik: str | None = None
    bank_account: str | None = None
    corr_account: str | None = None
    contact_phone: str | None = None
    contact_email: str | None = None

    model_config = {"from_attributes": True}


class InvoiceOut(BaseModel):
    id: uuid.UUID
    document_id: uuid.UUID
    invoice_number: str | None
    invoice_date: datetime | None
    due_date: datetime | None
    validity_date: datetime | None = None
    currency: str
    payment_id: str | None = None
    notes: str | None = None
    supplier_id: uuid.UUID | None
    buyer_id: uuid.UUID | None
    supplier: "PartyOut | None" = None
    buyer: "PartyOut | None" = None
    subtotal: float | None
    tax_amount: float | None
    total_amount: float | None
    status: InvoiceStatus
    overall_confidence: float | None
    lines: list[InvoiceLineOut] = []
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class InvoiceApproveRequest(BaseModel):
    comment: str | None = None


class InvoiceRejectRequest(BaseModel):
    reason: str


class InvoiceListResponse(BaseModel):
    items: list[InvoiceOut]
    total: int
    offset: int
    limit: int


class InvoiceValidationError(BaseModel):
    field: str
    error_type: str
    message: str
    expected: str | None = None
    actual: str | None = None
    severity: str = "error"


class InvoiceValidationResponse(BaseModel):
    invoice_id: uuid.UUID
    is_valid: bool
    errors: list[InvoiceValidationError] = []
    overall_confidence: float | None = None


class InvoiceFieldUpdate(BaseModel):
    """Update specific invoice fields after review."""

    invoice_number: str | None = None
    invoice_date: datetime | None = None
    due_date: datetime | None = None
    validity_date: datetime | None = None
    currency: str | None = None
    subtotal: float | None = None
    tax_amount: float | None = None
    total_amount: float | None = None
    payment_id: str | None = None
    notes: str | None = None


class InvoiceDeleteRequest(BaseModel):
    """Bulk delete request — pass ids OR filters."""
    ids: list[uuid.UUID] | None = None
    status: InvoiceStatus | None = None
    supplier_id: uuid.UUID | None = None
    delete_all: bool = False


class PriceComparison(BaseModel):
    line_number: int
    description: str | None
    current_price: float | None
    previous_price: float | None
    price_change_pct: float | None
    previous_invoice: str | None


class PriceCheckResponse(BaseModel):
    invoice_id: uuid.UUID
    supplier_name: str | None = None
    comparisons: list[PriceComparison] = []
    total_current: float | None = None
    total_previous: float | None = None
    total_change_pct: float | None = None
    previous_invoice_count: int = 0
