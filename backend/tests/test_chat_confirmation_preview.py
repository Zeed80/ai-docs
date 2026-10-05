"""The approval card shows what an email.send approval would actually send."""

import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api.chat_runs import _confirmation_preview
from app.auth.models import UserInfo
from app.db.models import DraftAction


def _user(sub: str) -> UserInfo:
    return UserInfo(sub=sub, email=f"{sub}@example.test", name=sub, preferred_username=sub)


async def _draft(factory, **data) -> uuid.UUID:
    async with factory() as db:
        draft = DraftAction(
            action_type="email_send",
            entity_type="email",
            draft_data={"status": "draft", **data},
        )
        db.add(draft)
        await db.commit()
        return draft.id


def _send(draft_id, digest="d1"):
    return {
        "tool": "email",
        "args": {"action": "send", "draft_id": str(draft_id), "expected_digest": digest},
    }


@pytest.mark.asyncio
async def test_owner_sees_recipient_subject_and_text(test_engine):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    draft_id = await _draft(
        factory,
        created_by_sub="alice",
        to_addresses=["buyer@example.test"],
        subject="Прайс-лист",
        body_text="Добрый день! " + "x" * 3000,
        content_digest="d1",
        attachment_ids=[str(uuid.uuid4())],
    )
    async with factory() as db:
        preview = await _confirmation_preview(db, _user("alice"), _send(draft_id))

    assert preview["to"] == ["buyer@example.test"]
    assert preview["subject"] == "Прайс-лист"
    assert preview["body_text"].startswith("Добрый день!")
    assert len(preview["body_text"]) == 2000 and preview["body_truncated"] is True
    assert preview["attachment_count"] == 1
    assert preview["digest_matches"] is True


@pytest.mark.asyncio
async def test_changed_draft_is_flagged(test_engine):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    draft_id = await _draft(factory, created_by_sub="alice", subject="s", content_digest="d2")
    async with factory() as db:
        preview = await _confirmation_preview(db, _user("alice"), _send(draft_id, "d1"))
    assert preview["digest_matches"] is False


@pytest.mark.asyncio
async def test_other_users_draft_and_other_actions_give_no_preview(test_engine):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    draft_id = await _draft(factory, created_by_sub="alice", subject="secret")
    async with factory() as db:
        assert await _confirmation_preview(db, _user("mallory"), _send(draft_id)) is None
        assert await _confirmation_preview(db, _user("alice"), _send(uuid.uuid4())) is None
        assert await _confirmation_preview(db, _user("alice"), _send("not-a-uuid")) is None
        other = {"tool": "invoices", "args": {"action": "approve", "invoice_id": "x"}}
        assert await _confirmation_preview(db, _user("alice"), other) is None
        assert await _confirmation_preview(db, _user("alice"), None) is None
