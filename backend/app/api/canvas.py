"""Canvas API — agent publishes rich content blocks to the web UI canvas panel."""

from __future__ import annotations

from typing import Any, Literal

import structlog
from fastapi import APIRouter
from pydantic import BaseModel

from app.core.chat_bus import chat_bus
from app.domain.workspace import append_workspace_block, upsert_workspace_block

router = APIRouter()
logger = structlog.get_logger()


class CanvasColumn(BaseModel):
    key: str
    header: str
    type: Literal["text", "number", "date", "boolean", "link", "download", "delete"] = "text"
    width: int | None = None


class CanvasDocumentItem(BaseModel):
    id: str
    title: str
    filename: str | None = None
    mime_type: str | None = None
    size_bytes: int | None = None
    download_url: str | None = None
    delete_url: str | None = None


class CanvasBlockPayload(BaseModel):
    type: Literal["markdown", "table", "image", "chart", "document"]
    title: str | None = None
    # markdown
    content: str | None = None
    # table
    columns: list[CanvasColumn] | None = None
    rows: list[dict[str, Any]] | None = None
    # image
    url: str | None = None
    alt: str | None = None
    # chart
    chart_type: Literal["bar", "line", "pie", "area"] | None = None
    chart_data: dict[str, Any] | None = None
    # document
    documents: list[CanvasDocumentItem] | None = None


class CanvasPublishRequest(BaseModel):
    canvas_id: str | None = None
    block: CanvasBlockPayload
    append: bool = True


class CanvasPublishResponse(BaseModel):
    status: str
    canvas_id: str | None


@router.post("/publish", response_model=CanvasPublishResponse)
async def publish_to_canvas(payload: CanvasPublishRequest) -> CanvasPublishResponse:
    """Skill: canvas.publish — Send a rich content block to the agent canvas panel.

    Supports markdown text, Excel-style tables, images, and charts.
    The block is broadcast to all active WebSocket clients via chat_bus.
    """
    block = payload.block.model_dump(exclude_none=True)
    if payload.canvas_id:
        stored = upsert_workspace_block(payload.canvas_id, block)
    else:
        stored = append_workspace_block(block)

    event = {
        "type": "canvas",
        "canvas_id": stored["id"],
        "block": stored,
        "append": payload.append,
    }
    await chat_bus.publish(event)
    logger.info("canvas_published", block_type=payload.block.type, canvas_id=payload.canvas_id)
    return CanvasPublishResponse(status="published", canvas_id=stored["id"])
