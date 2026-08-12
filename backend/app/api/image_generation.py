"""Image studio API — generate/edit raster images (drawings) via ComfyUI.

Draft-first: ``POST /generate`` queues a Celery job and returns the record
immediately; the result arrives asynchronously (poll ``GET /{id}`` or wait for
the mobile push). No approval gate — generated images are version drafts the
human keeps (``/accept``) or re-iterates (``/iterate``).

Also exposes the editable workflow library (``/workflows*``) and a prompt helper
(``/prompt-help``) that turns a rough RU description into a precise prompt.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal
from urllib.parse import quote

import httpx
import structlog
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field, TypeAdapter, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.cad_vectorizer_status import get_cad_vectorizer_development_status
from app.auth.jwt import get_current_user
from app.auth.models import UserInfo, UserRole
from app.db.models import (
    CadCertification,
    ComfyWorkflow,
    Document,
    ImageGeneration,
    ImageGenStatus,
    StudioJob,
    StudioJobKind,
)
from app.db.session import get_db
from app.services import studio_queue
from app.storage import download_file, upload_file

router = APIRouter()
logger = structlog.get_logger()

_SOURCE_PREFIX = "image-gen-src"
_ALLOWED_OPERATIONS = {"edit", "generate", "inpaint", "cleanup", "eskd", "vectorize"}
# Engineering results whose acceptance is approval-gated for the agent.
_GATED_OPERATIONS = {"techdraw", "vectorize"}
_ALLOWED_UPLOAD_TYPES = {
    "image/png", "image/jpeg", "image/jpg", "image/webp",
    "application/pdf", "application/octet-stream",
}
_ALLOWED_UPLOAD_EXTS = {"png", "jpg", "jpeg", "webp", "pdf"}
_MAX_SOURCE_BYTES = 50 * 1024 * 1024


class CadCertificationRequest(BaseModel):
    profile: Literal[
        "auto", "mechanical", "construction", "electrical", "hydraulic", "pid"
    ] = "auto"


# ── Schemas ──────────────────────────────────────────────────────────────────


class GenerateRequest(BaseModel):
    operation: Literal["edit", "generate", "inpaint", "cleanup", "eskd", "vectorize"] = "edit"
    prompt: str | None = None
    negative_prompt: str | None = None
    workflow_id: uuid.UUID | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    # Images already in MinIO (e.g. uploaded via /upload-source) and/or documents.
    source_image_paths: list[str] = Field(default_factory=list)
    source_document_ids: list[uuid.UUID] = Field(default_factory=list)
    mask_path: str | None = None
    # Link the generation to a document/case for traceability (optional; distinct
    # from source_document_ids, which are used as image sources for edit/inpaint).
    source_document_id: uuid.UUID | None = None
    case_id: uuid.UUID | None = None


class WorkflowIn(BaseModel):
    key: str
    title: str
    description: str | None = None
    category: str = "edit"
    operation: str = "edit"
    graph: dict[str, Any] = Field(default_factory=dict)
    inject_map: dict[str, Any] = Field(default_factory=dict)
    params_schema: dict[str, Any] = Field(default_factory=dict)


class WorkflowPatch(BaseModel):
    title: str | None = None
    description: str | None = None
    category: str | None = None
    operation: str | None = None
    graph: dict[str, Any] | None = None
    inject_map: dict[str, Any] | None = None
    params_schema: dict[str, Any] | None = None
    enabled: bool | None = None


class PromptHelpRequest(BaseModel):
    description: str
    operation: str = "edit"
    source_document_id: uuid.UUID | None = None


class TechDrawRequest(BaseModel):
    # Either a free-text description (→ LLM → spec) or a ready spec.
    description: str | None = None
    spec: dict[str, Any] | None = None
    view: Literal["front", "isometric", "section", "half_section"] = "front"
    source_document_id: uuid.UUID | None = None
    case_id: uuid.UUID | None = None


class HumanAssertionCorrectionRequest(BaseModel):
    value: dict[str, Any]
    unit: str | None = None
    note: str = Field(min_length=1, max_length=1000)
    idempotency_key: str = Field(min_length=8, max_length=200)
    source_bbox_normalized: tuple[float, float, float, float] | None = None
    rebuild: bool = False

    @model_validator(mode="after")
    def validate_source_bbox(self) -> "HumanAssertionCorrectionRequest":
        if self.source_bbox_normalized is None:
            return self
        x0, y0, x1, y1 = self.source_bbox_normalized
        if not (0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1):
            raise ValueError("source bbox must be normalized and non-empty")
        return self


class HumanAssertionBatchItem(BaseModel):
    assertion_id: str = Field(min_length=1)
    value: dict[str, Any]
    unit: str | None = None
    source_bbox_normalized: tuple[float, float, float, float] | None = None

    @model_validator(mode="after")
    def validate_source_bbox(self) -> "HumanAssertionBatchItem":
        if self.source_bbox_normalized is None:
            return self
        x0, y0, x1, y1 = self.source_bbox_normalized
        if not (0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1):
            raise ValueError("source bbox must be normalized and non-empty")
        return self


class HumanAssertionBatchCorrectionRequest(BaseModel):
    corrections: list[HumanAssertionBatchItem] = Field(min_length=1, max_length=100)
    note: str = Field(min_length=1, max_length=1000)
    idempotency_key: str = Field(min_length=8, max_length=200)
    rebuild: bool = False

    @model_validator(mode="after")
    def validate_unique_assertions(self) -> "HumanAssertionBatchCorrectionRequest":
        ids = [item.assertion_id for item in self.corrections]
        if len(ids) != len(set(ids)):
            raise ValueError("batch correction contains duplicate assertion ids")
        return self


def _set_compat_spec_value(spec: dict[str, Any], predicate: str, value: Any) -> bool:
    """Update one existing legacy-spec leaf without inventing a parallel schema."""
    tokens: list[str | int] = []
    for name, index in re.findall(r"([^.\[\]]+)|\[(\d+)\]", predicate):
        tokens.append(int(index) if index else name)
    if not tokens:
        return False
    current: Any = spec
    for token in tokens[:-1]:
        if isinstance(token, int):
            if not isinstance(current, list) or token >= len(current):
                return False
            current = current[token]
        else:
            if not isinstance(current, dict) or token not in current:
                return False
            current = current[token]
    final = tokens[-1]
    if isinstance(final, int):
        if not isinstance(current, list) or final >= len(current):
            return False
        current[final] = value
    else:
        if not isinstance(current, dict) or final not in current:
            return False
        current[final] = value
    return True


def _apply_compat_spec_update(
    compatibility: dict[str, Any], previous: Any, value: Any
) -> bool:
    """Mirror one corrected assertion into the legacy compatibility spec.

    Two subjects have a corresponding leaf there:

    - ``product:legacy-spec`` assertions — the predicate IS already that
      leaf's own dotted path.
    - ``feature:<id>`` assertions whose predicate is ``feature.param.<name>``
      or ``feature.location`` — the descriptive Ф1.2 Feature graph, mirrored
      onto the SAME underlying spec leaf via :func:`feature_spec_path`
      (decoding the id ``assign_stable_feature_ids`` assigned it, not a
      guess). This is what lets a human correct the native Feature graph
      directly and have geometry actually follow: the existing rebuild
      recompiles from this same compatibility spec, unchanged.

    Anything else (a Constraint, a BuildOperation with no source Feature —
    e.g. one added fresh in the 3D editor, ``feature.kind`` — which names a
    LIST membership, not a leaf value) has no compatibility-view counterpart
    to update, and an ``interval``/``enum_set``/``expression`` value has no
    single scalar to mirror.
    """
    if value.kind not in {"exact", "unknown"}:
        return False
    compatible_value = value.value if value.kind == "exact" else None
    if previous.subject_id == "product:legacy-spec":
        return _set_compat_spec_value(compatibility, previous.predicate, compatible_value)
    if previous.subject_id.startswith("feature:"):
        from app.ai.cad_emg_compat import feature_spec_path
        from app.domain.emg_predicates import FEATURE_PARAM_PREFIX, PREDICATE

        if previous.predicate.startswith(FEATURE_PARAM_PREFIX):
            field = previous.predicate.removeprefix(FEATURE_PARAM_PREFIX)
        elif previous.predicate == PREDICATE.FEATURE_LOCATION:
            field = "location"
        else:
            return False
        base_path = feature_spec_path(previous.subject_id.removeprefix("feature:"))
        if base_path is None:
            return False
        return _set_compat_spec_value(compatibility, f"{base_path}.{field}", compatible_value)
    return False


def _build_human_correction_change(
    *,
    previous: Any,
    value: Any,
    unit: str | None,
    note: str,
    actor_sub: str,
    digest: str,
    source_bbox_normalized: tuple[float, float, float, float] | None,
    source: Any | None,
    location_node_id: str | None,
    batch_patch_id: str | None = None,
) -> tuple[list[Any], list[Any], Any, list[Any]]:
    """Build the node/edge/assertion/evidence set for ONE human correction.

    Shared by the single-assertion and batch correction endpoints — a change
    to how a bbox or an evidence record is shaped only has to happen here,
    not in two near-identical copies.
    """
    from app.domain.engineering_model_graph import Assertion, Evidence, GraphEdge, GraphNode

    decision_id = f"evidence:human:{digest}"
    evidence_ids = [decision_id]
    add_nodes: list[Any] = []
    add_edges: list[Any] = []
    decision_payload: dict[str, Any] = {
        "actor_sub": actor_sub, "note": note, "superseded_assertion_id": previous.id,
    }
    if batch_patch_id is not None:
        decision_payload["batch_patch_id"] = batch_patch_id
    add_evidence = [Evidence(id=decision_id, kind="human_decision", payload=decision_payload)]
    if source_bbox_normalized is not None:
        if source is None:
            raise HTTPException(422, "Граф не содержит источник для SourceRegion")
        region_id = f"region:human:{digest}"
        raster_id = f"evidence:raster:human:{digest}"
        add_nodes.append(GraphNode(
            id=region_id, type="SourceRegion",
            name=f"Human region for {previous.predicate}",
        ))
        add_edges.append(GraphEdge(
            id=f"located:{region_id}", type="located_in",
            source_id=region_id, target_id=location_node_id or previous.subject_id,
        ))
        add_evidence.append(Evidence(
            id=raster_id, kind="raster_region", source_id=source.id,
            source_region_id=region_id,
            payload={
                "bbox_normalized": list(source_bbox_normalized),
                "fallback": False, "selected_by": "human",
            },
            sha256=source.sha256,
        ))
        evidence_ids.append(raster_id)
    replacement = Assertion(
        id=f"assertion:human:{digest}", subject_id=previous.subject_id,
        predicate=previous.predicate, value=value,
        unit=unit if unit is not None else previous.unit,
        coordinate_system=previous.coordinate_system, origin="human",
        assurance="human_approved", evidence_ids=evidence_ids, confidence=1.0,
        impacts=previous.impacts,
        impact_magnitude_percent=previous.impact_magnitude_percent,
        hypothesis_id=previous.hypothesis_id,
        supersedes_assertion_id=previous.id,
    )
    return add_nodes, add_edges, replacement, add_evidence


def _is_agent_service(user: UserInfo) -> bool:
    """True for the trusted internal agent identity (see auth.jwt._verify_api_key).

    The capability dispatcher (``/api/agent/cap/*``) never forwards the real
    chatting user's identity to the proxied REST call — it always presents as
    this fixed service sub (already granted ``UserRole.admin`` at the auth
    layer). Endpoints that scope data by ``owner_sub == user.sub`` must treat
    this identity as authorized for any owner, or every agent-mediated call
    against image_studio (list/get/accept/iterate/delete) 404s outright —
    that isn't a hypothetical: it reproduces on the live stack with auth on.
    """
    return user.sub == "agent-service"


def _is_admin(user: UserInfo) -> bool:
    return UserRole.admin in (user.roles or [])


def _can_use_studio(user: UserInfo) -> bool:
    return _is_admin(user) or any(
        role in (user.roles or [])
        for role in (UserRole.engineer, UserRole.technologist, UserRole.manager)
    ) or _is_agent_service(user)


def _can_manage_workflows(user: UserInfo) -> bool:
    return _is_admin(user) or UserRole.engineer in (user.roles or []) or _is_agent_service(user)


def _owns(gen: ImageGeneration | None, user: UserInfo) -> bool:
    return gen is not None and (gen.owner_sub == user.sub or _is_agent_service(user))


def _can_access_document(doc: Document | None, user: UserInfo) -> bool:
    return doc is not None and (
        getattr(doc, "owner_sub", None) in (None, user.sub)
        or _is_admin(user)
        or UserRole.manager in (user.roles or [])
        or _is_agent_service(user)
    )


def _can_read_workflow(wf: ComfyWorkflow | None, user: UserInfo) -> bool:
    return wf is not None and (
        wf.is_builtin
        or wf.owner_sub in (None, user.sub)
        or _can_manage_workflows(user)
    )


def _can_mutate_workflow(wf: ComfyWorkflow | None, user: UserInfo) -> bool:
    return wf is not None and (wf.owner_sub == user.sub or _can_manage_workflows(user))


def _prompt_text(body: GenerateRequest) -> str:
    return (body.prompt or "").strip()


async def _workflow_for_iteration(
    db: AsyncSession,
    parent: ImageGeneration,
    body: GenerateRequest,
    user: UserInfo,
) -> uuid.UUID | None:
    """Pick an iteration workflow without leaking cleanup pipelines into edit.

    Iteration defaults to ``edit`` even when the parent was produced by
    cleanup/inpaint/generate. Inheriting the parent's workflow blindly makes an
    edit iteration run through the parent's cleanup graph, which ignores the
    user's intent in practice. Only inherit when the parent workflow matches the
    requested operation; otherwise let the task resolver choose the default
    enabled workflow for that operation.
    """
    operation = body.operation or "edit"
    if body.workflow_id:
        wf = await db.get(ComfyWorkflow, body.workflow_id)
        if not _can_read_workflow(wf, user):
            raise HTTPException(404, "Воркфлоу не найден")
        if not wf.enabled:
            raise HTTPException(400, "Воркфлоу выключен")
        if wf.operation != operation:
            raise HTTPException(400, "Воркфлоу не подходит для выбранной операции")
        return body.workflow_id

    if not parent.workflow_id:
        return None
    parent_wf = await db.get(ComfyWorkflow, parent.workflow_id)
    if (
        parent_wf
        and parent_wf.enabled
        and parent_wf.operation == operation
        and _can_read_workflow(parent_wf, user)
    ):
        return parent.workflow_id
    return None


def _validate_source_path(path: str, user: UserInfo) -> str:
    if not path or path.startswith("/") or ".." in path.split("/"):
        raise HTTPException(400, "Недопустимый путь исходного изображения.")
    if _is_agent_service(user):
        return path
    expected = f"{_SOURCE_PREFIX}/{user.sub}/"
    if not path.startswith(expected):
        raise HTTPException(403, "Исходное изображение не принадлежит текущему пользователю.")
    return path


async def _resolve_source_path(path: str, db: AsyncSession, user: UserInfo) -> str:
    if path.startswith("generation:"):
        raw_id = path.removeprefix("generation:").strip()
        try:
            generation_id = uuid.UUID(raw_id)
        except ValueError:
            raise HTTPException(400, "Недопустимая ссылка на сгенерированное изображение.")
        gen = await db.get(ImageGeneration, generation_id)
        if not _owns(gen, user):
            raise HTTPException(404, "Сгенерированное изображение не найдено.")
        if not gen or not gen.result_path:
            raise HTTPException(400, "У выбранной генерации нет готового результата.")
        try:
            content = download_file(gen.result_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "generated_source_missing",
                generation_id=str(generation_id),
                path=gen.result_path,
                error=str(exc),
            )
            raise HTTPException(
                400,
                "Файл выбранной генерации не найден. Выберите другой результат.",
            ) from exc
        copied_path = f"{_SOURCE_PREFIX}/{user.sub}/{uuid.uuid4().hex}.png"
        upload_file(content, copied_path, "image/png")
        return copied_path
    return _validate_source_path(path, user)


def _gen_out(gen: ImageGeneration) -> dict:
    status = gen.status.value if hasattr(gen.status, "value") else gen.status
    progress = None
    if status == "running":
        try:
            from app.tasks.image_generation import read_progress

            progress = read_progress(str(gen.id))
        except Exception:  # noqa: BLE001
            progress = None
        if progress is None and gen.operation == "vectorize":
            process = (gen.params or {}).get("cad_process") or {}
            pct = int(process.get("progress_pct") or 0)
            progress = {
                "value": pct,
                "max": 100,
                "pct": pct,
                "node": process.get("current_message") or process.get("current_stage"),
                "ts": int(datetime.now(timezone.utc).timestamp()),
            }
    public_params = dict(gen.params or {})
    # Full model transcripts and per-pass recovery specs are fetched on demand.
    # Returning them in the 2.5-second Studio list poll turns observability into
    # multi-megabyte traffic and can itself make the progress UI appear stuck.
    public_params.pop("cad_model_outputs", None)
    public_params.pop("cad_partial_spec", None)
    return {
        "id": str(gen.id),
        "operation": gen.operation,
        "status": status,
        "progress": progress,
        "prompt": gen.prompt,
        "negative_prompt": gen.negative_prompt,
        "params": public_params,
        "source_image_paths": gen.source_image_paths or [],
        "mask_path": gen.mask_path,
        "has_result": bool(gen.result_path),
        "error": gen.error,
        "parent_id": str(gen.parent_id) if gen.parent_id else None,
        "accepted": gen.accepted,
        "accepted_by": gen.accepted_by,
        "accepted_at": gen.accepted_at.isoformat() if gen.accepted_at else None,
        "accepted_revision": gen.accepted_revision,
        "quality_rating": gen.quality_rating,
        "issue_tags": gen.issue_tags or [],
        "review_notes": gen.review_notes,
        "workflow_id": str(gen.workflow_id) if gen.workflow_id else None,
        "created_at": gen.created_at.isoformat() if gen.created_at else None,
        "source_document_id": str(gen.source_document_id) if gen.source_document_id else None,
        "case_id": str(gen.case_id) if gen.case_id else None,
    }


def _wf_out(wf: ComfyWorkflow) -> dict:
    return {
        "id": str(wf.id),
        "key": wf.key,
        "title": wf.title,
        "description": wf.description,
        "category": wf.category,
        "operation": wf.operation,
        "graph": wf.graph or {},
        "inject_map": wf.inject_map or {},
        "params_schema": wf.params_schema or {},
        "enabled": wf.enabled,
        "is_builtin": wf.is_builtin,
        "owner_sub": wf.owner_sub,
    }


# ── Source upload (UI helper) ────────────────────────────────────────────────


@router.post("/upload-source")
async def upload_source(
    file: UploadFile = File(...),
    kind: str = Form("source"),  # source | mask
    user: UserInfo = Depends(get_current_user),
) -> dict:
    """Store a source/mask image in MinIO; returns its path for /generate."""
    if not _can_use_studio(user):
        raise HTTPException(403, "Недостаточно прав для графической студии")
    if kind not in {"source", "mask"}:
        raise HTTPException(400, "kind должен быть source или mask")
    ctype = (file.content_type or "").split(";")[0].lower()
    if ctype and ctype not in _ALLOWED_UPLOAD_TYPES:
        raise HTTPException(400, "Поддержаны PNG, JPEG, WebP и PDF.")
    content = await file.read()
    if not content:
        raise HTTPException(400, "Пустой файл")
    if len(content) > _MAX_SOURCE_BYTES:
        raise HTTPException(413, "Изображение слишком большое для графической студии.")
    ext = "png"
    if file.filename and "." in file.filename:
        ext = file.filename.rsplit(".", 1)[-1].lower()[:5]
    if ext not in _ALLOWED_UPLOAD_EXTS:
        raise HTTPException(400, "Поддержаны PNG, JPEG, WebP и PDF.")
    path = f"{_SOURCE_PREFIX}/{user.sub}/{uuid.uuid4().hex}.{ext}"
    upload_file(content, path, file.content_type or "image/png")
    return {"path": path}


# ── Generate / list / get ────────────────────────────────────────────────────


@router.post("/generate")
async def generate(
    body: GenerateRequest,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> dict:
    if not _can_use_studio(user):
        raise HTTPException(403, "Недостаточно прав для графической студии")

    wf = None
    if body.workflow_id:
        wf = await db.get(ComfyWorkflow, body.workflow_id)
        if not _can_read_workflow(wf, user):
            raise HTTPException(404, "Воркфлоу не найден")
        if not wf.enabled:
            raise HTTPException(400, "Воркфлоу выключен")
        if wf.operation != body.operation:
            raise HTTPException(400, "Воркфлоу не подходит для выбранной операции")

    source_paths = [
        await _resolve_source_path(path, db, user)
        for path in body.source_image_paths
    ]
    for doc_id in body.source_document_ids:
        doc = await db.get(Document, doc_id)
        if not _can_access_document(doc, user):
            raise HTTPException(404, "Документ-источник не найден")
        if doc and doc.storage_path:
            source_paths.append(doc.storage_path)

    if body.source_document_id:
        doc = await db.get(Document, body.source_document_id)
        if not _can_access_document(doc, user):
            raise HTTPException(404, "Документ для связи не найден")

    description_vector = (
        body.operation == "vectorize"
        and str((body.params or {}).get("vectorize_method") or "trace") == "text_spec"
        and bool((body.prompt or "").strip())
    )
    if (
        body.operation in ("edit", "inpaint", "cleanup", "vectorize")
        and not source_paths
        and not description_vector
    ):
        raise HTTPException(400, "Для этой операции нужно исходное изображение.")
    if body.operation in ("edit", "inpaint") and not _prompt_text(body):
        raise HTTPException(400, "Для редактирования нужно текстовое указание (prompt).")
    # "eskd" is a text→image ЕСКД-styled generation (diffusion alternative to the
    # deterministic /techdraw render) — same input contract as "generate".
    if body.operation in ("generate", "eskd") and not _prompt_text(body):
        raise HTTPException(400, "Для генерации нужно текстовое описание (prompt).")

    await studio_queue.ensure_can_enqueue(
        db,
        owner_sub=user.sub,
        kind=StudioJobKind.image_generation,
        roles=user.roles,
    )

    params = dict(body.params or {})
    # Provenance link: vectorizing a previous (possibly diffusion) result must
    # know its ancestry — the pipeline compares against that generation's own
    # source to build the pixel-change mask (diffusion is not a truth source).
    for raw in body.source_image_paths:
        if raw.startswith("generation:"):
            params.setdefault("source_generation_id", raw.removeprefix("generation:").strip())
            break

    gen = ImageGeneration(
        owner_sub=user.sub,
        operation=body.operation,
        workflow_id=body.workflow_id,
        status=ImageGenStatus.queued,
        prompt=body.prompt,
        negative_prompt=body.negative_prompt,
        params=params,
        source_image_paths=source_paths,
        mask_path=body.mask_path,
        source_document_id=body.source_document_id,
        case_id=body.case_id,
    )
    db.add(gen)
    await db.flush()
    job = await studio_queue.create_image_job(db, gen, title=body.prompt or body.operation)
    await db.commit()
    await db.refresh(gen)
    await db.refresh(job)

    task_id = _enqueue(str(gen.id), body.operation)
    if task_id:
        job.celery_task_id = task_id
        gen.celery_task_id = task_id
        await db.commit()
    out = _gen_out(gen)
    out["job_id"] = str(job.id)
    return out


def _enqueue(generation_id: str, operation: str = "edit") -> str | None:
    try:
        from app.config import settings
        from app.tasks.celery_app import celery_app

        if settings.app_env == "test" and celery_app.conf.task_always_eager:
            return None
        if operation == "vectorize":
            # CPU-only deterministic trace — general queue, not the GPU studio lane.
            from app.tasks.cad_trace import run_cad_trace

            task = run_cad_trace.apply_async(args=[generation_id], queue="celery")
        else:
            from app.tasks.image_generation import run_image_generation

            task = run_image_generation.apply_async(args=[generation_id], queue="studio")
        return task.id
    except Exception as exc:  # noqa: BLE001
        logger.warning("image_gen_enqueue_failed", generation_id=generation_id, error=str(exc))
        return None


@router.get("")
async def list_generations(
    limit: int = 60,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> dict:
    query = select(ImageGeneration)
    if not _is_agent_service(user):
        query = query.where(ImageGeneration.owner_sub == user.sub)
    rows = (
        await db.execute(
            query.order_by(ImageGeneration.created_at.desc())
            .limit(min(limit, 200))
            .offset(offset)
        )
    ).scalars().all()
    return {"items": [_gen_out(g) for g in rows]}


@router.get("/vectorizer-development-status")
async def vectorizer_development_status(
    _user: UserInfo = Depends(get_current_user),
) -> dict[str, Any]:
    """Expose honest holdout metrics for the CAD page.

    Keep this static route above ``/{generation_id}``, otherwise FastAPI would
    try to parse ``vectorizer-development-status`` as a UUID.
    """

    return get_cad_vectorizer_development_status()


@router.get("/{generation_id}")
async def get_generation(
    generation_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> dict:
    gen = await db.get(ImageGeneration, generation_id)
    if not _owns(gen, user):
        raise HTTPException(404, "Не найдено")
    return _gen_out(gen)


async def _owned_generation_graph(
    generation_id: uuid.UUID,
    db: AsyncSession,
    user: UserInfo,
    *,
    lock: bool = False,
):
    """Resolve the latest EMG revision without exposing cross-owner graph IDs."""
    from app.services.engineering_model_graph import latest_graph_revision, load_graph

    gen = await db.get(ImageGeneration, generation_id)
    if not _owns(gen, user):
        raise HTTPException(404, "Не найдено")
    row = await latest_graph_revision(
        db, f"image-generation:{generation_id}", lock=lock
    )
    if row is None:
        raise HTTPException(404, "EngineeringModelGraph для оцифровки ещё не создан")
    return gen, row, load_graph(row)


def _generation_graph_response(gen, row, graph) -> dict:
    return {
        "generation_id": str(gen.id),
        "source_generation_status": gen.status.value,
        "workflow_status": (
            "review_required"
            if gen.status == ImageGenStatus.failed
            else gen.status.value
        ),
        "id": str(row.id),
        "engineering_project_id": None,
        "engineering_revision_id": None,
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


@router.get("/{generation_id}/model-graph")
async def get_generation_model_graph(
    generation_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> dict:
    """Return the latest immutable EMG even when the CAD build was blocked."""
    gen, row, graph = await _owned_generation_graph(generation_id, db, user)
    return _generation_graph_response(gen, row, graph)


@router.get("/{generation_id}/model-graph/download")
async def download_generation_model_graph(
    generation_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> Response:
    """Download the latest owner-scoped immutable EMG revision."""
    _gen, row, graph = await _owned_generation_graph(generation_id, db, user)
    payload = json.dumps(
        graph.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    filename = f"{row.graph_id.replace(':', '-')}-r{row.revision}.emg.json"
    return Response(
        content=payload,
        media_type="application/vnd.ptsai.emg+json",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Engineering-Graph-Revision": str(row.revision),
            "X-Engineering-Graph-SHA256": row.canonical_sha256,
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/{generation_id}/model-graph/patches")
async def list_generation_model_graph_patches(
    generation_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> list[dict]:
    from app.db.models import GraphPatchRecord

    _gen, row, _graph = await _owned_generation_graph(generation_id, db, user)
    rows = list((await db.execute(
        select(GraphPatchRecord)
        .where(GraphPatchRecord.graph_id == row.graph_id)
        .order_by(GraphPatchRecord.created_at.desc())
    )).scalars())
    return [{
        "id": str(item.id),
        "patch_id": item.patch_id,
        "producer": item.producer,
        "pass_id": item.pass_id,
        "accepted": item.accepted,
        "payload": item.payload,
        "validation_errors": item.validation_errors,
        "result_revision_id": (
            str(item.result_revision_id) if item.result_revision_id else None
        ),
        "created_at": item.created_at,
    } for item in rows]


@router.get("/{generation_id}/model-graph/trace-proposals")
async def list_generation_model_graph_traces(
    generation_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> list[dict]:
    from app.db.models import TraceProposalRecord, VisualVerificationRun

    _gen, row, _graph = await _owned_generation_graph(generation_id, db, user)
    proposals = list((await db.execute(
        select(TraceProposalRecord)
        .where(TraceProposalRecord.graph_revision_id == row.id)
        .order_by(TraceProposalRecord.source_region_id, TraceProposalRecord.rank)
    )).scalars())
    proposal_ids = [item.id for item in proposals]
    visuals = list((await db.execute(
        select(VisualVerificationRun).where(
            VisualVerificationRun.trace_proposal_id.in_(proposal_ids)
        )
    )).scalars()) if proposal_ids else []
    visual_by_proposal = {}
    for visual in visuals:
        visual_by_proposal.setdefault(visual.trace_proposal_id, []).append(visual)
    return [{
        "id": str(item.id),
        "proposal_id": item.proposal_id,
        "source_region_id": item.source_region_id,
        "assertion_id": item.assertion_id,
        "rank": item.rank,
        "status": item.status,
        "score": item.score,
        "payload": item.payload,
        "visual_verifications": [
            run.result | {"raw_output": run.raw_output}
            for run in visual_by_proposal.get(item.id, [])
        ],
    } for item in proposals]


@router.get("/{generation_id}/model-graph/assertions/{assertion_id}/impact")
async def get_generation_assertion_impact(
    generation_id: uuid.UUID,
    assertion_id: str,
    target_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> dict:
    from app.domain.engineering_model_graph import assertion_impact_report

    _gen, _row, graph = await _owned_generation_graph(generation_id, db, user)
    try:
        return assertion_impact_report(
            graph, assertion_id, target_id
        ).model_dump(mode="json")
    except KeyError as exc:
        raise HTTPException(404, "Assertion или build target не найден") from exc


@router.post("/{generation_id}/model-graph/verify")
async def verify_generation_model_graph(
    generation_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> dict:
    from app.services.engineering_model_graph import (
        persist_verification_run,
        verify_graph,
    )

    gen, row, graph = await _owned_generation_graph(generation_id, db, user)
    state, issues = verify_graph(graph)
    run = await persist_verification_run(db, row, state, issues)
    row.comprehension_status = state.comprehension
    row.build_status = state.build
    row.release_status = state.release
    await db.commit()
    return {
        "run_id": str(run.id),
        "workflow_status": (
            "review_required"
            if gen.status == ImageGenStatus.failed
            else gen.status.value
        ),
        "state": state.model_dump(mode="json"),
        "issues": issues,
    }


@router.post("/{generation_id}/model-graph/reader-runs", status_code=202)
async def start_generation_model_reader(
    generation_id: uuid.UUID,
    target_id: str = Query(default="preview"),
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> dict:
    _gen, row, graph = await _owned_generation_graph(generation_id, db, user)
    if not any(item.id == target_id for item in graph.build_targets):
        raise HTTPException(404, "Build target не найден")
    from app.tasks.engineering_model_reader import run_engineering_model_reader

    task = run_engineering_model_reader.apply_async(
        args=[row.graph_id, target_id], queue="gpu"
    )
    return {
        "task_id": task.id,
        "generation_id": str(generation_id),
        "graph_id": row.graph_id,
        "base_revision": row.revision,
        "base_sha256": row.canonical_sha256,
        "target_id": target_id,
    }


def _assertion_source_crop(gen, graph, assertion, *, full_sheet: bool = False):
    """Resolve one assertion-owned raster ROI and return a verified PNG crop."""
    from PIL import Image

    evidence_by_id = {item.id: item for item in graph.evidence}
    sources = {item.id: item for item in graph.sources}
    regions = [
        evidence_by_id[item_id]
        for item_id in assertion.evidence_ids
        if item_id in evidence_by_id
        and evidence_by_id[item_id].kind == "raster_region"
    ]
    regions.sort(key=lambda item: bool(item.payload.get("fallback")))
    if not regions:
        raise HTTPException(404, "Для assertion нет raster SourceRegion")
    evidence = regions[0]
    source = sources.get(evidence.source_id or "")
    source_path = source.uri if source and source.uri else None
    if not source_path:
        source_path = (gen.params or {}).get("normalized_source_path")
    if not source_path:
        paths = gen.source_image_paths or []
        source_path = paths[0] if paths else None
    if not source_path:
        raise HTTPException(404, "Источник SourceRegion недоступен")
    content = download_file(source_path)
    if source and source.sha256 and hashlib.sha256(content).hexdigest() != source.sha256:
        raise HTTPException(409, "Источник SourceRegion не прошёл SHA-256 проверку")
    try:
        image = Image.open(io.BytesIO(content)).convert("RGB")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(409, "Источник SourceRegion не является изображением") from exc
    normalized = evidence.payload.get("bbox_normalized")
    pixels = evidence.payload.get("bbox")
    if isinstance(normalized, (list, tuple)) and len(normalized) == 4:
        x0, y0, x1, y1 = (float(value) for value in normalized)
        bbox = (
            max(0, round(x0 * image.width)),
            max(0, round(y0 * image.height)),
            min(image.width, round(x1 * image.width)),
            min(image.height, round(y1 * image.height)),
        )
    elif isinstance(pixels, dict) and all(
        key in pixels for key in ("x0", "y0", "x1", "y1")
    ):
        bbox = (
            max(0, round(float(pixels["x0"]))),
            max(0, round(float(pixels["y0"]))),
            min(image.width, round(float(pixels["x1"]))),
            min(image.height, round(float(pixels["y1"]))),
        )
    else:
        raise HTTPException(409, "SourceRegion не содержит поддерживаемый bbox")
    if full_sheet:
        bbox = (0, 0, image.width, image.height)
    if bbox[0] >= bbox[2] or bbox[1] >= bbox[3]:
        raise HTTPException(409, "SourceRegion выходит за границы источника")
    buffer = io.BytesIO()
    image.crop(bbox).save(buffer, format="PNG")
    return evidence, buffer.getvalue()


@router.get(
    "/{generation_id}/model-graph/assertions/{assertion_id}/source-overlay"
)
async def get_generation_assertion_source_overlay(
    generation_id: uuid.UUID,
    assertion_id: str,
    mode: Literal["sheet", "source", "candidate", "overlay", "difference"] = "source",
    proposal_id: str | None = None,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> Response:
    from app.db.models import TraceProposalRecord
    from app.domain.engineering_model_graph import TraceProposal

    gen, row, graph = await _owned_generation_graph(generation_id, db, user)
    assertion = next((item for item in graph.assertions if item.id == assertion_id), None)
    if assertion is None:
        raise HTTPException(404, "Assertion не найден")
    evidence, crop = _assertion_source_crop(
        gen, graph, assertion, full_sheet=mode == "sheet"
    )
    content = crop
    resolved_proposal_id = None
    if mode not in {"source", "sheet"}:
        query = select(TraceProposalRecord).where(
            TraceProposalRecord.graph_revision_id == row.id,
            TraceProposalRecord.assertion_id == assertion_id,
        )
        if proposal_id:
            query = query.where(TraceProposalRecord.proposal_id == proposal_id)
        proposal_row = (
            await db.execute(query.order_by(TraceProposalRecord.rank))
        ).scalars().first()
        if proposal_row is None:
            raise HTTPException(404, "Trace proposal для overlay не найден")
        try:
            proposal = TraceProposal.model_validate(proposal_row.payload)
            from app.ai.engineering_hybrid_trace import _comparison_images

            images = _comparison_images(crop, proposal)
        except (TypeError, ValueError, KeyError) as exc:
            raise HTTPException(409, "Trace proposal нельзя визуализировать") from exc
        content = images[{"candidate": 1, "overlay": 2, "difference": 3}[mode]]
        resolved_proposal_id = proposal_row.proposal_id
    return Response(
        content=content,
        media_type="image/png",
        headers={
            "Cache-Control": "private, no-store",
            "X-Source-Region-Id": evidence.source_region_id or "",
            "X-Trace-Proposal-Id": resolved_proposal_id or "",
        },
    )


@router.post(
    "/{generation_id}/model-graph/assertions/{assertion_id}/corrections"
)
async def correct_generation_model_assertion(
    generation_id: uuid.UUID,
    assertion_id: str,
    body: HumanAssertionCorrectionRequest,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> dict:
    from app.domain.engineering_model_graph import AssertionValue, GraphPatch
    from app.services.engineering_model_graph import (
        DuplicatePatchError,
        load_graph,
        merge_and_persist_patch,
    )

    gen, row, graph = await _owned_generation_graph(generation_id, db, user)
    previous = next(
        (
            item for item in graph.assertions
            if item.id == assertion_id and item.state == "active"
        ),
        None,
    )
    if previous is None:
        raise HTTPException(404, "Активный assertion не найден")
    try:
        value = TypeAdapter(AssertionValue).validate_python(body.value)
    except ValueError as exc:
        raise HTTPException(422, "Значение assertion не соответствует closed union") from exc
    digest = hashlib.sha256(
        f"{graph.graph_id}:{body.idempotency_key}".encode()
    ).hexdigest()[:20]
    params = dict(gen.params or {})
    correction_event_id = None
    compatibility = json.loads(json.dumps(
        params.get("spec_corrected") or params.get("spec") or {}
    ))
    compatibility_updated = (
        isinstance(compatibility, dict)
        and _apply_compat_spec_update(compatibility, previous, value)
    )
    if compatibility_updated:
        correction_event_id = f"human-graph:{digest}"
        params["spec_corrected"] = compatibility
        params["spec_correction_event_id"] = correction_event_id
    if body.rebuild and not compatibility_updated:
        raise HTTPException(
            422,
            "Автопересборка доступна только для assertion со значением, зеркалируемым в "
            "спецификацию (product:legacy-spec, или feature.param.*/feature.location "
            "с id от assign_stable_feature_ids) — не для feature.kind и не для правок "
            "BuildOperation без исходной Feature.",
        )
    source = next((item for item in graph.sources if item.uri), None)
    location = next(
        (item.id for item in graph.nodes if item.type == "Sheet"),
        next((item.id for item in graph.nodes if item.type == "DocumentSet"), None),
    )
    add_nodes, add_edges, replacement, add_evidence = _build_human_correction_change(
        previous=previous, value=value, unit=body.unit, note=body.note,
        actor_sub=user.sub, digest=digest,
        source_bbox_normalized=body.source_bbox_normalized,
        source=source, location_node_id=location,
    )
    patch = GraphPatch(
        patch_id=f"patch:human:{digest}",
        base_revision=graph.revision,
        base_sha256=graph.canonical_sha256,
        producer="human",
        pass_id=f"human-correction:{assertion_id}",
        idempotency_key=body.idempotency_key,
        add_nodes=add_nodes,
        add_edges=add_edges,
        add_assertions=[replacement],
        add_evidence=add_evidence,
        supersede_assertion_ids=[previous.id],
    )
    try:
        revised_row, errors = await merge_and_persist_patch(
            db, patch, expected_graph_id=row.graph_id
        )
    except DuplicatePatchError as exc:
        await db.rollback()
        raise HTTPException(409, str(exc)) from exc
    if revised_row is None:
        await db.commit()
        raise HTTPException(409, {"validation_errors": errors})
    params["engineering_model_graph"] = {
        "revision_id": str(revised_row.id),
        "graph_id": revised_row.graph_id,
        "revision": revised_row.revision,
        "canonical_sha256": revised_row.canonical_sha256,
    }
    gen.params = params
    await db.commit()
    rebuild_task_id = None
    if body.rebuild:
        from app.tasks.cad_trace import rebuild_from_spec

        task = rebuild_from_spec.apply_async(
            args=[str(generation_id), correction_event_id], queue="celery"
        )
        rebuild_task_id = task.id
    return _generation_graph_response(
        gen, revised_row, load_graph(revised_row)
    ) | {
        "compatibility_spec_updated": compatibility_updated,
        "rebuild_task_id": rebuild_task_id,
    }


@router.post("/{generation_id}/model-graph/corrections")
async def correct_generation_model_assertions_batch(
    generation_id: uuid.UUID,
    body: HumanAssertionBatchCorrectionRequest,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> dict:
    """Apply several related human corrections as one immutable GraphPatch."""
    from app.domain.engineering_model_graph import AssertionValue, GraphPatch
    from app.services.engineering_model_graph import (
        DuplicatePatchError, load_graph, merge_and_persist_patch,
    )

    gen, row, graph = await _owned_generation_graph(generation_id, db, user)
    active = {item.id: item for item in graph.assertions if item.state == "active"}
    missing = [item.assertion_id for item in body.corrections if item.assertion_id not in active]
    if missing:
        raise HTTPException(404, {"inactive_or_missing_assertion_ids": missing})
    try:
        values = {
            item.assertion_id: TypeAdapter(AssertionValue).validate_python(item.value)
            for item in body.corrections
        }
    except ValueError as exc:
        raise HTTPException(422, "Значение assertion не соответствует closed union") from exc

    patch_digest = hashlib.sha256(
        f"{graph.graph_id}:{body.idempotency_key}".encode()
    ).hexdigest()[:20]
    params = dict(gen.params or {})
    compatibility = json.loads(json.dumps(
        params.get("spec_corrected") or params.get("spec") or {}
    ))
    compatibility_updated = False
    correction_event_id = f"human-graph:{patch_digest}"
    source = next((item for item in graph.sources if item.uri), None)
    location = next(
        (item.id for item in graph.nodes if item.type == "Sheet"),
        next((item.id for item in graph.nodes if item.type == "DocumentSet"), None),
    )
    add_nodes, add_edges, add_assertions, add_evidence = [], [], [], []
    superseded_ids = []

    batch_patch_id = f"patch:human:{patch_digest}"
    for correction in body.corrections:
        previous = active[correction.assertion_id]
        value = values[correction.assertion_id]
        digest = hashlib.sha256(
            f"{graph.graph_id}:{body.idempotency_key}:{previous.id}".encode()
        ).hexdigest()[:20]
        item_nodes, item_edges, replacement, item_evidence = _build_human_correction_change(
            previous=previous, value=value, unit=correction.unit, note=body.note,
            actor_sub=user.sub, digest=digest,
            source_bbox_normalized=correction.source_bbox_normalized,
            source=source, location_node_id=location,
            batch_patch_id=batch_patch_id,
        )
        add_nodes.extend(item_nodes)
        add_edges.extend(item_edges)
        add_evidence.extend(item_evidence)
        add_assertions.append(replacement)
        superseded_ids.append(previous.id)
        compatibility_updated = (
            _apply_compat_spec_update(compatibility, previous, value)
            or compatibility_updated
        )

    if body.rebuild and not compatibility_updated:
        raise HTTPException(
            422,
            "Автопересборка доступна только для assertions со значением, зеркалируемым в "
            "спецификацию (product:legacy-spec, или feature.param.*/feature.location "
            "с id от assign_stable_feature_ids) — не для feature.kind и не для правок "
            "BuildOperation без исходной Feature.",
        )
    if compatibility_updated:
        params["spec_corrected"] = compatibility
        params["spec_correction_event_id"] = correction_event_id
    patch = GraphPatch(
        patch_id=f"patch:human:{patch_digest}", base_revision=graph.revision,
        base_sha256=graph.canonical_sha256, producer="human",
        pass_id="human-batch-correction", idempotency_key=body.idempotency_key,
        add_nodes=add_nodes, add_edges=add_edges, add_assertions=add_assertions,
        add_evidence=add_evidence, supersede_assertion_ids=superseded_ids,
    )
    try:
        revised_row, errors = await merge_and_persist_patch(
            db, patch, expected_graph_id=row.graph_id
        )
    except DuplicatePatchError as exc:
        await db.rollback()
        raise HTTPException(409, str(exc)) from exc
    if revised_row is None:
        await db.commit()
        raise HTTPException(409, {"validation_errors": errors})
    params["engineering_model_graph"] = {
        "revision_id": str(revised_row.id), "graph_id": revised_row.graph_id,
        "revision": revised_row.revision,
        "canonical_sha256": revised_row.canonical_sha256,
    }
    gen.params = params
    await db.commit()
    rebuild_task_id = None
    if body.rebuild:
        from app.tasks.cad_trace import rebuild_from_spec

        rebuild_task_id = rebuild_from_spec.apply_async(
            args=[str(generation_id), correction_event_id], queue="celery"
        ).id
    return _generation_graph_response(gen, revised_row, load_graph(revised_row)) | {
        "compatibility_spec_updated": compatibility_updated,
        "corrected_assertion_ids": superseded_ids,
        "rebuild_task_id": rebuild_task_id,
    }


def _cad_trace_payload(gen: ImageGeneration, key: str) -> Any:
    params = gen.params or {}
    if key == "cad_reading":
        return params.get(key) or {
            "spec": params.get("spec"),
            "followup": params.get("spec_followup") or [],
            "crosscheck": params.get("spec_crosscheck"),
            "unresolved": params.get("spec_review_warnings") or [],
            "assumptions": params.get("spec_assumptions") or [],
        }
    return params.get(key)


@router.get("/{generation_id}/cad-process")
async def get_cad_process(
    generation_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> dict[str, Any]:
    """Live, durable timeline of drawing read, normalization and drafting.

    Events are committed while the worker runs, so this remains useful when a
    provider hangs, the task times out, or no CadIR/result was ever produced.
    """

    gen = await db.get(ImageGeneration, generation_id)
    if not _owns(gen, user):
        raise HTTPException(404, "Не найдено")
    process = _cad_trace_payload(gen, "cad_process")
    if not process:
        raise HTTPException(404, "Журнал этапов ещё не создан")
    return process


@router.get("/{generation_id}/cad-model-outputs")
async def get_cad_model_outputs(
    generation_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> dict[str, Any]:
    """Full CAD-reader prompts, answers and thinking, loaded only on demand."""

    gen = await db.get(ImageGeneration, generation_id)
    if not _owns(gen, user):
        raise HTTPException(404, "Не найдено")
    outputs = list((gen.params or {}).get("cad_model_outputs") or [])
    return {
        "generation_id": str(generation_id),
        "count": len(outputs),
        "outputs": outputs,
    }


@router.get("/{generation_id}/cad-reading")
async def get_cad_reading(
    generation_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> dict[str, Any]:
    """Everything established by the reader before 3D normalization."""
    gen = await db.get(ImageGeneration, generation_id)
    if not _owns(gen, user):
        raise HTTPException(404, "Не найдено")
    reading = _cad_trace_payload(gen, "cad_reading")
    if not reading or not reading.get("spec"):
        raise HTTPException(404, "Результат чтения ещё не сохранён")
    return reading


@router.get("/{generation_id}/solid-input")
async def get_solid_input(
    generation_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> dict[str, Any]:
    """Exact payload prepared for the CAD kernel, including canonical SHA."""
    gen = await db.get(ImageGeneration, generation_id)
    if not _owns(gen, user):
        raise HTTPException(404, "Не найдено")
    solid_input = _cad_trace_payload(gen, "solid_input")
    if not solid_input:
        raise HTTPException(404, "Вход 3D-модели ещё не сформирован")
    return solid_input


@router.get("/{generation_id}/solid-input/diff")
async def get_solid_input_diff(
    generation_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> dict[str, Any]:
    """Readable boundary between model reading and deterministic compilation."""
    gen = await db.get(ImageGeneration, generation_id)
    if not _owns(gen, user):
        raise HTTPException(404, "Не найдено")
    reading = _cad_trace_payload(gen, "cad_reading") or {}
    solid_input = _cad_trace_payload(gen, "solid_input") or {}
    solid = (gen.params or {}).get("solid_3d") or {}
    candidate = (solid_input.get("payload") or {}).get("candidate") or {}
    gate = solid.get("build_gate") or {}
    return {
        "read": reading.get("spec"),
        "normalized_feature_tree": candidate,
        "kernel_payload_sha256": solid_input.get("sha256"),
        "blockers": gate.get("blockers") or solid.get("blockers") or [],
        "warnings": gate.get("warnings") or solid.get("warnings") or [],
        "excluded": candidate.get("missing_data") or [],
        "build_status": solid.get("build_status") or "blocked",
        "kernel_result": solid.get("kernel_report"),
        "verification": solid.get("verification"),
    }


@router.get("/{generation_id}/result")
async def get_result(
    generation_id: uuid.UUID,
    thumb: bool = False,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> Response:
    gen = await db.get(ImageGeneration, generation_id)
    if not _owns(gen, user):
        raise HTTPException(404, "Не найдено")
    path = (gen.thumbnail_path if thumb else gen.result_path) or gen.result_path
    if not path:
        raise HTTPException(404, "Результат ещё не готов")
    data = download_file(path)
    return Response(content=data, media_type="image/png")


@router.get("/{generation_id}/source")
async def get_source(
    generation_id: uuid.UUID,
    index: int = 0,
    variant: Literal["original", "normalized"] = "original",
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> Response:
    gen = await db.get(ImageGeneration, generation_id)
    if not _owns(gen, user):
        raise HTTPException(404, "Не найдено")
    paths = gen.source_image_paths or []
    if variant == "normalized":
        normalized_path = (gen.params or {}).get("normalized_source_path")
        if normalized_path:
            paths = [normalized_path]
    if index >= len(paths):
        raise HTTPException(404, "Источник не найден")
    try:
        data = download_file(paths[index])
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "image_gen_source_missing",
            generation_id=str(generation_id),
            source_path=paths[index],
            error=str(exc),
        )
        raise HTTPException(404, "Файл источника не найден") from exc
    return Response(content=data, media_type="image/png")


_ARTIFACT_MEDIA_TYPES = {
    "dxf": "application/dxf",
    "dwg": "application/acad",
    "svg": "image/svg+xml",
    "ir": "application/json",
    "step": "model/step",
    "iges": "model/iges",
    "fcstd": "application/vnd.freecad",
    "stl": "model/stl",
    "pdf": "application/pdf",
    # Ф3.1/3.2: per-face topology mesh (content-stable face keys) for the
    # interactive 3D viewer's raycasting.
    "topology": "application/json",
}


@router.get("/{generation_id}/solid-preview")
async def get_solid_preview(
    generation_id: uuid.UUID,
    kind: Literal["step", "iges", "stl", "topology"] = "stl",
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> Response:
    """Serve the explicitly incomplete 3D draft for visual review.

    This endpoint is intentionally separate from ``/artifact``: preview files
    may be inspected before approval, but are never release artifacts and must
    not bypass the accepted-revision gate.
    """
    gen = await db.get(ImageGeneration, generation_id)
    if not _owns(gen, user):
        raise HTTPException(404, "Не найдено")
    solid = dict((gen.params or {}).get("solid_3d") or {})
    if not solid.get("built") or solid.get("build_status") != "preview_review_required":
        raise HTTPException(409, "Проверочный 3D-черновик для этой генерации отсутствует.")
    path = (solid.get("paths") or {}).get(kind)
    if not path:
        raise HTTPException(404, "Файл проверочного 3D-черновика не найден")
    try:
        data = download_file(path)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(404, "Файл проверочного 3D-черновика недоступен") from exc
    return Response(
        content=data,
        media_type=_ARTIFACT_MEDIA_TYPES[kind],
        headers={
            "Cache-Control": "no-store",
            "X-CAD-Artifact-Status": "preview-review-required",
        },
    )


@router.get("/{generation_id}/artifact")
async def get_artifact(
    generation_id: uuid.UUID,
    kind: Literal[
        "dxf", "dwg", "svg", "ir", "step", "iges", "fcstd", "stl", "pdf", "topology",
    ] = "dxf",
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> Response:
    gen = await db.get(ImageGeneration, generation_id)
    if not _owns(gen, user):
        raise HTTPException(404, "Не найдено")
    params = gen.params or {}
    if kind in ("step", "iges", "fcstd", "stl", "topology"):
        revision, _ir = await _load_current_ir(db, gen)
        if (
            not gen.accepted
            or gen.accepted_revision != revision.revision
            or params.get("cad_artifact_revision") != revision.revision
        ):
            raise HTTPException(409, "3D-артефакт не относится к текущей утверждённой ревизии.")
    if kind in ("dxf", "dwg", "pdf") and gen.operation == "vectorize":
        _revision, current_ir = await _load_current_ir(db, gen)
        if current_ir.scale is None or current_ir.scale_source is None:
            raise HTTPException(
                409,
                "Метрический масштаб не подтверждён — укажите мм/px или формат листа перед CAD-экспортом.",
            )
    path = params.get(f"{kind}_path")
    if not path and kind == "pdf":
        # Print PDF is derived lazily from the master DXF artifact (I4) and
        # cached: same layers/linetypes/lineweights, rendered vector-to-vector.
        dxf_path = params.get("dxf_path")
        if not dxf_path:
            raise HTTPException(404, "Артефакт не найден")
        from anyio import to_thread

        from app.ai.cad_ir.dxf_render import render_dxf_to_pdf

        dxf_data = await to_thread.run_sync(download_file, dxf_path)
        pdf_data = await to_thread.run_sync(render_dxf_to_pdf, dxf_data)
        path = dxf_path.rsplit(".", 1)[0] + ".pdf"
        await to_thread.run_sync(lambda: upload_file(pdf_data, path, _ARTIFACT_MEDIA_TYPES["pdf"]))
        gen.params = {**params, "pdf_path": path}
        await db.commit()
        return Response(content=pdf_data, media_type=_ARTIFACT_MEDIA_TYPES["pdf"])
    if not path and kind == "dwg":
        # DWG is derived lazily from the master DXF artifact and cached.
        dxf_path = params.get("dxf_path")
        if not dxf_path:
            raise HTTPException(404, "Артефакт не найден")
        from anyio import to_thread

        from app.services.dwg_convert import DwgConversionError, convert_dxf_to_dwg

        dxf_data = await to_thread.run_sync(download_file, dxf_path)
        try:
            dwg_data = await to_thread.run_sync(convert_dxf_to_dwg, dxf_data)
        except DwgConversionError as exc:
            raise HTTPException(
                422,
                f"{exc} — DWG-запись в LibreDWG экспериментальна; используйте DXF, "
                "любой CAD откроет и сохранит его как DWG",
            ) from exc
        path = dxf_path.rsplit(".", 1)[0] + ".dwg"
        await to_thread.run_sync(lambda: upload_file(dwg_data, path, _ARTIFACT_MEDIA_TYPES["dwg"]))
        gen.params = {**params, "dwg_path": path}
        await db.commit()
        return Response(content=dwg_data, media_type=_ARTIFACT_MEDIA_TYPES["dwg"])
    if not path:
        raise HTTPException(404, "Артефакт не найден")
    data = download_file(path)
    from app.core import metrics

    metrics.cad_export_total.labels(kind=kind, status="ok").inc()
    return Response(content=data, media_type=_ARTIFACT_MEDIA_TYPES.get(kind, "application/octet-stream"))


# ── Accept / iterate / delete ────────────────────────────────────────────────


def _is_agent_service_call(request: Request) -> bool:
    """True when this REST call was proxied by the internal agent capability
    dispatcher (``X-API-Key`` matches ``AGENT_SERVICE_KEY``), as opposed to a
    human browser session (cookie/JWT auth, no service key). Only meaningful
    when ``agent_service_key`` is configured — same caveat as
    ``capability_router._request_has_internal_approval``: without a
    configured key this signal can't distinguish caller identity either.
    """
    from app.config import settings

    return bool(settings.agent_service_key) and request.headers.get("X-API-Key") == settings.agent_service_key


@router.post("/{generation_id}/accept")
async def accept_generation(
    generation_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> dict:
    gen = await db.get(ImageGeneration, generation_id)
    if not _owns(gen, user):
        raise HTTPException(404, "Не найдено")
    if gen.status != ImageGenStatus.done:
        raise HTTPException(400, "Можно принять только готовый результат.")
    if gen.operation in _GATED_OPERATIONS and _is_agent_service_call(request):
        # capabilities.yml gates "accept_techdraw"/"accept_vectorize" (routed
        # to DIFFERENT REST paths below), not "accept" — an agent could
        # otherwise dodge the gate by simply calling the ungated action name
        # for the same id. A human clicking "Принять" in the Studio UI (no
        # service-key header) is unaffected — their click IS the approval.
        action = "accept_techdraw" if gen.operation == "techdraw" else "accept_vectorize"
        raise HTTPException(
            423,
            {
                "error_code": "approval_required",
                "message": (
                    "Приёмка точного чертежа требует подтверждения человека "
                    f"(используйте action={action})."
                ),
            },
        )
    gen.accepted = True
    gen.accepted_by = user.sub
    gen.accepted_at = datetime.now(timezone.utc)
    await db.commit()
    return _gen_out(gen)


@router.post("/{generation_id}/accept-techdraw")
async def accept_techdraw_generation(
    generation_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> dict:
    """Approval-gated acceptance of an exact (techdraw) drawing.

    Reachable by the agent only via the gated ``image_studio.accept_techdraw``
    capability action (see capabilities.yml); a human can also call it
    directly (e.g. a future dedicated UI button) with no extra ceremony —
    gating only applies to the agent's capability dispatch, not to humans.
    """
    gen = await db.get(ImageGeneration, generation_id)
    if not _owns(gen, user):
        raise HTTPException(404, "Не найдено")
    if gen.status != ImageGenStatus.done:
        raise HTTPException(400, "Можно принять только готовый результат.")
    if gen.operation != "techdraw":
        raise HTTPException(400, "Это не точный чертёж — используйте /accept.")
    gen.accepted = True
    gen.accepted_by = user.sub
    gen.accepted_at = datetime.now(timezone.utc)
    await db.commit()
    return _gen_out(gen)


class SpecCorrectionRequest(BaseModel):
    """Field-level corrections to what the CAD reader extracted.

    Only the fields a person actually supplies are applied; the rest keeps what
    the reader produced. Every correction becomes a training example for the
    reader — the corpus that item 6 of the digitize plan needs and that the
    existing geometry-only exporter does not collect.
    """

    part: str | None = None
    material: str | None = None
    designation: str | None = None
    scale: str | None = None
    mass: str | None = None
    body_type: str | None = None
    outer: list[dict[str, Any]] | None = None
    bore: list[dict[str, Any]] | None = None
    profile: dict[str, Any] | None = None
    dimensions: list[dict[str, Any]] | None = None
    annotations: list[dict[str, Any]] | None = None
    views: list[dict[str, Any]] | None = None
    chamfers: list[dict[str, Any]] | None = None
    grooves: list[dict[str, Any]] | None = None
    keyways: list[dict[str, Any]] | None = None
    cross_holes: list[dict[str, Any]] | None = None
    axial_holes: list[dict[str, Any]] | None = None
    circular_hole_patterns: list[dict[str, Any]] | None = None
    # Rebuild the part and the sheet from the corrected reading. Off by default:
    # a correction is worth recording even when the person is not ready to see
    # the consequences of it yet.
    rebuild: bool = False


@router.post("/{generation_id}/spec-correction")
async def correct_vectorize_spec(
    generation_id: uuid.UUID,
    body: SpecCorrectionRequest,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> dict:
    """Record what the reader got wrong on this sheet.

    The corrected spec is stored beside the original read, never replacing it:
    the pair IS the training signal, and keeping only the corrected version
    would throw away the half that says what to learn.
    """
    from app.ai.cad_reader_feedback import (
        build_correction_record,
        merge_correction,
        reconcile_corrected_feature_blockers,
    )

    gen = await db.get(ImageGeneration, generation_id)
    if not _owns(gen, user):
        raise HTTPException(404, "Не найдено")
    params = dict(gen.params or {})
    original_read_spec = params.get("spec")
    if not original_read_spec:
        raise HTTPException(400, "У этой оцифровки нет прочитанного спека")
    # Corrections are cumulative. The immutable original remains the left side
    # of the training pair, while a later edit starts from the last corrected
    # state instead of silently undoing an earlier human decision.
    correction_base = params.get("spec_corrected") or original_read_spec

    payload = body.model_dump()
    rebuild = bool(payload.pop("rebuild", False))
    supplied = {key: value for key, value in payload.items() if value is not None}
    if not supplied and not rebuild:
        raise HTTPException(400, "Не передано ни одного исправления")

    record = params.get("spec_correction_record") or {"diff": []}
    correction_event_id = params.get("spec_correction_event_id")
    if supplied:
        correction_event_id = str(uuid.uuid4())
        corrected = merge_correction(correction_base, supplied)
        corrected = reconcile_corrected_feature_blockers(corrected, set(supplied))
        from pydantic import ValidationError

        from app.ai.cad_recognize.spec_vectorize import EngineeringDrawingSpec

        try:
            corrected = EngineeringDrawingSpec.model_validate(corrected).model_dump(
                mode="json"
            )
        except ValidationError as exc:
            raise HTTPException(
                422,
                {
                    "message": "Исправленная спецификация не прошла проверку",
                    "fields": [
                        ".".join(str(part) for part in error["loc"])
                        for error in exc.errors()[:12]
                    ],
                },
            ) from exc
        record = build_correction_record(
            generation_id=str(generation_id),
            source_path=params.get("normalized_source_path"),
            read_spec=original_read_spec,
            corrected_spec=corrected,
            corrected_by=getattr(user, "sub", None),
            reader_models=(
                (params.get("cad_pipeline_manifest") or {})
                .get("components", {})
                .get("spec_reader", {})
                .get("models", [])
            ),
        )
        record["correction_event_id"] = correction_event_id
        params["spec_corrected"] = corrected
        params["spec_correction_record"] = record
        params["spec_correction_event_id"] = correction_event_id
        params["spec_correction_history"] = [
            *(params.get("spec_correction_history") or []),
            record,
        ]
        gen.params = params
        await db.commit()

    task_id = None
    if rebuild:
        # Rebuilding never re-reads the drawing: the reading is the expensive,
        # fallible half, and once a person has fixed a value, asking the model
        # again would be slower AND might come back with a different mistake.
        from app.tasks.cad_trace import rebuild_from_spec

        task = rebuild_from_spec.apply_async(
            args=[str(generation_id), correction_event_id],
            queue="celery",
        )
        task_id = task.id
    return {
        "ok": True,
        "diff": record["diff"],
        "correction_event_id": correction_event_id,
        "rebuild_task_id": task_id,
    }


@router.patch("/{generation_id}/cad-reading")
async def patch_cad_reading(
    generation_id: uuid.UUID,
    body: SpecCorrectionRequest,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> dict:
    """Public audit-oriented alias for field corrections to the saved read."""
    return await correct_vectorize_spec(generation_id, body, db, user)


@router.post("/{generation_id}/solid-input/rebuild")
async def rebuild_solid_input(
    generation_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> dict:
    """Recompile the stored/corrected reading without another VLM call."""
    return await correct_vectorize_spec(
        generation_id,
        SpecCorrectionRequest(rebuild=True),
        db,
        user,
    )


@router.post("/{generation_id}/accept-vectorize")
async def accept_vectorize_generation(
    generation_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> dict:
    """Approval-gated acceptance of a vectorized (scan→DXF) drawing — same
    contract as accept-techdraw: the agent reaches this only through the
    gated ``image_studio.accept_vectorize`` action; a human's direct call is
    itself the approval. Blocking validation issues must be resolved first."""
    gen = await db.get(ImageGeneration, generation_id)
    if not _owns(gen, user):
        raise HTTPException(404, "Не найдено")
    if gen.status != ImageGenStatus.done:
        raise HTTPException(400, "Можно принять только готовый результат.")
    if gen.operation != "vectorize":
        raise HTTPException(400, "Это не оцифрованный чертёж — используйте /accept.")
    from app.ai.cad_validate import validate_ir

    stored_errors = int(((gen.params or {}).get("validation") or {}).get("errors") or 0)
    if stored_errors:
        raise HTTPException(
            409,
            f"В отчёте валидации {stored_errors} блокирующих ошибок — исправьте их в редакторе перед приёмкой.",
        )
    revision, ir = await _load_current_ir(db, gen)
    checked_revision = (gen.params or {}).get("full_check_revision")
    if checked_revision != revision.revision:
        raise HTTPException(
            409,
            "Текущая ревизия не прошла полную проверку — запустите её после последнего изменения.",
        )
    if (gen.params or {}).get("full_check_status") not in ("passed", "findings"):
        raise HTTPException(
            409,
            "Полная проверка не завершилась успешно; недоступность модели не считается проверкой.",
        )
    params = dict(gen.params or {})
    if params.get("vectorize_method") == "spec":
        solid = dict(params.get("solid_3d") or {})
        geometry_ok = bool((solid.get("verification") or {}).get("ok"))
        feature_complete = bool(
            (solid.get("verification") or {}).get("feature_complete", True)
        )
        if not solid.get("built") or not geometry_ok or not feature_complete:
            raise HTTPException(
                409,
                "3D-модель не прошла геометрическую и feature-верификацию; исправьте чтение и пересоберите.",
            )
        source_check = solid.get("source_projection_verification") or {}
        paired = source_check.get("paired_comparison") or {}
        if not (
            source_check.get("ok") is True
            and source_check.get("status") == "paired_full_check_passed"
            and source_check.get("revision") == revision.revision
            and paired.get("ok") is True
        ):
            raise HTTPException(
                409,
                "Исходный чертёж и текущий рендер не прошли обязательную парную сверку.",
            )
    if any(not region.resolved for region in ir.unresolved_regions):
        raise HTTPException(
            409,
            "В чертеже остались нераспознанные области — обработайте их перед приёмкой.",
        )
    errors = len(validate_ir(ir).blocking)
    if errors:
        raise HTTPException(
            409,
            f"В отчёте валидации {errors} блокирующих ошибок — исправьте их в редакторе перед приёмкой.",
        )
    # No unresolved recognition hypothesis may cross the release boundary.
    open_review = {r.entity_id for r in ir.review if not r.resolved}
    if open_review:
        raise HTTPException(
            409,
            f"Неразрешённых элементов в очереди проверки: {len(open_review)} — "
            "подтвердите, исправьте или удалите их перед приёмкой.",
        )
    accepted_at = datetime.now(timezone.utc)
    gen.accepted = True
    gen.accepted_by = user.sub
    gen.accepted_at = accepted_at
    gen.accepted_revision = revision.revision
    revision.approved_by = user.sub
    revision.approved_at = accepted_at
    if params.get("vectorize_method") == "spec":
        solid = dict(params.get("solid_3d") or {})
        solid["build_status"] = "verified"
        solid["source_projection_verification"] = {
            **(solid.get("source_projection_verification") or {}),
            "ok": True,
            "approval_status": "human_approved_after_paired_full_check",
            "revision": revision.revision,
            "approved_by": user.sub,
        }
        params["solid_3d"] = solid
        gen.params = params
    await db.commit()
    return _gen_out(gen)


def _has_any_role(user: UserInfo, *roles: UserRole) -> bool:
    return UserRole.admin in user.roles or any(role in user.roles for role in roles)


@router.get("/{generation_id}/certification")
async def get_vectorize_certification(
    generation_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> dict:
    """Return the certificate of the current revision; stale signatures never carry forward."""
    from app.services.cad_certification import certification_out

    gen = await db.get(ImageGeneration, generation_id)
    if not _owns(gen, user):
        raise HTTPException(404, "Не найдено")
    revision, _ir = await _load_current_ir(db, gen)
    row = (
        await db.execute(
            select(CadCertification).where(CadCertification.cad_ir_revision_id == revision.id)
        )
    ).scalar_one_or_none()
    if row is None:
        return {"revision": revision.revision, "profile": "auto", "status": "draft"}
    return certification_out(row, revision)


@router.post("/{generation_id}/certification/drafter-approve")
async def approve_vectorize_as_drafter(
    generation_id: uuid.UUID,
    body: CadCertificationRequest,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> dict:
    from app.services.cad_certification import (
        CertificationBlocked,
        approve_by_drafter,
        certification_out,
    )

    if not _has_any_role(user, UserRole.engineer):
        raise HTTPException(403, "Подписать как чертёжник может только инженер.")
    gen = await db.get(ImageGeneration, generation_id)
    if not _owns(gen, user):
        raise HTTPException(404, "Не найдено")
    if gen.operation != "vectorize" or gen.status != ImageGenStatus.done:
        raise HTTPException(400, "Сертифицировать можно только готовую оцифровку.")
    revision, ir = await _load_current_ir(db, gen)
    try:
        row = await approve_by_drafter(
            db, revision, ir, actor_sub=user.sub, profile=body.profile
        )
    except CertificationBlocked as exc:
        raise HTTPException(409, str(exc)) from exc
    await db.commit()
    return certification_out(row, revision)


@router.post("/{generation_id}/certification/normcontrol-approve")
async def approve_vectorize_as_normcontroller(
    generation_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> dict:
    from app.services.cad_certification import (
        CertificationBlocked,
        approve_by_normcontroller,
        certification_out,
    )

    if not _has_any_role(user, UserRole.normcontroller):
        raise HTTPException(403, "Финальную подпись может поставить только нормоконтролёр.")
    gen = await db.get(ImageGeneration, generation_id)
    if not _owns(gen, user):
        raise HTTPException(404, "Не найдено")
    if gen.operation != "vectorize" or gen.status != ImageGenStatus.done:
        raise HTTPException(400, "Сертифицировать можно только готовую оцифровку.")
    revision, ir = await _load_current_ir(db, gen)
    try:
        row = await approve_by_normcontroller(
            db, gen, revision, ir, actor_sub=user.sub
        )
    except CertificationBlocked as exc:
        raise HTTPException(409, str(exc)) from exc
    await db.commit()
    return certification_out(row, revision)


async def _build_manifest(db: AsyncSession, gen: ImageGeneration) -> dict:
    from app.ai.cad_validate import validate_ir
    from app.services.cad_release import ReleaseBlocked, build_release_manifest

    revision, ir = await _load_current_ir(db, gen)
    certificate = (
        await db.execute(
            select(CadCertification).where(
                CadCertification.cad_ir_revision_id == revision.id,
                CadCertification.status == "certified",
            )
        )
    ).scalar_one_or_none()
    if certificate is None:
        raise HTTPException(
            409,
            "Текущая ревизия не имеет двух независимых подписей чертёжника и нормоконтролёра.",
        )
    validate_ir(ir)  # freshest report; blocking issues are re-derived here
    try:
        return build_release_manifest(
            generation_id=str(gen.id),
            revision=revision.revision,
            ir=ir,
            stored_ir_sha256=revision.ir_sha256,
            stored_artifact_hashes=revision.artifact_hashes or {},
            accepted=bool(gen.accepted),
            accepted_by=gen.accepted_by,
            accepted_at=gen.accepted_at.isoformat() if gen.accepted_at else None,
            accepted_revision=gen.accepted_revision,
            approved_by=revision.approved_by,
            approved_at=revision.approved_at.isoformat() if revision.approved_at else None,
            certification={
                "status": certificate.status,
                "profile": certificate.profile,
                "drafter_approved_by": certificate.drafter_approved_by,
                "drafter_approved_at": certificate.drafter_approved_at.isoformat()
                if certificate.drafter_approved_at else None,
                "normcontrol_approved_by": certificate.normcontrol_approved_by,
                "normcontrol_approved_at": certificate.normcontrol_approved_at.isoformat()
                if certificate.normcontrol_approved_at else None,
                "manifest_hash": certificate.manifest_hash,
            },
        )
    except ReleaseBlocked as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/{generation_id}/release-manifest")
async def get_release_manifest(
    generation_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> dict:
    """C5: reproducible release manifest for an accepted CAD drawing —
    CAD IR + artifact hashes (with a deterministic re-render check),
    validation report and approval trail, all under one manifest hash.
    409 until the drawing is accepted and free of blocking ЕСКД issues."""
    gen = await db.get(ImageGeneration, generation_id)
    if not _owns(gen, user):
        raise HTTPException(404, "Не найдено")
    if gen.operation != "vectorize":
        raise HTTPException(400, "Выпуск определён только для оцифрованных чертежей.")
    return await _build_manifest(db, gen)


@router.get("/{generation_id}/release-package")
async def get_release_package(
    generation_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> Response:
    """C5: the release bundle as a zip — DXF (R2010), SVG, the CAD IR JSON and
    manifest.json. Same release gate as the manifest."""
    import io
    import json
    import zipfile

    gen = await db.get(ImageGeneration, generation_id)
    if not _owns(gen, user):
        raise HTTPException(404, "Не найдено")
    if gen.operation != "vectorize":
        raise HTTPException(400, "Выпуск определён только для оцифрованных чертежей.")
    manifest = await _build_manifest(db, gen)
    params = gen.params or {}

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        for kind, name in (("dxf", "drawing.dxf"), ("svg", "drawing.svg"), ("ir", "cad_ir.json")):
            path = params.get(f"{kind}_path")
            if path:
                try:
                    zf.writestr(name, download_file(path))
                except Exception:  # noqa: BLE001 — a missing derived file must not sink the bundle
                    pass
    data = buf.getvalue()
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="release-{gen.id}.zip"'},
    )


def _invalidate_vector_approval(gen: ImageGeneration) -> None:
    """A new current revision is unchecked and never inherits approval."""
    if gen.operation != "vectorize":
        return
    params = dict(gen.params or {})
    params.pop("full_check_revision", None)
    params.pop("full_check_status", None)
    params.pop("full_check_source_comparison", None)
    solid = dict(params.get("solid_3d") or {})
    source_check = dict(solid.get("source_projection_verification") or {})
    if source_check:
        source_check.update({"ok": False, "status": "stale_after_revision_change"})
        solid["source_projection_verification"] = source_check
        params["solid_3d"] = solid
    gen.params = params
    if gen.accepted:
        gen.accepted = False
        gen.accepted_by = None
        gen.accepted_at = None
        gen.accepted_revision = None


@router.post("/{generation_id}/ir/full-check")
async def run_full_check(
    generation_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> dict:
    """Ф7.2: run levels 6-7 (LLM normcontrol + VLM visual critique) on top
    of the already-current deterministic levels 1-5 report, and save the
    merged result as a new revision. Explicitly opt-in (a separate call, not
    automatic on every PATCH) — the human decides when a model opinion is
    worth the latency/cost, per the module's "LLM strictly at the end"
    design. Any previous levels 6-7 issues are replaced, not accumulated:
    they're a judgement about a specific render, stale the moment the
    drawing changes again."""
    from app.ai.cad_validate import FullCheckUnavailableError, run_llm_review_levels
    from app.ai.norm_citation import resolve_norm_citations
    from app.services import cad_ir_store

    gen = await db.get(ImageGeneration, generation_id)
    if not _owns(gen, user):
        raise HTTPException(404, "Не найдено")
    revision, ir = await _load_current_ir(db, gen)
    if not gen.result_path:
        raise HTTPException(409, "Нет рендера для проверки — сначала сохраните ревизию.")
    png_bytes = download_file(gen.result_path)
    params = dict(gen.params or {})
    paired_required = params.get("vectorize_method") == "spec"
    source_png_bytes = None
    source_path = params.get("normalized_source_path")
    if not source_path and gen.source_image_paths:
        source_path = gen.source_image_paths[0]
    if source_path:
        try:
            source_png_bytes = download_file(source_path)
        except Exception:  # noqa: BLE001 — handled as a fail-closed missing source below
            source_png_bytes = None
    if paired_required and source_png_bytes is None:
        gen.params = {**params, "full_check_status": "source_missing"}
        await db.commit()
        raise HTTPException(
            409,
            "Парная проверка не выполнена: исходное изображение недоступно.",
        )

    try:
        llm_issues = await run_llm_review_levels(
            png_bytes,
            source_png_bytes=source_png_bytes,
            confidential=True,
            strict=True,
        )
    except FullCheckUnavailableError as exc:
        gen.params = {
            **(gen.params or {}),
            "full_check_status": "unavailable",
        }
        await db.commit()
        raise HTTPException(
            503,
            "Полная проверка не выполнена: локальная модель недоступна. "
            "Ревизия не считается проверенной.",
        ) from exc
    kept = [i for i in ir.validation.issues if i.code not in ("NORMCONTROL_LLM", "VLM_CRITIC")]
    ir.validation.issues = await resolve_norm_citations(kept + llm_issues, db)

    _invalidate_vector_approval(gen)
    row = await cad_ir_store.save_revision(db, gen, ir, origin="llm_review", created_by=user.sub)
    visual_issues = [issue for issue in llm_issues if issue.code == "VLM_CRITIC"]
    comparison = {
        "required": paired_required,
        "ran": source_png_bytes is not None,
        "ok": source_png_bytes is not None and not visual_issues,
        "revision": row.revision,
        "source_path": source_path,
        "result_path": gen.result_path,
        "issues": [issue.message_ru for issue in visual_issues],
        "method": "paired_vlm_source_vs_generated_render",
    }
    updated_params = {
        **(gen.params or {}),
        "full_check_revision": row.revision,
        "full_check_status": "findings" if llm_issues else "passed",
        "full_check_source_comparison": comparison,
    }
    if paired_required:
        solid = dict(updated_params.get("solid_3d") or {})
        previous = dict(solid.get("source_projection_verification") or {})
        sheet_ok = bool(
            (((solid.get("sheet") or {}).get("verification") or {}).get("ok"))
        )
        comparison_ok = bool(comparison["ok"] and sheet_ok)
        solid["source_projection_verification"] = {
            **previous,
            "ok": comparison_ok,
            "status": (
                "paired_full_check_passed" if comparison_ok
                else "paired_full_check_findings"
            ),
            "paired_comparison": comparison,
            "revision": row.revision,
        }
        updated_params["solid_3d"] = solid
    gen.params = updated_params
    await db.commit()
    return {
        "revision": row.revision,
        "origin": row.origin,
        "summary": row.summary,
        "source_comparison": comparison,
        "ir": ir.model_dump(),
    }


class AddedFeatureRequest(BaseModel):
    kind: Literal["boss", "pocket", "fillet", "chamfer", "shell", "thread"]
    profile: Literal["circle", "rectangle"] | None = None
    center_x_mm: float | None = Field(default=None, ge=0, le=100_000)
    center_y_mm: float | None = Field(default=None, ge=0, le=100_000)
    depth_mm: float | None = Field(default=None, gt=0, le=100_000)
    diameter_mm: float | None = Field(default=None, gt=0, le=100_000)
    width_mm: float | None = Field(default=None, gt=0, le=100_000)
    height_mm: float | None = Field(default=None, gt=0, le=100_000)
    edge_key: str | None = Field(default=None, min_length=16, max_length=128)
    size_mm: float | None = Field(default=None, gt=0, le=100_000)
    # D3: shell wall thickness; cosmetic-thread designation per ГОСТ 2.311
    thickness_mm: float | None = Field(default=None, gt=0, le=10_000)
    spec: str | None = Field(default=None, min_length=2, max_length=40)
    pitch_mm: float | None = Field(default=None, gt=0, le=100)

    @model_validator(mode="after")
    def validate_profile_dimensions(self) -> "AddedFeatureRequest":
        if self.kind == "shell":
            if self.thickness_mm is None:
                raise ValueError("shell требует thickness_mm")
            return self
        if self.kind == "thread":
            if self.spec is None or self.diameter_mm is None:
                raise ValueError("thread требует spec и diameter_mm")
            return self
        if self.thickness_mm is not None or self.spec is not None or self.pitch_mm is not None:
            raise ValueError("thickness_mm/spec/pitch_mm применимы только к shell/thread")
        if self.kind in ("fillet", "chamfer"):
            if self.edge_key is None or self.size_mm is None:
                raise ValueError("Операция ребра требует edge_key и size_mm")
            if any(value is not None for value in (
                self.profile, self.center_x_mm, self.center_y_mm, self.depth_mm,
                self.diameter_mm, self.width_mm, self.height_mm,
            )):
                raise ValueError("Операция ребра не принимает параметры профиля")
            return self
        if self.profile is None or self.center_x_mm is None or self.center_y_mm is None or self.depth_mm is None:
            raise ValueError("Операция тела требует профиль, центр и глубину")
        if self.edge_key is not None or self.size_mm is not None:
            raise ValueError("Операция тела не принимает параметры ребра")
        if self.profile == "circle":
            if self.diameter_mm is None or self.width_mm is not None or self.height_mm is not None:
                raise ValueError("Круглый профиль требует только diameter_mm")
        elif self.width_mm is None or self.height_mm is None or self.diameter_mm is not None:
            raise ValueError("Прямоугольный профиль требует width_mm и height_mm")
        return self


class AddNativeFeatureRequest(BaseModel):
    """Ф2-Ф3 нового CAD-редактора (/root/.claude/plans/starry-mapping-hippo.md):
    add one human-authored feature on top of the CURRENT EMG graph. The old
    2D-IR-candidate add-feature endpoint (Cad3dPanel.tsx's
    POST .../ir/feature-tree-candidates/{index}/step) has been removed
    (Фаза 3) — this is now the only add-feature path, reusing
    AddedFeatureRequest's own profile/edge validation unchanged."""

    feature: AddedFeatureRequest
    note: str = Field(min_length=1, max_length=1000)
    idempotency_key: str = Field(min_length=8, max_length=200)


@router.post("/{generation_id}/model-graph/features")
async def add_generation_model_graph_feature(
    generation_id: uuid.UUID,
    body: AddNativeFeatureRequest,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> dict:
    """Ф2 нового CAD-редактора: appends a new BuildOperation onto the
    graph's own current state (cad_trace.add_feature_to_graph —
    feature_tree_from_graph + persist_feature_tree_revision +
    _build_spec_solid, all reused unchanged) and always recompiles. Unlike
    .../model-graph/corrections, the graph mutation itself happens inside
    the async task (a brand-new BuildOperation has no existing assertion to
    patch synchronously), so the immediate response carries only the
    queued task id — the frontend already polls rebuild_task_id and
    reloads on success, exactly as it does for a correction rebuild.
    """
    gen, row, graph = await _owned_generation_graph(generation_id, db, user)
    if not any(item.type == "BuildOperation" for item in graph.nodes):
        raise HTTPException(422, "В графе ещё нет ни одной операции построения")

    from app.tasks.cad_trace import add_feature_to_graph

    task = add_feature_to_graph.apply_async(
        args=[
            str(generation_id),
            body.feature.kind,
            body.feature.model_dump(exclude={"kind"}, exclude_none=True),
            body.note,
            body.idempotency_key,
        ],
        queue="celery",
    )
    return {
        "generation_id": str(generation_id),
        "rebuild_task_id": task.id,
    }


@router.get("/{generation_id}/ir/feature-tree-candidates")
async def get_feature_tree_candidates(
    generation_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> dict:
    """Ф10: ranked 3D feature-tree HYPOTHESES derived from the current 2D
    IR — never a single "the" 3D model (a single orthographic view can't
    determine depth). Read-only, like get_ir. Kept for what it still shows
    (what depth guesses a fresh 2D read would produce); the compile-a-
    candidate mutation path it used to feed (Cad3dPanel.tsx) is gone as of
    Фаза 3 — see AddNativeFeatureRequest for the current add-feature flow."""
    from app.ai.cad_ir.feature_tree import generate_feature_tree_candidates

    gen = await db.get(ImageGeneration, generation_id)
    if not _owns(gen, user):
        raise HTTPException(404, "Не найдено")
    _revision, ir = await _load_current_ir(db, gen)
    candidates = generate_feature_tree_candidates(ir)
    return {"candidates": [c.model_dump() for c in candidates]}


@router.post("/{generation_id}/promote-to-drawing")
async def promote_vectorize_to_drawing(
    generation_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> dict:
    """Ф6.2: send an ACCEPTED vectorize result into the technology module —
    creates a Drawing + DrawingFeature rows (holes/threads) from the current
    CAD IR, the same models tp_generator.generate_process_plan_from_drawing
    already consumes for scanned drawings. Requires acceptance first (the
    same approval gate as accept-vectorize) — this is a second, separate
    step, not implied by acceptance, since not every accepted sketch is
    meant to become a manufacturing input."""
    from app.ai.cad_ir.adapters.to_drawing import promote_ir_to_drawing

    gen = await db.get(ImageGeneration, generation_id)
    if not _owns(gen, user):
        raise HTTPException(404, "Не найдено")
    if not gen.accepted:
        raise HTTPException(409, "Сначала примите чертёж (accept-vectorize).")
    revision, ir = await _load_current_ir(db, gen)
    if gen.accepted_revision != revision.revision:
        raise HTTPException(409, "Текущая ревизия не утверждена.")
    drawing = await promote_ir_to_drawing(db, gen, ir, revision.revision)
    await db.commit()
    return {
        "drawing_id": str(drawing.id),
        "features": len([e for e in ir.entities if e.type == "circle"]),
    }


# ── CAD IR (vectorize/editor) ────────────────────────────────────────────────


class IrPatchErrorCode(str, Enum):
    """Typed precondition-failure codes for PATCH /ir ops (Ф5.9) — a caller
    (frontend, or the agent's capability dispatcher) can branch on ``code``
    instead of parsing a Russian sentence. The HTTP status still carries the
    coarse category (400 malformed request, 404 unknown reference, 422
    well-formed but geometrically/semantically invalid)."""

    ENTITY_NOT_FOUND = "entity_not_found"
    MISSING_FIELD = "missing_field"
    INVALID_ENTITY = "invalid_entity"
    NOT_A_SEGMENT = "not_a_segment"
    FILLET_CHAMFER_GEOMETRY_INVALID = "fillet_chamfer_geometry_invalid"
    NO_ENCLOSED_REGION = "no_enclosed_region"
    INVALID_CONSTRAINT = "invalid_constraint"
    SKETCH_OP_INVALID = "sketch_op_invalid"


def _patch_error(status: int, code: IrPatchErrorCode, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code.value, "message": message})


class IrPatchOp(BaseModel):
    op: Literal[
        "confirm", "delete", "update", "add", "set_scale", "set_sheet_format",
        "move", "copy", "mirror", "fillet", "chamfer", "hatch_click",
        "trim", "extend", "offset", "pattern_linear", "pattern_polar",
        "split", "join", "set_construction",
        "set_constraints", "set_parameters", "set_title_block",
        "set_configurations", "apply_configuration",
        "define_block", "insert_block", "delete_block",
        "resolve_region",
    ]
    sheet_format: str | None = None  # A4..A0, for set_sheet_format
    title_block: dict[str, Any] | None = None  # form-1 fields, for set_title_block
    entity_id: str | None = None
    entity_id_2: str | None = None  # second segment, for fillet/chamfer
    region_id: str | None = None
    entity: dict[str, Any] | None = None
    scale: float | None = Field(default=None, gt=0)
    dx: float | None = None  # move/copy
    dy: float | None = None
    value: float | None = None  # fillet radius / chamfer distance
    mirror_p1: dict[str, float] | None = None  # mirror line, two points
    mirror_p2: dict[str, float] | None = None
    click_x: float | None = None  # hatch_click; trim/extend/offset reference point
    click_y: float | None = None
    count: int | None = Field(default=None, ge=2, le=500)  # pattern instance count
    constraints: list[dict[str, Any]] | None = None
    parameters: list[dict[str, Any]] | None = None
    configurations: list[dict[str, Any]] | None = None  # set_configurations
    config_name: str | None = None  # apply_configuration
    block_name: str | None = None  # define_block / insert_block / delete_block
    entity_ids: list[str] | None = None  # define_block source selection


class IrPatchRequest(BaseModel):
    ops: list[IrPatchOp] = Field(min_length=1, max_length=500)


async def _load_current_ir(db: AsyncSession, gen: ImageGeneration):
    from app.services import cad_ir_store

    revision = await cad_ir_store.latest_revision(db, gen.id)
    if revision is None:
        raise HTTPException(404, "У этой генерации нет CAD IR")
    return revision, cad_ir_store.load_ir(revision)


@router.get("/{generation_id}/ir")
async def get_ir(
    generation_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> dict:
    gen = await db.get(ImageGeneration, generation_id)
    if not _owns(gen, user):
        raise HTTPException(404, "Не найдено")
    revision, ir = await _load_current_ir(db, gen)
    return {
        "revision": revision.revision,
        "origin": revision.origin,
        "summary": revision.summary,
        "ir": ir.model_dump(),
    }


@router.get("/{generation_id}/ir/engineering-verification")
async def get_ir_engineering_verification(
    generation_id: uuid.UUID,
    profile: Literal["mechanical", "construction", "auto"] = "auto",
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> dict:
    """Return the interpreted drawing graph and its independent exactness gate."""
    from app.ai.cad_engineering_verify import verify_engineering_ir

    gen = await db.get(ImageGeneration, generation_id)
    if not _owns(gen, user):
        raise HTTPException(404, "Не найдено")
    revision, ir = await _load_current_ir(db, gen)
    verification = verify_engineering_ir(ir, profile=profile)
    return {
        "revision": revision.revision,
        "verification": verification.model_dump(),
    }


@router.patch("/{generation_id}/ir")
async def patch_ir(
    generation_id: uuid.UUID,
    body: IrPatchRequest,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> dict:
    """Deterministic IR edit: apply batch ops, re-validate, save a new
    revision and regenerate PNG/SVG/DXF. Zero LLM — the spec-table pattern
    applied to drawings."""
    from pydantic import TypeAdapter, ValidationError

    from app.ai.cad_ir.assurance import sanitize_incoming, set_assurance
    from app.ai.cad_ir.schema import CadParameter, Entity, GeometricConstraint
    from app.ai.cad_validate import validate_ir
    from app.services import cad_ir_store

    gen = await db.get(ImageGeneration, generation_id)
    if not _owns(gen, user):
        raise HTTPException(404, "Не найдено")
    _revision, ir = await _load_current_ir(db, gen)

    entity_adapter = TypeAdapter(Entity)
    by_id = {e.id: i for i, e in enumerate(ir.entities)}

    def _index_of(entity_id: str | None) -> int:
        if not entity_id or entity_id not in by_id:
            raise _patch_error(
                404, IrPatchErrorCode.ENTITY_NOT_FOUND, f"Элемент {entity_id!r} не найден в IR"
            )
        return by_id[entity_id]

    def _require(value: object, field: str) -> None:
        if value is None:
            raise _patch_error(
                400, IrPatchErrorCode.MISSING_FIELD, f"Для {op.op} нужно поле {field!r}"
            )

    # Every op below only ever mutates the LOCAL `ir` object in memory — none
    # of this reaches storage until `save_revision`/`commit` at the very end.
    # An exception anywhere in this loop propagates out of the request
    # handler before that point, so FastAPI returns the error and nothing
    # commits: a batch either lands as ONE new revision or leaves none at
    # all (verified by test_patch_ir_batch_failure_saves_no_partial_revision).
    for op in body.ops:
        if op.op == "resolve_region":
            _require(op.region_id, "region_id")
            region = next(
                (item for item in ir.unresolved_regions if item.id == op.region_id),
                None,
            )
            if region is None:
                raise _patch_error(
                    404,
                    IrPatchErrorCode.ENTITY_NOT_FOUND,
                    f"Нераспознанная область {op.region_id!r} не найдена",
                )
            region.resolved = True
        elif op.op == "confirm":
            idx = _index_of(op.entity_id)
            entity = ir.entities[idx]
            entity.confidence = 1.0
            entity.origin = "human"
            set_assurance(entity, "human_approved", "human")
            for item in ir.review:
                if item.entity_id == entity.id:
                    item.resolved = True
        elif op.op == "delete":
            idx = _index_of(op.entity_id)
            removed = ir.entities.pop(idx)
            ir.review = [r for r in ir.review if r.entity_id != removed.id]
            by_id = {e.id: i for i, e in enumerate(ir.entities)}
        elif op.op == "update":
            idx = _index_of(op.entity_id)
            _require(op.entity, "entity")
            payload = sanitize_incoming(
                {**op.entity, "id": op.entity_id, "origin": "human", "confidence": 1.0},
                actor="human",
            )
            try:
                ir.entities[idx] = entity_adapter.validate_python(payload)
            except ValidationError as exc:
                raise _patch_error(
                    422, IrPatchErrorCode.INVALID_ENTITY, f"Некорректный элемент: {exc.errors()[:3]}"
                ) from exc
            for item in ir.review:
                if item.entity_id == op.entity_id:
                    item.resolved = True
        elif op.op == "add":
            _require(op.entity, "entity")
            payload = sanitize_incoming(
                {**op.entity, "origin": "human", "confidence": 1.0}, actor="human"
            )
            payload.pop("id", None)
            try:
                entity = entity_adapter.validate_python(payload)
            except ValidationError as exc:
                raise _patch_error(
                    422, IrPatchErrorCode.INVALID_ENTITY, f"Некорректный элемент: {exc.errors()[:3]}"
                ) from exc
            ir.entities.append(entity)
            by_id[entity.id] = len(ir.entities) - 1
        elif op.op == "set_scale":
            if not op.scale:
                raise _patch_error(
                    400, IrPatchErrorCode.MISSING_FIELD, "Для set_scale нужен scale (мм/px)"
                )
            ir.scale = op.scale
            ir.scale_source = "manual"
        elif op.op == "set_sheet_format":
            # B6 one-step scale confirmation: the user picks the ГОСТ format;
            # scale is derived from the detected frame's pixel span (or the
            # full sheet when no frame box was stored). A-series aspect ratios
            # are identical, so this is the only reliable metric anchor.
            from app.tasks.cad_trace import _GOST_SHEETS, _frame_dimensions_mm

            fmt = op.sheet_format
            if fmt not in _GOST_SHEETS:
                raise _patch_error(
                    400, IrPatchErrorCode.MISSING_FIELD,
                    f"Неизвестный формат листа: {fmt}. Допустимо: {', '.join(_GOST_SHEETS)}",
                )
            short_mm, long_mm = _GOST_SHEETS[fmt]
            frame_px = ir.sheet.frame_px
            landscape = ir.source.image_width >= ir.source.image_height
            if frame_px:
                expected_w, expected_h = _frame_dimensions_mm(
                    fmt, landscape=landscape
                )
                ir.scale = (
                    expected_w / max(frame_px[2], 1.0)
                    + expected_h / max(frame_px[3], 1.0)
                ) / 2
            else:
                paper_w, paper_h = (
                    (long_mm, short_mm) if landscape else (short_mm, long_mm)
                )
                ir.scale = (
                    paper_w / ir.source.image_width
                    + paper_h / ir.source.image_height
                ) / 2
            ir.scale_source = "sheet_format"
            ir.sheet.format = fmt
            ir.sheet.width_mm, ir.sheet.height_mm = (
                (long_mm, short_mm)
                if ir.source.image_width >= ir.source.image_height
                else (short_mm, long_mm)
            )
        elif op.op == "move":
            from app.ai.cad_ir.transform import translate_entity

            idx = _index_of(op.entity_id)
            _require(op.dx, "dx")
            _require(op.dy, "dy")
            ir.entities[idx] = translate_entity(ir.entities[idx], op.dx, op.dy)
        elif op.op == "copy":
            from app.ai.cad_ir.transform import duplicate_entity

            idx = _index_of(op.entity_id)
            new_entity = duplicate_entity(ir.entities[idx], op.dx or 0.0, op.dy or 0.0)
            ir.entities.append(new_entity)
            by_id[new_entity.id] = len(ir.entities) - 1
        elif op.op == "mirror":
            from app.ai.cad_ir.schema import Point
            from app.ai.cad_ir.transform import mirror_entity

            idx = _index_of(op.entity_id)
            _require(op.mirror_p1, "mirror_p1")
            _require(op.mirror_p2, "mirror_p2")
            p1 = Point(**op.mirror_p1)
            p2 = Point(**op.mirror_p2)
            ir.entities[idx] = mirror_entity(ir.entities[idx], p1, p2)
        elif op.op in ("fillet", "chamfer"):
            from app.ai.cad_ir.schema import Segment
            from app.ai.cad_ir.transform import FilletChamferError, chamfer, fillet

            idx1 = _index_of(op.entity_id)
            idx2 = _index_of(op.entity_id_2)
            seg1, seg2 = ir.entities[idx1], ir.entities[idx2]
            if not isinstance(seg1, Segment) or not isinstance(seg2, Segment):
                raise _patch_error(
                    400, IrPatchErrorCode.NOT_A_SEGMENT, f"{op.op} работает только с двумя отрезками"
                )
            if not op.value or op.value <= 0:
                param = "радиус" if op.op == "fillet" else "дистанция"
                raise _patch_error(
                    400, IrPatchErrorCode.MISSING_FIELD, f"Для {op.op} нужен положительный {param} (value)"
                )
            try:
                new1, new2, extra = (fillet if op.op == "fillet" else chamfer)(seg1, seg2, op.value)
            except FilletChamferError as exc:
                raise _patch_error(422, IrPatchErrorCode.FILLET_CHAMFER_GEOMETRY_INVALID, str(exc)) from exc
            ir.entities[idx1] = new1
            ir.entities[idx2] = new2
            ir.entities.append(extra)
            by_id[extra.id] = len(ir.entities) - 1
        elif op.op == "hatch_click":
            from app.ai.cad_ir.hatch_click import hatch_region_at_point

            _require(op.click_x, "click_x")
            _require(op.click_y, "click_y")
            region = hatch_region_at_point(ir, op.click_x, op.click_y)
            if region is None:
                raise _patch_error(
                    422, IrPatchErrorCode.NO_ENCLOSED_REGION, "В точке клика нет замкнутой области"
                )
            ir.entities.append(region)
            by_id[region.id] = len(ir.entities) - 1
        elif op.op in ("trim", "extend"):
            from app.ai.cad_ir.schema import Point, Segment
            from app.ai.cad_ir.transform import (
                SketchOpError,
                extend_segment,
                trim_segment,
            )

            idx1 = _index_of(op.entity_id)
            idx2 = _index_of(op.entity_id_2)
            _require(op.click_x, "click_x")
            _require(op.click_y, "click_y")
            target, other = ir.entities[idx1], ir.entities[idx2]
            if not isinstance(target, Segment) or not isinstance(other, Segment):
                raise _patch_error(
                    400, IrPatchErrorCode.NOT_A_SEGMENT, f"{op.op} работает только с отрезками"
                )
            ref = Point(x=op.click_x, y=op.click_y)
            try:
                fn = trim_segment if op.op == "trim" else extend_segment
                ir.entities[idx1] = fn(target, other, ref)
            except SketchOpError as exc:
                raise _patch_error(422, IrPatchErrorCode.SKETCH_OP_INVALID, str(exc)) from exc
        elif op.op == "offset":
            from app.ai.cad_ir.schema import Point
            from app.ai.cad_ir.transform import SketchOpError, offset_entity

            idx = _index_of(op.entity_id)
            _require(op.value, "value")
            _require(op.click_x, "click_x")
            _require(op.click_y, "click_y")
            try:
                new_entity = offset_entity(
                    ir.entities[idx], op.value, Point(x=op.click_x, y=op.click_y)
                )
            except SketchOpError as exc:
                raise _patch_error(422, IrPatchErrorCode.SKETCH_OP_INVALID, str(exc)) from exc
            ir.entities.append(new_entity)
            by_id[new_entity.id] = len(ir.entities) - 1
        elif op.op in ("pattern_linear", "pattern_polar"):
            from app.ai.cad_ir.schema import Point
            from app.ai.cad_ir.transform import (
                SketchOpError,
                pattern_linear,
                pattern_polar,
            )

            idx = _index_of(op.entity_id)
            _require(op.count, "count")
            try:
                if op.op == "pattern_linear":
                    _require(op.dx, "dx")
                    _require(op.dy, "dy")
                    copies = pattern_linear(ir.entities[idx], op.count, op.dx, op.dy)
                else:
                    _require(op.click_x, "click_x")
                    _require(op.click_y, "click_y")
                    _require(op.value, "value")
                    copies = pattern_polar(
                        ir.entities[idx], op.count, Point(x=op.click_x, y=op.click_y), op.value
                    )
            except SketchOpError as exc:
                raise _patch_error(422, IrPatchErrorCode.SKETCH_OP_INVALID, str(exc)) from exc
            for copy_entity in copies:
                ir.entities.append(copy_entity)
                by_id[copy_entity.id] = len(ir.entities) - 1
        elif op.op == "split":
            from app.ai.cad_ir.schema import Point, Segment
            from app.ai.cad_ir.transform import SketchOpError, split_segment

            idx = _index_of(op.entity_id)
            _require(op.click_x, "click_x")
            _require(op.click_y, "click_y")
            target = ir.entities[idx]
            if not isinstance(target, Segment):
                raise _patch_error(
                    400, IrPatchErrorCode.NOT_A_SEGMENT, "split работает только с отрезком"
                )
            try:
                part_a, part_b = split_segment(target, Point(x=op.click_x, y=op.click_y))
            except SketchOpError as exc:
                raise _patch_error(422, IrPatchErrorCode.SKETCH_OP_INVALID, str(exc)) from exc
            ir.entities[idx] = part_a
            ir.entities.append(part_b)
            by_id = {e.id: i for i, e in enumerate(ir.entities)}
        elif op.op == "join":
            from app.ai.cad_ir.schema import Segment
            from app.ai.cad_ir.transform import SketchOpError, join_segments

            idx1 = _index_of(op.entity_id)
            idx2 = _index_of(op.entity_id_2)
            seg1, seg2 = ir.entities[idx1], ir.entities[idx2]
            if not isinstance(seg1, Segment) or not isinstance(seg2, Segment):
                raise _patch_error(
                    400, IrPatchErrorCode.NOT_A_SEGMENT, "join работает только с двумя отрезками"
                )
            try:
                joined = join_segments(seg1, seg2)
            except SketchOpError as exc:
                raise _patch_error(422, IrPatchErrorCode.SKETCH_OP_INVALID, str(exc)) from exc
            # replace the first, drop the second
            ir.entities[idx1] = joined
            ir.entities.pop(idx2)
            ir.review = [r for r in ir.review if r.entity_id != seg2.id]
            by_id = {e.id: i for i, e in enumerate(ir.entities)}
        elif op.op == "set_construction":
            idx = _index_of(op.entity_id)
            entity = ir.entities[idx]
            entity.construction = not entity.construction
            entity.origin = "human"
        elif op.op == "set_constraints":
            _require(op.constraints, "constraints")
            try:
                ir.constraints = TypeAdapter(list[GeometricConstraint]).validate_python(op.constraints)
            except ValidationError as exc:
                raise _patch_error(422, IrPatchErrorCode.INVALID_CONSTRAINT, f"Некорректные ограничения: {exc.errors()[:3]}") from exc
        elif op.op == "set_parameters":
            _require(op.parameters, "parameters")
            try:
                parameters = TypeAdapter(list[CadParameter]).validate_python(op.parameters)
            except ValidationError as exc:
                raise _patch_error(422, IrPatchErrorCode.INVALID_CONSTRAINT, f"Некорректные параметры: {exc.errors()[:3]}") from exc
            if len({parameter.name for parameter in parameters}) != len(parameters):
                raise _patch_error(422, IrPatchErrorCode.INVALID_CONSTRAINT, "Имена параметров должны быть уникальны")
            # A1: resolve expression-driven parameters (width = 2*height…) in
            # dependency order so the stored value is the computed number.
            from app.ai.cad_ir.param_expr import ParamExprError, apply_parameter_expressions

            try:
                parameters = apply_parameter_expressions(parameters)
            except ParamExprError as exc:
                raise _patch_error(422, IrPatchErrorCode.INVALID_CONSTRAINT, str(exc)) from exc
            ir.parameters = parameters
        elif op.op == "set_configurations":
            from app.ai.cad_ir.schema import SketchConfiguration

            _require(op.configurations, "configurations")
            try:
                configs = TypeAdapter(list[SketchConfiguration]).validate_python(op.configurations)
            except ValidationError as exc:
                raise _patch_error(422, IrPatchErrorCode.INVALID_CONSTRAINT, f"Некорректные конфигурации: {exc.errors()[:3]}") from exc
            if len({c.name for c in configs}) != len(configs):
                raise _patch_error(422, IrPatchErrorCode.INVALID_CONSTRAINT, "Имена конфигураций должны быть уникальны")
            ir.configurations = configs
        elif op.op == "apply_configuration":
            from app.ai.cad_ir.param_expr import ParamExprError, apply_parameter_expressions

            _require(op.config_name, "config_name")
            config = next((c for c in ir.configurations if c.name == op.config_name), None)
            if config is None:
                raise _patch_error(404, IrPatchErrorCode.ENTITY_NOT_FOUND, f"Конфигурация {op.config_name!r} не найдена")
            # write the config's values onto matching parameters, then re-resolve
            # any expression-driven parameters that depend on them.
            for parameter in ir.parameters:
                if parameter.name in config.values and not parameter.expression:
                    parameter.value = config.values[parameter.name]
            try:
                ir.parameters = apply_parameter_expressions(ir.parameters)
            except ParamExprError as exc:
                raise _patch_error(422, IrPatchErrorCode.INVALID_CONSTRAINT, str(exc)) from exc
        elif op.op == "define_block":
            from app.ai.cad_ir.blocks import define_block
            from app.ai.cad_ir.transform import SketchOpError

            _require(op.block_name, "block_name")
            _require(op.entity_ids, "entity_ids")
            try:
                define_block(ir, op.block_name, op.entity_ids)
            except SketchOpError as exc:
                raise _patch_error(422, IrPatchErrorCode.SKETCH_OP_INVALID, str(exc)) from exc
        elif op.op == "insert_block":
            from app.ai.cad_ir.blocks import insert_block
            from app.ai.cad_ir.transform import SketchOpError

            _require(op.block_name, "block_name")
            _require(op.click_x, "click_x")
            _require(op.click_y, "click_y")
            try:
                inserted = insert_block(
                    ir, op.block_name, op.click_x, op.click_y, op.value or 0.0
                )
            except SketchOpError as exc:
                raise _patch_error(422, IrPatchErrorCode.SKETCH_OP_INVALID, str(exc)) from exc
            by_id = {e.id: i for i, e in enumerate(ir.entities)}
            del inserted
        elif op.op == "delete_block":
            _require(op.block_name, "block_name")
            before = len(ir.blocks)
            ir.blocks = [b for b in ir.blocks if b.name != op.block_name]
            if len(ir.blocks) == before:
                raise _patch_error(404, IrPatchErrorCode.ENTITY_NOT_FOUND, f"Блок {op.block_name!r} не найден")
        elif op.op == "set_title_block":
            from app.ai.cad_ir.title_block import apply_title_block

            _require(op.title_block, "title_block")
            apply_title_block(ir, op.title_block)
            # entity list changed underneath the by_id cache; rebuild it.
            by_id = {e.id: i for i, e in enumerate(ir.entities)}

    # Recompute honest source→DXF fidelity after every edit. Otherwise a
    # manually repaired drawing would remain blocked by revision-0 scores,
    # while a blind "resolve" click could clear a region without adding the
    # missing CAD geometry.
    normalized_source = (gen.params or {}).get("normalized_source_path")
    if gen.operation == "vectorize" and normalized_source:
        try:
            from app.tasks.cad_trace import _assess_export_fidelity, _binarize

            source_bytes = download_file(normalized_source)
            ink, _width, _height = _binarize(source_bytes)
            _assess_export_fidelity(
                ir,
                ink,
                cad_ir_store._load_keep_raster(gen),
                int((gen.params or {}).get("render_thin_px") or 1),
                int((gen.params or {}).get("render_thick_px") or 2),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "cad_ir_fidelity_refresh_failed",
                generation_id=str(gen.id),
                error=str(exc)[:160],
            )
            ir.digitization_status = "review_required"
    validate_ir(ir)
    origin = "review" if all(
        o.op in ("confirm", "delete", "set_scale", "set_sheet_format", "resolve_region")
        for o in body.ops
    ) else "editor"
    _invalidate_vector_approval(gen)
    row = await cad_ir_store.save_revision(db, gen, ir, origin=origin, created_by=user.sub)
    await db.commit()
    return {"revision": row.revision, "origin": row.origin, "summary": row.summary, "ir": ir.model_dump()}


class IrRevertRequest(BaseModel):
    revision: int = Field(ge=0)


class IrSolveRequest(BaseModel):
    max_nfev: int = Field(default=200, ge=1, le=2000)


@router.post("/{generation_id}/ir/solve")
async def solve_ir_constraints(
    generation_id: uuid.UUID,
    body: IrSolveRequest,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> dict:
    """Explicitly rebuild a constrained sketch and persist one new CAD revision."""
    from app.ai.cad_ir.constraints import solve_constraints
    from app.ai.cad_validate import validate_ir
    from app.services import cad_ir_store

    gen = await db.get(ImageGeneration, generation_id)
    if not _owns(gen, user):
        raise HTTPException(404, "Не найдено")
    _revision, ir = await _load_current_ir(db, gen)
    result = solve_constraints(ir, max_nfev=body.max_nfev)
    if not result.converged:
        raise HTTPException(422, {"message": "Ограничения не удалось согласовать", "solver": result.__dict__})
    validate_ir(ir)
    _invalidate_vector_approval(gen)
    row = await cad_ir_store.save_revision(db, gen, ir, origin="solver", created_by=user.sub)
    await db.commit()
    return {"revision": row.revision, "summary": row.summary, "solver": result.__dict__, "ir": ir.model_dump()}


@router.get("/{generation_id}/ir/constraints/evaluate")
async def evaluate_ir_constraints(
    generation_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> dict:
    """A1: per-constraint satisfaction status of the current sketch WITHOUT
    solving — the constraints panel shows a green/red badge and the offending
    geometry per row, so a conflict is visible before the user hits Rebuild."""
    from app.ai.cad_ir.constraints import analyze_constraints, evaluate_constraints

    gen = await db.get(ImageGeneration, generation_id)
    if not _owns(gen, user):
        raise HTTPException(404, "Не найдено")
    _revision, ir = await _load_current_ir(db, gen)
    checks = evaluate_constraints(ir)
    dof = analyze_constraints(ir)
    return {
        "checks": [
            {
                "constraint_id": c.constraint_id,
                "ok": c.ok,
                "message": c.message,
                "entity_ids": list(c.entity_ids),
            }
            for c in checks
        ],
        "violated": sum(1 for c in checks if not c.ok),
        "dof": {
            "dof": dof.dof,
            "unknowns": dof.unknowns,
            "equations": dof.equations,
            "rank": dof.rank,
            "state": dof.state,
            "redundant": dof.redundant,
            "conflict": dof.conflict,
        },
    }


@router.get("/{generation_id}/ir/dfm-check")
async def dfm_check_ir(
    generation_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> dict:
    """F4: deterministic manufacturability findings over the current drawing —
    standard drill series, tool radii, wall/bridge thickness, thread series.
    Advisory for the technologist; requires a confirmed metric scale."""
    from app.ai.cad_dfm import check_dfm

    gen = await db.get(ImageGeneration, generation_id)
    if not _owns(gen, user):
        raise HTTPException(404, "Не найдено")
    _revision, ir = await _load_current_ir(db, gen)
    try:
        findings = check_dfm(ir)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {
        "findings": [
            {
                "code": f.code,
                "severity": f.severity,
                "message": f.message,
                "recommendation": f.recommendation,
                "entity_ids": f.entity_ids,
                "evidence": f.evidence,
            }
            for f in findings
        ],
        "errors": sum(1 for f in findings if f.severity == "error"),
        "warnings": sum(1 for f in findings if f.severity == "warn"),
    }


@router.post("/{generation_id}/ir/revert")
async def revert_ir(
    generation_id: uuid.UUID,
    body: IrRevertRequest,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> dict:
    """Undo/redo (Ф5.2): re-save an earlier revision's IR as the new current
    one — same deterministic rebuild as PATCH, zero LLM. History stays
    append-only (nothing is deleted, matching the project's audit
    philosophy); this just makes an old state current again, like a git
    revert. The frontend tracks which revision numbers to jump between for
    undo/redo — this endpoint only knows how to jump to one."""
    from app.ai.cad_validate import validate_ir
    from app.db.models import CadIrRevision
    from app.services import cad_ir_store

    gen = await db.get(ImageGeneration, generation_id)
    if not _owns(gen, user):
        raise HTTPException(404, "Не найдено")
    row = (
        await db.execute(
            select(CadIrRevision).where(
                CadIrRevision.generation_id == generation_id,
                CadIrRevision.revision == body.revision,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, f"Ревизия {body.revision} не найдена")
    ir = cad_ir_store.load_ir(row)
    validate_ir(ir)
    _invalidate_vector_approval(gen)
    new_row = await cad_ir_store.save_revision(db, gen, ir, origin="revert", created_by=user.sub)
    await db.commit()
    return {
        "revision": new_row.revision,
        "origin": new_row.origin,
        "summary": new_row.summary,
        "ir": ir.model_dump(),
    }


class BlankSheetRequest(BaseModel):
    # ГОСТ 2.301 format name; the sheet is created at ~4 px/mm working resolution.
    format: Literal["A4", "A3", "A2", "A1"] = "A4"
    landscape: bool = False
    title: str | None = None
    case_id: uuid.UUID | None = None
    # Off by default (matches techdraw.py's TitleBlock.show_frame=False —
    # most manual sketches don't want a border eating into a small A4/A3
    # canvas); explicit opt-in draws the ГОСТ 2.301 frame + 2.104 form-1
    # corner stamp as real, editable IR entities.
    with_frame: bool = False
    designation: str | None = None
    company: str | None = None


_BLANK_PX_PER_MM = 4.0
_BLANK_SIZES_MM = {"A4": (210, 297), "A3": (297, 420), "A2": (420, 594), "A1": (594, 841)}


class GenerationMetaRequest(BaseModel):
    # I5: document lifecycle — rename a CAD document and edit lightweight
    # metadata without touching the drawing itself. The display title is stored
    # in `prompt` (what docTitle() reads); project/object are free-text tags.
    title: str | None = None
    project: str | None = None
    object: str | None = None


@router.patch("/{generation_id}/meta")
async def update_generation_meta(
    generation_id: uuid.UUID,
    body: GenerationMetaRequest,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> dict:
    gen = await db.get(ImageGeneration, generation_id)
    if not _owns(gen, user):
        raise HTTPException(404, "Не найдено")
    if body.title is not None:
        gen.prompt = body.title.strip()[:200]
    params = dict(gen.params or {})
    if body.project is not None:
        params["project"] = body.project.strip()[:120] or None
    if body.object is not None:
        params["object"] = body.object.strip()[:120] or None
    gen.params = params
    await db.commit()
    await db.refresh(gen)
    return _gen_out(gen)


@router.post("/blank-sheet")
async def create_blank_sheet(
    body: BlankSheetRequest,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> dict:
    """Manual drafting entry point: an empty CAD IR sheet (known format and
    scale) the user draws on in the editor. No pipeline, no queue — the
    generation is born done at revision 0 and every stroke arrives via
    PATCH /ir."""
    from app.ai.cad_ir import CadIR, SourceInfo
    from app.ai.cad_ir.schema import SheetInfo
    from app.ai.cad_validate import validate_ir
    from app.services import cad_ir_store

    if not _can_use_studio(user):
        raise HTTPException(403, "Недостаточно прав для графической студии")

    short_mm, long_mm = _BLANK_SIZES_MM[body.format]
    w_mm, h_mm = (long_mm, short_mm) if body.landscape else (short_mm, long_mm)
    entities = []
    title_block: dict = {}
    if body.with_frame:
        from app.ai.cad_ir.blank_sheet import TB_H_MM, TB_W_MM, frame_and_title_block_entities

        entities = frame_and_title_block_entities(
            w_mm, h_mm, _BLANK_PX_PER_MM,
            name=body.title or "",
            designation=body.designation or "",
            company=body.company or "",
        )
        title_block = {
            "detected": True,
            "region": {
                "x0": (w_mm - 25.0 - TB_W_MM) * _BLANK_PX_PER_MM,
                "y0": (h_mm - 10.0 - TB_H_MM) * _BLANK_PX_PER_MM,
                "x1": (w_mm - 25.0) * _BLANK_PX_PER_MM,
                "y1": (h_mm - 10.0) * _BLANK_PX_PER_MM,
            },
        }
    ir = CadIR(
        source=SourceInfo(
            image_width=int(w_mm * _BLANK_PX_PER_MM),
            image_height=int(h_mm * _BLANK_PX_PER_MM),
            kind="blank",
        ),
        scale=1.0 / _BLANK_PX_PER_MM,
        scale_source="sheet_format",
        sheet=SheetInfo(
            format=body.format, width_mm=w_mm, height_mm=h_mm,
            frame=body.with_frame, title_block=title_block,
        ),
        entities=entities,
        recognizer_used="manual",
    )
    validate_ir(ir)

    gen = ImageGeneration(
        owner_sub=user.sub,
        operation="vectorize",
        status=ImageGenStatus.done,
        prompt=body.title,
        params={"blank": True, "sheet_format": body.format},
        source_image_paths=[],
        case_id=body.case_id,
    )
    db.add(gen)
    await db.flush()
    await cad_ir_store.save_revision(db, gen, ir, origin="editor", created_by=user.sub)
    await db.commit()
    await db.refresh(gen)
    return _gen_out(gen)


@router.post("/import-dxf")
async def import_dxf(
    file: UploadFile = File(...),
    title: str | None = Form(None),
    case_id: str | None = Form(None),
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> dict:
    """CAD-file entry point of the /cad section: an uploaded DXF becomes a
    CAD IR document at revision 0 — same lifecycle as a digitized scan or a
    blank sheet, no pipeline/queue involved."""
    from app.ai.cad_ir.adapters.from_dxf import DxfImportError, dxf_to_ir
    from app.ai.cad_validate import validate_ir
    from app.services import cad_ir_store

    if not _can_use_studio(user):
        raise HTTPException(403, "Недостаточно прав для графической студии")
    data = await file.read()
    if not data:
        raise HTTPException(400, "Пустой файл")
    if len(data) > 50 * 1024 * 1024:
        raise HTTPException(413, "Файл больше 50 МБ")
    try:
        ir = dxf_to_ir(data)
    except DxfImportError as exc:
        raise HTTPException(422, str(exc)) from exc
    validate_ir(ir)

    gen = ImageGeneration(
        owner_sub=user.sub,
        operation="vectorize",
        status=ImageGenStatus.done,
        prompt=title or (file.filename or "").rsplit(".", 1)[0] or None,
        params={"imported": True, "source_filename": file.filename},
        source_image_paths=[],
        case_id=uuid.UUID(case_id) if case_id else None,
    )
    db.add(gen)
    await db.flush()
    await cad_ir_store.save_revision(db, gen, ir, origin="import", created_by=user.sub)
    await db.commit()
    await db.refresh(gen)
    return _gen_out(gen)


@router.post("/{generation_id}/iterate")
async def iterate_generation(
    generation_id: uuid.UUID,
    body: GenerateRequest,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> dict:
    parent = await db.get(ImageGeneration, generation_id)
    if not _owns(parent, user):
        raise HTTPException(404, "Не найдено")
    if not parent.result_path:
        raise HTTPException(400, "У исходной генерации нет результата для итерации.")
    if (body.operation or "edit") in ("edit", "inpaint") and not _prompt_text(body):
        raise HTTPException(400, "Для итерации нужно текстовое указание (prompt).")
    workflow_id = await _workflow_for_iteration(db, parent, body, user)

    gen = ImageGeneration(
        # Inherit the parent's owner, not the caller's — when the agent
        # iterates on a user's behalf (see _is_agent_service), the new
        # version must stay visible in that user's own /studio list, not get
        # orphaned under the internal service identity.
        owner_sub=parent.owner_sub,
        operation=body.operation or "edit",
        workflow_id=workflow_id,
        status=ImageGenStatus.queued,
        prompt=body.prompt,
        negative_prompt=body.negative_prompt,
        params=body.params or {},
        source_image_paths=[parent.result_path],
        parent_id=parent.id,
    )
    db.add(gen)
    await db.flush()
    job = await studio_queue.create_image_job(db, gen, title=body.prompt or "Итерация")
    await db.commit()
    await db.refresh(gen)
    await db.refresh(job)
    task_id = _enqueue(str(gen.id))
    if task_id:
        job.celery_task_id = task_id
        gen.celery_task_id = task_id
        await db.commit()
    out = _gen_out(gen)
    out["job_id"] = str(job.id)
    return out


async def _delete_one(db: AsyncSession, gen: ImageGeneration) -> None:
    """Delete a generation + its MinIO files, re-parenting any iteration
    children to roots so the FK never blocks the delete (a failed/erroneous
    gen must always be removable)."""
    from sqlalchemy import delete as sa_delete
    from sqlalchemy import update as sa_update
    from app.db.models import CadIrRevision

    source_paths = [
        path
        for path in (gen.source_image_paths or [])
        if isinstance(path, str) and path.startswith(f"{_SOURCE_PREFIX}/")
    ]
    revisions = (
        await db.execute(select(CadIrRevision).where(CadIrRevision.generation_id == gen.id))
    ).scalars().all()
    params = gen.params or {}
    derived_paths = [
        params.get(key)
        for key in (
            "normalized_source_path", "keep_raster_path", "svg_path", "dxf_path", "dwg_path",
            "pdf_path", "step_path", "fcstd_path", "stl_path", "cad_report_path",
        )
    ]
    revision_paths = [revision.ir_path for revision in revisions]
    paths = {
        path
        for path in [
            gen.result_path, gen.thumbnail_path, gen.mask_path,
            *source_paths, *derived_paths, *revision_paths,
        ]
        if path
    }
    for path in paths:
        if path:
            try:
                from app.storage import delete_file

                delete_file(path)
            except Exception:  # noqa: BLE001 — leftover file is cosmetic
                pass
    await db.execute(
        sa_update(ImageGeneration)
        .where(ImageGeneration.parent_id == gen.id)
        .values(parent_id=None)
    )
    await db.execute(sa_delete(StudioJob).where(StudioJob.generation_id == gen.id))
    await db.execute(sa_delete(CadIrRevision).where(CadIrRevision.generation_id == gen.id))
    from app.tasks.image_generation import _clear_progress

    _clear_progress(str(gen.id))
    await db.delete(gen)


@router.delete("/{generation_id}")
async def delete_generation(
    generation_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> dict:
    gen = await db.get(ImageGeneration, generation_id)
    if not _owns(gen, user):
        raise HTTPException(404, "Не найдено")
    await _delete_one(db, gen)
    await db.commit()
    return {"ok": True}


class BulkDeleteBody(BaseModel):
    ids: list[uuid.UUID] = Field(default_factory=list, max_length=500)


@router.post("/bulk-delete")
async def bulk_delete_generations(
    body: BulkDeleteBody,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> dict:
    """Delete several generations at once (only the caller's own)."""
    deleted = 0
    for gid in body.ids:
        gen = await db.get(ImageGeneration, gid)
        if _owns(gen, user):
            await _delete_one(db, gen)
            deleted += 1
    await db.commit()
    return {"ok": True, "deleted": deleted}


@router.post("/clear-failed")
async def clear_failed_generations(
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> dict:
    """One-click cleanup of all the caller's failed generations."""
    q = select(ImageGeneration).where(ImageGeneration.status == ImageGenStatus.failed)
    if not _is_agent_service(user):
        q = q.where(ImageGeneration.owner_sub == user.sub)
    rows = (await db.execute(q)).scalars().all()
    for gen in rows:
        await _delete_one(db, gen)
    await db.commit()
    return {"ok": True, "deleted": len(rows)}


# ── Workflow library ─────────────────────────────────────────────────────────


@router.get("/workflows/list")
async def list_workflows(
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> dict:
    rows = (
        await db.execute(
            select(ComfyWorkflow)
            .where(
                (ComfyWorkflow.is_builtin.is_(True))
                | (ComfyWorkflow.owner_sub == user.sub)
                | (ComfyWorkflow.owner_sub.is_(None))
            )
            .order_by(ComfyWorkflow.category, ComfyWorkflow.title)
        )
    ).scalars().all()
    return {"items": [_wf_out(w) for w in rows]}


@router.post("/workflows")
async def create_workflow(
    body: WorkflowIn,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> dict:
    if not _can_manage_workflows(user):
        raise HTTPException(403, "Недостаточно прав для создания воркфлоу")
    wf = ComfyWorkflow(
        key=body.key,
        title=body.title,
        description=body.description,
        category=body.category,
        operation=body.operation,
        graph=body.graph,
        inject_map=body.inject_map,
        params_schema=body.params_schema,
        is_builtin=False,
        enabled=True,
        owner_sub=user.sub,
    )
    db.add(wf)
    await db.commit()
    await db.refresh(wf)
    return _wf_out(wf)


@router.post("/workflows/{workflow_id}/duplicate")
async def duplicate_workflow(
    workflow_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> dict:
    src = await db.get(ComfyWorkflow, workflow_id)
    if not _can_read_workflow(src, user):
        raise HTTPException(404, "Не найдено")
    if not _can_manage_workflows(user) and not src.is_builtin and src.owner_sub != user.sub:
        raise HTTPException(403, "Недостаточно прав для копирования воркфлоу")
    wf = ComfyWorkflow(
        key=f"{src.key}_copy_{uuid.uuid4().hex[:6]}",
        title=f"{src.title} (копия)",
        description=src.description,
        category=src.category,
        operation=src.operation,
        graph=src.graph,
        inject_map=src.inject_map,
        params_schema=src.params_schema,
        is_builtin=False,
        enabled=True,
        owner_sub=user.sub,
    )
    db.add(wf)
    await db.commit()
    await db.refresh(wf)
    return _wf_out(wf)


@router.patch("/workflows/{workflow_id}")
async def patch_workflow(
    workflow_id: uuid.UUID,
    body: WorkflowPatch,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> dict:
    wf = await db.get(ComfyWorkflow, workflow_id)
    if not _can_mutate_workflow(wf, user):
        raise HTTPException(404, "Не найдено")
    if wf.is_builtin:
        raise HTTPException(400, "Встроенный воркфлоу нельзя править — сделайте копию.")
    if not _can_manage_workflows(user) and wf.owner_sub != user.sub:
        raise HTTPException(403, "Недостаточно прав для изменения воркфлоу")
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(wf, field, value)
    await db.commit()
    await db.refresh(wf)
    return _wf_out(wf)


@router.delete("/workflows/{workflow_id}")
async def delete_workflow(
    workflow_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> dict:
    wf = await db.get(ComfyWorkflow, workflow_id)
    if not _can_mutate_workflow(wf, user):
        raise HTTPException(404, "Не найдено")
    if wf.is_builtin:
        raise HTTPException(400, "Встроенный воркфлоу нельзя удалить (только выключить).")
    if not _can_manage_workflows(user) and wf.owner_sub != user.sub:
        raise HTTPException(403, "Недостаточно прав для удаления воркфлоу")
    await db.delete(wf)
    await db.commit()
    return {"ok": True}


def _strip_placeholder_image_inputs(graph: dict) -> dict:
    """Our stored templates carry a placeholder filename ("input.png") on
    LoadImage nodes purely so the graph has a valid shape — it's never
    actually read at generation time (build_workflow() always overwrites it
    with the real upload, see comfyui_client.py). Forcing that placeholder
    onto the widget when the graph is pushed for viewing/editing in ComfyUI's
    own UI makes it show a broken "file not found" thumbnail on any server
    that doesn't happen to have a file with that exact name — dropping the
    input lets ComfyUI fall back to its own combo-widget default (the first
    file it actually has), a real, loadable preview instead."""
    import copy

    cloned = copy.deepcopy(graph)
    for node in cloned.values():
        if isinstance(node, dict) and node.get("class_type") == "LoadImage":
            node.get("inputs", {}).pop("image", None)
    return cloned


@router.post("/workflows/{workflow_id}/push-to-comfyui")
async def push_workflow_to_comfyui(
    workflow_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> dict:
    """Save this workflow's graph into ComfyUI's own userdata/workflows folder
    so it shows up in the embedded ComfyUI UI's Workflow browser (the studio's
    graph is stored in "API/prompt" format, not ComfyUI's visual-editor
    format with node positions — ComfyUI can still open it via its own
    "Load" dialog, it just auto-arranges nodes since no layout is saved)."""
    import re

    wf = await db.get(ComfyWorkflow, workflow_id)
    if not _can_read_workflow(wf, user):
        raise HTTPException(404, "Не найдено")
    if not _can_manage_workflows(user):
        raise HTTPException(403, "Недостаточно прав для публикации воркфлоу в ComfyUI")

    slug = re.sub(r"[^a-zA-Z0-9_\-]+", "_", wf.key).strip("_") or str(workflow_id)
    filename = f"workflows/{slug}.json"
    graph = _strip_placeholder_image_inputs(wf.graph)

    from app.ai.comfyui_client import ComfyUIClient

    client = ComfyUIClient.from_registry()
    async with httpx.AsyncClient(timeout=15.0) as http:
        try:
            resp = await http.post(
                f"{client.base_url}/userdata/{quote(filename, safe='')}",
                params={"overwrite": "true"},
                json=graph,
            )
        except httpx.RequestError as exc:
            raise HTTPException(502, f"ComfyUI сервер сейчас недоступен: {exc}") from None
    if resp.status_code >= 400:
        raise HTTPException(502, f"ComfyUI отклонил сохранение: {resp.status_code} {resp.text[:200]}")
    return {"ok": True, "filename": filename}


# ── Prompt helper ────────────────────────────────────────────────────────────

_PROMPT_HELP_SYSTEM = (
    "Ты — помощник инженера-технолога. Преврати грубое описание задачи в точный, "
    "лаконичный промпт для генерации/редактирования технического изображения "
    "(чертёж, оснастка, деталь, схема установки на станке). Верни JSON "
    '{"prompt": "...", "negative_prompt": "..."} без пояснений. Промпт — конкретный, '
    "с упоминанием вида (вид сверху/сбоку/изометрия), стиля (технический линейный "
    "чертёж / эскиз / 3D-рендер) и важных деталей. negative_prompt — что исключить "
    "(размытие, лишние объекты, цветной фон и т.п.). Стиль по умолчанию — ЕСКД: "
    "чёрно-белая линейная графика, без рамки листа и без углового штампа/основной "
    "надписи (это добавляется отдельно системой, не проси их в промпте и не "
    "упоминай в negative_prompt как то, что нужно оставить)."
)


@router.post("/prompt-help")
async def prompt_help(
    body: PromptHelpRequest,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> dict:
    """Expand a rough RU description into a precise ComfyUI prompt (local LLM)."""
    if body.operation not in _ALLOWED_OPERATIONS:
        raise HTTPException(400, "Неизвестная операция графической студии")
    grounding = ""
    if body.source_document_id:
        # Best-effort: ground the prompt in what the attached drawing shows.
        doc = await db.get(Document, body.source_document_id)
        if not _can_access_document(doc, user):
            raise HTTPException(404, "Документ не найден")
        if doc and getattr(doc, "summary", None):
            grounding = f"\nКонтекст приложенного изображения: {doc.summary[:600]}"

    user_msg = (
        f"Операция: {body.operation}. Описание задачи: {body.description}{grounding}"
    )
    try:
        from app.ai.router import AIRouter
        from app.ai.schemas import AIRequest, AITask, ChatMessage

        resp = await AIRouter().run(
            AIRequest(
                task=AITask.ENGINEERING_REASONING,
                messages=[
                    ChatMessage(role="system", content=_PROMPT_HELP_SYSTEM),
                    ChatMessage(role="user", content=user_msg),
                ],
                confidential=True,
                allow_cloud=False,
            )
        )
        text = (resp.text or "").strip()
        parsed = _extract_json(text)
        if parsed:
            return {
                "prompt": parsed.get("prompt", "").strip(),
                "negative_prompt": parsed.get("negative_prompt", "").strip(),
            }
        # Fall back to using the raw text as the prompt.
        return {"prompt": text or body.description, "negative_prompt": ""}
    except Exception as exc:  # noqa: BLE001
        logger.warning("prompt_help_failed", error=str(exc))
        return {"prompt": body.description, "negative_prompt": "", "fallback": True}


def _validate_spec_or_raise(spec: dict) -> None:
    """Deterministic engineering validation; 422 with the exact reason on failure."""
    from app.ai import techdraw_validate

    try:
        issues = techdraw_validate.blocking(techdraw_validate.validate_spec(spec))
    except Exception as exc:  # noqa: BLE001 — malformed structure (pydantic, etc.)
        raise HTTPException(422, f"Спецификация некорректна: {exc}")
    if issues:
        fix_note = "; ".join(f"{i.field_path}: {i.message}" for i in issues)
        raise HTTPException(422, f"Спецификация содержит ошибки: {fix_note}")


async def _nl_to_spec(description: str) -> dict:
    """LLM turns a NL part description into a TechDraw spec (local, confidential).

    Validates the result deterministically (see ``techdraw_validate``); on a
    blocking issue, retries ONCE with the exact error appended, then gives up
    with a 422 explaining what's wrong rather than rendering a bad spec.
    """
    from app.ai import techdraw_validate
    from app.ai.router import AIRouter
    from app.ai.schemas import AIRequest, AITask, ChatMessage
    from app.ai.techdraw import SPEC_SYSTEM_PROMPT
    from app.ai.techdraw_context import build_context_block

    system = SPEC_SYSTEM_PROMPT
    context = build_context_block(description)
    if context:
        system = (
            f"{SPEC_SYSTEM_PROMPT}\n\nСправочный контекст "
            f"(используй эти точные значения, не выдумывай другие):\n{context}"
        )

    async def _ask(messages: list) -> dict | None:
        resp = await AIRouter().run(
            AIRequest(task=AITask.ENGINEERING_REASONING, messages=messages,
                      confidential=True, allow_cloud=False)
        )
        return _extract_json((resp.text or "").strip())

    base_messages = [
        ChatMessage(role="system", content=system),
        ChatMessage(role="user", content=description),
    ]
    spec = await _ask(base_messages)
    if not spec:
        raise HTTPException(422, "Не удалось построить спецификацию из описания.")

    try:
        issues = techdraw_validate.blocking(techdraw_validate.validate_spec(spec))
    except Exception as exc:  # noqa: BLE001
        issues = [techdraw_validate.ValidationIssue("MALFORMED", "error", str(exc), "spec")]
    if not issues:
        return spec

    fix_note = "; ".join(f"{i.field_path}: {i.message}" for i in issues)
    spec2 = await _ask([
        *base_messages,
        ChatMessage(role="assistant", content=json.dumps(spec, ensure_ascii=False)),
        ChatMessage(role="user", content=f"В спецификации есть ошибки, исправь и верни ЗАНОВО весь JSON: {fix_note}"),
    ])
    if spec2:
        try:
            issues2 = techdraw_validate.blocking(techdraw_validate.validate_spec(spec2))
        except Exception:  # noqa: BLE001
            issues2 = issues
        if not issues2:
            return spec2

    raise HTTPException(422, f"Спецификация содержит ошибки после повторной попытки: {fix_note}")


@router.post("/techdraw")
async def techdraw(
    body: TechDrawRequest,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> dict:
    """Generate an EXACT technical drawing (deterministic vector render).

    Unlike ComfyUI generation, dimensions/tolerances/roughness are drawn by code,
    so the result is metrically exact and the text is real. Accepts a free-text
    description (→ LLM → spec) or a ready spec. Renders PNG + DXF synchronously.
    """
    from app.ai.techdraw import render_spec_to_dxf, render_spec_to_png

    if not _can_use_studio(user):
        raise HTTPException(403, "Недостаточно прав для графической студии")
    if body.source_document_id:
        doc = await db.get(Document, body.source_document_id)
        if not _can_access_document(doc, user):
            raise HTTPException(404, "Документ для связи не найден")

    spec = body.spec
    if spec is None:
        if not (body.description or "").strip():
            raise HTTPException(400, "Нужно описание или готовая спецификация.")
        spec = await _nl_to_spec(body.description)
    else:
        # A caller-supplied spec bypasses the LLM (and its repair loop), but
        # not engineering validation — an agent/API client can't sidestep it
        # just by constructing the JSON itself.
        _validate_spec_or_raise(spec)

    try:
        png = render_spec_to_png(spec, scale=2.0, view=body.view)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(422, f"Не удалось построить чертёж: {exc}")

    gen = ImageGeneration(
        owner_sub=user.sub,
        operation="techdraw",
        status=ImageGenStatus.done,
        prompt=body.description,
        params={"spec": spec, "view": body.view},
        source_image_paths=[],
        source_document_id=body.source_document_id,
        case_id=body.case_id,
    )
    db.add(gen)
    await db.flush()

    base = f"{_SOURCE_PREFIX.replace('-src', '')}/{user.sub}/{gen.id}"
    result_path = f"{base}.png"
    upload_file(png, result_path, "image/png")
    gen.result_path = result_path
    gen.thumbnail_path = result_path
    try:
        dxf = render_spec_to_dxf(spec)
        upload_file(dxf, f"{base}.dxf", "application/dxf")
        gen.params = {**gen.params, "dxf_path": f"{base}.dxf"}
    except Exception:  # noqa: BLE001
        pass
    await db.commit()
    await db.refresh(gen)
    return _gen_out(gen)


def _extract_json(text: str) -> dict | None:
    if not text:
        return None
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except Exception:  # noqa: BLE001
        return None
