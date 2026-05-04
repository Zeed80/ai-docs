from __future__ import annotations

from datetime import UTC, datetime
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ChatMessage, ChatMessageAttachment, ChatSession


def _now() -> datetime:
    return datetime.now(UTC)


async def create_chat_session(
    db: AsyncSession,
    *,
    user_key: str,
    title: str = "Новый чат",
) -> ChatSession:
    session = ChatSession(
        user_key=user_key,
        title=title,
        last_message_at=_now(),
    )
    db.add(session)
    await db.flush()
    return session


async def get_chat_session(
    db: AsyncSession,
    *,
    session_id: uuid.UUID,
    user_key: str,
    include_deleted: bool = False,
) -> ChatSession | None:
    query = select(ChatSession).where(
        ChatSession.id == session_id,
        ChatSession.user_key == user_key,
    )
    if not include_deleted:
        query = query.where(ChatSession.deleted_at.is_(None))
    result = await db.execute(query)
    return result.scalar_one_or_none()


async def ensure_chat_session(
    db: AsyncSession,
    *,
    user_key: str,
    session_id: uuid.UUID | None = None,
) -> ChatSession:
    if session_id is not None:
        existing = await get_chat_session(db, session_id=session_id, user_key=user_key)
        if existing is not None:
            return existing
    return await create_chat_session(db, user_key=user_key)


async def append_chat_message(
    db: AsyncSession,
    *,
    session_id: uuid.UUID,
    role: str,
    content: str | None = None,
    metadata: dict | None = None,
) -> ChatMessage:
    msg = ChatMessage(
        session_id=session_id,
        role=role,
        content=content,
        metadata_=metadata,
    )
    db.add(msg)
    await db.flush()

    chat_session = await db.get(ChatSession, session_id)
    if chat_session is not None:
        chat_session.last_message_at = _now()
        chat_session.updated_at = _now()
    return msg


async def append_chat_attachment(
    db: AsyncSession,
    *,
    session_id: uuid.UUID,
    message_id: uuid.UUID | None,
    document_id: uuid.UUID | None,
    file_name: str,
    mime_type: str | None = None,
    size_bytes: int | None = None,
    metadata: dict | None = None,
) -> ChatMessageAttachment:
    attachment = ChatMessageAttachment(
        session_id=session_id,
        message_id=message_id,
        document_id=document_id,
        file_name=file_name,
        mime_type=mime_type,
        size_bytes=size_bytes,
        metadata_=metadata,
    )
    db.add(attachment)
    await db.flush()
    return attachment


async def link_pending_attachments_to_message(
    db: AsyncSession,
    *,
    session_id: uuid.UUID,
    message_id: uuid.UUID,
    document_ids: list[uuid.UUID],
) -> None:
    if not document_ids:
        return
    result = await db.execute(
        select(ChatMessageAttachment).where(
            ChatMessageAttachment.session_id == session_id,
            ChatMessageAttachment.message_id.is_(None),
            ChatMessageAttachment.document_id.in_(document_ids),
        )
    )
    for item in result.scalars().all():
        item.message_id = message_id


async def list_chat_sessions(
    db: AsyncSession,
    *,
    user_key: str,
) -> list[ChatSession]:
    result = await db.execute(
        select(ChatSession)
        .where(
            ChatSession.user_key == user_key,
            ChatSession.deleted_at.is_(None),
        )
        .order_by(ChatSession.updated_at.desc())
    )
    return list(result.scalars().all())


async def list_chat_messages(
    db: AsyncSession,
    *,
    session_id: uuid.UUID,
    user_key: str,
) -> tuple[ChatSession | None, list[ChatMessage], list[ChatMessageAttachment]]:
    session = await get_chat_session(db, session_id=session_id, user_key=user_key)
    if session is None:
        return None, [], []

    msg_result = await db.execute(
        select(ChatMessage)
        .where(ChatMessage.session_id == session_id)
        .order_by(ChatMessage.created_at.asc())
    )
    messages = list(msg_result.scalars().all())

    att_result = await db.execute(
        select(ChatMessageAttachment)
        .where(ChatMessageAttachment.session_id == session_id)
        .order_by(ChatMessageAttachment.created_at.asc())
    )
    attachments = list(att_result.scalars().all())
    return session, messages, attachments
