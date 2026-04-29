from __future__ import annotations

import json

from datetime import timedelta

from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session

from backend.app.domain.models import (
    AgentAction,
    ApprovalGate,
    AuditEvent,
    AuditEventType,
    Document,
    DocumentArtifact,
    DocumentProcessingJob,
    DocumentStatus,
    DocumentVersion,
    Drawing,
    DrawingFeature,
    DraftEmail,
    EmailMessage,
    EmailThread,
    Invoice,
    InvoiceLine,
    ManufacturingCase,
    ProcessingJobStatus,
    PriceHistoryEntry,
    Supplier,
    TaskJob,
    TaskJobStatus,
    now_utc,
)
from backend.app.domain.schemas import (
    CaseCreate,
    CaseUpdate,
    DraftEmailCreate,
    DrawingAnalysisResult,
    EmailThreadCreate,
    InvoiceAnomalyCard,
    InvoiceCheckResult,
    InvoiceExtractionResult,
)


def create_case(db: Session, payload: CaseCreate, actor: str = "system") -> ManufacturingCase:
    case = ManufacturingCase(**payload.model_dump())
    db.add(case)
    db.flush()
    add_audit_event(
        db,
        event_type=AuditEventType.CASE_CREATED.value,
        message=f"Created manufacturing case: {case.title}",
        actor=actor,
        case_id=case.id,
    )
    db.commit()
    db.refresh(case)
    return case


def update_case(
    db: Session, case: ManufacturingCase, payload: CaseUpdate, actor: str = "system"
) -> ManufacturingCase:
    updates = payload.model_dump(exclude_unset=True)
    for key, value in updates.items():
        setattr(case, key, value)
    add_audit_event(
        db,
        event_type=AuditEventType.CASE_UPDATED.value,
        message=f"Updated manufacturing case: {case.title}",
        actor=actor,
        case_id=case.id,
        payload=updates,
    )
    db.commit()
    db.refresh(case)
    return case


def get_case(db: Session, case_id: str) -> ManufacturingCase | None:
    return db.get(ManufacturingCase, case_id)


def list_cases(db: Session) -> list[tuple[ManufacturingCase, int]]:
    stmt: Select = (
        select(ManufacturingCase, func.count(Document.id))
        .outerjoin(Document)
        .group_by(ManufacturingCase.id)
        .order_by(ManufacturingCase.created_at.desc())
    )
    return list(db.execute(stmt).all())


def create_document(
    db: Session,
    case: ManufacturingCase,
    filename: str,
    content_type: str | None,
    storage_path: str,
    sha256: str,
    size_bytes: int,
    status: DocumentStatus = DocumentStatus.UPLOADED,
    quarantine_reason: str | None = None,
    actor: str = "system",
) -> Document:
    document = Document(
        case_id=case.id,
        filename=filename,
        content_type=content_type,
        storage_path=storage_path,
        sha256=sha256,
        size_bytes=size_bytes,
        status=status.value,
    )
    db.add(document)
    db.flush()
    db.add(
        DocumentVersion(
            document_id=document.id,
            version=1,
            sha256=sha256,
            storage_path=storage_path,
        )
    )
    add_audit_event(
        db,
        event_type=AuditEventType.DOCUMENT_UPLOADED.value,
        message=f"Uploaded document: {filename}",
        actor=actor,
        case_id=case.id,
        document_id=document.id,
        payload={"sha256": sha256, "size_bytes": size_bytes},
    )
    if status == DocumentStatus.SUSPICIOUS:
        add_audit_event(
            db,
            event_type=AuditEventType.DOCUMENT_QUARANTINED.value,
            message=f"Quarantined suspicious document: {filename}",
            actor=actor,
            case_id=case.id,
            document_id=document.id,
            payload={
                "sha256": sha256,
                "size_bytes": size_bytes,
                "reason": quarantine_reason or "Extension is not allowlisted",
            },
        )
    db.commit()
    db.refresh(document)
    return document


def get_document(db: Session, document_id: str) -> Document | None:
    return db.get(Document, document_id)


def get_document_artifact(db: Session, artifact_id: str) -> DocumentArtifact | None:
    return db.get(DocumentArtifact, artifact_id)


def get_drawing(db: Session, drawing_id: str) -> Drawing | None:
    return db.get(Drawing, drawing_id)


def get_invoice(db: Session, invoice_id: str) -> Invoice | None:
    return db.get(Invoice, invoice_id)


def get_email_thread(db: Session, thread_id: str) -> EmailThread | None:
    return db.get(EmailThread, thread_id)


def get_draft_email(db: Session, draft_id: str) -> DraftEmail | None:
    return db.get(DraftEmail, draft_id)


def get_task_job(db: Session, task_id: str) -> TaskJob | None:
    return db.get(TaskJob, task_id)


def get_approval_gate(db: Session, gate_id: str) -> ApprovalGate | None:
    return db.get(ApprovalGate, gate_id)


def list_agent_actions(db: Session, case_id: str | None = None) -> list[AgentAction]:
    stmt = select(AgentAction).order_by(AgentAction.created_at.desc())
    if case_id:
        stmt = stmt.where(AgentAction.case_id == case_id)
    return list(db.scalars(stmt))


def list_task_jobs(db: Session, status: str | None = None, case_id: str | None = None) -> list[TaskJob]:
    stmt = select(TaskJob).order_by(TaskJob.created_at.desc())
    if status:
        stmt = stmt.where(TaskJob.status == status)
    if case_id:
        stmt = stmt.where(TaskJob.case_id == case_id)
    return list(db.scalars(stmt))


def list_approval_gates(
    db: Session,
    status: str | None = "pending",
    case_id: str | None = None,
) -> list[ApprovalGate]:
    stmt = select(ApprovalGate).order_by(ApprovalGate.created_at.desc())
    if status:
        stmt = stmt.where(ApprovalGate.status == status)
    if case_id:
        stmt = stmt.where(ApprovalGate.case_id == case_id)
    return list(db.scalars(stmt))


def list_case_documents(db: Session, case_id: str) -> list[Document]:
    return list(
        db.scalars(
            select(Document)
            .where(Document.case_id == case_id)
            .order_by(Document.created_at.desc())
        )
    )


def update_document_ai_result(
    db: Session,
    document: Document,
    *,
    event_type: str,
    status: DocumentStatus,
    ai_text: str | None,
    document_type: str | None = None,
    actor: str = "ai",
) -> Document:
    document.status = status.value
    if document_type is not None:
        document.document_type = document_type
    document.ai_summary = ai_text
    add_audit_event(
        db,
        event_type=event_type,
        message=f"AI updated document: {document.filename}",
        actor=actor,
        case_id=document.case_id,
        document_id=document.id,
        payload={"document_type": document_type, "status": status.value},
    )
    db.commit()
    db.refresh(document)
    return document


def create_processing_job(
    db: Session,
    document: Document,
    actor: str = "system",
) -> DocumentProcessingJob:
    document.status = DocumentStatus.PROCESSING.value
    job = DocumentProcessingJob(
        document_id=document.id,
        status=ProcessingJobStatus.RUNNING.value,
        started_at=now_utc(),
    )
    db.add(job)
    db.flush()
    add_audit_event(
        db,
        event_type=AuditEventType.DOCUMENT_PROCESSING_STARTED.value,
        message=f"Started document processing: {document.filename}",
        actor=actor,
        case_id=document.case_id,
        document_id=document.id,
        payload={"job_id": job.id, "status": job.status},
    )
    db.commit()
    db.refresh(job)
    db.refresh(document)
    return job


def complete_processing_job(
    db: Session,
    document: Document,
    job: DocumentProcessingJob,
    *,
    status: ProcessingJobStatus,
    parser_name: str,
    result_json: str | None,
    extracted_text: str | None,
    document_type: str | None = None,
    ai_summary: str | None = None,
    error_message: str | None = None,
    actor: str = "system",
) -> DocumentProcessingJob:
    job.status = status.value
    job.parser_name = parser_name
    job.result_json = result_json
    job.error_message = error_message
    job.finished_at = now_utc()

    document.extracted_text = extracted_text
    document.extraction_result_json = result_json
    if document_type:
        document.document_type = document_type
    if ai_summary is not None:
        document.ai_summary = ai_summary

    if status == ProcessingJobStatus.COMPLETED:
        document.status = DocumentStatus.PROCESSED.value
        event_type = AuditEventType.DOCUMENT_PROCESSING_COMPLETED.value
        message = f"Completed document processing: {document.filename}"
    elif status == ProcessingJobStatus.UNSUPPORTED:
        document.status = DocumentStatus.NEEDS_REVIEW.value
        event_type = AuditEventType.DOCUMENT_PROCESSING_FAILED.value
        message = f"Document processing needs review: {document.filename}"
    else:
        document.status = DocumentStatus.PROCESSING_FAILED.value
        event_type = AuditEventType.DOCUMENT_PROCESSING_FAILED.value
        message = f"Document processing failed: {document.filename}"

    add_audit_event(
        db,
        event_type=event_type,
        message=message,
        actor=actor,
        case_id=document.case_id,
        document_id=document.id,
        payload={
            "job_id": job.id,
            "status": job.status,
            "parser_name": parser_name,
            "error_message": error_message,
        },
    )
    db.commit()
    db.refresh(job)
    db.refresh(document)
    return job


def create_document_artifacts(
    db: Session,
    document: Document,
    artifacts: list[dict],
    actor: str = "system",
) -> list[DocumentArtifact]:
    created: list[DocumentArtifact] = []
    for payload in artifacts:
        artifact = DocumentArtifact(
            document_id=document.id,
            artifact_type=payload["artifact_type"],
            storage_path=payload["storage_path"],
            content_type=payload.get("content_type"),
            page_number=payload.get("page_number"),
            width=payload.get("width"),
            height=payload.get("height"),
            metadata_json=json.dumps(payload.get("metadata", {}), ensure_ascii=False),
        )
        db.add(artifact)
        db.flush()
        created.append(artifact)
        add_audit_event(
            db,
            event_type=AuditEventType.DOCUMENT_ARTIFACT_CREATED.value,
            message=f"Created document artifact: {artifact.artifact_type}",
            actor=actor,
            case_id=document.case_id,
            document_id=document.id,
            payload={
                "artifact_id": artifact.id,
                "artifact_type": artifact.artifact_type,
                "page_number": artifact.page_number,
            },
        )
    db.commit()
    for artifact in created:
        db.refresh(artifact)
    db.refresh(document)
    return created


def create_drawing_analysis(
    db: Session,
    document: Document,
    analysis: DrawingAnalysisResult,
    actor: str = "ai",
) -> Drawing:
    drawing = Drawing(
        case_id=document.case_id,
        document_id=document.id,
        title=analysis.title or document.filename,
        drawing_number=analysis.drawing_number,
        revision=analysis.revision,
        material_hint=analysis.material_hint,
        analysis_json=analysis.model_dump_json(),
    )
    db.add(drawing)
    db.flush()
    for feature in analysis.features:
        db.add(
            DrawingFeature(
                drawing_id=drawing.id,
                feature_type=feature.feature_type,
                description=feature.description,
                dimensions_json=json.dumps(feature.dimensions, ensure_ascii=False),
                tolerance=feature.tolerance,
                confidence=feature.confidence,
                reason=feature.reason,
            )
        )
    add_audit_event(
        db,
        event_type=AuditEventType.DRAWING_ANALYZED.value,
        message=f"Analyzed drawing document: {document.filename}",
        actor=actor,
        case_id=document.case_id,
        document_id=document.id,
        payload={
            "drawing_id": drawing.id,
            "drawing_number": analysis.drawing_number,
            "feature_count": len(analysis.features),
            "risk_count": len(analysis.risks),
            "question_count": len(analysis.questions),
        },
    )
    db.commit()
    db.refresh(drawing)
    return drawing


def add_customer_question_draft_audit(
    db: Session,
    drawing: Drawing,
    *,
    question_count: int,
    actor: str = "ai",
) -> None:
    add_audit_event(
        db,
        event_type=AuditEventType.CUSTOMER_QUESTION_DRAFTED.value,
        message=f"Drafted customer questions for drawing: {drawing.title}",
        actor=actor,
        case_id=drawing.case_id,
        document_id=drawing.document_id,
        payload={
            "drawing_id": drawing.id,
            "question_count": question_count,
            "approval_required": True,
        },
    )
    db.commit()


def create_invoice_from_extraction(
    db: Session,
    document: Document,
    extraction: InvoiceExtractionResult,
    actor: str = "ai",
) -> tuple[Invoice, InvoiceCheckResult]:
    supplier, requisites_diff = _get_or_create_supplier(db, extraction)
    checks = _check_invoice_extraction(db, document, supplier, extraction, requisites_diff)
    duplicate_status = _duplicate_status(checks)
    invoice = Invoice(
        case_id=document.case_id,
        supplier_id=supplier.id,
        document_id=document.id,
        invoice_number=extraction.invoice_number,
        invoice_date=extraction.invoice_date,
        currency=extraction.currency,
        subtotal_amount=extraction.subtotal_amount,
        tax_amount=extraction.tax_amount,
        total_amount=extraction.total_amount,
        arithmetic_ok="ok" if checks.arithmetic_ok else "failed",
        duplicate_status=duplicate_status,
        extraction_json=extraction.model_dump_json(),
    )
    db.add(invoice)
    db.flush()
    for index, line in enumerate(extraction.lines, start=1):
        invoice_line = InvoiceLine(
            invoice_id=invoice.id,
            line_no=line.line_no or index,
            description=line.description,
            sku=line.sku,
            quantity=line.quantity,
            unit=line.unit,
            unit_price=line.unit_price,
            line_total=line.line_total,
            tax_rate=line.tax_rate,
            confidence=line.confidence,
            reason=line.reason,
        )
        db.add(invoice_line)
        db.flush()
        if line.unit_price is not None:
            db.add(
                PriceHistoryEntry(
                    supplier_id=supplier.id,
                    invoice_id=invoice.id,
                    invoice_line_id=invoice_line.id,
                    item_key=_line_item_key(line.description, line.sku),
                    unit_price=line.unit_price,
                    currency=extraction.currency,
                    metadata_json=json.dumps(
                        {"invoice_number": extraction.invoice_number},
                        ensure_ascii=False,
                    ),
                )
            )
    document.document_type = "invoice"
    add_audit_event(
        db,
        event_type=AuditEventType.INVOICE_EXTRACTED.value,
        message=f"Extracted invoice: {extraction.invoice_number or document.filename}",
        actor=actor,
        case_id=document.case_id,
        document_id=document.id,
        payload={
            "invoice_id": invoice.id,
            "supplier_id": supplier.id,
            "arithmetic_ok": checks.arithmetic_ok,
            "duplicate_status": duplicate_status,
            "warnings": checks.warnings,
        },
    )
    if requisites_diff:
        add_audit_event(
            db,
            event_type=AuditEventType.SUPPLIER_REQUISITES_DIFF_DETECTED.value,
            message=f"Supplier requisites changed: {supplier.name}",
            actor=actor,
            case_id=document.case_id,
            document_id=document.id,
            payload={"supplier_id": supplier.id, "diff": requisites_diff},
        )
    db.commit()
    db.refresh(invoice)
    return invoice, checks


def build_invoice_anomaly_card(
    db: Session,
    invoice: Invoice,
    checks: InvoiceCheckResult | None = None,
    actor: str = "system",
) -> InvoiceAnomalyCard:
    signals: list[str] = []
    severity = "low"
    if invoice.arithmetic_ok == "failed":
        signals.append("Invoice arithmetic check failed")
        severity = "high"
    if invoice.duplicate_status != "unique":
        signals.append(f"Duplicate status: {invoice.duplicate_status}")
        severity = "high"
    if checks:
        signals.extend(checks.warnings)
        signals.extend([f"Supplier requisites diff: {item}" for item in checks.supplier_requisites_diff])
        if checks.supplier_requisites_diff and severity == "low":
            severity = "medium"
    for line in invoice.lines:
        if line.confidence < 0.65:
            signals.append(f"Low confidence line {line.line_no}: {line.description}")
            if severity == "low":
                severity = "medium"
        previous_price = _previous_unit_price(db, line)
        if previous_price and line.unit_price:
            delta = (line.unit_price - previous_price) / previous_price
            if delta > 0.2:
                signals.append(
                    f"Price increased by {round(delta * 100, 1)}% for {line.sku or line.description}"
                )
                if severity == "low":
                    severity = "medium"
    if not signals:
        signals.append("No deterministic anomalies detected")
    card = InvoiceAnomalyCard(
        severity=severity,
        title=f"Invoice {invoice.invoice_number or invoice.id} anomaly card",
        signals=signals,
        recommended_action=(
            "Hold for manual approval before payment"
            if severity in {"high", "medium"}
            else "Proceed with normal review"
        ),
        approval_required=True,
    )
    add_audit_event(
        db,
        event_type=AuditEventType.INVOICE_ANOMALY_CREATED.value,
        message=f"Created anomaly card for invoice: {invoice.invoice_number or invoice.id}",
        actor=actor,
        case_id=invoice.case_id,
        document_id=invoice.document_id,
        payload=card.model_dump(),
    )
    db.commit()
    return card


def add_invoice_export_audit(
    db: Session,
    invoice: Invoice,
    *,
    artifact_id: str | None = None,
    actor: str = "system",
) -> None:
    add_audit_event(
        db,
        event_type=AuditEventType.INVOICE_EXCEL_EXPORTED.value,
        message=f"Exported invoice to Excel: {invoice.invoice_number or invoice.id}",
        actor=actor,
        case_id=invoice.case_id,
        document_id=invoice.document_id,
        payload={"invoice_id": invoice.id, "artifact_id": artifact_id},
    )
    db.commit()


def add_onec_export_audit(db: Session, invoice: Invoice, actor: str = "system") -> None:
    add_audit_event(
        db,
        event_type=AuditEventType.ONEC_EXPORT_PREPARED.value,
        message=f"Prepared 1C export payload: {invoice.invoice_number or invoice.id}",
        actor=actor,
        case_id=invoice.case_id,
        document_id=invoice.document_id,
        payload={"invoice_id": invoice.id, "approval_required": True},
    )
    db.commit()


def create_email_thread(
    db: Session,
    payload: EmailThreadCreate,
    actor: str = "system",
) -> EmailThread:
    thread = EmailThread(
        case_id=payload.case_id,
        subject=payload.subject,
        external_thread_id=payload.external_thread_id,
    )
    db.add(thread)
    db.flush()
    if payload.message:
        message = EmailMessage(
            thread_id=thread.id,
            direction="inbound",
            external_message_id=payload.message.external_message_id,
            sender=payload.message.sender,
            recipients_json=json.dumps(payload.message.recipients, ensure_ascii=False),
            subject=payload.message.subject,
            body_text=payload.message.body_text,
            received_at=payload.message.received_at or now_utc(),
        )
        db.add(message)
        thread.last_message_at = message.received_at
        add_audit_event(
            db,
            event_type=AuditEventType.EMAIL_MESSAGE_INGESTED.value,
            message=f"Ingested email message: {message.subject}",
            actor=actor,
            case_id=thread.case_id,
            payload={"thread_id": thread.id, "message_id": message.id},
        )
    add_audit_event(
        db,
        event_type=AuditEventType.EMAIL_THREAD_CREATED.value,
        message=f"Created email thread: {thread.subject}",
        actor=actor,
        case_id=thread.case_id,
        payload={"thread_id": thread.id},
    )
    db.commit()
    db.refresh(thread)
    return thread


def create_draft_email(
    db: Session,
    payload: DraftEmailCreate,
    actor: str = "ai",
) -> DraftEmail:
    risk = _email_risk(payload)
    draft = DraftEmail(
        thread_id=payload.thread_id,
        case_id=payload.case_id,
        to_json=json.dumps(payload.to, ensure_ascii=False),
        cc_json=json.dumps(payload.cc, ensure_ascii=False),
        subject=payload.subject,
        body_text=payload.body_text,
        status="needs_approval",
        risk_json=json.dumps(risk, ensure_ascii=False),
        approval_required="true",
    )
    db.add(draft)
    add_audit_event(
        db,
        event_type=AuditEventType.EMAIL_DRAFT_CREATED.value,
        message=f"Created draft email: {draft.subject}",
        actor=actor,
        case_id=payload.case_id,
        payload={"draft_id": draft.id, "risk": risk, "approval_required": True},
    )
    db.commit()
    db.refresh(draft)
    return draft


def block_email_send_for_approval(
    db: Session,
    draft: DraftEmail,
    actor: str = "system",
) -> None:
    draft.status = "blocked_for_approval"
    add_audit_event(
        db,
        event_type=AuditEventType.EMAIL_SEND_BLOCKED_FOR_APPROVAL.value,
        message=f"Blocked email send pending approval: {draft.subject}",
        actor=actor,
        case_id=draft.case_id,
        payload={"draft_id": draft.id, "approval_required": True},
    )
    db.commit()
    db.refresh(draft)


def add_imap_placeholder_audit(
    db: Session,
    case_id: str | None = None,
    actor: str = "system",
) -> None:
    add_audit_event(
        db,
        event_type=AuditEventType.EMAIL_MESSAGE_INGESTED.value,
        message="IMAP polling placeholder checked; no external connection performed",
        actor=actor,
        case_id=case_id,
        payload={"adapter": "imap_placeholder", "imported_count": 0},
    )
    db.commit()


def add_agent_scenario_started_audit(
    db: Session,
    *,
    scenario: str,
    case_id: str | None = None,
    requested_tools: list[str] | None = None,
    max_steps: int | None = None,
    actor: str = "agent",
) -> None:
    add_audit_event(
        db,
        event_type=AuditEventType.AGENT_SCENARIO_STARTED.value,
        message=f"Agent scenario started: {scenario}",
        actor=actor,
        case_id=case_id,
        payload={
            "scenario": scenario,
            "requested_tools": requested_tools or [],
            "max_steps": max_steps,
        },
    )
    db.commit()


def add_agent_scenario_completed_audit(
    db: Session,
    *,
    scenario: str,
    case_id: str | None = None,
    status: str,
    action_count: int,
    approval_gate_count: int,
    warnings: list[str] | None = None,
    actor: str = "agent",
) -> None:
    add_audit_event(
        db,
        event_type=AuditEventType.AGENT_SCENARIO_COMPLETED.value,
        message=f"Agent scenario completed: {scenario}",
        actor=actor,
        case_id=case_id,
        payload={
            "scenario": scenario,
            "status": status,
            "action_count": action_count,
            "approval_gate_count": approval_gate_count,
            "warnings": warnings or [],
        },
    )
    db.commit()


def record_agent_action(
    db: Session,
    *,
    scenario: str,
    tool_name: str,
    step_no: int,
    case_id: str | None = None,
    status: str = "recorded",
    payload: dict | None = None,
    result: dict | None = None,
    actor: str = "agent",
) -> AgentAction:
    action = AgentAction(
        case_id=case_id,
        scenario=scenario,
        tool_name=tool_name,
        status=status,
        step_no=step_no,
        payload_json=json.dumps(payload or {}, ensure_ascii=False),
        result_json=json.dumps(result or {}, ensure_ascii=False),
    )
    db.add(action)
    db.flush()
    add_audit_event(
        db,
        event_type=AuditEventType.AGENT_ACTION_RECORDED.value,
        message=f"Agent action {tool_name}: {status}",
        actor=actor,
        case_id=case_id,
        payload={
            "action_id": action.id,
            "scenario": scenario,
            "tool_name": tool_name,
            "step_no": step_no,
            "status": status,
        },
    )
    db.commit()
    db.refresh(action)
    return action


def create_approval_gate(
    db: Session,
    *,
    gate_type: str,
    reason: str,
    case_id: str | None = None,
    action_id: str | None = None,
    payload: dict | None = None,
    actor: str = "agent",
) -> ApprovalGate:
    gate = ApprovalGate(
        case_id=case_id,
        action_id=action_id,
        gate_type=gate_type,
        reason=reason,
        payload_json=json.dumps(payload or {}, ensure_ascii=False),
    )
    db.add(gate)
    db.flush()
    add_audit_event(
        db,
        event_type=AuditEventType.APPROVAL_GATE_CREATED.value,
        message=f"Approval gate created: {gate_type}",
        actor=actor,
        case_id=case_id,
        payload={"gate_id": gate.id, "gate_type": gate_type, "reason": reason},
    )
    db.commit()
    db.refresh(gate)
    return gate


def create_task_job(
    db: Session,
    *,
    task_type: str,
    case_id: str | None = None,
    document_id: str | None = None,
    agent_action_id: str | None = None,
    approval_gate_id: str | None = None,
    payload: dict | None = None,
    max_attempts: int = 3,
    actor: str = "system",
) -> TaskJob:
    job = TaskJob(
        task_type=task_type,
        case_id=case_id,
        document_id=document_id,
        agent_action_id=agent_action_id,
        approval_gate_id=approval_gate_id,
        payload_json=json.dumps(payload or {}, ensure_ascii=False),
        max_attempts=max_attempts,
    )
    db.add(job)
    if document_id and task_type == "document.process":
        document = db.get(Document, document_id)
        if document:
            document.status = DocumentStatus.PROCESSING.value
    if agent_action_id:
        action = db.get(AgentAction, agent_action_id)
        if action:
            action.status = "queued"
            action.result_json = json.dumps({"task_id": job.id}, ensure_ascii=False)
    db.flush()
    add_audit_event(
        db,
        event_type=AuditEventType.TASK_JOB_CREATED.value,
        message=f"Task job created: {task_type}",
        actor=actor,
        case_id=case_id,
        document_id=document_id,
        payload={
            "task_id": job.id,
            "task_type": task_type,
            "agent_action_id": agent_action_id,
            "approval_gate_id": approval_gate_id,
        },
    )
    db.commit()
    db.refresh(job)
    return job


def next_runnable_task_job(db: Session) -> TaskJob | None:
    now = now_utc()
    return db.scalar(
        select(TaskJob)
        .where(
            TaskJob.status.in_([TaskJobStatus.PENDING.value, TaskJobStatus.RETRY_SCHEDULED.value]),
            or_(TaskJob.not_before.is_(None), TaskJob.not_before <= now),
        )
        .order_by(TaskJob.created_at.asc())
    )


def start_task_job(db: Session, job: TaskJob, actor: str = "system") -> TaskJob:
    job.status = TaskJobStatus.RUNNING.value
    job.attempt_count += 1
    job.started_at = now_utc()
    job.error_message = None
    add_audit_event(
        db,
        event_type=AuditEventType.TASK_JOB_STARTED.value,
        message=f"Task job started: {job.task_type}",
        actor=actor,
        case_id=job.case_id,
        document_id=job.document_id,
        payload={"task_id": job.id, "attempt_count": job.attempt_count},
    )
    db.commit()
    db.refresh(job)
    return job


def complete_task_job(
    db: Session,
    job: TaskJob,
    *,
    result: dict | None = None,
    actor: str = "system",
) -> TaskJob:
    job.status = TaskJobStatus.COMPLETED.value
    job.result_json = json.dumps(result or {}, ensure_ascii=False)
    job.finished_at = now_utc()
    if job.agent_action_id:
        action = db.get(AgentAction, job.agent_action_id)
        if action:
            action.status = "executed"
            action.result_json = json.dumps(result or {}, ensure_ascii=False)
    if job.approval_gate_id:
        gate = db.get(ApprovalGate, job.approval_gate_id)
        if gate:
            gate.status = "executed"
            add_audit_event(
                db,
                event_type=AuditEventType.APPROVAL_GATE_EXECUTED.value,
                message=f"Approval gate executed: {gate.gate_type}",
                actor=actor,
                case_id=gate.case_id,
                payload={"gate_id": gate.id, "task_id": job.id},
            )
    add_audit_event(
        db,
        event_type=AuditEventType.TASK_JOB_COMPLETED.value,
        message=f"Task job completed: {job.task_type}",
        actor=actor,
        case_id=job.case_id,
        document_id=job.document_id,
        payload={"task_id": job.id, "result": result or {}},
    )
    db.commit()
    db.refresh(job)
    return job


def fail_task_job(
    db: Session,
    job: TaskJob,
    *,
    error_message: str,
    actor: str = "system",
) -> TaskJob:
    job.error_message = error_message
    if job.attempt_count >= job.max_attempts:
        job.status = TaskJobStatus.DEAD_LETTER.value
        job.finished_at = now_utc()
        event_type = AuditEventType.TASK_JOB_DEAD_LETTERED.value
        message = f"Task job dead-lettered: {job.task_type}"
        if job.agent_action_id:
            action = db.get(AgentAction, job.agent_action_id)
            if action:
                action.status = "failed"
                action.result_json = json.dumps({"error": error_message}, ensure_ascii=False)
    else:
        job.status = TaskJobStatus.RETRY_SCHEDULED.value
        job.not_before = now_utc() + timedelta(seconds=min(60, 2 ** max(job.attempt_count, 1)))
        event_type = AuditEventType.TASK_JOB_RETRY_SCHEDULED.value
        message = f"Task job retry scheduled: {job.task_type}"
    add_audit_event(
        db,
        event_type=event_type,
        message=message,
        actor=actor,
        case_id=job.case_id,
        document_id=job.document_id,
        payload={"task_id": job.id, "error_message": error_message, "attempt_count": job.attempt_count},
    )
    db.commit()
    db.refresh(job)
    return job


def approve_approval_gate(
    db: Session,
    gate: ApprovalGate,
    *,
    actor: str,
    reason: str,
) -> ApprovalGate:
    gate.status = "approved"
    gate.decided_at = now_utc()
    payload = json.loads(gate.payload_json or "{}")
    payload.update({"decision_actor": actor, "decision_reason": reason})
    gate.payload_json = json.dumps(payload, ensure_ascii=False)
    add_audit_event(
        db,
        event_type=AuditEventType.APPROVAL_GATE_APPROVED.value,
        message=f"Approval gate approved: {gate.gate_type}",
        actor=actor,
        case_id=gate.case_id,
        payload={"gate_id": gate.id, "reason": reason},
    )
    db.commit()
    db.refresh(gate)
    return gate


def reject_approval_gate(
    db: Session,
    gate: ApprovalGate,
    *,
    actor: str,
    reason: str,
) -> ApprovalGate:
    gate.status = "rejected"
    gate.decided_at = now_utc()
    payload = json.loads(gate.payload_json or "{}")
    payload.update({"decision_actor": actor, "decision_reason": reason})
    gate.payload_json = json.dumps(payload, ensure_ascii=False)
    if gate.action_id:
        action = db.get(AgentAction, gate.action_id)
        if action:
            action.status = "rejected"
    add_audit_event(
        db,
        event_type=AuditEventType.APPROVAL_GATE_REJECTED.value,
        message=f"Approval gate rejected: {gate.gate_type}",
        actor=actor,
        case_id=gate.case_id,
        payload={"gate_id": gate.id, "reason": reason},
    )
    db.commit()
    db.refresh(gate)
    return gate


def add_signed_file_url_audit(
    db: Session,
    *,
    filename: str,
    expires_at: int,
    case_id: str | None = None,
    document_id: str | None = None,
    artifact_id: str | None = None,
    actor: str = "system",
) -> None:
    add_audit_event(
        db,
        event_type=AuditEventType.SIGNED_FILE_URL_CREATED.value,
        message=f"Created signed file URL: {filename}",
        actor=actor,
        case_id=case_id,
        document_id=document_id,
        payload={"artifact_id": artifact_id, "expires_at": expires_at},
    )
    db.commit()


def _email_risk(payload: DraftEmailCreate) -> dict:
    signals: list[str] = []
    if not payload.to:
        signals.append("missing_recipient")
    body_lower = payload.body_text.lower()
    risky_terms = ["оплат", "payment", "bank", "счет", "счёт", "реквизит"]
    if any(term in body_lower for term in risky_terms):
        signals.append("contains_financial_or_requisites_terms")
    if len(payload.body_text) > 5000:
        signals.append("long_email_body")
    severity = "medium" if signals else "low"
    return {
        "severity": severity,
        "signals": signals,
        "approval_required": True,
    }


def _get_or_create_supplier(db: Session, extraction: InvoiceExtractionResult) -> tuple[Supplier, list[str]]:
    supplier_data = extraction.supplier
    supplier: Supplier | None = None
    diff: list[str] = []
    if supplier_data.inn:
        supplier = db.scalar(select(Supplier).where(Supplier.inn == supplier_data.inn))
    if supplier is None:
        supplier = db.scalar(select(Supplier).where(Supplier.name == supplier_data.name))
    if supplier is None:
        supplier = Supplier(
            name=supplier_data.name,
            inn=supplier_data.inn,
            kpp=supplier_data.kpp,
            bank_details_json=json.dumps(supplier_data.bank_details, ensure_ascii=False),
        )
        db.add(supplier)
        db.flush()
    else:
        previous = {
            "name": supplier.name,
            "inn": supplier.inn,
            "kpp": supplier.kpp,
            "bank_details": json.loads(supplier.bank_details_json or "{}"),
        }
        incoming_bank_details = supplier_data.bank_details
        if supplier_data.inn and previous["inn"] and supplier_data.inn != previous["inn"]:
            diff.append(f"inn: {previous['inn']} -> {supplier_data.inn}")
        if supplier_data.kpp and previous["kpp"] and supplier_data.kpp != previous["kpp"]:
            diff.append(f"kpp: {previous['kpp']} -> {supplier_data.kpp}")
        if incoming_bank_details and incoming_bank_details != previous["bank_details"]:
            diff.append("bank_details changed")
        supplier.name = supplier_data.name or supplier.name
        supplier.inn = supplier_data.inn or supplier.inn
        supplier.kpp = supplier_data.kpp or supplier.kpp
        supplier.bank_details_json = json.dumps(supplier_data.bank_details, ensure_ascii=False)
    return supplier, diff


def _check_invoice_extraction(
    db: Session,
    document: Document,
    supplier: Supplier,
    extraction: InvoiceExtractionResult,
    supplier_requisites_diff: list[str],
) -> InvoiceCheckResult:
    warnings: list[str] = []
    arithmetic_ok = True
    line_total_sum = 0.0
    has_line_totals = False
    for line in extraction.lines:
        if line.quantity is not None and line.unit_price is not None and line.line_total is not None:
            expected = round(line.quantity * line.unit_price, 2)
            actual = round(line.line_total, 2)
            if abs(expected - actual) > 0.02:
                arithmetic_ok = False
                warnings.append(
                    f"Line {line.line_no or '?'} total mismatch: expected {expected}, got {actual}"
                )
        if line.line_total is not None:
            has_line_totals = True
            line_total_sum += line.line_total
    if has_line_totals and extraction.subtotal_amount is not None:
        if abs(round(line_total_sum, 2) - round(extraction.subtotal_amount, 2)) > 0.02:
            arithmetic_ok = False
            warnings.append(
                f"Subtotal mismatch: lines sum {round(line_total_sum, 2)}, "
                f"subtotal {round(extraction.subtotal_amount, 2)}"
            )
    if extraction.subtotal_amount is not None and extraction.tax_amount is not None:
        expected_total = round(extraction.subtotal_amount + extraction.tax_amount, 2)
        if extraction.total_amount is not None and abs(expected_total - round(extraction.total_amount, 2)) > 0.02:
            arithmetic_ok = False
            warnings.append(
                f"Total mismatch: subtotal+tax {expected_total}, total {round(extraction.total_amount, 2)}"
            )
    duplicate_by_hash = db.scalar(
        select(Invoice)
        .join(Document, Invoice.document_id == Document.id)
        .where(Document.sha256 == document.sha256)
    ) is not None
    duplicate_by_supplier_number = False
    if extraction.invoice_number:
        duplicate_by_supplier_number = (
            db.scalar(
                select(Invoice).where(
                    Invoice.supplier_id == supplier.id,
                    Invoice.invoice_number == extraction.invoice_number,
                )
            )
            is not None
        )
    if duplicate_by_hash:
        warnings.append("Duplicate document hash detected")
    if duplicate_by_supplier_number:
        warnings.append("Duplicate supplier invoice number detected")
    if supplier_requisites_diff:
        warnings.append("Supplier requisites changed")
    return InvoiceCheckResult(
        arithmetic_ok=arithmetic_ok,
        duplicate_by_hash=duplicate_by_hash,
        duplicate_by_supplier_number=duplicate_by_supplier_number,
        supplier_requisites_diff=supplier_requisites_diff,
        warnings=warnings,
    )


def _duplicate_status(checks: InvoiceCheckResult) -> str:
    if checks.duplicate_by_hash and checks.duplicate_by_supplier_number:
        return "duplicate_hash_and_number"
    if checks.duplicate_by_hash:
        return "duplicate_hash"
    if checks.duplicate_by_supplier_number:
        return "duplicate_supplier_number"
    return "unique"


def _line_item_key(description: str, sku: str | None) -> str:
    if sku:
        return sku.strip().lower()
    return " ".join(description.lower().split())[:255]


def _previous_unit_price(db: Session, line: InvoiceLine) -> float | None:
    item_key = _line_item_key(line.description, line.sku)
    entry = db.scalar(
        select(PriceHistoryEntry)
        .where(
            PriceHistoryEntry.item_key == item_key,
            PriceHistoryEntry.invoice_line_id != line.id,
        )
        .order_by(PriceHistoryEntry.observed_at.desc())
    )
    return entry.unit_price if entry else None


def add_audit_event(
    db: Session,
    *,
    event_type: str,
    message: str,
    actor: str = "system",
    case_id: str | None = None,
    document_id: str | None = None,
    payload: dict | None = None,
) -> AuditEvent:
    event = AuditEvent(
        case_id=case_id,
        document_id=document_id,
        event_type=event_type,
        actor=actor,
        message=message,
        payload_json=json.dumps(payload, ensure_ascii=False) if payload else None,
    )
    db.add(event)
    return event


def list_audit_events(db: Session, case_id: str) -> list[AuditEvent]:
    return list(
        db.scalars(
            select(AuditEvent)
            .where(AuditEvent.case_id == case_id)
            .order_by(AuditEvent.created_at.desc())
        )
    )
