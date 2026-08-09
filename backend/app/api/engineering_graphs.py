"""EngineeringModelGraph v1 revision, patch and verification API."""

import hashlib
import json
import uuid
from pathlib import PurePosixPath
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.jwt import get_current_user
from app.auth.models import UserInfo, require_permission
from app.config import settings
from app.db.models import (
    EngineeringGraphRevision,
    EngineeringProject,
    GraphPatchRecord,
    TraceProposalRecord,
    VisualVerificationRun,
)
from app.db.session import get_db
from app.domain.engineering_model_graph import (
    EngineeringModelGraph,
    GraphPatch,
    TraceAdmission,
    TraceProposal,
    VisualVerification,
    assertion_impact_report,
    compile_build_plan,
    critical_assertion_ids,
    evaluate_trace_admission,
)
from app.services.engineering_model_graph import (
    DuplicatePatchError,
    create_initial_graph,
    latest_graph_revision,
    load_graph,
    merge_and_persist_patch,
    persist_verification_run,
    verify_graph,
)

router = APIRouter()


class TraceEvaluationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    assertion_id: str
    proposal: TraceProposal
    visual: VisualVerification


@router.get("/status")
async def graph_pipeline_status() -> dict:
    return {
        "schema_version": "emg/1.0",
        "pipeline_enabled": settings.emg_pipeline_enabled,
        "enabled_profiles": sorted({
            item.strip().lower()
            for item in settings.emg_pipeline_profiles.split(",")
            if item.strip()
        }),
        "legacy_views": "derived",
        "production_defaults": {
            "max_wall_seconds": 900,
            "max_model_calls": 32,
            "call_timeout_seconds": 90,
            "no_progress_pass_limit": 2,
        },
    }


@router.post(
    "/projects/{project_id}/graphs",
    status_code=status.HTTP_201_CREATED,
)
async def create_graph(
    project_id: uuid.UUID,
    graph: EngineeringModelGraph,
    engineering_revision_id: uuid.UUID | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> dict:
    require_permission(user, "engineering.revision_create")
    if not await db.get(EngineeringProject, project_id):
        raise HTTPException(404, "Инженерный проект не найден")
    try:
        row = await create_initial_graph(
            db,
            graph,
            engineering_project_id=project_id,
            engineering_revision_id=engineering_revision_id,
        )
        await db.commit()
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(409, str(exc)) from exc
    return _revision_response(row, graph=graph.sealed())


@router.get("/projects/{project_id}/graphs")
async def list_project_graphs(
    project_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> list[dict]:
    rows = list((await db.execute(
        select(EngineeringGraphRevision)
        .where(EngineeringGraphRevision.engineering_project_id == project_id)
        .order_by(EngineeringGraphRevision.graph_id, EngineeringGraphRevision.revision.desc())
    )).scalars())
    latest: dict[str, EngineeringGraphRevision] = {}
    for row in rows:
        latest.setdefault(row.graph_id, row)
    return [_revision_response(row, graph=load_graph(row)) for row in latest.values()]


@router.get("/graphs/{graph_id}/latest")
async def get_latest_graph(graph_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    row = await latest_graph_revision(db, graph_id)
    if not row:
        raise HTTPException(404, "EngineeringModelGraph не найден")
    return _revision_response(row, graph=load_graph(row))


@router.post("/graphs/{graph_id}/reader-runs", status_code=status.HTTP_202_ACCEPTED)
async def start_reader_run(
    graph_id: str,
    target_id: str = Query(default="preview"),
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> dict:
    """Queue the adaptive local reader against the latest immutable revision."""
    require_permission(user, "engineering.revision_create")
    row = await latest_graph_revision(db, graph_id)
    if row is None:
        raise HTTPException(404, "EngineeringModelGraph не найден")
    graph = load_graph(row)
    if not any(item.id == target_id for item in graph.build_targets):
        raise HTTPException(404, "Build target не найден")
    from app.tasks.engineering_model_reader import run_engineering_model_reader

    task = run_engineering_model_reader.apply_async(
        args=[graph_id, target_id],
        queue="gpu",
    )
    return {
        "task_id": task.id,
        "graph_id": graph_id,
        "base_revision": row.revision,
        "base_sha256": row.canonical_sha256,
        "target_id": target_id,
    }


@router.get("/revisions/{revision_id}")
async def get_graph_revision(
    revision_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> dict:
    row = await db.get(EngineeringGraphRevision, revision_id)
    if not row:
        raise HTTPException(404, "Ревизия EngineeringModelGraph не найдена")
    return _revision_response(row, graph=load_graph(row))


@router.get("/revisions/{revision_id}/artifacts/{artifact_id}")
async def download_graph_artifact(
    revision_id: uuid.UUID,
    artifact_id: str,
    kind: Literal["artifact", "report"] = Query(default="artifact"),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Serve a graph-bound artifact after verifying its content address."""
    from anyio import to_thread

    from app.storage import download_file

    row = await db.get(EngineeringGraphRevision, revision_id)
    if row is None:
        raise HTTPException(404, "Ревизия EngineeringModelGraph не найдена")
    graph = load_graph(row)
    node = next(
        (item for item in graph.nodes if item.id == artifact_id and item.type == "Artifact"),
        None,
    )
    if node is None:
        raise HTTPException(404, "Artifact не найден в выбранной ревизии графа")
    evidence_ids = {
        evidence_id
        for assertion in graph.assertions
        if assertion.state == "active" and assertion.subject_id == artifact_id
        for evidence_id in assertion.evidence_ids
    }
    evidence = next(
        (
            item for item in reversed(graph.evidence)
            if item.id in evidence_ids
            and item.payload.get("artifact_path")
            and item.payload.get("report_path")
            and item.payload.get("artifact_sha256")
        ),
        None,
    )
    if evidence is None:
        raise HTTPException(409, "Artifact не связан с проверенным evidence")
    path_key = "artifact_path" if kind == "artifact" else "report_path"
    storage_path = evidence.payload[path_key]
    if (
        not isinstance(storage_path, str)
        or not storage_path.startswith("engineering/")
        or ".." in PurePosixPath(storage_path).parts
    ):
        raise HTTPException(409, "Evidence содержит недопустимый storage path")
    try:
        content = await to_thread.run_sync(download_file, storage_path)
    except Exception as exc:
        raise HTTPException(404, "Файл artifact отсутствует в объектном хранилище") from exc
    suffix = PurePosixPath(storage_path).suffix.lower()
    if kind == "artifact":
        expected_sha = evidence.payload["artifact_sha256"]
        actual_sha = hashlib.sha256(content).hexdigest()
        media_type = {
            ".svg": "image/svg+xml",
            ".step": "model/step",
            ".stp": "model/step",
            ".ifc": "application/x-step",
            ".stl": "model/stl",
            ".dxf": "application/dxf",
            ".pdf": "application/pdf",
        }.get(suffix, "application/octet-stream")
    else:
        try:
            report = json.loads(content)
            expected_sha = report.pop("canonical_report_sha256")
            actual_sha = hashlib.sha256(
                json.dumps(report, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise HTTPException(409, "Verification report невалиден") from exc
        media_type = "application/json"
    if not isinstance(expected_sha, str) or actual_sha != expected_sha:
        raise HTTPException(409, "Content-addressed artifact не прошёл SHA-256 проверку")
    safe_name = "".join(
        char if char.isalnum() or char in "-_." else "-" for char in artifact_id
    )[:160] or "engineering-artifact"
    disposition = "inline" if media_type == "image/svg+xml" else "attachment"
    return Response(
        content=content,
        media_type=media_type,
        headers={
            "Content-Disposition": f'{disposition}; filename="{safe_name}{suffix}"',
            "X-Engineering-Artifact-SHA256": actual_sha,
            "X-Engineering-Graph-Revision": str(row.revision),
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/graphs/{graph_id}/patches")
async def apply_patch(
    graph_id: str,
    patch: GraphPatch,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> dict:
    require_permission(user, "engineering.revision_create")
    try:
        row, errors = await merge_and_persist_patch(
            db, patch, expected_graph_id=graph_id
        )
        await db.commit()
    except DuplicatePatchError as exc:
        await db.rollback()
        raise HTTPException(409, str(exc)) from exc
    except LookupError as exc:
        await db.rollback()
        raise HTTPException(404, str(exc)) from exc
    if row is None:
        return {"accepted": False, "validation_errors": errors, "revision": None}
    return {
        "accepted": True,
        "validation_errors": [],
        "revision": _revision_response(row, graph=load_graph(row)),
    }


@router.get("/graphs/{graph_id}/patches")
async def list_patches(graph_id: str, db: AsyncSession = Depends(get_db)) -> list[dict]:
    rows = list((await db.execute(
        select(GraphPatchRecord)
        .where(GraphPatchRecord.graph_id == graph_id)
        .order_by(GraphPatchRecord.created_at.desc())
    )).scalars())
    return [{
        "id": str(row.id), "patch_id": row.patch_id, "producer": row.producer,
        "pass_id": row.pass_id, "accepted": row.accepted,
        "payload": row.payload, "validation_errors": row.validation_errors,
        "result_revision_id": str(row.result_revision_id) if row.result_revision_id else None,
        "created_at": row.created_at,
    } for row in rows]


@router.get("/revisions/{revision_id}/trace-proposals")
async def list_trace_proposals(
    revision_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> list[dict]:
    proposals = list((await db.execute(
        select(TraceProposalRecord)
        .where(TraceProposalRecord.graph_revision_id == revision_id)
        .order_by(TraceProposalRecord.source_region_id, TraceProposalRecord.rank)
    )).scalars())
    proposal_ids = [item.id for item in proposals]
    visuals = list((await db.execute(
        select(VisualVerificationRun).where(
            VisualVerificationRun.trace_proposal_id.in_(proposal_ids)
        )
    )).scalars()) if proposal_ids else []
    visual_by_proposal: dict[uuid.UUID, list[VisualVerificationRun]] = {}
    for visual in visuals:
        visual_by_proposal.setdefault(visual.trace_proposal_id, []).append(visual)
    return [{
        "id": str(item.id), "proposal_id": item.proposal_id,
        "source_region_id": item.source_region_id, "assertion_id": item.assertion_id,
        "rank": item.rank, "status": item.status, "score": item.score,
        "payload": item.payload,
        "visual_verifications": [run.result | {"raw_output": run.raw_output} for run in visual_by_proposal.get(item.id, [])],
    } for item in proposals]


@router.post("/revisions/{revision_id}/trace-proposals", status_code=status.HTTP_201_CREATED)
async def record_trace_proposal(
    revision_id: uuid.UUID,
    body: TraceEvaluationRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    row = await db.get(EngineeringGraphRevision, revision_id)
    if not row:
        raise HTTPException(404, "Ревизия EngineeringModelGraph не найдена")
    if body.proposal.id != body.visual.proposal_id:
        raise HTTPException(422, "Visual verification относится к другому proposal")
    graph = load_graph(row)
    regions = {item.id for item in graph.nodes if item.type == "SourceRegion"}
    if body.proposal.source_region_id not in regions:
        raise HTTPException(422, "Трассировка разрешена только внутри зарегистрированного SourceRegion")
    assertion = next((item for item in graph.assertions if item.id == body.assertion_id), None)
    if not assertion:
        raise HTTPException(422, "Assertion не найден")
    proposal_count = (await db.execute(
        select(TraceProposalRecord).where(
            TraceProposalRecord.graph_revision_id == revision_id,
            TraceProposalRecord.source_region_id == body.proposal.source_region_id,
        )
    )).scalars().all()
    if len(proposal_count) >= 3:
        raise HTTPException(409, "Для SourceRegion уже проверены три trace proposal")
    critical = set().union(*(
        critical_assertion_ids(graph, target.id) for target in graph.build_targets
    )) if graph.build_targets else set()
    validated_conflict = assertion.assurance in {"constraint_validated", "human_approved"}
    admission = evaluate_trace_admission(
        body.proposal,
        body.visual,
        assertion_is_non_critical=body.assertion_id not in critical,
        conflicts_with_validated=validated_conflict,
    )
    proposal_row = TraceProposalRecord(
        graph_revision_id=revision_id,
        proposal_id=body.proposal.id,
        source_region_id=body.proposal.source_region_id,
        assertion_id=body.assertion_id,
        rank=len(proposal_count) + 1,
        status="accepted" if admission.accepted else (
            "critical_unresolved" if "critical_dependency" in admission.reason_codes else "rejected"
        ),
        payload=body.proposal.model_dump(mode="json"),
        score=admission.score,
    )
    db.add(proposal_row)
    await db.flush()
    db.add(VisualVerificationRun(
        trace_proposal_id=proposal_row.id,
        verifier_model=body.visual.verifier_model,
        verdict=body.visual.verdict,
        result=body.visual.model_dump(mode="json", exclude={"raw_output"}),
        raw_output=body.visual.raw_output,
    ))
    await db.commit()
    return {
        "id": str(proposal_row.id),
        "status": proposal_row.status,
        "admission": admission.model_dump(mode="json"),
    }


@router.post("/revisions/{revision_id}/verify")
async def run_verification(
    revision_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> dict:
    row = await db.get(EngineeringGraphRevision, revision_id)
    if not row:
        raise HTTPException(404, "Ревизия EngineeringModelGraph не найдена")
    state, issues = verify_graph(load_graph(row))
    run = await persist_verification_run(db, row, state, issues)
    # Status columns are operational indexes over immutable run results; the
    # canonical graph blob and its hash are never rewritten.
    row.comprehension_status = state.comprehension
    row.build_status = state.build
    row.release_status = state.release
    await db.commit()
    return {
        "run_id": str(run.id),
        "state": state.model_dump(mode="json"),
        "issues": issues,
    }


@router.get("/revisions/{revision_id}/build-plan/{target_id}")
async def get_build_plan(
    revision_id: uuid.UUID,
    target_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict:
    row = await db.get(EngineeringGraphRevision, revision_id)
    if not row:
        raise HTTPException(404, "Ревизия EngineeringModelGraph не найдена")
    try:
        return compile_build_plan(load_graph(row), target_id).model_dump(mode="json")
    except KeyError as exc:
        raise HTTPException(404, "Build target не найден") from exc


@router.get("/revisions/{revision_id}/assertions/{assertion_id}/impact")
async def get_assertion_impact(
    revision_id: uuid.UUID,
    assertion_id: str,
    target_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
) -> dict:
    row = await db.get(EngineeringGraphRevision, revision_id)
    if not row:
        raise HTTPException(404, "Ревизия EngineeringModelGraph не найдена")
    try:
        return assertion_impact_report(
            load_graph(row), assertion_id, target_id
        ).model_dump(mode="json")
    except KeyError as exc:
        missing = exc.args[0] if exc.args else None
        detail = (
            "Build target не найден" if missing == target_id
            else "Assertion не найден"
        )
        raise HTTPException(404, detail) from exc


@router.post("/trace/evaluate", response_model=TraceAdmission)
async def evaluate_trace(
    proposal: TraceProposal,
    visual: VisualVerification,
    assertion_is_non_critical: bool = Query(...),
    conflicts_with_validated: bool = Query(False),
) -> TraceAdmission:
    if proposal.id != visual.proposal_id:
        raise HTTPException(422, "Visual verification относится к другому proposal")
    return evaluate_trace_admission(
        proposal,
        visual,
        assertion_is_non_critical=assertion_is_non_critical,
        conflicts_with_validated=conflicts_with_validated,
    )


def _revision_response(
    row: EngineeringGraphRevision, *, graph: EngineeringModelGraph
) -> dict:
    return {
        "id": str(row.id),
        "engineering_project_id": (
            str(row.engineering_project_id) if row.engineering_project_id else None
        ),
        "engineering_revision_id": str(row.engineering_revision_id) if row.engineering_revision_id else None,
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
