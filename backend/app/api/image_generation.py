"""Image studio API — generate/edit raster images (drawings) via ComfyUI.

Draft-first: ``POST /generate`` queues a Celery job and returns the record
immediately; the result arrives asynchronously (poll ``GET /{id}`` or wait for
the mobile push). No approval gate — generated images are version drafts the
human keeps (``/accept``) or re-iterates (``/iterate``).

Also exposes the editable workflow library (``/workflows*``) and a prompt helper
(``/prompt-help``) that turns a rough RU description into a precise prompt.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal
from urllib.parse import quote

import httpx
import structlog
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.jwt import get_current_user
from app.auth.models import UserInfo, UserRole
from app.db.models import (
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
_ALLOWED_UPLOAD_TYPES = {"image/png", "image/jpeg", "image/jpg", "image/webp", "application/octet-stream"}
_ALLOWED_UPLOAD_EXTS = {"png", "jpg", "jpeg", "webp"}
_MAX_SOURCE_BYTES = 50 * 1024 * 1024


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
    return {
        "id": str(gen.id),
        "operation": gen.operation,
        "status": status,
        "progress": progress,
        "prompt": gen.prompt,
        "negative_prompt": gen.negative_prompt,
        "params": gen.params or {},
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
        raise HTTPException(400, "Поддержаны только PNG, JPEG и WebP изображения.")
    content = await file.read()
    if not content:
        raise HTTPException(400, "Пустой файл")
    if len(content) > _MAX_SOURCE_BYTES:
        raise HTTPException(413, "Изображение слишком большое для графической студии.")
    ext = "png"
    if file.filename and "." in file.filename:
        ext = file.filename.rsplit(".", 1)[-1].lower()[:5]
    if ext not in _ALLOWED_UPLOAD_EXTS:
        raise HTTPException(400, "Поддержаны только PNG, JPEG и WebP изображения.")
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

    if body.operation in ("edit", "inpaint", "cleanup", "vectorize") and not source_paths:
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
}


@router.get("/{generation_id}/artifact")
async def get_artifact(
    generation_id: uuid.UUID,
    kind: Literal["dxf", "dwg", "svg", "ir", "step", "iges", "fcstd", "stl", "pdf"] = "dxf",
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> Response:
    gen = await db.get(ImageGeneration, generation_id)
    if not _owns(gen, user):
        raise HTTPException(404, "Не найдено")
    params = gen.params or {}
    if kind in ("step", "iges", "fcstd", "stl"):
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
    await db.commit()
    return _gen_out(gen)


async def _build_manifest(db: AsyncSession, gen: ImageGeneration) -> dict:
    from app.ai.cad_validate import validate_ir
    from app.services.cad_release import ReleaseBlocked, build_release_manifest

    revision, ir = await _load_current_ir(db, gen)
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
    from app.ai.cad_validate import run_llm_review_levels
    from app.ai.norm_citation import resolve_norm_citations
    from app.services import cad_ir_store

    gen = await db.get(ImageGeneration, generation_id)
    if not _owns(gen, user):
        raise HTTPException(404, "Не найдено")
    revision, ir = await _load_current_ir(db, gen)
    if not gen.result_path:
        raise HTTPException(409, "Нет рендера для проверки — сначала сохраните ревизию.")
    png_bytes = download_file(gen.result_path)

    llm_issues = await run_llm_review_levels(png_bytes, confidential=True)
    kept = [i for i in ir.validation.issues if i.code not in ("NORMCONTROL_LLM", "VLM_CRITIC")]
    ir.validation.issues = await resolve_norm_citations(kept + llm_issues, db)

    _invalidate_vector_approval(gen)
    row = await cad_ir_store.save_revision(db, gen, ir, origin="llm_review", created_by=user.sub)
    gen.params = {**(gen.params or {}), "full_check_revision": row.revision}
    await db.commit()
    return {"revision": row.revision, "origin": row.origin, "summary": row.summary, "ir": ir.model_dump()}


class FeatureParameterOverride(BaseModel):
    feature_index: int = Field(ge=0, lt=500)
    depth_mm: float | None = Field(default=None, gt=0, le=100_000)
    through: bool | None = None


class AddedFeatureRequest(BaseModel):
    kind: Literal["boss", "pocket", "fillet", "chamfer"]
    profile: Literal["circle", "rectangle"] | None = None
    center_x_mm: float | None = Field(default=None, ge=0, le=100_000)
    center_y_mm: float | None = Field(default=None, ge=0, le=100_000)
    depth_mm: float | None = Field(default=None, gt=0, le=100_000)
    diameter_mm: float | None = Field(default=None, gt=0, le=100_000)
    width_mm: float | None = Field(default=None, gt=0, le=100_000)
    height_mm: float | None = Field(default=None, gt=0, le=100_000)
    edge_key: str | None = Field(default=None, min_length=16, max_length=128)
    size_mm: float | None = Field(default=None, gt=0, le=100_000)

    @model_validator(mode="after")
    def validate_profile_dimensions(self) -> "AddedFeatureRequest":
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


class CompileFeatureTreeRequest(BaseModel):
    confirm_assumptions: bool = False
    feature_overrides: list[FeatureParameterOverride] = Field(default_factory=list, max_length=500)
    added_features: list[AddedFeatureRequest] = Field(default_factory=list, max_length=100)


def _apply_feature_overrides(candidate, overrides: list[FeatureParameterOverride]):
    """Apply only 3D-specific human decisions to a server-derived tree.

    The 2D footprint, hole diameter and hole position remain immutable here:
    changing those belongs in CAD IR, where revisioning and validation can see it.
    """
    if not overrides:
        return candidate
    updated = candidate.model_copy(deep=True)
    seen: set[int] = set()
    missing = list(updated.missing_data)
    for override in overrides:
        index = override.feature_index
        if index in seen:
            raise HTTPException(422, f"Параметры операции {index} переданы дважды")
        seen.add(index)
        if index >= len(updated.features):
            raise HTTPException(422, f"Операция {index} отсутствует в выбранной гипотезе")
        feature = updated.features[index]
        fields = override.model_fields_set
        if feature.kind == "extrude":
            if "through" in fields or "depth_mm" not in fields or override.depth_mm is None:
                raise HTTPException(422, "Для выдавливания разрешено менять только depth_mm")
            feature.params["depth_mm"] = override.depth_mm
            missing = [item for item in missing if "бокового вида" not in item and "глубина выдавливания" not in item]
        elif feature.kind == "hole":
            if "through" not in fields:
                raise HTTPException(422, "Для отверстия нужно явно выбрать сквозное или глухое")
            feature.params["through"] = override.through
            diameter = float(feature.params.get("diameter_mm") or 0)
            marker = f"глубина отверстия {diameter:g}мм"
            if override.through is True:
                if "depth_mm" in fields and override.depth_mm is not None:
                    raise HTTPException(422, "У сквозного отверстия нельзя задавать depth_mm")
                feature.params.pop("depth_mm", None)
                missing = [item for item in missing if marker not in item]
            elif override.through is False:
                if "depth_mm" not in fields or override.depth_mm is None:
                    raise HTTPException(422, "Для глухого отверстия нужна положительная depth_mm")
                feature.params["depth_mm"] = override.depth_mm
                missing = [item for item in missing if marker not in item]
            else:
                feature.params.pop("depth_mm", None)
        else:
            raise HTTPException(422, f"Редактирование операции {feature.kind} пока не поддерживается")
    updated.missing_data = missing
    updated.label = f"{updated.label}; параметры 3D уточнены человеком"
    return updated


def _append_human_features(candidate, additions: list[AddedFeatureRequest]):
    if not additions:
        return candidate
    from app.ai.cad_ir.feature_tree import Feature3D

    updated = candidate.model_copy(deep=True)
    insert_at = next(
        (index for index, feature in enumerate(updated.features) if feature.kind == "hole"),
        len(updated.features),
    )
    edge_seen = False
    for item in additions:
        if item.kind in ("fillet", "chamfer"):
            edge_seen = True
        elif edge_seen:
            raise HTTPException(422, "Операции тела должны предшествовать фаскам и скруглениям")
    human_features = [
        Feature3D(
            kind=item.kind,
            source_entity_ids=[],
            params=item.model_dump(exclude={"kind"}, exclude_none=True),
            confidence=1.0,
        )
        for item in additions
    ]
    updated.features[insert_at:insert_at] = human_features
    updated.label = f"{updated.label}; добавлено операций: {len(human_features)}"
    return updated


@router.get("/{generation_id}/ir/feature-tree-candidates")
async def get_feature_tree_candidates(
    generation_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> dict:
    """Ф10: ranked 3D feature-tree HYPOTHESES derived from the current 2D
    IR — never a single "the" 3D model (a single orthographic view can't
    determine depth). Read-only, like get_ir; the human picks a candidate
    and separately asks for it to be compiled (POST .../step)."""
    from app.ai.cad_ir.feature_tree import generate_feature_tree_candidates

    gen = await db.get(ImageGeneration, generation_id)
    if not _owns(gen, user):
        raise HTTPException(404, "Не найдено")
    _revision, ir = await _load_current_ir(db, gen)
    candidates = generate_feature_tree_candidates(ir)
    return {"candidates": [c.model_dump() for c in candidates]}


@router.post("/{generation_id}/ir/feature-tree-candidates/{index}/step")
async def compile_feature_tree_candidate_to_step(
    generation_id: uuid.UUID,
    index: int,
    body: CompileFeatureTreeRequest | None = None,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> Response:
    """Compile a human-picked hypothesis in the isolated FreeCAD kernel.

    STEP, FCStd and STL are generated from the same B-Rep and persisted against
    the accepted IR revision. Unknown depth/side-view assumptions require an
    explicit flag; merely accepting the 2D drawing is not consent to invent 3D.

    Requires
    acceptance first (same gate philosophy as promote-to-drawing — compiling
    a specific depth guess into a downloadable 3D artifact is a real
    decision, not implied by 2D acceptance)."""
    import hashlib
    import json

    from app.ai.cad_ir.feature_tree import generate_feature_tree_candidates
    from app.services.cad_kernel import (
        CadKernelError,
        CadKernelRejected,
        CadKernelUnavailable,
        compile_candidate,
    )

    gen = await db.get(ImageGeneration, generation_id)
    if not _owns(gen, user):
        raise HTTPException(404, "Не найдено")
    if not gen.accepted:
        raise HTTPException(409, "Сначала примите чертёж (accept-vectorize).")
    revision, ir = await _load_current_ir(db, gen)
    if gen.accepted_revision != revision.revision:
        raise HTTPException(409, "Текущая ревизия не утверждена.")
    candidates = generate_feature_tree_candidates(ir)
    if not (0 <= index < len(candidates)):
        raise HTTPException(404, f"Кандидат {index} не найден (всего {len(candidates)})")
    candidate = _apply_feature_overrides(
        candidates[index],
        body.feature_overrides if body else [],
    )
    candidate = _append_human_features(candidate, body.added_features if body else [])
    try:
        # D4: resolve the sheet material to a density so the kernel can report
        # mass. A material we can't classify simply yields no density (no mass).
        density_kg_m3: float | None = None
        material = (((ir.sheet.title_block or {}).get("fields") or {}).get("material"))
        if isinstance(material, str) and material.strip():
            from app.ai import techdraw_reference as tdref

            spec = tdref.classify_material(material)
            if spec is not None:
                density_kg_m3 = spec.density_kg_m3
        artifacts = await compile_candidate(
            candidate,
            confirm_assumptions=bool(body and body.confirm_assumptions),
            metadata={
                "generation_id": str(gen.id),
                "ir_revision": revision.revision,
                "candidate_index": index,
                "approved_by": gen.accepted_by,
                "density_kg_m3": density_kg_m3,
            },
        )
    except CadKernelRejected as exc:
        raise HTTPException(409, str(exc)) from exc
    except CadKernelUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    except CadKernelError as exc:
        raise HTTPException(502, str(exc)) from exc

    base = f"image-gen/{gen.owner_sub or 'shared'}/{gen.id}_3d_r{revision.revision}"
    paths = {
        "step_path": f"{base}.step",
        "fcstd_path": f"{base}.FCStd",
        "stl_path": f"{base}.stl",
        "cad_report_path": f"{base}_report.json",
    }
    report_bytes = json.dumps(artifacts.report, ensure_ascii=False, sort_keys=True).encode("utf-8")
    uploads = [
        (artifacts.step, paths["step_path"], "model/step"),
        (artifacts.fcstd, paths["fcstd_path"], "application/vnd.freecad"),
        (artifacts.stl, paths["stl_path"], "model/stl"),
        (report_bytes, paths["cad_report_path"], "application/json"),
    ]
    if artifacts.iges:
        paths["iges_path"] = f"{base}.iges"
        uploads.append((artifacts.iges, paths["iges_path"], "model/iges"))
    uploaded: list[str] = []
    try:
        for content, path, content_type in uploads:
            upload_file(content, path, content_type)
            uploaded.append(path)
    except Exception:
        from app.storage import delete_file

        for path in uploaded:
            try:
                delete_file(path)
            except Exception:  # noqa: BLE001
                pass
        raise

    revision.artifact_hashes = {
        **(revision.artifact_hashes or {}),
        "step": hashlib.sha256(artifacts.step).hexdigest(),
        "fcstd": hashlib.sha256(artifacts.fcstd).hexdigest(),
        "stl": hashlib.sha256(artifacts.stl).hexdigest(),
        **({"iges": hashlib.sha256(artifacts.iges).hexdigest()} if artifacts.iges else {}),
    }
    gen.params = {
        **(gen.params or {}),
        **paths,
        "cad_artifact_revision": revision.revision,
        "cad_candidate_index": index,
        "cad_feature_overrides": [item.model_dump(exclude_unset=True) for item in (body.feature_overrides if body else [])],
        "cad_added_features": [item.model_dump(mode="json", exclude_none=True) for item in (body.added_features if body else [])],
        "cad_feature_tree": candidate.model_dump(mode="json"),
        "cad_report": artifacts.report,
    }
    await db.commit()
    return Response(
        content=artifacts.step,
        media_type="model/step",
        headers={"X-CAD-Revision": str(revision.revision)},
    )


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
    ]
    sheet_format: str | None = None  # A4..A0, for set_sheet_format
    title_block: dict[str, Any] | None = None  # form-1 fields, for set_title_block
    entity_id: str | None = None
    entity_id_2: str | None = None  # second segment, for fillet/chamfer
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
    from app.ai.cad_ir.schema import CadParameter, Entity, GeometricConstraint, ReviewItem
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
        if op.op == "confirm":
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
            from app.tasks.cad_trace import _GOST_SHEETS

            fmt = op.sheet_format
            if fmt not in _GOST_SHEETS:
                raise _patch_error(
                    400, IrPatchErrorCode.MISSING_FIELD,
                    f"Неизвестный формат листа: {fmt}. Допустимо: {', '.join(_GOST_SHEETS)}",
                )
            _short_mm, long_mm = _GOST_SHEETS[fmt]
            frame_px = ir.sheet.frame_px
            long_px = (
                max(frame_px[2], frame_px[3])
                if frame_px
                else max(ir.source.image_width, ir.source.image_height)
            )
            ir.scale = long_mm / max(long_px, 1.0)
            ir.scale_source = "sheet_format"
            ir.sheet.format = fmt
            short_mm = _short_mm
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
            ir.parameters = parameters
        elif op.op == "set_title_block":
            from app.ai.cad_ir.title_block import apply_title_block

            _require(op.title_block, "title_block")
            apply_title_block(ir, op.title_block)
            # entity list changed underneath the by_id cache; rebuild it.
            by_id = {e.id: i for i, e in enumerate(ir.entities)}

    validate_ir(ir)
    origin = "review" if all(o.op in ("confirm", "delete", "set_scale", "set_sheet_format") for o in body.ops) else "editor"
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
    from app.ai.cad_ir.constraints import evaluate_constraints

    gen = await db.get(ImageGeneration, generation_id)
    if not _owns(gen, user):
        raise HTTPException(404, "Не найдено")
    _revision, ir = await _load_current_ir(db, gen)
    checks = evaluate_constraints(ir)
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
