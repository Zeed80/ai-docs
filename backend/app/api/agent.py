"""WebSocket endpoint for the AiAgent agent (Света)."""

import asyncio
import json
import re
import uuid

import structlog
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.ai.orchestrator import AgentOrchestrator
from app.ai.router import ai_router
from app.chat.store import (
    ChatSessionNotFoundError,
    append_chat_attachment,
    append_chat_message,
    ensure_chat_session,
    link_pending_attachments_to_message,
    list_chat_messages,
    update_chat_session_title,
)
from app.chat.user_key import get_ws_user_key
from app.core.chat_bus import chat_bus
from app.db.session import _get_session_factory

router = APIRouter()
logger = structlog.get_logger()


def _fallback_chat_title(first_message: str) -> str:
    text = re.sub(r"\s+", " ", (first_message or "").strip())
    if not text:
        return "Новый чат"
    text = re.sub(
        r"^(пожалуйста|света|выведи|покажи|сделай|напиши|расскажи|найди)\s+",
        "",
        text,
        flags=re.IGNORECASE,
    )
    return " ".join(text.split()[:5])[:45].strip(" ,.;:!?") or "Новый чат"


@router.websocket("/ws/chat")
async def chat_ws(ws: WebSocket) -> None:
    await ws.accept()
    logger.info("ws_chat_connected", client=ws.client)
    user_key = await get_ws_user_key(ws)
    db_factory = _get_session_factory()
    active_session_id: uuid.UUID | None = None
    assistant_buffer: list[str] = []
    turn_in_progress = False
    agent_sessions: dict[uuid.UUID, AgentOrchestrator] = {}
    active_agent_session: AgentOrchestrator | None = None

    async def generate_session_title(session_id: uuid.UUID, first_message: str) -> None:
        try:
            title = await ai_router.generate_chat_title(first_message)
        except Exception as exc:
            logger.warning("chat_title_generation_failed", error=str(exc))
            title = _fallback_chat_title(first_message)
        async with db_factory() as db:
            await update_chat_session_title(
                db,
                session_id=session_id,
                user_key=user_key,
                title=title,
            )
            await db.commit()
        await chat_bus.publish({
            "type": "chat.session_updated",
            "session_id": str(session_id),
            "title": title,
        })

    async def send(data: dict) -> None:
        try:
            await ws.send_text(json.dumps(data, ensure_ascii=False))
        except Exception:
            pass
        nonlocal assistant_buffer, turn_in_progress, active_session_id
        if not turn_in_progress or active_session_id is None:
            return
        msg_type = data.get("type")
        if msg_type == "text":
            token = str(data.get("content", "") or "")
            if token:
                assistant_buffer.append(token)
            return
        if msg_type == "tool_call":
            async with db_factory() as db:
                await append_chat_message(
                    db,
                    session_id=active_session_id,
                    role="tool",
                    content=f"Tool call: {data.get('tool')}",
                    metadata={"args": data.get("args"), "tool": data.get("tool")},
                )
                await db.commit()
            return
        if msg_type == "tool_result":
            async with db_factory() as db:
                await append_chat_message(
                    db,
                    session_id=active_session_id,
                    role="tool",
                    content=f"Tool result: {data.get('tool')}",
                    metadata={"result": data.get("result"), "tool": data.get("tool")},
                )
                await db.commit()
            return
        if msg_type == "approval_request":
            async with db_factory() as db:
                await append_chat_message(
                    db,
                    session_id=active_session_id,
                    role="approval",
                    content=f"Approval request: {data.get('tool')}",
                    metadata={"args": data.get("args"), "preview": data.get("preview")},
                )
                await db.commit()
            return
        if msg_type in {"error", "done"}:
            final_text = "".join(assistant_buffer).strip()
            assistant_buffer = []
            turn_in_progress = False
            if final_text:
                async with db_factory() as db:
                    await append_chat_message(
                        db,
                        session_id=active_session_id,
                        role="assistant",
                        content=final_text,
                    )
                    await db.commit()

    # Mirror Telegram conversations to this WebSocket client
    sub_id = chat_bus.subscribe(send)

    current_turn: asyncio.Task | None = None

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
                    raw_session_id = data.get("session_id")
                    incoming_session_id: uuid.UUID | None = None
                    if isinstance(raw_session_id, str):
                        try:
                            incoming_session_id = uuid.UUID(raw_session_id)
                        except ValueError:
                            incoming_session_id = None
                    try:
                        async with db_factory() as db:
                            chat_session = await ensure_chat_session(
                                db,
                                user_key=user_key,
                                session_id=incoming_session_id,
                            )
                            active_session_id = chat_session.id
                            user_message = await append_chat_message(
                                db,
                                session_id=chat_session.id,
                                role="user",
                                content=content,
                            )
                            attachments = data.get("attachments")
                            attachment_doc_ids: list[uuid.UUID] = []
                            if isinstance(attachments, list):
                                for item in attachments:
                                    if not isinstance(item, dict):
                                        continue
                                    raw_doc = item.get("document_id")
                                    parsed_doc: uuid.UUID | None = None
                                    if isinstance(raw_doc, str):
                                        try:
                                            parsed_doc = uuid.UUID(raw_doc)
                                            attachment_doc_ids.append(parsed_doc)
                                        except ValueError:
                                            parsed_doc = None
                                    await append_chat_attachment(
                                        db,
                                        session_id=chat_session.id,
                                        message_id=user_message.id,
                                        document_id=parsed_doc,
                                        file_name=str(item.get("file_name") or "attachment"),
                                        mime_type=(
                                            str(item.get("mime_type"))
                                            if item.get("mime_type")
                                            else None
                                        ),
                                        size_bytes=(
                                            int(item.get("size_bytes"))
                                            if isinstance(item.get("size_bytes"), int)
                                            else None
                                        ),
                                    )
                            await link_pending_attachments_to_message(
                                db,
                                session_id=chat_session.id,
                                message_id=user_message.id,
                                document_ids=attachment_doc_ids,
                            )
                            await db.commit()
                            session_id = chat_session.id
                            if session_id not in agent_sessions:
                                _, history, _ = await list_chat_messages(
                                    db,
                                    session_id=session_id,
                                    user_key=user_key,
                                )
                                restored = [
                                    {"role": msg.role, "content": msg.content or ""}
                                    for msg in history
                                    if msg.role in {"user", "assistant"}
                                    and msg.content
                                    and msg.id != user_message.id
                                ]
                                agent = AgentOrchestrator(send)
                                agent.hydrate_history(restored)
                                agent_sessions[session_id] = agent
                                if (
                                    not restored
                                    and chat_session.title.strip().lower() == "новый чат"
                                ):
                                    asyncio.create_task(
                                        generate_session_title(session_id, content)
                                    )
                            active_agent_session = agent_sessions[session_id]
                    except ChatSessionNotFoundError:
                        await send({
                            "type": "error",
                            "content": (
                                "Чат не найден или устарел. "
                                "Выберите чат из списка или создайте новый."
                            ),
                        })
                        continue
                    await send({"type": "session", "session_id": str(active_session_id)})
                    turn_in_progress = True
                    assistant_buffer = []
                    current_turn = asyncio.create_task(
                        active_agent_session.on_user_message(content)
                    )

            elif msg_type == "stop":
                if current_turn and not current_turn.done():
                    current_turn.cancel()

            elif msg_type == "approve":
                if active_agent_session is not None:
                    await active_agent_session.on_approval(True)

            elif msg_type == "reject":
                if active_agent_session is not None:
                    await active_agent_session.on_approval(False)

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
