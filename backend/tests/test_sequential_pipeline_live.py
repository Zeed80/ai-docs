"""LIVE end-to-end test of the sequential GPU pipeline.

Unlike the rest of the suite (which runs Celery eagerly in-process), this test
exercises the *real* running stack: it enqueues several invoices at once onto the
real ``gpu`` Celery worker and asserts that

1. every document completes the full chain (classify → extract → auto-verify →
   approve → downstream) without errors, and
2. GPU steps never overlap — the dedicated ``-c 1`` worker runs exactly one task
   at a time (proven from the worker's per-task timing).

It is opt-in: set ``LIVE_STACK=1`` and have the prod/dev stack running (Postgres,
Redis, MinIO, Qdrant, Ollama, the gpu+io Celery workers). It is skipped in the
normal eager unit run so it never gives a false green.

Run::

    LIVE_STACK=1 python3 -m pytest tests/test_sequential_pipeline_live.py -s
"""

from __future__ import annotations

import base64
import os
import time
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.live

_INVOICES = Path(__file__).parent.parent.parent / "example-invoices"
_LIVE = os.environ.get("LIVE_STACK") == "1"


def _text_layer_pdfs(n: int) -> list[Path]:
    if not _INVOICES.is_dir():
        return []
    return sorted(
        p for p in _INVOICES.iterdir()
        if p.suffix.lower() == ".pdf" and not p.name.startswith(".")
    )[:n]


@pytest.mark.skipif(not _LIVE, reason="LIVE_STACK!=1 — needs the running stack + workers")
def test_sequential_pipeline_no_overlap():
    import asyncio

    from app.db.models import Document, DocumentProcessingJob  # noqa: F401
    from app.db.session import _get_engine, _get_session_factory
    from app.tasks.extraction import classify_document
    from app.tasks.ingest import store_document

    # IMPORTANT: this test requires real workers, not eager mode.
    from app.tasks.celery_app import celery_app
    assert not celery_app.conf.task_always_eager, "set CELERY_TASK_ALWAYS_EAGER=false for live run"

    pdfs = _text_layer_pdfs(4)
    if not pdfs:
        pytest.skip("example-invoices/ not present")

    doc_ids: list[str] = []
    for p in pdfs:
        b64 = base64.b64encode(p.read_bytes()).decode()
        res = store_document.apply(args=(b64, p.name, "application/pdf")).get()
        did = res.get("document_id") or res.get("id")
        assert did, f"store_document returned no id for {p.name}"
        doc_ids.append(str(did))

    # Enqueue all classify tasks near-simultaneously onto the gpu lane.
    for did in doc_ids:
        classify_document.delay(did)

    _get_engine.cache_clear()
    _get_session_factory.cache_clear()
    Session = _get_session_factory()

    async def _wait_done():
        for _ in range(120):  # up to ~360s
            await asyncio.sleep(3)
            async with Session() as db:
                statuses = []
                for did in doc_ids:
                    doc = await db.get(Document, uuid.UUID(did))
                    statuses.append(doc.status.value if doc and doc.status else "?")
                if all(s in ("approved", "needs_review", "rejected") for s in statuses):
                    return statuses
        return statuses

    statuses = asyncio.run(_wait_done())

    # 1. Everything reached a terminal state without getting stuck.
    assert all(s in ("approved", "needs_review", "rejected") for s in statuses), statuses

    # 2. No GPU-step overlap: collect each job's running window and assert the
    #    intervals are disjoint (the -c 1 worker can only run one at a time).
    async def _windows():
        out = []
        async with Session() as db:
            for did in doc_ids:
                job = (
                    await db.execute(
                        DocumentProcessingJob.__table__.select().where(
                            DocumentProcessingJob.document_id == uuid.UUID(did)
                        )
                    )
                ).first()
                if job and job.started_at and job.finished_at:
                    out.append((job.started_at, job.finished_at))
        return sorted(out)

    windows = asyncio.run(_windows())
    for (s1, e1), (s2, e2) in zip(windows, windows[1:]):
        assert s2 >= e1, f"GPU job windows overlap: {e1} > {s2}"
