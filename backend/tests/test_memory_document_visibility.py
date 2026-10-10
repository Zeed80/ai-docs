"""E39: memory search returns a document's derived text only to whoever may
see that document. Graph nodes, chunks and evidence carried other
departments' snippets to any reader."""

from __future__ import annotations

import uuid

import pytest

from app.api.memory import _drop_invisible_document_hits
from app.auth.models import UserInfo, UserRole
from app.db.models import Department, Document, DocumentStatus
from app.domain.graph import MemorySearchHit


def _user(sub: str, role: UserRole) -> UserInfo:
    return UserInfo(sub=sub, email=f"{sub}@x", name=sub, preferred_username=sub, roles=[role])


def _hit(document_id) -> MemorySearchHit:
    return MemorySearchHit(
        kind="chunk",
        id=uuid.uuid4(),
        title="chunk",
        summary="secret text",
        score=1.0,
        source="vector",
        source_document_id=document_id,
    )


@pytest.mark.asyncio
async def test_another_owners_document_text_is_dropped(db_session):
    dept = Department(name=f"Bob dept {uuid.uuid4().hex[:6]}", code=uuid.uuid4().hex[:8])
    db_session.add(dept)
    await db_session.flush()
    bobs = Document(
        file_name="bob.pdf",
        file_hash=uuid.uuid4().hex,
        file_size=1,
        mime_type="application/pdf",
        storage_path="x",
        status=DocumentStatus.ingested,
        owner_sub="bob",
        department_id=dept.id,
    )
    legacy = Document(
        file_name="legacy.pdf",
        file_hash=uuid.uuid4().hex,
        file_size=1,
        mime_type="application/pdf",
        storage_path="y",
        status=DocumentStatus.ingested,
    )
    db_session.add_all([bobs, legacy])
    await db_session.flush()
    hits = [_hit(bobs.id), _hit(legacy.id), _hit(None)]

    alice = await _drop_invisible_document_hits(db_session, _user("alice", UserRole.viewer), hits)
    bob = await _drop_invisible_document_hits(db_session, _user("bob", UserRole.viewer), hits)
    admin = await _drop_invisible_document_hits(db_session, _user("root", UserRole.admin), hits)

    assert [h.source_document_id for h in alice] == [legacy.id, None]
    assert len(bob) == 3
    assert len(admin) == 3
