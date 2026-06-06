"""Workspace API — agent-owned rich output blocks and orchestrated views."""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.chat_bus import chat_bus
from app.db.models import Invoice, InvoiceLine, Party
from app.db.session import get_db
from app.domain.workspace import (
    clear_workspace_blocks,
    delete_workspace_block,
    get_workspace_block,
    list_workspace_blocks,
    upsert_workspace_block,
)
from app.formatting import format_money, format_number

router = APIRouter()

# Agent-published tables must show the FULL data set — the user asked for "all".
# `limit` on these tools is a safety cap only (protects against a runaway query),
# never a page size, so a small limit the model might pass can't drop rows.
_WORKSPACE_MAX_ROWS = 10000


class WorkspaceBlockResponse(BaseModel):
    items: list[dict[str, Any]]
    total: int


class WorkspaceInvoiceTableRequest(BaseModel):
    canvas_id: str = "agent:invoice-list"
    limit: int = 5000
    include_delete_actions: bool = True


class WorkspaceInvoiceItemsTableRequest(BaseModel):
    canvas_id: str = "agent:invoice-items"
    limit: int = 10000
    include_invoice_actions: bool = True
    supplier_query: str | None = None


class WorkspaceInvoiceItemsGroupedTableRequest(BaseModel):
    canvas_id: str = "agent:invoice-items-grouped"
    limit: int = 5000
    include_supplier: bool = False


class WorkspaceInvoiceItemsBySupplierTableRequest(BaseModel):
    canvas_id: str = "agent:invoice-items-by-supplier"
    limit: int = 10000


class WorkspaceInvoicePivotRequest(BaseModel):
    # Generic group-by for invoice items. group_by picks the grouping dimension;
    # the table always covers ALL data (SQL-side aggregation), so it works for any
    # phrasing instead of relying on a handful of pre-baked layouts.
    group_by: str = "supplier"  # supplier | invoice | item | month | currency | status
    # Optional column spec: [{"header": "...", "expr": "<catalog key>"}]. Lets the
    # caller choose AND name columns (e.g. add an ИНН column) while the data stays
    # complete. When omitted, a sensible default set is used.
    columns: list[dict[str, Any]] | None = None
    canvas_id: str = "agent:invoice-pivot"


class WorkspaceToolResponse(BaseModel):
    status: str
    canvas_id: str
    total: int
    shown: int
    message: str
    filters: dict[str, Any] = {}


class WorkspaceVerifyBlockRequest(BaseModel):
    canvas_id: str


class WorkspaceVerifyBlockResponse(BaseModel):
    exists: bool
    canvas_id: str
    block_type: str | None = None
    row_count: int | None = None
    updated_at: str | None = None


@router.get("/blocks", response_model=WorkspaceBlockResponse)
async def get_workspace_blocks() -> WorkspaceBlockResponse:
    """List current agent Workspace blocks."""
    items = list_workspace_blocks()
    return WorkspaceBlockResponse(items=items, total=len(items))


@router.get("/blocks/{block_id}", response_model=dict[str, Any] | None)
async def get_workspace_block_endpoint(block_id: str) -> dict[str, Any] | None:
    """Get one Workspace block by ID."""
    return get_workspace_block(block_id)


@router.post("/agent/verify-block", response_model=WorkspaceVerifyBlockResponse)
async def verify_workspace_block(
    payload: WorkspaceVerifyBlockRequest,
) -> WorkspaceVerifyBlockResponse:
    """Skill: workspace.verify_block — Verify that a block exists on the Workspace."""
    block = get_workspace_block(payload.canvas_id)
    rows = block.get("rows") if isinstance(block, dict) else None
    return WorkspaceVerifyBlockResponse(
        exists=block is not None,
        canvas_id=payload.canvas_id,
        block_type=(
            str(block.get("type"))
            if isinstance(block, dict) and block.get("type")
            else None
        ),
        row_count=len(rows) if isinstance(rows, list) else None,
        updated_at=(
            str(block.get("updated_at"))
            if isinstance(block, dict) and block.get("updated_at")
            else None
        ),
    )


@router.delete("/blocks/{block_id}", status_code=200)
async def delete_workspace_block_endpoint(block_id: str) -> dict[str, Any]:
    """Delete a Workspace block."""
    deleted = delete_workspace_block(block_id)
    await chat_bus.publish({"type": "workspace.updated"})
    return {"deleted": deleted, "block_id": block_id}


@router.delete("/blocks", status_code=200)
async def clear_workspace_blocks_endpoint() -> dict[str, Any]:
    """Clear all Workspace blocks."""
    clear_workspace_blocks()
    await chat_bus.publish({"type": "workspace.updated"})
    return {"deleted": "all"}


@router.post("/agent/invoices/table", response_model=WorkspaceToolResponse)
async def publish_invoice_table(
    payload: WorkspaceInvoiceTableRequest,
    db: AsyncSession = Depends(get_db),
) -> WorkspaceToolResponse:
    """Skill: workspace.invoice_table — Build and publish the full invoice table.

    This is an orchestrator tool: it queries SQL directly, prepares the stable
    Workspace table schema, adds supported file actions, stores the block, and
    notifies active clients to switch to the existing Workspace section.
    """
    total = (
        await db.execute(select(func.count()).select_from(Invoice))
    ).scalar_one()
    result = await db.execute(
        select(Invoice)
        .options(selectinload(Invoice.supplier))
        .order_by(Invoice.created_at.desc())
        .limit(_WORKSPACE_MAX_ROWS)
    )
    invoices = result.scalars().all()
    rows = [
        _invoice_workspace_row(inv, index, include_delete=payload.include_delete_actions)
        for index, inv in enumerate(invoices, start=1)
    ]
    block = {
        "id": payload.canvas_id,
        "type": "table",
        "title": f"Счета: полный список ({total})",
        "columns": _invoice_columns(include_delete=payload.include_delete_actions),
        "rows": rows,
        "source": "workspace.invoice_table",
    }
    stored = upsert_workspace_block(payload.canvas_id, block)
    await chat_bus.publish({
        "type": "workspace.updated",
        "canvas_id": payload.canvas_id,
        "block": stored,
    })
    return WorkspaceToolResponse(
        status="published",
        canvas_id=payload.canvas_id,
        total=total,
        shown=len(rows),
        message=f"Открыл на Рабочем столе таблицу со счетами: {len(rows)} из {total}.",
        filters={},
    )


@router.post("/agent/invoices/items-table", response_model=WorkspaceToolResponse)
async def publish_invoice_items_table(
    payload: WorkspaceInvoiceItemsTableRequest,
    db: AsyncSession = Depends(get_db),
) -> WorkspaceToolResponse:
    """Skill: workspace.invoice_items_table — Build and publish invoice line items.

    This orchestrator prepares the table template, fills it from SQL invoice
    lines, stores the result in the existing Workspace, and notifies clients.
    """
    await _publish_workspace_status("Готовлю шаблон таблицы товаров по счетам")
    columns = _invoice_item_columns(include_invoice_actions=payload.include_invoice_actions)

    await _publish_workspace_status("Заполняю таблицу строками счетов из БД")
    supplier_filter = (payload.supplier_query or "").strip()
    count_stmt = select(func.count()).select_from(InvoiceLine)
    rows_stmt = (
        select(InvoiceLine, Invoice)
        .join(Invoice, InvoiceLine.invoice_id == Invoice.id)
        .options(selectinload(Invoice.supplier))
        .order_by(Invoice.created_at.desc(), InvoiceLine.line_number.asc())
        .limit(_WORKSPACE_MAX_ROWS)
    )
    if supplier_filter:
        pattern = f"%{supplier_filter}%"
        count_stmt = (
            count_stmt
            .join(Invoice, InvoiceLine.invoice_id == Invoice.id)
            .join(Party, Invoice.supplier_id == Party.id)
            .where(Party.name.ilike(pattern))
        )
        rows_stmt = rows_stmt.join(Party, Invoice.supplier_id == Party.id).where(
            Party.name.ilike(pattern)
        )
    total = (
        await db.execute(count_stmt)
    ).scalar_one()
    result = await db.execute(rows_stmt)
    rows = [
        _invoice_item_workspace_row(
            line,
            invoice,
            index,
            include_invoice_actions=payload.include_invoice_actions,
        )
        for index, (line, invoice) in enumerate(result.all(), start=1)
    ]

    await _publish_workspace_status("Публикую таблицу на Рабочий стол")
    block = {
        "id": payload.canvas_id,
        "type": "table",
        "title": (
            f"Товары по счетам: {supplier_filter} ({total})"
            if supplier_filter
            else f"Товары по счетам ({total})"
        ),
        "columns": columns,
        "rows": rows,
        "source": "workspace.invoice_items_table",
        "filters": {"supplier_query": supplier_filter} if supplier_filter else {},
    }
    stored = upsert_workspace_block(payload.canvas_id, block)
    await chat_bus.publish({
        "type": "workspace.updated",
        "canvas_id": payload.canvas_id,
        "block": stored,
    })
    return WorkspaceToolResponse(
        status="published",
        canvas_id=payload.canvas_id,
        total=total,
        shown=len(rows),
        message=(
            f"Оставил на Рабочем столе товары поставщика {supplier_filter}: "
            f"{len(rows)} из {total}."
            if supplier_filter
            else f"Открыл на Рабочем столе таблицу товаров по счетам: {len(rows)} из {total}."
        ),
        filters={"supplier_query": supplier_filter} if supplier_filter else {},
    )


@router.post("/agent/invoices/items-grouped-table", response_model=WorkspaceToolResponse)
async def publish_invoice_items_grouped_table(
    payload: WorkspaceInvoiceItemsGroupedTableRequest,
    db: AsyncSession = Depends(get_db),
) -> WorkspaceToolResponse:
    """Skill: workspace.invoice_items_grouped_table — Group invoice items by invoice."""
    await _publish_workspace_status("Готовлю шаблон: товары сгруппированы по счетам")
    total = (
        await db.execute(select(func.count()).select_from(Invoice))
    ).scalar_one()
    await _publish_workspace_status("Загружаю счета и строки товаров из БД")
    result = await db.execute(
        select(Invoice)
        .options(selectinload(Invoice.lines), selectinload(Invoice.supplier))
        .order_by(Invoice.created_at.desc())
        .limit(_WORKSPACE_MAX_ROWS)
    )
    invoices = result.scalars().all()
    rows = [
        _invoice_items_grouped_workspace_row(
            invoice,
            index,
            include_supplier=payload.include_supplier,
        )
        for index, invoice in enumerate(invoices, start=1)
    ]

    await _publish_workspace_status("Публикую сгруппированную таблицу на Рабочий стол")
    block = {
        "id": payload.canvas_id,
        "type": "table",
        "title": f"Товары, сгруппированные по счетам ({total})",
        "columns": _invoice_items_grouped_columns(include_supplier=payload.include_supplier),
        "rows": rows,
        "source": "workspace.invoice_items_grouped_table",
        "source_agent_role": "invoice_specialist",
        "audit_status": "pending",
    }
    stored = upsert_workspace_block(payload.canvas_id, block)
    await chat_bus.publish({
        "type": "workspace.updated",
        "canvas_id": payload.canvas_id,
        "block": stored,
    })
    return WorkspaceToolResponse(
        status="published",
        canvas_id=payload.canvas_id,
        total=total,
        shown=len(rows),
        message=(
            "Открыл на Рабочем столе таблицу товаров, сгруппированных по счетам: "
            f"{len(rows)} из {total}."
        ),
        filters={},
    )


@router.post("/agent/invoices/items-by-supplier-table", response_model=WorkspaceToolResponse)
async def publish_invoice_items_by_supplier_table(
    payload: WorkspaceInvoiceItemsBySupplierTableRequest,
    db: AsyncSession = Depends(get_db),
) -> WorkspaceToolResponse:
    """Skill: workspace.invoice_items_by_supplier_table — Group invoice items by supplier."""
    await _publish_workspace_status("Готовлю шаблон: товары сгруппированы по поставщикам")
    await _publish_workspace_status("Загружаю поставщиков, счета и строки товаров из БД")

    # Fetch ALL line items (safety-capped) — the grouping must cover every
    # supplier. payload.limit bounds the number of OUTPUT supplier rows, NOT the
    # pre-grouping line fetch: limiting lines here (ordered by invoice recency)
    # silently dropped whole suppliers whose invoices weren't the most recent.
    result = await db.execute(
        select(InvoiceLine, Invoice)
        .join(Invoice, InvoiceLine.invoice_id == Invoice.id)
        .options(selectinload(Invoice.supplier))
        .order_by(Invoice.created_at.desc(), InvoiceLine.line_number.asc())
        .limit(_WORKSPACE_MAX_ROWS)
    )
    groups: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "supplier": "",
            "invoice_ids": set(),
            "items": [],
            "total": Decimal("0"),
        }
    )
    total_lines = 0
    for line, invoice in result.all():
        total_lines += 1
        supplier_name = invoice.supplier.name if invoice.supplier else "Без поставщика"
        group = groups[supplier_name]
        group["supplier"] = supplier_name
        group["invoice_ids"].add(str(invoice.id))
        group["items"].append(_format_supplier_grouped_item_line(line, invoice))
        if line.amount is not None:
            group["total"] += Decimal(str(line.amount))

    # Show ALL suppliers — never drop groups (the user asked for "all").
    ordered_groups = sorted(
        groups.values(), key=lambda item: str(item["supplier"]).lower()
    )
    rows = [
        _invoice_items_by_supplier_workspace_row(group, index)
        for index, group in enumerate(ordered_groups, start=1)
    ]

    await _publish_workspace_status("Публикую таблицу товаров по поставщикам на Рабочий стол")
    block = {
        "id": payload.canvas_id,
        "type": "table",
        "title": f"Товары, сгруппированные по поставщикам ({len(rows)})",
        "columns": _invoice_items_by_supplier_columns(),
        "rows": rows,
        "source": "workspace.invoice_items_by_supplier_table",
        "source_agent_role": "invoice_specialist",
        "audit_status": "pending",
    }
    stored = upsert_workspace_block(payload.canvas_id, block)
    await chat_bus.publish({
        "type": "workspace.updated",
        "canvas_id": payload.canvas_id,
        "block": stored,
    })
    return WorkspaceToolResponse(
        status="published",
        canvas_id=payload.canvas_id,
        total=len(rows),
        shown=len(rows),
        message=(
            "Открыл на Рабочем столе таблицу товаров, сгруппированных по поставщикам: "
            f"{len(rows)} поставщиков, {total_lines} строк товаров."
        ),
        filters={},
    )


_PIVOT_DIMENSIONS: dict[str, tuple[str, Any]] = {
    "supplier": ("Поставщик", lambda line, inv: inv.supplier.name if inv.supplier else "Без поставщика"),
    "invoice": ("Счёт", lambda line, inv: inv.invoice_number or f"б/н {str(inv.id)[:8]}"),
    "item": ("Товар", lambda line, inv: (line.description or line.sku or "—").strip() or "—"),
    "currency": ("Валюта", lambda line, inv: inv.currency or "RUB"),
    "status": ("Статус", lambda line, inv: inv.status.value if hasattr(inv.status, "value") else str(inv.status)),
    "month": ("Месяц", lambda line, inv: inv.invoice_date.strftime("%Y-%m") if inv.invoice_date else "—"),
}


def _avg_money(amounts: list[float]) -> Any:
    return _format_money(sum(amounts) / len(amounts)) if amounts else ""


# Column catalog: expr → (default header, cell type, value function over the
# per-group context). Lets the caller pick AND name any subset of columns while
# the data stays complete. Add an entry here to expose a new column.
_PIVOT_COLUMN_CATALOG: dict[str, tuple[str, str, Any]] = {
    "group": ("Группа", "text", lambda g: g["key"]),
    "supplier": ("Поставщик", "text", lambda g: g["supplier"].name if g["supplier"] else ""),
    "supplier_inn": ("ИНН", "text", lambda g: (g["supplier"].inn or "") if g["supplier"] else ""),
    "supplier_kpp": ("КПП", "text", lambda g: (g["supplier"].kpp or "") if g["supplier"] else ""),
    "items": ("Товары", "text", lambda g: "\n".join(g["items"])),
    "item_names": ("Наименования", "text", lambda g: "\n".join(dict.fromkeys(g["item_names"]))),
    "item_count": ("Позиций", "number", lambda g: len(g["items"])),
    "invoice_count": ("Счетов", "number", lambda g: len(g["invoice_ids"])),
    "invoice_numbers": ("Счета", "text", lambda g: ", ".join(dict.fromkeys(g["invoice_numbers"]))),
    "total_amount": ("Сумма", "number", lambda g: _format_money(g["total"])),
    "avg_amount": ("Средняя сумма", "number", lambda g: _avg_money(g["amounts"])),
    "min_amount": ("Мин. сумма", "number", lambda g: _format_money(min(g["amounts"])) if g["amounts"] else ""),
    "max_amount": ("Макс. сумма", "number", lambda g: _format_money(max(g["amounts"])) if g["amounts"] else ""),
    "quantity_total": ("Кол-во", "number", lambda g: _format_number(g["quantity"])),
    "currencies": ("Валюта", "text", lambda g: ", ".join(sorted(g["currencies"]))),
    "statuses": ("Статус", "text", lambda g: ", ".join(sorted(g["statuses"]))),
    "first_date": ("Первый счёт", "date", lambda g: _format_date(min(g["dates"])) if g["dates"] else ""),
    "last_date": ("Последний счёт", "date", lambda g: _format_date(max(g["dates"])) if g["dates"] else ""),
}
# Generous synonym map (EN + RU) so the model never has to guess exact keys.
_PIVOT_COLUMN_ALIASES = {
    "sum": "total_amount", "amount": "total_amount", "total": "total_amount",
    "сумма": "total_amount", "итого": "total_amount", "стоимость": "total_amount",
    "inn": "supplier_inn", "инн": "supplier_inn",
    "kpp": "supplier_kpp", "кпп": "supplier_kpp",
    "supplier_name": "supplier", "поставщик": "supplier", "контрагент": "supplier",
    "vendor": "supplier",
    "currency": "currencies", "валюта": "currencies",
    "status": "statuses", "статус": "statuses",
    "count": "invoice_count", "invoices": "invoice_numbers",
    "счета": "invoice_numbers", "счетов": "invoice_count", "кол_во_счетов": "invoice_count",
    "quantity": "quantity_total", "qty": "quantity_total", "количество": "quantity_total",
    "кол_во": "quantity_total", "колво": "quantity_total",
    "name": "item_names", "names": "item_names", "item": "items", "item_name": "items",
    "product": "items", "products": "items", "товар": "items", "товары": "items",
    "наименование": "items", "наименования": "items", "позиции": "items",
    "price": "avg_amount", "цена": "avg_amount", "unit_price": "avg_amount",
    "средняя": "avg_amount", "avg": "avg_amount",
    "positions": "item_count", "item_count": "item_count", "позиций": "item_count",
    "date": "last_date", "дата": "last_date",
}


def _resolve_pivot_columns(
    columns: list[dict[str, Any]] | None, dim_header: str
) -> list[tuple[str, str, Any, str]]:
    """Return [(header, type, value_fn, expr)] for the requested spec.

    Tolerant of imperfect keys from the model (normalise + synonyms) and ALWAYS
    keeps the grouping dimension as the first column so a row is never anonymous,
    even if the caller forgot it. Unknown exprs are dropped, not fatal.
    """
    resolved: list[tuple[str, str, Any, str]] = []
    seen_exprs: set[str] = set()
    for col in columns or []:
        raw = str(col.get("expr") or col.get("key") or col.get("header") or "").strip().lower()
        norm = raw.replace(" ", "_").replace("-", "_")
        expr = _PIVOT_COLUMN_ALIASES.get(norm, norm)
        entry = _PIVOT_COLUMN_CATALOG.get(expr)
        if not entry or expr in seen_exprs:
            continue
        header = str(col.get("header") or "").strip() or (dim_header if expr == "group" else entry[0])
        resolved.append((header, entry[1], entry[2], expr))
        seen_exprs.add(expr)

    g = _PIVOT_COLUMN_CATALOG
    if not resolved:
        # No usable spec → sensible default set.
        return [
            (dim_header, "text", g["group"][2], "group"),
            ("Счетов", "number", g["invoice_count"][2], "invoice_count"),
            ("Товары", "text", g["items"][2], "items"),
            ("Сумма", "number", g["total_amount"][2], "total_amount"),
        ]
    # Guarantee the grouping dimension is present (as the leading column) so the
    # table always shows WHAT each row is, regardless of the caller's columns.
    if not ({"group", "supplier"} & seen_exprs):
        resolved.insert(0, (dim_header, "text", g["group"][2], "group"))
    return resolved


@router.post("/agent/invoices/pivot-table", response_model=WorkspaceToolResponse)
async def publish_invoice_pivot_table(
    payload: WorkspaceInvoicePivotRequest,
    db: AsyncSession = Depends(get_db),
) -> WorkspaceToolResponse:
    """Skill: workspace.invoice_pivot — Group invoice items by ANY dimension,
    with a CHOSEN set of columns.

    group_by selects the grouping dimension; `columns` (optional) selects and
    names the output columns from a catalog (supplier, supplier_inn, items,
    item_count, invoice_count, total_amount, avg_amount, currencies, …). Always
    aggregated over ALL line items in SQL/code — complete and flexible, so it
    replaces LLM-hand-built tables for any grouped/aggregated request.
    """
    _dim_aliases = {
        "supplier_name": "supplier", "поставщик": "supplier", "контрагент": "supplier",
        "vendor": "supplier", "invoice_number": "invoice", "счёт": "invoice", "счет": "invoice",
        "item_name": "item", "товар": "item", "product": "item",
        "месяц": "month", "валюта": "currency", "статус": "status",
    }
    gb = str(payload.group_by or "supplier").strip().lower().replace(" ", "_")
    gb = _dim_aliases.get(gb, gb)
    header, key_fn = _PIVOT_DIMENSIONS.get(gb, _PIVOT_DIMENSIONS["supplier"])
    col_specs = _resolve_pivot_columns(payload.columns, header)
    await _publish_workspace_status(f"Группирую товары по: {header}")

    result = await db.execute(
        select(InvoiceLine, Invoice)
        .join(Invoice, InvoiceLine.invoice_id == Invoice.id)
        .options(selectinload(Invoice.supplier))
        .order_by(Invoice.created_at.desc(), InvoiceLine.line_number.asc())
        .limit(_WORKSPACE_MAX_ROWS)
    )

    def _new_group() -> dict[str, Any]:
        return {
            "key": "", "items": [], "item_names": [], "invoice_ids": set(),
            "invoice_numbers": [], "total": Decimal("0"), "amounts": [],
            "quantity": Decimal("0"), "currencies": set(), "statuses": set(),
            "dates": [], "supplier": None,
        }

    groups: dict[str, dict[str, Any]] = defaultdict(_new_group)
    total_lines = 0
    for line, invoice in result.all():
        total_lines += 1
        key = str(key_fn(line, invoice))
        g = groups[key]
        g["key"] = key
        g["items"].append(_format_supplier_grouped_item_line(line, invoice))
        g["item_names"].append((line.description or line.sku or "—").strip() or "—")
        g["invoice_ids"].add(str(invoice.id))
        g["invoice_numbers"].append(invoice.invoice_number or f"б/н {str(invoice.id)[:8]}")
        if line.amount is not None:
            amt = Decimal(str(line.amount))
            g["total"] += amt
            g["amounts"].append(float(line.amount))
        if line.quantity is not None:
            g["quantity"] += Decimal(str(line.quantity))
        g["currencies"].add(invoice.currency or "RUB")
        g["statuses"].add(invoice.status.value if hasattr(invoice.status, "value") else str(invoice.status))
        if invoice.invoice_date:
            g["dates"].append(invoice.invoice_date)
        if g["supplier"] is None and invoice.supplier is not None:
            g["supplier"] = invoice.supplier

    ordered = sorted(groups.values(), key=lambda it: str(it["key"]).lower())
    columns = [{"key": "index", "header": "№", "type": "number", "width": 56}]
    for i, (col_header, col_type, _fn, _expr) in enumerate(col_specs):
        columns.append({"key": f"c{i}", "header": col_header, "type": col_type})
    rows = []
    for idx, g in enumerate(ordered, start=1):
        row: dict[str, Any] = {"index": idx}
        for i, (_h, _t, fn, _expr) in enumerate(col_specs):
            row[f"c{i}"] = fn(g)
        rows.append(row)

    await _publish_workspace_status("Публикую сгруппированную таблицу на Рабочий стол")
    block = {
        "id": payload.canvas_id,
        "type": "table",
        "title": f"Товары по: {header} ({len(rows)})",
        "columns": columns,
        "rows": rows,
        "source": "workspace.invoice_pivot",
        "source_agent_role": "invoice_specialist",
        "audit_status": "pending",
    }
    stored = upsert_workspace_block(payload.canvas_id, block)
    await chat_bus.publish(
        {"type": "workspace.updated", "canvas_id": payload.canvas_id, "block": stored}
    )
    return WorkspaceToolResponse(
        status="published",
        canvas_id=payload.canvas_id,
        total=len(rows),
        shown=len(rows),
        message=(
            f"Открыл таблицу: товары по «{header}» — {len(rows)} групп, "
            f"{total_lines} строк товаров, колонок: {len(col_specs)}."
        ),
        filters={},
    )


class WorkspaceGeneralBlockRequest(BaseModel):
    canvas_id: str = "agent:custom"
    title: str
    columns: list[dict[str, Any]]
    rows: list[dict[str, Any]]
    block_type: str = "table"


class WorkspaceSqlTableRequest(BaseModel):
    canvas_id: str = "agent:sql-table"
    task: str
    limit: int = 200


@router.post("/agent/generated/sql-table", response_model=WorkspaceToolResponse)
async def publish_sql_table(
    payload: WorkspaceSqlTableRequest,
) -> WorkspaceToolResponse:
    """Skill: workspace.sql_table — Build and publish a table using SQL-first pipeline.

    LLM generates validated SQL from task description, result comes from real DB —
    no hallucination. Use this instead of workspace.general when the agent needs
    to display real data (invoices, suppliers, anomalies, etc.).
    """
    from app.ai.table_sql_pipeline import build_table_from_task
    from app.ai.ollama_client import reasoning_generate

    async def _generate(prompt: str, system: str) -> str:
        return await reasoning_generate(prompt, system=system, format_json=False)

    try:
        block = await build_table_from_task(
            task=payload.task,
            limit=payload.limit,
            generate_fn=_generate,
        )
    except Exception as exc:
        return WorkspaceToolResponse(
            status="error",
            canvas_id=payload.canvas_id,
            total=0,
            shown=0,
            message=f"Не удалось построить таблицу: {exc}",
        )

    if block.get("status") == "error":
        return WorkspaceToolResponse(
            status="error",
            canvas_id=payload.canvas_id,
            total=0,
            shown=0,
            message=block.get("message", "Ошибка построения таблицы"),
        )

    # build_table_from_task returns {"status": "ok", "data": <canvas_block>, "sql": ...}
    canvas_block = block.get("data", block)
    canvas_id = payload.canvas_id
    canvas_block["id"] = canvas_id
    stored = upsert_workspace_block(canvas_id, canvas_block)
    await chat_bus.publish({
        "type": "workspace.updated",
        "canvas_id": canvas_id,
        "block": stored,
    })
    row_count = len(canvas_block.get("rows", []))
    title = canvas_block.get("title", "Таблица")
    return WorkspaceToolResponse(
        status="published",
        canvas_id=canvas_id,
        total=row_count,
        shown=row_count,
        message=f"Опубликовал таблицу «{title}»: {row_count} строк. SQL: {block.get('sql', '')[:120]}",
        filters={},
    )


@router.post("/agent/generated/general", response_model=WorkspaceToolResponse)
async def publish_general_block(
    payload: WorkspaceGeneralBlockRequest,
) -> WorkspaceToolResponse:
    """Skill: workspace.general — Publish any custom table or block to the Workspace.

    Agent builds columns and rows from fetched data, then publishes here.
    Re-publishing with the same canvas_id updates the existing block.
    """
    block = {
        "id": payload.canvas_id,
        "type": payload.block_type,
        "title": payload.title,
        "columns": payload.columns,
        "rows": payload.rows,
        "source": "workspace.general",
    }
    stored = upsert_workspace_block(payload.canvas_id, block)
    await chat_bus.publish({
        "type": "workspace.updated",
        "canvas_id": payload.canvas_id,
        "block": stored,
    })
    return WorkspaceToolResponse(
        status="published",
        canvas_id=payload.canvas_id,
        total=len(payload.rows),
        shown=len(payload.rows),
        message=f"Опубликовал таблицу «{payload.title}»: {len(payload.rows)} строк.",
        filters={},
    )


def _invoice_columns(*, include_delete: bool) -> list[dict[str, Any]]:
    columns: list[dict[str, Any]] = [
        {"key": "index", "header": "№", "type": "number", "width": 56},
        {"key": "invoice_number", "header": "Номер счета", "type": "text"},
        {"key": "invoice_date", "header": "Дата", "type": "date"},
        {"key": "supplier", "header": "Поставщик", "type": "text"},
        {"key": "total_amount", "header": "Сумма", "type": "number"},
        {"key": "currency", "header": "Валюта", "type": "text", "width": 72},
        {"key": "status", "header": "Статус", "type": "text"},
        {"key": "document_download", "header": "Документ", "type": "download"},
    ]
    if include_delete:
        columns.extend([
            {"key": "invoice_delete", "header": "Удалить счет", "type": "delete"},
            {"key": "document_delete", "header": "Удалить документ", "type": "delete"},
        ])
    return columns


def _invoice_items_grouped_columns(*, include_supplier: bool = False) -> list[dict[str, Any]]:
    columns: list[dict[str, Any]] = [
        {"key": "index", "header": "№", "type": "number", "width": 56},
        {"key": "invoice_number", "header": "Номер счета", "type": "text"},
        {"key": "invoice_date", "header": "Дата счета", "type": "date"},
        {"key": "items", "header": "Перечень товаров", "type": "text"},
        {"key": "total_amount", "header": "Общая сумма", "type": "number"},
        {"key": "notes", "header": "Примечание", "type": "text"},
    ]
    if include_supplier:
        columns.insert(1, {"key": "supplier", "header": "Поставщик", "type": "text"})
    return columns


def _invoice_items_by_supplier_columns() -> list[dict[str, Any]]:
    return [
        {"key": "index", "header": "№", "type": "number", "width": 56},
        {"key": "supplier", "header": "Поставщик", "type": "text"},
        {"key": "invoice_count", "header": "Счетов", "type": "number", "width": 80},
        {"key": "items", "header": "Товары по счетам", "type": "text"},
        {"key": "total_amount", "header": "Сумма товаров", "type": "number"},
    ]


def _invoice_item_columns(*, include_invoice_actions: bool) -> list[dict[str, Any]]:
    columns: list[dict[str, Any]] = [
        {"key": "index", "header": "№", "type": "number", "width": 56},
        {"key": "invoice_number", "header": "Счет", "type": "text"},
        {"key": "invoice_date", "header": "Дата счета", "type": "date"},
        {"key": "supplier", "header": "Поставщик", "type": "text"},
        {"key": "line_number", "header": "Строка", "type": "number", "width": 72},
        {"key": "sku", "header": "Артикул/SKU", "type": "text"},
        {"key": "description", "header": "Наименование товара", "type": "text"},
        {"key": "quantity", "header": "Количество", "type": "number"},
        {"key": "unit", "header": "Ед.", "type": "text", "width": 64},
        {"key": "unit_price", "header": "Цена", "type": "number"},
        {"key": "amount", "header": "Сумма строки", "type": "number"},
        {"key": "tax_rate", "header": "НДС %", "type": "number"},
        {"key": "tax_amount", "header": "НДС", "type": "number"},
        {"key": "currency", "header": "Валюта", "type": "text", "width": 72},
        {"key": "invoice_status", "header": "Статус счета", "type": "text"},
        {"key": "document_download", "header": "Документ", "type": "download"},
    ]
    if include_invoice_actions:
        columns.append({"key": "invoice_delete", "header": "Удалить счет", "type": "delete"})
    return columns


def _invoice_workspace_row(
    invoice: Invoice,
    index: int,
    *,
    include_delete: bool,
) -> dict[str, Any]:
    document_id = str(invoice.document_id)
    invoice_id = str(invoice.id)
    row: dict[str, Any] = {
        "index": index,
        "id": invoice_id,
        "document_id": document_id,
        "invoice_number": invoice.invoice_number or "",
        "invoice_date": _format_date(invoice.invoice_date),
        "supplier": invoice.supplier.name if invoice.supplier else "",
        "total_amount": _format_money(invoice.total_amount),
        "currency": invoice.currency or "RUB",
        "status": invoice.status.value if hasattr(invoice.status, "value") else str(invoice.status),
        "document_download": {
            "href": f"/api/documents/{document_id}/download",
            "label": "Скачать",
        },
    }
    if include_delete:
        title = invoice.invoice_number or invoice_id
        row["invoice_delete"] = {
            "href": f"/api/invoices/{invoice_id}",
            "label": "Удалить",
            "confirm": f"Удалить счет {title}?",
            "method": "DELETE",
        }
        row["document_delete"] = {
            "href": f"/api/documents/{document_id}",
            "label": "Удалить",
            "confirm": f"Удалить документ счета {title}?",
            "method": "DELETE",
        }
    return row


def _invoice_items_grouped_workspace_row(
    invoice: Invoice,
    index: int,
    *,
    include_supplier: bool = False,
) -> dict[str, Any]:
    lines = sorted(invoice.lines, key=lambda line: line.line_number)
    item_lines = [_format_grouped_item_line(line) for line in lines]
    row = {
        "index": index,
        "invoice_id": str(invoice.id),
        "document_id": str(invoice.document_id),
        "invoice_number": invoice.invoice_number or "",
        "invoice_date": _format_date(invoice.invoice_date),
        "items": "\n".join(line for line in item_lines if line),
        "total_amount": _format_money(invoice.total_amount),
        "notes": invoice.notes or "",
    }
    if include_supplier:
        row["supplier"] = invoice.supplier.name if invoice.supplier else ""
    return row


def _format_grouped_item_line(line: InvoiceLine) -> str:
    description = (line.description or line.sku or "").strip()
    quantity = _format_number(line.quantity)
    unit = (line.unit or "").strip()
    if quantity and unit:
        suffix = f" — {quantity} {unit}"
    elif quantity:
        suffix = f" — {quantity}"
    else:
        suffix = ""
    return f"{description}{suffix}".strip()


def _format_supplier_grouped_item_line(line: InvoiceLine, invoice: Invoice) -> str:
    invoice_number = invoice.invoice_number or str(invoice.id)
    invoice_date = _format_date(invoice.invoice_date)
    description = (line.description or line.sku or "").strip()
    quantity = _format_number(line.quantity)
    unit = (line.unit or "").strip()
    amount = _format_money(line.amount)
    parts = [f"счет {invoice_number}"]
    if invoice_date:
        parts.append(f"от {invoice_date}")
    prefix = " ".join(parts)
    quantity_text = f" — {quantity} {unit}".rstrip() if quantity else ""
    amount_text = f"; сумма {amount}" if amount else ""
    return f"{prefix}: {description}{quantity_text}{amount_text}".strip()


def _invoice_items_by_supplier_workspace_row(
    group: dict[str, Any],
    index: int,
) -> dict[str, Any]:
    invoice_ids = group.get("invoice_ids")
    items = group.get("items")
    return {
        "index": index,
        "supplier": str(group.get("supplier") or ""),
        "invoice_count": len(invoice_ids) if isinstance(invoice_ids, set) else 0,
        "items": "\n".join(item for item in items if item) if isinstance(items, list) else "",
        "total_amount": _format_money(group.get("total")),
    }


def _invoice_item_workspace_row(
    line: InvoiceLine,
    invoice: Invoice,
    index: int,
    *,
    include_invoice_actions: bool,
) -> dict[str, Any]:
    document_id = str(invoice.document_id)
    invoice_id = str(invoice.id)
    invoice_title = invoice.invoice_number or invoice_id
    row: dict[str, Any] = {
        "index": index,
        "line_id": str(line.id),
        "invoice_id": invoice_id,
        "document_id": document_id,
        "invoice_number": invoice.invoice_number or "",
        "invoice_date": _format_date(invoice.invoice_date),
        "supplier": invoice.supplier.name if invoice.supplier else "",
        "line_number": line.line_number,
        "sku": line.sku or "",
        "description": line.description or "",
        "quantity": _format_number(line.quantity),
        "unit": line.unit or "",
        "unit_price": _format_money(line.unit_price),
        "amount": _format_money(line.amount),
        "tax_rate": _format_number(line.tax_rate),
        "tax_amount": _format_money(line.tax_amount),
        "currency": invoice.currency or "RUB",
        "invoice_status": (
            invoice.status.value if hasattr(invoice.status, "value") else str(invoice.status)
        ),
        "document_download": {
            "href": f"/api/documents/{document_id}/download",
            "label": "Скачать",
        },
    }
    if include_invoice_actions:
        row["invoice_delete"] = {
            "href": f"/api/invoices/{invoice_id}",
            "label": "Удалить",
            "confirm": f"Удалить счет {invoice_title}?",
            "method": "DELETE",
        }
    return row


def _format_date(value: Any) -> str:
    if not value:
        return ""
    return value.strftime("%d.%m.%Y") if hasattr(value, "strftime") else str(value)


def _format_money(value: Any) -> str:
    return format_money(value)


def _format_number(value: Any) -> str:
    return format_number(value)


async def _publish_workspace_status(message: str) -> None:
    await chat_bus.publish({"type": "status", "content": message})
