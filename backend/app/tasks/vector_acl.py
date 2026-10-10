"""E41: keep the vector index's document rights in step with the database."""

from __future__ import annotations

from app.tasks.celery_app import celery_app


@celery_app.task(name="vector.sync_document_acl", max_retries=3, default_retry_delay=30, bind=True)
def sync_document_acl_task(self, document_ids: list[str] | None = None) -> int:
    """Restamp ``acl`` on the given documents' points (all when None)."""
    from app.vector.acl import sync_document_acl

    try:
        return sync_document_acl(document_ids)
    except Exception as exc:  # Qdrant down: retry, the nightly run is the backstop
        raise self.retry(exc=exc) from exc
