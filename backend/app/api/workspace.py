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
        .limit(min(max(payload.limit, 1), 5000))
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
        .limit(min(max(payload.limit, 1), 10000))
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
        .limit(min(max(payload.limit, 1), 5000))
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

    result = await db.execute(
        select(InvoiceLine, Invoice)
        .join(Invoice, InvoiceLine.invoice_id == Invoice.id)
        .options(selectinload(Invoice.supplier))
        .order_by(Invoice.created_at.desc(), InvoiceLine.line_number.asc())
        .limit(min(max(payload.limit, 1), 10000))
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

    rows = [
        _invoice_items_by_supplier_workspace_row(group, index)
        for index, group in enumerate(
            sorted(groups.values(), key=lambda item: str(item["supplier"]).lower()),
            start=1,
        )
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
