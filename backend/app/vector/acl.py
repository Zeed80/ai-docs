"""E41: document rights inside the vector index.

Every point that carries a document's text (the document vector, its chunks
and evidence spans, in every ``documents__*`` collection) has an ``acl``
payload: owner, department, whether the document type is company-wide, and
a version. A restricted user's search filters on it inside Qdrant, so other
departments' fragments neither crowd out the user's own top-k nor reach a
reranker or a model.

The index is not the authority. Callers still recheck every hit against the
database (``GraphAccess.visible_documents``) before showing text, so a stale
``acl`` can cost recall, never leak. Points written before this had no
``acl``: a restricted search does not match them (quarantine) until
``sync_document_acl`` stamps them — at startup, nightly, and after any change
of a document's owner, department or type (ORM event → task after commit).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from typing import Any

import structlog

logger = structlog.get_logger()

ACL_VERSION = 1
DOCUMENT_COLLECTION_PREFIX = "documents"


def acl_payload(owner_sub: str | None, department_id: Any, doc_type: Any) -> dict[str, Any]:
    from app.domain.access import SHARED_DOCUMENT_TYPES

    kind = getattr(doc_type, "value", doc_type)
    return {
        "acl": {
            "owner": owner_sub or "",
            "dept": str(department_id) if department_id else "",
            "shared": bool(kind in SHARED_DOCUMENT_TYPES),
            "v": ACL_VERSION,
        }
    }


def document_acl_payload(doc) -> dict[str, Any]:
    return acl_payload(doc.owner_sub, doc.department_id, doc.doc_type)


async def acl_filter(db, user) -> Any | None:
    """Qdrant condition for ``user``, or None when they see every document."""
    from qdrant_client.models import FieldCondition, Filter, MatchAny, MatchValue

    from app.domain.access import _is_unrestricted, _visible_department_ids

    if _is_unrestricted(user):
        return None
    should = [
        FieldCondition(key="acl.shared", match=MatchValue(value=True)),
        Filter(
            must=[
                FieldCondition(key="acl.owner", match=MatchValue(value="")),
                FieldCondition(key="acl.dept", match=MatchValue(value="")),
            ]
        ),
    ]
    if user.sub:
        should.append(FieldCondition(key="acl.owner", match=MatchValue(value=user.sub)))
    departments = [str(d) for d in await _visible_department_ids(db, user)]
    if departments:
        should.append(FieldCondition(key="acl.dept", match=MatchAny(any=departments)))
    return Filter(should=should)


def _document_collections(client) -> list[str]:
    return [
        c.name
        for c in client.get_collections().collections
        if c.name == DOCUMENT_COLLECTION_PREFIX
        or c.name.startswith(DOCUMENT_COLLECTION_PREFIX + "__")
    ]


def stamp_documents(rows: Iterable[tuple[uuid.UUID, str | None, Any, Any]]) -> int:
    """Write ``acl`` onto every point of each document; returns documents done."""
    from qdrant_client.models import FieldCondition, Filter, MatchValue

    from app.vector.qdrant_store import get_client

    client = get_client()
    collections = _document_collections(client)
    done = 0
    for doc_id, owner_sub, department_id, doc_type in rows:
        payload = acl_payload(owner_sub, department_id, doc_type)
        selector = Filter(
            should=[
                FieldCondition(key="doc_id", match=MatchValue(value=str(doc_id))),
                FieldCondition(key="document_id", match=MatchValue(value=str(doc_id))),
            ]
        )
        for name in collections:
            client.set_payload(collection_name=name, payload=payload, points=selector)
        done += 1
    return done


def sync_document_acl(document_ids: list[str] | None = None) -> int:
    """Restamp the given documents (all when None) from the database."""
    from sqlalchemy import select

    from app.db.models import Document
    from app.db.sync_session import sync_session

    with sync_session() as db:
        query = select(Document.id, Document.owner_sub, Document.department_id, Document.doc_type)
        if document_ids is not None:
            query = query.where(Document.id.in_([uuid.UUID(str(i)) for i in document_ids]))
        rows = db.execute(query).all()
    done = stamp_documents(rows)
    purged = 0
    if document_ids is None:
        purged = purge_orphans({str(row[0]) for row in rows})
    logger.info(
        "vector_acl_synced", documents=done, scoped=document_ids is not None, orphans=purged
    )
    return done


def purge_orphans(existing: set[str]) -> int:
    """Delete points of documents that no longer exist (missed deletions)."""
    from qdrant_client.models import FieldCondition, Filter, MatchAny

    from app.vector.qdrant_store import get_client

    client = get_client()
    removed = 0
    for name in _document_collections(client):
        orphan_ids: set[str] = set()
        offset = None
        while True:
            points, offset = client.scroll(
                name, limit=512, offset=offset, with_payload=["doc_id", "document_id"]
            )
            for point in points:
                payload = point.payload or {}
                doc_id = payload.get("document_id") or payload.get("doc_id")
                if doc_id and doc_id not in existing:
                    orphan_ids.add(doc_id)
            if offset is None:
                break
        if orphan_ids:
            ids = sorted(orphan_ids)
            selector = Filter(
                should=[
                    FieldCondition(key="doc_id", match=MatchAny(any=ids)),
                    FieldCondition(key="document_id", match=MatchAny(any=ids)),
                ]
            )
            before = client.count(name).count
            client.delete(collection_name=name, points_selector=selector)
            removed += before - client.count(name).count
    return removed


# ── change capture ──────────────────────────────────────────────────────────

_PENDING_KEY = "aiw_vector_acl_pending"
_WATCHED = ("owner_sub", "department_id", "doc_type")


def _on_flush(session, _flush_context, _instances) -> None:
    from sqlalchemy import inspect

    from app.db.models import Document

    for obj in session.dirty:
        if not isinstance(obj, Document):
            continue
        state = inspect(obj)
        if any(state.attrs[name].history.has_changes() for name in _WATCHED):
            session.info.setdefault(_PENDING_KEY, set()).add(str(obj.id))


def _after_commit(session) -> None:
    pending = session.info.pop(_PENDING_KEY, None)
    if not pending:
        return
    try:
        from app.tasks.vector_acl import sync_document_acl_task

        sync_document_acl_task.delay(sorted(pending))
    except Exception as exc:  # noqa: BLE001 — the nightly sync still catches it
        logger.warning("vector_acl_enqueue_failed", documents=len(pending), error=str(exc))


def _after_rollback(session) -> None:
    session.info.pop(_PENDING_KEY, None)


_installed = False


def install_change_capture() -> None:
    """Listen on every ORM session; idempotent."""
    global _installed
    if _installed:
        return
    from sqlalchemy import event
    from sqlalchemy.orm import Session

    event.listen(Session, "before_flush", _on_flush)
    event.listen(Session, "after_commit", _after_commit)
    event.listen(Session, "after_rollback", _after_rollback)
    _installed = True


def uninstall_change_capture() -> None:
    global _installed
    if not _installed:
        return
    from sqlalchemy import event
    from sqlalchemy.orm import Session

    event.remove(Session, "before_flush", _on_flush)
    event.remove(Session, "after_commit", _after_commit)
    event.remove(Session, "after_rollback", _after_rollback)
    _installed = False
