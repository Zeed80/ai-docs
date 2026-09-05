"""Canonical Engineering IR projects and revision-safe domain projections."""

import pathlib
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.audit.service import add_timeline_event, log_action
from app.auth.jwt import get_current_user
from app.auth.models import UserInfo, require_permission
from app.db.models import (
    BOM,
    CadIrRevision,
    Drawing,
    DrawingAssemblyBOM,
    EngineeringAnalysisCase,
    EngineeringAnalysisRun,
    EngineeringAssembly,
    EngineeringAssemblyComponent,
    EngineeringAssemblyMate,
    EngineeringChangeRequest,
    EngineeringMaterial,
    EngineeringMaterialAssignment,
    EngineeringProject,
    EngineeringProjection,
    EngineeringRevision,
    EngineeringValidationRun,
    ManufacturingCheckResult,
    ManufacturingProcessPlan,
)
from app.db.session import get_db
from app.domain.emg_predicates import PREDICATE
from app.domain.engineering import (
    AssemblyComponentsFromBomRequest,
    AssemblyComponentsFromBomResult,
    AssemblyComponentUnresolved,
    AssemblySolvePreviewResult,
    AssemblySolveSkippedMate,
    ChangeRequestCreate,
    ChangeRequestOut,
    ChangeRequestSign,
    EngineeringAnalysisCaseCreate,
    EngineeringAnalysisCaseOut,
    EngineeringAnalysisRunOut,
    EngineeringApprovalRequest,
    EngineeringAssemblyComponentCreate,
    EngineeringAssemblyComponentOut,
    EngineeringAssemblyCreate,
    EngineeringAssemblyMateCreate,
    EngineeringAssemblyMateOut,
    EngineeringAssemblyOut,
    EngineeringAssemblyValidation,
    EngineeringMaterialAssignmentCreate,
    EngineeringMaterialAssignmentOut,
    EngineeringMaterialCreate,
    EngineeringMaterialOut,
    EngineeringProjectCreate,
    EngineeringProjectDetail,
    EngineeringProjectionCreate,
    EngineeringProjectionOut,
    EngineeringProjectOut,
    EngineeringRevisionCreate,
    EngineeringRevisionOut,
    EngineeringValidationRunOut,
)

router = APIRouter()

_PROJECTABLE_MODELS = {
    "drawing": Drawing,
    "bom": BOM,
    "manufacturing_process_plan": ManufacturingProcessPlan,
    "cad_ir_revision": CadIrRevision,
}


def _artifact_report_with_storage(
    report: dict,
    *,
    artifact_path: str,
    report_path: str,
) -> dict:
    """Bind a deterministic verification report to content-addressed storage."""
    import hashlib
    import json

    enriched = {key: value for key, value in report.items() if key != "canonical_report_sha256"}
    enriched.update({"artifact_path": artifact_path, "report_path": report_path})
    enriched["canonical_report_sha256"] = hashlib.sha256(
        json.dumps(enriched, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return enriched


async def _persist_verified_svg_patch(
    *,
    db: AsyncSession,
    latest_row: Any,
    graph: Any,
    graph_id: str,
    svg: bytes,
    report: dict,
    patch: Any,
) -> tuple[Any, Any, bool]:
    """Atomically store SVG/report and merge its already validated GraphPatch."""
    import json

    from anyio import to_thread

    from app.services.engineering_model_graph import load_graph, merge_and_persist_patch
    from app.storage import delete_file, upload_file

    artifact_sha = report["artifact_sha256"]
    existing = next(
        (
            item
            for item in graph.evidence
            if item.kind == "projection_comparison"
            and item.payload.get("artifact_sha256") == artifact_sha
            and item.payload.get("artifact_path") == report["artifact_path"]
        ),
        None,
    )
    if existing is not None:
        return latest_row, graph, True
    report_bytes = json.dumps(
        report, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    uploaded: list[str] = []
    try:
        await to_thread.run_sync(upload_file, svg, report["artifact_path"], "image/svg+xml")
        uploaded.append(report["artifact_path"])
        await to_thread.run_sync(
            upload_file, report_bytes, report["report_path"], "application/json"
        )
        uploaded.append(report["report_path"])
        row, errors = await merge_and_persist_patch(db, patch, expected_graph_id=graph_id)
        if row is None:
            raise ValueError("SVG GraphPatch отклонён: " + ", ".join(errors))
        await db.commit()
        return row, load_graph(row), False
    except Exception:
        await db.rollback()
        for path in reversed(uploaded):
            try:
                await to_thread.run_sync(delete_file, path)
            except Exception:  # noqa: BLE001 - preserve the primary error
                pass
        raise


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
    await log_action(
        db,
        action="engineering.project.create",
        entity_type="engineering_project",
        entity_id=project.id,
    )
    await db.commit()
    await db.refresh(project)
    return project


@router.get("/projects", response_model=list[EngineeringProjectOut])
async def list_projects(db: AsyncSession = Depends(get_db)) -> list[EngineeringProject]:
    result = await db.execute(
        select(EngineeringProject).order_by(EngineeringProject.updated_at.desc())
    )
    return list(result.scalars())


@router.get("/materials", response_model=list[EngineeringMaterialOut])
async def list_materials(db: AsyncSession = Depends(get_db)) -> list[EngineeringMaterial]:
    return list(
        (
            await db.execute(select(EngineeringMaterial).order_by(EngineeringMaterial.designation))
        ).scalars()
    )


@router.post(
    "/materials", response_model=EngineeringMaterialOut, status_code=status.HTTP_201_CREATED
)
async def create_material(
    body: EngineeringMaterialCreate, db: AsyncSession = Depends(get_db)
) -> EngineeringMaterial:
    material = EngineeringMaterial(**body.model_dump())
    db.add(material)
    await db.commit()
    await db.refresh(material)
    return material


@router.get("/projects/{project_id}", response_model=EngineeringProjectDetail)
async def get_project(
    project_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> EngineeringProject:
    result = await db.execute(
        select(EngineeringProject)
        .where(EngineeringProject.id == project_id)
        .options(selectinload(EngineeringProject.revisions))
    )
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(404, "Инженерный проект не найден")
    return project


@router.post(
    "/projects/{project_id}/revisions",
    response_model=EngineeringRevisionOut,
    status_code=status.HTTP_201_CREATED,
)
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
    await db.execute(
        select(EngineeringProject.id).where(EngineeringProject.id == project_id).with_for_update()
    )
    latest = (
        await db.execute(
            select(EngineeringRevision)
            .where(EngineeringRevision.engineering_project_id == project_id)
            .order_by(EngineeringRevision.revision.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
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
        stale = (
            await db.execute(
                select(EngineeringProjection)
                .join(EngineeringRevision)
                .where(
                    EngineeringRevision.engineering_project_id == project_id,
                    EngineeringProjection.state == "current",
                )
            )
        ).scalars()
        for projection in stale:
            projection.state = "stale"
    project.status = "needs_review" if revision.status == "needs_review" else "validated"
    await db.flush()
    await log_action(
        db,
        action="engineering.revision.create",
        entity_type="engineering_revision",
        entity_id=revision.id,
        user_id=body.created_by,
    )
    await db.commit()
    await db.refresh(revision)
    return revision


@router.post(
    "/revisions/{revision_id}/projections",
    response_model=EngineeringProjectionOut,
    status_code=status.HTTP_201_CREATED,
)
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
        raise HTTPException(
            400,
            "Поддерживаются проекции drawing, cad_ir_revision, bom и manufacturing_process_plan",
        )
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
async def list_projections(
    revision_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> list[EngineeringProjection]:
    result = await db.execute(
        select(EngineeringProjection)
        .where(EngineeringProjection.engineering_revision_id == revision_id)
        .order_by(EngineeringProjection.created_at)
    )
    return list(result.scalars())


@router.get(
    "/revisions/{revision_id}/materials", response_model=list[EngineeringMaterialAssignmentOut]
)
async def list_material_assignments(
    revision_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> list[EngineeringMaterialAssignment]:
    result = await db.execute(
        select(EngineeringMaterialAssignment)
        .where(EngineeringMaterialAssignment.engineering_revision_id == revision_id)
        .options(selectinload(EngineeringMaterialAssignment.material))
    )
    return list(result.scalars())


@router.post(
    "/revisions/{revision_id}/materials",
    response_model=EngineeringMaterialAssignmentOut,
    status_code=status.HTTP_201_CREATED,
)
async def assign_material(
    revision_id: uuid.UUID,
    body: EngineeringMaterialAssignmentCreate,
    db: AsyncSession = Depends(get_db),
) -> EngineeringMaterialAssignment:
    revision = await db.get(EngineeringRevision, revision_id)
    if not revision:
        raise HTTPException(404, "Инженерная ревизия не найдена")
    if revision.status == "approved":
        raise HTTPException(400, "Нельзя изменять материал утвержденной ревизии")
    if not await db.get(EngineeringMaterial, body.material_id):
        raise HTTPException(404, "Материал не найден")
    assignment = EngineeringMaterialAssignment(
        engineering_revision_id=revision_id, **body.model_dump()
    )
    db.add(assignment)
    await db.commit()
    result = await db.execute(
        select(EngineeringMaterialAssignment)
        .where(EngineeringMaterialAssignment.id == assignment.id)
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
async def list_assemblies(
    revision_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> list[EngineeringAssembly]:
    return list(
        (
            await db.execute(
                select(EngineeringAssembly).where(
                    EngineeringAssembly.engineering_revision_id == revision_id
                )
            )
        ).scalars()
    )


@router.post(
    "/revisions/{revision_id}/assemblies",
    response_model=EngineeringAssemblyOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_assembly(
    revision_id: uuid.UUID, body: EngineeringAssemblyCreate, db: AsyncSession = Depends(get_db)
) -> EngineeringAssembly:
    await _editable_revision(db, revision_id)
    assembly = EngineeringAssembly(engineering_revision_id=revision_id, **body.model_dump())
    db.add(assembly)
    await db.commit()
    await db.refresh(assembly)
    return assembly


@router.post(
    "/assemblies/{assembly_id}/components",
    response_model=EngineeringAssemblyComponentOut,
    status_code=status.HTTP_201_CREATED,
)
async def add_assembly_component(
    assembly_id: uuid.UUID,
    body: EngineeringAssemblyComponentCreate,
    db: AsyncSession = Depends(get_db),
) -> EngineeringAssemblyComponent:
    assembly = await db.get(EngineeringAssembly, assembly_id)
    if not assembly:
        raise HTTPException(404, "Сборка не найдена")
    await _editable_revision(db, assembly.engineering_revision_id)
    component = EngineeringAssemblyComponent(
        engineering_assembly_id=assembly_id, **body.model_dump()
    )
    db.add(component)
    await db.commit()
    await db.refresh(component)
    return component


@router.post(
    "/assemblies/{assembly_id}/components/from-bom",
    response_model=AssemblyComponentsFromBomResult,
    summary="Skill: engineering.assembly_components_from_bom — sync components from a BOM.",
)
async def sync_assembly_components_from_bom(
    assembly_id: uuid.UUID,
    body: AssemblyComponentsFromBomRequest,
    db: AsyncSession = Depends(get_db),
) -> AssemblyComponentsFromBomResult:
    """Фаза 4.2 Уровень A: BOM-строка → EngineeringAssemblyComponent.

    Matches each DrawingAssemblyBOM row's drawing_number against an already
    digitized Drawing linked to an EngineeringRevision (via the existing
    /revisions/{id}/projections mechanism — entity_type=drawing sets
    Drawing.engineering_revision_id). A match sets component_revision_id;
    no match, or more than one candidate Drawing, leaves it unresolved —
    never guessed. Re-running upserts by a deterministic instance_key
    (bom-<item_no>), so it never duplicates rows.
    """
    assembly = await db.get(EngineeringAssembly, assembly_id)
    if not assembly:
        raise HTTPException(404, "Сборка не найдена")
    await _editable_revision(db, assembly.engineering_revision_id)

    drawing = await db.get(Drawing, body.drawing_id)
    if not drawing:
        raise HTTPException(404, "Чертёж не найден")

    bom_rows = list(
        (
            await db.execute(
                select(DrawingAssemblyBOM)
                .where(DrawingAssemblyBOM.drawing_id == body.drawing_id)
                .order_by(DrawingAssemblyBOM.item_no)
            )
        ).scalars()
    )
    if not bom_rows:
        raise HTTPException(409, "У чертежа нет извлечённой спецификации (assembly-bom/extract)")

    existing_components = {
        c.instance_key: c
        for c in (
            await db.execute(
                select(EngineeringAssemblyComponent).where(
                    EngineeringAssemblyComponent.engineering_assembly_id == assembly_id,
                )
            )
        ).scalars()
    }

    matched: list[EngineeringAssemblyComponent] = []
    unresolved: list[AssemblyComponentUnresolved] = []

    for item in bom_rows:
        instance_key = f"bom-{item.item_no}"
        normalized = (item.drawing_number or "").strip()
        component_revision_id: uuid.UUID | None = None
        bom_meta: dict[str, Any] = {
            "source": "drawing_bom",
            "source_drawing_id": str(body.drawing_id),
            "drawing_number": item.drawing_number,
            "material": item.material,
            "note": item.note,
            "balloon_coords": item.balloon_coords,
            "confidence": item.confidence,
        }

        if not normalized:
            unresolved.append(
                AssemblyComponentUnresolved(
                    item_no=item.item_no,
                    designation=item.designation,
                    drawing_number=item.drawing_number,
                    reason="missing_drawing_number",
                )
            )
        else:
            candidates = list(
                (
                    await db.execute(
                        select(Drawing).where(
                            func.lower(func.trim(Drawing.drawing_number)) == normalized.lower(),
                            Drawing.engineering_revision_id.is_not(None),
                        )
                    )
                ).scalars()
            )
            if len(candidates) == 1:
                component_revision_id = candidates[0].engineering_revision_id
            elif len(candidates) == 0:
                unresolved.append(
                    AssemblyComponentUnresolved(
                        item_no=item.item_no,
                        designation=item.designation,
                        drawing_number=item.drawing_number,
                        reason="no_match",
                    )
                )
            else:
                bom_meta["ambiguous_drawing_ids"] = [str(c.id) for c in candidates]
                unresolved.append(
                    AssemblyComponentUnresolved(
                        item_no=item.item_no,
                        designation=item.designation,
                        drawing_number=item.drawing_number,
                        reason="ambiguous",
                    )
                )

        existing = existing_components.get(instance_key)
        if existing:
            existing.designation = item.designation
            existing.quantity = max(1, round(item.quantity))
            existing.component_revision_id = component_revision_id
            existing.metadata_ = {**existing.metadata_, "bom": bom_meta}
            matched.append(existing)
        else:
            component = EngineeringAssemblyComponent(
                engineering_assembly_id=assembly_id,
                component_revision_id=component_revision_id,
                instance_key=instance_key,
                designation=item.designation,
                quantity=max(1, round(item.quantity)),
                sort_order=item.item_no,
                metadata_={"bom": bom_meta},
            )
            db.add(component)
            matched.append(component)

    await db.commit()
    for component in matched:
        await db.refresh(component)

    return AssemblyComponentsFromBomResult(
        assembly_id=assembly_id,
        drawing_id=body.drawing_id,
        components=matched,
        unresolved=unresolved,
    )


@router.post(
    "/assemblies/{assembly_id}/mates",
    response_model=EngineeringAssemblyMateOut,
    status_code=status.HTTP_201_CREATED,
)
async def add_assembly_mate(
    assembly_id: uuid.UUID, body: EngineeringAssemblyMateCreate, db: AsyncSession = Depends(get_db)
) -> EngineeringAssemblyMate:
    assembly = await db.get(EngineeringAssembly, assembly_id)
    if not assembly:
        raise HTTPException(404, "Сборка не найдена")
    await _editable_revision(db, assembly.engineering_revision_id)
    keys = set(
        (
            await db.execute(
                select(EngineeringAssemblyComponent.instance_key).where(
                    EngineeringAssemblyComponent.engineering_assembly_id == assembly_id
                )
            )
        ).scalars()
    )
    if (
        body.first_instance_key not in keys
        or body.second_instance_key not in keys
        or body.first_instance_key == body.second_instance_key
    ):
        raise HTTPException(422, "Сопряжение должно ссылаться на два разных экземпляра сборки")
    mate = EngineeringAssemblyMate(engineering_assembly_id=assembly_id, **body.model_dump())
    db.add(mate)
    await db.commit()
    await db.refresh(mate)
    return mate


def _solve_frame(raw: Any) -> dict[str, Any] | None:
    """Validate a mate.parameters.first_frame/second_frame dict.

    Required shape: {"position_mm": [x,y,z], "axis"?: [x,y,z], "angle_deg"?: n}.
    Returns None (never raises) for anything malformed -- the caller reports
    the mate as skipped rather than guess a frame.
    """
    if not isinstance(raw, dict):
        return None
    position = raw.get("position_mm")
    if (
        not isinstance(position, list)
        or len(position) != 3
        or any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in position)
    ):
        return None
    axis = raw.get("axis", [0.0, 0.0, 1.0])
    if (
        not isinstance(axis, list)
        or len(axis) != 3
        or any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in axis)
        or all(abs(float(v)) < 1e-12 for v in axis)
    ):
        return None
    angle = raw.get("angle_deg", 0.0)
    if isinstance(angle, bool) or not isinstance(angle, (int, float)):
        return None
    return {
        "position_mm": [float(v) for v in position],
        "axis": [float(v) for v in axis],
        "angle_deg": float(angle),
    }


async def _call_kernel_assembly_solve(payload: dict[str, Any]) -> dict[str, Any]:
    import httpx

    from app.config import settings

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                f"{settings.cad_kernel_url.rstrip('/')}/assembly/solve", json=payload
            )
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"cad-kernel недоступен: {exc}") from exc
    if response.status_code != 200:
        raise HTTPException(422, f"kernel отклонил запрос: {response.text[:300]}")
    return response.json()


@router.post(
    "/assemblies/{assembly_id}/solve",
    response_model=AssemblySolvePreviewResult,
    summary="Skill: engineering.assembly_solve_preview — preview joint-solved placements.",
)
async def solve_assembly_preview(
    assembly_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> AssemblySolvePreviewResult:
    """Фаза 4.2 Уровень B: read-only preview via the kernel's real solver.

    Writes nothing back to components -- component.transform already has an
    established, narrower consumer (_exact_interference/POST /interference:
    translate + rotate_z_deg only), and a joint's solved rotation is
    generally a full 3D axis/angle the interference path can't represent
    yet. Applying a solved result to a component is a deliberately separate,
    not-yet-built follow-up.

    Each mate needs an explicit parameters.first_frame/second_frame (this
    project has no cross-component face-selection UI yet — see memory
    project_cad_assembly_solver_spike_2026_08_11); mate_type is mapped to a
    kernel joint type via the documented, non-1:1 MATE_TYPE_TO_JOINT_TYPE
    table. A mate with an unsupported type, a dangling component reference,
    or a missing/malformed frame is excluded and reported in skipped_mates
    — never guessed. A component is grounded via metadata.grounded (the
    same convention analyze_assembly_dof already uses).
    """
    from app.domain.assembly import MATE_TYPE_TO_JOINT_TYPE

    assembly = await db.get(EngineeringAssembly, assembly_id)
    if not assembly:
        raise HTTPException(404, "Сборка не найдена")
    components = list(
        (
            await db.execute(
                select(EngineeringAssemblyComponent).where(
                    EngineeringAssemblyComponent.engineering_assembly_id == assembly_id,
                    EngineeringAssemblyComponent.suppressed.is_(False),
                )
            )
        ).scalars()
    )
    if not components:
        raise HTTPException(409, "Сборка не содержит активных компонентов")
    mates = list(
        (
            await db.execute(
                select(EngineeringAssemblyMate).where(
                    EngineeringAssemblyMate.engineering_assembly_id == assembly_id,
                )
            )
        ).scalars()
    )

    key_set = {component.instance_key for component in components}
    grounded_keys = {
        component.instance_key
        for component in components
        if bool((component.metadata_ or {}).get("grounded"))
    }
    if not grounded_keys:
        raise HTTPException(409, "Нет ни одного заземлённого компонента (metadata.grounded)")

    kernel_components = []
    for component in components:
        transform = component.transform or {}
        translate = transform.get("translate", [0.0, 0.0, 0.0])
        if not isinstance(translate, list) or len(translate) != 3:
            translate = [0.0, 0.0, 0.0]
        kernel_components.append(
            {
                "key": component.instance_key,
                "position_mm": [float(value) for value in translate],
                "axis": [0.0, 0.0, 1.0],
                "angle_deg": float(transform.get("rotate_z_deg", 0.0) or 0.0),
                "grounded": component.instance_key in grounded_keys,
            }
        )

    joints = []
    skipped: list[AssemblySolveSkippedMate] = []
    for mate in mates:
        joint_type = MATE_TYPE_TO_JOINT_TYPE.get(mate.mate_type.lower())
        if joint_type is None:
            skipped.append(
                AssemblySolveSkippedMate(
                    mate_id=mate.id,
                    mate_type=mate.mate_type,
                    reason="unsupported_mate_type",
                )
            )
            continue
        if mate.first_instance_key not in key_set or mate.second_instance_key not in key_set:
            skipped.append(
                AssemblySolveSkippedMate(
                    mate_id=mate.id,
                    mate_type=mate.mate_type,
                    reason="invalid_component_reference",
                )
            )
            continue
        parameters = mate.parameters or {}
        first_frame = _solve_frame(parameters.get("first_frame"))
        second_frame = _solve_frame(parameters.get("second_frame"))
        if first_frame is None or second_frame is None:
            skipped.append(
                AssemblySolveSkippedMate(
                    mate_id=mate.id,
                    mate_type=mate.mate_type,
                    reason="missing_frames",
                )
            )
            continue
        joints.append(
            {
                "type": joint_type,
                "first": {"key": mate.first_instance_key, **first_frame},
                "second": {"key": mate.second_instance_key, **second_frame},
            }
        )

    payload = {"components": kernel_components, "joints": joints}
    result = await _call_kernel_assembly_solve(payload)

    return AssemblySolvePreviewResult(
        assembly_id=assembly_id,
        solved=result["solved"],
        status_code=result["status_code"],
        reason=result["reason"],
        placements=result["placements"],
        grounded_instances=sorted(grounded_keys),
        skipped_mates=skipped,
    )


def _overlap(a: dict, b: dict) -> bool:
    required = ("x_min", "x_max", "y_min", "y_max", "z_min", "z_max")
    if not all(key in a and key in b for key in required):
        return False
    return all(
        float(a[f"{axis}_min"]) < float(b[f"{axis}_max"])
        and float(b[f"{axis}_min"]) < float(a[f"{axis}_max"])
        for axis in ("x", "y", "z")
    )


async def _exact_interference(
    components: list[EngineeringAssemblyComponent],
) -> tuple[list[dict], list[str], str | None]:
    """E5: exact B-Rep interference via the CAD kernel for components that
    declare an occupancy solid (metadata.shape: box|cylinder + transform).
    Returns (collisions, checked_instance_keys, degradation_note)."""
    import httpx

    from app.config import settings

    exact = [
        component
        for component in components
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
            response = await client.post(
                f"{settings.cad_kernel_url.rstrip('/')}/interference", json=payload
            )
        if response.status_code != 200:
            return [], [], f"kernel отклонил exact-проверку: {response.text[:200]}"
        return (
            response.json().get("collisions", []),
            [component.instance_key for component in exact],
            None,
        )
    except httpx.HTTPError as exc:
        # The kernel being down must not block validation — degrade to AABB
        # loudly, never silently.
        return [], [], f"cad-kernel недоступен, точная проверка пропущена: {exc}"


@router.post("/assemblies/{assembly_id}/validate", response_model=EngineeringAssemblyValidation)
async def validate_assembly(
    assembly_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> EngineeringAssemblyValidation:
    assembly = await db.get(EngineeringAssembly, assembly_id)
    if not assembly:
        raise HTTPException(404, "Сборка не найдена")
    components = list(
        (
            await db.execute(
                select(EngineeringAssemblyComponent).where(
                    EngineeringAssemblyComponent.engineering_assembly_id == assembly_id
                )
            )
        ).scalars()
    )
    exact_collisions, exact_keys, degraded = await _exact_interference(components)
    exact_key_set = set(exact_keys)
    # AABB stays for components without declared geometry; exact-checked pairs
    # are excluded so a bounding-box false positive can't contradict the kernel.
    collisions = [
        (first.instance_key, second.instance_key)
        for index, first in enumerate(components)
        if not first.suppressed and first.bounds
        for second in components[index + 1 :]
        if not second.suppressed
        and second.bounds
        and _overlap(first.bounds, second.bounds)
        and not (first.instance_key in exact_key_set and second.instance_key in exact_key_set)
    ]
    collisions.extend((item["first"], item["second"]) for item in exact_collisions)
    keys = {component.instance_key for component in components}
    mates = list(
        (
            await db.execute(
                select(EngineeringAssemblyMate).where(
                    EngineeringAssemblyMate.engineering_assembly_id == assembly_id
                )
            )
        ).scalars()
    )
    invalid = [
        str(mate.id)
        for mate in mates
        if mate.first_instance_key not in keys
        or mate.second_instance_key not in keys
        or mate.first_instance_key == mate.second_instance_key
    ]
    from app.domain.assembly import analyze_assembly_dof

    dof = analyze_assembly_dof(components, mates)
    return EngineeringAssemblyValidation(
        assembly_id=assembly_id,
        collisions=collisions,
        invalid_mates=invalid,
        exact_collisions=exact_collisions,
        exact_checked=sorted(exact_key_set),
        degraded=degraded,
        **dof.model_dump(exclude={"invalid_mates"}),
    )


@router.post("/assemblies/{assembly_id}/model-graph")
async def sync_assembly_model_graph(
    assembly_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> dict:
    """Project an editable assembly into one immutable EMG revision chain."""
    require_permission(user, "engineering.revision_create")
    assembly = await db.get(EngineeringAssembly, assembly_id)
    if not assembly:
        raise HTTPException(404, "Сборка не найдена")
    revision = await db.get(EngineeringRevision, assembly.engineering_revision_id)
    if revision is None:
        raise HTTPException(409, "Инженерная ревизия сборки не найдена")
    components = list(
        (
            await db.execute(
                select(EngineeringAssemblyComponent).where(
                    EngineeringAssemblyComponent.engineering_assembly_id == assembly_id
                )
            )
        ).scalars()
    )
    mates = list(
        (
            await db.execute(
                select(EngineeringAssemblyMate).where(
                    EngineeringAssemblyMate.engineering_assembly_id == assembly_id
                )
            )
        ).scalars()
    )
    validation = await validate_assembly(assembly_id, db)

    from app.ai.assembly_emg import assembly_as_graph, assembly_revision_patch
    from app.services.engineering_model_graph import (
        create_initial_graph,
        latest_graph_revision,
        load_graph,
        merge_and_persist_patch,
    )

    graph_id = f"assembly:{assembly_id}"
    desired = assembly_as_graph(
        graph_id=graph_id,
        name=assembly.name,
        designation=assembly.designation,
        components=components,
        mates=mates,
        dof=validation,
        collisions=validation.collisions,
        exact_checked=validation.exact_checked,
        interference_degraded=validation.degraded,
    )
    latest = await latest_graph_revision(db, graph_id, lock=True)
    if latest is None:
        row = await create_initial_graph(
            db,
            desired,
            engineering_project_id=revision.engineering_project_id,
            engineering_revision_id=revision.id,
        )
        graph = desired
    else:
        current = load_graph(latest)
        patch = assembly_revision_patch(current, desired)
        if patch is None:
            row, graph = latest, current
        else:
            row, errors = await merge_and_persist_patch(
                db,
                patch,
                expected_graph_id=graph_id,
            )
            if row is None:
                await db.rollback()
                raise HTTPException(409, "GraphPatch отклонён: " + ", ".join(errors))
            graph = load_graph(row)
    await db.commit()
    return {
        "id": str(row.id),
        "engineering_project_id": str(row.engineering_project_id),
        "engineering_revision_id": str(row.engineering_revision_id),
        "graph_id": row.graph_id,
        "revision": row.revision,
        "parent_revision": row.parent_revision,
        "canonical_sha256": row.canonical_sha256,
        "profile": row.profile,
        "comprehension_status": row.comprehension_status,
        "build_status": row.build_status,
        "release_status": row.release_status,
        "graph": graph.model_dump(mode="json"),
    }


@router.post("/assemblies/{assembly_id}/model-graph/build")
async def build_assembly_model_graph(
    assembly_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> dict:
    """Build, reopen and attach a multi-solid STEP to the latest assembly EMG."""
    import hashlib
    import io
    import json
    import zipfile

    import httpx

    from app.config import settings
    from app.domain.engineering_model_graph import (
        Assertion,
        Evidence,
        ExactValue,
        GraphEdge,
        GraphNode,
        GraphPatch,
        compile_build_plan,
    )
    from app.services.engineering_model_graph import (
        evaluate_build_admission,
        latest_graph_revision,
        load_graph,
        merge_and_persist_patch,
    )
    from app.storage import delete_file, upload_file

    synced = await sync_assembly_model_graph(assembly_id, db, user)
    graph_id = synced["graph_id"]
    latest = await latest_graph_revision(db, graph_id, lock=True)
    if latest is None:
        raise HTTPException(409, "Assembly EngineeringModelGraph не создан")
    graph = load_graph(latest)
    reopen_assertion = next(
        (
            item
            for item in graph.assertions
            if item.state == "active" and item.predicate == PREDICATE.ASSEMBLY_ARTIFACT_REOPEN_VALID
        ),
        None,
    )
    if reopen_assertion is None:
        raise HTTPException(409, "В assembly graph отсутствует artifact reopen gate")
    non_step_blockers = {
        item.id
        for item in graph.assertions
        if item.state == "active" and item.predicate == PREDICATE.ASSEMBLY_REQUIRED_2D_COMPLETE
    }
    admission = evaluate_build_admission(
        graph,
        "production",
        "assembly_step",
        pending_output_assertion_ids={reopen_assertion.id, *non_step_blockers},
    )
    if not admission.allowed:
        raise HTTPException(
            409,
            detail=admission.model_dump(mode="json"),
        )
    assembly = await db.get(EngineeringAssembly, assembly_id)
    components = list(
        (
            await db.execute(
                select(EngineeringAssemblyComponent).where(
                    EngineeringAssemblyComponent.engineering_assembly_id == assembly_id,
                    EngineeringAssemblyComponent.suppressed.is_(False),
                )
            )
        ).scalars()
    )
    if assembly is None or not components:
        raise HTTPException(409, "Сборка не содержит активных компонентов")
    if any(not isinstance(item.metadata_.get("shape"), dict) for item in components):
        raise HTTPException(409, "Каждый компонент должен иметь exact metadata.shape")
    payload = {
        "name": assembly.name,
        "components": [
            {
                "key": item.instance_key,
                "shape": item.metadata_["shape"],
                "transform": item.transform or {},
            }
            for item in components
        ],
        "metadata": {
            "graph_id": graph.graph_id,
            "graph_revision": graph.revision,
            "graph_sha256": graph.canonical_sha256,
        },
    }
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(
                f"{settings.cad_kernel_url.rstrip('/')}/assembly/compile",
                json=payload,
            )
    except httpx.HTTPError as exc:
        raise HTTPException(503, f"cad-kernel недоступен: {exc}") from exc
    if response.status_code != 200:
        raise HTTPException(422, "cad-kernel отклонил сборку: " + response.text[:300])
    try:
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            step_bytes = archive.read("assembly.step")
            report_bytes = archive.read("assembly-report.json")
        report = json.loads(report_bytes)
    except (KeyError, ValueError, zipfile.BadZipFile) as exc:
        raise HTTPException(502, "cad-kernel вернул повреждённый assembly artifact") from exc
    if not report.get("reopen", {}).get("valid"):
        raise HTTPException(422, "STEP reopen verification failed")
    step_sha = hashlib.sha256(step_bytes).hexdigest()
    if report["reopen"].get("step_sha256") != step_sha:
        raise HTTPException(502, "STEP SHA не совпал с kernel report")
    artifact_path = f"engineering/assemblies/{assembly_id}/r{graph.revision}-{step_sha}.step"
    report_path = artifact_path.removesuffix(".step") + ".json"
    artifact_id = f"artifact:assembly-step:{step_sha[:20]}"
    operation_id = f"operation:assembly-compile:{step_sha[:20]}"
    nodes = [
        GraphNode(id=operation_id, type="BuildOperation", name="Compile assembly STEP"),
        GraphNode(id=artifact_id, type="Artifact", name="Assembly STEP"),
    ]
    edges = [
        GraphEdge(
            id=f"depends:{operation_id}",
            type="depends_on",
            source_id=operation_id,
            target_id="product:assembly",
        ),
        GraphEdge(
            id=f"generated:{artifact_id}",
            type="generated_by",
            source_id=artifact_id,
            target_id=operation_id,
        ),
    ]
    instance_subjects = {
        str(item.value.value): item.subject_id
        for item in graph.assertions
        if item.state == "active"
        and item.predicate == PREDICATE.COMPONENT_INSTANCE_KEY
        and item.value.kind == "exact"
    }
    for component_report in report.get("components", []):
        key = str(component_report.get("instance_key"))
        subject_id = instance_subjects.get(key)
        if subject_id is None:
            raise HTTPException(502, f"Kernel report содержит неизвестный instance {key}")
        topology_id = f"topology:solid:{hashlib.sha256(key.encode()).hexdigest()[:16]}"
        nodes.append(GraphNode(id=topology_id, type="TopologyElement", name=key))
        edges.append(
            GraphEdge(
                id=f"maps:{subject_id}:{topology_id}",
                type="maps_to_topology",
                source_id=subject_id,
                target_id=topology_id,
            )
        )
    evidence_id = f"evidence:assembly-reopen:{step_sha[:20]}"
    assertions = [
        Assertion(
            id=f"assertion:assembly-reopen:{step_sha[:20]}",
            subject_id="product:assembly",
            predicate=PREDICATE.ASSEMBLY_ARTIFACT_REOPEN_VALID,
            value=ExactValue(kind="exact", value=True),
            origin="derived",
            assurance="constraint_validated",
            evidence_ids=[evidence_id],
            confidence=1.0,
            impacts=["base_topology", "operational_safety"],
            supersedes_assertion_id=reopen_assertion.id,
        ),
        Assertion(
            id=f"assertion:artifact-sha:{step_sha[:20]}",
            subject_id=artifact_id,
            predicate=PREDICATE.ARTIFACT_SHA256,
            value=ExactValue(kind="exact", value=step_sha),
            origin="derived",
            assurance="constraint_validated",
            evidence_ids=[evidence_id],
            confidence=1.0,
            impacts=["base_topology"],
        ),
        Assertion(
            id=f"assertion:operation-kind:{step_sha[:20]}",
            subject_id=operation_id,
            predicate=PREDICATE.OPERATION_KIND,
            value=ExactValue(kind="exact", value="assembly_compile"),
            origin="derived",
            assurance="constraint_validated",
            evidence_ids=[evidence_id],
            confidence=1.0,
            impacts=["base_topology", "component_count"],
        ),
    ]
    patch = GraphPatch(
        patch_id=f"assembly-build:{step_sha}",
        base_revision=graph.revision,
        base_sha256=graph.canonical_sha256,
        producer="system",
        pass_id=f"assembly-build:r{graph.revision + 1}",
        idempotency_key=f"assembly-build:{graph.canonical_sha256}:{step_sha}",
        add_nodes=nodes,
        add_edges=edges,
        add_assertions=assertions,
        add_evidence=[
            Evidence(
                id=evidence_id,
                kind="kernel_topology",
                payload={
                    "artifact_path": artifact_path,
                    "report_path": report_path,
                    "report": report,
                },
                sha256=hashlib.sha256(report_bytes).hexdigest(),
            )
        ],
        supersede_assertion_ids=[reopen_assertion.id],
    )
    uploaded: list[str] = []
    try:
        upload_file(step_bytes, artifact_path, "model/step")
        uploaded.append(artifact_path)
        upload_file(report_bytes, report_path, "application/json")
        uploaded.append(report_path)
        row, errors = await merge_and_persist_patch(
            db,
            patch,
            expected_graph_id=graph_id,
        )
        if row is None:
            raise ValueError("Artifact GraphPatch отклонён: " + ", ".join(errors))
        await db.commit()
    except Exception as exc:
        await db.rollback()
        for path in reversed(uploaded):
            try:
                delete_file(path)
            except Exception:  # noqa: BLE001 - preserve the primary build error
                pass
        if isinstance(exc, ValueError):
            raise HTTPException(409, str(exc)) from exc
        raise
    built_graph = load_graph(row)
    built_plan = compile_build_plan(built_graph, "production")
    return {
        "graph_id": graph_id,
        "revision": row.revision,
        "canonical_sha256": row.canonical_sha256,
        "artifact_path": artifact_path,
        "artifact_sha256": step_sha,
        "report_path": report_path,
        "solid_count": report["reopen"]["solid_count"],
        "production_export_allowed": built_plan.production_export_allowed,
        "critical_assumption_ids": built_plan.critical_assumption_ids,
    }


@router.post("/assemblies/{assembly_id}/model-graph/drawing")
async def build_assembly_model_graph_drawing(
    assembly_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> dict:
    """Build, reopen, store and admit assembled plus exploded SVG views."""
    import hashlib

    from app.ai.assembly_emg import assembly_drawing_patch, build_assembly_drawing_svg
    from app.domain.engineering_model_graph import compile_build_plan
    from app.services.engineering_model_graph import latest_graph_revision, load_graph

    await sync_assembly_model_graph(assembly_id, db, user)
    assembly = await db.get(EngineeringAssembly, assembly_id)
    if assembly is None:
        raise HTTPException(404, "Сборка не найдена")
    components = list(
        (
            await db.execute(
                select(EngineeringAssemblyComponent).where(
                    EngineeringAssemblyComponent.engineering_assembly_id == assembly_id,
                    EngineeringAssemblyComponent.suppressed.is_(False),
                )
            )
        ).scalars()
    )
    mates = list(
        (
            await db.execute(
                select(EngineeringAssemblyMate).where(
                    EngineeringAssemblyMate.engineering_assembly_id == assembly_id,
                )
            )
        ).scalars()
    )
    if not components:
        raise HTTPException(409, "Сборка не содержит активных компонентов")
    graph_id = f"assembly:{assembly_id}"
    latest = await latest_graph_revision(db, graph_id, lock=True)
    if latest is None:
        raise HTTPException(409, "Assembly EngineeringModelGraph не создан")
    graph = load_graph(latest)
    try:
        svg, raw_report = build_assembly_drawing_svg(
            components=components, mates=mates, name=assembly.name
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    artifact_sha = hashlib.sha256(svg).hexdigest()
    base = f"engineering/assemblies/{assembly_id}/drawing-{artifact_sha}"
    report = _artifact_report_with_storage(
        raw_report,
        artifact_path=base + ".svg",
        report_path=base + ".json",
    )
    try:
        patch = assembly_drawing_patch(graph, svg=svg, report=report)
        row, built_graph, replay = await _persist_verified_svg_patch(
            db=db,
            latest_row=latest,
            graph=graph,
            graph_id=graph_id,
            svg=svg,
            report=report,
            patch=patch,
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    plan = compile_build_plan(built_graph, "production")
    return {
        "graph_id": graph_id,
        "revision": row.revision,
        "canonical_sha256": row.canonical_sha256,
        "artifact_path": report["artifact_path"],
        "artifact_sha256": artifact_sha,
        "report_path": report["report_path"],
        "views": report["views"],
        "production_export_allowed": plan.production_export_allowed,
        "critical_assumption_ids": plan.critical_assumption_ids,
        "idempotent_replay": replay,
    }


@router.post(
    "/construction/ifc/parse",
    summary="Skill: engineering.construction_ifc_parse — read IFC into a ConstructionModel.",
)
async def parse_construction_ifc(
    file: UploadFile = File(...),
    site_name: str | None = None,
    building_name: str | None = None,
) -> dict:
    """Фаза 5.1: promote the IFC reader from an offline script to a service call.

    Stateless — writes nothing to the database. The caller submits the
    returned `construction_model` as `payload.construction_model` when
    creating an EngineeringRevision (POST /projects/{id}/revisions), which
    the existing /construction-model-graph endpoints already consume
    unchanged. `model` is null when the file yields no storeys/elements or
    fails ConstructionModel's own validation (e.g. a rotated wall whose
    opening doesn't corner-contain in an axis-aligned box) — check `report`
    for why; nothing is ever guessed to force a result.
    """
    import functools
    import tempfile

    from anyio import to_thread

    from app.ai.ifc_reader import ifc_to_construction_model

    raw = await file.read()
    if not raw:
        raise HTTPException(422, "Пустой IFC файл")
    with tempfile.NamedTemporaryFile(suffix=".ifc") as tmp:
        tmp.write(raw)
        tmp.flush()
        try:
            model, report = await to_thread.run_sync(
                functools.partial(
                    ifc_to_construction_model,
                    pathlib.Path(tmp.name),
                    site_name=site_name,
                    building_name=building_name,
                ),
            )
        except Exception as exc:
            raise HTTPException(422, f"IfcOpenShell не смог прочитать файл: {exc}") from exc
    return {
        "construction_model": model.model_dump(mode="json") if model else None,
        "report": report,
    }


@router.post(
    "/construction/2d/parse",
    summary=(
        "Skill: engineering.construction_2d_parse — read a 2D floor plan into a ConstructionModel."
    ),
)
async def parse_construction_drawing(
    file: UploadFile = File(...),
    site_name: str | None = None,
    building_name: str | None = None,
    allow_cloud: bool = False,
) -> dict:
    """Ф5.2: VLM reads orthogonal wall/opening/storey semantics — the
    coordinates, thickness and height actually dimensioned on the sheet;
    deterministic code (`construction_reader.construction_read_as_model`,
    which reuses `ConstructionModel`'s OWN axis-aligned containment check)
    builds the geometry. A non-orthogonal wall or a wall with no readable
    height is excluded individually and reported — never guessed. Same
    stateless {construction_model, report} shape as
    `/construction/ifc/parse`: submit `construction_model` as
    `payload.construction_model` when creating a revision.

    No real architectural drawing corpus exists in this repository yet — see
    `construction_reader.py`'s own module docstring for that caveat.
    """
    from app.ai.construction_reader import read_construction_drawing

    raw = await file.read()
    if not raw:
        raise HTTPException(422, "Пустой файл чертежа")
    model, report = await read_construction_drawing(
        raw, allow_cloud=allow_cloud, site_name=site_name, building_name=building_name
    )
    return {
        "construction_model": model.model_dump(mode="json") if model else None,
        "report": report,
    }


@router.post(
    "/system/2d/parse",
    summary=(
        "Skill: engineering.system_2d_parse — read a P&ID/MEP/electrical/"
        "hydraulic diagram into an EngineeringSystemModel."
    ),
)
async def parse_system_drawing(
    file: UploadFile = File(...),
    profile: Literal["mep", "electrical", "hydraulic", "pid"] = Query(...),
    allow_cloud: bool = False,
) -> dict:
    """Ф5.3: single-pass VLM read of equipment/ports/connections — this
    domain carries no geometry, so unlike the mechanical/construction
    readers there is no separate deterministic geometry step; fail-closed
    gating instead comes straight from `EngineeringSystemModel`'s own
    validator (medium/direction compatibility, port cardinality), applied
    connection-by-connection in `system_reader.system_read_as_model` so one
    bad connection excludes only itself. Same stateless
    {system_model, report} shape the existing `/system-model-graph`
    endpoints already consume via `payload.system_model`.

    No real P&ID/MEP drawing corpus exists in this repository yet — see
    `system_reader.py`'s own module docstring for that caveat.
    """
    from app.ai.system_reader import read_system_diagram

    raw = await file.read()
    if not raw:
        raise HTTPException(422, "Пустой файл схемы")
    model, report = await read_system_diagram(raw, profile=profile, allow_cloud=allow_cloud)
    return {
        "system_model": model.model_dump(mode="json") if model else None,
        "report": report,
    }


@router.post("/revisions/{revision_id}/construction-model-graph")
async def sync_construction_model_graph(
    revision_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> dict:
    """Project an immutable construction_model payload into canonical EMG."""
    import hashlib

    require_permission(user, "engineering.revision_create")
    from app.ai.construction_emg import ConstructionModel, construction_as_graph
    from app.domain.engineering_model_graph import (
        Evidence,
        GraphPatch,
        graph_contract_upgrade_patch,
    )
    from app.services.engineering_model_graph import (
        create_initial_graph,
        latest_graph_revision,
        load_graph,
        merge_and_persist_patch,
    )

    revision = await db.get(EngineeringRevision, revision_id)
    if revision is None:
        raise HTTPException(404, "Инженерная ревизия не найдена")
    try:
        model = ConstructionModel.model_validate(revision.payload.get("construction_model"))
    except Exception as exc:
        raise HTTPException(422, f"construction_model невалиден: {exc}") from exc
    graph_id = f"construction:{revision_id}"
    desired = construction_as_graph(
        graph_id=graph_id,
        model=model,
        source_revision_id=str(revision_id),
        source_approved=revision.status == "approved",
    )
    latest = await latest_graph_revision(db, graph_id, lock=True)
    if latest is None:
        graph = desired
        latest = await create_initial_graph(
            db,
            graph,
            engineering_project_id=revision.engineering_project_id,
            engineering_revision_id=revision.id,
        )
        await db.commit()
    else:
        graph = load_graph(latest)
        upgrade = graph_contract_upgrade_patch(
            graph,
            desired,
            patch_prefix="construction-contract-upgrade",
        )
        if upgrade is not None:
            upgraded, errors = await merge_and_persist_patch(
                db,
                upgrade,
                expected_graph_id=graph_id,
            )
            if upgraded is None:
                await db.rollback()
                raise HTTPException(
                    409,
                    "Construction contract GraphPatch отклонён: " + ", ".join(errors),
                )
            await db.commit()
            latest = upgraded
            graph = load_graph(upgraded)
        approvable = [
            item
            for item in graph.assertions
            if item.state == "active"
            and item.origin == "human"
            and item.assurance == "observed"
            and item.value.kind != "unknown"
        ]
        if revision.status == "approved" and approvable:
            approval_seed = f"{revision_id}:{revision.approved_by or 'approved'}"
            approval_digest = hashlib.sha256(approval_seed.encode()).hexdigest()
            evidence_id = f"evidence:construction-approval:{approval_digest[:20]}"
            replacements = [
                item.model_copy(
                    update={
                        "id": f"assertion:construction-approved:{hashlib.sha256(item.id.encode()).hexdigest()[:20]}",
                        "assurance": "human_approved",
                        "evidence_ids": [evidence_id],
                        "supersedes_assertion_id": item.id,
                    }
                )
                for item in approvable
            ]
            patch = GraphPatch(
                patch_id=f"construction-approval:{approval_digest}",
                base_revision=graph.revision,
                base_sha256=graph.canonical_sha256,
                producer="human",
                pass_id=f"construction-approval:r{graph.revision + 1}",
                idempotency_key=f"construction-approval:{graph.canonical_sha256}:{approval_digest}",
                add_assertions=replacements,
                add_evidence=[
                    Evidence(
                        id=evidence_id,
                        kind="human_decision",
                        payload={
                            "engineering_revision_id": str(revision_id),
                            "approved_by": revision.approved_by,
                            "approved_at": (
                                revision.approved_at.isoformat() if revision.approved_at else None
                            ),
                        },
                        sha256=approval_digest,
                    )
                ],
                supersede_assertion_ids=[item.id for item in approvable],
            )
            row, errors = await merge_and_persist_patch(
                db,
                patch,
                expected_graph_id=graph_id,
            )
            if row is None:
                await db.rollback()
                raise HTTPException(409, "Approval GraphPatch отклонён: " + ", ".join(errors))
            await db.commit()
            latest = row
            graph = load_graph(row)
    return {
        "id": str(latest.id),
        "engineering_project_id": str(latest.engineering_project_id),
        "engineering_revision_id": str(latest.engineering_revision_id),
        "graph_id": latest.graph_id,
        "revision": latest.revision,
        "canonical_sha256": latest.canonical_sha256,
        "profile": latest.profile,
        "comprehension_status": latest.comprehension_status,
        "build_status": latest.build_status,
        "release_status": latest.release_status,
        "graph": graph.model_dump(mode="json"),
    }


@router.post("/revisions/{revision_id}/construction-model-graph/build")
async def build_construction_model_graph(
    revision_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> dict:
    """Build, reopen and attach a provisional or production IFC4 artifact."""
    import hashlib
    import json

    from anyio import to_thread

    from app.ai.construction_emg import ConstructionModel, compile_construction_ifc
    from app.domain.engineering_model_graph import (
        Assertion,
        Evidence,
        ExactValue,
        GraphEdge,
        GraphNode,
        GraphPatch,
        compile_build_plan,
    )
    from app.services.engineering_model_graph import (
        evaluate_build_admission,
        latest_graph_revision,
        load_graph,
        merge_and_persist_patch,
    )
    from app.storage import delete_file, upload_file

    await sync_construction_model_graph(revision_id, db, user)
    revision = await db.get(EngineeringRevision, revision_id)
    if revision is None:
        raise HTTPException(404, "Инженерная ревизия не найдена")
    model = ConstructionModel.model_validate(revision.payload["construction_model"])
    graph_id = f"construction:{revision_id}"
    latest = await latest_graph_revision(db, graph_id, lock=True)
    if latest is None:
        raise HTTPException(409, "Construction EngineeringModelGraph не создан")
    graph = load_graph(latest)
    reopen_assertion = next(
        (
            item
            for item in graph.assertions
            if item.state == "active" and item.predicate == PREDICATE.CONSTRUCTION_IFC_REOPEN_VALID
        ),
        None,
    )
    if reopen_assertion is None:
        raise HTTPException(409, "В construction graph отсутствует IFC reopen gate")
    if (
        reopen_assertion.value.kind == "exact"
        and reopen_assertion.value.value is True
        and reopen_assertion.evidence_ids
    ):
        evidence_by_id = {item.id: item for item in graph.evidence}
        evidence = evidence_by_id.get(reopen_assertion.evidence_ids[0])
        if evidence and evidence.payload.get("artifact_path"):
            plan = compile_build_plan(graph, "production")
            return {
                "graph_id": graph_id,
                "revision": latest.revision,
                "canonical_sha256": latest.canonical_sha256,
                "artifact_path": evidence.payload["artifact_path"],
                "artifact_sha256": evidence.payload["report"]["ifc_sha256"],
                "report_path": evidence.payload["report_path"],
                "ifc_reopen_valid": True,
                "production_export_allowed": plan.production_export_allowed,
                "provisional": not plan.production_export_allowed,
                "critical_assumption_ids": plan.critical_assumption_ids,
                "idempotent_replay": True,
            }

    pending_output_ids = {
        item.id
        for item in graph.assertions
        if item.state == "active"
        and item.predicate
        in {
            PREDICATE.CONSTRUCTION_IFC_REOPEN_VALID,
            PREDICATE.CONSTRUCTION_REQUIRED_SHEETS_COMPLETE,
        }
    }
    admission = evaluate_build_admission(
        graph,
        "production",
        "construction_ifc",
        pending_output_assertion_ids=pending_output_ids,
    )
    if not admission.allowed:
        raise HTTPException(409, detail=admission.model_dump(mode="json"))

    try:
        ifc_bytes, report = await to_thread.run_sync(
            compile_construction_ifc,
            model,
        )
    except Exception as exc:
        raise HTTPException(422, f"IfcOpenShell отклонил construction model: {exc}") from exc
    if not report.get("valid"):
        raise HTTPException(422, "IFC reopen verification failed: " + json.dumps(report)[:500])
    ifc_sha = hashlib.sha256(ifc_bytes).hexdigest()
    if report.get("ifc_sha256") != ifc_sha:
        raise HTTPException(502, "IFC SHA не совпал с reopen report")
    artifact_path = f"engineering/construction/{revision_id}/r{graph.revision}-{ifc_sha}.ifc"
    report_path = artifact_path.removesuffix(".ifc") + ".json"
    artifact_id = f"artifact:construction-ifc:{ifc_sha[:20]}"
    operation_id = f"operation:construction-ifc:{ifc_sha[:20]}"
    nodes = [
        GraphNode(id=operation_id, type="BuildOperation", name="Compile construction IFC4"),
        GraphNode(id=artifact_id, type="Artifact", name="Construction IFC4"),
    ]
    edges = [
        GraphEdge(
            id=f"depends:{operation_id}",
            type="depends_on",
            source_id=operation_id,
            target_id="product:building",
        ),
        GraphEdge(
            id=f"generated:{artifact_id}",
            type="generated_by",
            source_id=artifact_id,
            target_id=operation_id,
        ),
    ]
    graph_node_ids = {item.id for item in graph.nodes}
    for product in report.get("products", []):
        source_id = str(product.get("source_id") or "")
        subject_id = f"feature:construction:{hashlib.sha256(source_id.encode()).hexdigest()[:16]}"
        if subject_id not in graph_node_ids:
            raise HTTPException(502, f"IFC report содержит неизвестный source element {source_id}")
        topology_id = f"topology:ifc:{product['global_id']}"
        nodes.append(
            GraphNode(
                id=topology_id,
                type="TopologyElement",
                name=f"{product['ifc_class']} {product.get('name') or source_id}",
            )
        )
        edges.append(
            GraphEdge(
                id=f"maps:{subject_id}:{topology_id}",
                type="maps_to_topology",
                source_id=subject_id,
                target_id=topology_id,
            )
        )
    report_bytes = json.dumps(
        report,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    evidence_id = f"evidence:construction-ifc:{ifc_sha[:20]}"
    patch = GraphPatch(
        patch_id=f"construction-build:{ifc_sha}",
        base_revision=graph.revision,
        base_sha256=graph.canonical_sha256,
        producer="system",
        pass_id=f"construction-build:r{graph.revision + 1}",
        idempotency_key=f"construction-build:{graph.canonical_sha256}:{ifc_sha}",
        add_nodes=nodes,
        add_edges=edges,
        add_assertions=[
            Assertion(
                id=f"assertion:construction-ifc-reopen:{ifc_sha[:20]}",
                subject_id="product:building",
                predicate=PREDICATE.CONSTRUCTION_IFC_REOPEN_VALID,
                value=ExactValue(kind="exact", value=True),
                origin="derived",
                assurance="constraint_validated",
                evidence_ids=[evidence_id],
                confidence=1.0,
                impacts=["base_topology", "regulatory_check"],
                supersedes_assertion_id=reopen_assertion.id,
            ),
            Assertion(
                id=f"assertion:construction-ifc-sha:{ifc_sha[:20]}",
                subject_id=artifact_id,
                predicate=PREDICATE.ARTIFACT_SHA256,
                value=ExactValue(kind="exact", value=ifc_sha),
                origin="derived",
                assurance="constraint_validated",
                evidence_ids=[evidence_id],
                confidence=1.0,
                impacts=["base_topology"],
            ),
        ],
        add_evidence=[
            Evidence(
                id=evidence_id,
                kind="kernel_topology",
                payload={
                    "artifact_path": artifact_path,
                    "report_path": report_path,
                    "report": report,
                },
                sha256=hashlib.sha256(report_bytes).hexdigest(),
            )
        ],
        supersede_assertion_ids=[reopen_assertion.id],
    )
    uploaded: list[str] = []
    try:
        await to_thread.run_sync(upload_file, ifc_bytes, artifact_path, "application/x-step")
        uploaded.append(artifact_path)
        await to_thread.run_sync(upload_file, report_bytes, report_path, "application/json")
        uploaded.append(report_path)
        row, errors = await merge_and_persist_patch(
            db,
            patch,
            expected_graph_id=graph_id,
        )
        if row is None:
            raise ValueError("Artifact GraphPatch отклонён: " + ", ".join(errors))
        await db.commit()
    except Exception as exc:
        await db.rollback()
        for path in reversed(uploaded):
            try:
                await to_thread.run_sync(delete_file, path)
            except Exception:  # noqa: BLE001 - preserve primary build error
                pass
        if isinstance(exc, ValueError):
            raise HTTPException(409, str(exc)) from exc
        raise
    built_graph = load_graph(row)
    plan = compile_build_plan(built_graph, "production")
    return {
        "graph_id": graph_id,
        "revision": row.revision,
        "canonical_sha256": row.canonical_sha256,
        "artifact_path": artifact_path,
        "artifact_sha256": ifc_sha,
        "report_path": report_path,
        "ifc_reopen_valid": True,
        "product_class_counts": report["product_class_counts"],
        "production_export_allowed": plan.production_export_allowed,
        "provisional": not plan.production_export_allowed,
        "critical_assumption_ids": plan.critical_assumption_ids,
        "idempotent_replay": False,
    }


@router.post("/revisions/{revision_id}/construction-model-graph/sheets")
async def build_construction_model_graph_sheets(
    revision_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> dict:
    """Build, reopen, store and admit every storey plan plus a section."""
    import hashlib

    from app.ai.construction_emg import (
        ConstructionModel,
        build_construction_sheets_svg,
        construction_sheets_patch,
    )
    from app.domain.engineering_model_graph import compile_build_plan
    from app.services.engineering_model_graph import latest_graph_revision, load_graph

    await sync_construction_model_graph(revision_id, db, user)
    revision = await db.get(EngineeringRevision, revision_id)
    if revision is None:
        raise HTTPException(404, "Инженерная ревизия не найдена")
    try:
        model = ConstructionModel.model_validate(revision.payload.get("construction_model"))
    except Exception as exc:
        raise HTTPException(422, f"construction_model невалиден: {exc}") from exc
    graph_id = f"construction:{revision_id}"
    latest = await latest_graph_revision(db, graph_id, lock=True)
    if latest is None:
        raise HTTPException(409, "Construction EngineeringModelGraph не создан")
    graph = load_graph(latest)
    svg, raw_report = build_construction_sheets_svg(model)
    artifact_sha = hashlib.sha256(svg).hexdigest()
    base = f"engineering/construction/{revision_id}/sheets-{artifact_sha}"
    report = _artifact_report_with_storage(
        raw_report,
        artifact_path=base + ".svg",
        report_path=base + ".json",
    )
    try:
        patch = construction_sheets_patch(graph, svg=svg, report=report)
        row, built_graph, replay = await _persist_verified_svg_patch(
            db=db,
            latest_row=latest,
            graph=graph,
            graph_id=graph_id,
            svg=svg,
            report=report,
            patch=patch,
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    plan = compile_build_plan(built_graph, "production")
    return {
        "graph_id": graph_id,
        "revision": row.revision,
        "canonical_sha256": row.canonical_sha256,
        "artifact_path": report["artifact_path"],
        "artifact_sha256": artifact_sha,
        "report_path": report["report_path"],
        "views": report["views"],
        "production_export_allowed": plan.production_export_allowed,
        "critical_assumption_ids": plan.critical_assumption_ids,
        "idempotent_replay": replay,
    }


@router.post("/revisions/{revision_id}/system-model-graph")
async def sync_system_model_graph(
    revision_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> dict:
    """Project MEP/P&ID/electrical/hydraulic ports into immutable EMG."""
    import hashlib

    require_permission(user, "engineering.revision_create")
    from app.ai.system_emg import EngineeringSystemModel, system_as_graph
    from app.domain.engineering_model_graph import (
        Evidence,
        GraphPatch,
        graph_contract_upgrade_patch,
    )
    from app.services.engineering_model_graph import (
        create_initial_graph,
        latest_graph_revision,
        load_graph,
        merge_and_persist_patch,
    )

    revision = await db.get(EngineeringRevision, revision_id)
    if revision is None:
        raise HTTPException(404, "Инженерная ревизия не найдена")
    try:
        model = EngineeringSystemModel.model_validate(revision.payload.get("system_model"))
    except Exception as exc:
        raise HTTPException(422, f"system_model невалиден: {exc}") from exc
    graph_id = f"system:{revision_id}"
    desired = system_as_graph(
        graph_id=graph_id,
        model=model,
        source_revision_id=str(revision_id),
        source_approved=revision.status == "approved",
    )
    latest = await latest_graph_revision(db, graph_id, lock=True)
    if latest is None:
        graph = desired
        latest = await create_initial_graph(
            db,
            graph,
            engineering_project_id=revision.engineering_project_id,
            engineering_revision_id=revision.id,
        )
        await db.commit()
    else:
        graph = load_graph(latest)
        upgrade = graph_contract_upgrade_patch(
            graph,
            desired,
            patch_prefix="system-contract-upgrade",
        )
        if upgrade is not None:
            upgraded, errors = await merge_and_persist_patch(
                db,
                upgrade,
                expected_graph_id=graph_id,
            )
            if upgraded is None:
                await db.rollback()
                raise HTTPException(
                    409,
                    "System contract GraphPatch отклонён: " + ", ".join(errors),
                )
            await db.commit()
            latest = upgraded
            graph = load_graph(upgraded)
        approvable = [
            item
            for item in graph.assertions
            if item.state == "active"
            and item.origin == "human"
            and item.assurance == "observed"
            and item.value.kind != "unknown"
        ]
        if revision.status == "approved" and approvable:
            approval_seed = f"system:{revision_id}:{revision.approved_by or 'approved'}"
            approval_digest = hashlib.sha256(approval_seed.encode()).hexdigest()
            evidence_id = f"evidence:system-approval:{approval_digest[:20]}"
            replacements = [
                item.model_copy(
                    update={
                        "id": f"assertion:system-approved:{hashlib.sha256(item.id.encode()).hexdigest()[:20]}",
                        "assurance": "human_approved",
                        "evidence_ids": [evidence_id],
                        "supersedes_assertion_id": item.id,
                    }
                )
                for item in approvable
            ]
            patch = GraphPatch(
                patch_id=f"system-approval:{approval_digest}",
                base_revision=graph.revision,
                base_sha256=graph.canonical_sha256,
                producer="human",
                pass_id=f"system-approval:r{graph.revision + 1}",
                idempotency_key=f"system-approval:{graph.canonical_sha256}:{approval_digest}",
                add_assertions=replacements,
                add_evidence=[
                    Evidence(
                        id=evidence_id,
                        kind="human_decision",
                        payload={
                            "engineering_revision_id": str(revision_id),
                            "approved_by": revision.approved_by,
                            "approved_at": (
                                revision.approved_at.isoformat() if revision.approved_at else None
                            ),
                        },
                        sha256=approval_digest,
                    )
                ],
                supersede_assertion_ids=[item.id for item in approvable],
            )
            row, errors = await merge_and_persist_patch(
                db,
                patch,
                expected_graph_id=graph_id,
            )
            if row is None:
                await db.rollback()
                raise HTTPException(409, "Approval GraphPatch отклонён: " + ", ".join(errors))
            await db.commit()
            latest = row
            graph = load_graph(row)
    from app.domain.engineering_model_graph import compile_build_plan

    production = compile_build_plan(graph, "production")
    return {
        "id": str(latest.id),
        "engineering_project_id": str(latest.engineering_project_id),
        "engineering_revision_id": str(latest.engineering_revision_id),
        "graph_id": latest.graph_id,
        "revision": latest.revision,
        "canonical_sha256": latest.canonical_sha256,
        "profile": latest.profile,
        "comprehension_status": latest.comprehension_status,
        "build_status": latest.build_status,
        "release_status": latest.release_status,
        "production_export_allowed": production.production_export_allowed,
        "critical_assumption_ids": production.critical_assumption_ids,
        "graph": graph.model_dump(mode="json"),
    }


@router.post("/revisions/{revision_id}/system-model-graph/diagram")
async def build_system_model_graph_diagram(
    revision_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> dict:
    """Build, reopen, store and admit a connectivity-complete system SVG."""
    import hashlib

    from app.ai.system_emg import (
        EngineeringSystemModel,
        build_system_diagram_svg,
        system_diagram_patch,
    )
    from app.domain.engineering_model_graph import compile_build_plan
    from app.services.engineering_model_graph import latest_graph_revision, load_graph

    await sync_system_model_graph(revision_id, db, user)
    revision = await db.get(EngineeringRevision, revision_id)
    if revision is None:
        raise HTTPException(404, "Инженерная ревизия не найдена")
    try:
        model = EngineeringSystemModel.model_validate(revision.payload.get("system_model"))
    except Exception as exc:
        raise HTTPException(422, f"system_model невалиден: {exc}") from exc
    graph_id = f"system:{revision_id}"
    latest = await latest_graph_revision(db, graph_id, lock=True)
    if latest is None:
        raise HTTPException(409, "System EngineeringModelGraph не создан")
    graph = load_graph(latest)
    svg, raw_report = build_system_diagram_svg(model)
    artifact_sha = hashlib.sha256(svg).hexdigest()
    base = f"engineering/systems/{revision_id}/diagram-{artifact_sha}"
    report = _artifact_report_with_storage(
        raw_report,
        artifact_path=base + ".svg",
        report_path=base + ".json",
    )
    try:
        patch = system_diagram_patch(graph, svg=svg, report=report)
        row, built_graph, replay = await _persist_verified_svg_patch(
            db=db,
            latest_row=latest,
            graph=graph,
            graph_id=graph_id,
            svg=svg,
            report=report,
            patch=patch,
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    plan = compile_build_plan(built_graph, "production")
    return {
        "graph_id": graph_id,
        "revision": row.revision,
        "canonical_sha256": row.canonical_sha256,
        "artifact_path": report["artifact_path"],
        "artifact_sha256": artifact_sha,
        "report_path": report["report_path"],
        "views": ["system-diagram"],
        "production_export_allowed": plan.production_export_allowed,
        "critical_assumption_ids": plan.critical_assumption_ids,
        "idempotent_replay": replay,
    }


@router.post("/revisions/{revision_id}/mixed-model-graph")
async def sync_mixed_model_graph(
    revision_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> dict:
    """Compose pinned profile revisions and explicit cross-profile dependencies."""
    import hashlib

    require_permission(user, "engineering.revision_create")
    from app.ai.mixed_emg import MixedModel, compose_mixed_graph
    from app.db.models import EngineeringGraphRevision
    from app.domain.engineering_model_graph import Evidence, GraphPatch, compile_build_plan
    from app.services.engineering_model_graph import (
        create_initial_graph,
        latest_graph_revision,
        load_graph,
        merge_and_persist_patch,
    )

    revision = await db.get(EngineeringRevision, revision_id)
    if revision is None:
        raise HTTPException(404, "Инженерная ревизия не найдена")
    try:
        model = MixedModel.model_validate(revision.payload.get("mixed_model"))
    except Exception as exc:
        raise HTTPException(422, f"mixed_model невалиден: {exc}") from exc
    member_graphs = {}
    for member in model.members:
        row = (
            await db.execute(
                select(EngineeringGraphRevision).where(
                    EngineeringGraphRevision.graph_id == member.graph_id,
                    EngineeringGraphRevision.revision == member.revision,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(
                409,
                f"Member graph {member.alias} revision не найден",
            )
        if row.engineering_project_id != revision.engineering_project_id:
            raise HTTPException(409, f"Member graph {member.alias} принадлежит другому проекту")
        if row.canonical_sha256 != member.canonical_sha256:
            raise HTTPException(409, f"Member graph {member.alias} canonical SHA устарел")
        member_graphs[member.alias] = load_graph(row)

    graph_id = f"mixed:{revision_id}"
    latest = await latest_graph_revision(db, graph_id, lock=True)
    if latest is None:
        try:
            graph = compose_mixed_graph(
                graph_id=graph_id,
                model=model,
                member_graphs=member_graphs,
                source_revision_id=str(revision_id),
                source_approved=revision.status == "approved",
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        latest = await create_initial_graph(
            db,
            graph,
            engineering_project_id=revision.engineering_project_id,
            engineering_revision_id=revision.id,
        )
        await db.commit()
    else:
        graph = load_graph(latest)
        approvable = [
            item
            for item in graph.assertions
            if item.state == "active"
            and item.predicate == PREDICATE.CROSS_PROFILE_LINK
            and item.origin == "human"
            and item.assurance == "observed"
        ]
        if revision.status == "approved" and approvable:
            approval_seed = f"mixed:{revision_id}:{revision.approved_by or 'approved'}"
            approval_digest = hashlib.sha256(approval_seed.encode()).hexdigest()
            evidence_id = f"evidence:mixed-approval:{approval_digest[:20]}"
            replacements = [
                item.model_copy(
                    update={
                        "id": f"assertion:mixed-approved:{hashlib.sha256(item.id.encode()).hexdigest()[:20]}",
                        "assurance": "human_approved",
                        "evidence_ids": [evidence_id],
                        "supersedes_assertion_id": item.id,
                    }
                )
                for item in approvable
            ]
            patch = GraphPatch(
                patch_id=f"mixed-approval:{approval_digest}",
                base_revision=graph.revision,
                base_sha256=graph.canonical_sha256,
                producer="human",
                pass_id=f"mixed-approval:r{graph.revision + 1}",
                idempotency_key=f"mixed-approval:{graph.canonical_sha256}:{approval_digest}",
                add_assertions=replacements,
                add_evidence=[
                    Evidence(
                        id=evidence_id,
                        kind="human_decision",
                        payload={
                            "engineering_revision_id": str(revision_id),
                            "approved_by": revision.approved_by,
                            "approved_at": (
                                revision.approved_at.isoformat() if revision.approved_at else None
                            ),
                        },
                        sha256=approval_digest,
                    )
                ],
                supersede_assertion_ids=[item.id for item in approvable],
            )
            row, errors = await merge_and_persist_patch(
                db,
                patch,
                expected_graph_id=graph_id,
            )
            if row is None:
                await db.rollback()
                raise HTTPException(409, "Approval GraphPatch отклонён: " + ", ".join(errors))
            await db.commit()
            latest = row
            graph = load_graph(row)
    production = compile_build_plan(graph, "production")
    return {
        "id": str(latest.id),
        "engineering_project_id": str(latest.engineering_project_id),
        "engineering_revision_id": str(latest.engineering_revision_id),
        "graph_id": latest.graph_id,
        "revision": latest.revision,
        "canonical_sha256": latest.canonical_sha256,
        "profile": latest.profile,
        "comprehension_status": latest.comprehension_status,
        "build_status": latest.build_status,
        "release_status": latest.release_status,
        "production_export_allowed": production.production_export_allowed,
        "critical_assumption_ids": production.critical_assumption_ids,
        "member_revisions": [
            {
                "alias": item.alias,
                "graph_id": item.graph_id,
                "revision": item.revision,
                "canonical_sha256": item.canonical_sha256,
            }
            for item in sorted(model.members, key=lambda member: member.alias)
        ],
        "graph": graph.model_dump(mode="json"),
    }


@router.post("/revisions/{revision_id}/mixed-model-graph/bundle")
async def build_mixed_model_bundle(
    revision_id: uuid.UUID,
    mode: str = "provisional",
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> dict:
    """Create a deterministic ZIP of pinned graphs and verified domain artifacts."""
    if mode not in {"provisional", "production"}:
        raise HTTPException(422, "mode должен быть provisional или production")
    import hashlib

    from anyio import to_thread

    from app.ai.mixed_bundle import (
        build_mixed_artifact_bundle,
        mixed_bundle_fingerprint,
    )
    from app.ai.mixed_emg import MixedModel
    from app.db.models import EngineeringGraphRevision
    from app.domain.engineering_model_graph import (
        Assertion,
        Evidence,
        ExactValue,
        GraphEdge,
        GraphNode,
        GraphPatch,
        compile_build_plan,
    )
    from app.services.engineering_model_graph import (
        latest_graph_revision,
        load_graph,
        merge_and_persist_patch,
    )
    from app.storage import delete_file, download_file, upload_file

    await sync_mixed_model_graph(revision_id, db, user)
    revision = await db.get(EngineeringRevision, revision_id)
    if revision is None:
        raise HTTPException(404, "Инженерная ревизия не найдена")
    model = MixedModel.model_validate(revision.payload["mixed_model"])
    mixed_row = await latest_graph_revision(db, f"mixed:{revision_id}", lock=True)
    if mixed_row is None:
        raise HTTPException(409, "Mixed EngineeringModelGraph не создан")
    mixed_graph = load_graph(mixed_row)
    members = {}
    for member in model.members:
        row = (
            await db.execute(
                select(EngineeringGraphRevision).where(
                    EngineeringGraphRevision.graph_id == member.graph_id,
                    EngineeringGraphRevision.revision == member.revision,
                    EngineeringGraphRevision.canonical_sha256 == member.canonical_sha256,
                )
            )
        ).scalar_one_or_none()
        if row is None or row.engineering_project_id != revision.engineering_project_id:
            raise HTTPException(409, f"Pinned member {member.alias} больше недоступен")
        members[member.alias] = load_graph(row)
    production_plan = compile_build_plan(mixed_graph, "production")
    if mode == "production" and not production_plan.production_export_allowed:
        raise HTTPException(
            409,
            "Production bundle заблокирован critical assertions: "
            + ", ".join(production_plan.critical_assumption_ids),
        )
    fingerprint = mixed_bundle_fingerprint(mixed_graph, members, mode)
    replay = next(
        (
            item
            for item in reversed(mixed_graph.evidence)
            if item.kind == "calculation"
            and item.payload.get("bundle_kind") == "mixed_artifact_bundle"
            and item.payload.get("input_fingerprint") == fingerprint
            and item.payload.get("mode") == mode
        ),
        None,
    )
    if replay is not None:
        return {
            "graph_id": mixed_graph.graph_id,
            "revision": mixed_row.revision,
            "canonical_sha256": mixed_row.canonical_sha256,
            "mode": mode,
            "bundle_path": replay.payload["bundle_path"],
            "manifest_path": replay.payload["manifest_path"],
            "bundle_sha256": replay.payload["bundle_sha256"],
            "complete": replay.payload["complete"],
            "missing_required_artifacts": replay.payload["missing_required_artifacts"],
            "production_export_allowed": production_plan.production_export_allowed,
            "idempotent_replay": True,
        }
    try:
        bundle_bytes, manifest_bytes, manifest = await to_thread.run_sync(
            lambda: build_mixed_artifact_bundle(
                graph=mixed_graph,
                members=members,
                mode=mode,
                load_artifact=download_file,
            )
        )
    except (KeyError, ValueError) as exc:
        raise HTTPException(409, f"Bundle verification failed: {exc}") from exc
    if mode == "production" and not manifest["complete"]:
        missing = ", ".join(
            f"{item['member']}:{item['reason']}" for item in manifest["missing_required_artifacts"]
        )
        raise HTTPException(409, "Production bundle неполон: " + missing)
    bundle_sha = manifest["bundle_sha256"]
    base = f"engineering/mixed/{revision_id}/{mode}-{bundle_sha}"
    bundle_path = base + ".zip"
    manifest_path = base + ".manifest.json"
    operation_id = f"operation:mixed-bundle:{bundle_sha[:20]}"
    artifact_id = f"artifact:mixed-bundle:{bundle_sha[:20]}"
    evidence_id = f"evidence:mixed-bundle:{bundle_sha[:20]}"
    patch = GraphPatch(
        patch_id=f"mixed-bundle:{mode}:{bundle_sha}",
        base_revision=mixed_graph.revision,
        base_sha256=mixed_graph.canonical_sha256,
        producer="system",
        pass_id=f"mixed-bundle:r{mixed_graph.revision + 1}",
        idempotency_key=f"mixed-bundle:{fingerprint}",
        add_nodes=[
            GraphNode(id=operation_id, type="BuildOperation", name="Build mixed artifact bundle"),
            GraphNode(id=artifact_id, type="Artifact", name=f"Mixed {mode} bundle"),
        ],
        add_edges=[
            GraphEdge(
                id=f"depends:{operation_id}",
                type="depends_on",
                source_id=operation_id,
                target_id="document-set:mixed",
            ),
            GraphEdge(
                id=f"generated:{artifact_id}",
                type="generated_by",
                source_id=artifact_id,
                target_id=operation_id,
            ),
        ],
        add_assertions=[
            Assertion(
                id=f"assertion:mixed-bundle-sha:{bundle_sha[:20]}",
                subject_id=artifact_id,
                predicate=PREDICATE.ARTIFACT_BUNDLE_SHA256,
                value=ExactValue(kind="exact", value=bundle_sha),
                origin="derived",
                assurance="constraint_validated",
                evidence_ids=[evidence_id],
                confidence=1.0,
            ),
            Assertion(
                id=f"assertion:mixed-bundle-complete:{bundle_sha[:20]}",
                subject_id=artifact_id,
                predicate=PREDICATE.ARTIFACT_BUNDLE_COMPLETE,
                value=ExactValue(kind="exact", value=manifest["complete"]),
                origin="derived",
                assurance="constraint_validated",
                evidence_ids=[evidence_id],
                confidence=1.0,
            ),
        ],
        add_evidence=[
            Evidence(
                id=evidence_id,
                kind="calculation",
                payload={
                    "bundle_kind": "mixed_artifact_bundle",
                    "mode": mode,
                    "input_fingerprint": fingerprint,
                    "bundle_path": bundle_path,
                    "manifest_path": manifest_path,
                    "bundle_sha256": bundle_sha,
                    "complete": manifest["complete"],
                    "missing_required_artifacts": manifest["missing_required_artifacts"],
                },
                sha256=hashlib.sha256(manifest_bytes).hexdigest(),
            )
        ],
    )
    uploaded = []
    try:
        await to_thread.run_sync(upload_file, bundle_bytes, bundle_path, "application/zip")
        uploaded.append(bundle_path)
        await to_thread.run_sync(upload_file, manifest_bytes, manifest_path, "application/json")
        uploaded.append(manifest_path)
        row, errors = await merge_and_persist_patch(
            db,
            patch,
            expected_graph_id=mixed_graph.graph_id,
        )
        if row is None:
            raise ValueError("Bundle GraphPatch отклонён: " + ", ".join(errors))
        await db.commit()
    except Exception as exc:
        await db.rollback()
        for path in reversed(uploaded):
            try:
                await to_thread.run_sync(delete_file, path)
            except Exception:  # noqa: BLE001 - preserve primary bundle error
                pass
        if isinstance(exc, ValueError):
            raise HTTPException(409, str(exc)) from exc
        raise
    return {
        "graph_id": mixed_graph.graph_id,
        "revision": row.revision,
        "canonical_sha256": row.canonical_sha256,
        "mode": mode,
        "bundle_path": bundle_path,
        "manifest_path": manifest_path,
        "bundle_sha256": bundle_sha,
        "complete": manifest["complete"],
        "missing_required_artifacts": manifest["missing_required_artifacts"],
        "production_export_allowed": production_plan.production_export_allowed,
        "idempotent_replay": False,
    }


@router.get("/revisions/{revision_id}/mixed-model-graph/bundle")
async def download_mixed_model_bundle(
    revision_id: uuid.UUID,
    mode: str = "provisional",
    db: AsyncSession = Depends(get_db),
):
    """Download the latest recorded coordinated bundle for a mixed revision."""
    from anyio import to_thread
    from fastapi.responses import Response

    from app.services.engineering_model_graph import latest_graph_revision, load_graph
    from app.storage import download_file

    row = await latest_graph_revision(db, f"mixed:{revision_id}")
    if row is None:
        raise HTTPException(404, "Mixed EngineeringModelGraph не найден")
    graph = load_graph(row)
    evidence = next(
        (
            item
            for item in reversed(graph.evidence)
            if item.kind == "calculation"
            and item.payload.get("bundle_kind") == "mixed_artifact_bundle"
            and item.payload.get("mode") == mode
        ),
        None,
    )
    if evidence is None:
        raise HTTPException(404, "Bundle для выбранного mode не найден")
    content = await to_thread.run_sync(download_file, evidence.payload["bundle_path"])
    return Response(
        content=content,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="mixed-{revision_id}-{mode}.zip"',
            "X-Engineering-Artifact-SHA256": evidence.payload["bundle_sha256"],
            "X-Engineering-Artifact-Status": mode,
        },
    )


@router.get(
    "/revisions/{revision_id}/validation-runs", response_model=list[EngineeringValidationRunOut]
)
async def list_validation_runs(
    revision_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> list[EngineeringValidationRun]:
    return list(
        (
            await db.execute(
                select(EngineeringValidationRun)
                .where(EngineeringValidationRun.engineering_revision_id == revision_id)
                .order_by(EngineeringValidationRun.created_at.desc())
            )
        ).scalars()
    )


@router.post("/revisions/{revision_id}/validate", response_model=EngineeringValidationRunOut)
async def validate_revision(
    revision_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> EngineeringValidationRun:
    """Aggregate deterministic CAD, assembly and technology findings for release."""
    revision = await db.get(EngineeringRevision, revision_id)
    if not revision:
        raise HTTPException(404, "Инженерная ревизия не найдена")
    findings = list((revision.validation or {}).get("issues", []))
    assemblies = list(
        (
            await db.execute(
                select(EngineeringAssembly).where(
                    EngineeringAssembly.engineering_revision_id == revision_id
                )
            )
        ).scalars()
    )
    for assembly in assemblies:
        components = list(
            (
                await db.execute(
                    select(EngineeringAssemblyComponent).where(
                        EngineeringAssemblyComponent.engineering_assembly_id == assembly.id
                    )
                )
            ).scalars()
        )
        for index, first in enumerate(components):
            for second in components[index + 1 :]:
                if (
                    not first.suppressed
                    and not second.suppressed
                    and first.bounds
                    and second.bounds
                    and _overlap(first.bounds, second.bounds)
                ):
                    findings.append(
                        {
                            "code": "ASSEMBLY_INTERFERENCE",
                            "severity": "error",
                            "entity_ids": [str(first.id), str(second.id)],
                            "message_ru": f"Коллизия {first.instance_key} / {second.instance_key}",
                            "level": 2,
                        }
                    )
    projections = list(
        (
            await db.execute(
                select(EngineeringProjection).where(
                    EngineeringProjection.engineering_revision_id == revision_id
                )
            )
        ).scalars()
    )
    cad_revision_ids = [
        item.entity_id for item in projections if item.entity_type == "cad_ir_revision"
    ]
    if cad_revision_ids:
        cad_revisions = list(
            (
                await db.execute(
                    select(CadIrRevision).where(CadIrRevision.id.in_(cad_revision_ids))
                )
            ).scalars()
        )
        approved_cad_ids = {
            item.id for item in cad_revisions if item.approved_by and item.approved_at
        }
        for cad_revision_id in cad_revision_ids:
            if cad_revision_id not in approved_cad_ids:
                findings.append(
                    {
                        "code": "CAD_IR_NOT_APPROVED",
                        "severity": "error",
                        "entity_ids": [str(cad_revision_id)],
                        "message_ru": "Связанная CAD IR ревизия не принята человеком",
                        "level": 2,
                    }
                )
    plan_ids = [
        item.entity_id for item in projections if item.entity_type == "manufacturing_process_plan"
    ]
    if plan_ids:
        checks = list(
            (
                await db.execute(
                    select(ManufacturingCheckResult).where(
                        ManufacturingCheckResult.process_plan_id.in_(plan_ids),
                        ManufacturingCheckResult.status == "open",
                    )
                )
            ).scalars()
        )
        findings.extend(
            {
                "code": check.check_code,
                "severity": "error" if check.severity in {"critical", "error"} else "warn",
                "entity_ids": [],
                "message_ru": check.message,
                "level": 5,
            }
            for check in checks
        )
    analysis_cases = list(
        (
            await db.execute(
                select(EngineeringAnalysisCase).where(
                    EngineeringAnalysisCase.engineering_revision_id == revision_id
                )
            )
        ).scalars()
    )
    for case in analysis_cases:
        if case.status == "failed":
            findings.append(
                {
                    "code": "ANALYSIS_FAILED",
                    "severity": "error",
                    "entity_ids": [str(case.id)],
                    "message_ru": f"Расчет {case.name} не прошел критерий прочности",
                    "level": 2,
                }
            )
    blocked = any(item.get("severity") == "error" for item in findings if isinstance(item, dict))
    run = EngineeringValidationRun(
        engineering_revision_id=revision_id,
        status="failed" if blocked else "passed",
        findings=findings,
        summary={
            "total": len(findings),
            "errors": sum(
                item.get("severity") == "error" for item in findings if isinstance(item, dict)
            ),
        },
    )
    db.add(run)
    revision.validation = {"issues": findings}
    revision.status = "needs_review" if blocked else "validated"
    await db.commit()
    await db.refresh(run)
    return run


@router.get(
    "/revisions/{revision_id}/analysis-cases", response_model=list[EngineeringAnalysisCaseOut]
)
async def list_analysis_cases(
    revision_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> list[EngineeringAnalysisCase]:
    return list(
        (
            await db.execute(
                select(EngineeringAnalysisCase).where(
                    EngineeringAnalysisCase.engineering_revision_id == revision_id
                )
            )
        ).scalars()
    )


@router.post(
    "/revisions/{revision_id}/analysis-cases",
    response_model=EngineeringAnalysisCaseOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_analysis_case(
    revision_id: uuid.UUID, body: EngineeringAnalysisCaseCreate, db: AsyncSession = Depends(get_db)
) -> EngineeringAnalysisCase:
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
        raise HTTPException(
            422,
            f"Для типа {case.analysis_type!r} нет solver; доступны: {', '.join(sorted(SOLVERS))}",
        )
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
        from app.core import metrics

        metrics.cad_solver_runs_total.labels(
            analysis_type=case.analysis_type, status="invalid_input"
        ).inc()
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
    from app.core import metrics

    metrics.cad_solver_runs_total.labels(analysis_type=case.analysis_type, status=run.status).inc()
    await db.commit()
    await db.refresh(case)
    return case


@router.get("/analysis-cases/{case_id}/runs", response_model=list[EngineeringAnalysisRunOut])
async def list_analysis_runs(
    case_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> list[EngineeringAnalysisRun]:
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
    await log_action(
        db,
        action="engineering.revision.approve",
        entity_type="engineering_revision",
        entity_id=revision.id,
        user_id=body.approved_by,
    )
    await add_timeline_event(
        db,
        entity_type="engineering_revision",
        entity_id=revision.id,
        event_type="approved",
        summary="Инженерная ревизия утверждена",
        actor=body.approved_by,
    )
    await db.commit()
    await db.refresh(revision)
    return revision


# ── E3: change management ─────────────────────────────────────────────────────


async def _change_impact(db: AsyncSession, revision: EngineeringRevision) -> dict:
    """Auto impact analysis of the affected revision — plain data, no LLM:
    what depends on this geometry and would go stale if it changes."""
    projections = (
        (
            await db.execute(
                select(EngineeringProjection).where(
                    EngineeringProjection.engineering_revision_id == revision.id
                )
            )
        )
        .scalars()
        .all()
    )
    assemblies = (
        (
            await db.execute(
                select(EngineeringAssembly).where(
                    EngineeringAssembly.engineering_revision_id == revision.id
                )
            )
        )
        .scalars()
        .all()
    )
    analysis_cases = (
        (
            await db.execute(
                select(EngineeringAnalysisCase).where(
                    EngineeringAnalysisCase.engineering_revision_id == revision.id
                )
            )
        )
        .scalars()
        .all()
    )
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
                {
                    "type": p.projection_type,
                    "entity_type": p.entity_type,
                    "entity_id": str(p.entity_id),
                }
                for p in projections
            ],
        },
        "assemblies": len(assemblies),
        "analysis_cases": len(analysis_cases),
        "last_validation_status": last_run.status if last_run else None,
    }


@router.post(
    "/projects/{project_id}/change-requests",
    response_model=ChangeRequestOut,
    status_code=status.HTTP_201_CREATED,
)
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
            raise HTTPException(
                409,
                "Применённый запрос изменения нельзя заменить — создайте новый поверх его ревизии",
            )
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
    await log_action(
        db,
        action="engineering.change_request.create",
        entity_type="engineering_change_request",
        entity_id=change.id,
        user_id=body.created_by,
    )
    await add_timeline_event(
        db,
        entity_type="engineering_change_request",
        entity_id=change.id,
        event_type="created",
        summary=f"Запрос изменения №{change.number}: {change.title}",
        actor=body.created_by,
    )
    await db.commit()
    await db.refresh(change)
    return change


@router.get("/projects/{project_id}/change-requests", response_model=list[ChangeRequestOut])
async def list_change_requests(
    project_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> list[EngineeringChangeRequest]:
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
    elif {s["reviewer"] for s in change.signatures if s["decision"] == "approve"} >= set(
        change.reviewers
    ):
        change.status = "approved"
        change.decided_at = datetime.now(UTC)
    else:
        change.status = "review"
    await log_action(
        db,
        action="engineering.change_request.sign",
        entity_type="engineering_change_request",
        entity_id=change.id,
        user_id=body.reviewer,
    )
    await add_timeline_event(
        db,
        entity_type="engineering_change_request",
        entity_id=change.id,
        event_type=f"signed_{body.decision}",
        summary=f"{body.reviewer}: {body.decision}",
        actor=body.reviewer,
    )
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
        raise HTTPException(
            409, "Применить можно только согласованный запрос (все подписи approve)"
        )
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
    await log_action(
        db,
        action="engineering.change_request.apply",
        entity_type="engineering_change_request",
        entity_id=change.id,
        user_id=change.created_by,
    )
    await add_timeline_event(
        db,
        entity_type="engineering_change_request",
        entity_id=change.id,
        event_type="applied",
        summary=f"Создана ревизия {new_revision.revision} (база {affected.revision})",
        actor=change.created_by,
    )
    await db.commit()
    await db.refresh(change)
    return change
