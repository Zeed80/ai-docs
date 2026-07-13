"""Persistence + deterministic re-render of CAD IR revisions.

IR JSON lives in MinIO (megabytes on big sheets); ``cad_ir_revisions`` rows
track history. Every save re-renders PNG/SVG/DXF from the IR — renders are
pure functions of the IR, so UI edits (0 LLM) and pipeline output go through
the same door. A cached DWG is dropped on every save: it derives from the
DXF and is re-converted lazily on next download.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.cad_ir import CadIR
from app.ai.cad_ir.dxf_render import render_ir_to_dxf
from app.ai.cad_ir.png_render import render_ir_to_png
from app.ai.cad_ir.svg_render import render_ir_to_svg
from app.db.models import CadIrRevision, ImageGeneration
from app.storage import delete_file, download_file, upload_file

logger = structlog.get_logger()

_PREFIX = "image-gen"


def _base_path(gen: ImageGeneration) -> str:
    return f"{_PREFIX}/{gen.owner_sub or 'shared'}/{gen.id}"


async def latest_revision(db: AsyncSession, generation_id: uuid.UUID) -> CadIrRevision | None:
    return (
        await db.execute(
            select(CadIrRevision)
            .where(CadIrRevision.generation_id == generation_id)
            .order_by(CadIrRevision.revision.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


def load_ir(revision: CadIrRevision) -> CadIR:
    return CadIR.model_validate_json(download_file(revision.ir_path))


def _summary(ir: CadIR) -> dict[str, Any]:
    return {
        "counts": ir.counts(),
        "issues": len(ir.validation.issues),
        "errors": len(ir.validation.blocking),
        "review_pending": sum(1 for r in ir.review if not r.resolved),
        "coverage_recall": ir.validation.coverage_recall,
        "coverage_precision": ir.validation.coverage_precision,
        "scale": ir.scale,
        "scale_source": ir.scale_source,
        "recognizer_used": ir.recognizer_used,
    }


def _load_keep_raster(gen: ImageGeneration):
    """Raster passthrough regions (solid fills, preserved text ink) survive
    across revisions as a stored mask — an editor patch must not silently
    drop them from the preview."""
    path = (gen.params or {}).get("keep_raster_path")
    if not path:
        return None
    try:
        import cv2
        import numpy as np

        data = download_file(path)
        img = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
        return img > 127
    except Exception as exc:  # noqa: BLE001
        logger.warning("keep_raster_load_failed", generation_id=str(gen.id), error=str(exc))
        return None


def _store_keep_raster(gen: ImageGeneration, keep_raster: Any, base: str) -> str | None:
    try:
        import cv2
        import numpy as np

        mask = (np.asarray(keep_raster).astype(np.uint8)) * 255
        ok, buf = cv2.imencode(".png", mask)
        if not ok:
            return None
        path = f"{base}_keep.png"
        upload_file(buf.tobytes(), path, "image/png")
        return path
    except Exception as exc:  # noqa: BLE001
        logger.warning("keep_raster_store_failed", generation_id=str(gen.id), error=str(exc))
        return None


async def save_revision(
    db: AsyncSession,
    gen: ImageGeneration,
    ir: CadIR,
    *,
    origin: str,
    created_by: str | None,
    keep_raster: Any | None = None,
    thin_px: int | None = None,
    thick_px: int | None = None,
    make_thumbnail: bool = True,
) -> CadIrRevision:
    """Persist a new IR revision and regenerate every derived artifact.

    Does not commit — the caller owns the transaction. On follow-up
    revisions (review/editor patches) the raster mask and stroke widths are
    restored from what the pipeline stored with revision 0.
    """
    # Serialize concurrent revision-number allocation for the SAME
    # generation: without this row lock, two concurrent PATCH/revert/
    # full-check calls could both read the same latest_revision and race to
    # insert the same next revision number, one of them hitting the
    # (generation_id, revision) unique constraint as a raw IntegrityError
    # instead of a clean "someone else is editing this" — a real gap the
    # single-user common case never exercises, but agent+human editing the
    # same drawing at once genuinely can. Requires gen's row to already
    # exist (true at every call site: callers always flush/commit the
    # ImageGeneration row before calling save_revision).
    await db.execute(
        select(ImageGeneration.id).where(ImageGeneration.id == gen.id).with_for_update()
    )
    base = _base_path(gen)
    prev = await latest_revision(db, gen.id)
    revision = 0 if prev is None else prev.revision + 1

    params_in = dict(gen.params or {})
    if keep_raster is None:
        keep_raster = _load_keep_raster(gen)
        keep_raster_path = params_in.get("keep_raster_path")
    else:
        keep_raster_path = _store_keep_raster(gen, keep_raster, base)
    thin_px = thin_px if thin_px is not None else int(params_in.get("render_thin_px") or 1)
    thick_px = thick_px if thick_px is not None else int(params_in.get("render_thick_px") or 2)

    # Track what we've actually put in MinIO so a failure partway through
    # this function (a rendering bug, a malformed IR that slipped past
    # validation) can be cleaned up instead of leaving orphaned blobs with
    # no DB row pointing at them. Doesn't cover the caller's OWN commit
    # failing after this function returns successfully — that would need a
    # real two-phase commit, which storage + Postgres don't share — but it
    # closes the much larger window of "anything in here raised".
    uploaded_paths: list[str] = []

    def _tracked_upload(content: bytes, path: str, content_type: str) -> str:
        result = upload_file(content, path, content_type)
        uploaded_paths.append(path)
        return result

    try:
        ir_path = f"{base}_ir_r{revision}.json"
        ir_bytes = ir.model_dump_json().encode("utf-8")
        _tracked_upload(ir_bytes, ir_path, "application/json")

        png = render_ir_to_png(ir, keep_raster=keep_raster, thin_px=thin_px, thick_px=thick_px)
        svg = render_ir_to_svg(ir)
        dxf = render_ir_to_dxf(ir)
        _tracked_upload(png, f"{base}.png", "image/png")
        _tracked_upload(svg, f"{base}.svg", "image/svg+xml")
        _tracked_upload(dxf, f"{base}.dxf", "application/dxf")
    except Exception:
        for path in uploaded_paths:
            try:
                delete_file(path)
            except Exception as cleanup_exc:  # noqa: BLE001 — best-effort, don't mask the original error
                logger.warning("cad_ir_orphan_cleanup_failed", path=path, error=str(cleanup_exc))
        raise

    row = CadIrRevision(
        generation_id=gen.id,
        revision=revision,
        ir_path=ir_path,
        created_by=created_by,
        origin=origin,
        summary=_summary(ir),
        ir_sha256=hashlib.sha256(ir_bytes).hexdigest(),
        artifact_hashes={
            "png": hashlib.sha256(png).hexdigest(),
            "svg": hashlib.sha256(svg).hexdigest(),
            "dxf": hashlib.sha256(dxf).hexdigest(),
        },
    )
    db.add(row)

    thumb_path = None
    if make_thumbnail:
        try:
            from app.tasks.image_generation import _make_thumbnail

            thumb = _make_thumbnail(png)
            if thumb:
                thumb_path = f"{base}_thumb.png"
                upload_file(thumb, thumb_path, "image/png")
        except Exception as exc:  # noqa: BLE001 — preview nicety only
            logger.warning("cad_ir_thumbnail_failed", generation_id=str(gen.id), error=str(exc))

    gen.result_path = f"{base}.png"
    if thumb_path:
        gen.thumbnail_path = thumb_path
    params = dict(gen.params or {})
    params.update(
        {
            "ir_path": ir_path,
            "ir_revision": revision,
            "keep_raster_path": keep_raster_path,
            "render_thin_px": thin_px,
            "render_thick_px": thick_px,
            "svg_path": f"{base}.svg",
            "dxf_path": f"{base}.dxf",
            "scale": ir.scale,
            "recognizer_used": ir.recognizer_used,
            "validation": {
                "codes": sorted({i.code for i in ir.validation.issues}),
                "errors": len(ir.validation.blocking),
                "coverage_recall": ir.validation.coverage_recall,
                "coverage_precision": ir.validation.coverage_precision,
            },
            "review_pending": sum(1 for r in ir.review if not r.resolved),
        }
    )
    # All compiled CAD files derive from this exact IR revision. They must not
    # survive an edit under the same generation id.
    stale_keys = (
        "dwg_path", "pdf_path", "step_path", "fcstd_path", "stl_path", "cad_report_path",
    )
    for key in stale_keys:
        stale_path = params.pop(key, None)
        if stale_path:
            try:
                delete_file(stale_path)
            except Exception as exc:  # noqa: BLE001
                logger.warning("cad_derived_artifact_cleanup_failed", path=stale_path, error=str(exc))
    params.pop("cad_artifact_revision", None)
    params.pop("cad_candidate_index", None)
    params.pop("cad_feature_overrides", None)
    params.pop("cad_added_features", None)
    params.pop("cad_feature_tree", None)
    params.pop("cad_report", None)
    gen.params = params

    logger.info(
        "cad_ir_revision_saved",
        generation_id=str(gen.id),
        revision=revision,
        origin=origin,
        **{k: v for k, v in row.summary.items() if k in ("issues", "errors", "review_pending")},
    )
    return row
