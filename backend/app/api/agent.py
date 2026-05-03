"""WebSocket endpoint for the AiAgent agent (Света)."""

import asyncio
import json

import structlog
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.ai.agent_loop import AgentSession
from app.core.chat_bus import chat_bus

router = APIRouter()
logger = structlog.get_logger()


@router.websocket("/ws/chat")
async def chat_ws(ws: WebSocket) -> None:
    await ws.accept()
    logger.info("ws_chat_connected", client=ws.client)

    async def send(data: dict) -> None:
        try:
            await ws.send_text(json.dumps(data, ensure_ascii=False))
        except Exception:
            pass

    # Mirror Telegram conversations to this WebSocket client
    sub_id = chat_bus.subscribe(send)

    session = AgentSession(send)
    current_turn: asyncio.Task | None = None

    # Greeting — may fail if health-check client disconnects immediately
    try:
        await send({"type": "text", "content": "Привет! Я Света. Чем могу помочь?"})
        await send({"type": "done"})
    except Exception:
        chat_bus.unsubscribe(sub_id)
        return

    try:
        while True:
            raw = await ws.receive_text()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue

            msg_type = data.get("type")

            if msg_type == "message":
                content = data.get("content", "").strip()
                if content:
                    if current_turn and not current_turn.done():
                        await send({
                            "type": "error",
                            "content": "Предыдущая задача ещё выполняется.",
                        })
                        continue
                    current_turn = asyncio.create_task(session.on_user_message(content))

            elif msg_type == "approve":
                await session.on_approval(True)

            elif msg_type == "reject":
                await session.on_approval(False)

    except WebSocketDisconnect:
        logger.info("ws_chat_disconnected")
        if current_turn and not current_turn.done():
            current_turn.cancel()
    except Exception as e:
        logger.error("ws_chat_error", error=str(e))
        try:
            await send({"type": "error", "content": str(e)})
        except Exception:
            pass
        if current_turn and not current_turn.done():
            current_turn.cancel()
    finally:
        chat_bus.unsubscribe(sub_id)
