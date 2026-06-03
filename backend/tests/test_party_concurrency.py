"""Regression test for the _upsert_party concurrency race.

Several Celery workers process invoices from the SAME counterparty in parallel.
Before the fix, check-then-insert with no unique constraint created duplicate
Party rows, which then crashed INN lookups with MultipleResultsFound.

This drives real concurrent sync sessions against the test Postgres and asserts
exactly one Party survives and no worker errors out.
"""

from __future__ import annotations

import threading

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.db.models import Party


@pytest.mark.asyncio
async def test_upsert_party_concurrent_single_row(test_engine):
    sync_url = test_engine.url.render_as_string(hide_password=False).replace(
        "+asyncpg", "+psycopg2"
    )
    if not sync_url.startswith("postgresql"):
        pytest.skip("concurrency test requires PostgreSQL")
    try:
        sync_engine = create_engine(sync_url, pool_size=12, max_overflow=4)
        sync_engine.connect().close()
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"sync engine unavailable: {exc}")

    from app.tasks.extraction import _upsert_party

    inn = "7707083893"
    with Session(sync_engine) as s:
        s.query(Party).filter(Party.inn == inn).delete()
        s.commit()

    results: list[str] = []
    errors: list[Exception] = []
    barrier = threading.Barrier(8)

    def worker(i: int) -> None:
        try:
            barrier.wait(timeout=10)  # maximise contention
            with Session(sync_engine) as s:
                pid = _upsert_party(
                    s, {"name": 'ООО "Конкурент"', "inn": inn, "kpp": "770701001"}, "supplier"
                )
                # Force a further flush in the SAME session (as
                # process_approved_document does when it then inserts the
                # Invoice) — this surfaces an orphaned pending INSERT left behind
                # by a failed savepoint, which would poison the transaction.
                s.flush()
                s.commit()
                results.append(str(pid))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"workers raised: {errors}"
    with Session(sync_engine) as s:
        count = s.execute(
            select(func.count()).select_from(Party).where(Party.inn == inn)
        ).scalar()
        s.query(Party).filter(Party.inn == inn).delete()
        s.commit()

    assert count == 1, f"expected 1 deduplicated party, found {count}"
    assert len(set(results)) == 1, f"workers resolved to different parties: {set(results)}"
    sync_engine.dispose()
