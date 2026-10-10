"""E25 synthetic recipient: a gateway route and a fenced one-commit effect.

Run by the harness as its own uvicorn process against the test database. The
effect is one Party row named after the request marker, committed through the
real get_db with the real E24 fence, so the receipt is written in the same
commit as the effect.
"""

from __future__ import annotations

import os
import uuid

import httpx
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.effect_fence import EFFECT_FENCE_HEADER, EffectFenceMiddleware, EffectFenceRejected
from app.db.models import Party
from app.db.session import get_db

app = FastAPI()
app.add_middleware(EffectFenceMiddleware)


@app.exception_handler(EffectFenceRejected)
async def _rejected(_request, exc: EffectFenceRejected):
    return JSONResponse(
        status_code=409,
        content={"detail": {"error_code": "effect_fence_rejected", "reason": exc.reason}},
    )


@app.get("/health")
async def health() -> dict:
    return {"ok": True}


@app.post("/api/agent/cap/{capability}")
async def gateway(capability: str, request: Request) -> JSONResponse:
    """Like the real gateway: relays the fence to the recipient it calls."""
    body = await request.json()
    headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() in {"x-api-key", EFFECT_FENCE_HEADER.lower(), "x-acting-user"}
    }
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            f"{os.environ['E25_SELF_URL']}/effects", json=body, headers=headers
        )
    return JSONResponse(status_code=response.status_code, content=response.json())


@app.post("/effects")
async def effect(body: dict, db: AsyncSession = Depends(get_db)) -> dict:
    marker = str(body.get("marker"))
    db.add(Party(name=f"e25:{marker}", inn=str(uuid.uuid4().int)[:12]))
    await db.commit()
    return {"status": "ok", "marker": marker}
