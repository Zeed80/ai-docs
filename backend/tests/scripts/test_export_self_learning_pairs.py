"""export_self_learning_pairs.py: only generations with a human correction
beyond revision 0 are exported; auto-only generations are skipped."""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "scripts"))


@pytest.fixture
def fake_storage(monkeypatch):
    blobs: dict[str, bytes] = {}

    def _upload(content: bytes, path: str, content_type: str = "application/octet-stream") -> str:
        blobs[path] = content
        return path

    def _download(path: str) -> bytes:
        return blobs[path]

    monkeypatch.setattr("app.storage.upload_file", _upload)
    monkeypatch.setattr("app.storage.download_file", _download)
    return blobs


def _ir_json(entity_count: int) -> bytes:
    from app.ai.cad_ir import CadIR, SourceInfo
    from app.ai.cad_ir.schema import Point, Segment

    ir = CadIR(
        source=SourceInfo(image_width=200, image_height=100),
        entities=[Segment(p1=Point(x=0, y=i * 5), p2=Point(x=100, y=i * 5)) for i in range(entity_count)],
    )
    return ir.model_dump_json().encode("utf-8")


@pytest.mark.asyncio
async def test_export_skips_auto_only_and_exports_corrected(db_session, fake_storage, monkeypatch, tmp_path):
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.db.models import CadIrRevision, ImageGeneration, ImageGenStatus
    import export_self_learning_pairs as export_mod

    conn = db_session.bind

    def _factory():
        return AsyncSession(bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint")

    monkeypatch.setattr("app.db.session._get_session_factory", lambda: _factory)

    fake_storage["image-gen-src/auto_only/src.png"] = b"src-a"
    auto_only = ImageGeneration(
        owner_sub=None, operation="vectorize", status=ImageGenStatus.done,
        params={}, source_image_paths=["image-gen-src/auto_only/src.png"],
    )
    db_session.add(auto_only)
    await db_session.flush()
    fake_storage["ir/auto_only_r0.json"] = _ir_json(3)
    db_session.add(CadIrRevision(
        generation_id=auto_only.id, revision=0, ir_path="ir/auto_only_r0.json", origin="auto",
    ))

    fake_storage["image-gen-src/corrected/src.png"] = b"src-b"
    corrected = ImageGeneration(
        owner_sub=None, operation="vectorize", status=ImageGenStatus.done,
        params={}, source_image_paths=["image-gen-src/corrected/src.png"],
    )
    db_session.add(corrected)
    await db_session.flush()
    fake_storage["ir/corrected_r0.json"] = _ir_json(3)
    fake_storage["ir/corrected_r1.json"] = _ir_json(5)  # human added 2 entities
    db_session.add(CadIrRevision(
        generation_id=corrected.id, revision=0, ir_path="ir/corrected_r0.json", origin="auto",
    ))
    db_session.add(CadIrRevision(
        generation_id=corrected.id, revision=1, ir_path="ir/corrected_r1.json", origin="editor",
    ))
    await db_session.commit()

    out_dir = tmp_path / "export"
    rc = await export_mod._run(out_dir, min_revision=1)
    assert rc == 0

    manifest = [json.loads(line) for line in (out_dir / "self_learning.jsonl").read_text().splitlines()]
    assert len(manifest) == 1
    row = manifest[0]
    assert row["generation_id"] == str(corrected.id)
    assert row["revisions_span"] == 1
    assert row["correction_origin"] == "editor"
    assert pathlib.Path(row["image"]).read_bytes() == b"src-b"

    import numpy as np

    seq = np.load(row["sequence"])
    assert seq.shape[0] >= 5  # 5 segments + EOS row
