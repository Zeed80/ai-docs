"""Ad-hoc editable spreadsheets ("листы") — an Excel-like surface.

Sheets live in the workspace and in Postgres (:class:`WorkspaceSheet`); editing
them never touches production tables. Each mutation re-publishes the sheet as a
workspace block of type ``sheet`` so the desktop renders it live.

Skills (capability ``sheets``):
- create / get / list / delete
- patch_cells / add_row / add_column / set_formula
- from_spec — materialise a read-only spec-table into an editable sheet
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.chat_bus import chat_bus
from app.db.models import WorkspaceSheet
from app.db.session import get_db
from app.domain.formula_engine import evaluate_sheet
from app.domain.sheet_templates import list_templates, resolve_template
from app.domain.workspace import (
    delete_workspace_block,
    get_workspace_block,
    upsert_workspace_block,
)

logger = structlog.get_logger()

router = APIRouter()


def _canvas_id(sheet_id: Any) -> str:
    return f"sheet:{sheet_id}"


def _default_columns(n: int = 3) -> list[dict]:
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    return [
        {"key": letters[i], "header": letters[i], "type": "text", "editable": True}
        for i in range(min(n, len(letters)))
    ]


def _layout(sheet: WorkspaceSheet) -> dict:
    layout = sheet.layout if isinstance(sheet.layout, dict) else {}
    merges = layout.get("merges")
    return {**layout, "merges": merges if isinstance(merges, list) else []}


async def _publish(db: AsyncSession, sheet: WorkspaceSheet) -> dict:
    """Evaluate formulas and publish the sheet as a workspace block."""
    columns = list(sheet.columns or [])
    # Computed view: formula cells/columns resolved to values for display.
    computed_rows = evaluate_sheet(columns, list(sheet.rows or []))
    block = {
        "id": _canvas_id(sheet.id),
        "type": "sheet",
        "title": sheet.title,
        "sheet_id": str(sheet.id),
        "columns": columns,
        "rows": computed_rows,
        # Raw (un-evaluated) rows so the editor can show formulas on focus.
        "raw_rows": list(sheet.rows or []),
        "layout": _layout(sheet),
        "source": "workspace.sheet",
    }
    stored = upsert_workspace_block(_canvas_id(sheet.id), block)
    await chat_bus.publish(
        {"type": "workspace.updated", "canvas_id": _canvas_id(sheet.id), "block": stored}
    )
    return block


async def _load(db: AsyncSession, sheet_id: uuid.UUID) -> WorkspaceSheet:
    sheet = (
        await db.execute(select(WorkspaceSheet).where(WorkspaceSheet.id == sheet_id))
    ).scalar_one_or_none()
    if sheet is None:
        raise HTTPException(404, "Лист не найден")
    return sheet


# ── Schemas ──────────────────────────────────────────────────────────────────


class SheetColumn(BaseModel):
    key: str
    header: str | None = None
    type: str = "text"
    width: int | None = None
    formula: str | None = None
    editable: bool = True


class CreateSheetRequest(BaseModel):
    title: str = "Лист"
    columns: list[SheetColumn] | None = None
    rows: list[dict[str, Any]] | None = None
    owner_sub: str | None = None


class CellEdit(BaseModel):
    row: int
    col: str
    value: Any = None


class PatchCellsRequest(BaseModel):
    edits: list[CellEdit]


class AddRowRequest(BaseModel):
    values: dict[str, Any] | None = None
    count: int = 1


class AddColumnRequest(BaseModel):
    key: str
    header: str | None = None
    type: str = "text"
    formula: str | None = None
    before: str | None = None
    after: str | None = None


class SetFormulaRequest(BaseModel):
    # Column-wide formula, or a single cell when `row` is given.
    column: str
    formula: str | None = None
    row: int | None = None


class RenameColumnRequest(BaseModel):
    key: str
    header: str


class MergeCellsRequest(BaseModel):
    start_row: int
    end_row: int
    start_col: str
    end_col: str


class UnmergeCellsRequest(BaseModel):
    merge_id: str | None = None
    row: int | None = None
    col: str | None = None


class FromSpecRequest(BaseModel):
    canvas_id: str
    title: str | None = None
    owner_sub: str | None = None


class FromTemplateRequest(BaseModel):
    template: str
    title: str | None = None
    owner_sub: str | None = None


class SheetResponse(BaseModel):
    status: str
    sheet_id: str
    canvas_id: str
    title: str
    rows: int
    columns: int


def _resp(sheet: WorkspaceSheet, status: str = "ok") -> SheetResponse:
    return SheetResponse(
        status=status,
        sheet_id=str(sheet.id),
        canvas_id=_canvas_id(sheet.id),
        title=sheet.title,
        rows=len(sheet.rows or []),
        columns=len(sheet.columns or []),
    )


def _column_index(columns: list[dict], key: str) -> int | None:
    for idx, col in enumerate(columns):
        if col.get("key") == key:
            return idx
    return None


def _merge_intersects(a: dict, b: dict) -> bool:
    return not (
        int(a["end_row"]) < int(b["start_row"])
        or int(a["start_row"]) > int(b["end_row"])
        or int(a["end_col_index"]) < int(b["start_col_index"])
        or int(a["start_col_index"]) > int(b["end_col_index"])
    )


def _normalise_merge(
    columns: list[dict],
    payload: MergeCellsRequest,
) -> dict:
    start_idx = _column_index(columns, payload.start_col)
    end_idx = _column_index(columns, payload.end_col)
    if start_idx is None:
        raise HTTPException(400, f"Нет столбца «{payload.start_col}»")
    if end_idx is None:
        raise HTTPException(400, f"Нет столбца «{payload.end_col}»")
    start_row = min(payload.start_row, payload.end_row)
    end_row = max(payload.start_row, payload.end_row)
    start_col_index = min(start_idx, end_idx)
    end_col_index = max(start_idx, end_idx)
    if start_row < 0:
        raise HTTPException(400, "Отрицательный индекс строки")
    if start_row == end_row and start_col_index == end_col_index:
        raise HTTPException(400, "Диапазон объединения должен содержать больше одной ячейки")
    return {
        "id": f"merge:{uuid.uuid4()}",
        "start_row": start_row,
        "end_row": end_row,
        "start_col": columns[start_col_index]["key"],
        "end_col": columns[end_col_index]["key"],
        "start_col_index": start_col_index,
        "end_col_index": end_col_index,
    }


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.post("/sheets/create", response_model=SheetResponse)
async def create_sheet(
    payload: CreateSheetRequest, db: AsyncSession = Depends(get_db)
) -> SheetResponse:
    """Skill: sheets.create — Create a new editable sheet."""
    columns = (
        [c.model_dump() for c in payload.columns]
        if payload.columns
        else _default_columns()
    )
    rows = payload.rows if payload.rows is not None else [{} for _ in range(3)]
    sheet = WorkspaceSheet(
        title=payload.title or "Лист",
        owner_sub=payload.owner_sub,
        columns=columns,
        rows=rows,
    )
    db.add(sheet)
    await db.commit()
    await db.refresh(sheet)
    await _publish(db, sheet)
    return _resp(sheet, "created")


@router.get("/sheets")
async def list_sheets(db: AsyncSession = Depends(get_db)) -> dict:
    """Skill: sheets.list — List ad-hoc sheets."""
    rows = (
        await db.execute(
            select(WorkspaceSheet).order_by(WorkspaceSheet.updated_at.desc()).limit(200)
        )
    ).scalars().all()
    return {
        "sheets": [
            {
                "sheet_id": str(s.id),
                "canvas_id": _canvas_id(s.id),
                "title": s.title,
                "rows": len(s.rows or []),
                "columns": len(s.columns or []),
                "updated_at": s.updated_at.isoformat() if s.updated_at else None,
            }
            for s in rows
        ]
    }


@router.get("/sheets/{sheet_id}")
async def get_sheet(
    sheet_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> dict:
    """Skill: sheets.get — Read a sheet (raw + computed rows)."""
    sheet = await _load(db, sheet_id)
    computed = evaluate_sheet(list(sheet.columns or []), list(sheet.rows or []))
    return {
        "sheet_id": str(sheet.id),
        "canvas_id": _canvas_id(sheet.id),
        "title": sheet.title,
        "columns": sheet.columns,
        "rows": computed,
        "raw_rows": sheet.rows,
        "layout": _layout(sheet),
    }


@router.post("/sheets/{sheet_id}/patch-cells", response_model=SheetResponse)
async def patch_cells(
    sheet_id: uuid.UUID, payload: PatchCellsRequest, db: AsyncSession = Depends(get_db)
) -> SheetResponse:
    """Skill: sheets.patch_cells — Set one or more cell values (raw/formula)."""
    sheet = await _load(db, sheet_id)
    rows = list(sheet.rows or [])
    valid_keys = {c["key"] for c in (sheet.columns or [])}
    for edit in payload.edits:
        if edit.col not in valid_keys:
            raise HTTPException(400, f"Нет столбца «{edit.col}»")
        while edit.row >= len(rows):
            rows.append({})
        if edit.row < 0:
            raise HTTPException(400, "Отрицательный индекс строки")
        rows[edit.row] = {**rows[edit.row], edit.col: edit.value}
    sheet.rows = rows
    await db.commit()
    await db.refresh(sheet)
    await _publish(db, sheet)
    return _resp(sheet, "patched")


@router.post("/sheets/{sheet_id}/add-row", response_model=SheetResponse)
async def add_row(
    sheet_id: uuid.UUID, payload: AddRowRequest, db: AsyncSession = Depends(get_db)
) -> SheetResponse:
    """Skill: sheets.add_row — Append blank or pre-filled row(s)."""
    sheet = await _load(db, sheet_id)
    rows = list(sheet.rows or [])
    for _ in range(max(1, payload.count)):
        rows.append(dict(payload.values or {}))
    sheet.rows = rows
    await db.commit()
    await db.refresh(sheet)
    await _publish(db, sheet)
    return _resp(sheet, "row_added")


@router.post("/sheets/{sheet_id}/add-column", response_model=SheetResponse)
async def add_column(
    sheet_id: uuid.UUID, payload: AddColumnRequest, db: AsyncSession = Depends(get_db)
) -> SheetResponse:
    """Skill: sheets.add_column — Add a (optionally computed) column."""
    sheet = await _load(db, sheet_id)
    columns = list(sheet.columns or [])
    if any(c["key"] == payload.key for c in columns):
        raise HTTPException(400, f"Столбец «{payload.key}» уже есть")
    new_col = {
        "key": payload.key,
        "header": payload.header or payload.key,
        "type": payload.type,
        "editable": True,
    }
    if payload.formula:
        new_col["formula"] = payload.formula
    idx = len(columns)
    if payload.before:
        idx = next((i for i, c in enumerate(columns) if c["key"] == payload.before), idx)
    elif payload.after:
        pos = next((i for i, c in enumerate(columns) if c["key"] == payload.after), None)
        if pos is not None:
            idx = pos + 1
    columns.insert(idx, new_col)
    sheet.columns = columns
    await db.commit()
    await db.refresh(sheet)
    await _publish(db, sheet)
    return _resp(sheet, "column_added")


@router.post("/sheets/{sheet_id}/set-formula", response_model=SheetResponse)
async def set_formula(
    sheet_id: uuid.UUID, payload: SetFormulaRequest, db: AsyncSession = Depends(get_db)
) -> SheetResponse:
    """Skill: sheets.set_formula — Set a column-wide or single-cell formula."""
    sheet = await _load(db, sheet_id)
    valid_keys = {c["key"] for c in (sheet.columns or [])}
    if payload.column not in valid_keys:
        raise HTTPException(400, f"Нет столбца «{payload.column}»")
    if payload.row is None:
        # Column-wide formula. Deep-copy: in-place JSON dict mutation isn't
        # tracked by SQLAlchemy and would be reverted on refresh.
        columns = [dict(c) for c in (sheet.columns or [])]
        for c in columns:
            if c["key"] == payload.column:
                if payload.formula:
                    c["formula"] = payload.formula
                else:
                    c.pop("formula", None)
        sheet.columns = columns
    else:
        rows = list(sheet.rows or [])
        while payload.row >= len(rows):
            rows.append({})
        rows[payload.row] = {**rows[payload.row], payload.column: payload.formula}
        sheet.rows = rows
    await db.commit()
    await db.refresh(sheet)
    await _publish(db, sheet)
    return _resp(sheet, "formula_set")


@router.post("/sheets/{sheet_id}/rename-column", response_model=SheetResponse)
async def rename_column(
    sheet_id: uuid.UUID, payload: RenameColumnRequest, db: AsyncSession = Depends(get_db)
) -> SheetResponse:
    """Skill: sheets.rename_column — Rename a column's human-readable header."""
    sheet = await _load(db, sheet_id)
    # Deep-copy so the reassignment is a genuine new value (in-place JSON dict
    # mutation isn't tracked by SQLAlchemy and would be lost on refresh).
    columns = [dict(c) for c in (sheet.columns or [])]
    found = False
    for c in columns:
        if c.get("key") == payload.key:
            c["header"] = payload.header
            found = True
    if not found:
        raise HTTPException(400, f"Нет столбца «{payload.key}»")
    sheet.columns = columns
    await db.commit()
    await db.refresh(sheet)
    await _publish(db, sheet)
    return _resp(sheet, "renamed")


@router.post("/sheets/{sheet_id}/merge-cells", response_model=SheetResponse)
async def merge_cells(
    sheet_id: uuid.UUID,
    payload: MergeCellsRequest,
    db: AsyncSession = Depends(get_db),
) -> SheetResponse:
    """Skill: sheets.merge_cells — Merge a rectangular cell range in a sheet."""
    sheet = await _load(db, sheet_id)
    columns = list(sheet.columns or [])
    merge = _normalise_merge(columns, payload)
    layout = _layout(sheet)
    merges = [dict(m) for m in layout.get("merges", []) if isinstance(m, dict)]
    for existing in merges:
        if _merge_intersects(existing, merge):
            raise HTTPException(400, "Диапазон пересекается с уже объединёнными ячейками")
    layout["merges"] = [*merges, merge]
    sheet.layout = layout
    await db.commit()
    await db.refresh(sheet)
    await _publish(db, sheet)
    return _resp(sheet, "merged")


@router.post("/sheets/{sheet_id}/unmerge-cells", response_model=SheetResponse)
async def unmerge_cells(
    sheet_id: uuid.UUID,
    payload: UnmergeCellsRequest,
    db: AsyncSession = Depends(get_db),
) -> SheetResponse:
    """Skill: sheets.unmerge_cells — Remove a merge by id or by covered cell."""
    sheet = await _load(db, sheet_id)
    columns = list(sheet.columns or [])
    col_idx = _column_index(columns, payload.col) if payload.col else None
    layout = _layout(sheet)
    kept: list[dict] = []
    removed = False
    for merge in [m for m in layout.get("merges", []) if isinstance(m, dict)]:
        match_id = payload.merge_id and merge.get("id") == payload.merge_id
        match_cell = (
            payload.row is not None
            and col_idx is not None
            and int(merge["start_row"]) <= payload.row <= int(merge["end_row"])
            and int(merge["start_col_index"]) <= col_idx <= int(merge["end_col_index"])
        )
        if match_id or match_cell:
            removed = True
            continue
        kept.append(dict(merge))
    if not removed:
        raise HTTPException(400, "Объединение не найдено")
    layout["merges"] = kept
    sheet.layout = layout
    await db.commit()
    await db.refresh(sheet)
    await _publish(db, sheet)
    return _resp(sheet, "unmerged")


@router.delete("/sheets/{sheet_id}", response_model=SheetResponse)
async def delete_sheet(
    sheet_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> SheetResponse:
    """Skill: sheets.delete — Delete a sheet and its workspace block."""
    sheet = await _load(db, sheet_id)
    resp = _resp(sheet, "deleted")
    delete_workspace_block(_canvas_id(sheet.id))
    await db.delete(sheet)
    await db.commit()
    return resp


@router.post("/sheets/from-spec", response_model=SheetResponse)
async def from_spec(
    payload: FromSpecRequest, db: AsyncSession = Depends(get_db)
) -> SheetResponse:
    """Skill: sheets.from_spec — Copy a read-only spec-table into an editable sheet."""
    block = get_workspace_block(payload.canvas_id)
    if not block or block.get("type") != "table":
        raise HTTPException(400, "Источник не является таблицей рабочего стола")
    src_cols = block.get("columns") or []
    columns = [
        {
            "key": c.get("key"),
            "header": c.get("header") or c.get("key"),
            "type": c.get("type", "text"),
            "editable": True,
        }
        for c in src_cols
        if c.get("type") not in ("link", "download", "delete")
    ]
    keys = {c["key"] for c in columns}
    rows = [
        {k: v for k, v in (r or {}).items() if k in keys}
        for r in (block.get("rows") or [])
    ]
    sheet = WorkspaceSheet(
        title=payload.title or f"{block.get('title') or 'Лист'} (правка)",
        owner_sub=payload.owner_sub,
        columns=columns,
        rows=rows,
    )
    db.add(sheet)
    await db.commit()
    await db.refresh(sheet)
    await _publish(db, sheet)
    return _resp(sheet, "created")


@router.get("/sheets/templates/list")
async def sheet_templates() -> dict:
    """Skill: sheets.templates — List ready-made sheet templates."""
    return {"templates": list_templates()}


@router.post("/sheets/from-template", response_model=SheetResponse)
async def from_template(
    payload: FromTemplateRequest, db: AsyncSession = Depends(get_db)
) -> SheetResponse:
    """Skill: sheets.from_template — Create a sheet from a named template."""
    tpl = resolve_template(payload.template)
    if tpl is None:
        names = ", ".join(t["key"] for t in list_templates())
        raise HTTPException(400, f"Нет шаблона «{payload.template}». Доступны: {names}")
    sheet = WorkspaceSheet(
        title=payload.title or tpl["title"],
        owner_sub=payload.owner_sub,
        columns=[dict(c) for c in tpl["columns"]],
        rows=[dict(r) for r in tpl.get("rows", [])],
    )
    db.add(sheet)
    await db.commit()
    await db.refresh(sheet)
    await _publish(db, sheet)
    return _resp(sheet, "created")
