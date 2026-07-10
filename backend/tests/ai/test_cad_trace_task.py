"""End-to-end run of the vectorize Celery pipeline body (no broker):
synthetic scan → recognize → IR revision 0 → renders → done record."""

from __future__ import annotations

import pytest

pytest.importorskip("cv2")


@pytest.fixture
def fake_storage(monkeypatch):
    blobs: dict[str, bytes] = {}

    def _upload(content: bytes, path: str, content_type: str = "application/octet-stream") -> str:
        blobs[path] = content
        return path

    def _download(path: str) -> bytes:
        return blobs[path]

    for mod in ("app.storage", "app.services.cad_ir_store"):
        monkeypatch.setattr(f"{mod}.upload_file", _upload)
        monkeypatch.setattr(f"{mod}.download_file", _download)
    return blobs


def _scan_png() -> bytes:
    import cv2
    import numpy as np

    img = np.full((400, 500), 255, dtype=np.uint8)
    cv2.line(img, (50, 60), (450, 60), 0, 4)
    cv2.line(img, (50, 60), (50, 340), 0, 4)
    cv2.line(img, (450, 60), (450, 340), 0, 4)
    cv2.line(img, (50, 340), (450, 340), 0, 4)
    cv2.circle(img, (250, 200), 70, 0, 2)
    ok, buf = cv2.imencode(".png", img)
    assert ok
    return buf.tobytes()


@pytest.mark.asyncio
async def test_cad_trace_run_end_to_end(db_session, fake_storage, monkeypatch):
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.db.models import CadIrRevision, ImageGeneration, ImageGenStatus
    from app.tasks import cad_trace

    conn = db_session.bind

    def _factory():
        return AsyncSession(
            bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )

    monkeypatch.setattr("app.db.session._get_session_factory", lambda: _factory)

    fake_storage["image-gen-src/test/scan.png"] = _scan_png()
    gen = ImageGeneration(
        owner_sub=None,
        operation="vectorize",
        status=ImageGenStatus.queued,
        params={},
        source_image_paths=["image-gen-src/test/scan.png"],
    )
    db_session.add(gen)
    await db_session.commit()

    result = await cad_trace._run(str(gen.id), task_id=None)
    assert result.get("ok"), result
    assert result["entities"] > 0

    await db_session.refresh(gen)
    assert gen.status == ImageGenStatus.done
    params = gen.params
    assert params["dxf_path"] in fake_storage
    assert params["svg_path"] in fake_storage
    assert params["ir_path"] in fake_storage
    assert gen.result_path in fake_storage
    assert params["recognizer_used"] == "cv"
    validation = params["validation"]
    assert validation["coverage_recall"] is not None

    # DXF artifact is CAD-readable and contains the recognized circle.
    import io

    import ezdxf

    doc = ezdxf.read(io.StringIO(fake_storage[params["dxf_path"]].decode("utf-8")))
    types = {e.dxftype() for e in doc.modelspace()}
    assert "CIRCLE" in types
    # The frame comes back as separate LINEs or one closed LWPOLYLINE —
    # both are valid CAD geometry for a rectangle.
    assert types & {"LINE", "LWPOLYLINE"}

    rev = (
        await db_session.execute(
            select(CadIrRevision).where(CadIrRevision.generation_id == gen.id)
        )
    ).scalar_one()
    assert rev.revision == 0
    assert rev.origin == "auto"
    assert rev.summary["counts"]["circle"] >= 1


@pytest.mark.asyncio
async def test_cad_trace_flags_diffusion_added_ink(db_session, fake_storage, monkeypatch):
    """Vectorizing a diffusion (cleanup) result compares against the ORIGINAL
    photo: a stroke the diffusion invented must be flagged, not trusted."""
    import cv2
    import numpy as np

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.db.models import ImageGeneration, ImageGenStatus
    from app.services import cad_ir_store
    from app.tasks import cad_trace

    conn = db_session.bind

    def _factory():
        return AsyncSession(
            bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )

    monkeypatch.setattr("app.db.session._get_session_factory", lambda: _factory)
    # Tesseract falsely "reads" bare synthetic lines as text and its exclusion
    # boxes would reroute the hallucinated stroke into the raster path (which
    # has its own orphan-region issue). Pin OCR off to test the entity path.
    monkeypatch.setattr("app.tasks.cad_trace._ocr_text_entities", lambda _b: ([], []))

    def _sheet(extra: bool) -> bytes:
        img = np.full((400, 500), 255, dtype=np.uint8)
        cv2.line(img, (50, 60), (450, 60), 0, 4)
        cv2.circle(img, (250, 220), 70, 0, 2)
        if extra:
            cv2.line(img, (150, 120), (350, 120), 0, 4)  # hallucinated
        ok, buf = cv2.imencode(".png", img)
        assert ok
        return buf.tobytes()

    fake_storage["image-gen-src/test/photo.png"] = _sheet(extra=False)
    fake_storage["image-gen/test/diffused.png"] = _sheet(extra=True)

    parent = ImageGeneration(
        owner_sub=None,
        operation="cleanup",
        status=ImageGenStatus.done,
        params={},
        source_image_paths=["image-gen-src/test/photo.png"],
        result_path="image-gen/test/diffused.png",
    )
    db_session.add(parent)
    await db_session.flush()

    gen = ImageGeneration(
        owner_sub=None,
        operation="vectorize",
        status=ImageGenStatus.queued,
        params={"source_generation_id": str(parent.id)},
        source_image_paths=["image-gen/test/diffused.png"],
    )
    db_session.add(gen)
    await db_session.commit()

    result = await cad_trace._run(str(gen.id), task_id=None)
    assert result.get("ok"), result

    await db_session.refresh(gen)
    revision = await cad_ir_store.latest_revision(db_session, gen.id)
    ir = cad_ir_store.load_ir(revision)
    codes = {i.code for i in ir.validation.issues}
    assert "DIFFUSION_ADDED_INK" in codes, codes
    reasons = {r.reason for r in ir.review if not r.resolved}
    assert "diffusion_modified" in reasons
    # flagged entities stayed at the bottom of the ladder
    flagged_ids = next(
        i.entity_ids for i in ir.validation.issues if i.code == "DIFFUSION_ADDED_INK"
    )
    for eid in flagged_ids:
        assert ir.entity_by_id(eid).assurance == "inferred"


@pytest.mark.asyncio
async def test_cad_trace_declines_dense_sheet(db_session, fake_storage, monkeypatch):
    import cv2
    import numpy as np

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.db.models import ImageGeneration, ImageGenStatus
    from app.tasks import cad_trace

    conn = db_session.bind

    def _factory():
        return AsyncSession(
            bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )

    monkeypatch.setattr("app.db.session._get_session_factory", lambda: _factory)

    solid = np.zeros((200, 200), dtype=np.uint8)  # fully black sheet
    ok, buf = cv2.imencode(".png", solid)
    fake_storage["image-gen-src/test/black.png"] = buf.tobytes()
    gen = ImageGeneration(
        owner_sub=None,
        operation="vectorize",
        status=ImageGenStatus.queued,
        params={},
        source_image_paths=["image-gen-src/test/black.png"],
    )
    db_session.add(gen)
    await db_session.commit()

    result = await cad_trace._run(str(gen.id), task_id=None)
    assert "error" in result
    await db_session.refresh(gen)
    assert gen.status == ImageGenStatus.failed
    assert "Очистка" in (gen.error or "")


@pytest.mark.asyncio
async def test_cad_trace_vlm_enrichment_promotes_thread_reading(db_session, fake_storage, monkeypatch):
    """params.vlm_dimensions=true end-to-end: a mocked VLM call reads a
    thread designation off a low-confidence OCR text crop; Ф4.2's
    cross-check (parse_thread validity) decisively promotes it to
    constraint_validated with no human involved."""
    import cv2
    import numpy as np
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.db.models import ImageGeneration, ImageGenStatus
    from app.services import cad_ir_store
    from app.tasks import cad_trace

    conn = db_session.bind

    def _factory():
        return AsyncSession(
            bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )

    monkeypatch.setattr("app.db.session._get_session_factory", lambda: _factory)

    # Force OCR to report a low-confidence text region so Ф4.1 escalates it.
    from app.ai.text_preserve import TextRegion

    monkeypatch.setattr(
        "app.ai.text_preserve.detect_text_regions",
        lambda _b: [TextRegion(text="M1B", x=60, y=60, w=40, h=16, conf=40.0)],
    )

    async def _fake_vlm(_crop_bytes, **_kw):
        return [
            {"text": "M18", "value_mm": None, "kind": "thread", "tolerance": None, "confidence": 0.6},
            {"text": "M1B", "value_mm": None, "kind": "unclear", "tolerance": None, "confidence": 0.4},
        ]

    monkeypatch.setattr("app.ai.vlm_dimensions.read_crop_hypotheses", _fake_vlm)

    img = np.full((300, 400), 255, dtype=np.uint8)
    cv2.line(img, (50, 60), (350, 60), 0, 4)
    cv2.line(img, (50, 60), (50, 260), 0, 4)
    ok, buf = cv2.imencode(".png", img)
    assert ok
    fake_storage["image-gen-src/test/thread.png"] = buf.tobytes()

    gen = ImageGeneration(
        owner_sub=None,
        operation="vectorize",
        status=ImageGenStatus.queued,
        params={"vlm_dimensions": True},
        source_image_paths=["image-gen-src/test/thread.png"],
    )
    db_session.add(gen)
    await db_session.commit()

    result = await cad_trace._run(str(gen.id), task_id=None)
    assert result.get("ok"), result

    revision = await cad_ir_store.latest_revision(db_session, gen.id)
    ir = cad_ir_store.load_ir(revision)
    text_entities = [e for e in ir.entities if e.type == "text"]
    assert text_entities, "OCR text entity should have been created"
    promoted = [e for e in text_entities if e.text == "M18"]
    assert promoted, [e.text for e in text_entities]
    assert promoted[0].assurance == "constraint_validated"
    assert promoted[0].origin == "vlm"
