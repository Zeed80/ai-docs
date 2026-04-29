"""Manufacturing technology API — process plans, operations, resources, norms."""

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.audit.service import log_action
from app.db.models import (
    BOM,
    Document,
    EntityMention,
    KnowledgeEdge,
    KnowledgeNode,
    ManufacturingCheckResult,
    ManufacturingNormEstimate,
    ManufacturingOperation,
    ManufacturingOperationTemplate,
    ManufacturingProcessPlan,
    ManufacturingResource,
    TechnologyCorrection,
    TechnologyLearningRule,
)
from app.db.session import get_db
from app.domain.technology import (
    ManufacturingResourceCreate,
    ManufacturingResourceOut,
    LearningSuggestionOut,
    LearningSuggestionResponse,
    LearningRuleActivateRequest,
    LearningRuleCreate,
    LearningRuleListResponse,
    LearningRuleOut,
    NormEstimateApproveRequest,
    NormEstimateCreate,
    NormEstimateOut,
    OperationCreate,
    OperationOut,
    OperationTemplateCreate,
    OperationTemplateListResponse,
    OperationTemplateOut,
    ProcessPlanApproveRequest,
    ProcessPlanCreate,
    ProcessPlanDetail,
    ProcessPlanEstimateNormsRequest,
    ProcessPlanEstimateNormsResponse,
    ProcessPlanDraftFromDocumentRequest,
    ProcessPlanDraftFromDocumentResponse,
    ProcessPlanListResponse,
    ProcessPlanOut,
    ResourceListResponse,
    TechnologyCheckResponse,
    TechnologyCorrectionCreate,
    TechnologyCorrectionOut,
)

router = APIRouter()


@router.get("/resources", response_model=ResourceListResponse)
async def list_resources(
    resource_type: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    """Skill: tech.resource_list — List machines, tools, fixtures, and equipment."""
    query = select(ManufacturingResource)
    count_query = select(func.count()).select_from(ManufacturingResource)
    if resource_type:
        query = query.where(ManufacturingResource.resource_type == resource_type)
        count_query = count_query.where(ManufacturingResource.resource_type == resource_type)
    total = (await db.execute(count_query)).scalar() or 0
    result = await db.execute(
        query.order_by(ManufacturingResource.name).offset(offset).limit(limit)
    )
    return ResourceListResponse(items=list(result.scalars().all()), total=total)


@router.post("/resources", response_model=ManufacturingResourceOut, status_code=201)
async def create_resource(
    payload: ManufacturingResourceCreate,
    db: AsyncSession = Depends(get_db),
):
    """Skill: tech.resource_create — Create a manufacturing resource."""
    resource = ManufacturingResource(**payload.model_dump(by_alias=False))
    db.add(resource)
    await db.flush()
    node = await _get_or_create_resource_node(db, resource)
    await log_action(
        db,
        action="tech.resource_create",
        entity_type="manufacturing_resource",
        entity_id=resource.id,
        details={"node_id": str(node.id), "resource_type": resource.resource_type},
    )
    await db.commit()
    await db.refresh(resource)
    return resource


@router.get("/operation-templates", response_model=OperationTemplateListResponse)
async def list_operation_templates(
    operation_type: str | None = None,
    active_only: bool = True,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    """Skill: tech.operation_template_list — List technology operation templates."""
    query = select(ManufacturingOperationTemplate)
    count_query = select(func.count()).select_from(ManufacturingOperationTemplate)
    if operation_type:
        query = query.where(ManufacturingOperationTemplate.operation_type == operation_type)
        count_query = count_query.where(
            ManufacturingOperationTemplate.operation_type == operation_type
        )
    if active_only:
        query = query.where(ManufacturingOperationTemplate.is_active.is_(True))
        count_query = count_query.where(ManufacturingOperationTemplate.is_active.is_(True))
    total = (await db.execute(count_query)).scalar() or 0
    result = await db.execute(
        query.order_by(ManufacturingOperationTemplate.operation_type, ManufacturingOperationTemplate.name)
        .offset(offset)
        .limit(limit)
    )
    return OperationTemplateListResponse(items=list(result.scalars().all()), total=total)


@router.post("/operation-templates", response_model=OperationTemplateOut, status_code=201)
async def create_operation_template(
    payload: OperationTemplateCreate,
    db: AsyncSession = Depends(get_db),
):
    """Skill: tech.operation_template_create — Create an operation template."""
    template = ManufacturingOperationTemplate(**payload.model_dump(by_alias=False))
    db.add(template)
    await db.flush()
    await log_action(
        db,
        action="tech.operation_template_create",
        entity_type="manufacturing_operation_template",
        entity_id=template.id,
        details={"operation_type": template.operation_type, "name": template.name},
    )
    await db.commit()
    await db.refresh(template)
    return template


@router.get("/process-plans", response_model=ProcessPlanListResponse)
async def list_process_plans(
    status: str | None = None,
    document_id: uuid.UUID | None = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    """Skill: tech.process_plan_list — List manufacturing process plans."""
    query = select(ManufacturingProcessPlan)
    count_query = select(func.count()).select_from(ManufacturingProcessPlan)
    if status:
        query = query.where(ManufacturingProcessPlan.status == status)
        count_query = count_query.where(ManufacturingProcessPlan.status == status)
    if document_id:
        query = query.where(ManufacturingProcessPlan.document_id == document_id)
        count_query = count_query.where(ManufacturingProcessPlan.document_id == document_id)
    total = (await db.execute(count_query)).scalar() or 0
    result = await db.execute(
        query.order_by(ManufacturingProcessPlan.created_at.desc()).offset(offset).limit(limit)
    )
    return ProcessPlanListResponse(items=list(result.scalars().all()), total=total)


@router.post("/process-plans", response_model=ProcessPlanOut, status_code=201)
async def create_process_plan(
    payload: ProcessPlanCreate,
    db: AsyncSession = Depends(get_db),
):
    """Skill: tech.process_plan_create — Create a manufacturing process plan."""
    if payload.document_id and not await db.get(Document, payload.document_id):
        raise HTTPException(404, "Document not found")
    if payload.bom_id and not await db.get(BOM, payload.bom_id):
        raise HTTPException(404, "BOM not found")

    plan = ManufacturingProcessPlan(**payload.model_dump(by_alias=False))
    db.add(plan)
    await db.flush()
    plan_node = await _get_or_create_process_plan_node(db, plan)
    if payload.document_id:
        document_node = await _get_document_node(db, payload.document_id)
        if document_node:
            await _create_edge(
                db,
                source_id=plan_node.id,
                target_id=document_node.id,
                edge_type="derived_from",
                reason="Process plan was created from document context",
                source_document_id=payload.document_id,
            )
    await log_action(
        db,
        action="tech.process_plan_create",
        entity_type="manufacturing_process_plan",
        entity_id=plan.id,
        details={"node_id": str(plan_node.id), "product_name": plan.product_name},
    )
    await db.commit()
    await db.refresh(plan)
    return plan


@router.post(
    "/process-plans/draft-from-document",
    response_model=ProcessPlanDraftFromDocumentResponse,
    status_code=201,
)
async def draft_process_plan_from_document(
    payload: ProcessPlanDraftFromDocumentRequest,
    db: AsyncSession = Depends(get_db),
):
    """Skill: tech.process_plan_draft_from_document — Draft process plan from memory."""
    document = await db.get(Document, payload.document_id)
    if not document:
        raise HTTPException(404, "Document not found")

    if payload.rebuild_existing:
        existing_result = await db.execute(
            select(ManufacturingProcessPlan).where(
                ManufacturingProcessPlan.document_id == payload.document_id,
                ManufacturingProcessPlan.status == "draft",
            )
        )
        for existing in existing_result.scalars().all():
            await db.delete(existing)
        await db.flush()

    mentions = await _mentions_by_type(db, payload.document_id)
    product_name = payload.product_name or _product_name_from_document(document)
    material = _first_value(mentions, "material")
    standards = mentions.get("standard", [])

    plan = ManufacturingProcessPlan(
        document_id=payload.document_id,
        product_name=product_name,
        product_code=payload.product_code,
        status="draft",
        standard_system="ЕСТД",
        route_summary=_route_summary(mentions),
        material=material,
        quality_requirements=_quality_requirements(standards),
        created_by=payload.created_by,
        metadata_={"source": "draft_from_document", "document_id": str(payload.document_id)},
    )
    db.add(plan)
    await db.flush()

    resources_created = 0
    machine_resource = None
    tool_resource = None
    fixture_resource = None
    if machine_name := _first_value(mentions, "machine"):
        machine_resource, created = await _get_or_create_resource(
            db,
            resource_type="machine",
            name=machine_name,
        )
        resources_created += int(created)
    if tool_name := _first_value(mentions, "tool"):
        tool_resource, created = await _get_or_create_resource(
            db,
            resource_type="tool",
            name=tool_name,
        )
        resources_created += int(created)
    if fixture_name := _first_value(mentions, "fixture"):
        fixture_resource, created = await _get_or_create_resource(
            db,
            resource_type="fixture",
            name=fixture_name,
        )
        resources_created += int(created)

    operations_created = 0
    for operation_payload in _draft_operations(
        mentions,
        machine_resource_id=machine_resource.id if machine_resource else None,
        tool_resource_id=tool_resource.id if tool_resource else None,
        fixture_resource_id=fixture_resource.id if fixture_resource else None,
    ):
        operation = ManufacturingOperation(process_plan_id=plan.id, **operation_payload)
        db.add(operation)
        await db.flush()
        await _link_operation_graph(db, plan, operation)
        operations_created += 1

    plan_node = await _get_or_create_process_plan_node(db, plan)
    document_node = await _get_document_node(db, payload.document_id)
    if document_node:
        await _create_edge(
            db,
            source_id=plan_node.id,
            target_id=document_node.id,
            edge_type="derived_from",
            reason="Draft process plan was created from document memory",
            source_document_id=payload.document_id,
        )

    await log_action(
        db,
        action="tech.process_plan_draft_from_document",
        entity_type="manufacturing_process_plan",
        entity_id=plan.id,
        details={
            "document_id": str(payload.document_id),
            "operations_created": operations_created,
            "resources_created": resources_created,
        },
    )
    await db.commit()

    detail = await _load_process_plan_detail(db, plan.id)
    return ProcessPlanDraftFromDocumentResponse(
        process_plan=detail,
        resources_created=resources_created,
        operations_created=operations_created,
        source_mentions=mentions,
    )


@router.get("/process-plans/{plan_id}", response_model=ProcessPlanDetail)
async def get_process_plan(
    plan_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Skill: tech.process_plan_get — Get process plan with operations and norms."""
    plan = await _load_process_plan_detail(db, plan_id)
    if not plan:
        raise HTTPException(404, "Process plan not found")
    return plan


@router.post("/process-plans/{plan_id}/approve", response_model=ProcessPlanOut)
async def approve_process_plan(
    plan_id: uuid.UUID,
    payload: ProcessPlanApproveRequest,
    db: AsyncSession = Depends(get_db),
):
    """Skill: tech.process_plan_approve — Approve process plan after review."""
    plan = await db.get(ManufacturingProcessPlan, plan_id)
    if not plan:
        raise HTTPException(404, "Process plan not found")
    if plan.status == "approved":
        return plan

    plan.status = "approved"
    plan.approved_by = payload.approved_by
    plan.approved_at = datetime.now(UTC)
    metadata = dict(plan.metadata_ or {})
    if payload.comment:
        metadata["approval_comment"] = payload.comment
    plan.metadata_ = metadata

    plan_node = await _get_or_create_process_plan_node(db, plan)
    plan_node.metadata_ = {
        **(plan_node.metadata_ or {}),
        "status": "approved",
        "approved_by": payload.approved_by,
    }
    await log_action(
        db,
        action="tech.process_plan_approve",
        entity_type="manufacturing_process_plan",
        entity_id=plan.id,
        details={"approved_by": payload.approved_by},
    )
    await db.commit()
    await db.refresh(plan)
    return plan


@router.post("/process-plans/{plan_id}/validate", response_model=TechnologyCheckResponse)
async def validate_process_plan(
    plan_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Skill: tech.process_plan_validate — Validate manufacturability and completeness."""
    plan = await _load_process_plan_detail(db, plan_id)
    if not plan:
        raise HTTPException(404, "Process plan not found")

    previous = await db.execute(
        select(ManufacturingCheckResult).where(
            ManufacturingCheckResult.process_plan_id == plan_id,
            ManufacturingCheckResult.status == "open",
        )
    )
    for check in previous.scalars().all():
        check.status = "superseded"

    checks = _build_process_plan_checks(plan)
    for check in checks:
        db.add(ManufacturingCheckResult(process_plan_id=plan_id, **check))
    await db.flush()

    result = await db.execute(
        select(ManufacturingCheckResult)
        .where(
            ManufacturingCheckResult.process_plan_id == plan_id,
            ManufacturingCheckResult.status == "open",
        )
        .order_by(ManufacturingCheckResult.severity, ManufacturingCheckResult.created_at)
    )
    saved_checks = list(result.scalars().all())
    await log_action(
        db,
        action="tech.process_plan_validate",
        entity_type="manufacturing_process_plan",
        entity_id=plan_id,
        details={"checks": len(saved_checks)},
    )
    await db.commit()
    return _technology_check_response(plan_id, saved_checks)


@router.post("/process-plans/{plan_id}/estimate-norms", response_model=ProcessPlanEstimateNormsResponse)
async def estimate_process_plan_norms(
    plan_id: uuid.UUID,
    payload: ProcessPlanEstimateNormsRequest,
    db: AsyncSession = Depends(get_db),
):
    """Skill: tech.norm_estimate_suggest — Suggest operation time and cutting parameters."""
    plan = await _load_process_plan_detail(db, plan_id)
    if not plan:
        raise HTTPException(404, "Process plan not found")

    existing_by_operation = {
        estimate.operation_id
        for estimate in plan.norm_estimates
        if estimate.operation_id is not None
    }
    estimates: list[ManufacturingNormEstimate] = []
    skipped_existing = 0
    for operation in plan.operations:
        if operation.id in existing_by_operation and not payload.overwrite_existing:
            skipped_existing += 1
            continue
        suggestion = _estimate_operation_norm(operation, batch_size=payload.batch_size)
        if operation.cutting_parameters is None and suggestion["cutting_parameters"]:
            operation.cutting_parameters = suggestion["cutting_parameters"]
        estimate = ManufacturingNormEstimate(
            process_plan_id=plan_id,
            operation_id=operation.id,
            setup_minutes=suggestion["setup_minutes"],
            machine_minutes=suggestion["machine_minutes"],
            labor_minutes=suggestion["labor_minutes"],
            batch_size=payload.batch_size,
            confidence=suggestion["confidence"],
            method="deterministic_technology_heuristic",
            assumptions=suggestion["assumptions"],
            created_by=payload.created_by,
            metadata_={"status": "proposed", "operation_type": operation.operation_type},
        )
        db.add(estimate)
        await db.flush()
        estimates.append(estimate)

    await log_action(
        db,
        action="tech.norm_estimate_suggest",
        entity_type="manufacturing_process_plan",
        entity_id=plan_id,
        details={"created": len(estimates), "skipped_existing": skipped_existing},
    )
    await db.commit()
    for estimate in estimates:
        await db.refresh(estimate)
    return ProcessPlanEstimateNormsResponse(
        process_plan_id=plan_id,
        estimates=estimates,
        created=len(estimates),
        skipped_existing=skipped_existing,
    )


@router.post("/process-plans/{plan_id}/operations", response_model=OperationOut, status_code=201)
async def add_operation(
    plan_id: uuid.UUID,
    payload: OperationCreate,
    db: AsyncSession = Depends(get_db),
):
    """Skill: tech.operation_add — Add a process operation and graph links."""
    plan = await db.get(ManufacturingProcessPlan, plan_id)
    if not plan:
        raise HTTPException(404, "Process plan not found")
    await _validate_resource_refs(db, payload)

    operation = ManufacturingOperation(
        process_plan_id=plan_id,
        **payload.model_dump(by_alias=False),
    )
    db.add(operation)
    await db.flush()
    await _link_operation_graph(db, plan, operation)
    await log_action(
        db,
        action="tech.operation_add",
        entity_type="manufacturing_operation",
        entity_id=operation.id,
        details={"process_plan_id": str(plan_id), "sequence_no": operation.sequence_no},
    )
    await db.commit()
    await db.refresh(operation)
    return operation


@router.post("/process-plans/{plan_id}/norm-estimates", response_model=NormEstimateOut, status_code=201)
async def create_norm_estimate(
    plan_id: uuid.UUID,
    payload: NormEstimateCreate,
    db: AsyncSession = Depends(get_db),
):
    """Skill: tech.norm_estimate_create — Create labor and machine time estimate."""
    if not await db.get(ManufacturingProcessPlan, plan_id):
        raise HTTPException(404, "Process plan not found")
    if payload.operation_id:
        operation = await db.get(ManufacturingOperation, payload.operation_id)
        if not operation or operation.process_plan_id != plan_id:
            raise HTTPException(404, "Operation not found in process plan")

    estimate = ManufacturingNormEstimate(
        process_plan_id=plan_id,
        **payload.model_dump(by_alias=False),
    )
    db.add(estimate)
    await db.flush()
    await log_action(
        db,
        action="tech.norm_estimate_create",
        entity_type="manufacturing_norm_estimate",
        entity_id=estimate.id,
        details={"process_plan_id": str(plan_id), "confidence": estimate.confidence},
    )
    await db.commit()
    await db.refresh(estimate)
    return estimate


@router.post("/norm-estimates/{estimate_id}/approve", response_model=NormEstimateOut)
async def approve_norm_estimate(
    estimate_id: uuid.UUID,
    payload: NormEstimateApproveRequest,
    db: AsyncSession = Depends(get_db),
):
    """Skill: tech.norm_estimate_approve — Approve labor and machine time estimate."""
    estimate = await db.get(ManufacturingNormEstimate, estimate_id)
    if not estimate:
        raise HTTPException(404, "Norm estimate not found")
    metadata = dict(estimate.metadata_ or {})
    metadata["status"] = "approved"
    metadata["approved_by"] = payload.approved_by
    metadata["approved_at"] = datetime.now(UTC).isoformat()
    if payload.comment:
        metadata["approval_comment"] = payload.comment
    estimate.metadata_ = metadata
    await log_action(
        db,
        action="tech.norm_estimate_approve",
        entity_type="manufacturing_norm_estimate",
        entity_id=estimate.id,
        details={"approved_by": payload.approved_by},
    )
    await db.commit()
    await db.refresh(estimate)
    return estimate


@router.post("/corrections", response_model=TechnologyCorrectionOut, status_code=201)
async def record_correction(
    payload: TechnologyCorrectionCreate,
    db: AsyncSession = Depends(get_db),
):
    """Skill: tech.correction_record — Record a human correction for learning."""
    await _validate_correction_refs(db, payload)
    correction = TechnologyCorrection(**payload.model_dump(by_alias=False))
    db.add(correction)
    await db.flush()
    await log_action(
        db,
        action="tech.correction_record",
        entity_type="technology_correction",
        entity_id=correction.id,
        details={
            "target_entity_type": correction.entity_type,
            "field_name": correction.field_name,
        },
    )
    await db.commit()
    await db.refresh(correction)
    return correction


@router.get("/learning-suggestions", response_model=LearningSuggestionResponse)
async def list_learning_suggestions(
    min_occurrences: int = Query(3, ge=2, le=20),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    """Skill: tech.learning_suggest — Suggest rules from repeated corrections."""
    result = await db.execute(
        select(
            TechnologyCorrection.entity_type,
            TechnologyCorrection.field_name,
            TechnologyCorrection.old_value,
            TechnologyCorrection.new_value,
            func.count(TechnologyCorrection.id).label("occurrences"),
        )
        .group_by(
            TechnologyCorrection.entity_type,
            TechnologyCorrection.field_name,
            TechnologyCorrection.old_value,
            TechnologyCorrection.new_value,
        )
        .having(func.count(TechnologyCorrection.id) >= min_occurrences)
        .order_by(func.count(TechnologyCorrection.id).desc())
        .limit(limit)
    )
    suggestions = [
        _learning_suggestion(
            entity_type=row.entity_type,
            field_name=row.field_name,
            old_value=row.old_value,
            new_value=row.new_value,
            occurrences=row.occurrences,
            min_occurrences=min_occurrences,
        )
        for row in result.all()
    ]
    return LearningSuggestionResponse(suggestions=suggestions, total=len(suggestions))


@router.get("/learning-rules", response_model=LearningRuleListResponse)
async def list_learning_rules(
    status: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    """Skill: tech.learning_rule_list — List proposed and active learning rules."""
    query = select(TechnologyLearningRule)
    count_query = select(func.count()).select_from(TechnologyLearningRule)
    if status:
        query = query.where(TechnologyLearningRule.status == status)
        count_query = count_query.where(TechnologyLearningRule.status == status)
    total = (await db.execute(count_query)).scalar() or 0
    result = await db.execute(
        query.order_by(TechnologyLearningRule.created_at.desc()).offset(offset).limit(limit)
    )
    return LearningRuleListResponse(items=list(result.scalars().all()), total=total)


@router.post("/learning-rules", response_model=LearningRuleOut, status_code=201)
async def create_learning_rule(
    payload: LearningRuleCreate,
    db: AsyncSession = Depends(get_db),
):
    """Skill: tech.learning_rule_create — Save a proposed learning rule."""
    rule = TechnologyLearningRule(**payload.model_dump(by_alias=False))
    db.add(rule)
    await db.flush()
    await log_action(
        db,
        action="tech.learning_rule_create",
        entity_type="technology_learning_rule",
        entity_id=rule.id,
        details={"entity_type": rule.entity_type, "field_name": rule.field_name},
    )
    await db.commit()
    await db.refresh(rule)
    return rule


@router.post("/learning-rules/{rule_id}/activate", response_model=LearningRuleOut)
async def activate_learning_rule(
    rule_id: uuid.UUID,
    payload: LearningRuleActivateRequest,
    db: AsyncSession = Depends(get_db),
):
    """Skill: tech.learning_rule_activate — Activate a proposed learning rule."""
    rule = await db.get(TechnologyLearningRule, rule_id)
    if not rule:
        raise HTTPException(404, "Learning rule not found")
    if rule.status == "active":
        return rule
    rule.status = "active"
    rule.activated_by = payload.activated_by
    rule.activated_at = datetime.now(UTC)
    metadata = dict(rule.metadata_ or {})
    if payload.comment:
        metadata["activation_comment"] = payload.comment
    rule.metadata_ = metadata
    await log_action(
        db,
        action="tech.learning_rule_activate",
        entity_type="technology_learning_rule",
        entity_id=rule.id,
        details={"activated_by": payload.activated_by},
    )
    await db.commit()
    await db.refresh(rule)
    return rule


async def _validate_resource_refs(db: AsyncSession, payload: OperationCreate) -> None:
    for resource_id in (
        payload.machine_resource_id,
        payload.tool_resource_id,
        payload.fixture_resource_id,
    ):
        if resource_id and not await db.get(ManufacturingResource, resource_id):
            raise HTTPException(404, "Manufacturing resource not found")


async def _validate_correction_refs(
    db: AsyncSession,
    payload: TechnologyCorrectionCreate,
) -> None:
    if payload.source_document_id and not await db.get(Document, payload.source_document_id):
        raise HTTPException(404, "Source document not found")
    if payload.process_plan_id and not await db.get(
        ManufacturingProcessPlan, payload.process_plan_id
    ):
        raise HTTPException(404, "Process plan not found")
    if payload.operation_id and not await db.get(ManufacturingOperation, payload.operation_id):
        raise HTTPException(404, "Operation not found")


async def _load_process_plan_detail(
    db: AsyncSession,
    plan_id: uuid.UUID,
) -> ManufacturingProcessPlan | None:
    result = await db.execute(
        select(ManufacturingProcessPlan)
        .where(ManufacturingProcessPlan.id == plan_id)
        .options(
            selectinload(ManufacturingProcessPlan.operations),
            selectinload(ManufacturingProcessPlan.norm_estimates),
        )
    )
    return result.scalar_one_or_none()


async def _mentions_by_type(db: AsyncSession, document_id: uuid.UUID) -> dict[str, list[str]]:
    result = await db.execute(
        select(EntityMention)
        .where(EntityMention.document_id == document_id)
        .order_by(EntityMention.entity_type, EntityMention.start_offset)
    )
    values: dict[str, list[str]] = {}
    seen: set[tuple[str, str]] = set()
    for mention in result.scalars().all():
        key = (mention.entity_type, mention.mention_text.lower())
        if key in seen:
            continue
        seen.add(key)
        values.setdefault(mention.entity_type, []).append(mention.mention_text)
    return values


async def _get_or_create_resource(
    db: AsyncSession,
    *,
    resource_type: str,
    name: str,
) -> tuple[ManufacturingResource, bool]:
    result = await db.execute(
        select(ManufacturingResource).where(
            ManufacturingResource.resource_type == resource_type,
            ManufacturingResource.name == name,
        )
    )
    resource = result.scalar_one_or_none()
    if resource:
        return resource, False
    resource = ManufacturingResource(
        resource_type=resource_type,
        name=name,
        status="active",
        metadata_={"source": "draft_from_document"},
    )
    db.add(resource)
    await db.flush()
    await _get_or_create_resource_node(db, resource)
    return resource, True


def _draft_operations(
    mentions: dict[str, list[str]],
    *,
    machine_resource_id: uuid.UUID | None,
    tool_resource_id: uuid.UUID | None,
    fixture_resource_id: uuid.UUID | None,
) -> list[dict]:
    operations: list[dict] = []
    if mentions.get("material"):
        operations.append(
            {
                "sequence_no": 10,
                "operation_code": "010",
                "name": "Заготовительная",
                "operation_type": "blank_preparation",
                "transition_text": "Подготовить заготовку по материалу документа.",
                "control_requirements": "Проверить марку материала и состояние заготовки.",
            }
        )

    machine_name = _first_value(mentions, "machine") or ""
    tool_name = _first_value(mentions, "tool") or ""
    operation_type = _operation_type(machine_name, tool_name)
    operations.append(
        {
            "sequence_no": 20,
            "operation_code": "020",
            "name": _operation_name(operation_type),
            "operation_type": operation_type,
            "machine_resource_id": machine_resource_id,
            "tool_resource_id": tool_resource_id,
            "fixture_resource_id": fixture_resource_id,
            "transition_text": "Выполнить основную механическую обработку по чертежу.",
            "control_requirements": "Контролировать размеры и шероховатость после обработки.",
            "metadata_": {"source": "draft_from_document"},
        }
    )

    if mentions.get("standard"):
        operations.append(
            {
                "sequence_no": 90,
                "operation_code": "090",
                "name": "Контрольная",
                "operation_type": "quality_control",
                "transition_text": "Проверить изделие по указанным стандартам и требованиям.",
                "control_requirements": "; ".join(mentions["standard"]),
                "metadata_": {"source": "draft_from_document"},
            }
        )
    return operations


def _build_process_plan_checks(plan: ManufacturingProcessPlan) -> list[dict]:
    checks: list[dict] = []
    if not plan.material:
        checks.append(
            _check(
                "missing_material",
                "critical",
                "В техпроцессе не указан материал.",
                "Укажите марку материала и стандарт/ТУ перед утверждением.",
            )
        )
    if not plan.operations:
        checks.append(
            _check(
                "missing_operations",
                "critical",
                "В техпроцессе нет операций.",
                "Добавьте маршрутные операции по ЕСТД.",
            )
        )
        return checks

    operation_types = {operation.operation_type for operation in plan.operations}
    if "quality_control" not in operation_types:
        checks.append(
            _check(
                "missing_quality_control",
                "warning",
                "В маршруте нет контрольной операции.",
                "Добавьте контроль размеров, шероховатости и требований чертежа.",
            )
        )

    for operation in plan.operations:
        if operation.operation_type in {"turning", "milling", "drilling", "grinding", "machining"}:
            if not operation.machine_resource_id:
                checks.append(
                    _check(
                        "missing_machine",
                        "critical",
                        f"Операция {operation.sequence_no} не содержит станок.",
                        "Назначьте подходящее оборудование.",
                        operation_id=operation.id,
                    )
                )
            if not operation.tool_resource_id:
                checks.append(
                    _check(
                        "missing_tool",
                        "warning",
                        f"Операция {operation.sequence_no} не содержит режущий инструмент.",
                        "Назначьте инструмент или укажите причину отсутствия.",
                        operation_id=operation.id,
                    )
                )
            if not operation.control_requirements:
                checks.append(
                    _check(
                        "missing_operation_control",
                        "warning",
                        f"Операция {operation.sequence_no} не содержит контрольные требования.",
                        "Укажите контролируемые размеры, допуски или стандарт контроля.",
                        operation_id=operation.id,
                    )
                )

    operation_ids_with_norms = {
        estimate.operation_id for estimate in plan.norm_estimates if estimate.operation_id
    }
    for operation in plan.operations:
        if operation.id not in operation_ids_with_norms and not (
            operation.machine_minutes or operation.labor_minutes
        ):
            checks.append(
                _check(
                    "missing_norm",
                    "warning",
                    f"Для операции {operation.sequence_no} нет нормы времени.",
                    "Добавьте норму времени или расчетное допущение.",
                    operation_id=operation.id,
                )
            )
    return checks


def _check(
    check_code: str,
    severity: str,
    message: str,
    recommendation: str,
    *,
    operation_id: uuid.UUID | None = None,
) -> dict:
    return {
        "operation_id": operation_id,
        "check_code": check_code,
        "severity": severity,
        "status": "open",
        "message": message,
        "recommendation": recommendation,
        "created_by": "system",
        "metadata_": {"method": "deterministic_technology_checks"},
    }


def _technology_check_response(
    plan_id: uuid.UUID,
    checks: list[ManufacturingCheckResult],
) -> TechnologyCheckResponse:
    return TechnologyCheckResponse(
        process_plan_id=plan_id,
        checks=checks,
        total=len(checks),
        critical=sum(1 for check in checks if check.severity == "critical"),
        warnings=sum(1 for check in checks if check.severity == "warning"),
    )


def _learning_suggestion(
    *,
    entity_type: str,
    field_name: str,
    old_value: str | None,
    new_value: str | None,
    occurrences: int,
    min_occurrences: int,
) -> LearningSuggestionOut:
    confidence = min(0.95, 0.55 + (occurrences - min_occurrences + 1) * 0.1)
    return LearningSuggestionOut(
        suggestion_type="normalization_rule",
        entity_type=entity_type,
        field_name=field_name,
        old_value=old_value,
        new_value=new_value,
        occurrences=occurrences,
        confidence=round(confidence, 2),
        recommendation=(
            f"Повторяющаяся правка поля {field_name}: "
            f"{old_value or '<empty>'} -> {new_value or '<empty>'}. "
            "Проверьте и добавьте правило нормализации/шаблон."
        ),
    )


def _estimate_operation_norm(
    operation: ManufacturingOperation,
    *,
    batch_size: float,
) -> dict:
    operation_type = operation.operation_type or "machining"
    base = {
        "blank_preparation": (8.0, 6.0, 12.0),
        "turning": (15.0, 22.0, 28.0),
        "milling": (18.0, 26.0, 32.0),
        "drilling": (10.0, 14.0, 18.0),
        "grinding": (20.0, 30.0, 36.0),
        "quality_control": (5.0, 0.0, 12.0),
        "machining": (15.0, 24.0, 30.0),
    }.get(operation_type, (12.0, 18.0, 24.0))
    setup_minutes, machine_minutes, labor_minutes = base
    batch_factor = max(1.0, batch_size)
    per_part_setup = setup_minutes / batch_factor
    confidence = 0.55
    cutting_parameters = _cutting_parameters_for(operation_type)
    assumptions = [
        "Предварительная эвристика для чернового планирования.",
        "Требует проверки технологом и утверждения нормы.",
        f"Партия: {batch_size:g}.",
    ]
    return {
        "setup_minutes": round(per_part_setup, 2),
        "machine_minutes": machine_minutes,
        "labor_minutes": labor_minutes,
        "confidence": confidence,
        "cutting_parameters": cutting_parameters,
        "assumptions": assumptions,
    }


def _cutting_parameters_for(operation_type: str) -> dict | None:
    if operation_type == "turning":
        return {"vc_m_min": 90, "feed_mm_rev": 0.25, "depth_mm": 1.5}
    if operation_type == "milling":
        return {"vc_m_min": 120, "feed_mm_tooth": 0.08, "depth_mm": 1.0}
    if operation_type == "drilling":
        return {"vc_m_min": 45, "feed_mm_rev": 0.18}
    if operation_type == "grinding":
        return {"wheel_speed_m_s": 30, "feed_mm_pass": 0.02}
    return None


def _operation_type(machine_name: str, tool_name: str) -> str:
    text = f"{machine_name} {tool_name}".lower()
    if "токар" in text or "резец" in text:
        return "turning"
    if "фрез" in text:
        return "milling"
    if "сверл" in text:
        return "drilling"
    if "шлиф" in text:
        return "grinding"
    return "machining"


def _operation_name(operation_type: str) -> str:
    names = {
        "turning": "Токарная",
        "milling": "Фрезерная",
        "drilling": "Сверлильная",
        "grinding": "Шлифовальная",
        "machining": "Механическая обработка",
    }
    return names.get(operation_type, "Механическая обработка")


def _first_value(values: dict[str, list[str]], key: str) -> str | None:
    items = values.get(key) or []
    return items[0] if items else None


def _product_name_from_document(document: Document) -> str:
    return document.file_name.rsplit(".", 1)[0] or document.file_name


def _route_summary(mentions: dict[str, list[str]]) -> str:
    facts = []
    if material := _first_value(mentions, "material"):
        facts.append(f"материал: {material}")
    if machine := _first_value(mentions, "machine"):
        facts.append(f"оборудование: {machine}")
    if tool := _first_value(mentions, "tool"):
        facts.append(f"инструмент: {tool}")
    return "Черновой маршрут по памяти документа" + (f" ({'; '.join(facts)})" if facts else "")


def _quality_requirements(standards: list[str]) -> str | None:
    if not standards:
        return None
    return "Контроль с учетом стандартов: " + "; ".join(standards)


async def _link_operation_graph(
    db: AsyncSession,
    plan: ManufacturingProcessPlan,
    operation: ManufacturingOperation,
) -> None:
    plan_node = await _get_or_create_process_plan_node(db, plan)
    operation_node = await _get_or_create_operation_node(db, operation)
    await _create_edge(
        db,
        source_id=plan_node.id,
        target_id=operation_node.id,
        edge_type="contains",
        reason="Process plan contains manufacturing operation",
        source_document_id=plan.document_id,
    )

    for resource_id, edge_type in (
        (operation.machine_resource_id, "uses_machine"),
        (operation.tool_resource_id, "uses_tool"),
        (operation.fixture_resource_id, "uses_fixture"),
    ):
        if not resource_id:
            continue
        resource = await db.get(ManufacturingResource, resource_id)
        if not resource:
            continue
        resource_node = await _get_or_create_resource_node(db, resource)
        await _create_edge(
            db,
            source_id=operation_node.id,
            target_id=resource_node.id,
            edge_type=edge_type,
            reason=f"Operation uses {resource.resource_type}",
            source_document_id=plan.document_id,
        )


async def _get_or_create_process_plan_node(
    db: AsyncSession,
    plan: ManufacturingProcessPlan,
) -> KnowledgeNode:
    canonical_key = f"process_plan:{plan.id}"
    result = await db.execute(select(KnowledgeNode).where(KnowledgeNode.canonical_key == canonical_key))
    node = result.scalar_one_or_none()
    if node:
        return node
    node = KnowledgeNode(
        node_type="process_plan",
        title=plan.product_name,
        canonical_key=canonical_key,
        entity_type="manufacturing_process_plan",
        entity_id=plan.id,
        summary=plan.route_summary,
        confidence=0.9,
        created_by=plan.created_by,
        metadata_={"status": plan.status, "standard_system": plan.standard_system},
    )
    db.add(node)
    await db.flush()
    return node


async def _get_or_create_operation_node(
    db: AsyncSession,
    operation: ManufacturingOperation,
) -> KnowledgeNode:
    canonical_key = f"operation:{operation.id}"
    result = await db.execute(select(KnowledgeNode).where(KnowledgeNode.canonical_key == canonical_key))
    node = result.scalar_one_or_none()
    if node:
        return node
    node = KnowledgeNode(
        node_type="operation",
        title=f"{operation.sequence_no}. {operation.name}",
        canonical_key=canonical_key,
        entity_type="manufacturing_operation",
        entity_id=operation.id,
        summary=operation.transition_text,
        confidence=0.9,
        created_by="sveta",
        metadata_={"operation_type": operation.operation_type},
    )
    db.add(node)
    await db.flush()
    return node


async def _get_or_create_resource_node(
    db: AsyncSession,
    resource: ManufacturingResource,
) -> KnowledgeNode:
    canonical_key = f"{resource.resource_type}:{resource.id}"
    result = await db.execute(select(KnowledgeNode).where(KnowledgeNode.canonical_key == canonical_key))
    node = result.scalar_one_or_none()
    if node:
        return node
    node = KnowledgeNode(
        node_type=resource.resource_type,
        title=resource.name,
        canonical_key=canonical_key,
        entity_type="manufacturing_resource",
        entity_id=resource.id,
        summary=resource.notes,
        confidence=0.9,
        created_by="sveta",
        metadata_={"code": resource.code, "model": resource.model, "standard": resource.standard},
    )
    db.add(node)
    await db.flush()
    return node


async def _get_document_node(db: AsyncSession, document_id: uuid.UUID) -> KnowledgeNode | None:
    result = await db.execute(
        select(KnowledgeNode).where(
            KnowledgeNode.entity_type == "document",
            KnowledgeNode.entity_id == document_id,
        )
    )
    return result.scalar_one_or_none()


async def _create_edge(
    db: AsyncSession,
    *,
    source_id: uuid.UUID,
    target_id: uuid.UUID,
    edge_type: str,
    reason: str,
    source_document_id: uuid.UUID | None = None,
) -> KnowledgeEdge:
    result = await db.execute(
        select(KnowledgeEdge).where(
            KnowledgeEdge.source_node_id == source_id,
            KnowledgeEdge.target_node_id == target_id,
            KnowledgeEdge.edge_type == edge_type,
        )
    )
    edge = result.scalar_one_or_none()
    if edge:
        return edge
    edge = KnowledgeEdge(
        source_node_id=source_id,
        target_node_id=target_id,
        edge_type=edge_type,
        confidence=0.9,
        reason=reason,
        source_document_id=source_document_id,
        created_by="sveta",
        metadata_={"source": "technology_api"},
    )
    db.add(edge)
    await db.flush()
    return edge
