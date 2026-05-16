"""Comments API — threaded comments on any entity."""
from __future__ import annotations
import re
import uuid
import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from app.auth.jwt import get_current_user
from app.auth.models import UserInfo
from app.db.session import get_db
from app.db.models import Comment, User

router = APIRouter()
logger = structlog.get_logger()

_MENTION_RE = re.compile(r"@([\w\-\.]+)", re.UNICODE)


class CommentOut(BaseModel):
    id: uuid.UUID
    entity_type: str
    entity_id: uuid.UUID
    author_sub: str
    author_name: str
    text: str
    parent_id: uuid.UUID | None
    created_at: str

    model_config = {"from_attributes": True}


class CommentCreate(BaseModel):
    entity_type: str
    entity_id: uuid.UUID
    text: str
    parent_id: uuid.UUID | None = None


class CommentUpdate(BaseModel):
    text: str


def _to_out(c: Comment, author_name: str | None = None) -> CommentOut:
    return CommentOut(
        id=c.id,
        entity_type=c.entity_type,
        entity_id=c.entity_id,
        author_sub=c.user_id,
        author_name=author_name or c.user_id,
        text=c.body,
        parent_id=c.parent_id,
        created_at=c.created_at.isoformat(),
    )


async def _resolve_name(db: AsyncSession, user_sub: str) -> str:
    result = await db.execute(select(User.name).where(User.sub == user_sub))
    return result.scalar_one_or_none() or user_sub


async def _fire_mention_notifications(
    db: AsyncSession,
    comment: Comment,
    author_sub: str,
) -> None:
    from app.services.notifications import create_notification
    from app.db.models import NotificationType

    mentions = set(_MENTION_RE.findall(comment.body))
    for mention in mentions:
        user_q = await db.execute(
            select(User).where(
                or_(User.preferred_username == mention, User.sub == mention)
            )
        )
        mentioned = user_q.scalar_one_or_none()
        if mentioned and mentioned.sub != author_sub:
            action_url = f"/{comment.entity_type}s/{comment.entity_id}"
            await create_notification(
                db=db,
                user_sub=mentioned.sub,
                type=NotificationType.mention,
                title="Вас упомянули в комментарии",
                body=comment.body[:200],
                entity_type=comment.entity_type,
                entity_id=comment.entity_id,
                action_url=action_url,
            )


@router.get("", response_model=list[CommentOut])
async def list_comments(
    entity_type: str,
    entity_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: UserInfo = Depends(get_current_user),
) -> list[CommentOut]:
    result = await db.execute(
        select(Comment)
        .where(Comment.entity_type == entity_type, Comment.entity_id == entity_id)
        .order_by(Comment.created_at.asc())
    )
    rows = result.scalars().all()
    out = []
    for c in rows:
        name = await _resolve_name(db, c.user_id)
        out.append(_to_out(c, name))
    return out


@router.post("", response_model=CommentOut, status_code=201)
async def create_comment(
    body: CommentCreate,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> CommentOut:
    if body.parent_id is not None:
        parent = await db.get(Comment, body.parent_id)
        if parent is None:
            raise HTTPException(status_code=404, detail="Parent comment not found")

    comment = Comment(
        entity_type=body.entity_type,
        entity_id=body.entity_id,
        user_id=user.sub,
        body=body.text,
        parent_id=body.parent_id,
    )
    db.add(comment)
    await db.commit()
    await db.refresh(comment)
    logger.info("comment_created", id=str(comment.id), entity_type=body.entity_type)
    await _fire_mention_notifications(db, comment, user.sub)
    name = await _resolve_name(db, user.sub)
    return _to_out(comment, name)


@router.patch("/{comment_id}", response_model=CommentOut)
async def update_comment(
    comment_id: uuid.UUID,
    body: CommentUpdate,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> CommentOut:
    comment = await db.get(Comment, comment_id)
    if comment is None:
        raise HTTPException(status_code=404, detail="Comment not found")
    if comment.user_id != user.sub:
        raise HTTPException(status_code=403, detail="Not your comment")
    comment.body = body.text
    await db.commit()
    await db.refresh(comment)
    name = await _resolve_name(db, user.sub)
    return _to_out(comment, name)


@router.delete("/{comment_id}", status_code=204)
async def delete_comment(
    comment_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> None:
    comment = await db.get(Comment, comment_id)
    if comment is None:
        raise HTTPException(status_code=404, detail="Comment not found")
    if comment.user_id != user.sub:
        raise HTTPException(status_code=403, detail="Not your comment")
    await db.delete(comment)
    await db.commit()
