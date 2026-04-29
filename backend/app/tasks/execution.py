from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from backend.app.ai import AIRouter
from backend.app.domain.models import ProcessingJobStatus, TaskJob
from backend.app.domain.schemas import DocumentArtifactRead
from backend.app.domain.services import (
    complete_processing_job,
    complete_task_job,
    create_document_artifacts,
    create_processing_job,
    fail_task_job,
    get_document,
    start_task_job,
)
from backend.app.domain.storage import LocalFileStorage
from backend.app.tasks.document_processing import process_document


async def run_task_job(
    db: Session,
    job: TaskJob,
    *,
    ai_router: AIRouter,
    storage: LocalFileStorage,
) -> TaskJob:
    started = start_task_job(db, job)
    try:
        result = await _execute_task(db, started, ai_router=ai_router, storage=storage)
    except Exception as exc:
        return fail_task_job(db, started, error_message=str(exc))
    return complete_task_job(db, started, result=result)


async def _execute_task(
    db: Session,
    job: TaskJob,
    *,
    ai_router: AIRouter,
    storage: LocalFileStorage,
) -> dict[str, Any]:
    if job.task_type == "document.process":
        return await _execute_document_process(db, job, ai_router=ai_router, storage=storage)
    if job.task_type == "email.send.request_approval":
        return {"status": "executed_placeholder", "message": "SMTP send placeholder executed after approval"}
    if job.task_type == "invoice.export.1c.prepare":
        return {"status": "executed_placeholder", "message": "1C export placeholder executed after approval"}
    if job.task_type in {"document.invoice_extraction", "document.drawing_analysis", "email.draft", "invoice.export.xlsx"}:
        return {
            "status": "planned_placeholder",
            "message": "Safe tool execution placeholder; call explicit API endpoint for full action",
        }
    raise ValueError(f"Unsupported task type: {job.task_type}")


async def _execute_document_process(
    db: Session,
    job: TaskJob,
    *,
    ai_router: AIRouter,
    storage: LocalFileStorage,
) -> dict[str, Any]:
    if not job.document_id:
        raise ValueError("document.process task requires document_id")
    document = get_document(db, job.document_id)
    if document is None:
        raise ValueError("Document not found")
    processing_job = create_processing_job(db, document)
    try:
        result = await process_document(document, ai_router, storage)
        if result.artifacts:
            created_artifacts = create_document_artifacts(
                db,
                document,
                [artifact.model_dump(exclude={"id", "document_id", "created_at"}) for artifact in result.artifacts],
            )
            result.artifacts = [
                DocumentArtifactRead(
                    id=artifact.id,
                    document_id=artifact.document_id,
                    artifact_type=artifact.artifact_type,
                    storage_path=artifact.storage_path,
                    content_type=artifact.content_type,
                    page_number=artifact.page_number,
                    width=artifact.width,
                    height=artifact.height,
                    metadata={},
                    created_at=artifact.created_at,
                )
                for artifact in created_artifacts
            ]
        structured = result.structured
        completed = complete_processing_job(
            db,
            document,
            processing_job,
            status=_processing_status(result.status),
            parser_name=result.parser_name,
            result_json=result.model_dump_json(),
            extracted_text=result.text_preview,
            document_type=structured.document_type if structured else None,
            ai_summary=structured.summary if structured else None,
            error_message=result.unsupported_reason,
        )
    except Exception as exc:
        completed = complete_processing_job(
            db,
            document,
            processing_job,
            status=ProcessingJobStatus.FAILED,
            parser_name="document_processing",
            result_json=None,
            extracted_text=None,
            error_message=str(exc),
        )
        raise
    return {
        "document_processing_job_id": completed.id,
        "document_id": document.id,
        "document_status": document.status,
        "processing_status": completed.status,
        "parser_name": completed.parser_name,
    }


def _processing_status(value: str) -> ProcessingJobStatus:
    try:
        return ProcessingJobStatus(value)
    except ValueError:
        return ProcessingJobStatus.FAILED
