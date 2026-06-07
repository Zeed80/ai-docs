"""Extraction Celery tasks — classify, extract, validate.

Pipeline: classify → extract → validate → update document status.
Runs on 'extraction' queue.
"""

import asyncio
import base64
import uuid
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import (
    Document,
    DocumentExtraction,
    DocumentProcessingJob,
    DocumentStatus,
    DocumentType,
    ExtractionField,
    Invoice,
    InvoiceLine,
    InvoiceStatus,
    WarehouseReceipt,
    WarehouseReceiptLine,
)
from app.tasks.celery_app import celery_app

logger = structlog.get_logger()

PIPELINE_STEP_DEFINITIONS = [
    ("store", "Файл сохранен"),
    ("memory_seed", "Первичная память"),
    ("classification", "Классификация"),
    ("extraction", "Распознавание"),
    ("sql_records", "Записи SQL"),
    ("memory_graph", "Память и граф"),
    ("embedding", "Векторизация"),
]


def _get_sync_session() -> Session:
    engine = create_engine(settings.database_url_sync, pool_pre_ping=True)
    return Session(engine)


def _run_async(coro):
    """Run async coroutine from sync Celery task.

    Always creates a fresh event loop to avoid two bugs:
    1. "cannot reuse already awaited coroutine" — happens when a domain RuntimeError
       raised inside the coroutine was mistakenly caught by the old except-RuntimeError
       fallback, causing asyncio.run() to re-use an already-consumed coroutine.
    2. Fork-inherited loop state — forked Celery workers inherit asyncio state from
       the parent process, making asyncio.get_event_loop() unreliable.
    """
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = None

    if loop is not None and loop.is_running():
        # We're already inside an event loop (e.g. called from async context via thread).
        # Use a thread-pool to create a new isolated loop.
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coro).result()

    # Standard Celery sync-worker path: always create a fresh loop.
    # asyncio.run() handles cleanup (close, shutdown_asyncgens) reliably.
    return asyncio.run(coro)


def _default_pipeline_steps() -> list[dict]:
    return [
        {"key": key, "label": label, "status": "pending"}
        for key, label in PIPELINE_STEP_DEFINITIONS
    ]


def _latest_processing_job(db: Session, doc: Document) -> DocumentProcessingJob | None:
    return db.execute(
        select(DocumentProcessingJob)
        .where(DocumentProcessingJob.document_id == doc.id)
        .order_by(DocumentProcessingJob.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def _get_or_create_processing_job(
    db: Session,
    doc: Document,
    *,
    current_step: str,
    celery_task_id: str | None = None,
) -> DocumentProcessingJob:
    job = _latest_processing_job(db, doc)
    if not job or job.status in {"done", "failed"}:
        job = DocumentProcessingJob(
            document_id=doc.id,
            status="running",
            pipeline_steps=_default_pipeline_steps(),
            current_step=current_step,
            started_at=datetime.now(UTC),
            celery_task_id=celery_task_id,
        )
        db.add(job)
        db.flush()
    else:
        job.status = "running"
        job.current_step = current_step
        job.started_at = job.started_at or datetime.now(UTC)
        if celery_task_id and not job.celery_task_id:
            job.celery_task_id = celery_task_id
    return job


def _ensure_step_entries(job: DocumentProcessingJob) -> list[dict]:
    existing = {
        step.get("key"): dict(step)
        for step in (job.pipeline_steps or [])
        if isinstance(step, dict) and step.get("key")
    }
    steps = []
    for key, label in PIPELINE_STEP_DEFINITIONS:
        step = existing.get(key, {"key": key, "label": label, "status": "pending"})
        step.setdefault("label", label)
        step.setdefault("status", "pending")
        steps.append(step)
    return steps


def _set_job_step(
    job: DocumentProcessingJob,
    key: str,
    status: str,
    *,
    error: str | None = None,
) -> None:
    steps = []
    for step in _ensure_step_entries(job):
        if step["key"] == key:
            step = {**step, "status": status}
            if error:
                step["error"] = error
        steps.append(step)
    job.pipeline_steps = steps
    job.current_step = key if status in {"queued", "running", "failed"} else job.current_step
    if error:
        job.error = error


def _step_status(job: DocumentProcessingJob, key: str) -> str | None:
    for step in _ensure_step_entries(job):
        if step["key"] == key:
            return step.get("status")
    return None


def _skip_remaining_steps(job: DocumentProcessingJob, keys: set[str]) -> None:
    for key in keys:
        if _step_status(job, key) in {"pending", "queued", None}:
            _set_job_step(job, key, "skipped")


def _finish_job(
    job: DocumentProcessingJob,
    status: str,
    *,
    error: str | None = None,
) -> None:
    job.status = status
    job.error = error
    job.finished_at = datetime.now(UTC)
    if status == "done":
        job.current_step = "completed"


@celery_app.task(name="app.tasks.extraction.classify_document", bind=True, max_retries=2)
def classify_document(self, document_id: str, force: bool = False) -> dict:
    """Classify document type using the configured OCR/extraction model.

    Updates Document.doc_type and doc_type_confidence.
    """
    logger.info("classify_start", document_id=document_id)

    with _get_sync_session() as db:
        doc = db.get(Document, uuid.UUID(document_id))
        if not doc:
            return {"error": "Document not found"}
        job = _get_or_create_processing_job(
            db,
            doc,
            current_step="classification",
            celery_task_id=getattr(self.request, "id", None),
        )
        _set_job_step(job, "store", "done")
        if doc.source_channel:
            _set_job_step(job, "memory_seed", "done")
        _set_job_step(job, "classification", "running")

        metadata = doc.metadata_ or {}
        if (
            not force
            and metadata.get("manual_doc_type_override")
            and doc.doc_type is not None
        ):
            doc.doc_type_confidence = doc.doc_type_confidence or 1.0
            doc.status = DocumentStatus.extracting
            _set_job_step(job, "classification", "done")
            db.commit()
            doc_type = doc.doc_type.value
            if doc_type == "invoice":
                _set_job_step(job, "extraction", "queued")
                extract_invoice.delay(document_id)
                db.commit()
            elif doc_type in GENERIC_EXTRACTION_TYPES:
                _set_job_step(job, "extraction", "queued")
                db.commit()
                extract_generic_fields.delay(document_id)
            else:
                doc.status = DocumentStatus.needs_review
                _skip_remaining_steps(
                    job,
                    {"extraction", "sql_records", "memory_graph"},
                )
                _finish_job(job, "done")
                db.commit()
            logger.info(
                "classify_skipped_manual_override",
                document_id=document_id,
                doc_type=doc_type,
            )
            return {
                "document_id": document_id,
                "doc_type": doc_type,
                "confidence": doc.doc_type_confidence,
                "manual_override": True,
            }

        doc.status = DocumentStatus.classifying
        db.commit()

        # ── Early detection: drawing formats are unambiguous by extension ─────
        _drawing_extensions = frozenset({"dxf", "dwg", "step", "stp", "iges", "slddrw", "ipt"})
        _file_ext = (doc.file_name.rsplit(".", 1)[-1].lower() if "." in doc.file_name else "")
        if _file_ext in _drawing_extensions:
            doc_type = "drawing"
            confidence = 1.0
        else:
            # Get PDF/text content for AI classification
            text = _get_document_text(doc)
            if not text:
                logger.warning("classify_no_text", document_id=document_id)
                doc.status = DocumentStatus.needs_review
                if doc.mime_type.startswith("image/"):
                    error = (
                        "Изображение требует модели с поддержкой vision. "
                        "Настройте Ollama vision-модель или llamacpp-модель с vision=true "
                        "в разделе Настройки → Модели."
                    )
                else:
                    error = "No text extracted from document"
                _set_job_step(job, "classification", "failed", error=error)
                _finish_job(job, "failed", error=error)
                db.commit()
                return {"error": error}

            try:
                from app.ai.router import ai_router
                from app.tasks.gpu_lock import gpu_single_flight
                with gpu_single_flight(f"classify:{document_id}"):
                    result = _run_async(ai_router.classify_document(text))
                doc_type = result.get("type", "other")
                confidence = result.get("confidence", 0.5)
                valid_types = {t.value for t in DocumentType}
                if doc_type not in valid_types:
                    doc_type = "other"
            except Exception as e:
                logger.error("classify_error", document_id=document_id, error=str(e))
                doc.status = DocumentStatus.needs_review
                _set_job_step(job, "classification", "failed", error=str(e))
                _finish_job(job, "failed", error=str(e))
                db.commit()
                self.retry(countdown=30, exc=e)
                return {"error": str(e)}

        # Classify via Ollama
        try:
            doc.doc_type = DocumentType(doc_type)
            doc.doc_type_confidence = confidence
            doc.status = DocumentStatus.extracting
            _set_job_step(job, "classification", "done")
            db.commit()

            logger.info(
                "classify_done",
                document_id=document_id,
                doc_type=doc_type,
                confidence=confidence,
            )

            # Chain: if invoice → extract; if drawing → trigger drawing analysis
            if doc_type == "invoice":
                _set_job_step(job, "extraction", "queued")
                extract_invoice.delay(document_id)
                db.commit()
            elif doc_type == "drawing":
                try:
                    from app.tasks.drawing_analysis import _create_drawing_from_doc_sync
                    ext = doc.file_name.rsplit(".", 1)[-1].lower() if "." in doc.file_name else "pdf"
                    _create_drawing_from_doc_sync(
                        str(doc.id), doc.file_name, ext, doc.storage_path or ""
                    )
                except Exception as exc:
                    logger.warning("drawing_from_doc_failed", document_id=document_id, error=str(exc))
                doc.status = DocumentStatus.analyzed
                _skip_remaining_steps(job, {"extraction", "sql_records", "memory_graph"})
                _finish_job(job, "done")
                db.commit()
            elif doc_type in GENERIC_EXTRACTION_TYPES:
                _set_job_step(job, "extraction", "queued")
                db.commit()
                extract_generic_fields.delay(document_id)
            else:
                doc.status = DocumentStatus.needs_review
                _skip_remaining_steps(
                    job,
                    {"extraction", "sql_records", "memory_graph"},
                )
                _finish_job(job, "done")
                db.commit()

            return {
                "document_id": document_id,
                "doc_type": doc_type,
                "confidence": confidence,
            }

        except Exception as e:
            logger.error("classify_chain_error", document_id=document_id, error=str(e))
            doc.status = DocumentStatus.needs_review
            _set_job_step(job, "classification", "failed", error=str(e))
            _finish_job(job, "failed", error=str(e))
            db.commit()
            self.retry(countdown=30, exc=e)
            return {"error": str(e)}


@celery_app.task(name="app.tasks.extraction.extract_invoice", bind=True, max_retries=2)
def extract_invoice(self, document_id: str) -> dict:
    """Extract invoice fields using the configured OCR/extraction model.

    Creates DocumentExtraction, ExtractionFields, Invoice, InvoiceLines.
    """
    logger.info("extract_start", document_id=document_id)
    import time
    _t0 = time.monotonic()

    with _get_sync_session() as db:
        doc = db.get(Document, uuid.UUID(document_id))
        if not doc:
            return {"error": "Document not found"}
        job = _get_or_create_processing_job(
            db,
            doc,
            current_step="extraction",
            celery_task_id=getattr(self.request, "id", None),
        )
        _set_job_step(job, "extraction", "running")
        doc.status = DocumentStatus.extracting
        db.commit()

        text = _get_document_text(doc)
        if not text:
            needs_ocr = (
                doc.mime_type.startswith("image/")
                or doc.mime_type == "application/pdf"
                or (doc.file_name or "").lower().endswith(".pdf")
            )
            if needs_ocr:
                chain = _resolve_ocr_model_chain()
                if chain:
                    # Models are configured but none responded — likely Ollama is offline.
                    error = (
                        "OCR завершился без результата: все локальные vision-модели "
                        f"({', '.join(m for m, _ in chain)}) не ответили или вернули пустой текст. "
                        "Проверьте доступность Ollama/llamacpp и повторите позже."
                    )
                else:
                    error = (
                        "Документ требует OCR, но ни одна vision-модель не настроена. "
                        "Настройте Ollama vision-модель или llamacpp-модель с vision=true "
                        "в разделе Настройки → Модели."
                    )
            else:
                error = "No text extracted from document"
            doc.status = DocumentStatus.needs_review
            _set_job_step(job, "extraction", "failed", error=error)
            _finish_job(job, "failed", error=error)
            db.commit()
            return {"error": error}

        start_time = time.time()

        try:
            from app.ai.router import ai_router
            from app.tasks.gpu_lock import gpu_single_flight

            with gpu_single_flight(f"extract:{document_id}"):
                extracted = _run_async(ai_router.extract_invoice(text))

            processing_time_ms = int((time.time() - start_time) * 1000)

            # Filename-based fallback for mandatory fields the LLM missed.
            # Patterns: "№ KA-15203", "от 04.10.2024", "от 2024-10-04".
            if not extracted.get("invoice_number") or not extracted.get("invoice_date"):
                _fallback_from_filename(doc.file_name, extracted)

            # Autocorrect impossible total (total_amount < subtotal is physically
            # impossible — most likely the LLM picked a wrong number). Silently
            # replace with subtotal + tax_amount and mark for review.
            _autocorrect_impossible_total(extracted)

        except Exception as e:
            logger.error("extract_error", document_id=document_id, error=str(e))
            doc.status = DocumentStatus.needs_review
            _set_job_step(job, "extraction", "failed", error=str(e))
            _finish_job(job, "failed", error=str(e))
            db.commit()
            self.retry(countdown=30, exc=e)
            return {"error": str(e)}

        # Validate arithmetic
        from app.ai.confidence import (
            compute_field_confidences,
            compute_overall_confidence,
            validate_arithmetic,
        )

        validation_errors = validate_arithmetic(extracted)
        ai_confidences = extracted.get("field_confidences", {})
        field_confs = compute_field_confidences(extracted, ai_confidences, validation_errors)
        overall_confidence = compute_overall_confidence(field_confs)

        # Bbox binding
        bbox_map: dict[str, dict | None] = {}
        try:
            content = _download_document(doc)
            if content:
                from app.ai.pdf_processor import bind_bboxes, extract_pdf
                pdf_data = extract_pdf(content, render_pages=False)
                field_values = {fc.field_name: fc.value for fc in field_confs}
                bbox_map = bind_bboxes(pdf_data.pages, field_values)
        except Exception as e:
            logger.warning("bbox_binding_failed", error=str(e))

        # Apply normalization rules before saving
        field_confs = _apply_normalization_rules(db, field_confs)

        # Save DocumentExtraction
        from app.ai.model_resolver import get_ocr_model as _get_ocr
        _ocr_cfg = _get_ocr()
        extraction = DocumentExtraction(
            document_id=doc.id,
            model_name=f"{_ocr_cfg.provider}/{_ocr_cfg.model}",
            raw_output=extracted,
            structured_data=extracted,
            overall_confidence=overall_confidence,
            processing_time_ms=processing_time_ms,
        )
        db.add(extraction)
        db.flush()
        _set_job_step(job, "extraction", "done")

        # Save ExtractionFields
        for fc in field_confs:
            bbox = bbox_map.get(fc.field_name)
            ef = ExtractionField(
                extraction_id=extraction.id,
                field_name=fc.field_name,
                field_value=fc.value,
                confidence=fc.confidence,
                confidence_reason=fc.reason,
                bbox_page=bbox["page"] if bbox else None,
                bbox_x=bbox["x"] if bbox else None,
                bbox_y=bbox["y"] if bbox else None,
                bbox_w=bbox["w"] if bbox else None,
                bbox_h=bbox["h"] if bbox else None,
            )
            db.add(ef)

        doc.status = DocumentStatus.needs_review
        _finish_job(job, "done")

        # Record control-digit failures so the review UI / agent can flag them
        # ("ИНН/счёт не прошёл контрольную сумму — проверьте") even on manual review.
        _csum = _checksum_issues(extracted)
        doc.metadata_ = {**(doc.metadata_ or {}), "checksum_issues": _csum}
        if _csum:
            logger.warning(
                "extract_checksum_issues", document_id=document_id, issues=_csum
            )

        # Queue auto-verification. A per-upload ``auto_verify`` flag wins; when
        # absent, fall back to the global ``auto_verify_enabled`` config (on by
        # default) so email-ingested and bulk-uploaded invoices are auto-approved
        # when confident — minimal human intervention.
        doc_meta = doc.metadata_ or {}
        _auto_verify = doc_meta.get("auto_verify")
        if _auto_verify is None:
            from app.api.ai_settings import get_ai_config as _get_ai_cfg
            _auto_verify = bool(_get_ai_cfg().get("auto_verify_enabled", True))
        if _auto_verify:
            auto_verify_document.delay(document_id)
            logger.info("auto_verify_queued", document_id=document_id)

        db.commit()

        # NOTE: embedding is intentionally NOT queued here. It is built exactly
        # once, on approval, by process_approved_document (with richer data:
        # parties, ИНН, line items). Embedding pre-approval would (a) double the
        # GPU work — it was re-embedded on approve anyway — and (b) vectorise
        # data that may still change during human review. Documents that stay in
        # needs_review get embedded when they are approved.

        logger.info(
            "extract_done",
            document_id=document_id,
            confidence=overall_confidence,
            field_count=len(field_confs),
            processing_ms=processing_time_ms,
            validation_errors=len(validation_errors),
        )

        return {
            "document_id": document_id,
            "overall_confidence": overall_confidence,
            "field_count": len(field_confs),
            "validation_errors": validation_errors,
        }


#: Non-invoice document types that get structured field extraction.
GENERIC_EXTRACTION_TYPES = frozenset(
    {"letter", "contract", "act", "waybill", "commercial_offer"}
)


@celery_app.task(name="app.tasks.extraction.extract_generic_fields", bind=True, max_retries=2)
def extract_generic_fields(self, document_id: str) -> dict:
    """Extract editable fields for non-invoice documents (letter/contract/act/…).

    Mirrors :func:`extract_invoice` but stores a flat, type-aware field list into
    DocumentExtraction / ExtractionField so the same review UI can display and
    correct them. No arithmetic validation (not financial line items).
    """
    logger.info("extract_generic_start", document_id=document_id)
    import time

    with _get_sync_session() as db:
        doc = db.get(Document, uuid.UUID(document_id))
        if not doc:
            return {"error": "Document not found"}
        job = _get_or_create_processing_job(
            db,
            doc,
            current_step="extraction",
            celery_task_id=getattr(self.request, "id", None),
        )
        _set_job_step(job, "extraction", "running")
        doc.status = DocumentStatus.extracting
        db.commit()

        doc_type = doc.doc_type.value if doc.doc_type else "other"
        text = _get_document_text(doc)
        if not text:
            error = "No text extracted from document"
            doc.status = DocumentStatus.needs_review
            _set_job_step(job, "extraction", "failed", error=error)
            _finish_job(job, "failed", error=error)
            db.commit()
            return {"error": error}

        start_time = time.time()
        try:
            from app.ai.router import ai_router

            result = _run_async(ai_router.extract_document_fields(text, doc_type))
            processing_time_ms = int((time.time() - start_time) * 1000)
        except Exception as e:
            logger.error("extract_generic_error", document_id=document_id, error=str(e))
            doc.status = DocumentStatus.needs_review
            _set_job_step(job, "extraction", "failed", error=str(e))
            _finish_job(job, "failed", error=str(e))
            db.commit()
            self.retry(countdown=30, exc=e)
            return {"error": str(e)}

        raw_fields = result.get("fields") or []
        # Normalize to (name, value, confidence) triples.
        triples: list[tuple[str, str | None, float]] = []
        for f in raw_fields:
            if not isinstance(f, dict):
                continue
            name = str(f.get("name") or "").strip()
            if not name:
                continue
            value = f.get("value")
            value = None if value is None else str(value)
            try:
                conf = float(f.get("confidence", 0.6))
            except (TypeError, ValueError):
                conf = 0.6
            triples.append((name, value, max(0.0, min(1.0, conf))))

        overall_confidence = (
            sum(c for _, _, c in triples) / len(triples) if triples else 0.0
        )

        # Bbox binding (only meaningful for PDFs with a text layer).
        bbox_map: dict[str, dict | None] = {}
        try:
            content = _download_document(doc)
            if content and doc.mime_type == "application/pdf":
                from app.ai.pdf_processor import bind_bboxes, extract_pdf

                pdf_data = extract_pdf(content, render_pages=False)
                bbox_map = bind_bboxes(
                    pdf_data.pages, {n: v for n, v, _ in triples}
                )
        except Exception as e:
            logger.warning("generic_bbox_binding_failed", error=str(e))

        from app.ai.model_resolver import get_ocr_model as _get_ocr

        _ocr_cfg = _get_ocr()
        extraction = DocumentExtraction(
            document_id=doc.id,
            model_name=f"{_ocr_cfg.provider}/{_ocr_cfg.model}",
            raw_output=result,
            structured_data=result,
            overall_confidence=overall_confidence,
            processing_time_ms=processing_time_ms,
        )
        db.add(extraction)
        db.flush()
        _set_job_step(job, "extraction", "done")

        for name, value, conf in triples:
            bbox = bbox_map.get(name)
            db.add(
                ExtractionField(
                    extraction_id=extraction.id,
                    field_name=name,
                    field_value=value,
                    confidence=conf,
                    bbox_page=bbox["page"] if bbox else None,
                    bbox_x=bbox["x"] if bbox else None,
                    bbox_y=bbox["y"] if bbox else None,
                    bbox_w=bbox["w"] if bbox else None,
                    bbox_h=bbox["h"] if bbox else None,
                )
            )

        doc.status = DocumentStatus.needs_review
        _skip_remaining_steps(job, {"sql_records", "memory_graph"})
        _finish_job(job, "done")
        db.commit()

        try:
            from app.tasks.embedding import embed_document

            embed_document.delay(document_id)
        except Exception as _e:
            logger.warning(
                "embed_queue_failed_post_generic", document_id=document_id, error=str(_e)
            )

        logger.info(
            "extract_generic_done",
            document_id=document_id,
            doc_type=doc_type,
            field_count=len(triples),
            confidence=overall_confidence,
        )
        return {
            "document_id": document_id,
            "doc_type": doc_type,
            "field_count": len(triples),
            "overall_confidence": overall_confidence,
        }


@celery_app.task(name="app.tasks.extraction.process_document")
def process_document(document_id: str, force: bool = False) -> dict:
    """Full pipeline: classify → extract → validate.

    Entry point for newly ingested documents.
    """
    return classify_document(document_id, force)


def _get_document_text(doc: Document) -> str:
    """Get text via the parser registry; fall back to VLM OCR when needed.

    Text-bearing formats (PDF text layer, DOCX, XLSX, EML, DXF/DWG, STEP, plain
    text) are handled by :func:`app.ai.parsers.parse_document`. Images and
    scanned PDFs (no text layer) are flagged ``needs_ocr`` and routed to the
    local VLM OCR fallback below.

    If OCR returns empty on first attempt (model cold start), waits 10 s and
    retries once.
    """
    import time

    from app.ai.parsers import parse_document

    content = _download_document(doc)
    if not content:
        return ""

    parsed = parse_document(content, doc.file_name, doc.mime_type)
    if parsed.text.strip():
        return parsed.text

    if not parsed.needs_ocr:
        # Nothing extractable and not an OCR candidate (e.g. unsupported binary).
        return parsed.text

    # OCR fallback — strictly local VLM (see _ocr_*_content).
    is_pdf = doc.mime_type == "application/pdf" or (doc.file_name or "").lower().endswith(".pdf")
    if is_pdf:
        text = _ocr_pdf_content(content, doc)
        if not text.strip():
            logger.warning("pdf_ocr_empty_retry", document_id=str(doc.id))
            time.sleep(10)
            text = _ocr_pdf_content(content, doc)
        return text

    # Otherwise it is an image flagged for OCR.
    text = _ocr_image_content(content, doc.mime_type, doc)
    if not text.strip():
        logger.warning("image_ocr_empty_retry", document_id=str(doc.id))
        time.sleep(10)
        text = _ocr_image_content(content, doc.mime_type, doc)
    return text


def _get_configured_ocr_model() -> str:
    """Read OCR model name from ai_config (via model_resolver). Legacy: returns only model name."""
    from app.ai.model_resolver import get_ocr_model
    return get_ocr_model().model


def _get_configured_ocr_model_and_provider() -> tuple[str, str]:
    """Read OCR model + provider from ai_config (via model_resolver)."""
    from app.ai.model_resolver import get_ocr_model
    cfg = get_ocr_model()
    return cfg.model, cfg.provider



def _ollama_vision_ocr(images_b64: list[str], ollama_model: str, prompt: str) -> str:
    """Call Ollama vision API with the model the user configured. Retries once on cold-start empty."""
    import time
    import httpx

    def _call() -> str:
        resp = httpx.post(
            f"{str(settings.ollama_url).rstrip('/')}/api/chat",
            json={
                "model": ollama_model,
                "messages": [{"role": "user", "content": prompt, "images": images_b64}],
                "stream": False,
                # Disable extended thinking: qwen3.5:9b (and other thinking models)
                # otherwise spend the whole token budget on reasoning and return an
                # EMPTY transcription — the cause of ~300 s "empty, try next" OCR
                # stalls. OCR is pure transcription; no reasoning is needed.
                "think": False,
                "options": {
                    "temperature": 0.0,
                    # Bound generation so a misbehaving run can't burn the full
                    # 300 s timeout; enough for a dense full-page invoice.
                    "num_predict": 8192,
                },
            },
            timeout=300.0,
        )
        resp.raise_for_status()
        return resp.json().get("message", {}).get("content", "")

    # Retry on BOTH transient errors (e.g. a 400/503 while Ollama is loading the
    # model, with OLLAMA_NUM_PARALLEL=1 + a queue) and cold-start empty output.
    # Two attempts with a short backoff — enough to ride out a model load.
    last_err: Exception | None = None
    for attempt in range(2):
        try:
            text = _call()
            if text.strip():
                return text
            logger.warning("ollama_vision_ocr_empty_retry", model=ollama_model, attempt=attempt)
        except Exception as e:  # noqa: BLE001
            last_err = e
            logger.warning("ollama_vision_ocr_error_retry", model=ollama_model, attempt=attempt, error=str(e))
        time.sleep(5)
    if last_err is not None:
        logger.warning("ollama_vision_ocr_failed", model=ollama_model, error=str(last_err))
    else:
        logger.warning("ollama_vision_ocr_empty", model=ollama_model)
    return ""


def _llamacpp_vision_ocr(images_b64: list[str], prompt: str) -> str:
    """Send images to llamacpp via OpenAI-compatible /v1/chat/completions."""
    import httpx

    base = settings.llamacpp_url.rstrip("/")
    # Strip /v1 suffix if already included in the configured URL
    if base.endswith("/v1"):
        base = base[:-3]

    content: list[dict] = [{"type": "text", "text": prompt}]
    for img in images_b64:
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{img}"},
        })

    try:
        resp = httpx.post(
            f"{base}/v1/chat/completions",
            json={
                "model": "local",
                "messages": [{"role": "user", "content": content}],
                "temperature": 0.0,
                "stream": False,
            },
            timeout=300.0,
        )
        resp.raise_for_status()
        choices = resp.json().get("choices") or []
        if choices:
            msg = choices[0].get("message") or {}
            return msg.get("content") or ""
    except Exception as e:
        logger.warning("llamacpp_vision_ocr_failed", error=str(e))
    return ""


_OCR_PROMPT = (
    "Распознай ВЕСЬ текст на изображении документа. "
    "Точно сохрани: номера счётов/договоров, даты, ИНН, КПП, ОГРН, БИК, "
    "расчётные и корреспондентские счета, наименования организаций и товаров, "
    "единицы измерения, количества, цены, суммы, НДС, итоговые суммы. "
    "Таблицы сохрани построчно. Если текст нечёткий — распознай максимально точно. "
    "Верни ТОЛЬКО текст без пояснений."
)


def _preprocess_image(content: bytes) -> bytes:
    """Enhance image contrast/brightness for better OCR on dark or washed-out scans."""
    try:
        from PIL import Image, ImageEnhance
        import io

        img = Image.open(io.BytesIO(content)).convert("RGB")
        img = ImageEnhance.Contrast(img).enhance(1.5)
        img = ImageEnhance.Sharpness(img).enhance(1.3)
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=False)
        return buf.getvalue()
    except Exception:
        return content


def _provider_supports_vision(model: str, provider: str) -> bool:
    """Whether a (model, provider) pair can accept images.

    llama.cpp must report vision support via /props; other local providers
    (Ollama, vLLM, lmstudio) are assumed capable when the user configured them
    for OCR. Cloud providers are never reached here (INVOICE_OCR is confidential
    and routing validation rejects non-local keys).
    """
    if provider != "llamacpp":
        return True
    import httpx

    try:
        resp = httpx.get(
            f"{settings.llamacpp_url.rstrip('/v1').rstrip('/')}/props",
            timeout=5.0,
        )
        props = resp.json()
        modalities = props.get("modalities") or {}
        if modalities.get("vision") or props.get("vision"):
            return True
    except Exception:
        pass
    logger.info("ocr_llamacpp_no_vision_skip", model=model)
    return False


def _resolve_vision_ocr_model() -> tuple[str, str] | None:
    """Return the primary (model, provider) for vision OCR, or None if unusable."""
    model, provider = _get_configured_ocr_model_and_provider()
    return (model, provider) if _provider_supports_vision(model, provider) else None


def _resolve_ocr_model_chain() -> list[tuple[str, str]]:
    """Ordered list of local (model, provider) OCR candidates to try in turn.

    Built from the INVOICE_OCR task routing (``models[0]`` is primary, the rest
    are fallbacks), filtered to vision-capable providers. Falls back to the
    single configured OCR model when routing is unavailable. All candidates are
    local — confidentiality is preserved.
    """
    chain: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    try:
        from app.ai.schemas import AITask
        from app.ai.task_routing import _registry as _routing_registry
        from app.ai.task_routing import get_routing_for

        routing = get_routing_for(AITask.INVOICE_OCR)
        for key in routing.models:
            model = provider = None
            try:
                cap = _routing_registry().models.get(key)
                if cap is not None:
                    model, provider = cap.provider_model, cap.provider.value
            except Exception:
                model = provider = None
            if not model or not provider:
                continue
            pair = (model, provider)
            if pair in seen or not _provider_supports_vision(model, provider):
                continue
            seen.add(pair)
            chain.append(pair)
    except Exception as exc:
        logger.debug("ocr_chain_routing_unavailable", error=str(exc))

    if not chain:
        primary = _resolve_vision_ocr_model()
        if primary:
            chain.append(primary)
    return chain


def _preprocess_ocr_page(raw_image: bytes) -> bytes:
    """Enhance a rendered page/image for OCR: CLAHE + deskew, contrast fallback.

    Reuses the drawing preprocessor (OpenCV CLAHE/deskew/scaling) used for
    technical drawings; on any failure falls back to the lightweight PIL
    contrast/sharpness enhancement.
    """
    try:
        from app.ai.drawing_preprocessor import preprocess_drawing_image

        pre = preprocess_drawing_image(raw_image, fmt="pdf_raster", max_views=1)
        if pre.full_image:
            return pre.full_image
    except Exception as exc:
        logger.debug("ocr_preprocess_drawing_failed", error=str(exc))
    return _preprocess_image(raw_image)


def _ocr_image_with_chain(image_bytes: bytes, chain: list[tuple[str, str]]) -> str:
    """OCR a single already-encoded image, trying each model until non-empty."""
    encoded = base64.b64encode(image_bytes).decode("ascii")
    for model, provider in chain:
        if provider == "llamacpp":
            text = _llamacpp_vision_ocr([encoded], _OCR_PROMPT)
        else:
            text = _ollama_vision_ocr([encoded], model, _OCR_PROMPT)
        if text.strip():
            return text
        logger.info("ocr_model_empty_try_next", model=model, provider=provider)
    return ""


def _ocr_image_content(content: bytes, mime_type: str, doc: Document) -> str:
    """OCR an image using the local vision model chain (strictly local)."""
    chain = _resolve_ocr_model_chain()
    if not chain:
        logger.warning(
            "image_ocr_skipped_no_vision_model",
            document_id=str(doc.id),
            hint="Configure an Ollama vision model or a llamacpp model with vision support",
        )
        return ""
    logger.info("image_ocr_start", document_id=str(doc.id), candidates=len(chain))
    enhanced = _preprocess_ocr_page(content)
    text = _ocr_image_with_chain(enhanced, chain)
    logger.info("image_ocr_done", document_id=str(doc.id), text_len=len(text))
    return text


def _ocr_pdf_content(content: bytes, doc: Document) -> str:
    """OCR a scanned PDF page-by-page with the local vision model chain.

    Renders up to ``settings.ocr_max_pages`` pages, preprocesses each
    (CLAHE/deskew), OCRs each page in its own VLM call, then concatenates with
    ``[Страница N]`` markers. Per-page calls avoid table/field jumbling that
    occurs when many pages are sent in a single request.
    """
    chain = _resolve_ocr_model_chain()
    if not chain:
        logger.warning(
            "pdf_ocr_skipped_no_vision_model",
            document_id=str(doc.id),
            hint="Configure an Ollama vision model or a llamacpp model with vision support",
        )
        return ""
    try:
        import fitz

        max_pages = getattr(settings, "ocr_max_pages", 15)
        scale = getattr(settings, "ocr_render_scale", 2.5)
        page_images: list[bytes] = []
        with fitz.open(stream=content, filetype="pdf") as pdf:
            n_pages = min(len(pdf), max_pages)
            for i in range(n_pages):
                pixmap = pdf[i].get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
                page_images.append(_preprocess_ocr_page(pixmap.tobytes("png")))
        if not page_images:
            return ""
        logger.info(
            "pdf_ocr_start",
            document_id=str(doc.id),
            pages=len(page_images),
            candidates=len(chain),
        )
        parts: list[str] = []
        for idx, image_bytes in enumerate(page_images, start=1):
            page_text = _ocr_image_with_chain(image_bytes, chain)
            if page_text.strip():
                parts.append(f"[Страница {idx}]\n{page_text.strip()}")
        text = "\n\n".join(parts)
        logger.info("pdf_ocr_done", document_id=str(doc.id), text_len=len(text))
        return text
    except Exception as e:
        logger.warning("pdf_ocr_failed", document_id=str(doc.id), error=str(e))
        return ""



def _normalize_ocr_text(text: str) -> str:
    return "\n".join(line.strip() for line in text.splitlines() if line.strip()).strip()


def _looks_like_document_text(text: str) -> bool:
    normalized = _normalize_ocr_text(text)
    if len(normalized) < 30:
        return False
    alpha_num = sum(ch.isalnum() for ch in normalized)
    if alpha_num / max(len(normalized), 1) < 0.35:
        return False
    words = [word.lower() for word in normalized.split()]
    if len(words) >= 5 and len(set(words)) <= 2:
        return False
    return True


def _download_document(doc: Document) -> bytes | None:
    """Download document content from MinIO."""
    try:
        from app.storage import download_file
        return download_file(doc.storage_path)
    except Exception as e:
        logger.warning("document_download_failed", error=str(e), path=doc.storage_path)
        return None


def _apply_normalization_rules(db: Session, field_confs: list) -> list:
    """Apply active NormalizationRules to extracted field values before saving."""
    import re

    from app.db.models import NormalizationRule, NormRuleStatus

    active_rules = db.execute(
        select(NormalizationRule).where(
            NormalizationRule.status == NormRuleStatus.active
        )
    ).scalars().all()

    if not active_rules:
        return field_confs

    for fc in field_confs:
        for rule in active_rules:
            if rule.field_name != fc.field_name:
                continue
            if not fc.value:
                continue

            old_val = fc.value
            if rule.is_regex:
                try:
                    new_val = re.sub(rule.pattern, rule.replacement, old_val)
                except re.error:
                    continue
            else:
                if old_val == rule.pattern:
                    new_val = rule.replacement
                else:
                    continue

            if new_val != old_val:
                fc = fc._replace(value=new_val) if hasattr(fc, '_replace') else fc
                # For dataclass-like objects, update in place
                if hasattr(fc, 'value'):
                    fc.value = new_val
                fc.reason = "normalization_applied"
                rule.apply_count += 1
                rule.last_applied_at = datetime.now(UTC)
                logger.info(
                    "norm_rule_applied",
                    field=fc.field_name,
                    old=old_val,
                    new=new_val,
                    rule_id=str(rule.id),
                )

    db.commit()
    return field_confs


def _compare_extractions(
    main: dict, others: list[dict], threshold: float = 0.95
) -> tuple[bool, float]:
    """Compare key invoice fields across all model extractions.

    Rules:
    - invoice_number and invoice_date are mandatory: if either is missing in
      main extraction → reject immediately (score=0).
    - For each key field present in main, check that ALL other extractions agree.
    - Score = fraction of agreeing fields. Approves when score >= ``threshold``
      (the user-configured auto-approve confidence threshold).
    """
    MANDATORY = {"invoice_number", "invoice_date"}
    KEY_FIELDS = [
        "invoice_number", "invoice_date", "total_amount", "currency",
        "subtotal", "tax_amount",
    ]

    # Mandatory fields must be present
    for mf in MANDATORY:
        if not main.get(mf):
            logger.info(
                "auto_verify_reject_mandatory_missing",
                field=mf,
                value=main.get(mf),
            )
            return False, 0.0

    if not others:
        return False, 0.0

    agreements = 0
    total = 0
    for field in KEY_FIELDS:
        main_val = main.get(field)
        if main_val is None:
            continue
        total += 1
        main_str = str(main_val).strip()
        # Every verify extraction must have this field and agree
        all_match = True
        for o in others:
            other_val = o.get(field)
            if other_val is None or str(other_val).strip() != main_str:
                all_match = False
                break
        if all_match:
            agreements += 1

    if total == 0:
        return False, 0.0
    score = agreements / total
    return score >= threshold, score


def _extract_invoice_with_model(text: str, model_name: str) -> dict:
    """Re-extract invoice using a specific Ollama model for verification."""
    import json as _json
    import re

    import httpx

    from app.config import settings

    if not model_name or not model_name.strip():
        return {}

    prompt = (
        "Extract invoice fields as JSON object with these keys only: "
        "invoice_number, invoice_date, due_date, currency, subtotal, tax_amount, total_amount, "
        "supplier (object with name, inn), buyer (object with name, inn). "
        "Use null for missing fields. Return ONLY the JSON object, no explanation.\n\n"
        f"Document text:\n{text[:8000]}"
    )

    try:
        response = httpx.post(
            f"{str(settings.ollama_url).rstrip('/')}/api/chat",
            json={
                "model": model_name,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
                "options": {"temperature": 0.1},
            },
            timeout=180.0,
        )
        response.raise_for_status()
        body = response.json()
        content = body.get("message", {}).get("content", "")
        match = re.search(r'\{.*\}', content, re.DOTALL)
        if match:
            return _json.loads(match.group())
        return {}
    except Exception as e:
        logger.warning("verify_extract_failed", model=model_name, error=str(e))
        return {}


def _upsert_party(db: Session, data: dict, role: str) -> uuid.UUID | None:
    """Create or update a Party from extracted supplier/buyer data. Returns party.id or None."""
    if not data:
        return None
    name = data.get("name")
    inn = data.get("inn")
    if not name and not inn:
        return None

    from app.db.models import Party, PartyRole

    try:
        party_role = PartyRole(role)
    except ValueError:
        party_role = PartyRole.supplier

    def _by_inn():
        # Tolerate pre-existing duplicates: pick the oldest deterministically
        # instead of crashing with MultipleResultsFound.
        return db.execute(
            select(Party).where(Party.inn == inn).order_by(Party.created_at.asc())
        ).scalars().first()

    party = _by_inn() if inn else None

    # Fall back to name match (covers suppliers without INN or OCR errors in INN)
    if party is None and name:
        normalized = name.strip().upper()
        party = db.execute(
            select(Party)
            .where(
                func.upper(func.trim(Party.name)) == normalized,
                Party.role.in_([PartyRole.supplier, PartyRole.both, party_role]),
            )
            .order_by(Party.created_at.asc())
        ).scalars().first()

    if party is None:
        from sqlalchemy.exc import IntegrityError

        new_party = Party(name=name or inn, inn=inn, role=party_role)
        try:
            # SAVEPOINT so a unique-violation from a concurrent worker doesn't
            # poison the surrounding transaction. ``add`` must be INSIDE the
            # nested block so a rollback discards the pending INSERT — otherwise
            # the orphaned object is re-flushed later and crashes the txn.
            with db.begin_nested():
                db.add(new_party)
                db.flush()
            party = new_party
        except IntegrityError:
            # The SAVEPOINT rollback already discarded the pending INSERT
            # (``add`` was inside it), so just re-select the row that won.
            party = _by_inn() if inn else None
            if party is None:
                raise

    # Update fields only if extracted value is non-empty and current value is null
    def _set_if_better(attr: str, value):
        if value and not getattr(party, attr, None):
            setattr(party, attr, value)

    _set_if_better("name", name)
    _set_if_better("kpp", data.get("kpp"))
    _set_if_better("address", data.get("address"))
    _set_if_better("bank_name", data.get("bank_name"))
    _set_if_better("bank_bik", data.get("bank_bik"))
    _set_if_better("bank_account", data.get("bank_account"))
    _set_if_better("corr_account", data.get("corr_account"))
    _set_if_better("contact_phone", data.get("phone"))
    _set_if_better("contact_email", data.get("email"))
    db.flush()
    return party.id


def _checksum_issues(extracted: dict) -> list[str]:
    """Checksummable fields that are PRESENT but FAIL their control-digit check.

    A non-empty result means the invoice must not be auto-approved — wrong digits
    in an ИНН or bank account could route a payment to the wrong recipient, so it
    is held for mandatory human review (draft-first safety gate).
    """
    from app.ai import ru_validators as rv

    issues: list[str] = []
    supplier = extracted.get("supplier") or {}
    buyer = extracted.get("buyer") or {}
    bik = supplier.get("bank_bik")

    if supplier.get("inn") and not rv.inn_valid(supplier["inn"]):
        issues.append("supplier.inn")
    if buyer.get("inn") and not rv.inn_valid(buyer["inn"]):
        issues.append("buyer.inn")
    if bik and not rv.bik_valid(bik):
        issues.append("supplier.bank_bik")
    if supplier.get("bank_account") and bik and not rv.account_valid(supplier["bank_account"], bik):
        issues.append("supplier.bank_account")
    if supplier.get("corr_account") and bik and not rv.corr_account_valid(supplier["corr_account"], bik):
        issues.append("supplier.corr_account")
    return issues


@celery_app.task(name="app.tasks.extraction.auto_verify_document", bind=True, max_retries=1)
def auto_verify_document(self, document_id: str) -> dict:
    """Auto-verification: re-extract with verify models and compare.

    If all key fields agree across models → auto-approve.
    Otherwise leave as needs_review.
    """
    logger.info("auto_verify_start", document_id=document_id)

    from app.api.ai_settings import get_ai_config

    cfg = get_ai_config()
    verify_model_1 = cfg.get("verify_model_1", "")

    with _get_sync_session() as db:
        doc = db.get(Document, uuid.UUID(document_id))
        if not doc:
            return {"error": "not_found"}

        # Get main extraction
        main_extraction = db.execute(
            select(DocumentExtraction)
            .where(DocumentExtraction.document_id == doc.id)
            .order_by(DocumentExtraction.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()

        if not main_extraction:
            return {"error": "no_extraction"}

        main_data = main_extraction.structured_data or {}
        text = _get_document_text(doc)

        if not text:
            return {"error": "no_text"}

        # ── Significant-field confidence gate (user-tunable threshold) ───────
        # Auto-approve only when EVERY significant field (sums, line items,
        # payment requisites) meets the threshold AND all control-digit checksums
        # pass. Insignificant fields (address/phone/notes/free-text name) are
        # excluded so they never hold up an otherwise-correct invoice. Any
        # significant field below the threshold is surfaced for human review.
        from app.ai.confidence import (
            compute_field_confidences,
            significant_fields_confidence,
            validate_arithmetic,
        )

        threshold = float(cfg.get("auto_approve_confidence_threshold", 0.95) or 0.95)
        threshold = min(max(threshold, 0.5), 0.99)

        verrs = validate_arithmetic(main_data)
        field_confs = compute_field_confidences(
            main_data, main_data.get("field_confidences", {}) or {}, verrs
        )
        sig = significant_fields_confidence(field_confs, threshold)

        present = {fc.field_name for fc in field_confs if fc.value is not None}
        mandatory_missing = [
            f for f in ("invoice_number", "invoice_date") if f not in present
        ]

        def _hold_for_review(reason: str, **extra) -> dict:
            doc.metadata_ = {
                **(doc.metadata_ or {}),
                "low_confidence_significant": sig.low_fields,
                "auto_verify_reason": reason,
                "significant_confidence": sig.score,
                "auto_approve_threshold": threshold,
            }
            db.commit()
            logger.info(
                "auto_verify_hold_for_review",
                document_id=document_id,
                reason=reason,
                significant_confidence=sig.score,
                low_fields=[f["field"] for f in sig.low_fields],
            )
            return {
                "document_id": document_id,
                "significant_confidence": sig.score,
                "threshold": threshold,
                "auto_approved": False,
                "reject_reason": reason,
                "low_confidence_significant": sig.low_fields,
                **extra,
            }

        if mandatory_missing:
            return _hold_for_review("mandatory_fields_missing", mandatory_missing=mandatory_missing)

        # ── Checksum safety gate (hard stop) ─────────────────────────────────
        # Never auto-approve when an ИНН / bank account fails its control digit.
        checksum_problems = _checksum_issues(main_data)
        if checksum_problems:
            doc.metadata_ = {**(doc.metadata_ or {}), "checksum_issues": checksum_problems}
            return _hold_for_review("checksum_validation_failed", checksum_issues=checksum_problems)

        # ── Confidence gate ──────────────────────────────────────────────────
        if sig.low_fields:
            return _hold_for_review("significant_fields_low_confidence")

        # ── Optional consensus layer ─────────────────────────────────────────
        # When a verify model is configured, re-extract and require key fields to
        # agree at >= threshold — an extra safety net on top of the confidence +
        # checksum gates. Skipped (and not required) when no verify model is set.
        consensus = 1.0
        models_used: list[str] = []
        if verify_model_1 and verify_model_1.strip():
            logger.info("auto_verify_extracting", model=verify_model_1, document_id=document_id)
            from app.tasks.gpu_lock import gpu_single_flight
            with gpu_single_flight(f"verify:{document_id}"):
                extracted = _extract_invoice_with_model(text, verify_model_1)
            if extracted:
                models_used.append(verify_model_1)
                agree, consensus = _compare_extractions(main_data, [extracted], threshold=threshold)
                if not agree:
                    return _hold_for_review("verify_model_disagreement", consensus=consensus)
            else:
                logger.warning("auto_verify_model_failed", model=verify_model_1)

        # ── Approve ──────────────────────────────────────────────────────────
        doc.status = DocumentStatus.approved
        doc.metadata_ = {
            **(doc.metadata_ or {}),
            "low_confidence_significant": [],
            "significant_confidence": sig.score,
            "auto_approve_threshold": threshold,
            "auto_verify_reason": "auto_approved",
        }
        job = _latest_processing_job(db, doc)
        if job:
            job.current_step = "auto_verified"
        db.commit()
        # Trigger post-approval pipeline (Invoice/Party/memory/embeddings)
        process_approved_document.delay(document_id)
        logger.info(
            "auto_verify_approved",
            document_id=document_id,
            significant_confidence=sig.score,
            consensus=consensus,
            models_used=models_used,
        )
        return {
            "document_id": document_id,
            "significant_confidence": sig.score,
            "consensus": consensus,
            "threshold": threshold,
            "auto_approved": True,
            "models_used": models_used,
        }


def _invoice_memory_text(extracted: dict) -> str:
    """Compact text for graph/memory built from already-extracted invoice data
    (parties, ИНН, products, amounts) — avoids an expensive re-OCR."""
    if not extracted:
        return ""
    parts: list[str] = []
    if extracted.get("invoice_number"):
        parts.append(f"Счёт № {extracted['invoice_number']}")
    if extracted.get("invoice_date"):
        parts.append(f"от {extracted['invoice_date']}")
    for role, label in (("supplier", "Поставщик"), ("buyer", "Покупатель")):
        p = extracted.get(role) or {}
        if p.get("name"):
            line = f"{label}: {p['name']}"
            if p.get("inn"):
                line += f", ИНН {p['inn']}"
            parts.append(line)
    for li in extracted.get("lines") or []:
        desc = (li.get("description") or li.get("sku") or "").strip()
        if desc:
            parts.append(desc)
    if extracted.get("total_amount") is not None:
        parts.append(
            f"Итого: {extracted['total_amount']} {extracted.get('currency', 'RUB')}"
        )
    return ". ".join(parts)


@celery_app.task(name="app.tasks.extraction.process_approved_document", bind=True, max_retries=1)
def process_approved_document(self, document_id: str) -> dict:
    """Post-approval pipeline: create Invoice/Lines/Party, build memory graph, queue embeddings.

    Called after document status is set to 'approved' — either by manual approval or auto-verify.
    Only verified data gets written to Invoice/Party tables and the knowledge graph.
    """
    logger.info("post_approve_start", document_id=document_id)
    from sqlalchemy import delete as _sa_delete
    from sqlalchemy import update as _sa_update

    with _get_sync_session() as db:
        doc = db.get(Document, uuid.UUID(document_id))
        if not doc:
            return {"error": "not_found"}

        extraction = db.execute(
            select(DocumentExtraction)
            .where(DocumentExtraction.document_id == doc.id)
            .order_by(DocumentExtraction.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()

        if not extraction:
            logger.warning("post_approve_no_extraction", document_id=document_id)
            return {"error": "no_extraction"}

        extracted = extraction.structured_data or {}

        # Reopen the existing processing job so steps stay on the same job
        job = _latest_processing_job(db, doc)
        if job:
            job.status = "running"
            job.current_step = "sql_records"
        else:
            job = DocumentProcessingJob(
                document_id=doc.id,
                status="running",
                pipeline_steps=_default_pipeline_steps(),
                current_step="sql_records",
                started_at=datetime.now(UTC),
            )
            db.add(job)
            db.flush()

        # ── Step: sql_records ────────────────────────────────────────────────
        _set_job_step(job, "sql_records", "running")

        supplier_data = extracted.get("supplier", {}) or {}
        buyer_data = extracted.get("buyer", {}) or {}
        supplier_party_id = _upsert_party(db, supplier_data, role="supplier")
        buyer_party_id = _upsert_party(db, buyer_data, role="buyer")

        # Remove existing invoice if re-approved (e.g. re-uploaded / re-processed)
        existing = db.execute(
            select(Invoice).where(Invoice.document_id == doc.id)
        ).scalar_one_or_none()
        if existing:
            # Detach any warehouse-receipt lines that reference this invoice's
            # lines BEFORE deleting them. Re-approval must be idempotent: it must
            # not violate the warehouse_receipt_lines FK, and must not destroy a
            # receipt the warehouse flow already created. The receipt keeps its
            # quantities; only the now-stale invoice-line link is cleared.
            line_ids = (
                db.execute(
                    select(InvoiceLine.id).where(InvoiceLine.invoice_id == existing.id)
                )
                .scalars()
                .all()
            )
            if line_ids:
                db.execute(
                    _sa_update(WarehouseReceiptLine)
                    .where(WarehouseReceiptLine.invoice_line_id.in_(line_ids))
                    .values(invoice_line_id=None)
                )
            # Detach receipts that point at the invoice itself (warehouse_receipts
            # .invoice_id FK) before deleting it — same idempotency guarantee.
            db.execute(
                _sa_update(WarehouseReceipt)
                .where(WarehouseReceipt.invoice_id == existing.id)
                .values(invoice_id=None)
            )
            # Detach price-history entries (nullable FK) and drop derived payment
            # schedules (non-nullable FK) so the invoice row can be replaced.
            from app.db.models import PaymentSchedule, PriceHistoryEntry
            db.execute(
                _sa_update(PriceHistoryEntry)
                .where(PriceHistoryEntry.invoice_id == existing.id)
                .values(invoice_id=None)
            )
            db.execute(
                _sa_delete(PaymentSchedule).where(PaymentSchedule.invoice_id == existing.id)
            )
            db.execute(_sa_delete(InvoiceLine).where(InvoiceLine.invoice_id == existing.id))
            db.delete(existing)
            db.flush()

        invoice = Invoice(
            document_id=doc.id,
            invoice_number=extracted.get("invoice_number"),
            invoice_date=_parse_date(extracted.get("invoice_date")),
            due_date=_parse_date(extracted.get("due_date")),
            validity_date=_parse_date(extracted.get("validity_date")),
            currency=extracted.get("currency", "RUB"),
            subtotal=extracted.get("subtotal"),
            tax_amount=extracted.get("tax_amount"),
            total_amount=extracted.get("total_amount"),
            payment_id=extracted.get("payment_id"),
            notes=extracted.get("notes"),
            supplier_id=supplier_party_id,
            buyer_id=buyer_party_id,
            status=InvoiceStatus.approved,
            overall_confidence=extraction.overall_confidence,
        )
        db.add(invoice)
        db.flush()

        for line_data in extracted.get("lines", []):
            db.add(InvoiceLine(
                invoice_id=invoice.id,
                line_number=line_data.get("line_number", 0),
                sku=line_data.get("sku"),
                description=line_data.get("description"),
                quantity=line_data.get("quantity"),
                unit=line_data.get("unit"),
                unit_price=line_data.get("unit_price"),
                amount=line_data.get("amount"),
                tax_rate=line_data.get("tax_rate"),
                tax_amount=line_data.get("tax_amount"),
                weight=line_data.get("weight"),
            ))

        _set_job_step(job, "sql_records", "done")

        # ── Auto-create pending warehouse receipt ────────────────────────────
        try:
            invoice_lines = db.execute(
                select(InvoiceLine).where(InvoiceLine.invoice_id == invoice.id)
            ).scalars().all()
            if invoice_lines:
                from sqlalchemy import func as _func
                receipt_count = db.execute(
                    select(_func.count()).select_from(WarehouseReceipt)
                ).scalar() or 0
                receipt_number = f"ПО-{receipt_count + 1:04d}"
                pending_receipt = WarehouseReceipt(
                    invoice_id=invoice.id,
                    document_id=doc.id,
                    supplier_id=invoice.supplier_id,
                    status="pending",
                    receipt_number=receipt_number,
                    received_by="auto",
                )
                db.add(pending_receipt)
                db.flush()
                for il in invoice_lines:
                    db.add(WarehouseReceiptLine(
                        receipt_id=pending_receipt.id,
                        description=il.description or "",
                        quantity_expected=il.quantity or 0,
                        quantity_received=il.quantity or 0,
                        unit=il.unit or "шт",
                        invoice_line_id=il.id,
                    ))
                logger.info(
                    "auto_pending_receipt_created",
                    document_id=document_id,
                    receipt_number=receipt_number,
                    lines=len(invoice_lines),
                )
        except Exception as e:
            logger.warning("auto_pending_receipt_failed", document_id=document_id, error=str(e))

        # ── SupplierProfile update ───────────────────────────────────────────
        if supplier_party_id:
            from app.db.models import SupplierProfile
            profile = db.execute(
                select(SupplierProfile).where(SupplierProfile.party_id == supplier_party_id)
            ).scalar_one_or_none()
            if not profile:
                profile = SupplierProfile(party_id=supplier_party_id, total_invoices=0, total_amount=0.0)
                db.add(profile)
            db.flush()
            profile.total_invoices = (profile.total_invoices or 0) + 1
            if invoice.total_amount:
                profile.total_amount = (profile.total_amount or 0.0) + float(invoice.total_amount)
            if invoice.invoice_date:
                # Compare tz-safely: a stored date read back without tzinfo (e.g.
                # from a backend that drops it) must not raise against the
                # tz-aware parsed invoice_date.
                _inv_dt = invoice.invoice_date
                _last_dt = profile.last_invoice_date
                if _inv_dt.tzinfo is None:
                    _inv_dt = _inv_dt.replace(tzinfo=UTC)
                if _last_dt is not None and _last_dt.tzinfo is None:
                    _last_dt = _last_dt.replace(tzinfo=UTC)
                if _last_dt is None or _inv_dt > _last_dt:
                    profile.last_invoice_date = invoice.invoice_date

        # ── Step: memory_graph ───────────────────────────────────────────────
        # Build the graph from the already-extracted data — do NOT re-OCR here
        # (re-running the vision model under concurrent approvals starves the GPU
        # and trips the Celery soft time limit).
        text = _invoice_memory_text(extracted) or (doc.file_name or "")
        try:
            from app.domain.memory_builder import build_document_memory_sync
            _set_job_step(job, "memory_graph", "running")
            memory_result = build_document_memory_sync(db, doc, text=text)
            _set_job_step(job, "memory_graph", "done")
            logger.info(
                "post_approve_memory_built",
                document_id=document_id,
                chunks=memory_result.chunks_created,
                mentions=memory_result.mentions_created,
                edges=memory_result.edges_created,
            )
        except Exception as e:
            _set_job_step(job, "memory_graph", "failed", error=str(e))
            logger.warning("post_approve_memory_failed", document_id=document_id, error=str(e))

        # ── Step: embedding ──────────────────────────────────────────────────
        if _step_status(job, "embedding") != "done":
            _set_job_step(job, "embedding", "queued")

        _finish_job(job, "done")
        db.commit()

        # Dispatch embedding and anomaly tasks after commit
        from app.tasks.embedding import embed_document
        embed_document.delay(document_id)
        check_invoice_anomalies.delay(str(invoice.id))

        logger.info(
            "post_approve_done",
            document_id=document_id,
            invoice_id=str(invoice.id),
            supplier_party_id=str(supplier_party_id) if supplier_party_id else None,
            lines=len(extracted.get("lines", [])),
        )

        try:
            from app.core.metrics import extraction_duration_seconds
            extraction_duration_seconds.observe(time.monotonic() - _t0)
        except Exception:
            pass
        return {
            "document_id": document_id,
            "invoice_id": str(invoice.id),
            "supplier_party_id": str(supplier_party_id) if supplier_party_id else None,
            "lines": len(extracted.get("lines", [])),
        }


def _normalize_company_name(name: str) -> str:
    """Strip common Russian legal form prefixes for fuzzy comparison."""
    import re as _re
    s = name.lower().strip()
    for pat in [
        r'общество с ограниченной ответственностью',
        r'акционерное общество',
        r'закрытое акционерное общество',
        r'публичное акционерное общество',
        r'индивидуальный предприниматель',
        r'\bооо\b', r'\bао\b', r'\bзао\b', r'\bпао\b', r'\bип\b', r'\bгуп\b', r'\bмуп\b',
    ]:
        s = _re.sub(pat, '', s)
    s = _re.sub(r'["\'\«\»\(\)]', '', s)
    return _re.sub(r'\s+', ' ', s).strip()


def _llm_match_supplier_name(new_name: str, existing_parties: list) -> "uuid.UUID | None":
    """LLM-based supplier name deduplication. INN match should be tried first."""
    import json as _json
    import re as _re
    import httpx
    from app.config import settings

    if not new_name or not existing_parties:
        return None

    # Fast path: normalized string match
    norm_new = _normalize_company_name(new_name)
    for p in existing_parties:
        if _normalize_company_name(p.name) == norm_new:
            logger.info("supplier_norm_matched", new=new_name, existing=p.name)
            return p.id

    # LLM path — route through AIRouter (classification, local-only).
    names_list = "\n".join(f"{i + 1}. {p.name}" for i, p in enumerate(existing_parties[:30]))
    prompt = (
        f'Task: decide if the new company is the same legal entity as any in the list.\n'
        f'New company: "{new_name}"\n'
        f'Existing companies:\n{names_list}\n\n'
        f'Rules: ООО = Общество с ограниченной ответственностью, '
        f'АО = Акционерное общество, ЗАО = Закрытое АО, ИП = Индивидуальный предприниматель.\n'
        f'Answer JSON only: {{"match": true/false, "index": <1-based int or null>}}'
    )
    try:
        from app.ai.router import ai_router
        from app.ai.schemas import AIRequest, AITask, ChatMessage

        response = _run_async(
            ai_router.run(
                AIRequest(
                    task=AITask.CLASSIFICATION,
                    messages=[ChatMessage(role="user", content=prompt)],
                    confidential=True,
                )
            )
        )
        content = response.text or ""
        m = _re.search(r'\{.*?\}', content, _re.DOTALL)
        if m:
            result = _json.loads(m.group())
            if result.get("match") and result.get("index"):
                idx = int(result["index"]) - 1
                if 0 <= idx < len(existing_parties):
                    matched = existing_parties[idx]
                    logger.info("llm_supplier_matched", new=new_name, matched=matched.name)
                    return matched.id
    except Exception as e:
        logger.warning("llm_supplier_match_failed", error=str(e))
    return None


@celery_app.task(name="app.tasks.extraction.auto_supplier_task", bind=True, max_retries=1)
def auto_supplier_task(self, document_id: str) -> dict:
    """After document approval: match or create supplier Party, update SupplierProfile."""
    logger.info("auto_supplier_start", document_id=document_id)

    from app.db.models import Invoice, DocumentLink, SupplierProfile, Party, PartyRole

    with _get_sync_session() as db:
        doc = db.get(Document, uuid.UUID(document_id))
        if not doc:
            return {"error": "not_found"}

        # Find linked invoice
        invoice = db.execute(
            select(Invoice).where(Invoice.document_id == doc.id)
        ).scalar_one_or_none()

        if not invoice:
            link = db.execute(
                select(DocumentLink).where(
                    DocumentLink.document_id == doc.id,
                    DocumentLink.linked_entity_type == "invoice",
                )
            ).scalar_one_or_none()
            if link:
                invoice = db.get(Invoice, link.linked_entity_id)

        if not invoice:
            return {"error": "no_invoice"}

        # Get extraction structured_data for full supplier info
        extraction = db.execute(
            select(DocumentExtraction)
            .where(DocumentExtraction.document_id == doc.id)
            .order_by(DocumentExtraction.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()

        supplier_data: dict = {}
        if extraction and extraction.structured_data:
            supplier_data = extraction.structured_data.get("supplier") or {}

        supplier_name = supplier_data.get("name")
        supplier_inn = supplier_data.get("inn")

        party_id = invoice.supplier_id

        if not party_id:
            # 1. INN exact match
            if supplier_inn:
                existing = db.execute(
                    select(Party).where(Party.inn == supplier_inn)
                ).scalar_one_or_none()
                if existing:
                    party_id = existing.id

            # 2. Email match + similar name
            supplier_email = supplier_data.get("email")
            if not party_id and supplier_email and supplier_name:
                existing = db.execute(
                    select(Party).where(Party.contact_email.ilike(supplier_email))
                ).scalar_one_or_none()
                if existing and (
                    supplier_name.lower() in existing.name.lower()
                    or existing.name.lower() in supplier_name.lower()
                ):
                    party_id = existing.id

            # 3. Phone match + similar name
            supplier_phone = supplier_data.get("phone")
            if not party_id and supplier_phone and supplier_name:
                import re as _re
                def _norm(p: str) -> str:
                    d = _re.sub(r"\D", "", p)
                    return ("7" + d[1:]) if len(d) == 11 and d.startswith("8") else d

                norm_phone = _norm(supplier_phone)
                candidates_phone = db.execute(
                    select(Party).where(Party.contact_phone.isnot(None))
                ).scalars().all()
                for cp in candidates_phone:
                    if cp.contact_phone and _norm(cp.contact_phone) == norm_phone:
                        if (supplier_name.lower() in cp.name.lower()
                                or cp.name.lower() in supplier_name.lower()):
                            party_id = cp.id
                            break

            # 4. LLM name match
            if not party_id and supplier_name:
                candidates = db.execute(
                    select(Party).where(Party.role.in_(["supplier", "both"])).limit(50)
                ).scalars().all()
                party_id = _llm_match_supplier_name(supplier_name, list(candidates))

            # 5. Create new Party
            if not party_id and (supplier_name or supplier_inn):
                new_party = Party(
                    name=supplier_name or supplier_inn,
                    inn=supplier_inn,
                    role=PartyRole.supplier,
                    kpp=supplier_data.get("kpp"),
                    address=supplier_data.get("address"),
                    bank_name=supplier_data.get("bank_name"),
                    bank_bik=supplier_data.get("bank_bik"),
                    bank_account=supplier_data.get("bank_account"),
                    corr_account=supplier_data.get("corr_account"),
                    contact_phone=supplier_data.get("phone"),
                    contact_email=supplier_data.get("email"),
                )
                db.add(new_party)
                db.flush()
                party_id = new_party.id
                logger.info("auto_supplier_created", party_id=str(party_id), name=supplier_name)

            if party_id:
                invoice.supplier_id = party_id
                # Update any party fields that were missing
                party = db.get(Party, party_id)
                if party and supplier_data:
                    def _fill(attr, val):
                        if val and not getattr(party, attr, None):
                            setattr(party, attr, val)
                    _fill("kpp", supplier_data.get("kpp"))
                    _fill("address", supplier_data.get("address"))
                    _fill("bank_name", supplier_data.get("bank_name"))
                    _fill("bank_bik", supplier_data.get("bank_bik"))
                    _fill("bank_account", supplier_data.get("bank_account"))
                    _fill("corr_account", supplier_data.get("corr_account"))
                    _fill("contact_phone", supplier_data.get("phone"))
                    _fill("contact_email", supplier_data.get("email"))

        # Update SupplierProfile stats
        if party_id:
            profile = db.execute(
                select(SupplierProfile).where(SupplierProfile.party_id == party_id)
            ).scalar_one_or_none()
            if not profile:
                profile = SupplierProfile(party_id=party_id, total_invoices=0, total_amount=0.0)
                db.add(profile)

            profile.total_invoices = (profile.total_invoices or 0) + 1
            if invoice.total_amount:
                profile.total_amount = (profile.total_amount or 0.0) + float(invoice.total_amount)
            if invoice.invoice_date:
                # Compare tz-safely: a stored date read back without tzinfo (e.g.
                # from a backend that drops it) must not raise against the
                # tz-aware parsed invoice_date.
                _inv_dt = invoice.invoice_date
                _last_dt = profile.last_invoice_date
                if _inv_dt.tzinfo is None:
                    _inv_dt = _inv_dt.replace(tzinfo=UTC)
                if _last_dt is not None and _last_dt.tzinfo is None:
                    _last_dt = _last_dt.replace(tzinfo=UTC)
                if _last_dt is None or _inv_dt > _last_dt:
                    profile.last_invoice_date = invoice.invoice_date

            db.commit()
            logger.info("auto_supplier_done", document_id=document_id, party_id=str(party_id))
            return {"party_id": str(party_id)}

        logger.warning("auto_supplier_no_data", document_id=document_id)
        return {"error": "no_supplier_data"}


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        from datetime import date
        d = date.fromisoformat(value)
        return datetime(d.year, d.month, d.day, tzinfo=UTC)
    except (ValueError, TypeError):
        return None


def _fallback_from_filename(filename: str, extracted: dict) -> None:
    """Fill missing invoice_number / invoice_date from the file name.

    Handles patterns common in Russian invoice filenames:
      "ВЕКПРОМ № KA-15203 от 04.10.2024(1).pdf"
      "Счёт №1234 от 2024-01-15.pdf"
    Mutates *extracted* in place; only fills null/absent fields.
    """
    import re

    if not filename:
        return

    stem = filename.rsplit(".", 1)[0]

    if not extracted.get("invoice_number"):
        # "№ KA-15203", "№1234", "N 5678"
        m = re.search(r"[№N]\s*([A-Za-zА-Яа-я0-9\-/]+)", stem)
        if m:
            extracted["invoice_number"] = m.group(1)
            logger.info("fallback_invoice_number_from_filename", value=m.group(1))

    if not extracted.get("invoice_date"):
        # "от 04.10.2024" → ISO "2024-10-04"
        m = re.search(r"от\s+(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{4})", stem)
        if m:
            d, mo, y = m.group(1).zfill(2), m.group(2).zfill(2), m.group(3)
            extracted["invoice_date"] = f"{y}-{mo}-{d}"
            logger.info("fallback_invoice_date_from_filename", value=extracted["invoice_date"])
            return
        # ISO in filename: "2024-10-04"
        m = re.search(r"(\d{4})-(\d{2})-(\d{2})", stem)
        if m:
            extracted["invoice_date"] = m.group(0)
            logger.info("fallback_invoice_date_from_filename", value=m.group(0))


def _autocorrect_impossible_total(extracted: dict) -> None:
    """When total_amount < subtotal (physically impossible), recompute from subtotal + tax.

    Mutates *extracted* in place. The validator will still flag total_amount as
    arithmetic_error (it can't know we corrected it), but confidence scoring uses
    the corrected value, which satisfies the VAT formula and passes validation.
    """
    subtotal = extracted.get("subtotal")
    tax_amount = extracted.get("tax_amount")
    total = extracted.get("total_amount")

    if subtotal is None or total is None:
        return

    try:
        s, g = float(subtotal), float(total)
    except (TypeError, ValueError):
        return

    if g < s:
        corrected = round(s + (float(tax_amount) if tax_amount is not None else 0.0), 2)
        logger.warning(
            "autocorrect_impossible_total",
            original=g,
            corrected=corrected,
            subtotal=s,
        )
        extracted["total_amount"] = corrected
        extracted["_total_autocorrected"] = True


@celery_app.task(name="app.tasks.extraction.check_invoice_anomalies", bind=True, max_retries=1)
def check_invoice_anomalies(self, invoice_id: str) -> dict:
    """Run all anomaly detectors on a newly approved invoice (fire-and-forget)."""
    logger.info("anomaly_check_start", invoice_id=invoice_id)
    try:
        result = _run_async(_run_anomaly_check(invoice_id))
        logger.info(
            "anomaly_check_done",
            invoice_id=invoice_id,
            found=result.get("found", 0),
        )
        return result
    except Exception as e:
        logger.warning("anomaly_check_failed", invoice_id=invoice_id, error=str(e))
        return {"error": str(e)}


async def _run_anomaly_check(invoice_id: str) -> dict:
    """Async inner: reuse detector logic from anomalies API."""
    from sqlalchemy.orm import selectinload as _selectinload
    from app.db.models import AnomalyCard, Invoice
    from app.db.session import _get_session_factory

    factory = _get_session_factory()
    async with factory() as db:
        from sqlalchemy.ext.asyncio import AsyncSession
        from sqlalchemy import select as _select

        result = await db.execute(
            _select(Invoice)
            .where(Invoice.id == uuid.UUID(invoice_id))
            .options(
                _selectinload(Invoice.lines),
                _selectinload(Invoice.supplier),
            )
        )
        invoice = result.scalar_one_or_none()
        if not invoice:
            return {"error": "invoice_not_found"}

        from app.api.anomalies import (
            _detect_duplicate,
            _detect_new_supplier,
            _detect_price_spike,
            _detect_requisite_change,
            _detect_unknown_items,
        )

        anomalies: list[AnomalyCard] = []
        for detector in (
            _detect_duplicate,
            _detect_new_supplier,
            _detect_requisite_change,
            _detect_price_spike,
            _detect_unknown_items,
        ):
            try:
                card = await detector(db, invoice)
                if card:
                    anomalies.append(card)
            except Exception as exc:
                logger.warning(
                    "anomaly_detector_failed",
                    detector=detector.__name__,
                    error=str(exc),
                )

        for a in anomalies:
            db.add(a)
        if anomalies:
            await db.commit()

        return {"found": len(anomalies), "invoice_id": invoice_id}
