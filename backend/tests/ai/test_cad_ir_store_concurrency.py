"""Regression test for the cad_ir_store.save_revision revision-number race
(fix #13): two concurrent PATCH/revert/full-check calls on the SAME
generation used to be able to read the same latest_revision and race to
insert the same next revision number, one of them hitting the
(generation_id, revision) unique constraint as a raw IntegrityError.

Drives real concurrent async sessions against the test Postgres — a single
shared-connection session (the usual db_session fixture) can't reproduce a
race at all, since queries on one connection are inherently serialized.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.cad_ir.schema import CadIR, Segment, Point, SourceInfo
from app.db.models import CadIrRevision, ImageGeneration, ImageGenStatus


@pytest.mark.asyncio
async def test_concurrent_save_revision_does_not_collide(test_engine, monkeypatch):
    blobs: dict[str, bytes] = {}

    def _upload(content: bytes, path: str, content_type: str = "application/octet-stream") -> str:
        blobs[path] = content
        return path

    def _download(path: str) -> bytes:
        return blobs[path]

    monkeypatch.setattr("app.services.cad_ir_store.upload_file", _upload)
    monkeypatch.setattr("app.services.cad_ir_store.download_file", _download)

    from app.services import cad_ir_store

    gen_id = uuid.uuid4()
    async with AsyncSession(test_engine) as setup:
        setup.add(ImageGeneration(
            id=gen_id, owner_sub="race-test", operation="vectorize",
            status=ImageGenStatus.done, params={}, source_image_paths=[],
        ))
        await setup.commit()

    def _ir() -> CadIR:
        return CadIR(
            source=SourceInfo(image_width=100, image_height=100), scale=1.0,
            entities=[Segment(p1=Point(x=0, y=0), p2=Point(x=10, y=0))],
        )

    async def _save() -> int:
        async with AsyncSession(test_engine) as session:
            gen = await session.get(ImageGeneration, gen_id)
            row = await cad_ir_store.save_revision(
                session, gen, _ir(), origin="editor", created_by="race-test",
            )
            revision = row.revision  # read before commit expires the ORM object
            await session.commit()
            return revision

    results = await asyncio.gather(*(_save() for _ in range(5)), return_exceptions=True)

    errors = [r for r in results if isinstance(r, Exception)]
    assert not errors, f"concurrent save_revision calls raised: {errors}"

    revisions = sorted(results)
    assert revisions == list(range(5)), "each concurrent call must get a distinct, gapless revision number"

    async with AsyncSession(test_engine) as check:
        from sqlalchemy import select

        rows = (
            await check.execute(
                select(CadIrRevision.revision).where(CadIrRevision.generation_id == gen_id)
            )
        ).scalars().all()
        assert sorted(rows) == list(range(5))
