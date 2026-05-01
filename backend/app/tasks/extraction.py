"""Extraction Celery tasks — classify, extract, validate.

Pipeline: classify → extract → validate → update document status.
Runs on 'extraction' queue.
"""

import asyncio
import base64
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

        # Check if auto_verify is requested
        doc_meta = doc.metadata_ or {}
        if doc_meta.get("auto_verify"):
            auto_verify_document.delay(document_id)
            logger.info("auto_verify_queued", document_id=document_id)

        db.commit()

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


def _get_configured_ocr_model() -> str:
    """Read OCR model name from ai_config.json, fall back to config default."""
    try:
        from app.api.ai_settings import get_ai_config
        return get_ai_config().get("model_ocr") or settings.ollama_model_ocr
    except Exception:
        return settings.ollama_model_ocr


_VISION_FALLBACK_MODELS = ["gemma4:e4b", "gemma4:e2b", "gemma4:31b"]


def _ollama_vision_ocr(images_b64: list[str], model_name: str, prompt: str) -> str:
    """Call Ollama vision API synchronously. images_b64 must be raw base64 (no data: prefix).

    If the configured model returns empty (no vision support), automatically retries
    with known vision-capable fallback models.
    """
    import httpx

    def _call(model: str) -> str:
        resp = httpx.post(
            f"{str(settings.ollama_url).rstrip('/')}/api/chat",
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt, "images": images_b64}],
                "stream": False,
                "options": {"temperature": 0.0},
            },
            timeout=300.0,
        )
        resp.raise_for_status()
        return resp.json().get("message", {}).get("content", "")

    # Try the configured model first
    try:
        text = _call(model_name)
        if text.strip():
            return text
        logger.warning("ollama_vision_ocr_empty", model=model_name)
    except Exception as e:
        logger.warning("ollama_vision_ocr_failed", model=model_name, error=str(e))

    # Fallback to known vision-capable models
    for fallback in _VISION_FALLBACK_MODELS:
        if fallback == model_name:
            continue
        try:
            text = _call(fallback)
            if text.strip():
                logger.info("ollama_vision_ocr_fallback_used", primary=model_name, fallback=fallback)
                return text
        except Exception as e:
            logger.warning("ollama_vision_ocr_fallback_failed", model=fallback, error=str(e))

    return ""


def _ocr_image_content(content: bytes, mime_type: str, doc: Document) -> str:
    """OCR an image using the configured OCR model."""
    model = _get_configured_ocr_model()
    logger.info("image_ocr_start", document_id=str(doc.id), model=model)
    encoded = base64.b64encode(content).decode("ascii")
    text = _ollama_vision_ocr(
        [encoded],
        model,
        (
            "Распознай весь видимый текст документа. "
            "Сохрани номера, даты, ИНН/КПП, суммы, наименования, "
            "табличные строки и единицы измерения. Верни только текст."
        ),
    )
    logger.info("image_ocr_done", document_id=str(doc.id), model=model, text_len=len(text))
    return text


def _ocr_pdf_content(content: bytes, doc: Document) -> str:
    """Render the first pages of a scanned PDF and OCR them using the configured model."""
    try:
        import fitz

        images_b64: list[str] = []
        with fitz.open(stream=content, filetype="pdf") as pdf:
            for page in list(pdf)[:3]:
                pixmap = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5), alpha=False)
                png_bytes = pixmap.tobytes("png")
                images_b64.append(base64.b64encode(png_bytes).decode("ascii"))
        if not images_b64:
            return ""

        model = _get_configured_ocr_model()
        logger.info("pdf_ocr_start", document_id=str(doc.id), model=model, pages=len(images_b64))
        text = _ollama_vision_ocr(
            images_b64,
            model,
            (
                "Распознай текст этих страниц PDF. "
                "Сохрани номера, даты, реквизиты, суммы и таблицы. "
                "Верни только текст."
            ),
        )
        logger.info("pdf_ocr_done", document_id=str(doc.id), model=model, text_len=len(text))
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


def _compare_extractions(main: dict, others: list[dict]) -> tuple[bool, float]:
    """Compare key invoice fields across all model extractions.

    Rules:
    - invoice_number and invoice_date are mandatory: if either is missing in
      main extraction → reject immediately (score=0).
    - For each key field present in main, check that ALL other extractions agree.
    - Score = fraction of agreeing fields. threshold = 0.95.
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
    return score >= 0.95, score


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
    verify_model_2 = cfg.get("verify_model_2", "")

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

        # Check mandatory field confidence from ExtractionField records
        from app.db.models import ExtractionField as EF
        MANDATORY_CONF_THRESHOLD = 0.95
        MANDATORY_FIELDS_CHECK = ["invoice_number", "invoice_date"]

        low_conf_mandatory: list[str] = []
        for field_name in MANDATORY_FIELDS_CHECK:
            ef = db.execute(
                select(EF)
                .where(
                    EF.extraction_id == main_extraction.id,
                    EF.field_name == field_name,
                )
            ).scalar_one_or_none()
            if ef is None or ef.field_value is None:
                low_conf_mandatory.append(f"{field_name}:missing")
            elif ef.confidence is not None and ef.confidence < MANDATORY_CONF_THRESHOLD:
                low_conf_mandatory.append(
                    f"{field_name}:{ef.confidence:.2f}"
                )

        if low_conf_mandatory:
            logger.info(
                "auto_verify_reject_low_confidence",
                document_id=document_id,
                mandatory_issues=low_conf_mandatory,
            )
            return {
                "document_id": document_id,
                "consensus": 0.0,
                "auto_approved": False,
                "reject_reason": "mandatory_fields_low_confidence",
                "mandatory_issues": low_conf_mandatory,
            }

        # Run verify extractions sequentially with all configured models
        verify_extractions: list[dict] = []
        models_used: list[str] = []

        for model_key, model_name in [
            ("verify_model_1", verify_model_1),
            ("verify_model_2", verify_model_2),
        ]:
            if not model_name or not model_name.strip():
                continue
            logger.info("auto_verify_extracting", model=model_name, document_id=document_id)
            extracted = _extract_invoice_with_model(text, model_name)
            if extracted:
                verify_extractions.append(extracted)
                models_used.append(model_name)
            else:
                logger.warning("auto_verify_model_failed", model=model_name)

        if not verify_extractions:
            logger.warning("auto_verify_no_verify_models", document_id=document_id)
            return {"error": "no_verify_models_configured"}

        should_approve, consensus = _compare_extractions(main_data, verify_extractions)

        logger.info(
            "auto_verify_result",
            document_id=document_id,
            consensus=consensus,
            should_approve=should_approve,
            models_used=models_used,
            verify_count=len(verify_extractions),
        )

        if should_approve:
            doc.status = DocumentStatus.approved
            job = _latest_processing_job(db, doc)
            if job:
                job.current_step = "auto_verified"
            db.commit()
            # Trigger post-approval pipeline (Invoice/Party/memory/embeddings)
            process_approved_document.delay(document_id)
            logger.info("auto_verify_approved", document_id=document_id, consensus=consensus)

        return {
            "document_id": document_id,
            "consensus": consensus,
            "auto_approved": should_approve,
            "models_used": models_used,
            "verify_count": len(verify_extractions),
        }


@celery_app.task(name="app.tasks.extraction.process_approved_document", bind=True, max_retries=1)
def process_approved_document(self, document_id: str) -> dict:
    """Post-approval pipeline: create Invoice/Lines/Party, build memory graph, queue embeddings.

    Called after document status is set to 'approved' — either by manual approval or auto-verify.
    Only verified data gets written to Invoice/Party tables and the knowledge graph.
    """
    logger.info("post_approve_start", document_id=document_id)
    from sqlalchemy import delete as _sa_delete

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

        # Remove existing invoice if re-approved (e.g. re-uploaded)
        existing = db.execute(
            select(Invoice).where(Invoice.document_id == doc.id)
        ).scalar_one_or_none()
        if existing:
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
                if not profile.last_invoice_date or invoice.invoice_date > profile.last_invoice_date:
                    profile.last_invoice_date = invoice.invoice_date

        # ── Step: memory_graph ───────────────────────────────────────────────
        text = _get_document_text(doc)
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

        # Dispatch embedding task after commit so the doc state is final
        from app.tasks.embedding import embed_document
        embed_document.delay(document_id)

        logger.info(
            "post_approve_done",
            document_id=document_id,
            invoice_id=str(invoice.id),
            supplier_party_id=str(supplier_party_id) if supplier_party_id else None,
            lines=len(extracted.get("lines", [])),
        )

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

    # LLM path
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
        resp = httpx.post(
            f"{str(settings.ollama_url).rstrip('/')}/api/chat",
            json={
                "model": settings.ollama_model_ocr,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
                "options": {"temperature": 0.0},
            },
            timeout=30.0,
        )
        resp.raise_for_status()
        content = resp.json().get("message", {}).get("content", "")
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

            # 2. LLM name match
            if not party_id and supplier_name:
                candidates = db.execute(
                    select(Party).where(Party.role.in_(["supplier", "both"])).limit(50)
                ).scalars().all()
                party_id = _llm_match_supplier_name(supplier_name, list(candidates))

            # 3. Create new Party
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
                if not profile.last_invoice_date or invoice.invoice_date > profile.last_invoice_date:
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
