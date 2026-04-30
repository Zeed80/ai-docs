"""Extraction Celery tasks — classify, extract, validate.

Pipeline: classify → extract → validate → update document status.
Runs on 'extraction' queue.
"""

import asyncio
import base64
import subprocess
import tempfile
import uuid
from datetime import UTC, datetime

import structlog
from sqlalchemy import create_engine, select
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
    """Run async function from sync Celery task."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                return pool.submit(asyncio.run, coro).result()
        return loop.run_until_complete(coro)
    except RuntimeError:
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
    """Classify document type using gemma4:e4b.

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

        # Get PDF text
        text = _get_document_text(doc)
        if not text:
            logger.warning("classify_no_text", document_id=document_id)
            doc.status = DocumentStatus.needs_review
            error = "No text extracted from document"
            _set_job_step(job, "classification", "failed", error=error)
            _finish_job(job, "failed", error=error)
            db.commit()
            return {"error": error}

        # Classify via Ollama
        try:
            from app.ai.router import ai_router

            result = _run_async(ai_router.classify_document(text))

            doc_type = result.get("type", "other")
            confidence = result.get("confidence", 0.5)

            # Validate type
            valid_types = {t.value for t in DocumentType}
            if doc_type not in valid_types:
                doc_type = "other"

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

            # Chain: if invoice → extract
            if doc_type == "invoice":
                _set_job_step(job, "extraction", "queued")
                extract_invoice.delay(document_id)
                db.commit()
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
            logger.error("classify_error", document_id=document_id, error=str(e))
            doc.status = DocumentStatus.needs_review
            _set_job_step(job, "classification", "failed", error=str(e))
            _finish_job(job, "failed", error=str(e))
            db.commit()
            self.retry(countdown=30, exc=e)
            return {"error": str(e)}


@celery_app.task(name="app.tasks.extraction.extract_invoice", bind=True, max_retries=2)
def extract_invoice(self, document_id: str) -> dict:
    """Extract invoice fields using gemma4:e4b.

    Creates DocumentExtraction, ExtractionFields, Invoice, InvoiceLines.
    """
    logger.info("extract_start", document_id=document_id)
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

            extracted = _run_async(ai_router.extract_invoice(text))

            processing_time_ms = int((time.time() - start_time) * 1000)

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
        extraction = DocumentExtraction(
            document_id=doc.id,
            model_name=settings.ollama_model_ocr,
            raw_output=extracted,
            structured_data=extracted,
            overall_confidence=overall_confidence,
            processing_time_ms=processing_time_ms,
        )
        db.add(extraction)
        db.flush()
        _set_job_step(job, "extraction", "done")
        _set_job_step(job, "sql_records", "running")

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

        # Upsert supplier Party from extracted data
        supplier_data = extracted.get("supplier", {}) or {}
        buyer_data = extracted.get("buyer", {}) or {}
        supplier_party_id = _upsert_party(db, supplier_data, role="supplier")
        buyer_party_id = _upsert_party(db, buyer_data, role="buyer")

        # Delete existing invoice for this document (re-extraction)
        existing = db.execute(
            select(Invoice).where(Invoice.document_id == doc.id)
        ).scalar_one_or_none()
        if existing:
            db.execute(
                __import__("sqlalchemy", fromlist=["delete"]).delete(InvoiceLine).where(
                    InvoiceLine.invoice_id == existing.id
                )
            )
            db.delete(existing)
            db.flush()

        # Create Invoice
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
            status=InvoiceStatus.needs_review,
            overall_confidence=overall_confidence,
        )

        db.add(invoice)
        db.flush()

        for line_data in extracted.get("lines", []):
            line = InvoiceLine(
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
            )
            db.add(line)

        doc.status = DocumentStatus.needs_review
        _set_job_step(job, "sql_records", "done")

        try:
            from app.domain.memory_builder import build_document_memory_sync

            _set_job_step(job, "memory_graph", "running")
            memory_result = build_document_memory_sync(db, doc, text=text)
            _set_job_step(job, "memory_graph", "done")
            logger.info(
                "document_memory_built",
                document_id=document_id,
                chunks=memory_result.chunks_created,
                mentions=memory_result.mentions_created,
                edges=memory_result.edges_created,
            )
        except Exception as e:
            _set_job_step(job, "memory_graph", "failed", error=str(e))
            logger.warning("document_memory_build_failed", document_id=document_id, error=str(e))

        if _step_status(job, "embedding") != "done":
            _set_job_step(job, "embedding", "queued")
        _finish_job(job, "done")
        db.commit()

        logger.info(
            "extract_done",
            document_id=document_id,
            invoice_id=str(invoice.id),
            confidence=overall_confidence,
            lines=len(extracted.get("lines", [])),
            processing_ms=processing_time_ms,
            validation_errors=len(validation_errors),
        )

        return {
            "document_id": document_id,
            "invoice_id": str(invoice.id),
            "overall_confidence": overall_confidence,
            "line_count": len(extracted.get("lines", [])),
            "validation_errors": validation_errors,
        }


@celery_app.task(name="app.tasks.extraction.process_document")
def process_document(document_id: str, force: bool = False) -> dict:
    """Full pipeline: classify → extract → validate.

    Entry point for newly ingested documents.
    """
    return classify_document(document_id, force)


def _get_document_text(doc: Document) -> str:
    """Get text from a document — try PDF extraction, fallback to stored text."""
    content = _download_document(doc)
    if not content:
        return ""

    if doc.mime_type == "application/pdf":
        try:
            from app.ai.pdf_processor import extract_pdf
            pdf_data = extract_pdf(content, render_pages=False)
            if pdf_data.full_text.strip():
                return pdf_data.full_text
            return _ocr_pdf_content(content, doc)
        except Exception as e:
            logger.warning("pdf_text_extraction_failed", error=str(e))
            return _ocr_pdf_content(content, doc)

    if doc.mime_type.startswith("image/"):
        return _ocr_image_content(content, doc.mime_type, doc)

    # Plain text
    try:
        return content.decode("utf-8", errors="replace")
    except Exception:
        return ""


def _ocr_image_content(content: bytes, mime_type: str, doc: Document) -> str:
    """OCR an image through the local vision model."""
    tesseract_text = _ocr_image_with_tesseract(content, mime_type, doc)
    if _looks_like_document_text(tesseract_text):
        return tesseract_text

    try:
        from app.ai.router import ai_router
        from app.ai.schemas import AIRequest, AITask, ChatMessage

        encoded = base64.b64encode(content).decode("ascii")
        data_uri = f"data:{mime_type};base64,{encoded}"
        response = _run_async(
            ai_router.run(
                AIRequest(
                    task=AITask.INVOICE_OCR,
                    messages=[
                        ChatMessage(
                            role="user",
                            content=(
                                "Распознай весь видимый текст документа. "
                                "Сохрани номера, даты, ИНН/КПП, суммы, наименования, "
                                "табличные строки и единицы измерения. Верни только текст."
                            ),
                        )
                    ],
                    images=[data_uri],
                    confidential=True,
                    metadata={"document_id": str(doc.id), "local_only": True},
                )
            )
        )
        text = response.text or ""
        logger.info("image_ocr_done", document_id=str(doc.id), text_len=len(text))
        if _looks_like_document_text(text):
            return text
        if tesseract_text.strip():
            return tesseract_text
        return text
    except Exception as e:
        logger.warning("image_ocr_failed", document_id=str(doc.id), error=str(e))
        return tesseract_text


def _ocr_pdf_content(content: bytes, doc: Document) -> str:
    """Render the first pages of a scanned PDF and OCR them through vision."""
    try:
        import fitz

        images: list[str] = []
        tesseract_pages: list[str] = []
        with fitz.open(stream=content, filetype="pdf") as pdf:
            for page in list(pdf)[:3]:
                pixmap = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5), alpha=False)
                png_content = pixmap.tobytes("png")
                page_text = _ocr_image_with_tesseract(png_content, "image/png", doc)
                if page_text.strip():
                    tesseract_pages.append(page_text)
                encoded = base64.b64encode(png_content).decode("ascii")
                images.append(f"data:image/png;base64,{encoded}")
        tesseract_text = "\n\n".join(tesseract_pages).strip()
        if _looks_like_document_text(tesseract_text):
            return tesseract_text
        if not images:
            return ""

        from app.ai.router import ai_router
        from app.ai.schemas import AIRequest, AITask, ChatMessage

        response = _run_async(
            ai_router.run(
                AIRequest(
                    task=AITask.INVOICE_OCR,
                    messages=[
                        ChatMessage(
                            role="user",
                            content=(
                                "Распознай текст этих страниц PDF. "
                                "Сохрани номера, даты, реквизиты, суммы и таблицы. "
                                "Верни только текст."
                            ),
                        )
                    ],
                    images=images,
                    confidential=True,
                    metadata={"document_id": str(doc.id), "local_only": True},
                )
            )
        )
        text = response.text or ""
        logger.info("pdf_ocr_done", document_id=str(doc.id), text_len=len(text))
        if _looks_like_document_text(text):
            return text
        return tesseract_text or text
    except Exception as e:
        logger.warning("pdf_ocr_failed", document_id=str(doc.id), error=str(e))
        return ""


def _ocr_image_with_tesseract(content: bytes, mime_type: str, doc: Document) -> str:
    suffix = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/tiff": ".tiff",
        "image/bmp": ".bmp",
    }.get(mime_type, ".img")
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=True) as temp:
            temp.write(content)
            temp.flush()
            result = subprocess.run(
                [
                    "tesseract",
                    temp.name,
                    "stdout",
                    "-l",
                    "rus+eng",
                    "--psm",
                    "6",
                    "--dpi",
                    "300",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=90,
            )
        text = _normalize_ocr_text(result.stdout)
        if result.returncode == 0 and text:
            logger.info("tesseract_ocr_done", document_id=str(doc.id), text_len=len(text))
            return text
        if result.stderr:
            logger.warning(
                "tesseract_ocr_failed",
                document_id=str(doc.id),
                error=result.stderr[-500:],
            )
    except FileNotFoundError:
        logger.warning("tesseract_not_installed", document_id=str(doc.id))
    except subprocess.TimeoutExpired:
        logger.warning("tesseract_ocr_timeout", document_id=str(doc.id))
    except Exception as e:
        logger.warning("tesseract_ocr_error", document_id=str(doc.id), error=str(e))
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


def _upsert_party(db: Session, data: dict, role: str) -> uuid.UUID | None:
    """Create or update a Party from extracted supplier/buyer data. Returns party.id or None."""
    if not data:
        return None
    name = data.get("name")
    inn = data.get("inn")
    if not name and not inn:
        return None

    from app.db.models import Party, PartyRole

    party = None
    if inn:
        party = db.execute(
            select(Party).where(Party.inn == inn)
        ).scalar_one_or_none()

    if party is None:
        try:
            party_role = PartyRole(role)
        except ValueError:
            party_role = PartyRole.supplier
        party = Party(
            name=name or inn,
            inn=inn,
            role=party_role,
        )
        db.add(party)
        db.flush()

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


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        from datetime import date
        d = date.fromisoformat(value)
        return datetime(d.year, d.month, d.day, tzinfo=UTC)
    except (ValueError, TypeError):
        return None
