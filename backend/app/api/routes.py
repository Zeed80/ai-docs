from __future__ import annotations

import json
import base64
from io import BytesIO
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from fastapi.responses import Response
from sqlalchemy.orm import Session

from backend.app.ai import AIRouter
from backend.app.ai.schemas import AIRequest, AITask, ChatMessage
from backend.app.auth import get_current_user, permissions_for_roles, require_permission
from backend.app.config import get_settings
from backend.app.db.session import get_db
from backend.app.dependencies import get_ai_router, get_storage
from backend.app.domain.models import AuditEventType, DocumentStatus
from backend.app.domain.schemas import (
    AIActionRead,
    AgentActionRead,
    AgentScenarioRunRead,
    AgentScenarioRunRequest,
    AgentToolSpecRead,
    ApprovalGateDecisionRead,
    ApprovalGateDecisionRequest,
    ApprovalGateRead,
    AuthUserRead,
    AuditEventRead,
    CaseCreate,
    CaseRead,
    CaseUpdate,
    DocumentClassifyRequest,
    DocumentExtractRequest,
    DocumentArtifactRead,
    DocumentExtractionResult,
    DocumentProcessingJobRead,
    DocumentRead,
    DraftEmailCreate,
    DraftEmailRead,
    EmailMessageRead,
    EmailSendAttemptRead,
    EmailThreadCreate,
    EmailThreadRead,
    ImapPollRead,
    CustomerQuestionDraft,
    CustomerQuestionDraftRead,
    DrawingAnalysisRead,
    DrawingAnalysisResult,
    DrawingFeatureRead,
    DrawingRead,
    InvoiceExtractionRead,
    InvoiceExtractionResult,
    InvoiceExportRead,
    InvoiceLineRead,
    InvoiceAnomalyCard,
    InvoiceRead,
    OneCExportRead,
    SignedFileUrlRead,
    SupplierRead,
    TaskJobRead,
)
from backend.app.domain.services import (
    add_signed_file_url_audit,
    complete_processing_job,
    create_case,
    create_document,
    create_document_artifacts,
    create_drawing_analysis,
    create_invoice_from_extraction,
    create_processing_job,
    get_case,
    get_document,
    get_document_artifact,
    get_drawing,
    list_audit_events,
    list_case_documents,
    list_cases,
    update_case,
    update_document_ai_result,
    add_customer_question_draft_audit,
    add_imap_placeholder_audit,
    add_invoice_export_audit,
    add_onec_export_audit,
    block_email_send_for_approval,
    build_invoice_anomaly_card,
    approve_approval_gate,
    create_draft_email,
    create_email_thread,
    create_task_job,
    get_draft_email,
    get_email_thread,
    get_invoice,
    get_approval_gate,
    get_task_job,
    list_approval_gates,
    list_task_jobs,
    next_runnable_task_job,
    reject_approval_gate,
)
from backend.app.security import create_signed_file_token, verify_signed_file_token
from backend.app.domain.storage import LocalFileStorage
from backend.app.tasks.document_processing import process_document
from backend.app.tasks.execution import run_task_job
from backend.app.tasks.aiagent import load_tool_registry, run_aiagent_scenario


router = APIRouter()


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/api/auth/me", response_model=AuthUserRead)
async def get_me(user: AuthUserRead = Depends(get_current_user)) -> AuthUserRead:
    return user


@router.get("/api/auth/permissions")
async def get_my_permissions(user: AuthUserRead = Depends(get_current_user)) -> dict[str, list[str]]:
    return {"roles": user.roles, "permissions": permissions_for_roles(user.roles)}


@router.post("/api/cases", response_model=CaseRead, status_code=status.HTTP_201_CREATED)
async def create_manufacturing_case(payload: CaseCreate, db: Session = Depends(get_db)) -> CaseRead:
    case = create_case(db, payload)
    return _case_read(case, document_count=0)


@router.get("/api/cases", response_model=list[CaseRead])
async def list_manufacturing_cases(db: Session = Depends(get_db)) -> list[CaseRead]:
    return [_case_read(case, document_count=count) for case, count in list_cases(db)]


@router.get("/api/cases/{case_id}", response_model=CaseRead)
async def get_manufacturing_case(case_id: str, db: Session = Depends(get_db)) -> CaseRead:
    case = get_case(db, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="Case not found")
    return _case_read(case, document_count=len(case.documents))


@router.patch("/api/cases/{case_id}", response_model=CaseRead)
async def update_manufacturing_case(
    case_id: str, payload: CaseUpdate, db: Session = Depends(get_db)
) -> CaseRead:
    case = get_case(db, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="Case not found")
    updated = update_case(db, case, payload)
    return _case_read(updated, document_count=len(updated.documents))


@router.post(
    "/api/cases/{case_id}/documents",
    response_model=DocumentRead,
    status_code=status.HTTP_201_CREATED,
)
async def upload_document(
    case_id: str,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    storage: LocalFileStorage = Depends(get_storage),
) -> DocumentRead:
    case = get_case(db, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="Case not found")
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")
    filename = file.filename or "document.bin"
    extension = Path(filename).suffix.lower()
    settings = get_settings()
    if extension not in settings.upload_extension_allowlist:
        storage_path, sha256, size_bytes = storage.save_quarantine(case_id, filename, content)
        document_status = DocumentStatus.SUSPICIOUS
        quarantine_reason = f"Extension {extension or '[none]'} is not allowlisted"
    else:
        storage_path, sha256, size_bytes = storage.save(case_id, filename, content)
        document_status = DocumentStatus.UPLOADED
        quarantine_reason = None
    document = create_document(
        db,
        case,
        filename=filename,
        content_type=file.content_type,
        storage_path=storage_path,
        sha256=sha256,
        size_bytes=size_bytes,
        status=document_status,
        quarantine_reason=quarantine_reason,
    )
    return _document_read(document)


@router.get("/api/cases/{case_id}/documents", response_model=list[DocumentRead])
async def list_documents(case_id: str, db: Session = Depends(get_db)) -> list[DocumentRead]:
    if get_case(db, case_id) is None:
        raise HTTPException(status_code=404, detail="Case not found")
    return [_document_read(document) for document in list_case_documents(db, case_id)]


@router.get("/api/documents/{document_id}", response_model=DocumentRead)
async def get_document_card(document_id: str, db: Session = Depends(get_db)) -> DocumentRead:
    document = get_document(db, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="Document not found")
    return _document_read(document)


@router.post("/api/documents/{document_id}/download-url", response_model=SignedFileUrlRead)
async def create_document_download_url(
    document_id: str,
    db: Session = Depends(get_db),
    _user: AuthUserRead = Depends(require_permission("document:read")),
) -> SignedFileUrlRead:
    document = get_document(db, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="Document not found")
    settings = get_settings()
    token, expires_at = create_signed_file_token(
        storage_path=document.storage_path,
        filename=document.filename,
        content_type=document.content_type,
        secret=settings.file_url_signing_secret,
        ttl_seconds=settings.signed_file_url_ttl_seconds,
        document_id=document.id,
    )
    add_signed_file_url_audit(
        db,
        filename=document.filename,
        expires_at=expires_at,
        case_id=document.case_id,
        document_id=document.id,
    )
    return SignedFileUrlRead(
        url=f"/api/files/signed/{token}",
        expires_at=expires_at,
        filename=document.filename,
        content_type=document.content_type,
    )


@router.post("/api/artifacts/{artifact_id}/download-url", response_model=SignedFileUrlRead)
async def create_artifact_download_url(
    artifact_id: str,
    db: Session = Depends(get_db),
    _user: AuthUserRead = Depends(require_permission("document:read")),
) -> SignedFileUrlRead:
    artifact = get_document_artifact(db, artifact_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail="Artifact not found")
    settings = get_settings()
    filename = Path(artifact.storage_path).name
    token, expires_at = create_signed_file_token(
        storage_path=artifact.storage_path,
        filename=filename,
        content_type=artifact.content_type,
        secret=settings.file_url_signing_secret,
        ttl_seconds=settings.signed_file_url_ttl_seconds,
        document_id=artifact.document_id,
        artifact_id=artifact.id,
    )
    add_signed_file_url_audit(
        db,
        filename=filename,
        expires_at=expires_at,
        case_id=artifact.document.case_id,
        document_id=artifact.document_id,
        artifact_id=artifact.id,
    )
    return SignedFileUrlRead(
        url=f"/api/files/signed/{token}",
        expires_at=expires_at,
        filename=filename,
        content_type=artifact.content_type,
    )


@router.get("/api/files/signed/{token}")
async def download_signed_file(token: str) -> Response:
    settings = get_settings()
    try:
        payload = verify_signed_file_token(token, secret=settings.file_url_signing_secret)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    filename = payload.get("filename") or "download.bin"
    content = Path(payload["storage_path"]).read_bytes()
    return Response(
        content=content,
        media_type=payload.get("content_type") or "application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/api/cases/{case_id}/audit", response_model=list[AuditEventRead])
async def get_case_audit(case_id: str, db: Session = Depends(get_db)) -> list[AuditEventRead]:
    if get_case(db, case_id) is None:
        raise HTTPException(status_code=404, detail="Case not found")
    return [AuditEventRead.model_validate(event) for event in list_audit_events(db, case_id)]


@router.post("/api/documents/{document_id}/classify", response_model=AIActionRead)
async def classify_document(
    document_id: str,
    payload: DocumentClassifyRequest,
    db: Session = Depends(get_db),
    ai_router: AIRouter = Depends(get_ai_router),
) -> AIActionRead:
    document = get_document(db, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="Document not found")
    prompt = payload.prompt or _classification_prompt(document.filename, document.content_type)
    ai_response = await ai_router.run(
        AIRequest(
            task=AITask.CLASSIFICATION,
            messages=[ChatMessage(role="user", content=prompt)],
            confidential=True,
        )
    )
    document_type = _guess_document_type(ai_response.text or "", document.filename)
    updated = update_document_ai_result(
        db,
        document,
        event_type=AuditEventType.DOCUMENT_CLASSIFIED.value,
        status=DocumentStatus.CLASSIFIED,
        document_type=document_type,
        ai_text=ai_response.text,
    )
    return AIActionRead(document=_document_read(updated), ai_text=ai_response.text)


@router.post("/api/documents/{document_id}/extract", response_model=AIActionRead)
async def extract_document(
    document_id: str,
    payload: DocumentExtractRequest,
    db: Session = Depends(get_db),
    ai_router: AIRouter = Depends(get_ai_router),
) -> AIActionRead:
    document = get_document(db, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="Document not found")
    source_text = _safe_text_preview(document.storage_path)
    prompt = (
        f"{payload.extraction_goal}\n\n"
        f"Filename: {document.filename}\n"
        f"Known type: {document.document_type or 'unknown'}\n"
        f"Content preview:\n{source_text}"
    )
    ai_response = await ai_router.run(
        AIRequest(
            task=AITask.STRUCTURED_EXTRACTION,
            messages=[ChatMessage(role="user", content=prompt)],
            confidential=True,
        )
    )
    updated = update_document_ai_result(
        db,
        document,
        event_type=AuditEventType.DOCUMENT_EXTRACTED.value,
        status=DocumentStatus.EXTRACTED,
        ai_text=ai_response.text,
    )
    return AIActionRead(document=_document_read(updated), ai_text=ai_response.text)


@router.post("/api/documents/{document_id}/process", response_model=TaskJobRead)
async def process_document_endpoint(
    document_id: str,
    db: Session = Depends(get_db),
) -> TaskJobRead:
    document = get_document(db, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="Document not found")
    if document.status == DocumentStatus.SUSPICIOUS.value:
        raise HTTPException(status_code=409, detail="Suspicious document is quarantined")
    task = create_task_job(
        db,
        task_type="document.process",
        case_id=document.case_id,
        document_id=document.id,
        payload={"document_id": document.id},
    )
    return _task_job_read(task)


@router.get("/api/tasks", response_model=list[TaskJobRead])
async def list_tasks(
    status: str | None = None,
    case_id: str | None = None,
    db: Session = Depends(get_db),
) -> list[TaskJobRead]:
    return [_task_job_read(job) for job in list_task_jobs(db, status=status, case_id=case_id)]


@router.post("/api/tasks/{task_id}/run", response_model=TaskJobRead)
async def run_task_endpoint(
    task_id: str,
    db: Session = Depends(get_db),
    ai_router: AIRouter = Depends(get_ai_router),
    storage: LocalFileStorage = Depends(get_storage),
) -> TaskJobRead:
    job = get_task_job(db, task_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Task not found")
    if job.status not in {"pending", "retry_scheduled"}:
        raise HTTPException(status_code=409, detail=f"Task cannot be run from status {job.status}")
    executed = await run_task_job(db, job, ai_router=ai_router, storage=storage)
    return _task_job_read(executed)


@router.post("/api/tasks/run-next", response_model=TaskJobRead | None)
async def run_next_task_endpoint(
    db: Session = Depends(get_db),
    ai_router: AIRouter = Depends(get_ai_router),
    storage: LocalFileStorage = Depends(get_storage),
) -> TaskJobRead | None:
    job = next_runnable_task_job(db)
    if job is None:
        return None
    executed = await run_task_job(db, job, ai_router=ai_router, storage=storage)
    return _task_job_read(executed)


@router.post("/api/documents/{document_id}/drawing-analysis", response_model=DrawingAnalysisRead)
async def analyze_drawing_document(
    document_id: str,
    db: Session = Depends(get_db),
    ai_router: AIRouter = Depends(get_ai_router),
) -> DrawingAnalysisRead:
    document = get_document(db, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="Document not found")

    prompt = _drawing_analysis_prompt(document)
    ai_response = await ai_router.run(
        AIRequest(
            task=AITask.DRAWING_ANALYSIS,
            messages=[ChatMessage(role="user", content=prompt)],
            images=_document_image_data_uris(document),
            response_schema=DrawingAnalysisResult,
            confidential=True,
            metadata={"document_id": document.id, "local_only": True},
        )
    )
    analysis = _drawing_analysis_data(ai_response.data)
    drawing = create_drawing_analysis(db, document, analysis)
    return DrawingAnalysisRead(drawing=_drawing_read(drawing), analysis=analysis)


@router.post("/api/drawings/{drawing_id}/customer-question-draft", response_model=CustomerQuestionDraftRead)
async def draft_customer_questions(
    drawing_id: str,
    db: Session = Depends(get_db),
    ai_router: AIRouter = Depends(get_ai_router),
) -> CustomerQuestionDraftRead:
    drawing = get_drawing(db, drawing_id)
    if drawing is None:
        raise HTTPException(status_code=404, detail="Drawing not found")

    response = await ai_router.run(
        AIRequest(
            task=AITask.EMAIL_DRAFTING,
            messages=[
                ChatMessage(
                    role="user",
                    content=_customer_question_prompt(drawing),
                )
            ],
            response_schema=CustomerQuestionDraft,
            confidential=True,
            metadata={"drawing_id": drawing.id, "approval_required": True, "local_only": True},
        )
    )
    draft = _customer_question_draft_data(response.data)
    draft.approval_required = True
    add_customer_question_draft_audit(db, drawing, question_count=len(draft.questions))
    return CustomerQuestionDraftRead(drawing=_drawing_read(drawing), draft=draft)


@router.post("/api/documents/{document_id}/invoice-extraction", response_model=InvoiceExtractionRead)
async def extract_invoice_document(
    document_id: str,
    db: Session = Depends(get_db),
    ai_router: AIRouter = Depends(get_ai_router),
) -> InvoiceExtractionRead:
    document = get_document(db, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="Document not found")

    response = await ai_router.run(
        AIRequest(
            task=AITask.STRUCTURED_EXTRACTION,
            messages=[ChatMessage(role="user", content=_invoice_extraction_prompt(document))],
            response_schema=InvoiceExtractionResult,
            confidential=True,
            metadata={"document_id": document.id, "local_only": True},
        )
    )
    extraction = _invoice_extraction_data(response.data)
    invoice, checks = create_invoice_from_extraction(db, document, extraction)
    anomaly_card = build_invoice_anomaly_card(db, invoice, checks)
    return InvoiceExtractionRead(
        invoice=_invoice_read(invoice),
        extraction=extraction,
        checks=checks,
        anomaly_card=anomaly_card,
    )


@router.post("/api/invoices/{invoice_id}/anomaly-card", response_model=InvoiceAnomalyCard)
async def get_invoice_anomaly_card(invoice_id: str, db: Session = Depends(get_db)) -> InvoiceAnomalyCard:
    invoice = get_invoice(db, invoice_id)
    if invoice is None:
        raise HTTPException(status_code=404, detail="Invoice not found")
    return build_invoice_anomaly_card(db, invoice)


@router.post("/api/invoices/{invoice_id}/export.xlsx", response_model=InvoiceExportRead)
async def export_invoice_xlsx(
    invoice_id: str,
    db: Session = Depends(get_db),
    storage: LocalFileStorage = Depends(get_storage),
) -> InvoiceExportRead:
    invoice = get_invoice(db, invoice_id)
    if invoice is None:
        raise HTTPException(status_code=404, detail="Invoice not found")
    if invoice.document_id is None:
        raise HTTPException(status_code=409, detail="Invoice has no source document for artifact storage")
    content = _invoice_xlsx_bytes(invoice)
    storage_path, sha256, size_bytes = storage.save_artifact(
        invoice.document_id,
        f"invoice-{invoice.invoice_number or invoice.id}.xlsx",
        content,
    )
    artifacts = create_document_artifacts(
        db,
        invoice.document,
        [
            {
                "artifact_type": "invoice_excel_export",
                "storage_path": storage_path,
                "content_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "metadata": {"invoice_id": invoice.id, "sha256": sha256, "size_bytes": size_bytes},
            }
        ],
    )
    artifact = artifacts[0]
    add_invoice_export_audit(db, invoice, artifact_id=artifact.id)
    return InvoiceExportRead(invoice=_invoice_read(invoice), artifact=_artifact_read(artifact))


@router.post("/api/invoices/{invoice_id}/1c-export", response_model=OneCExportRead)
async def prepare_onec_export(invoice_id: str, db: Session = Depends(get_db)) -> OneCExportRead:
    invoice = get_invoice(db, invoice_id)
    if invoice is None:
        raise HTTPException(status_code=404, detail="Invoice not found")
    payload = _onec_invoice_payload(invoice)
    add_onec_export_audit(db, invoice)
    return OneCExportRead(invoice=_invoice_read(invoice), payload=payload)


@router.post("/api/email/threads", response_model=EmailThreadRead, status_code=status.HTTP_201_CREATED)
async def create_email_thread_endpoint(
    payload: EmailThreadCreate,
    db: Session = Depends(get_db),
) -> EmailThreadRead:
    if payload.case_id and get_case(db, payload.case_id) is None:
        raise HTTPException(status_code=404, detail="Case not found")
    thread = create_email_thread(db, payload)
    return _email_thread_read(thread)


@router.post("/api/email/imap/poll", response_model=ImapPollRead)
async def poll_imap_placeholder(
    case_id: str | None = None,
    db: Session = Depends(get_db),
) -> ImapPollRead:
    if case_id and get_case(db, case_id) is None:
        raise HTTPException(status_code=404, detail="Case not found")
    add_imap_placeholder_audit(db, case_id=case_id)
    return ImapPollRead(
        message="IMAP adapter placeholder only; no external connection was performed",
    )


@router.post("/api/email/drafts", response_model=DraftEmailRead, status_code=status.HTTP_201_CREATED)
async def create_draft_email_endpoint(
    payload: DraftEmailCreate,
    db: Session = Depends(get_db),
) -> DraftEmailRead:
    if payload.case_id and get_case(db, payload.case_id) is None:
        raise HTTPException(status_code=404, detail="Case not found")
    if payload.thread_id and get_email_thread(db, payload.thread_id) is None:
        raise HTTPException(status_code=404, detail="Email thread not found")
    draft = create_draft_email(db, payload)
    return _draft_email_read(draft)


@router.post("/api/email/drafts/{draft_id}/send", response_model=EmailSendAttemptRead)
async def send_draft_email_blocked(
    draft_id: str,
    db: Session = Depends(get_db),
) -> EmailSendAttemptRead:
    draft = get_draft_email(db, draft_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="Draft email not found")
    block_email_send_for_approval(db, draft)
    return EmailSendAttemptRead(
        draft=_draft_email_read(draft),
        reason="SMTP sending is blocked until explicit human approval is implemented",
    )


@router.get("/api/approvals", response_model=list[ApprovalGateRead])
async def list_approvals(
    status: str | None = "pending",
    case_id: str | None = None,
    db: Session = Depends(get_db),
    _user: AuthUserRead = Depends(require_permission("agent:read")),
) -> list[ApprovalGateRead]:
    return [_approval_gate_read(gate) for gate in list_approval_gates(db, status=status, case_id=case_id)]


@router.post("/api/approvals/{gate_id}/approve", response_model=ApprovalGateDecisionRead)
async def approve_gate_endpoint(
    gate_id: str,
    payload: ApprovalGateDecisionRequest,
    db: Session = Depends(get_db),
    _user: AuthUserRead = Depends(require_permission("agent:run")),
) -> ApprovalGateDecisionRead:
    gate = get_approval_gate(db, gate_id)
    if gate is None:
        raise HTTPException(status_code=404, detail="Approval gate not found")
    if gate.status != "pending":
        raise HTTPException(status_code=409, detail=f"Approval gate cannot be approved from status {gate.status}")
    approved = approve_approval_gate(db, gate, actor=payload.actor, reason=payload.reason)
    task = _task_for_approved_gate(db, approved)
    return ApprovalGateDecisionRead(
        approval_gate=_approval_gate_read(approved),
        task=_task_job_read(task) if task else None,
    )


@router.post("/api/approvals/{gate_id}/reject", response_model=ApprovalGateDecisionRead)
async def reject_gate_endpoint(
    gate_id: str,
    payload: ApprovalGateDecisionRequest,
    db: Session = Depends(get_db),
    _user: AuthUserRead = Depends(require_permission("agent:run")),
) -> ApprovalGateDecisionRead:
    gate = get_approval_gate(db, gate_id)
    if gate is None:
        raise HTTPException(status_code=404, detail="Approval gate not found")
    if gate.status != "pending":
        raise HTTPException(status_code=409, detail=f"Approval gate cannot be rejected from status {gate.status}")
    rejected = reject_approval_gate(db, gate, actor=payload.actor, reason=payload.reason)
    return ApprovalGateDecisionRead(approval_gate=_approval_gate_read(rejected), task=None)


@router.get("/api/agent/tools", response_model=list[AgentToolSpecRead])
async def list_agent_tools(
    _user: AuthUserRead = Depends(require_permission("agent:read")),
) -> list[AgentToolSpecRead]:
    registry = load_tool_registry()
    return [AgentToolSpecRead(**tool) for tool in registry.values()]


@router.post("/api/agent/scenarios/{scenario_name}/run", response_model=AgentScenarioRunRead)
async def run_agent_scenario(
    scenario_name: str,
    payload: AgentScenarioRunRequest,
    db: Session = Depends(get_db),
    _user: AuthUserRead = Depends(require_permission("agent:run")),
) -> AgentScenarioRunRead:
    if payload.case_id and get_case(db, payload.case_id) is None:
        raise HTTPException(status_code=404, detail="Case not found")
    try:
        actions, gates, warnings, max_steps = run_aiagent_scenario(
            db,
            scenario_name=scenario_name,
            payload=payload,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Scenario not found") from None
    return AgentScenarioRunRead(
        scenario=scenario_name,
        status="completed_with_gates" if gates else "completed",
        max_steps=max_steps,
        actions=[_agent_action_read(action) for action in actions],
        approval_gates=[_approval_gate_read(gate) for gate in gates],
        warnings=warnings,
    )


def _case_read(case, document_count: int) -> CaseRead:
    return CaseRead.model_validate(case).model_copy(update={"document_count": document_count})


def _document_read(document) -> DocumentRead:
    return DocumentRead(
        id=document.id,
        case_id=document.case_id,
        filename=document.filename,
        content_type=document.content_type,
        sha256=document.sha256,
        size_bytes=document.size_bytes,
        storage_path=document.storage_path,
        status=document.status,
        document_type=document.document_type,
        extracted_text=document.extracted_text,
        extraction_result=_json_or_none(document.extraction_result_json),
        ai_summary=document.ai_summary,
        artifacts=[_artifact_read(artifact) for artifact in document.artifacts],
        created_at=document.created_at,
        updated_at=document.updated_at,
    )


def _artifact_read(artifact) -> DocumentArtifactRead:
    return DocumentArtifactRead(
        id=artifact.id,
        document_id=artifact.document_id,
        artifact_type=artifact.artifact_type,
        storage_path=artifact.storage_path,
        content_type=artifact.content_type,
        page_number=artifact.page_number,
        width=artifact.width,
        height=artifact.height,
        metadata=_json_or_none(artifact.metadata_json) or {},
        created_at=artifact.created_at,
    )


def _processing_job_read(job) -> DocumentProcessingJobRead:
    result_data = _json_or_none(job.result_json)
    result = DocumentExtractionResult.model_validate(result_data) if result_data else None
    return DocumentProcessingJobRead(
        id=job.id,
        document_id=job.document_id,
        status=job.status,
        parser_name=job.parser_name,
        error_message=job.error_message,
        result=result,
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
        document=_document_read(job.document),
    )


def _task_job_read(job) -> TaskJobRead:
    return TaskJobRead(
        id=job.id,
        task_type=job.task_type,
        status=job.status,
        case_id=job.case_id,
        document_id=job.document_id,
        agent_action_id=job.agent_action_id,
        approval_gate_id=job.approval_gate_id,
        attempt_count=job.attempt_count,
        max_attempts=job.max_attempts,
        not_before=job.not_before,
        payload=_json_or_none(job.payload_json) or {},
        result=_json_or_none(job.result_json) or {},
        error_message=job.error_message,
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
    )


def _drawing_read(drawing) -> DrawingRead:
    return DrawingRead(
        id=drawing.id,
        case_id=drawing.case_id,
        document_id=drawing.document_id,
        title=drawing.title,
        drawing_number=drawing.drawing_number,
        revision=drawing.revision,
        status=drawing.status,
        material_hint=drawing.material_hint,
        analysis=_json_or_none(drawing.analysis_json),
        features=[_drawing_feature_read(feature) for feature in drawing.features],
        created_at=drawing.created_at,
        updated_at=drawing.updated_at,
    )


def _drawing_feature_read(feature) -> DrawingFeatureRead:
    return DrawingFeatureRead(
        id=feature.id,
        drawing_id=feature.drawing_id,
        feature_type=feature.feature_type,
        description=feature.description,
        dimensions=_json_or_none(feature.dimensions_json) or {},
        tolerance=feature.tolerance,
        confidence=feature.confidence,
        reason=feature.reason,
        created_at=feature.created_at,
    )


def _drawing_analysis_data(data: Any) -> DrawingAnalysisResult:
    if isinstance(data, DrawingAnalysisResult):
        return data
    if isinstance(data, dict):
        return DrawingAnalysisResult.model_validate(data)
    raise HTTPException(status_code=502, detail="AI drawing analysis response failed validation")


def _customer_question_draft_data(data: Any) -> CustomerQuestionDraft:
    if isinstance(data, CustomerQuestionDraft):
        return data
    if isinstance(data, dict):
        return CustomerQuestionDraft.model_validate(data)
    raise HTTPException(status_code=502, detail="AI customer question draft failed validation")


def _invoice_extraction_data(data: Any) -> InvoiceExtractionResult:
    if isinstance(data, InvoiceExtractionResult):
        return data
    if isinstance(data, dict):
        return InvoiceExtractionResult.model_validate(data)
    raise HTTPException(status_code=502, detail="AI invoice extraction failed validation")


def _supplier_read(supplier) -> SupplierRead | None:
    if supplier is None:
        return None
    return SupplierRead(
        id=supplier.id,
        name=supplier.name,
        inn=supplier.inn,
        kpp=supplier.kpp,
        bank_details=_json_or_none(supplier.bank_details_json) or {},
        created_at=supplier.created_at,
        updated_at=supplier.updated_at,
    )


def _invoice_line_read(line) -> InvoiceLineRead:
    return InvoiceLineRead(
        id=line.id,
        invoice_id=line.invoice_id,
        line_no=line.line_no,
        description=line.description,
        sku=line.sku,
        quantity=line.quantity,
        unit=line.unit,
        unit_price=line.unit_price,
        line_total=line.line_total,
        tax_rate=line.tax_rate,
        confidence=line.confidence,
        reason=line.reason,
        created_at=line.created_at,
    )


def _invoice_read(invoice) -> InvoiceRead:
    return InvoiceRead(
        id=invoice.id,
        case_id=invoice.case_id,
        supplier=_supplier_read(invoice.supplier),
        document_id=invoice.document_id,
        invoice_number=invoice.invoice_number,
        invoice_date=invoice.invoice_date,
        currency=invoice.currency,
        subtotal_amount=invoice.subtotal_amount,
        tax_amount=invoice.tax_amount,
        total_amount=invoice.total_amount,
        status=invoice.status,
        arithmetic_ok=invoice.arithmetic_ok,
        duplicate_status=invoice.duplicate_status,
        extraction=_json_or_none(invoice.extraction_json),
        lines=[_invoice_line_read(line) for line in invoice.lines],
        created_at=invoice.created_at,
        updated_at=invoice.updated_at,
    )


def _email_thread_read(thread) -> EmailThreadRead:
    return EmailThreadRead(
        id=thread.id,
        case_id=thread.case_id,
        subject=thread.subject,
        external_thread_id=thread.external_thread_id,
        status=thread.status,
        last_message_at=thread.last_message_at,
        messages=[_email_message_read(message) for message in thread.messages],
        created_at=thread.created_at,
        updated_at=thread.updated_at,
    )


def _email_message_read(message) -> EmailMessageRead:
    return EmailMessageRead(
        id=message.id,
        thread_id=message.thread_id,
        direction=message.direction,
        external_message_id=message.external_message_id,
        sender=message.sender,
        recipients=_json_list(message.recipients_json),
        subject=message.subject,
        body_text=message.body_text,
        received_at=message.received_at,
        attachments=[],
        created_at=message.created_at,
    )


def _draft_email_read(draft) -> DraftEmailRead:
    return DraftEmailRead(
        id=draft.id,
        thread_id=draft.thread_id,
        case_id=draft.case_id,
        to=_json_list(draft.to_json),
        cc=_json_list(draft.cc_json),
        subject=draft.subject,
        body_text=draft.body_text,
        status=draft.status,
        risk=_json_or_none(draft.risk_json) or {},
        approval_required=draft.approval_required == "true",
        created_at=draft.created_at,
        updated_at=draft.updated_at,
    )


def _agent_action_read(action) -> AgentActionRead:
    return AgentActionRead(
        id=action.id,
        case_id=action.case_id,
        scenario=action.scenario,
        tool_name=action.tool_name,
        status=action.status,
        step_no=action.step_no,
        payload=_json_or_none(action.payload_json) or {},
        result=_json_or_none(action.result_json) or {},
        created_at=action.created_at,
    )


def _approval_gate_read(gate) -> ApprovalGateRead:
    return ApprovalGateRead(
        id=gate.id,
        case_id=gate.case_id,
        action_id=gate.action_id,
        gate_type=gate.gate_type,
        status=gate.status,
        reason=gate.reason,
        payload=_json_or_none(gate.payload_json) or {},
        created_at=gate.created_at,
        decided_at=gate.decided_at,
    )


def _task_for_approved_gate(db: Session, gate):
    if gate.gate_type not in {"email.send.request_approval", "invoice.export.1c.prepare"}:
        return None
    payload = _json_or_none(gate.payload_json) or {}
    document_id = payload.get("document_id") if isinstance(payload.get("document_id"), str) else None
    return create_task_job(
        db,
        task_type=gate.gate_type,
        case_id=gate.case_id,
        document_id=document_id,
        agent_action_id=gate.action_id,
        approval_gate_id=gate.id,
        payload=payload,
    )


def _invoice_xlsx_bytes(invoice) -> bytes:
    try:
        from openpyxl import Workbook
    except ImportError as exc:
        raise HTTPException(status_code=501, detail="openpyxl is not installed") from exc
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Invoice"
    sheet.append(["Invoice number", invoice.invoice_number or ""])
    sheet.append(["Invoice date", invoice.invoice_date or ""])
    sheet.append(["Supplier", invoice.supplier.name if invoice.supplier else ""])
    sheet.append(["INN", invoice.supplier.inn if invoice.supplier else ""])
    sheet.append(["Currency", invoice.currency])
    sheet.append(["Subtotal", invoice.subtotal_amount])
    sheet.append(["Tax", invoice.tax_amount])
    sheet.append(["Total", invoice.total_amount])
    sheet.append([])
    sheet.append(["#", "SKU", "Description", "Qty", "Unit", "Unit price", "Line total", "VAT"])
    for line in invoice.lines:
        sheet.append(
            [
                line.line_no,
                line.sku,
                line.description,
                line.quantity,
                line.unit,
                line.unit_price,
                line.line_total,
                line.tax_rate,
            ]
        )
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _onec_invoice_payload(invoice) -> dict[str, Any]:
    return {
        "type": "invoice",
        "approval_required": True,
        "invoice": {
            "id": invoice.id,
            "number": invoice.invoice_number,
            "date": invoice.invoice_date,
            "currency": invoice.currency,
            "subtotal": invoice.subtotal_amount,
            "tax": invoice.tax_amount,
            "total": invoice.total_amount,
            "arithmetic_ok": invoice.arithmetic_ok,
            "duplicate_status": invoice.duplicate_status,
        },
        "supplier": {
            "id": invoice.supplier.id,
            "name": invoice.supplier.name,
            "inn": invoice.supplier.inn,
            "kpp": invoice.supplier.kpp,
        }
        if invoice.supplier
        else None,
        "lines": [
            {
                "line_no": line.line_no,
                "sku": line.sku,
                "description": line.description,
                "quantity": line.quantity,
                "unit": line.unit,
                "unit_price": line.unit_price,
                "line_total": line.line_total,
                "tax_rate": line.tax_rate,
            }
            for line in invoice.lines
        ],
    }


def _json_or_none(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _json_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


def _processing_status(value: str):
    from backend.app.domain.models import ProcessingJobStatus

    try:
        return ProcessingJobStatus(value)
    except ValueError:
        return ProcessingJobStatus.FAILED


def _classification_prompt(filename: str, content_type: str | None) -> str:
    return (
        "Classify this manufacturing workspace document. "
        "Return a short label such as invoice, drawing, quote, email, process_plan, norm_sheet, "
        "tooling_catalog, or unknown, plus a short reason.\n"
        f"Filename: {filename}\nContent-Type: {content_type or 'unknown'}"
    )


def _drawing_analysis_prompt(document) -> str:
    extraction = _json_or_none(document.extraction_result_json) or {}
    extracted_text = document.extracted_text or _safe_text_preview(document.storage_path, limit=8000)
    return (
        "Analyze this manufacturing drawing or technical document. "
        "Return JSON matching this schema: "
        "{title, drawing_number, revision, material_hint, summary, unclear_items, risks, "
        "questions, features:[{feature_type, description, dimensions, tolerance, confidence, reason}]}.\n"
        "Focus on what is known, what is risky, and which questions a technologist should ask. "
        "Do not invent dimensions; mark uncertainty in unclear_items/questions.\n\n"
        f"Filename: {document.filename}\n"
        f"Content-Type: {document.content_type or 'unknown'}\n"
        f"Known document type: {document.document_type or 'unknown'}\n"
        f"Extracted text:\n{extracted_text}\n\n"
        f"Previous extraction JSON:\n{json.dumps(extraction, ensure_ascii=False)[:8000]}"
    )


def _document_image_data_uris(document) -> list[str]:
    uris: list[str] = []
    for artifact in document.artifacts:
        if not artifact.content_type or not artifact.content_type.startswith("image/"):
            continue
        path = Path(artifact.storage_path)
        if not path.exists():
            continue
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        uris.append(f"data:{artifact.content_type};base64,{encoded}")
    return uris


def _customer_question_prompt(drawing) -> str:
    analysis = _json_or_none(drawing.analysis_json) or {}
    return (
        "Draft a concise Russian customer clarification email for a manufacturing drawing. "
        "Do not send anything. Return JSON matching: "
        "{subject:string, body:string, questions:[string], risks:[string], approval_required:true}. "
        "Keep the tone professional and ask only questions supported by unclear_items, risks, "
        "or missing manufacturing data.\n\n"
        f"Drawing title: {drawing.title}\n"
        f"Drawing number: {drawing.drawing_number or 'unknown'}\n"
        f"Revision: {drawing.revision or 'unknown'}\n"
        f"Material hint: {drawing.material_hint or 'unknown'}\n"
        f"Analysis JSON:\n{json.dumps(analysis, ensure_ascii=False)[:12000]}"
    )


def _invoice_extraction_prompt(document) -> str:
    extraction = _json_or_none(document.extraction_result_json) or {}
    text = document.extracted_text or _safe_text_preview(document.storage_path, limit=16000)
    return (
        "Extract invoice data from this manufacturing procurement document. "
        "Return JSON matching this schema: "
        "{document_type:'invoice', supplier:{name, inn, kpp, bank_details, confidence, reason}, "
        "invoice_number, invoice_date, currency, subtotal_amount, tax_amount, total_amount, "
        "lines:[{line_no, description, sku, quantity, unit, unit_price, line_total, tax_rate, "
        "confidence, reason}], confidence, reason}. "
        "Use numbers as numeric values, RUB as default currency, and do not invent missing fields. "
        "Russian invoices often contain 'Счет №', 'ИНН', 'КПП', 'Итого', 'НДС', and table rows.\n\n"
        f"Filename: {document.filename}\n"
        f"Content-Type: {document.content_type or 'unknown'}\n"
        f"Extracted text:\n{text}\n\n"
        f"Previous processing JSON:\n{json.dumps(extraction, ensure_ascii=False)[:12000]}"
    )


def _guess_document_type(ai_text: str, filename: str) -> str:
    haystack = f"{ai_text} {filename}".lower()
    if any(token in haystack for token in ["invoice", "счет", "счёт"]):
        return "invoice"
    if any(token in haystack for token in ["drawing", "чертеж", "чертёж", ".dxf", ".dwg", ".step"]):
        return "drawing"
    if any(token in haystack for token in ["quote", "кп", "quotation"]):
        return "quote"
    if any(token in haystack for token in ["process", "техпроцесс", "route"]):
        return "process_plan"
    return "unknown"


def _safe_text_preview(storage_path: str, limit: int = 12000) -> str:
    path = Path(storage_path)
    if not path.exists() or path.suffix.lower() not in {".txt", ".md", ".csv", ".json", ".xml"}:
        return "[binary or unsupported preview; OCR/render pipeline will be added in the next slice]"
    try:
        return path.read_text(encoding="utf-8", errors="ignore")[:limit]
    except OSError:
        return "[failed to read text preview]"
