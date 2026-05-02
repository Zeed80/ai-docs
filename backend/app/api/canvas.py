"""Canvas API — agent publishes rich content blocks to the web UI canvas panel."""

from __future__ import annotations

from typing import Any, Literal

import structlog
from fastapi import APIRouter
from pydantic import BaseModel

from app.core.chat_bus import chat_bus

router = APIRouter()
logger = structlog.get_logger()


class CanvasColumn(BaseModel):
    key: str
    header: str
    type: Literal["text", "number", "date", "boolean"] = "text"
    width: int | None = None


class CanvasBlockPayload(BaseModel):
    type: Literal["markdown", "table", "image", "chart"]
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
    event = {
        "type": "canvas",
        "canvas_id": payload.canvas_id,
        "block": payload.block.model_dump(exclude_none=True),
        "append": payload.append,
    }
    await chat_bus.publish(event)
    logger.info("canvas_published", block_type=payload.block.type, canvas_id=payload.canvas_id)
    return CanvasPublishResponse(status="published", canvas_id=payload.canvas_id)
