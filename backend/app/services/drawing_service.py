"""Centralized drawing creation and analysis service."""

from __future__ import annotations
import uuid
import structlog
from typing import Any

logger = structlog.get_logger()

DRAWING_EXTENSIONS = frozenset({"dxf", "dwg", "pdf", "step", "stp", "iges",
                                  "svg", "png", "jpg", "jpeg", "tiff", "bmp", "webp"})


async def create_and_analyze_drawing(
    *,
    file_bytes: bytes,
    filename: str,
    fmt: str,                         # file extension lower-cased
    db,                               # AsyncSession
    document_id: uuid.UUID | None = None,
    drawing_number: str | None = None,
    is_confidential: bool = True,
    allow_cloud: bool = False,
    max_views: int = 6,
    force_drawing_type: str | None = None,
    created_by: str = "user",
) -> tuple[Any, str | None]:
    """Create Drawing record, upload to MinIO, enqueue analysis.

    Returns (drawing, task_id).
    Raises nothing — logs warnings on MinIO/Celery failure.
    """
    from app.db.models import Drawing, DrawingStatus

    drawing = Drawing(
        document_id=document_id,
        drawing_number=drawing_number,
        filename=filename,
        format=fmt,
        is_confidential=is_confidential,
        status=DrawingStatus.uploaded,
    )
    db.add(drawing)
    await db.flush()  # get drawing.id

    # Upload to MinIO
    storage_path = await _upload_drawing_to_minio(file_bytes, filename, str(drawing.id))
    if storage_path:
        drawing.metadata_ = {
            **(drawing.metadata_ or {}),
            "storage_path": storage_path,
            "created_by": created_by,
        }
    if document_id:
        drawing.metadata_ = {**(drawing.metadata_ or {}), "from_document": True}

    await db.commit()
    await db.refresh(drawing)

    # Enqueue Celery analysis
    task_id: str | None = None
    try:
        from app.tasks.drawing_analysis import analyze_drawing
        task = analyze_drawing.delay(
            str(drawing.id), None, allow_cloud, max_views, force_drawing_type
        )
        task_id = task.id
        drawing.celery_task_id = task_id
        await db.commit()
        logger.info("drawing_analysis_enqueued",
                    drawing_id=str(drawing.id), task_id=task_id, filename=filename)
    except Exception as exc:
        logger.warning("drawing_analysis_enqueue_failed",
                       drawing_id=str(drawing.id), error=str(exc))

    return drawing, task_id


async def _upload_drawing_to_minio(
    file_bytes: bytes, filename: str, drawing_id: str
) -> str | None:
    """Upload file bytes to MinIO under drawings/{drawing_id}/{filename}.
    Returns storage_path or None on failure.
    """
    try:
        from app.config import settings
        from minio import Minio
        import io

        client = Minio(
            settings.minio_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=settings.minio_secure,
        )
        bucket = settings.minio_bucket
        storage_path = f"drawings/{drawing_id}/{filename}"

        # Ensure bucket exists
        if not client.bucket_exists(bucket):
            client.make_bucket(bucket)

        client.put_object(
            bucket,
            storage_path,
            io.BytesIO(file_bytes),
            length=len(file_bytes),
        )
        return storage_path
    except Exception as exc:
        logger.warning("drawing_minio_upload_failed", filename=filename, error=str(exc))
        return None
