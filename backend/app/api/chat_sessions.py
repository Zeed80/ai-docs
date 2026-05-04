from __future__ import annotations

from datetime import UTC, datetime
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.chat.store import (
    create_chat_session,
    get_chat_session,
    list_chat_messages,
    list_chat_sessions,
)
from app.chat.user_key import get_user_key
from app.db.models import ChatMessageAttachment
from app.db.session import get_db

router = APIRouter()


class ChatAttachmentRead(BaseModel):
    id: uuid.UUID
    message_id: uuid.UUID | None
    document_id: uuid.UUID | None
    file_name: str
    mime_type: str | None
    size_bytes: int | None
    created_at: datetime


class ChatMessageRead(BaseModel):
    id: uuid.UUID
    session_id: uuid.UUID
    role: str
    content: str | None
    metadata: dict | None = None
    created_at: datetime
    attachments: list[ChatAttachmentRead] = Field(default_factory=list)


class ChatSessionRead(BaseModel):
    id: uuid.UUID
    title: str
    user_key: str
    created_at: datetime
    updated_at: datetime
    last_message_at: datetime | None


class ChatSessionCreateRequest(BaseModel):
    title: str = "Новый чат"


@router.get("/sessions", response_model=list[ChatSessionRead])
async def get_sessions(
    db: AsyncSession = Depends(get_db),
    user_key: str = Depends(get_user_key),
) -> list[ChatSessionRead]:
    sessions = await list_chat_sessions(db, user_key=user_key)
    return [
        ChatSessionRead(
            id=session.id,
            title=session.title,
            user_key=session.user_key,
            created_at=session.created_at,
            updated_at=session.updated_at,
            last_message_at=session.last_message_at,
        )
        for session in sessions
    ]


@router.post("/sessions", response_model=ChatSessionRead, status_code=status.HTTP_201_CREATED)
async def create_session(
    payload: ChatSessionCreateRequest,
    db: AsyncSession = Depends(get_db),
    user_key: str = Depends(get_user_key),
) -> ChatSessionRead:
    session = await create_chat_session(
        db,
        user_key=user_key,
        title=payload.title.strip() or "Новый чат",
    )
    await db.commit()
    await db.refresh(session)
    return ChatSessionRead(
        id=session.id,
        title=session.title,
        user_key=session.user_key,
        created_at=session.created_at,
        updated_at=session.updated_at,
        last_message_at=session.last_message_at,
    )


@router.get("/sessions/{session_id}/messages", response_model=list[ChatMessageRead])
async def get_session_messages(
    session_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user_key: str = Depends(get_user_key),
) -> list[ChatMessageRead]:
    session, messages, attachments = await list_chat_messages(
        db,
        session_id=session_id,
        user_key=user_key,
    )
    if session is None:
        raise HTTPException(status_code=404, detail="Chat session not found")

    attachments_by_message: dict[uuid.UUID, list[ChatMessageAttachment]] = {}
    for item in attachments:
        if item.message_id is None:
            continue
        attachments_by_message.setdefault(item.message_id, []).append(item)

    return [
        ChatMessageRead(
            id=msg.id,
            session_id=msg.session_id,
            role=msg.role,
            content=msg.content,
            metadata=msg.metadata_,
            created_at=msg.created_at,
            attachments=[
                ChatAttachmentRead(
                    id=att.id,
                    message_id=att.message_id,
                    document_id=att.document_id,
                    file_name=att.file_name,
                    mime_type=att.mime_type,
                    size_bytes=att.size_bytes,
                    created_at=att.created_at,
                )
                for att in attachments_by_message.get(msg.id, [])
            ],
        )
        for msg in messages
    ]


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def soft_delete_session(
    session_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user_key: str = Depends(get_user_key),
) -> None:
    session = await get_chat_session(
        db,
        session_id=session_id,
        user_key=user_key,
        include_deleted=False,
    )
    if session is None:
        raise HTTPException(status_code=404, detail="Chat session not found")
    session.deleted_at = datetime.now(UTC)
    await db.commit()
