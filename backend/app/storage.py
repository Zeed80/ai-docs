"""MinIO object storage client — upload, download, presigned URLs."""

import io
from urllib.parse import urlparse

import structlog
from minio import Minio
from minio.error import S3Error

from app.config import settings

logger = structlog.get_logger()

_client: Minio | None = None


def get_minio_client() -> Minio:
    global _client
    if _client is None:
        _client = Minio(
            settings.minio_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=settings.minio_secure,
        )
        _ensure_bucket()
    return _client


def _ensure_bucket() -> None:
    """Create the default bucket if it doesn't exist."""
    client = _client
    if client is None:
        return
    bucket = settings.minio_bucket
    try:
        if not client.bucket_exists(bucket):
            client.make_bucket(bucket)
            logger.info("minio_bucket_created", bucket=bucket)
    except S3Error as e:
        logger.error("minio_bucket_check_failed", error=str(e))


def upload_file(
    content: bytes,
    storage_path: str,
    content_type: str = "application/octet-stream",
) -> str:
    """Upload file to MinIO. Returns the storage path."""
    client = get_minio_client()
    bucket = settings.minio_bucket

    client.put_object(
        bucket,
        storage_path,
        io.BytesIO(content),
        length=len(content),
        content_type=content_type,
    )
    logger.info("minio_upload", path=storage_path, size=len(content))
    return storage_path


def download_file(storage_path: str) -> bytes:
    """Download file from MinIO."""
    client = get_minio_client()
    bucket = settings.minio_bucket

    response = client.get_object(bucket, storage_path)
    try:
        return response.read()
    finally:
        response.close()
        response.release_conn()


def get_presigned_url(
    storage_path: str,
    expires_hours: int = 1,
    *,
    expiry: int | None = None,
) -> str:
    """Generate a presigned URL for direct browser access."""
    from datetime import timedelta

    client = get_minio_client()
    bucket = settings.minio_bucket
    expires = timedelta(seconds=expiry) if expiry is not None else timedelta(hours=expires_hours)

    url = client.presigned_get_object(
        bucket,
        storage_path,
        expires=expires,
    )
    return url


def delete_file(storage_path: str) -> None:
    """Delete file from MinIO."""
    client = get_minio_client()
    bucket = settings.minio_bucket
    client.remove_object(bucket, storage_path)
    logger.info("minio_delete", path=storage_path)


def file_exists(storage_path: str) -> bool:
    """Check if file exists in MinIO."""
    client = get_minio_client()
    bucket = settings.minio_bucket
    try:
        client.stat_object(bucket, storage_path)
        return True
    except S3Error:
        return False
