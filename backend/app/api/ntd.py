"""NTD API — SQL-first normative base and norm-control checks."""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.audit.service import add_timeline_event, log_action
from app.db.models import (
    Document,
    DocumentChunk,
    DocumentExtraction,
    DocumentStatus,
    ExtractionField,
    NTDCheckFinding,
    NTDCheckRun,
    NTDControlSettings,
    NormativeClause,
    NormativeDocument,
    NormativeDocumentVersion,
    NormativeRequirement,
)
from app.db.session import get_db
from app.db.text_search import text_search_condition, text_search_rank
from app.domain.ntd import (
    NTDCheckRunDetail,
    NTDCheckAvailabilityResponse,
    NTDCheckRunOut,
    NTDCheckRunRequest,
    NTDControlSettingsOut,
    NTDControlSettingsUpdate,
    NTDDocumentCreateFromSourceRequest,
    NTDDocumentCreateFromSourceResponse,
    NTDDocumentIndexRequest,
    NTDDocumentIndexResponse,
    NTDFindingDecisionRequest,
    NTDFindingOut,
    NTDRequirementSearchResponse,
    NormativeClauseCreate,
    NormativeClauseOut,
    NormativeDocumentCreate,
    NormativeDocumentOut,
    NormativeRequirementCreate,
    NormativeRequirementOut,
)
from app.domain.ntd_checker import build_ntd_findings, build_semantic_ntd_findings
from app.domain.ntd_parser import detect_normative_metadata, parse_normative_text

router = APIRouter()


@router.get("/settings/ntd-control", response_model=NTDControlSettingsOut)
async def get_ntd_control_settings(db: AsyncSession = Depends(get_db)):
    """Skill: ntd.control_settings_get — Read NTD norm-control mode."""
    settings = await _get_or_create_settings(db)
    await db.commit()
    return NTDControlSettingsOut(
        mode=settings.mode,
        updated_by=settings.updated_by,
        updated_at=settings.updated_at,
    )


@router.patch("/settings/ntd-control", response_model=NTDControlSettingsOut)
async def update_ntd_control_settings(
    payload: NTDControlSettingsUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Skill: ntd.control_settings_update — Set manual or automatic NTD norm-control mode."""
    settings = await _get_or_create_settings(db)
    settings.mode = payload.mode
    settings.updated_by = payload.updated_by
    await log_action(
        db,
        action="ntd.control_settings_update",
        entity_type="ntd_control_settings",
        entity_id=settings.id,
        user_id=payload.updated_by,
        details={"mode": payload.mode},
    )
    await db.commit()
    await db.refresh(settings)
    return NTDControlSettingsOut(
        mode=settings.mode,
        updated_by=settings.updated_by,
        updated_at=settings.updated_at,
    )


@router.get("/ntd/documents", response_model=list[NormativeDocumentOut])
async def list_normative_documents(
    status_filter: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    """Skill: ntd.document_list — List normative documents."""
    query = select(NormativeDocument).order_by(NormativeDocument.code)
    if status_filter:
        query = query.where(NormativeDocument.status == status_filter)
    result = await db.execute(query)
    return result.scalars().all()


@router.post(
    "/ntd/documents",
    response_model=NormativeDocumentOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_normative_document(
    payload: NormativeDocumentCreate,
    db: AsyncSession = Depends(get_db),
):
    """Skill: ntd.document_create — Create a normative document record."""
    doc = NormativeDocument(
        code=payload.code,
        title=payload.title,
        document_type=payload.document_type,
        status=payload.status,
        scope=payload.scope,
        source_document_id=payload.source_document_id,
        metadata_=payload.metadata_,
    )
    db.add(doc)
    await db.flush()
    version = NormativeDocumentVersion(
        normative_document_id=doc.id,
        version_label=payload.version,
        status=payload.status,
        source_document_id=payload.source_document_id,
    )
    db.add(version)
    await db.flush()
    doc.current_version_id = version.id
    await log_action(
        db,
        action="ntd.document_create",
        entity_type="normative_document",
        entity_id=doc.id,
        details={"code": doc.code, "version": payload.version},
    )
    await db.commit()
    await db.refresh(doc)
    return doc


@router.post(
    "/ntd/documents/from-source",
    response_model=NTDDocumentCreateFromSourceResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_normative_document_from_source(
    payload: NTDDocumentCreateFromSourceRequest,
    db: AsyncSession = Depends(get_db),
):
    """Skill: ntd.document_create_from_source — Create and optionally index NTD from an uploaded document."""
    source_document = await db.get(Document, payload.source_document_id)
    if not source_document:
        raise HTTPException(status_code=404, detail="Source document not found")
    text = await _document_text(db, source_document.id)
    if not text.strip():
        raise HTTPException(status_code=409, detail="Source document has no extracted text")

    detected = detect_normative_metadata(text, fallback_title=source_document.file_name)
    normative_document = NormativeDocument(
        code=payload.code or detected.code,
        title=payload.title or detected.title,
        document_type=payload.document_type or detected.document_type,
        status=payload.status,
        scope=text[:1000],
        source_document_id=source_document.id,
        metadata_={"source": "detected_from_document"},
    )
    db.add(normative_document)
    await db.flush()
    version = NormativeDocumentVersion(
        normative_document_id=normative_document.id,
        version_label=payload.version or detected.version,
        status=payload.status,
        source_document_id=source_document.id,
    )
    db.add(version)
    await db.flush()
    normative_document.current_version_id = version.id

    await log_action(
        db,
        action="ntd.document_create_from_source",
        entity_type="normative_document",
        entity_id=normative_document.id,
        user_id=payload.actor,
        details={"source_document_id": str(source_document.id), "code": normative_document.code},
    )

    index_result = None
    if payload.index_immediately:
        index_result = await _index_normative_document_from_text(
            db,
            normative_document,
            text=text,
            source_document_id=source_document.id,
            requirement_type=payload.requirement_type,
            replace_existing=False,
            actor=payload.actor,
        )

    await db.commit()
    await db.refresh(normative_document)
    return NTDDocumentCreateFromSourceResponse(
        normative_document=NormativeDocumentOut.model_validate(normative_document),
        index_result=index_result,
    )


@router.post("/ntd/documents/{normative_document_id}/index", response_model=NTDDocumentIndexResponse)
async def index_normative_document(
    normative_document_id: uuid.UUID,
    payload: NTDDocumentIndexRequest,
    db: AsyncSession = Depends(get_db),
):
    """Skill: ntd.document_index — Parse source document text into NTD clauses and requirements."""
    normative_document = await db.get(NormativeDocument, normative_document_id)
    if not normative_document:
        raise HTTPException(status_code=404, detail="Normative document not found")

    source_document_id = payload.source_document_id or normative_document.source_document_id
    text = ""
    if source_document_id:
        source_document = await db.get(Document, source_document_id)
        if not source_document:
            raise HTTPException(status_code=404, detail="Source document not found")
        text = await _document_text(db, source_document.id)
    if not text.strip():
        text = "\n".join(part for part in [normative_document.title, normative_document.scope] if part)
    if not text.strip():
        raise HTTPException(status_code=409, detail="No text available for NTD indexing")

    response = await _index_normative_document_from_text(
        db,
        normative_document,
        text,
        source_document_id=source_document_id,
        requirement_type=payload.requirement_type,
        replace_existing=payload.replace_existing,
        actor=payload.actor,
    )
    await db.commit()
    return response


async def _index_normative_document_from_text(
    db: AsyncSession,
    normative_document: NormativeDocument,
    *,
    text: str,
    source_document_id: uuid.UUID | None,
    requirement_type: str,
    replace_existing: bool,
    actor: str,
) -> NTDDocumentIndexResponse:
    if replace_existing:
        existing_requirements = await db.execute(
            select(NormativeRequirement).where(
                NormativeRequirement.normative_document_id == normative_document.id
            )
        )
        for requirement in existing_requirements.scalars().all():
            await db.delete(requirement)
        existing_clauses = await db.execute(
            select(NormativeClause).where(
                NormativeClause.normative_document_id == normative_document.id
            )
        )
        for clause in existing_clauses.scalars().all():
            await db.delete(clause)
        await db.flush()

    parsed = parse_normative_text(
        text,
        code=normative_document.code,
        default_requirement_type=requirement_type,
    )
    clauses_by_number: dict[str, NormativeClause] = {}
    for parsed_clause in parsed.clauses:
        clause = NormativeClause(
            normative_document_id=normative_document.id,
            version_id=normative_document.current_version_id,
            clause_number=parsed_clause.clause_number,
            title=parsed_clause.title,
            text=parsed_clause.text,
            metadata_={"source": "ntd_parser"},
        )
        db.add(clause)
        await db.flush()
        clauses_by_number[parsed_clause.clause_number] = clause

    for parsed_requirement in parsed.requirements:
        clause = clauses_by_number.get(parsed_requirement.clause_number)
        db.add(
            NormativeRequirement(
                normative_document_id=normative_document.id,
                clause_id=clause.id if clause else None,
                requirement_code=parsed_requirement.requirement_code,
                requirement_type=parsed_requirement.requirement_type,
                applies_to=[parsed_requirement.requirement_type],
                text=parsed_requirement.text,
                required_keywords=parsed_requirement.required_keywords,
                severity=parsed_requirement.severity,
                is_active=True,
                metadata_={"source": "ntd_parser"},
            )
        )

    await log_action(
        db,
        action="ntd.document_index",
        entity_type="normative_document",
        entity_id=normative_document.id,
        user_id=actor,
        details={
            "source_document_id": str(source_document_id) if source_document_id else None,
            "clauses_created": len(parsed.clauses),
            "requirements_created": len(parsed.requirements),
        },
    )
    return NTDDocumentIndexResponse(
        normative_document_id=normative_document.id,
        source_document_id=source_document_id,
        clauses_created=len(parsed.clauses),
        requirements_created=len(parsed.requirements),
        text_chars=len(text),
    )


@router.post(
    "/ntd/clauses",
    response_model=NormativeClauseOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_normative_clause(
    payload: NormativeClauseCreate,
    db: AsyncSession = Depends(get_db),
):
    """Skill: ntd.clause_create — Create a normative document clause."""
    document = await db.get(NormativeDocument, payload.normative_document_id)
    if not document:
        raise HTTPException(status_code=404, detail="Normative document not found")
    clause = NormativeClause(
        normative_document_id=payload.normative_document_id,
        version_id=payload.version_id or document.current_version_id,
        parent_clause_id=payload.parent_clause_id,
        clause_number=payload.clause_number,
        title=payload.title,
        text=payload.text,
        metadata_=payload.metadata_,
    )
    db.add(clause)
    await db.commit()
    await db.refresh(clause)
    return clause


@router.post(
    "/ntd/requirements",
    response_model=NormativeRequirementOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_normative_requirement(
    payload: NormativeRequirementCreate,
    db: AsyncSession = Depends(get_db),
):
    """Skill: ntd.requirement_create — Create a normative requirement."""
    document = await db.get(NormativeDocument, payload.normative_document_id)
    if not document:
        raise HTTPException(status_code=404, detail="Normative document not found")
    requirement = NormativeRequirement(
        normative_document_id=payload.normative_document_id,
        clause_id=payload.clause_id,
        requirement_code=payload.requirement_code,
        requirement_type=payload.requirement_type,
        applies_to=payload.applies_to,
        text=payload.text,
        required_keywords=payload.required_keywords,
        severity=payload.severity,
        is_active=payload.is_active,
        metadata_=payload.metadata_,
    )
    db.add(requirement)
    await db.commit()
    await db.refresh(requirement)
    return requirement


@router.get("/ntd/requirements/search", response_model=NTDRequirementSearchResponse)
async def search_requirements(
    query: str,
    limit: int = 20,
    db: AsyncSession = Depends(get_db),
):
    """Skill: ntd.requirement_search — Search SQL-first NTD requirements."""
    columns = [
        NormativeRequirement.requirement_code,
        NormativeRequirement.requirement_type,
        NormativeRequirement.text,
        NormativeDocument.code,
        NormativeDocument.title,
    ]
    rank = text_search_rank(db, columns, query)
    statement = (
        select(NormativeRequirement)
        .join(NormativeDocument, NormativeDocument.id == NormativeRequirement.normative_document_id)
        .where(NormativeRequirement.is_active.is_(True), text_search_condition(db, columns, query))
    )
    if rank is not None:
        statement = statement.order_by(desc(rank), NormativeRequirement.requirement_code)
    else:
        statement = statement.order_by(NormativeRequirement.requirement_code)
    result = await db.execute(statement.limit(limit))
    requirements = result.scalars().all()
    return NTDRequirementSearchResponse(
        query=query,
        requirements=requirements,
        total=len(requirements),
    )


@router.post("/documents/{document_id}/ntd-check", response_model=NTDCheckRunDetail)
async def run_document_ntd_check(
    document_id: uuid.UUID,
    payload: NTDCheckRunRequest | None = None,
    db: AsyncSession = Depends(get_db),
):
    """Skill: ntd.norm_control_run — Check one document against applicable NTD."""
    request = payload or NTDCheckRunRequest(document_id=document_id)
    if request.document_id != document_id:
        raise HTTPException(status_code=400, detail="Path document_id and payload document_id differ")
    return await _run_ntd_check(db, request)


@router.get("/documents/{document_id}/ntd-check/availability", response_model=NTDCheckAvailabilityResponse)
async def get_document_ntd_check_availability(
    document_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Skill: ntd.check_availability — Explain whether NTD check can run for a document."""
    document = await db.get(Document, document_id)
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")
    settings = await _get_or_create_settings(db)
    text = await _document_text(db, document.id)
    active_requirements_result = await db.execute(
        select(func.count())
        .select_from(NormativeRequirement)
        .where(NormativeRequirement.is_active.is_(True))
    )
    active_requirements = int(active_requirements_result.scalar_one())

    reasons: list[str] = []
    if document.status == DocumentStatus.suspicious:
        reasons.append("document_quarantined")
    if not text.strip():
        reasons.append("document_has_no_text")
    if active_requirements == 0:
        reasons.append("ntd_requirements_not_configured")

    await db.commit()
    return NTDCheckAvailabilityResponse(
        document_id=document.id,
        can_check=not reasons,
        reasons=reasons,
        active_requirements=active_requirements,
        has_text=bool(text.strip()),
        mode=settings.mode,
    )


@router.post("/ntd/checks/run", response_model=NTDCheckRunDetail)
async def run_ntd_check(
    payload: NTDCheckRunRequest,
    db: AsyncSession = Depends(get_db),
):
    """Skill: ntd.norm_control_run_payload — Check a document against applicable NTD."""
    return await _run_ntd_check(db, payload)


@router.get("/documents/{document_id}/ntd-checks", response_model=list[NTDCheckRunOut])
async def list_document_ntd_checks(
    document_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Skill: ntd.check_list — List NTD checks for a document."""
    result = await db.execute(
        select(NTDCheckRun)
        .where(NTDCheckRun.document_id == document_id)
        .order_by(NTDCheckRun.created_at.desc())
    )
    return result.scalars().all()


@router.get("/ntd/checks/{check_id}", response_model=NTDCheckRunDetail)
async def get_ntd_check(
    check_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Skill: ntd.check_get — Get NTD check details and findings."""
    result = await db.execute(
        select(NTDCheckRun)
        .where(NTDCheckRun.id == check_id)
        .options(selectinload(NTDCheckRun.findings))
    )
    check = result.scalar_one_or_none()
    if not check:
        raise HTTPException(status_code=404, detail="NTD check not found")
    return NTDCheckRunDetail(check=check, findings=check.findings)


@router.post(
    "/ntd/checks/{check_id}/findings/{finding_id}/decide",
    response_model=NTDFindingOut,
)
async def decide_ntd_finding(
    check_id: uuid.UUID,
    finding_id: uuid.UUID,
    payload: NTDFindingDecisionRequest,
    db: AsyncSession = Depends(get_db),
):
    """Skill: ntd.finding_decide — Record a human decision for an NTD finding."""
    finding = await db.get(NTDCheckFinding, finding_id)
    if not finding or finding.check_id != check_id:
        raise HTTPException(status_code=404, detail="NTD finding not found")
    status_by_action = {
        "accept": "accepted",
        "reject": "rejected",
        "mark_not_applicable": "not_applicable",
        "create_correction_task": "correction_task",
    }
    finding.status = status_by_action[payload.action]
    finding.decided_by = payload.decided_by
    finding.decided_at = datetime.now(UTC)
    finding.decision_comment = payload.comment

    check = await db.get(NTDCheckRun, check_id)
    if check:
        await _refresh_check_counts(db, check)
    await log_action(
        db,
        action="ntd.finding_decide",
        entity_type="ntd_check_finding",
        entity_id=finding.id,
        user_id=payload.decided_by,
        details={"action": payload.action, "status": finding.status},
    )
    await db.commit()
    await db.refresh(finding)
    return finding


async def _run_ntd_check(db: AsyncSession, payload: NTDCheckRunRequest) -> NTDCheckRunDetail:
    document = await db.get(Document, payload.document_id)
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")
    if document.status == DocumentStatus.suspicious:
        raise HTTPException(status_code=409, detail="Quarantined document cannot be checked")

    text = await _document_text(db, document.id)
    if not text.strip():
        raise HTTPException(status_code=409, detail="Document has no extracted text")

    settings = await _get_or_create_settings(db)
    requirements = await _applicable_requirements(
        db,
        text=text,
        document_type=document.doc_type.value if document.doc_type else None,
        normative_document_ids=payload.normative_document_ids,
    )
    check = NTDCheckRun(
        document_id=document.id,
        status="completed",
        mode=settings.mode,
        triggered_by=payload.triggered_by,
        summary="Проверка выполнена без замечаний.",
        metadata_={
            "requirements_checked": len(requirements),
            "semantic_ai": payload.semantic_ai,
            "semantic_max_requirements": payload.semantic_max_requirements,
        },
    )
    db.add(check)
    await db.flush()

    findings = build_ntd_findings(check, document, text, requirements)
    if payload.semantic_ai:
        semantic_findings = await build_semantic_ntd_findings(
            check,
            document,
            text,
            requirements,
            max_requirements=payload.semantic_max_requirements,
        )
        findings.extend(_deduplicate_findings(findings, semantic_findings))
    for finding in findings:
        db.add(finding)
    check.findings_total = len(findings)
    check.findings_open = len(findings)
    if findings:
        check.summary = f"Найдено замечаний НТД: {len(findings)}."

    await log_action(
        db,
        action="ntd.norm_control_run",
        entity_type="document",
        entity_id=document.id,
        user_id=payload.actor,
        details={
            "triggered_by": payload.triggered_by,
            "mode": settings.mode,
            "findings_total": len(findings),
        },
    )
    await add_timeline_event(
        db,
        entity_type="document",
        entity_id=document.id,
        event_type="ntd_check_completed",
        summary=check.summary or "Проверка НТД выполнена.",
        actor=payload.actor,
        details={"check_id": str(check.id), "findings_total": len(findings)},
    )
    await db.commit()
    await db.refresh(check)
    result = await db.execute(
        select(NTDCheckFinding)
        .where(NTDCheckFinding.check_id == check.id)
        .order_by(NTDCheckFinding.severity.desc(), NTDCheckFinding.created_at)
    )
    return NTDCheckRunDetail(check=check, findings=result.scalars().all())


async def _get_or_create_settings(db: AsyncSession) -> NTDControlSettings:
    result = await db.execute(
        select(NTDControlSettings).where(NTDControlSettings.singleton_key == "default")
    )
    settings = result.scalar_one_or_none()
    if settings:
        return settings
    settings = NTDControlSettings(singleton_key="default", mode="manual")
    db.add(settings)
    await db.flush()
    return settings


def _deduplicate_findings(
    existing: list[NTDCheckFinding],
    candidates: list[NTDCheckFinding],
) -> list[NTDCheckFinding]:
    seen = {
        (
            finding.requirement_id,
            finding.finding_code,
            (finding.message or "").strip().lower(),
        )
        for finding in existing
    }
    unique: list[NTDCheckFinding] = []
    for candidate in candidates:
        key = (
            candidate.requirement_id,
            candidate.finding_code,
            (candidate.message or "").strip().lower(),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
    return unique


async def _document_text(db: AsyncSession, document_id: uuid.UUID) -> str:
    parts: list[str] = []
    chunks = await db.execute(
        select(DocumentChunk)
        .where(DocumentChunk.document_id == document_id)
        .order_by(DocumentChunk.chunk_index)
    )
    parts.extend(chunk.text for chunk in chunks.scalars().all() if chunk.text)

    extractions = await db.execute(
        select(DocumentExtraction)
        .where(DocumentExtraction.document_id == document_id)
        .order_by(DocumentExtraction.created_at.desc())
        .limit(3)
    )
    extraction_ids = [extraction.id for extraction in extractions.scalars().all()]
    if extraction_ids:
        fields = await db.execute(
            select(ExtractionField).where(ExtractionField.extraction_id.in_(extraction_ids))
        )
        for field in fields.scalars().all():
            value = field.corrected_value or field.field_value
            if value:
                parts.append(f"{field.field_name}: {value}")
    return "\n".join(parts)


async def _applicable_requirements(
    db: AsyncSession,
    *,
    text: str,
    document_type: str | None,
    normative_document_ids: list[uuid.UUID] | None,
) -> list[NormativeRequirement]:
    query = select(NormativeRequirement).where(NormativeRequirement.is_active.is_(True))
    if normative_document_ids:
        query = query.where(NormativeRequirement.normative_document_id.in_(normative_document_ids))
    result = await db.execute(query.order_by(NormativeRequirement.requirement_code))
    requirements = result.scalars().all()
    if normative_document_ids:
        return requirements

    lower_text = text.lower()
    standard_codes = set(re.findall(r"\b(?:гост|ост|ту|стп)\s*[\d.\-–—/]+", lower_text, re.I))
    applicable: list[NormativeRequirement] = []
    for requirement in requirements:
        applies_to = {str(item).lower() for item in (requirement.applies_to or [])}
        code = requirement.requirement_code.lower()
        if document_type and document_type.lower() in applies_to:
            applicable.append(requirement)
        elif any(term in lower_text for term in applies_to if term):
            applicable.append(requirement)
        elif any(code.startswith(std_code) for std_code in standard_codes):
            applicable.append(requirement)
        elif not applies_to:
            applicable.append(requirement)
    return applicable


async def _refresh_check_counts(db: AsyncSession, check: NTDCheckRun) -> None:
    total = await db.execute(
        select(func.count()).select_from(NTDCheckFinding).where(NTDCheckFinding.check_id == check.id)
    )
    open_count = await db.execute(
        select(func.count())
        .select_from(NTDCheckFinding)
        .where(NTDCheckFinding.check_id == check.id, NTDCheckFinding.status == "open")
    )
    check.findings_total = int(total.scalar_one())
    check.findings_open = int(open_count.scalar_one())
