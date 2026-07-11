"""Regression test for fix #14: a render failure partway through
save_revision must not leave orphaned MinIO blobs with no DB row pointing
at them — everything uploaded during THIS call gets cleaned up before the
exception propagates."""

from __future__ import annotations

import pytest

from app.ai.cad_ir.schema import CadIR, Point, Segment, SourceInfo
from app.db.models import ImageGeneration, ImageGenStatus


@pytest.fixture
def fake_storage(monkeypatch):
    blobs: dict[str, bytes] = {}
    deleted: list[str] = []

    def _upload(content: bytes, path: str, content_type: str = "application/octet-stream") -> str:
        blobs[path] = content
        return path

    def _download(path: str) -> bytes:
        return blobs[path]

    def _delete(path: str) -> None:
        deleted.append(path)
        blobs.pop(path, None)

    monkeypatch.setattr("app.services.cad_ir_store.upload_file", _upload)
    monkeypatch.setattr("app.services.cad_ir_store.download_file", _download)
    monkeypatch.setattr("app.services.cad_ir_store.delete_file", _delete)
    return blobs, deleted


def _ir() -> CadIR:
    return CadIR(
        source=SourceInfo(image_width=100, image_height=100), scale=1.0,
        entities=[Segment(p1=Point(x=0, y=0), p2=Point(x=10, y=0))],
    )


@pytest.mark.asyncio
async def test_render_failure_cleans_up_already_uploaded_blobs(db_session, fake_storage, monkeypatch):
    blobs, deleted = fake_storage

    def _boom(ir):
        raise RuntimeError("simulated rendering bug")

    monkeypatch.setattr("app.services.cad_ir_store.render_ir_to_dxf", _boom)

    from app.services import cad_ir_store

    gen = ImageGeneration(
        owner_sub="u1", operation="vectorize", status=ImageGenStatus.done,
        params={}, source_image_paths=[],
    )
    db_session.add(gen)
    await db_session.flush()

    with pytest.raises(RuntimeError, match="simulated rendering bug"):
        await cad_ir_store.save_revision(db_session, gen, _ir(), origin="editor", created_by="u1")

    # The IR json was uploaded before rendering (which raised before ANY of
    # png/svg/dxf got uploaded, since all three renders happen before any
    # of them are uploaded) — it must be cleaned up, not left as an orphan.
    assert not blobs, f"orphaned blobs survived: {list(blobs)}"
    assert deleted == [f"image-gen/u1/{gen.id}_ir_r0.json"]


@pytest.mark.asyncio
async def test_successful_save_leaves_all_blobs_in_place(db_session, fake_storage):
    blobs, deleted = fake_storage

    from app.services import cad_ir_store

    gen = ImageGeneration(
        owner_sub="u1", operation="vectorize", status=ImageGenStatus.done,
        params={}, source_image_paths=[],
    )
    db_session.add(gen)
    await db_session.flush()

    row = await cad_ir_store.save_revision(db_session, gen, _ir(), origin="editor", created_by="u1")
    await db_session.commit()

    assert row.revision == 0
    assert deleted == []
    assert len(blobs) >= 4  # ir json + png + svg + dxf
