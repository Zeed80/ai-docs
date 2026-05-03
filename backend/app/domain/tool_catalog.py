"""Pydantic schemas for ToolSupplier, ToolCatalogEntry, catalog import."""

import uuid
from datetime import datetime
from typing import Any

from pydantic import AliasChoices, BaseModel, Field

from app.db.models import ToolTypeEnum, ToolSourceEnum


# ── Tool Supplier ─────────────────────────────────────────────────────────────


class ToolSupplierCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=500)
    website: str | None = None
    country: str | None = None
    contact_info: dict | None = None
    catalog_format: str | None = None
    notes: str | None = None
    main_supplier_id: uuid.UUID | None = None


class ToolSupplierUpdate(BaseModel):
    name: str | None = None
    website: str | None = None
    country: str | None = None
    contact_info: dict | None = None
    catalog_format: str | None = None
    notes: str | None = None
    is_active: bool | None = None
    main_supplier_id: uuid.UUID | None = None


class ToolSupplierOut(BaseModel):
    id: uuid.UUID
    name: str
    website: str | None = None
    country: str | None = None
    contact_info: dict | None = None
    catalog_format: str | None = None
    notes: str | None = None
    is_active: bool
    main_supplier_id: uuid.UUID | None = None
    created_at: datetime
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}


class ToolSupplierListResponse(BaseModel):
    items: list[ToolSupplierOut]
    total: int


# ── Tool Catalog Entry ────────────────────────────────────────────────────────


class ToolCatalogEntryCreate(BaseModel):
    supplier_id: uuid.UUID | None = None
    part_number: str | None = None
    tool_type: ToolTypeEnum
    name: str = Field(..., min_length=1, max_length=500)
    description: str | None = None
    diameter_mm: float | None = None
    length_mm: float | None = None
    parameters: dict[str, Any] | None = None
    material: str | None = None
    coating: str | None = None
    price_currency: str = "RUB"
    price_value: float | None = None
    catalog_page: int | None = None
    metadata_: dict | None = Field(
        None,
        validation_alias=AliasChoices("metadata_", "metadata"),
        serialization_alias="metadata",
    )


class ToolCatalogEntryUpdate(BaseModel):
    tool_type: ToolTypeEnum | None = None
    name: str | None = None
    description: str | None = None
    diameter_mm: float | None = None
    length_mm: float | None = None
    parameters: dict[str, Any] | None = None
    material: str | None = None
    coating: str | None = None
    price_currency: str | None = None
    price_value: float | None = None
    catalog_page: int | None = None
    is_active: bool | None = None
    metadata_: dict | None = Field(
        None,
        validation_alias=AliasChoices("metadata_", "metadata"),
        serialization_alias="metadata",
    )


class ToolCatalogEntryOut(BaseModel):
    id: uuid.UUID
    supplier_id: uuid.UUID | None = None
    part_number: str | None = None
    tool_type: ToolTypeEnum
    name: str
    description: str | None = None
    diameter_mm: float | None = None
    length_mm: float | None = None
    parameters: dict[str, Any] | None = None
    material: str | None = None
    coating: str | None = None
    price_currency: str
    price_value: float | None = None
    catalog_page: int | None = None
    is_active: bool
    metadata_: dict | None = Field(
        None,
        validation_alias=AliasChoices("metadata_", "metadata"),
        serialization_alias="metadata",
    )
    created_at: datetime
    updated_at: datetime | None = None

    model_config = {"from_attributes": True, "populate_by_name": True}


class ToolCatalogEntryWithSupplierOut(ToolCatalogEntryOut):
    supplier: ToolSupplierOut | None = None


class ToolCatalogListResponse(BaseModel):
    items: list[ToolCatalogEntryOut]
    total: int
    page: int
    page_size: int


# ── Catalog Search ────────────────────────────────────────────────────────────


class ToolCatalogSearchRequest(BaseModel):
    query: str | None = None
    tool_type: ToolTypeEnum | None = None
    supplier_id: uuid.UUID | None = None
    diameter_min: float | None = None
    diameter_max: float | None = None
    material: str | None = None
    coating: str | None = None
    max_price: float | None = None
    limit: int = Field(20, ge=1, le=100)
    semantic: bool = True


class ToolSuggestionRequest(BaseModel):
    feature_id: uuid.UUID
    limit: int = Field(5, ge=1, le=20)
    tool_types: list[ToolTypeEnum] | None = None


class ToolSuggestionItem(BaseModel):
    entry: ToolCatalogEntryOut
    supplier: ToolSupplierOut | None = None
    score: float
    reason: str | None = None
    warehouse_available: bool = False
    warehouse_qty: float | None = None


class ToolSuggestionResponse(BaseModel):
    feature_id: uuid.UUID
    suggestions: list[ToolSuggestionItem]
    model_used: str | None = None


# ── Catalog Import ────────────────────────────────────────────────────────────


class CatalogImportResult(BaseModel):
    supplier_id: uuid.UUID
    supplier_name: str
    entries_created: int
    entries_updated: int
    entries_skipped: int
    errors: list[str] = []
    task_id: str | None = None


class CatalogImportRow(BaseModel):
    """Normalized row from parsed catalog file."""
    part_number: str | None = None
    name: str
    tool_type: ToolTypeEnum
    description: str | None = None
    diameter_mm: float | None = None
    length_mm: float | None = None
    material: str | None = None
    coating: str | None = None
    price_currency: str = "RUB"
    price_value: float | None = None
    catalog_page: int | None = None
    parameters: dict[str, Any] | None = None
