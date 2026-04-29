"""Pydantic schemas for Tables & Export domain."""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


# ── Table Query (table.query) ──────────────────────────────────────────────


class TableColumn(BaseModel):
    key: str
    label: str
    sortable: bool = True
    filterable: bool = True
    data_type: str = "string"  # string, number, date, enum


class TableSort(BaseModel):
    column: str
    direction: str = "desc"  # asc, desc


class TableFilter(BaseModel):
    column: str
    operator: str = "eq"  # eq, ne, gt, lt, gte, lte, contains, in
    value: str | float | list[str] | None = None


class TableQueryRequest(BaseModel):
    table: str = "invoices"  # invoices, documents
    columns: list[str] | None = None
    filters: list[TableFilter] = []
    sort: list[TableSort] = []
    search: str | None = None
    offset: int = 0
    limit: int = Field(50, le=500)


class TableRow(BaseModel):
    id: str
    data: dict


class TableQueryResponse(BaseModel):
    columns: list[TableColumn]
    rows: list[TableRow]
    total: int
    offset: int
    limit: int


# ── SavedView ──────────────────────────────────────────────────────────────


class SavedViewCreate(BaseModel):
    name: str
    table: str = "invoices"
    columns: list[str] | None = None
    filters: list[TableFilter] = []
    sort: list[TableSort] = []
    is_shared: bool = False


class SavedViewOut(BaseModel):
    id: uuid.UUID
    name: str
    table: str
    columns: list[str] | None = None
    filters: list[TableFilter] = []
    sort: list[TableSort] = []
    is_shared: bool = False
    created_by: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


# ── Export ─────────────────────────────────────────────────────────────────


class ExportRequest(BaseModel):
    table: str = "invoices"
    filters: list[TableFilter] = []
    columns: list[str] | None = None
    format: str = "xlsx"  # xlsx, csv


class ExportResponse(BaseModel):
    task_id: str
    status: str = "queued"


class Export1CRequest(BaseModel):
    invoice_ids: list[uuid.UUID] | None = None
    filters: list[TableFilter] = []
    format: str = "commerceml"  # commerceml


class Export1CResponse(BaseModel):
    task_id: str
    status: str = "queued"


# ── Import ─────────────────────────────────────────────────────────────────


class ImportDiffRow(BaseModel):
    row_index: int
    entity_id: uuid.UUID | None = None
    action: str  # create, update, skip
    changes: dict = {}


class ImportDiffResponse(BaseModel):
    import_id: uuid.UUID
    file_name: str
    total_rows: int
    creates: int
    updates: int
    skips: int
    errors: int
    diff: list[ImportDiffRow]


class ImportApplyRequest(BaseModel):
    import_id: uuid.UUID
    confirmed_rows: list[int] | None = None  # None = all
