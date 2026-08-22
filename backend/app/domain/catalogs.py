"""Pydantic schemas for supplier catalogs — the browsing/viewing side.

Separate from domain/tool_catalog.py (which is about positions and ingestion):
this module answers "what catalogs does this supplier have, how far along is
each, and what is on page N".
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class CatalogPageOut(BaseModel):
    page_number: int
    status: str
    skip_reason: str | None = None
    entries_count: int = 0
    width: int | None = None
    height: int | None = None
    thumb_url: str | None = None
    image_url: str | None = None


class CatalogOut(BaseModel):
    """One catalog file of one supplier, with its parsing progress.

    `document_id` is None for the pseudo-catalog "Без привязки": positions
    imported before page-wise parsing have no file behind them, and hiding them
    would be worse than showing them honestly.
    """

    document_id: uuid.UUID | None = None
    file_name: str
    file_size: int
    uploaded_at: datetime | None = None
    supplier_id: uuid.UUID | None = None
    supplier_name: str | None = None
    party_id: uuid.UUID | None = None

    page_count: int = 0
    pages_ready: int = 0
    entries_count: int = 0
    entries_with_image: int = 0

    status: str = "queued"
    current_step: str | None = None
    error: str | None = None
    progress_done: int = 0
    progress_total: int = 0
    cover_url: str | None = None
    download_url: str | None = None
    is_archive: bool = False
    legacy: bool = False
    # Parsing was stopped by a person; the run is resumable but NOT active, and
    # the UI must not poll (or show a spinner) as if it were.
    paused: bool = False


class CatalogListResponse(BaseModel):
    items: list[CatalogOut] = []
    total: int = 0


class CatalogPagesResponse(BaseModel):
    document_id: uuid.UUID
    page_count: int = 0
    items: list[CatalogPageOut] = []


class CatalogEntryOut(BaseModel):
    """A catalog position as the browser shows it: with picture and page."""

    id: uuid.UUID
    part_number: str | None = None
    name: str
    description: str | None = None
    tool_type: str
    diameter_mm: float | None = None
    length_mm: float | None = None
    material: str | None = None
    coating: str | None = None
    price_value: float | None = None
    price_currency: str = "RUB"
    unit: str | None = None

    supplier_id: uuid.UUID | None = None
    supplier_name: str | None = None
    catalog_document_id: uuid.UUID | None = None
    catalog_name: str | None = None
    page_number: int | None = None

    image_url: str | None = None
    thumb_url: str | None = None
    # "crop" — cut out of the page; "page" — a preview of the whole page, shown
    # with a badge so a page preview is never mistaken for a product photo.
    image_kind: str | None = None
    image_bbox: dict | None = None
    score: float | None = None
    legacy: bool = False


class CatalogFacetValue(BaseModel):
    key: str
    label: str
    count: int


class CatalogFacets(BaseModel):
    suppliers: list[CatalogFacetValue] = []
    catalogs: list[CatalogFacetValue] = []
    tool_types: list[CatalogFacetValue] = []
    with_price: int = 0
    with_image: int = 0


class CatalogSearchRequest(BaseModel):
    query: str | None = None
    # Singular fields stay for existing callers; the plural ones are what the
    # UI uses — "искать в этих двух каталогах" and "у этих поставщиков" are the
    # normal questions, and one-at-a-time filters could not express them.
    supplier_id: uuid.UUID | None = None
    party_id: uuid.UUID | None = None
    catalog_document_id: uuid.UUID | None = None
    supplier_ids: list[uuid.UUID] | None = None
    party_ids: list[uuid.UUID] | None = None
    catalog_document_ids: list[uuid.UUID] | None = None
    page_number: int | None = None
    tool_type: str | None = None
    has_price: bool | None = None
    has_image: bool | None = None
    price_min: float | None = None
    price_max: float | None = None
    diameter_min: float | None = None
    diameter_max: float | None = None
    page: int = Field(1, ge=1)
    page_size: int = Field(40, ge=1, le=200)
    include_facets: bool = True


class CatalogSearchResponse(BaseModel):
    items: list[CatalogEntryOut] = []
    total: int = 0
    page: int = 1
    page_size: int = 40
    facets: CatalogFacets | None = None
    # Ready-to-publish workspace block: the agent shows what it found without
    # re-deriving a table from prose (same contract as attach_web_catalog).
    report: dict = {}
    message: str = ""
    # Which retrieval branches contributed — an empty vector branch is a real
    # finding (positions not embedded yet), not something to hide.
    diagnostics: dict = {}


class CatalogSimilarRequest(BaseModel):
    """Analogues of a position — "чем это заменить"."""

    entry_id: uuid.UUID | None = None
    query: str | None = None
    exclude_same_supplier: bool = False
    limit: int = Field(10, ge=1, le=50)


class CatalogSimilarResponse(BaseModel):
    source: CatalogEntryOut | None = None
    items: list[CatalogEntryOut] = []
    message: str = ""
    report: dict = {}
