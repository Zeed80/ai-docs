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
from typing import Any
from urllib.parse import quote

import httpx
import structlog
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.jwt import get_current_user
from app.auth.models import UserInfo
from app.db.models import ComfyWorkflow, Document, ImageGeneration, ImageGenStatus
from app.db.session import get_db
from app.storage import download_file, upload_file

router = APIRouter()
logger = structlog.get_logger()

_SOURCE_PREFIX = "image-gen-src"


# ── Schemas ──────────────────────────────────────────────────────────────────


class GenerateRequest(BaseModel):
    operation: str = Field(default="edit")  # edit | generate | inpaint | cleanup
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
    view: str = "front"  # front (2D drawing) | isometric (3D pictorial)
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


def _owns(gen: ImageGeneration | None, user: UserInfo) -> bool:
    return gen is not None and (gen.owner_sub == user.sub or _is_agent_service(user))


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
    }


# ── Source upload (UI helper) ────────────────────────────────────────────────


@router.post("/upload-source")
async def upload_source(
    file: UploadFile = File(...),
    kind: str = Form("source"),  # source | mask
    user: UserInfo = Depends(get_current_user),
) -> dict:
    """Store a source/mask image in MinIO; returns its path for /generate."""
    content = await file.read()
    if not content:
        raise HTTPException(400, "Пустой файл")
    ext = "png"
    if file.filename and "." in file.filename:
        ext = file.filename.rsplit(".", 1)[-1].lower()[:5]
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
    source_paths = list(body.source_image_paths)
    for doc_id in body.source_document_ids:
        doc = await db.get(Document, doc_id)
        if doc and doc.storage_path:
            source_paths.append(doc.storage_path)

    if body.operation in ("edit", "inpaint", "cleanup") and not source_paths:
        raise HTTPException(400, "Для этой операции нужно исходное изображение.")
    if body.operation == "generate" and not (body.prompt or "").strip():
        raise HTTPException(400, "Для генерации нужно текстовое описание (prompt).")

    gen = ImageGeneration(
        owner_sub=user.sub,
        operation=body.operation,
        workflow_id=body.workflow_id,
        status=ImageGenStatus.queued,
        prompt=body.prompt,
        negative_prompt=body.negative_prompt,
        params=body.params or {},
        source_image_paths=source_paths,
        mask_path=body.mask_path,
        source_document_id=body.source_document_id,
        case_id=body.case_id,
    )
    db.add(gen)
    await db.commit()
    await db.refresh(gen)

    _enqueue(str(gen.id))
    return _gen_out(gen)


def _enqueue(generation_id: str) -> None:
    try:
        from app.tasks.image_generation import run_image_generation

        run_image_generation.delay(generation_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("image_gen_enqueue_failed", generation_id=generation_id, error=str(exc))


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
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> Response:
    gen = await db.get(ImageGeneration, generation_id)
    if not _owns(gen, user):
        raise HTTPException(404, "Не найдено")
    paths = gen.source_image_paths or []
    if index >= len(paths):
        raise HTTPException(404, "Источник не найден")
    data = download_file(paths[index])
    return Response(content=data, media_type="image/png")


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
    if gen.operation == "techdraw" and _is_agent_service_call(request):
        # capabilities.yml gates "accept_techdraw" (routed to a DIFFERENT REST
        # path below), not "accept" — an agent could otherwise dodge the gate
        # by simply calling the ungated action name for the same id. A human
        # clicking "Принять" in the Studio UI (no service-key header) is
        # unaffected — their click IS the required approval.
        raise HTTPException(
            423,
            {
                "error_code": "approval_required",
                "message": (
                    "Приёмка точного чертежа требует подтверждения человека "
                    "(используйте action=accept_techdraw)."
                ),
            },
        )
    gen.accepted = True
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
    await db.commit()
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

    gen = ImageGeneration(
        # Inherit the parent's owner, not the caller's — when the agent
        # iterates on a user's behalf (see _is_agent_service), the new
        # version must stay visible in that user's own /studio list, not get
        # orphaned under the internal service identity.
        owner_sub=parent.owner_sub,
        operation=body.operation or "edit",
        workflow_id=body.workflow_id or parent.workflow_id,
        status=ImageGenStatus.queued,
        prompt=body.prompt,
        negative_prompt=body.negative_prompt,
        params=body.params or {},
        source_image_paths=[parent.result_path],
        parent_id=parent.id,
    )
    db.add(gen)
    await db.commit()
    await db.refresh(gen)
    _enqueue(str(gen.id))
    return _gen_out(gen)


async def _delete_one(db: AsyncSession, gen: ImageGeneration) -> None:
    """Delete a generation + its MinIO files, re-parenting any iteration
    children to roots so the FK never blocks the delete (a failed/erroneous
    gen must always be removable)."""
    from sqlalchemy import update as sa_update

    for path in [gen.result_path, gen.thumbnail_path, gen.mask_path,
                 *(gen.source_image_paths or [])]:
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
    if not src:
        raise HTTPException(404, "Не найдено")
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
    if not wf:
        raise HTTPException(404, "Не найдено")
    if wf.is_builtin:
        raise HTTPException(400, "Встроенный воркфлоу нельзя править — сделайте копию.")
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
    if not wf:
        raise HTTPException(404, "Не найдено")
    if wf.is_builtin:
        raise HTTPException(400, "Встроенный воркфлоу нельзя удалить (только выключить).")
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
    if not wf:
        raise HTTPException(404, "Не найдено")

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
    grounding = ""
    if body.source_document_id:
        # Best-effort: ground the prompt in what the attached drawing shows.
        doc = await db.get(Document, body.source_document_id)
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
