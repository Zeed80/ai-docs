"""LoRA training API — the studio's "Обучение LoRA" tab.

Flow: upload sources → create dataset (Celery prepares targets/controls/
captions with QA, stores previews) → create training run (confirmation
dialog lives in the panel; the run queues immediately — user decision
2026-07-05, the ``lora.train`` approval gate was removed) → live progress
(step/loss/eta/samples) → checkpoints → deploy a checkpoint to the ComfyUI
node.

All objects are owner-scoped: only the owner (or an admin / the internal
agent-service identity) may see or mutate them.
"""

from __future__ import annotations

import pathlib
import re
import shutil
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

import structlog
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.lora_base_models import (
    DEFAULT_BASE_MODEL,
    LORA_BASE_MODELS,
    base_model_info,
    eta_hours,
    get_hf_token,
    hf_token_status,
)
from app.auth.jwt import get_current_user
from app.auth.models import UserInfo, UserRole
from app.db.models import (
    LoraDataset,
    LoraDatasetStatus,
    LoraRunStatus,
    LoraTrainingRun,
)
from app.db.session import get_db
from app.storage import download_file

router = APIRouter()
logger = structlog.get_logger()

_ALLOWED_UPLOAD_SUFFIXES = {".png", ".jpg", ".jpeg", ".dxf", ".dwg", ".pdf"}
_COMFYUI_LORAS_DIR = pathlib.Path("/comfyui-loras")
_MAX_UPLOAD_BYTES = 200 * 1024 * 1024
_MAX_LORA_BYTES = 4 * 1024 * 1024 * 1024  # a rank-128 LoRA is ~hundreds of MB
_PREPARING_STALE = timedelta(minutes=15)
_QUEUED_STALE = timedelta(minutes=30)


def _data_dir() -> pathlib.Path:
    from app.config import settings

    return pathlib.Path(getattr(settings, "lora_data_dir", "/lora-data"))


# ── Access control ───────────────────────────────────────────────────────────


def _is_agent_service(user: UserInfo) -> bool:
    # Same contract as image_generation._is_agent_service: the capability
    # dispatcher presents the fixed service sub for agent-mediated calls.
    return user.sub == "agent-service"


def _is_admin(user: UserInfo) -> bool:
    return UserRole.admin in (user.roles or [])


def _sees_all(user: UserInfo) -> bool:
    return _is_admin(user) or _is_agent_service(user)


def _can_access(obj: LoraDataset | LoraTrainingRun | None, user: UserInfo) -> bool:
    return obj is not None and (obj.owner_sub == user.sub or _sees_all(user))


async def _get_dataset_checked(db: AsyncSession, dataset_id: uuid.UUID,
                               user: UserInfo) -> LoraDataset:
    ds = await db.get(LoraDataset, dataset_id)
    if not _can_access(ds, user):
        raise HTTPException(404, "Датасет не найден")
    return ds


async def _get_run_checked(db: AsyncSession, run_id: uuid.UUID,
                           user: UserInfo) -> LoraTrainingRun:
    run = await db.get(LoraTrainingRun, run_id)
    if not _can_access(run, user):
        raise HTTPException(404, "Запуск не найден")
    return run


# ── Schemas ──────────────────────────────────────────────────────────────────


class DatasetParams(BaseModel):
    """Validated preparation recipe (was a free-form dict — a typo'd value
    used to surface as a cryptic ValueError hours into preparation)."""

    model_config = {"extra": "forbid"}

    synth_count: int = Field(default=0, ge=0, le=2000)
    per_image: int = Field(default=2, ge=1, le=6)
    seed: int = Field(default=42, ge=0, le=2**31 - 1)
    target_long_side: Literal[768, 1024, 1536, 2048] = 1024
    caption_model: str | None = Field(default=None, max_length=120)
    caption_fallback: str | None = Field(default=None, max_length=120)
    instruction: str | None = Field(default=None, max_length=2000)
    eskd_sheet: bool = True


class DatasetCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    preset: Literal["drawing_cleanup", "drawing_edit"] = "drawing_cleanup"
    source_paths: list[str] = Field(default_factory=list, max_length=500)  # from /upload
    params: DatasetParams = Field(default_factory=DatasetParams)


class RunConfig(BaseModel):
    model_config = {"extra": "forbid"}

    steps: int = Field(default=2500, ge=100, le=20000)
    lr: float = Field(default=1e-4, gt=0, le=1e-2)
    rank: int = Field(default=32, ge=4, le=128)
    resolution: Literal[512, 768, 1024] = 768
    save_every: int = Field(default=500, ge=50, le=5000)
    sample_every: int = Field(default=250, ge=50, le=5000)
    base_model: str = DEFAULT_BASE_MODEL
    resume_from: str | None = Field(default=None, max_length=1000)

    @field_validator("base_model")
    @classmethod
    def _known_base_model(cls, v: str) -> str:
        if v not in LORA_BASE_MODELS:
            raise ValueError(
                f"Неизвестная базовая модель «{v}». "
                f"Доступны: {', '.join(sorted(LORA_BASE_MODELS))}"
            )
        return v

    @field_validator("resume_from")
    @classmethod
    def _safe_resume_path(cls, v: str | None) -> str | None:
        if v is None:
            return v
        p = pathlib.PurePosixPath(v)
        if p.is_absolute() or ".." in p.parts:
            raise ValueError("resume_from: только относительный путь без «..»")
        return v


class RunCreate(BaseModel):
    dataset_id: uuid.UUID
    name: str = Field(min_length=1, max_length=200)
    config: RunConfig = Field(default_factory=RunConfig)
    # Optional: continue-training (fine-tune) an existing LoRA. A ref from
    # GET /loras: "upload:<file>" | "run:<run_id>:<checkpoint>" | "node:<file>".
    resume_lora: str | None = Field(default=None, max_length=400)


def _dataset_out(ds: LoraDataset) -> dict:
    return {
        "id": str(ds.id),
        "name": ds.name,
        "status": ds.status.value,
        "preset": ds.preset,
        "params": ds.params or {},
        "stats": ds.stats or {},
        "preview_paths": ds.preview_paths or [],
        "error": ds.error,
        "created_at": ds.created_at.isoformat() if ds.created_at else None,
    }


def _run_out(run: LoraTrainingRun) -> dict:
    cfg = run.config or {}
    steps = int(cfg.get("steps", 2500))
    return {
        "id": str(run.id),
        "dataset_id": str(run.dataset_id),
        "name": run.name,
        "status": run.status.value,
        "config": cfg,
        "base_family": run.base_family or "qwen",
        "eta_hours": eta_hours(cfg.get("base_model"), steps),
        "progress": run.progress or {},
        "checkpoints": run.checkpoints or [],
        "sample_paths": run.sample_paths or [],
        "control_paths": run.control_paths or [],
        "error": run.error,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
    }


# ── Watchdog: surface silently-dead background work ─────────────────────────


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _age(ts: datetime | None) -> timedelta:
    if ts is None:
        return timedelta(0)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return _utcnow() - ts


async def _watchdog_datasets(db: AsyncSession, datasets: list[LoraDataset]) -> None:
    """A dataset stuck in ``preparing`` with no live worker heartbeat is dead
    (worker restarted mid-task); mark it failed instead of showing an
    eternal spinner."""
    from app.ai import gpu_lock

    dirty = False
    for ds in datasets:
        if ds.status != LoraDatasetStatus.preparing:
            continue
        if _age(ds.updated_at or ds.created_at) < _PREPARING_STALE:
            continue
        try:
            alive = gpu_lock._redis().get(gpu_lock.dataset_heartbeat_key(str(ds.id)))
        except Exception:  # noqa: BLE001 — no Redis, no verdict
            continue
        if not alive:
            ds.status = LoraDatasetStatus.failed
            ds.error = ("Подготовка прервана (воркер перезапущен?). "
                        "Создайте датасет заново — готовые артефакты переиспользуются.")
            dirty = True
    if dirty:
        await db.commit()


async def _watchdog_runs(db: AsyncSession, runs: list[LoraTrainingRun]) -> None:
    """``queued`` is legitimate for hours while another run holds the GPU
    (the dedicated queue is serialized); it is dead only when nothing holds
    the lock, nothing heartbeats and the row hasn't moved for a while."""
    from app.ai import gpu_lock

    dirty = False
    for run in runs:
        if run.status != LoraRunStatus.queued:
            continue
        if _age(run.updated_at or run.created_at) < _QUEUED_STALE:
            continue
        try:
            if gpu_lock.is_locked():
                continue
            alive = gpu_lock._redis().get(gpu_lock.run_heartbeat_key(str(run.id)))
        except Exception:  # noqa: BLE001
            continue
        if not alive:
            run.status = LoraRunStatus.failed
            run.error = ("Задача обучения потеряна (воркер перезапущен до старта?). "
                         "Создайте запуск заново.")
            dirty = True
    if dirty:
        await db.commit()


# ── Uploads ──────────────────────────────────────────────────────────────────


@router.post("/upload")
async def upload_source(
    file: UploadFile = File(...),
    user: UserInfo = Depends(get_current_user),
):
    suffix = pathlib.Path(file.filename or "src").suffix.lower()
    if suffix not in _ALLOWED_UPLOAD_SUFFIXES:
        raise HTTPException(400, f"Неподдерживаемый формат: {suffix}. "
                                 f"Допустимо: {', '.join(sorted(_ALLOWED_UPLOAD_SUFFIXES))}")
    safe_name = re.sub(r"[^\w.\-]", "_", pathlib.Path(file.filename or "src").name,
                       flags=re.UNICODE)
    rel = pathlib.Path("uploads") / user.sub / f"{uuid.uuid4().hex[:8]}_{safe_name}"
    dest = _data_dir() / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    size = 0
    try:
        with dest.open("wb") as fh:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > _MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        413, f"Файл больше {_MAX_UPLOAD_BYTES // (1024 * 1024)} МБ")
                fh.write(chunk)
    except HTTPException:
        dest.unlink(missing_ok=True)
        raise
    return {"path": str(rel)}


def _validate_source_paths(paths: list[str], user: UserInfo) -> None:
    """Sources must stay inside the caller's own uploads folder — the worker
    reads these paths, so anything looser is an arbitrary-file read."""
    for src in paths:
        p = pathlib.PurePosixPath(src)
        if p.is_absolute() or ".." in p.parts:
            raise HTTPException(400, f"Недопустимый путь источника: {src}")
        parts = p.parts
        if len(parts) < 3 or parts[0] != "uploads":
            raise HTTPException(400, f"Источник должен быть загружен через /upload: {src}")
        if parts[1] != user.sub and not _sees_all(user):
            raise HTTPException(400, f"Источник принадлежит другому пользователю: {src}")


# ── Datasets ─────────────────────────────────────────────────────────────────


@router.get("/datasets")
async def list_datasets(
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
):
    q = select(LoraDataset).order_by(LoraDataset.created_at.desc()).limit(100)
    if not _sees_all(user):
        q = q.where(LoraDataset.owner_sub == user.sub)
    rows = (await db.execute(q)).scalars().all()
    await _watchdog_datasets(db, rows)
    return {"datasets": [_dataset_out(d) for d in rows]}


@router.post("/datasets")
async def create_dataset(
    body: DatasetCreate,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
):
    if body.preset == "drawing_edit" and body.source_paths:
        raise HTTPException(400, "Пресет «Правки чертежей» обучается только на "
                                 "синтетических парах — загруженные файлы не используются.")
    if body.preset == "drawing_cleanup" and not body.source_paths and body.params.synth_count <= 0:
        raise HTTPException(400, "Нужны исходники и/или synth_count > 0.")
    _validate_source_paths(body.source_paths, user)

    ds = LoraDataset(
        owner_sub=user.sub,
        name=body.name,
        preset=body.preset,
        params=body.params.model_dump(exclude_none=True),
        source_paths=body.source_paths,
        status=LoraDatasetStatus.preparing,
    )
    db.add(ds)
    await db.commit()
    await db.refresh(ds)

    from app.tasks.celery_app import celery_app

    task = celery_app.send_task("lora.prepare_dataset", args=[str(ds.id)])
    ds.celery_task_id = task.id
    await db.commit()
    return _dataset_out(ds)


@router.get("/datasets/{dataset_id}")
async def get_dataset(
    dataset_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
):
    ds = await _get_dataset_checked(db, dataset_id, user)
    await _watchdog_datasets(db, [ds])
    return _dataset_out(ds)


_PREVIEW_DATASET_RE = re.compile(r"^lora/([0-9a-f-]{36})/")
_PREVIEW_RUN_RE = re.compile(r"^lora/runs/([0-9a-f-]{36})/")


@router.get("/preview")
async def preview_image(
    path: str,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
):
    if ".." in path or not path.startswith("lora/"):
        raise HTTPException(400, "Недопустимый путь")
    # Previews may show confidential drawings — enforce the same ownership
    # rule as the objects themselves.
    m_run = _PREVIEW_RUN_RE.match(path)
    m_ds = None if m_run else _PREVIEW_DATASET_RE.match(path)
    if m_run:
        await _get_run_checked(db, uuid.UUID(m_run.group(1)), user)
    elif m_ds:
        await _get_dataset_checked(db, uuid.UUID(m_ds.group(1)), user)
    else:
        raise HTTPException(400, "Недопустимый путь")
    try:
        return Response(content=download_file(path), media_type="image/jpeg")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(404, f"Файл не найден: {exc}")


# ── Training runs ────────────────────────────────────────────────────────────


@router.get("/base-models")
async def base_models(user: UserInfo = Depends(get_current_user)):
    """Base-model catalog for the run form (label, family, VRAM fit)."""
    return {
        "models": [
            {
                "key": key,
                "label": info["label"],
                "family": info["family"],
                "fits_24gb": info["fits_24gb"],
                "vram_note": info.get("vram_note"),
                "sec_per_step": info.get("sec_per_step"),
                "gated": info.get("gated", False),
            }
            for key, info in LORA_BASE_MODELS.items()
        ],
        "default": DEFAULT_BASE_MODEL,
    }


# ── LoRA library (continue-training / fine-tune) ─────────────────────────────


def _loras_dir(user: UserInfo) -> pathlib.Path:
    return _data_dir() / "loras" / user.sub


async def _resolve_lora_source(ref: str, db: AsyncSession,
                               user: UserInfo) -> pathlib.Path:
    """Ref → absolute source path to INSPECT (no copy). Owner-scoped."""
    kind, _, rest = ref.partition(":")
    if kind == "upload":
        src = _loras_dir(user) / pathlib.PurePosixPath(rest).name
    elif kind == "node":
        src = _COMFYUI_LORAS_DIR / pathlib.PurePosixPath(rest).name
    elif kind == "run":
        run_id, _, ckpt = rest.partition(":")
        try:
            run = await _get_run_checked(db, uuid.UUID(run_id), user)
        except (ValueError, HTTPException):
            raise HTTPException(404, "Запуск LoRA не найден")
        ckpt = pathlib.PurePosixPath(ckpt).name
        if ckpt not in (run.checkpoints or []):
            raise HTTPException(404, "Чекпойнта нет у запуска")
        src = pathlib.Path(run.output_dir or "") / f"run_{run_id}" / ckpt
    else:
        raise HTTPException(400, f"Неизвестная ссылка на LoRA: {ref}")
    if not src.exists():
        raise HTTPException(404, "Файл LoRA не найден")
    return src


async def _prepare_resume_path(ref: str, db: AsyncSession, user: UserInfo) -> str:
    """Ref → path RELATIVE to lora_data that the trainer container can see.
    Node/ComfyUI LoRAs (outside the shared volume) are copied into the user's
    loras cache so the trainer mount reaches them."""
    src = await _resolve_lora_source(ref, db, user)
    kind = ref.split(":", 1)[0]
    if kind == "upload":
        return f"loras/{user.sub}/{src.name}"
    if kind == "run":
        run_id = ref.split(":")[1]
        return f"runs/{run_id}/output/run_{run_id}/{src.name}"
    # node → copy into the shared volume
    dest_rel = f"loras/{user.sub}/_node/{src.name}"
    dest = _data_dir() / dest_rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dest)
    return dest_rel


@router.post("/loras/upload")
async def upload_lora(
    file: UploadFile = File(...),
    user: UserInfo = Depends(get_current_user),
):
    """Upload a third-party (or exported) LoRA .safetensors to continue
    training from. Returns its ref and inspected metadata."""
    name = pathlib.Path(file.filename or "lora.safetensors").name
    if not name.endswith(".safetensors"):
        raise HTTPException(400, "Ожидается файл .safetensors")
    safe = re.sub(r"[^\w.\-]", "_", name, flags=re.UNICODE)
    dest = _loras_dir(user) / safe
    dest.parent.mkdir(parents=True, exist_ok=True)
    size = 0
    try:
        with dest.open("wb") as fh:
            while chunk := await file.read(4 * 1024 * 1024):
                size += len(chunk)
                if size > _MAX_LORA_BYTES:
                    raise HTTPException(413, "Файл LoRA слишком большой")
                fh.write(chunk)
    except HTTPException:
        dest.unlink(missing_ok=True)
        raise

    from app.ai.lora_inspect import inspect_lora

    info = inspect_lora(dest)
    if not info.get("ok"):
        dest.unlink(missing_ok=True)
        raise HTTPException(400, f"Не удалось прочитать LoRA: {info.get('error')}")
    return {"ref": f"upload:{safe}", "label": safe, "source": "upload", **info}


@router.get("/loras")
async def list_loras(
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
):
    """Available LoRAs to continue training from: uploaded files, this user's
    finished-run checkpoints, and LoRAs deployed on the ComfyUI node. Each
    carries inspected family/rank so the UI can flag compatibility."""
    from app.ai.lora_inspect import inspect_lora

    items: list[dict] = []

    # 1. Uploaded.
    up = _loras_dir(user)
    if up.exists():
        for f in sorted(up.glob("*.safetensors")):
            items.append({"ref": f"upload:{f.name}", "label": f.name,
                          "source": "upload", **inspect_lora(f)})

    # 2. Own finished-run checkpoints.
    q = select(LoraTrainingRun).where(
        LoraTrainingRun.status == LoraRunStatus.done
    ).order_by(LoraTrainingRun.created_at.desc()).limit(50)
    if not _sees_all(user):
        q = q.where(LoraTrainingRun.owner_sub == user.sub)
    for run in (await db.execute(q)).scalars().all():
        for ckpt in (run.checkpoints or []):
            src = pathlib.Path(run.output_dir or "") / f"run_{run.id}" / ckpt
            if not src.exists():
                continue
            info = inspect_lora(src)
            items.append({
                "ref": f"run:{run.id}:{ckpt}",
                "label": f"{run.name} · {ckpt.split('_')[-1].replace('.safetensors', '')}",
                "source": "run", **info,
            })

    # 3. Deployed on the ComfyUI node (shared, admin/engineer curated).
    if _COMFYUI_LORAS_DIR.exists():
        for f in sorted(_COMFYUI_LORAS_DIR.glob("*.safetensors")):
            items.append({"ref": f"node:{f.name}", "label": f.name,
                          "source": "node", **inspect_lora(f)})

    return {"loras": items}


class LoraCheckBody(BaseModel):
    ref: str = Field(max_length=400)
    base_model: str = DEFAULT_BASE_MODEL
    rank: int = Field(default=32, ge=4, le=128)


@router.post("/loras/check")
async def check_lora(
    body: LoraCheckBody,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
):
    """Compatibility of a LoRA with a chosen base model + rank, for live UI
    feedback before launching."""
    from app.ai.lora_inspect import check_compatibility, inspect_lora

    src = await _resolve_lora_source(body.ref, db, user)
    info = inspect_lora(src)
    family = base_model_info(body.base_model)["family"]
    check = check_compatibility(info, family, body.rank)
    return {"info": info, "check": check}


@router.get("/hf-token")
async def get_hf_token_status(user: UserInfo = Depends(get_current_user)):
    """Whether a HuggingFace token is configured for gated FLUX.2 models. The
    token itself is managed in Настройки → Модели (shared across providers);
    this only reports its presence so the panel can hint correctly."""
    return hf_token_status()


@router.post("/runs")
async def create_run(
    body: RunCreate,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
):
    ds = await _get_dataset_checked(db, body.dataset_id, user)
    if ds.status != LoraDatasetStatus.ready:
        raise HTTPException(409, "Датасет ещё не готов")

    info = base_model_info(body.config.base_model)
    # Pre-flight: a gated HF base model without a configured token is doomed
    # to a 401 ~10 min into the download — fail fast with actionable guidance.
    if info.get("gated") and not get_hf_token():
        from app.ai.lora_base_models import HF_GATED_HELP

        raise HTTPException(400, HF_GATED_HELP.format(hf=info["hf"]))

    config = body.config.model_dump(exclude_none=True)

    # Continue-training (fine-tune) an existing LoRA: verify compatibility
    # BEFORE queuing (a family mismatch would crash the run), then resolve to
    # a trainer-visible path and align rank to the LoRA's own rank.
    if body.resume_lora:
        from app.ai.lora_inspect import check_compatibility, inspect_lora

        src = await _resolve_lora_source(body.resume_lora, db, user)
        lora_info = inspect_lora(src)
        check = check_compatibility(lora_info, info["family"], int(config.get("rank", 32)))
        if not check["compatible"]:
            raise HTTPException(400, "LoRA несовместима с выбранной базовой "
                                     "моделью:\n" + "\n".join(check["reasons"]))
        config["resume_from"] = await _prepare_resume_path(body.resume_lora, db, user)
        if check.get("suggested_rank"):
            config["rank"] = int(check["suggested_rank"])

    run = LoraTrainingRun(
        owner_sub=user.sub,
        dataset_id=ds.id,
        name=body.name,
        config=config,
        base_family=info["family"],
        status=LoraRunStatus.queued,
    )
    db.add(run)
    await db.flush()

    from app.tasks.celery_app import celery_app

    task = celery_app.send_task("lora.run_training", args=[str(run.id)])
    run.celery_task_id = task.id
    await db.commit()
    await db.refresh(run)
    logger.info("lora_training_queued", run_id=str(run.id), user=user.sub,
                base_model=body.config.base_model)
    return _run_out(run)


@router.get("/runs")
async def list_runs(
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
):
    q = select(LoraTrainingRun).order_by(LoraTrainingRun.created_at.desc()).limit(100)
    if not _sees_all(user):
        q = q.where(LoraTrainingRun.owner_sub == user.sub)
    rows = (await db.execute(q)).scalars().all()
    await _watchdog_runs(db, rows)
    return {"runs": [_run_out(r) for r in rows]}


@router.get("/runs/{run_id}")
async def get_run(
    run_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
):
    run = await _get_run_checked(db, run_id, user)
    await _watchdog_runs(db, [run])
    return _run_out(run)


@router.post("/runs/{run_id}/stop")
async def stop_run(
    run_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
):
    run = await _get_run_checked(db, run_id, user)
    if run.status == LoraRunStatus.queued:
        # Not started yet: revoke the task and cancel outright. The revoke is
        # best-effort (the task may start this very second) — _train re-checks
        # the status on entry and bows out of a cancelled run.
        from app.tasks.celery_app import celery_app

        if run.celery_task_id:
            try:
                celery_app.control.revoke(run.celery_task_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("lora_revoke_failed", run_id=str(run_id),
                               error=str(exc)[:120])
        run.status = LoraRunStatus.cancelled
        await db.commit()
        return {"ok": True, "status": run.status.value}
    if run.status != LoraRunStatus.running:
        raise HTTPException(409, f"Нельзя остановить запуск в статусе {run.status.value}")
    run.status = LoraRunStatus.stopping
    await db.commit()
    # Redis flag: the supervisor's refresher thread sees it within a minute
    # even when the trainer is in a silent phase (logs stalled).
    from app.ai import gpu_lock

    try:
        gpu_lock.request_stop(str(run_id))
    except Exception as exc:  # noqa: BLE001
        logger.warning("lora_stop_flag_failed", run_id=str(run_id), error=str(exc)[:120])
    return {"ok": True, "status": run.status.value}


def _safe_lora_filename(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    return cleaned.strip("._") or "lora"


async def _deploy(run: LoraTrainingRun, checkpoint: str) -> str:
    if checkpoint not in (run.checkpoints or []):
        raise HTTPException(404, "Такого чекпойнта нет у запуска")
    if not _COMFYUI_LORAS_DIR.exists():
        raise HTTPException(
            503,
            "Каталог LoRA узла ComfyUI не смонтирован в контейнер "
            "(volume /comfyui-loras). Добавьте bind-mount в docker-compose.",
        )
    src = pathlib.Path(run.output_dir or "") / f"run_{run.id}" / checkpoint
    if not src.exists():
        raise HTTPException(404, f"Файл чекпойнта не найден: {src.name}")
    dest_name = _safe_lora_filename(f"{run.name}_{checkpoint}")
    dest = (_COMFYUI_LORAS_DIR / dest_name).resolve()
    if dest.parent != _COMFYUI_LORAS_DIR.resolve():
        raise HTTPException(400, "Недопустимое имя файла LoRA")
    shutil.copyfile(src, dest)
    return dest_name


@router.post("/runs/{run_id}/deploy")
async def deploy_checkpoint(
    run_id: uuid.UUID,
    checkpoint: str,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
):
    """Copy a finished checkpoint into the ComfyUI node's loras folder (host
    bind mount) so cleanup/edit workflows can reference it."""
    run = await _get_run_checked(db, run_id, user)
    lora_name = await _deploy(run, checkpoint)
    return {"ok": True, "lora_name": lora_name,
            "base_family": run.base_family or "qwen"}


class MakeWorkflowBody(BaseModel):
    checkpoint: str
    strength: float = Field(default=1.0, ge=0.0, le=2.0)
    title: str | None = None


_PRESET_OPERATION = {"drawing_cleanup": "cleanup", "drawing_edit": "edit"}


@router.post("/runs/{run_id}/make-workflow")
async def make_workflow(
    run_id: uuid.UUID,
    body: MakeWorkflowBody,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
):
    """One-click application of a trained LoRA: deploy the checkpoint to the
    ComfyUI node AND clone the operation's builtin workflow with a LoRA node
    wired in. The builtin must match the run's base-model family — a FLUX.2
    LoRA in a Qwen graph silently produces garbage. For cleanup LoRAs the
    clone ships the measured working point (Lightning off, steps=25, cfg=1.0
    — see project memory: cfg 3 makes the v2 LoRA drop the sheet frame and
    drift the layout)."""
    run = await _get_run_checked(db, run_id, user)
    lora_name = await _deploy(run, body.checkpoint)
    family = run.base_family or "qwen"

    ds = await db.get(LoraDataset, run.dataset_id)
    operation = _PRESET_OPERATION.get(ds.preset if ds else "", "cleanup")

    from app.db.models import ComfyWorkflow

    base = (
        await db.execute(
            select(ComfyWorkflow).where(
                ComfyWorkflow.operation == operation,
                ComfyWorkflow.base_family == family,
                ComfyWorkflow.is_builtin.is_(True),
                ComfyWorkflow.enabled.is_(True),
            ).limit(1)
        )
    ).scalar_one_or_none()
    if not base:
        raise HTTPException(
            409,
            f"Нет встроенного воркфлоу для операции {operation} и базовой "
            f"модели {family}. Для FLUX.2 установите модель на узле ComfyUI "
            "и добавьте встроенный воркфлоу.",
        )

    graph = dict(base.graph or {})
    sampler_id = next(
        (nid for nid, node in graph.items() if node.get("class_type") == "KSampler"), None
    )
    if not sampler_id:
        raise HTTPException(409, "В базовом воркфлоу не найден KSampler")
    prev_model = graph[sampler_id]["inputs"]["model"]
    new_id = str(max(int(n) for n in graph if n.isdigit()) + 1)
    graph[new_id] = {
        "class_type": "LoraLoaderModelOnly",
        "inputs": {"model": prev_model, "lora_name": lora_name,
                   "strength_model": body.strength},
    }
    graph[sampler_id] = dict(graph[sampler_id])
    graph[sampler_id]["inputs"] = dict(graph[sampler_id]["inputs"])
    graph[sampler_id]["inputs"]["model"] = [new_id, 0]

    if operation == "cleanup" and family == "qwen":
        graph[sampler_id]["inputs"]["steps"] = 25
        graph[sampler_id]["inputs"]["cfg"] = 1.0
        for node in graph.values():
            if (node.get("class_type") == "LoraLoaderModelOnly"
                    and "Lightning" in str(node["inputs"].get("lora_name", ""))):
                node["inputs"] = dict(node["inputs"])
                node["inputs"]["strength_model"] = 0.0

    inject_map = dict(base.inject_map or {})
    inject_map["custom_lora_strength"] = {"node": new_id, "input": "strength_model"}
    params_schema = dict(base.params_schema or {})
    params_schema["custom_lora_strength"] = {
        "type": "float", "default": body.strength, "min": 0.0, "max": 2.0,
        "label": f"Сила LoRA «{run.name}»",
    }
    if operation == "cleanup":
        # A trained cleanup LoRA renders clean soft-toned output on its own;
        # the classic binarize+vectorize pass visibly degrades it (user-
        # confirmed against raw ComfyUI). Schema defaults flow into task
        # params automatically.
        params_schema["postprocess"] = {
            "type": "str", "default": "text_only",
            "label": "Постобработка (full / text_only / none)",
        }

    wf = ComfyWorkflow(
        key=f"lora_{run_id.hex[:8]}_{body.checkpoint[:20]}",
        is_builtin=False,
        enabled=True,
        owner_sub=user.sub,
        title=body.title or f"{base.title} + LoRA «{run.name}»",
        description=f"Клон «{base.title}» с обученной LoRA {lora_name} "
                    f"(чекпойнт {body.checkpoint}).",
        category=base.category,
        operation=operation,
        base_family=family,
        graph=graph,
        inject_map=inject_map,
        params_schema=params_schema,
    )
    db.add(wf)
    await db.commit()
    await db.refresh(wf)
    return {"ok": True, "workflow_id": str(wf.id), "title": wf.title, "lora_name": lora_name}


@router.delete("/datasets/{dataset_id}")
async def delete_dataset(
    dataset_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
):
    """Remove a dataset with its volume files, source uploads (unless another
    dataset still references them) and MinIO previews. Refused while a
    training run still references it."""
    ds = await _get_dataset_checked(db, dataset_id, user)
    runs = (
        await db.execute(
            select(LoraTrainingRun).where(
                LoraTrainingRun.dataset_id == dataset_id,
                LoraTrainingRun.status.in_(
                    [LoraRunStatus.pending_approval, LoraRunStatus.queued,
                     LoraRunStatus.running, LoraRunStatus.stopping]
                ),
            )
        )
    ).scalars().first()
    if runs:
        raise HTTPException(409, "Датасет используется активным обучением")
    if ds.dataset_dir:
        shutil.rmtree(ds.dataset_dir, ignore_errors=True)

    own_sources = {s for s in (ds.source_paths or []) if s.startswith("uploads/")}
    if own_sources:
        others = (
            await db.execute(select(LoraDataset.source_paths)
                             .where(LoraDataset.id != dataset_id))
        ).scalars().all()
        still_used = {s for paths in others for s in (paths or [])}
        for src in own_sources - still_used:
            (_data_dir() / src).unlink(missing_ok=True)

    _delete_minio_prefix(f"lora/{dataset_id}/")
    await db.delete(ds)
    await db.commit()
    return {"ok": True}


@router.delete("/runs/{run_id}")
async def delete_run(
    run_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
):
    """Remove a finished run with its outputs (checkpoints included — deploy
    what you need to the ComfyUI node first)."""
    run = await _get_run_checked(db, run_id, user)
    if run.status in (LoraRunStatus.queued, LoraRunStatus.running, LoraRunStatus.stopping):
        raise HTTPException(409, "Сначала остановите обучение")
    shutil.rmtree(_data_dir() / "runs" / str(run_id), ignore_errors=True)
    _delete_minio_prefix(f"lora/runs/{run_id}/")
    await db.delete(run)
    await db.commit()
    return {"ok": True}


def _delete_minio_prefix(prefix: str) -> None:
    try:
        from app.storage import delete_prefix

        delete_prefix(prefix)
    except Exception as exc:  # noqa: BLE001 — leftovers are cosmetic
        logger.warning("lora_minio_cleanup_failed", prefix=prefix, error=str(exc)[:120])


@router.get("/caption-models")
async def caption_models(user: UserInfo = Depends(get_current_user)):
    """LOCAL vision models from the catalog for dataset captioning.
    Local-only on purpose: real drawings are confidential (Dual AI)."""
    from app.ai.router import ai_router

    out = []
    for key, cap in ai_router.registry.models.items():
        try:
            mods = {m.value for m in cap.modalities}
            if "vision" in mods and cap.local_only and cap.status.value != "disabled":
                out.append({"key": key, "model": cap.provider_model,
                            "provider": cap.provider.value})
        except Exception:  # noqa: BLE001
            continue
    return {"models": out}


@router.get("/gpu-status")
async def gpu_status(user: UserInfo = Depends(get_current_user)):
    from app.ai import gpu_lock

    return {"training_lock": gpu_lock.holder(), "ts": time.time()}
