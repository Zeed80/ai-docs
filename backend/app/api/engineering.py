"""Canonical Engineering IR projects and revision-safe domain projections."""

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.audit.service import add_timeline_event, log_action
from app.auth.jwt import get_current_user
from app.auth.models import UserInfo, require_permission
from app.db.models import BOM, CadIrRevision, Drawing, EngineeringAnalysisCase, EngineeringAnalysisRun, EngineeringAssembly, EngineeringAssemblyComponent, EngineeringAssemblyMate, EngineeringChangeRequest, EngineeringMaterial, EngineeringMaterialAssignment, EngineeringProject, EngineeringProjection, EngineeringRevision, EngineeringValidationRun, ManufacturingCheckResult, ManufacturingProcessPlan
from app.db.session import get_db
from app.domain.engineering import (
    ChangeRequestCreate,
    ChangeRequestOut,
    ChangeRequestSign,
    EngineeringApprovalRequest,
    EngineeringAssemblyComponentCreate,
    EngineeringAssemblyComponentOut,
    EngineeringAssemblyCreate,
    EngineeringAssemblyMateCreate,
    EngineeringAssemblyMateOut,
    EngineeringAssemblyOut,
    EngineeringAssemblyValidation,
    EngineeringAnalysisCaseCreate,
    EngineeringAnalysisCaseOut,
    EngineeringAnalysisRunOut,
    EngineeringValidationRunOut,
    EngineeringMaterialAssignmentCreate,
    EngineeringMaterialAssignmentOut,
    EngineeringMaterialCreate,
    EngineeringMaterialOut,
    EngineeringProjectCreate,
    EngineeringProjectDetail,
    EngineeringProjectOut,
    EngineeringProjectionCreate,
    EngineeringProjectionOut,
    EngineeringRevisionCreate,
    EngineeringRevisionOut,
)

router = APIRouter()

_PROJECTABLE_MODELS = {
    "drawing": Drawing,
    "bom": BOM,
    "manufacturing_process_plan": ManufacturingProcessPlan,
    "cad_ir_revision": CadIrRevision,
}


def _blocking_errors(validation: dict) -> bool:
    """Accept both the CAD IR report and the new validation-run representation."""
    issues = validation.get("issues", []) if isinstance(validation, dict) else []
    return any(isinstance(item, dict) and item.get("severity") == "error" for item in issues)


@router.post("/projects", response_model=EngineeringProjectOut, status_code=status.HTTP_201_CREATED)
async def create_project(
    body: EngineeringProjectCreate,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> EngineeringProject:
    require_permission(user, "engineering.project_create")
    project = EngineeringProject(
        name=body.name,
        code=body.code,
        project_id=body.project_id,
        description=body.description,
        metadata_=body.metadata_,
    )
    db.add(project)
    await db.flush()
    await log_action(db, action="engineering.project.create", entity_type="engineering_project", entity_id=project.id)
    await db.commit()
    await db.refresh(project)
    return project


@router.get("/projects", response_model=list[EngineeringProjectOut])
async def list_projects(db: AsyncSession = Depends(get_db)) -> list[EngineeringProject]:
    result = await db.execute(select(EngineeringProject).order_by(EngineeringProject.updated_at.desc()))
    return list(result.scalars())


@router.get("/materials", response_model=list[EngineeringMaterialOut])
async def list_materials(db: AsyncSession = Depends(get_db)) -> list[EngineeringMaterial]:
    return list((await db.execute(select(EngineeringMaterial).order_by(EngineeringMaterial.designation))).scalars())


@router.post("/materials", response_model=EngineeringMaterialOut, status_code=status.HTTP_201_CREATED)
async def create_material(body: EngineeringMaterialCreate, db: AsyncSession = Depends(get_db)) -> EngineeringMaterial:
    material = EngineeringMaterial(**body.model_dump())
    db.add(material)
    await db.commit()
    await db.refresh(material)
    return material


@router.get("/projects/{project_id}", response_model=EngineeringProjectDetail)
async def get_project(project_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> EngineeringProject:
    result = await db.execute(
        select(EngineeringProject).where(EngineeringProject.id == project_id).options(selectinload(EngineeringProject.revisions))
    )
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(404, "Инженерный проект не найден")
    return project


@router.post("/projects/{project_id}/revisions", response_model=EngineeringRevisionOut, status_code=status.HTTP_201_CREATED)
async def create_revision(
    project_id: uuid.UUID,
    body: EngineeringRevisionCreate,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> EngineeringRevision:
    require_permission(user, "engineering.revision_create")
    project = await db.get(EngineeringProject, project_id)
    if not project:
        raise HTTPException(404, "Инженерный проект не найден")
    await db.execute(select(EngineeringProject.id).where(EngineeringProject.id == project_id).with_for_update())
    latest = (await db.execute(
        select(EngineeringRevision).where(EngineeringRevision.engineering_project_id == project_id)
        .order_by(EngineeringRevision.revision.desc()).limit(1)
    )).scalar_one_or_none()
    latest_number = latest.revision if latest else None
    if body.base_revision != latest_number:
        raise HTTPException(409, "Ревизия устарела: обновите проект перед сохранением")
    revision = EngineeringRevision(
        engineering_project_id=project_id,
        revision=0 if latest_number is None else latest_number + 1,
        base_revision=body.base_revision,
        payload=body.payload,
        validation=body.validation,
        origin=body.origin,
        change_summary=body.change_summary,
        created_by=body.created_by,
        status="needs_review" if _blocking_errors(body.validation) else "validated",
    )
    db.add(revision)
    # A fresh canonical revision invalidates all projections from the previous
    # source revision. Their underlying business records remain readable but
    # cannot be mistaken for current engineering output.
    if latest:
        stale = (await db.execute(
            select(EngineeringProjection).join(EngineeringRevision).where(
                EngineeringRevision.engineering_project_id == project_id,
                EngineeringProjection.state == "current",
            )
        )).scalars()
        for projection in stale:
            projection.state = "stale"
    project.status = "needs_review" if revision.status == "needs_review" else "validated"
    await db.flush()
    await log_action(db, action="engineering.revision.create", entity_type="engineering_revision", entity_id=revision.id, user_id=body.created_by)
    await db.commit()
    await db.refresh(revision)
    return revision


@router.post("/revisions/{revision_id}/projections", response_model=EngineeringProjectionOut, status_code=status.HTTP_201_CREATED)
async def create_projection(
    revision_id: uuid.UUID, body: EngineeringProjectionCreate, db: AsyncSession = Depends(get_db)
) -> EngineeringProjection:
    revision = await db.get(EngineeringRevision, revision_id)
    if not revision:
        raise HTTPException(404, "Инженерная ревизия не найдена")
    if revision.status == "approved":
        raise HTTPException(400, "Нельзя изменять проекции утвержденной ревизии")
    target_model = _PROJECTABLE_MODELS.get(body.entity_type)
    if target_model is None:
        raise HTTPException(400, "Поддерживаются проекции drawing, cad_ir_revision, bom и manufacturing_process_plan")
    target = await db.get(target_model, body.entity_id)
    if target is None:
        raise HTTPException(404, "Объект проекции не найден")
    projection = EngineeringProjection(
        engineering_revision_id=revision_id,
        projection_type=body.projection_type,
        entity_type=body.entity_type,
        entity_id=body.entity_id,
        metadata_=body.metadata_,
    )
    db.add(projection)
    # Operational records expose a direct convenience FK. A CAD IR snapshot
    # intentionally stays immutable and is linked only through this projection.
    if hasattr(target, "engineering_revision_id"):
        target.engineering_revision_id = revision.id
    await db.flush()
    await db.commit()
    await db.refresh(projection)
    return projection


@router.get("/revisions/{revision_id}/projections", response_model=list[EngineeringProjectionOut])
async def list_projections(revision_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> list[EngineeringProjection]:
    result = await db.execute(
        select(EngineeringProjection)
        .where(EngineeringProjection.engineering_revision_id == revision_id)
        .order_by(EngineeringProjection.created_at)
    )
    return list(result.scalars())


@router.get("/revisions/{revision_id}/materials", response_model=list[EngineeringMaterialAssignmentOut])
async def list_material_assignments(revision_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> list[EngineeringMaterialAssignment]:
    result = await db.execute(
        select(EngineeringMaterialAssignment)
        .where(EngineeringMaterialAssignment.engineering_revision_id == revision_id)
        .options(selectinload(EngineeringMaterialAssignment.material))
    )
    return list(result.scalars())


@router.post("/revisions/{revision_id}/materials", response_model=EngineeringMaterialAssignmentOut, status_code=status.HTTP_201_CREATED)
async def assign_material(
    revision_id: uuid.UUID, body: EngineeringMaterialAssignmentCreate, db: AsyncSession = Depends(get_db)
) -> EngineeringMaterialAssignment:
    revision = await db.get(EngineeringRevision, revision_id)
    if not revision:
        raise HTTPException(404, "Инженерная ревизия не найдена")
    if revision.status == "approved":
        raise HTTPException(400, "Нельзя изменять материал утвержденной ревизии")
    if not await db.get(EngineeringMaterial, body.material_id):
        raise HTTPException(404, "Материал не найден")
    assignment = EngineeringMaterialAssignment(engineering_revision_id=revision_id, **body.model_dump())
    db.add(assignment)
    await db.commit()
    result = await db.execute(
        select(EngineeringMaterialAssignment).where(EngineeringMaterialAssignment.id == assignment.id)
        .options(selectinload(EngineeringMaterialAssignment.material))
    )
    return result.scalar_one()


async def _editable_revision(db: AsyncSession, revision_id: uuid.UUID) -> EngineeringRevision:
    revision = await db.get(EngineeringRevision, revision_id)
    if not revision:
        raise HTTPException(404, "Инженерная ревизия не найдена")
    if revision.status == "approved":
        raise HTTPException(400, "Нельзя изменять утвержденную ревизию")
    return revision


@router.get("/revisions/{revision_id}/assemblies", response_model=list[EngineeringAssemblyOut])
async def list_assemblies(revision_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> list[EngineeringAssembly]:
    return list((await db.execute(select(EngineeringAssembly).where(EngineeringAssembly.engineering_revision_id == revision_id))).scalars())


@router.post("/revisions/{revision_id}/assemblies", response_model=EngineeringAssemblyOut, status_code=status.HTTP_201_CREATED)
async def create_assembly(revision_id: uuid.UUID, body: EngineeringAssemblyCreate, db: AsyncSession = Depends(get_db)) -> EngineeringAssembly:
    await _editable_revision(db, revision_id)
    assembly = EngineeringAssembly(engineering_revision_id=revision_id, **body.model_dump())
    db.add(assembly)
    await db.commit()
    await db.refresh(assembly)
    return assembly


@router.post("/assemblies/{assembly_id}/components", response_model=EngineeringAssemblyComponentOut, status_code=status.HTTP_201_CREATED)
async def add_assembly_component(assembly_id: uuid.UUID, body: EngineeringAssemblyComponentCreate, db: AsyncSession = Depends(get_db)) -> EngineeringAssemblyComponent:
    assembly = await db.get(EngineeringAssembly, assembly_id)
    if not assembly:
        raise HTTPException(404, "Сборка не найдена")
    await _editable_revision(db, assembly.engineering_revision_id)
    component = EngineeringAssemblyComponent(engineering_assembly_id=assembly_id, **body.model_dump())
    db.add(component)
    await db.commit()
    await db.refresh(component)
    return component


@router.post("/assemblies/{assembly_id}/mates", response_model=EngineeringAssemblyMateOut, status_code=status.HTTP_201_CREATED)
async def add_assembly_mate(assembly_id: uuid.UUID, body: EngineeringAssemblyMateCreate, db: AsyncSession = Depends(get_db)) -> EngineeringAssemblyMate:
    assembly = await db.get(EngineeringAssembly, assembly_id)
    if not assembly:
        raise HTTPException(404, "Сборка не найдена")
    await _editable_revision(db, assembly.engineering_revision_id)
    keys = set((await db.execute(select(EngineeringAssemblyComponent.instance_key).where(EngineeringAssemblyComponent.engineering_assembly_id == assembly_id))).scalars())
    if body.first_instance_key not in keys or body.second_instance_key not in keys or body.first_instance_key == body.second_instance_key:
        raise HTTPException(422, "Сопряжение должно ссылаться на два разных экземпляра сборки")
    mate = EngineeringAssemblyMate(engineering_assembly_id=assembly_id, **body.model_dump())
    db.add(mate)
    await db.commit()
    await db.refresh(mate)
    return mate


def _overlap(a: dict, b: dict) -> bool:
    required = ("x_min", "x_max", "y_min", "y_max", "z_min", "z_max")
    if not all(key in a and key in b for key in required):
        return False
    return all(float(a[f"{axis}_min"]) < float(b[f"{axis}_max"]) and float(b[f"{axis}_min"]) < float(a[f"{axis}_max"]) for axis in ("x", "y", "z"))


async def _exact_interference(components: list[EngineeringAssemblyComponent]) -> tuple[list[dict], list[str], str | None]:
    """E5: exact B-Rep interference via the CAD kernel for components that
    declare an occupancy solid (metadata.shape: box|cylinder + transform).
    Returns (collisions, checked_instance_keys, degradation_note)."""
    import httpx

    from app.config import settings

    exact = [
        component for component in components
        if not component.suppressed and isinstance(component.metadata_.get("shape"), dict)
    ]
    if len(exact) < 2:
        return [], [], None
    payload = {
        "components": [
            {
                "key": component.instance_key,
                "shape": component.metadata_["shape"],
                "transform": component.transform or {},
            }
            for component in exact
        ],
    }
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(f"{settings.cad_kernel_url.rstrip('/')}/interference", json=payload)
        if response.status_code != 200:
            return [], [], f"kernel отклонил exact-проверку: {response.text[:200]}"
        return response.json().get("collisions", []), [component.instance_key for component in exact], None
    except httpx.HTTPError as exc:
        # The kernel being down must not block validation — degrade to AABB
        # loudly, never silently.
        return [], [], f"cad-kernel недоступен, точная проверка пропущена: {exc}"


@router.post("/assemblies/{assembly_id}/validate", response_model=EngineeringAssemblyValidation)
async def validate_assembly(assembly_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> EngineeringAssemblyValidation:
    assembly = await db.get(EngineeringAssembly, assembly_id)
    if not assembly:
        raise HTTPException(404, "Сборка не найдена")
    components = list((await db.execute(select(EngineeringAssemblyComponent).where(EngineeringAssemblyComponent.engineering_assembly_id == assembly_id))).scalars())
    exact_collisions, exact_keys, degraded = await _exact_interference(components)
    exact_key_set = set(exact_keys)
    # AABB stays for components without declared geometry; exact-checked pairs
    # are excluded so a bounding-box false positive can't contradict the kernel.
    collisions = [
        (first.instance_key, second.instance_key)
        for index, first in enumerate(components)
        if not first.suppressed and first.bounds
        for second in components[index + 1:]
        if not second.suppressed and second.bounds and _overlap(first.bounds, second.bounds)
        and not (first.instance_key in exact_key_set and second.instance_key in exact_key_set)
    ]
    collisions.extend((item["first"], item["second"]) for item in exact_collisions)
    keys = {component.instance_key for component in components}
    mates = list((await db.execute(select(EngineeringAssemblyMate).where(EngineeringAssemblyMate.engineering_assembly_id == assembly_id))).scalars())
    invalid = [str(mate.id) for mate in mates if mate.first_instance_key not in keys or mate.second_instance_key not in keys or mate.first_instance_key == mate.second_instance_key]
    return EngineeringAssemblyValidation(
        assembly_id=assembly_id,
        collisions=collisions,
        invalid_mates=invalid,
        exact_collisions=exact_collisions,
        exact_checked=sorted(exact_key_set),
        degraded=degraded,
    )


@router.get("/revisions/{revision_id}/validation-runs", response_model=list[EngineeringValidationRunOut])
async def list_validation_runs(revision_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> list[EngineeringValidationRun]:
    return list((await db.execute(
        select(EngineeringValidationRun).where(EngineeringValidationRun.engineering_revision_id == revision_id)
        .order_by(EngineeringValidationRun.created_at.desc())
    )).scalars())


@router.post("/revisions/{revision_id}/validate", response_model=EngineeringValidationRunOut)
async def validate_revision(revision_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> EngineeringValidationRun:
    """Aggregate deterministic CAD, assembly and technology findings for release."""
    revision = await db.get(EngineeringRevision, revision_id)
    if not revision:
        raise HTTPException(404, "Инженерная ревизия не найдена")
    findings = list((revision.validation or {}).get("issues", []))
    assemblies = list((await db.execute(select(EngineeringAssembly).where(EngineeringAssembly.engineering_revision_id == revision_id))).scalars())
    for assembly in assemblies:
        components = list((await db.execute(select(EngineeringAssemblyComponent).where(EngineeringAssemblyComponent.engineering_assembly_id == assembly.id))).scalars())
        for index, first in enumerate(components):
            for second in components[index + 1:]:
                if not first.suppressed and not second.suppressed and first.bounds and second.bounds and _overlap(first.bounds, second.bounds):
                    findings.append({"code": "ASSEMBLY_INTERFERENCE", "severity": "error", "entity_ids": [str(first.id), str(second.id)], "message_ru": f"Коллизия {first.instance_key} / {second.instance_key}", "level": 2})
    projections = list((await db.execute(select(EngineeringProjection).where(EngineeringProjection.engineering_revision_id == revision_id))).scalars())
    cad_revision_ids = [item.entity_id for item in projections if item.entity_type == "cad_ir_revision"]
    if cad_revision_ids:
        cad_revisions = list((await db.execute(
            select(CadIrRevision).where(CadIrRevision.id.in_(cad_revision_ids))
        )).scalars())
        approved_cad_ids = {item.id for item in cad_revisions if item.approved_by and item.approved_at}
        for cad_revision_id in cad_revision_ids:
            if cad_revision_id not in approved_cad_ids:
                findings.append({
                    "code": "CAD_IR_NOT_APPROVED",
                    "severity": "error",
                    "entity_ids": [str(cad_revision_id)],
                    "message_ru": "Связанная CAD IR ревизия не принята человеком",
                    "level": 2,
                })
    plan_ids = [item.entity_id for item in projections if item.entity_type == "manufacturing_process_plan"]
    if plan_ids:
        checks = list((await db.execute(select(ManufacturingCheckResult).where(ManufacturingCheckResult.process_plan_id.in_(plan_ids), ManufacturingCheckResult.status == "open"))).scalars())
        findings.extend({"code": check.check_code, "severity": "error" if check.severity in {"critical", "error"} else "warn", "entity_ids": [], "message_ru": check.message, "level": 5} for check in checks)
    analysis_cases = list((await db.execute(
        select(EngineeringAnalysisCase).where(EngineeringAnalysisCase.engineering_revision_id == revision_id)
    )).scalars())
    for case in analysis_cases:
        if case.status == "failed":
            findings.append({
                "code": "ANALYSIS_FAILED",
                "severity": "error",
                "entity_ids": [str(case.id)],
                "message_ru": f"Расчет {case.name} не прошел критерий прочности",
                "level": 2,
            })
    blocked = any(item.get("severity") == "error" for item in findings if isinstance(item, dict))
    run = EngineeringValidationRun(
        engineering_revision_id=revision_id,
        status="failed" if blocked else "passed",
        findings=findings,
        summary={"total": len(findings), "errors": sum(item.get("severity") == "error" for item in findings if isinstance(item, dict))},
    )
    db.add(run)
    revision.validation = {"issues": findings}
    revision.status = "needs_review" if blocked else "validated"
    await db.commit()
    await db.refresh(run)
    return run


@router.get("/revisions/{revision_id}/analysis-cases", response_model=list[EngineeringAnalysisCaseOut])
async def list_analysis_cases(revision_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> list[EngineeringAnalysisCase]:
    return list((await db.execute(select(EngineeringAnalysisCase).where(EngineeringAnalysisCase.engineering_revision_id == revision_id))).scalars())


@router.post("/revisions/{revision_id}/analysis-cases", response_model=EngineeringAnalysisCaseOut, status_code=status.HTTP_201_CREATED)
async def create_analysis_case(revision_id: uuid.UUID, body: EngineeringAnalysisCaseCreate, db: AsyncSession = Depends(get_db)) -> EngineeringAnalysisCase:
    await _editable_revision(db, revision_id)
    if body.material_id and not await db.get(EngineeringMaterial, body.material_id):
        raise HTTPException(404, "Материал не найден")
    case = EngineeringAnalysisCase(engineering_revision_id=revision_id, **body.model_dump())
    db.add(case)
    await db.commit()
    await db.refresh(case)
    return case


def _material_snapshot(material: EngineeringMaterial | None) -> dict | None:
    """F2: freeze the material card at run time — the live card may change."""
    if material is None:
        return None
    return {
        "id": str(material.id),
        "designation": material.designation,
        "standard": material.standard,
        "density_kg_m3": material.density_kg_m3,
        "elastic_modulus_mpa": material.elastic_modulus_mpa,
        "yield_strength_mpa": material.yield_strength_mpa,
        "tensile_strength_mpa": material.tensile_strength_mpa,
        "thermal_expansion_1_k": material.thermal_expansion_1_k,
    }


async def _next_run_number(db: AsyncSession, case_id: uuid.UUID) -> int:
    last = (
        await db.execute(
            select(EngineeringAnalysisRun.run_number)
            .where(EngineeringAnalysisRun.analysis_case_id == case_id)
            .order_by(EngineeringAnalysisRun.run_number.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return (last or 0) + 1


@router.post("/analysis-cases/{case_id}/run", response_model=EngineeringAnalysisCaseOut)
async def run_analysis_case(
    case_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> EngineeringAnalysisCase:
    require_permission(user, "engineering.analysis_run")
    """F1/F2: run the deterministic solver AND record an immutable run — the
    inputs, the material card as-of-now, the solver name/version and the
    verdict are frozen per execution; the case row mirrors only the latest
    run. A bad input is recorded too (status invalid_input) before the 422 —
    the audit trail keeps failed attempts, not only successes."""
    from app.domain.analysis_solvers import SOLVER_VERSION, SOLVERS, AnalysisInputError

    case = await db.get(EngineeringAnalysisCase, case_id)
    if not case:
        raise HTTPException(404, "Расчетный case не найден")
    await _editable_revision(db, case.engineering_revision_id)
    solver = SOLVERS.get(case.analysis_type)
    if solver is None:
        raise HTTPException(422, f"Для типа {case.analysis_type!r} нет solver; доступны: {', '.join(sorted(SOLVERS))}")
    material = await db.get(EngineeringMaterial, case.material_id) if case.material_id else None
    run = EngineeringAnalysisRun(
        analysis_case_id=case.id,
        run_number=await _next_run_number(db, case.id),
        status="invalid_input",
        inputs_snapshot=dict(case.inputs or {}),
        material_snapshot=_material_snapshot(material),
        solver_name=case.analysis_type,
        solver_version=SOLVER_VERSION,
    )
    db.add(run)
    try:
        outcome = solver(case.inputs or {}, material)
    except AnalysisInputError as exc:
        run.error = str(exc)
        await db.commit()
        raise HTTPException(422, str(exc)) from exc
    run.status = (
        "computed" if outcome.passed is None else ("passed" if outcome.passed else "failed")
    )
    run.results = outcome.results
    run.assumptions = outcome.assumptions
    case.results = outcome.results
    case.assumptions = outcome.assumptions
    case.solver = f"analytical/{SOLVER_VERSION}"
    case.status = run.status
    case.executed_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(case)
    return case


@router.get("/analysis-cases/{case_id}/runs", response_model=list[EngineeringAnalysisRunOut])
async def list_analysis_runs(case_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> list[EngineeringAnalysisRun]:
    """F2: the immutable execution history, newest first."""
    return list(
        (
            await db.execute(
                select(EngineeringAnalysisRun)
                .where(EngineeringAnalysisRun.analysis_case_id == case_id)
                .order_by(EngineeringAnalysisRun.run_number.desc())
            )
        ).scalars()
    )


@router.post("/revisions/{revision_id}/approve", response_model=EngineeringRevisionOut)
async def approve_revision(
    revision_id: uuid.UUID,
    body: EngineeringApprovalRequest,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> EngineeringRevision:
    require_permission(user, "engineering.revision_approve")
    revision = await db.get(EngineeringRevision, revision_id)
    if not revision:
        raise HTTPException(404, "Инженерная ревизия не найдена")
    if _blocking_errors(revision.validation):
        raise HTTPException(400, "Нельзя утвердить ревизию с блокирующими замечаниями")
    revision.status = "approved"
    revision.approved_by = body.approved_by
    revision.approved_at = datetime.now(UTC)
    project = await db.get(EngineeringProject, revision.engineering_project_id)
    if project:
        project.status = "approved"
    await log_action(db, action="engineering.revision.approve", entity_type="engineering_revision", entity_id=revision.id, user_id=body.approved_by)
    await add_timeline_event(db, entity_type="engineering_revision", entity_id=revision.id, event_type="approved", summary="Инженерная ревизия утверждена", actor=body.approved_by)
    await db.commit()
    await db.refresh(revision)
    return revision


# ── E3: change management ─────────────────────────────────────────────────────


async def _change_impact(db: AsyncSession, revision: EngineeringRevision) -> dict:
    """Auto impact analysis of the affected revision — plain data, no LLM:
    what depends on this geometry and would go stale if it changes."""
    projections = (
        await db.execute(
            select(EngineeringProjection).where(
                EngineeringProjection.engineering_revision_id == revision.id
            )
        )
    ).scalars().all()
    assemblies = (
        await db.execute(
            select(EngineeringAssembly).where(
                EngineeringAssembly.engineering_revision_id == revision.id
            )
        )
    ).scalars().all()
    analysis_cases = (
        await db.execute(
            select(EngineeringAnalysisCase).where(
                EngineeringAnalysisCase.engineering_revision_id == revision.id
            )
        )
    ).scalars().all()
    last_run = (
        await db.execute(
            select(EngineeringValidationRun)
            .where(EngineeringValidationRun.engineering_revision_id == revision.id)
            .order_by(EngineeringValidationRun.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    by_state: dict[str, int] = {}
    for projection in projections:
        by_state[projection.state] = by_state.get(projection.state, 0) + 1
    return {
        "revision": revision.revision,
        "revision_status": revision.status,
        "revision_approved": revision.status == "approved",
        "projections": {
            "total": len(projections),
            "by_state": by_state,
            "targets": [
                {"type": p.projection_type, "entity_type": p.entity_type, "entity_id": str(p.entity_id)}
                for p in projections
            ],
        },
        "assemblies": len(assemblies),
        "analysis_cases": len(analysis_cases),
        "last_validation_status": last_run.status if last_run else None,
    }


@router.post("/projects/{project_id}/change-requests", response_model=ChangeRequestOut, status_code=status.HTTP_201_CREATED)
async def create_change_request(
    project_id: uuid.UUID,
    body: ChangeRequestCreate,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> EngineeringChangeRequest:
    require_permission(user, "engineering.change_create")
    project = await db.get(EngineeringProject, project_id)
    if not project:
        raise HTTPException(404, "Инженерный проект не найден")
    revision = await db.get(EngineeringRevision, body.affected_revision_id)
    if not revision or revision.engineering_project_id != project_id:
        raise HTTPException(404, "Затронутая ревизия не найдена в этом проекте")
    superseded = None
    if body.supersedes_id is not None:
        superseded = await db.get(EngineeringChangeRequest, body.supersedes_id)
        if not superseded or superseded.engineering_project_id != project_id:
            raise HTTPException(404, "Заменяемый запрос изменения не найден в этом проекте")
        if superseded.status == "applied":
            raise HTTPException(409, "Применённый запрос изменения нельзя заменить — создайте новый поверх его ревизии")
    last_number = (
        await db.execute(
            select(EngineeringChangeRequest.number)
            .where(EngineeringChangeRequest.engineering_project_id == project_id)
            .order_by(EngineeringChangeRequest.number.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    change = EngineeringChangeRequest(
        engineering_project_id=project_id,
        number=(last_number or 0) + 1,
        title=body.title,
        reason=body.reason,
        status="review" if body.reviewers else "draft",
        affected_revision_id=revision.id,
        impact=await _change_impact(db, revision),
        reviewers=body.reviewers,
        signatures=[],
        supersedes_id=body.supersedes_id,
        created_by=body.created_by,
    )
    db.add(change)
    if superseded is not None:
        superseded.status = "superseded"
    await db.flush()
    await log_action(db, action="engineering.change_request.create", entity_type="engineering_change_request", entity_id=change.id, user_id=body.created_by)
    await add_timeline_event(db, entity_type="engineering_change_request", entity_id=change.id, event_type="created", summary=f"Запрос изменения №{change.number}: {change.title}", actor=body.created_by)
    await db.commit()
    await db.refresh(change)
    return change


@router.get("/projects/{project_id}/change-requests", response_model=list[ChangeRequestOut])
async def list_change_requests(project_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> list[EngineeringChangeRequest]:
    return list(
        (
            await db.execute(
                select(EngineeringChangeRequest)
                .where(EngineeringChangeRequest.engineering_project_id == project_id)
                .order_by(EngineeringChangeRequest.number.desc())
            )
        ).scalars()
    )


@router.post("/change-requests/{change_id}/sign", response_model=ChangeRequestOut)
async def sign_change_request(
    change_id: uuid.UUID,
    body: ChangeRequestSign,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> EngineeringChangeRequest:
    require_permission(user, "engineering.change_sign")
    """A reviewer's signature. Every listed reviewer approving → approved;
    any single reject → rejected. Signatures are append-only per reviewer."""
    change = await db.get(EngineeringChangeRequest, change_id)
    if not change:
        raise HTTPException(404, "Запрос изменения не найден")
    if change.status not in ("draft", "review"):
        raise HTTPException(409, f"Запрос в статусе {change.status!r} больше не подписывается")
    if body.reviewer not in (change.reviewers or []):
        raise HTTPException(403, "Подписант не входит в список согласующих этого запроса")
    if any(s.get("reviewer") == body.reviewer for s in (change.signatures or [])):
        raise HTTPException(409, "Этот согласующий уже подписал запрос")
    change.signatures = [
        *(change.signatures or []),
        {
            "reviewer": body.reviewer,
            "decision": body.decision,
            "comment": body.comment,
            "at": datetime.now(UTC).isoformat(),
        },
    ]
    if body.decision == "reject":
        change.status = "rejected"
        change.decided_at = datetime.now(UTC)
    elif {s["reviewer"] for s in change.signatures if s["decision"] == "approve"} >= set(change.reviewers):
        change.status = "approved"
        change.decided_at = datetime.now(UTC)
    else:
        change.status = "review"
    await log_action(db, action="engineering.change_request.sign", entity_type="engineering_change_request", entity_id=change.id, user_id=body.reviewer)
    await add_timeline_event(db, entity_type="engineering_change_request", entity_id=change.id, event_type=f"signed_{body.decision}", summary=f"{body.reviewer}: {body.decision}", actor=body.reviewer)
    await db.commit()
    await db.refresh(change)
    return change


@router.post("/change-requests/{change_id}/apply", response_model=ChangeRequestOut)
async def apply_change_request(
    change_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> EngineeringChangeRequest:
    require_permission(user, "engineering.change_apply")
    """Turn an approved change request into a change ORDER: mint a new draft
    revision based on the affected one (which is never mutated) and record it
    on the request. Editing then proceeds on the new revision as usual."""
    change = await db.get(EngineeringChangeRequest, change_id)
    if not change:
        raise HTTPException(404, "Запрос изменения не найден")
    if change.status != "approved":
        raise HTTPException(409, "Применить можно только согласованный запрос (все подписи approve)")
    affected = await db.get(EngineeringRevision, change.affected_revision_id)
    if not affected:
        raise HTTPException(404, "Затронутая ревизия не найдена")
    last = (
        await db.execute(
            select(EngineeringRevision.revision)
            .where(EngineeringRevision.engineering_project_id == change.engineering_project_id)
            .order_by(EngineeringRevision.revision.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    new_revision = EngineeringRevision(
        engineering_project_id=change.engineering_project_id,
        revision=(last or 0) + 1,
        base_revision=affected.revision,
        status="draft",
        origin="change_order",
        change_summary=f"Изменение №{change.number}: {change.title} — {change.reason}",
        payload=dict(affected.payload or {}),
        validation={},
        created_by=change.created_by,
    )
    db.add(new_revision)
    await db.flush()
    change.status = "applied"
    change.applied_revision_id = new_revision.id
    await log_action(db, action="engineering.change_request.apply", entity_type="engineering_change_request", entity_id=change.id, user_id=change.created_by)
    await add_timeline_event(db, entity_type="engineering_change_request", entity_id=change.id, event_type="applied", summary=f"Создана ревизия {new_revision.revision} (база {affected.revision})", actor=change.created_by)
    await db.commit()
    await db.refresh(change)
    return change
