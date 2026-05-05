"""Workspace API — agent-owned rich output blocks and orchestrated views."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.chat_bus import chat_bus
from app.db.models import Invoice, InvoiceLine
from app.db.session import get_db
from app.domain.workspace import (
    clear_workspace_blocks,
    delete_workspace_block,
    list_workspace_blocks,
    upsert_workspace_block,
)

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


class WorkspaceToolResponse(BaseModel):
    status: str
    canvas_id: str
    total: int
    shown: int
    message: str


@router.get("/blocks", response_model=WorkspaceBlockResponse)
async def get_workspace_blocks() -> WorkspaceBlockResponse:
    """List current agent Workspace blocks."""
    items = list_workspace_blocks()
    return WorkspaceBlockResponse(items=items, total=len(items))


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
    total = (
        await db.execute(select(func.count()).select_from(InvoiceLine))
    ).scalar_one()
    result = await db.execute(
        select(InvoiceLine, Invoice)
        .join(Invoice, InvoiceLine.invoice_id == Invoice.id)
        .options(selectinload(Invoice.supplier))
        .order_by(Invoice.created_at.desc(), InvoiceLine.line_number.asc())
        .limit(min(max(payload.limit, 1), 10000))
    )
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
        "title": f"Товары по счетам ({total})",
        "columns": columns,
        "rows": rows,
        "source": "workspace.invoice_items_table",
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
        message=f"Открыл на Рабочем столе таблицу товаров по счетам: {len(rows)} из {total}.",
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
    if value is None:
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    return f"{number:,.2f}".replace(",", " ").replace(".00", "")


def _format_number(value: Any) -> str:
    if value is None:
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    return f"{number:,.4f}".replace(",", " ").rstrip("0").rstrip(".")


async def _publish_workspace_status(message: str) -> None:
    await chat_bus.publish({"type": "status", "content": message})
