"""Team Rooms API — group chat + direct messages between users and agent."""
from __future__ import annotations

import re
import unicodedata
import uuid
from datetime import datetime, timezone
from typing import Any

import structlog
from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from sqlalchemy import and_, func, or_, select, outerjoin
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.auth.jwt import get_current_user
from app.auth.models import UserInfo
from app.core.chat_bus import chat_bus
from app.db.models import Room, RoomMember, RoomMessage, RoomMessageAttachment, RoomType, User
from app.db.session import get_db

router = APIRouter()
logger = structlog.get_logger()

_MIME_SIZE_LIMITS: dict[str, int] = {
    "image/": 20 * 1024 * 1024,   # 20 MB
    "video/": 200 * 1024 * 1024,  # 200 MB
    "application/pdf": 50 * 1024 * 1024,
    "": 50 * 1024 * 1024,          # default
}

_MENTION_RE = re.compile(r"@([\w\-\.]+)", re.UNICODE)


# ── Pydantic schemas ──────────────────────────────────────────────────────────


class RoomCreate(BaseModel):
    name: str
    description: str | None = None


class RoomOut(BaseModel):
    id: uuid.UUID
    name: str
    type: str
    description: str | None
    created_by: str
    is_archived: bool
    unread_count: int = 0
    member_count: int = 0
    last_message_at: datetime | None = None

    class Config:
        from_attributes = True


class RoomListResponse(BaseModel):
    items: list[RoomOut]
    total: int


class MemberOut(BaseModel):
    user_sub: str
    role: str
    joined_at: datetime
    name: str | None = None
    email: str | None = None


class MessageOut(BaseModel):
    id: uuid.UUID
    room_id: uuid.UUID
    sender_sub: str
    sender_name: str | None = None
    content: str
    content_type: str
    reply_to_id: uuid.UUID | None
    metadata: dict | None = None
    is_edited: bool
    edited_at: datetime | None
    created_at: datetime
    attachments: list["AttachmentOut"] = []


class AttachmentOut(BaseModel):
    id: uuid.UUID
    file_name: str
    file_size: int
    mime_type: str
    document_id: uuid.UUID | None
    thumbnail_key: str | None


class MessageCreate(BaseModel):
    content: str
    reply_to_id: uuid.UUID | None = None


class AddMember(BaseModel):
    user_sub: str


class ApproveFromChat(BaseModel):
    entity_type: str
    entity_id: uuid.UUID
    action_type: str
    assigned_to: str | None = None
    comment: str | None = None


# ── Helpers ───────────────────────────────────────────────────────────────────


def _safe_filename(name: str) -> str:
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    name = re.sub(r"[^\w\-\.]", "_", name)
    return name[:200] or "file"


def _size_limit(mime: str) -> int:
    for prefix, limit in _MIME_SIZE_LIMITS.items():
        if prefix and mime.startswith(prefix):
            return limit
    return _MIME_SIZE_LIMITS[""]


async def _get_member_or_403(
    db: AsyncSession, room_id: uuid.UUID, user_sub: str
) -> RoomMember:
    result = await db.execute(
        select(RoomMember).where(
            RoomMember.room_id == room_id, RoomMember.user_sub == user_sub
        )
    )
    member = result.scalar_one_or_none()
    if member is None:
        raise HTTPException(status_code=403, detail="Not a member of this room")
    return member


async def _load_message(db: AsyncSession, msg_id: uuid.UUID) -> RoomMessage:
    """Reload a message with attachments eagerly loaded (avoids lazy-load in async)."""
    result = await db.execute(
        select(RoomMessage)
        .options(selectinload(RoomMessage.attachments))
        .where(RoomMessage.id == msg_id)
    )
    return result.scalar_one()


async def _enrich_message(db: AsyncSession, msg: RoomMessage) -> dict:
    user_result = await db.execute(
        select(User.name).where(User.sub == msg.sender_sub)
    )
    sender_name = user_result.scalar_one_or_none() or msg.sender_sub
    attachments = [
        AttachmentOut(
            id=a.id,
            file_name=a.file_name,
            file_size=a.file_size,
            mime_type=a.mime_type,
            document_id=a.document_id,
            thumbnail_key=a.thumbnail_key,
        )
        for a in msg.attachments
    ]
    return MessageOut(
        id=msg.id,
        room_id=msg.room_id,
        sender_sub=msg.sender_sub,
        sender_name=sender_name,
        content=msg.content,
        content_type=msg.content_type,
        reply_to_id=msg.reply_to_id,
        metadata=msg.metadata_,
        is_edited=msg.is_edited,
        edited_at=msg.edited_at,
        created_at=msg.created_at,
        attachments=attachments,
    ).model_dump()


async def _unread_count(db: AsyncSession, room_id: uuid.UUID, user_sub: str) -> int:
    member_result = await db.execute(
        select(RoomMember.last_read_at).where(
            RoomMember.room_id == room_id, RoomMember.user_sub == user_sub
        )
    )
    last_read = member_result.scalar_one_or_none()
    if last_read is None:
        result = await db.execute(
            select(func.count()).where(RoomMessage.room_id == room_id)
        )
    else:
        result = await db.execute(
            select(func.count()).where(
                RoomMessage.room_id == room_id,
                RoomMessage.created_at > last_read,
            )
        )
    return result.scalar() or 0


async def _fire_mention_notifications(
    db: AsyncSession, room_id: uuid.UUID, msg: RoomMessage
) -> None:
    """Create Notification records for @mentioned users."""
    from app.services.notifications import create_notification
    from app.db.models import NotificationType

    mentions = set(_MENTION_RE.findall(msg.content))
    for mention in mentions:
        # resolve mention to user_sub (try username then email prefix)
        user_q = await db.execute(
            select(User).where(
                or_(User.preferred_username == mention, User.sub == mention)
            )
        )
        user = user_q.scalar_one_or_none()
        if user and user.sub != msg.sender_sub:
            await create_notification(
                db=db,
                user_sub=user.sub,
                type=NotificationType.mention,
                title="Вас упомянули в чате",
                body=msg.content[:200],
                entity_type="room_message",
                entity_id=msg.id,
                action_url=f"/chat/{room_id}",
            )


# ── Room CRUD ─────────────────────────────────────────────────────────────────


@router.get("", response_model=RoomListResponse)
async def list_rooms(
    user: UserInfo = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RoomListResponse:
    """List all rooms where the current user is a member (single-query, no N+1)."""
    # Subquery: member count per room
    member_count_sq = (
        select(RoomMember.room_id, func.count(RoomMember.user_sub).label("member_count"))
        .group_by(RoomMember.room_id)
        .subquery()
    )
    # Subquery: last message timestamp per room
    last_msg_sq = (
        select(RoomMessage.room_id, func.max(RoomMessage.created_at).label("last_message_at"))
        .group_by(RoomMessage.room_id)
        .subquery()
    )
    # Subquery: my last_read_at per room
    my_read_sq = (
        select(RoomMember.room_id, RoomMember.last_read_at)
        .where(RoomMember.user_sub == user.sub)
        .subquery()
    )
    # Subquery: unread count per room for this user
    unread_sq = (
        select(
            RoomMessage.room_id,
            func.count(RoomMessage.id).label("unread_count"),
        )
        .join(my_read_sq, my_read_sq.c.room_id == RoomMessage.room_id)
        .where(
            or_(
                my_read_sq.c.last_read_at.is_(None),
                RoomMessage.created_at > my_read_sq.c.last_read_at,
            )
        )
        .group_by(RoomMessage.room_id)
        .subquery()
    )

    stmt = (
        select(
            Room,
            func.coalesce(member_count_sq.c.member_count, 0).label("member_count"),
            last_msg_sq.c.last_message_at,
            func.coalesce(unread_sq.c.unread_count, 0).label("unread_count"),
        )
        .join(RoomMember, RoomMember.room_id == Room.id)
        .outerjoin(member_count_sq, member_count_sq.c.room_id == Room.id)
        .outerjoin(last_msg_sq, last_msg_sq.c.room_id == Room.id)
        .outerjoin(unread_sq, unread_sq.c.room_id == Room.id)
        .where(RoomMember.user_sub == user.sub, Room.is_archived == False)  # noqa: E712
        .order_by(Room.updated_at.desc())
    )

    rows = (await db.execute(stmt)).all()
    out = [
        RoomOut(
            id=room.id,
            name=room.name,
            type=str(room.type),
            description=room.description,
            created_by=room.created_by,
            is_archived=room.is_archived,
            unread_count=unread_count,
            member_count=member_count,
            last_message_at=last_message_at,
        )
        for room, member_count, last_message_at, unread_count in rows
    ]
    return RoomListResponse(items=out, total=len(out))


@router.post("", response_model=RoomOut, status_code=201)
async def create_room(
    payload: RoomCreate,
    user: UserInfo = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RoomOut:
    room = Room(
        name=payload.name,
        description=payload.description,
        type=RoomType.group,
        created_by=user.sub,
    )
    db.add(room)
    await db.flush()
    member = RoomMember(room_id=room.id, user_sub=user.sub, role="owner")
    db.add(member)
    await db.commit()
    await db.refresh(room)
    return RoomOut(
        id=room.id, name=room.name, type=str(room.type),
        description=room.description, created_by=room.created_by,
        is_archived=room.is_archived, unread_count=0, member_count=1,
    )


@router.get("/dm/{target_sub}", response_model=RoomOut)
async def get_or_create_dm(
    target_sub: str,
    user: UserInfo = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RoomOut:
    """Open (or create) a direct message room with another user."""
    if target_sub == user.sub:
        raise HTTPException(status_code=400, detail="Cannot DM yourself")

    # Find existing DM between the two users
    result = await db.execute(
        select(Room)
        .join(RoomMember, RoomMember.room_id == Room.id)
        .where(
            Room.type == RoomType.direct,
            RoomMember.user_sub == user.sub,
        )
        .options(selectinload(Room.members))
    )
    for room in result.scalars().all():
        subs = {m.user_sub for m in room.members}
        if target_sub in subs and len(subs) == 2:
            unread = await _unread_count(db, room.id, user.sub)
            return RoomOut(
                id=room.id, name=room.name, type=str(room.type),
                description=room.description, created_by=room.created_by,
                is_archived=room.is_archived, unread_count=unread,
                member_count=2,
            )

    # Create new DM
    room = Room(name="", type=RoomType.direct, created_by=user.sub)
    db.add(room)
    await db.flush()
    db.add(RoomMember(room_id=room.id, user_sub=user.sub, role="owner"))
    db.add(RoomMember(room_id=room.id, user_sub=target_sub, role="member"))
    await db.commit()
    await db.refresh(room)
    return RoomOut(
        id=room.id, name=room.name, type=str(room.type),
        description=room.description, created_by=room.created_by,
        is_archived=room.is_archived, unread_count=0, member_count=2,
    )


@router.get("/{room_id}", response_model=RoomOut)
async def get_room(
    room_id: uuid.UUID,
    user: UserInfo = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RoomOut:
    """Get a single room by ID (user must be a member)."""
    await _get_member_or_403(db, room_id, user.sub)
    room = await db.get(Room, room_id)
    if room is None:
        raise HTTPException(status_code=404, detail="Room not found")
    member_count_result = await db.execute(
        select(func.count()).where(RoomMember.room_id == room.id)
    )
    member_count = member_count_result.scalar() or 0
    unread = await _unread_count(db, room.id, user.sub)
    return RoomOut(
        id=room.id,
        name=room.name,
        type=str(room.type),
        description=room.description,
        created_by=room.created_by,
        is_archived=room.is_archived,
        unread_count=unread,
        member_count=member_count,
    )


@router.get("/{room_id}/members", response_model=list[MemberOut])
async def list_members(
    room_id: uuid.UUID,
    user: UserInfo = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[MemberOut]:
    await _get_member_or_403(db, room_id, user.sub)
    result = await db.execute(
        select(RoomMember).where(RoomMember.room_id == room_id)
    )
    members = result.scalars().all()
    out = []
    for m in members:
        user_result = await db.execute(
            select(User.name, User.email).where(User.sub == m.user_sub)
        )
        row = user_result.first()
        out.append(MemberOut(
            user_sub=m.user_sub,
            role=m.role,
            joined_at=m.joined_at,
            name=row.name if row else None,
            email=row.email if row else None,
        ))
    return out


@router.post("/{room_id}/members", status_code=201)
async def add_member(
    room_id: uuid.UUID,
    payload: AddMember,
    user: UserInfo = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    me = await _get_member_or_403(db, room_id, user.sub)
    if me.role != "owner":
        raise HTTPException(status_code=403, detail="Only room owner can add members")
    existing = await db.execute(
        select(RoomMember).where(
            RoomMember.room_id == room_id, RoomMember.user_sub == payload.user_sub
        )
    )
    if existing.scalar_one_or_none():
        return {"status": "already_member"}
    db.add(RoomMember(room_id=room_id, user_sub=payload.user_sub))
    await db.commit()
    await chat_bus.push_to_room(str(room_id), {"type": "member_joined", "user_sub": payload.user_sub})
    return {"status": "added"}


@router.delete("/{room_id}/members/{sub}")
async def remove_member(
    room_id: uuid.UUID,
    sub: str,
    user: UserInfo = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    me = await _get_member_or_403(db, room_id, user.sub)
    if me.role != "owner" and sub != user.sub:
        raise HTTPException(status_code=403, detail="Only owner can remove others")
    result = await db.execute(
        select(RoomMember).where(RoomMember.room_id == room_id, RoomMember.user_sub == sub)
    )
    member = result.scalar_one_or_none()
    if member:
        await db.delete(member)
        await db.commit()
    await chat_bus.push_to_room(str(room_id), {"type": "member_left", "user_sub": sub})
    return {"status": "removed"}


# ── Messages ──────────────────────────────────────────────────────────────────


@router.get("/{room_id}/messages")
async def get_messages(
    room_id: uuid.UUID,
    before: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=50, le=100),
    user: UserInfo = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    await _get_member_or_403(db, room_id, user.sub)

    stmt = (
        select(RoomMessage)
        .where(RoomMessage.room_id == room_id)
        .options(selectinload(RoomMessage.attachments))
        .order_by(RoomMessage.created_at.desc())
        .limit(limit)
    )
    if before:
        ref_result = await db.execute(
            select(RoomMessage.created_at).where(RoomMessage.id == before)
        )
        ref_ts = ref_result.scalar_one_or_none()
        if ref_ts:
            stmt = stmt.where(RoomMessage.created_at < ref_ts)

    result = await db.execute(stmt)
    msgs = list(reversed(result.scalars().all()))
    return [await _enrich_message(db, m) for m in msgs]


@router.post("/{room_id}/messages", status_code=201)
async def send_message(
    room_id: uuid.UUID,
    payload: MessageCreate,
    user: UserInfo = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    from app.ai.input_sanitizer import sanitize_user_input

    await _get_member_or_403(db, room_id, user.sub)
    clean_content, warnings = sanitize_user_input(payload.content)

    msg = RoomMessage(
        room_id=room_id,
        sender_sub=user.sub,
        content=clean_content,
        content_type="text",
        reply_to_id=payload.reply_to_id,
    )
    db.add(msg)
    await db.flush()
    await _fire_mention_notifications(db, room_id, msg)
    await db.commit()

    msg = await _load_message(db, msg.id)
    enriched = await _enrich_message(db, msg)
    await chat_bus.push_to_room(str(room_id), {"type": "message", "data": enriched})
    return enriched


class MessageUpdate(BaseModel):
    content: str


@router.patch("/{room_id}/messages/{msg_id}")
async def edit_message(
    room_id: uuid.UUID,
    msg_id: uuid.UUID,
    payload: MessageUpdate,
    user: UserInfo = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await _get_member_or_403(db, room_id, user.sub)
    msg = await db.get(RoomMessage, msg_id)
    if msg is None or msg.room_id != room_id:
        raise HTTPException(status_code=404, detail="Message not found")
    if msg.sender_sub != user.sub:
        raise HTTPException(status_code=403, detail="Not your message")
    msg.content = payload.content
    msg.is_edited = True
    msg.edited_at = datetime.now(timezone.utc)
    await db.commit()
    msg = await _load_message(db, msg.id)
    enriched = await _enrich_message(db, msg)
    await chat_bus.push_to_room(str(room_id), {"type": "message_edited", "data": enriched})
    return enriched


@router.delete("/{room_id}/messages/{msg_id}", status_code=204)
async def delete_message(
    room_id: uuid.UUID,
    msg_id: uuid.UUID,
    user: UserInfo = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    await _get_member_or_403(db, room_id, user.sub)
    msg = await db.get(RoomMessage, msg_id)
    if msg is None or msg.room_id != room_id:
        raise HTTPException(status_code=404, detail="Message not found")
    if msg.sender_sub != user.sub:
        raise HTTPException(status_code=403, detail="Not your message")
    await db.delete(msg)
    await db.commit()
    await chat_bus.push_to_room(str(room_id), {"type": "message_deleted", "data": {"id": str(msg_id)}})


@router.post("/{room_id}/upload", status_code=201)
async def upload_file(
    room_id: uuid.UUID,
    file: UploadFile = File(...),
    user: UserInfo = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await _get_member_or_403(db, room_id, user.sub)

    data = await file.read()

    # Verify MIME type from file content, not just browser header
    try:
        import filetype as _ft
        detected = _ft.guess(data)
        mime = detected.mime if detected else (file.content_type or "application/octet-stream")
    except Exception:
        mime = file.content_type or "application/octet-stream"

    if len(data) > _size_limit(mime):
        raise HTTPException(status_code=413, detail=f"File too large for MIME type {mime}")

    safe_name = _safe_filename(file.filename or "upload")
    storage_key = f"rooms/{room_id}/{uuid.uuid4()}/{safe_name}"

    # Store in MinIO
    try:
        from app.core.storage import get_minio_client
        client = get_minio_client()
        import io
        client.put_object(
            "documents",
            storage_key,
            io.BytesIO(data),
            length=len(data),
            content_type=mime,
        )
    except Exception as exc:
        logger.warning("minio_upload_failed", error=str(exc))
        storage_key = f"local/{storage_key}"

    # Create message + attachment
    msg = RoomMessage(
        room_id=room_id,
        sender_sub=user.sub,
        content=safe_name,
        content_type="file",
    )
    db.add(msg)
    await db.flush()

    attachment = RoomMessageAttachment(
        message_id=msg.id,
        file_name=file.filename or safe_name,
        file_size=len(data),
        mime_type=mime,
        storage_key=storage_key,
    )
    db.add(attachment)
    await db.commit()

    msg = await _load_message(db, msg.id)
    enriched = await _enrich_message(db, msg)
    await chat_bus.push_to_room(str(room_id), {"type": "message", "data": enriched})

    return {"message_id": str(msg.id), "attachment_id": str(attachment.id), "storage_key": storage_key}


@router.post("/{room_id}/read")
async def mark_read(
    room_id: uuid.UUID,
    message_id: uuid.UUID | None = Query(default=None),
    user: UserInfo = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    member = await _get_member_or_403(db, room_id, user.sub)
    member.last_read_at = datetime.now(timezone.utc)
    await db.commit()
    return {"status": "ok"}


# ── Quick actions ─────────────────────────────────────────────────────────────


@router.post("/{room_id}/messages/{msg_id}/recognize")
async def recognize_attachment(
    room_id: uuid.UUID,
    msg_id: uuid.UUID,
    user: UserInfo = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Send the first attachment of a message to the OCR/ingest pipeline."""
    await _get_member_or_403(db, room_id, user.sub)

    attach_result = await db.execute(
        select(RoomMessageAttachment).where(RoomMessageAttachment.message_id == msg_id).limit(1)
    )
    attachment = attach_result.scalar_one_or_none()
    if not attachment:
        raise HTTPException(status_code=404, detail="No attachment on this message")

    # Dispatch Celery ingest task
    try:
        from app.tasks.ingest import ingest_from_storage
        task = ingest_from_storage.delay(
            storage_key=attachment.storage_key,
            file_name=attachment.file_name,
            mime_type=attachment.mime_type,
            source_channel="chat",
            uploaded_by=user.sub,
        )
        attachment.ingest_job_id = task.id
        await db.commit()
        job_id = task.id
    except Exception as exc:
        logger.warning("ingest_dispatch_failed", error=str(exc))
        job_id = None

    # Post status message in room
    status_msg = RoomMessage(
        room_id=room_id,
        sender_sub="sveta",
        content=f"Отправлено на распознавание: {attachment.file_name}",
        content_type="system",
        metadata_={"job_id": job_id, "attachment_id": str(attachment.id)},
    )
    db.add(status_msg)
    await db.commit()

    return {"status": "queued", "job_id": job_id}


@router.post("/{room_id}/messages/{msg_id}/approve")
async def create_approval_from_chat(
    room_id: uuid.UUID,
    msg_id: uuid.UUID,
    payload: ApproveFromChat,
    user: UserInfo = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Create an Approval request linked to a chat message."""
    from app.db.models import Approval, ApprovalActionType

    await _get_member_or_403(db, room_id, user.sub)

    try:
        action_type = ApprovalActionType(payload.action_type)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Unknown action_type: {payload.action_type}")

    approval = Approval(
        action_type=action_type,
        entity_type=payload.entity_type,
        entity_id=payload.entity_id,
        requested_by=user.sub,
        assigned_to=payload.assigned_to,
        context={"comment": payload.comment, "room_id": str(room_id), "message_id": str(msg_id)},
    )
    db.add(approval)
    await db.flush()
    await db.commit()
    await db.refresh(approval)

    return {"approval_id": str(approval.id), "status": "pending"}


@router.post("/{room_id}/messages/{msg_id}/forward-doc")
async def forward_document(
    room_id: uuid.UUID,
    msg_id: uuid.UUID,
    user: UserInfo = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Forward a document from a chat message as a Handover to another user."""
    await _get_member_or_403(db, room_id, user.sub)

    msg_result = await db.execute(
        select(RoomMessage).options(selectinload(RoomMessage.attachments)).where(RoomMessage.id == msg_id)
    )
    msg = msg_result.scalar_one_or_none()
    if not msg:
        raise HTTPException(status_code=404, detail="Message not found")

    attachment = next((a for a in msg.attachments if a.document_id), None)
    if not attachment or not attachment.document_id:
        raise HTTPException(status_code=400, detail="No linked document on this message")

    return {
        "document_id": str(attachment.document_id),
        "forward_url": f"/documents/{attachment.document_id}",
    }


# ── WebSocket ─────────────────────────────────────────────────────────────────


@router.websocket("/ws/{room_id}")
async def room_websocket(
    room_id: uuid.UUID,
    websocket: WebSocket,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Real-time WebSocket for a room — broadcasts messages and events."""
    await websocket.accept()

    # Authenticate via cookie
    token = websocket.cookies.get("access_token")
    if not token:
        await websocket.close(code=4001)
        return

    try:
        from app.auth.jwt import _verify_token
        from app.config import settings
        if settings.auth_enabled:
            user_info = await _verify_token(token)
            user_sub = user_info.sub
        else:
            user_sub = "dev-user"
    except Exception:
        await websocket.close(code=4001)
        return

    # Verify membership
    result = await db.execute(
        select(RoomMember).where(
            RoomMember.room_id == room_id, RoomMember.user_sub == user_sub
        )
    )
    if result.scalar_one_or_none() is None:
        await websocket.close(code=4003)
        return

    async def on_event(event: dict) -> None:
        try:
            await websocket.send_json(event)
        except Exception:
            pass

    sid = chat_bus.subscribe_room(str(room_id), on_event)
    try:
        while True:
            data = await websocket.receive_json()
            event_type = data.get("type")
            if event_type == "typing":
                await chat_bus.push_to_room(
                    str(room_id),
                    {"type": "typing", "user_sub": user_sub},
                )
            # Other client-side events handled via REST
    except WebSocketDisconnect:
        pass
    finally:
        chat_bus.unsubscribe_room(str(room_id), sid)
