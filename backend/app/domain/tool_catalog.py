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


# ── Web-sourced catalog ingestion — Ф3 (AGENT_AUTONOMY_ROADMAP.md) ────────────
#
# Draft-first: entries created this way get metadata_.review_status="ingested"
# (see backend/app/tasks/drawing_analysis.py::_create_catalog_entries_from_rows),
# unlike the manual-upload path (upload_catalog/refresh_catalog above), which
# is unchanged and stays unreviewed-by-default for backward compatibility.


class IngestWebSourceRequest(BaseModel):
    """One fetched page/document (shape mirrors WebDiscoverSource from
    app.api.computer_use — the exploratory web_discover capability's output)
    to structure into catalog entries for one supplier."""

    url: str = Field(..., min_length=4, max_length=2048)
    title: str | None = None
    text: str = Field(..., min_length=1)
    snippet: str | None = None


class IngestWebSourceResult(BaseModel):
    supplier_id: uuid.UUID
    source_url: str
    entries_created: int
    entries_conflicted: int
    entries_skipped: int
    anomaly_ids: list[uuid.UUID] = []
    errors: list[str] = []


class SupplierRefRequest(BaseModel):
    """Reference to a supplier by any identifier the agent actually has.

    The agent talks about suppliers by NAME (what the user said, what the
    invoice says); the tool catalog is keyed by ToolSupplier.id and the
    procurement side by Party.id. Accepting all three here is what lets a
    chat turn ("прикрепи каталог к ООО Мир Станочника") reach the catalog
    without the model having to guess a UUID.
    """

    supplier_id: uuid.UUID | None = None   # ToolSupplier.id
    party_id: uuid.UUID | None = None      # Party.id (procurement supplier)
    supplier_name: str | None = Field(default=None, max_length=500)


class SupplierCandidate(BaseModel):
    party_id: uuid.UUID | None = None
    tool_supplier_id: uuid.UUID | None = None
    name: str
    inn: str | None = None


class ResolveSupplierResult(BaseModel):
    """Either a single resolved supplier, or the candidates to ask about."""

    resolved: bool
    tool_supplier_id: uuid.UUID | None = None
    party_id: uuid.UUID | None = None
    name: str | None = None
    candidates: list[SupplierCandidate] = []
    message: str = ""


class AttachWebCatalogRequest(SupplierRefRequest):
    """Fetch web pages/PDFs and attach their contents to a supplier's catalog.

    Accepts one ``url`` or a list of ``urls`` — "найди ВСЕ каталоги и прикрепи"
    is one call, not one call per file the agent has to remember to repeat.
    """

    url: str | None = Field(default=None, min_length=4, max_length=2048)
    urls: list[str] | None = Field(default=None, max_length=20)
    title: str | None = None
    # Scanned-PDF catalogs need OCR; keep the page cap explicit and bounded.
    max_pages: int = Field(10, ge=0, le=20)
    # How much of a long catalog to actually parse (chunks of ~6 000 chars).
    # Each chunk is one LLM call on a shared GPU — the default keeps a chat
    # turn to a few minutes; raise it for a deliberate full-catalog import.
    max_chunks: int = Field(8, ge=1, le=24)
    # Wait for the ingestion instead of queueing it. Off by default: parsing a
    # real catalog is minutes per fragment, and a chat turn must not hold the
    # connection (or the GPU) for that long.
    wait: bool = False

    def source_urls(self) -> list[str]:
        out: list[str] = []
        for candidate in [self.url, *(self.urls or [])]:
            value = (candidate or "").strip()
            if value and value not in out:
                out.append(value)
        return out


class CatalogIngestStatusRequest(SupplierRefRequest):
    """Progress of the background catalog ingestion for one supplier."""


class CatalogIngestStatusResult(BaseModel):
    tool_supplier_id: uuid.UUID
    party_id: uuid.UUID | None = None
    supplier_name: str
    in_progress: bool = False
    entries_created: int = 0
    entries_conflicted: int = 0
    sources: list[dict] = []
    report: dict = {}
    message: str = ""


class AttachWebCatalogResult(BaseModel):
    tool_supplier_id: uuid.UUID
    party_id: uuid.UUID | None = None
    supplier_name: str
    source_url: str
    final_url: str | None = None
    entries_created: int = 0
    entries_conflicted: int = 0
    entries_skipped: int = 0
    anomaly_ids: list[uuid.UUID] = []
    errors: list[str] = []
    text_chars: int = 0
    message: str = ""
    # queued (handed to the worker) | done (parsed inline, wait=true)
    status: str = "done"
    task_id: str | None = None
    # Per-source outcome + a ready-to-publish report block (clickable links),
    # so the agent's summary table is built from what actually happened rather
    # than re-derived by the model from prose.
    sources: list["AttachedSourceResult"] = []
    report: dict = {}


class DiscoverCatalogsRequest(SupplierRefRequest):
    """Find every catalog / price list a supplier publishes on the web."""

    # Explicit site when the supplier's is known; otherwise it is resolved from
    # ToolSupplier.website, then from a web search on the supplier's name.
    website: str | None = None
    max_candidates: int = Field(20, ge=1, le=50)
    # Catalog pages to open and mine for file links (each is a real page load).
    max_pages_to_scan: int = Field(4, ge=0, le=10)


class CatalogCandidate(BaseModel):
    url: str
    title: str = ""
    kind: str = "page"  # pdf | page | spreadsheet
    found_via: str = ""  # search | site_scan
    on_supplier_site: bool = False


class DiscoverCatalogsResult(BaseModel):
    tool_supplier_id: uuid.UUID | None = None
    party_id: uuid.UUID | None = None
    supplier_name: str | None = None
    website: str | None = None
    candidates: list[CatalogCandidate] = []
    scanned_pages: list[str] = []
    diagnostics: list[str] = []
    message: str = ""
    # Ready-to-publish workspace block (clickable links), same contract as
    # AttachWebCatalogResult.report — "найди каталоги" also deserves a table.
    report: dict = {}


class AttachedSourceResult(BaseModel):
    url: str
    title: str | None = None
    status: str  # queued | running | attached | empty | error
    entries_created: int = 0
    entries_conflicted: int = 0
    entries_skipped: int = 0
    text_chars: int = 0
    message: str = ""
