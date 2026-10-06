"""Strata provider: status, GPU switch Ollama ⇄ Strata, quantization.

  GET  /api/local-models/strata/status     — container, server, quants, owner
  GET  /api/local-models/strata/slot-plan  — what a switch to Strata moves
  POST /api/local-models/strata/switch     — {target: strata|ollama, ...}
  PUT  /api/local-models/strata/config     — {model, context, vision}
  GET  /api/local-models/strata/logs       — container log tail

The switch is the operator's explicit decision, so it may reassign slots:
slots now served by GPU-Ollama move to Strata (one revision, rollback-able
like any other), and switching back restores exactly the slots that still
point at Strata. Nothing is moved silently — the plan is shown first.
"""

from __future__ import annotations

from typing import Literal

import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import gpu_runtime, model_runtime_store
from app.ai.providers import strata_manager
from app.ai.schemas import ProviderKind
from app.auth.jwt import get_current_user, require_role
from app.auth.models import UserInfo, UserRole
from app.db.session import get_db

router = APIRouter()
logger = structlog.get_logger()
_admin = [Depends(require_role(UserRole.admin))]

STRATA_MODEL_KEY = "strata_qwen3_8_flash_next"
# Slots whose job Strata can do. Embedding/rerank need a vector model and
# stay where they are (normally the CPU Ollama node, untouched by the switch).
_MOVABLE_MODALITIES = {"text", "tool_calling", "vision"}


class SlotPlanItem(BaseModel):
    slot: str
    label: str
    current_model: str | None
    move: bool
    reason: str


class SwitchIn(BaseModel):
    target: Literal["strata", "ollama"]
    # target=strata: slots to move onto Strata (None → every movable GPU-Ollama slot).
    move_slots: list[str] | None = None
    # target=ollama: restore the slots the last switch moved (if still on Strata).
    restore_slots: bool = True


class ConfigIn(BaseModel):
    model: str
    context: int = strata_manager.DEFAULT_CONTEXT
    vision: bool = True


def _on_gpu_ollama(cap) -> bool:
    from app.ai.provider_registry import select_instance

    if cap.provider != ProviderKind.OLLAMA:
        return False
    try:
        node = select_instance(cap.provider, cap.provider_model, cap.preferred_instance)
    except Exception:  # noqa: BLE001 — not on any node: it would load on the default (GPU) one
        return True
    return gpu_runtime.is_gpu_ollama(node.base_url)


def _slot_plan(vision: bool) -> list[SlotPlanItem]:
    from app.api import providers_api as pa

    registry = pa._registry()
    plan: list[SlotPlanItem] = []
    for slot, _group, label, _hint, _local in pa._SLOTS:
        key = pa._slot_current_model(slot, registry)
        cap = registry.models.get(key) if key else None
        modality = pa._SLOT_MODALITY.get(slot)
        if cap is None:
            plan.append(
                SlotPlanItem(
                    slot=slot, label=label, current_model=key, move=False, reason="не назначен"
                )
            )
            continue
        if cap.provider == ProviderKind.STRATA:
            plan.append(
                SlotPlanItem(
                    slot=slot, label=label, current_model=key, move=False, reason="уже на Strata"
                )
            )
            continue
        if not _on_gpu_ollama(cap):
            plan.append(
                SlotPlanItem(
                    slot=slot,
                    label=label,
                    current_model=key,
                    move=False,
                    reason="не на GPU-Ollama — переключение его не касается",
                )
            )
            continue
        if modality not in _MOVABLE_MODALITIES:
            plan.append(
                SlotPlanItem(
                    slot=slot,
                    label=label,
                    current_model=key,
                    move=False,
                    reason="Strata не умеет эту работу (нужна векторная модель) — слот перестанет работать",
                )
            )
            continue
        if modality == "vision" and not vision:
            plan.append(
                SlotPlanItem(
                    slot=slot,
                    label=label,
                    current_model=key,
                    move=False,
                    reason="нужны картинки, а Strata установлена без них — слот перестанет работать",
                )
            )
            continue
        plan.append(
            SlotPlanItem(
                slot=slot, label=label, current_model=key, move=True, reason="GPU-Ollama → Strata"
            )
        )
    return plan


# Tasks with no slot on the assignment screen still have a model: the
# work-order verifier reads engineering_reasoning, the whole-sheet graph
# pipeline its cad_drawing_graph_* tasks. Left on GPU-Ollama they are refused
# while Strata holds the card — live, the verifier was. They are offered in the
# same plan as "task:<name>" items and their chains are kept for the restore.
TASK_PREFIX = "task:"
_VISION_TASKS = {
    "invoice_ocr",
    "drawing_analysis_vlm",
    "cad_spec_read",
    "cad_text_ocr",
    "cad_drawing_graph_read",
    "cad_drawing_graph_layout",
    "cad_drawing_graph_fragment_read",
    "cad_drawing_graph_evidence_verify",
}
_VECTOR_TASKS = {"embedding", "reranking"}


def _slot_covered_tasks() -> set[str]:
    from app.api import providers_api as pa

    covered = {"invoice_ocr"}
    for slot, *_ in pa._SLOTS:
        covered.update(i for i in pa._slot_affected(slot) if "." not in i and " " not in i)
    return covered


def _task_plan(vision: bool) -> list[SlotPlanItem]:
    from app.ai.task_routing import get_task_routing
    from app.api import providers_api as pa

    registry = pa._registry()
    covered = _slot_covered_tasks()
    plan: list[SlotPlanItem] = []
    for task, routing in sorted(get_task_routing().items(), key=lambda kv: kv[0].value):
        name = task.value
        if name in covered or name in _VECTOR_TASKS:
            continue
        key = routing.primary
        cap = registry.models.get(key) if key else None
        # Only what the switch actually affects is listed.
        if cap is None or not _on_gpu_ollama(cap):
            continue
        label = f"Служебная задача {name}"
        if name in _VISION_TASKS and not vision:
            plan.append(
                SlotPlanItem(
                    slot=TASK_PREFIX + name,
                    label=label,
                    current_model=key,
                    move=False,
                    reason="нужны картинки, а Strata установлена без них — задача перестанет работать",
                )
            )
            continue
        plan.append(
            SlotPlanItem(
                slot=TASK_PREFIX + name,
                label=label,
                current_model=key,
                move=True,
                reason="без слота на экране назначений: GPU-Ollama → Strata",
            )
        )
    return plan


async def _set_task_chains(db: AsyncSession, chains: dict[str, list[str]]) -> None:
    from app.ai.schemas import AITask
    from app.ai.task_routing import get_routing_for, save_task_routing

    for name, models in chains.items():
        task = AITask(name)
        save_task_routing(task, get_routing_for(task).model_copy(update={"models": models}))
        await model_runtime_store.persist_task_routing(
            db, task=name, routing=get_routing_for(task).model_dump(mode="json")
        )
    await db.commit()
    await model_runtime_store.hydrate_runtime_cache(db)


async def _move_tasks(db: AsyncSession, names: list[str]) -> dict[str, list[str]]:
    """Put Strata at the head of each task's chain; returns the original chains."""
    from app.ai.schemas import AITask
    from app.ai.task_routing import get_routing_for

    saved = {name: list(get_routing_for(AITask(name)).models) for name in names}
    await _set_task_chains(
        db,
        {
            name: [STRATA_MODEL_KEY, *[m for m in models if m != STRATA_MODEL_KEY]]
            for name, models in saved.items()
        },
    )
    return saved


async def _restore_tasks(db: AsyncSession) -> list[str]:
    from app.ai.schemas import AITask
    from app.ai.task_routing import get_routing_for

    saved = strata_manager.get_switch_tasks()
    target = {
        name: models
        for name, models in saved.items()
        # Re-assigned since the switch: the operator's decision now.
        if (get_routing_for(AITask(name)).models or [None])[0] == STRATA_MODEL_KEY
    }
    if target:
        await _set_task_chains(db, target)
    strata_manager.set_switch_tasks({})
    return sorted(TASK_PREFIX + n for n in target)


async def _apply_slots(
    db: AsyncSession, user: UserInfo, target: dict[str, str | None]
) -> str | None:
    """Assign ``target`` through the same path as the assignment screen; returns the revision id."""
    from app.api import providers_api as pa

    if not target:
        return None
    registry = pa._registry()
    loaded = await pa._loaded_index()
    diff, warnings, errors = await pa._validate_assignment_draft(registry, target, loaded)
    if errors:
        raise HTTPException(400, {"errors": [e.model_dump() for e in errors]})
    if not diff:
        return None
    before = pa._assignment_snapshot(registry)
    await pa._apply_draft_atomic(db, diff, before, registry)
    after = pa._assignment_snapshot(pa._registry())
    revision = await model_runtime_store.create_assignment_revision(
        db,
        created_by=user.sub,
        before_snapshot=before,
        after_snapshot=after,
        diff=[d.model_dump() for d in diff],
        warnings=[w.model_dump() for w in warnings],
    )
    await db.commit()
    await model_runtime_store.hydrate_runtime_cache(db)
    return str(revision.id)


@router.get("/status", dependencies=_admin)
async def strata_status() -> dict:
    return await strata_manager.status()


@router.get("/slot-plan", response_model=list[SlotPlanItem], dependencies=_admin)
async def strata_slot_plan() -> list[SlotPlanItem]:
    vision = strata_manager.read_desired()["vision"]
    return _slot_plan(vision) + _task_plan(vision)


@router.post("/switch", dependencies=_admin)
async def strata_switch(
    body: SwitchIn,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_current_user),
) -> dict:
    moved: list[str] = []
    restored: list[str] = []
    unloaded: list[str] = []
    try:
        if body.target == "strata":
            vision = strata_manager.read_desired()["vision"]
            plan = _slot_plan(vision) + _task_plan(vision)
            movable = {p.slot for p in plan if p.move}
            wanted = movable if body.move_slots is None else set(body.move_slots) & movable
            slots = sorted(w for w in wanted if not w.startswith(TASK_PREFIX))
            tasks = sorted(w.removeprefix(TASK_PREFIX) for w in wanted if w.startswith(TASK_PREFIX))
            unloaded = await strata_manager.start_strata()
            revision = await _apply_slots(db, user, {slot: STRATA_MODEL_KEY for slot in slots})
            if revision:
                strata_manager.set_switch_revision(revision)
                moved += slots
            if tasks:
                strata_manager.set_switch_tasks(
                    {**strata_manager.get_switch_tasks(), **await _move_tasks(db, tasks)}
                )
                moved += [TASK_PREFIX + t for t in tasks]
        else:
            await strata_manager.stop_strata()
            if body.restore_slots:
                restored = await _restore_slots(db, user) + await _restore_tasks(db)
    except strata_manager.StrataError as exc:
        raise HTTPException(409, str(exc)) from exc
    logger.info(
        "gpu_runtime_switched", target=body.target, moved=moved, restored=restored, by=user.sub
    )
    return {
        "ok": True,
        "moved": moved,
        "restored": restored,
        "ollama_unloaded": unloaded,
        "status": await strata_manager.status(),
    }


async def _restore_slots(db: AsyncSession, user: UserInfo) -> list[str]:
    """Undo the last switch for the slots that still point at Strata."""
    from app.api import providers_api as pa

    revision_id = strata_manager._get_switch_revision()
    if not revision_id:
        return []
    revision = await model_runtime_store.get_assignment_revision(db, revision_id)
    if revision is None:
        strata_manager.set_switch_revision(None)
        return []
    old = {
        item.get("slot"): item.get("old_model") for item in revision.diff or [] if item.get("slot")
    }
    restored: set[str] = set()
    # Two passes, because slots can share storage: "ocr_large" is the second
    # element of the invoice_ocr chain whose head is "ocr_fast", so putting
    # ocr_fast back re-exposes Strata in ocr_large. Live, a one-pass restore
    # left ocr_large on Strata after the GPU had gone back to Ollama.
    for _ in range(2):
        registry = pa._registry()
        # A slot the operator re-assigned since the switch is their decision now.
        target = {
            slot: model
            for slot, model in old.items()
            if pa._slot_current_model(slot, registry) == STRATA_MODEL_KEY
        }
        if not target:
            break
        await _apply_slots(db, user, target)
        restored.update(target)
    strata_manager.set_switch_revision(None)
    return sorted(restored)


@router.post("/install", dependencies=_admin)
async def strata_install() -> dict:
    try:
        await strata_manager.install_strata()
    except strata_manager.StrataError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"ok": True, "status": await strata_manager.status()}


@router.put("/config", dependencies=_admin)
async def strata_config(body: ConfigIn) -> dict:
    desired = strata_manager.read_desired()
    try:
        strata_manager.check_quant_fits_disk(body.model)
        # Context and images are recorded in a quant's run config at setup:
        # an installed quant needs a setup pass (no new download) to change them.
        installed = body.model in strata_manager.installed_quants()
        settings_changed = (body.context, body.vision) != (desired["context"], desired["vision"])
        strata_manager.write_desired(
            model=body.model,
            context=body.context,
            vision=body.vision,
            reinstall=installed and settings_changed or desired["reinstall_pending"],
        )
        restarted = await strata_manager.restart_if_running()
    except strata_manager.StrataError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"ok": True, "restarted": restarted, "status": await strata_manager.status()}


class RuntimeIn(BaseModel):
    idle_unload_s: int
    free_comfyui: bool = True


@router.put("/runtime", dependencies=_admin)
async def strata_runtime(body: RuntimeIn) -> dict:
    """Idle unload / ComfyUI hand-off. Applied by a restart (Strata reads them at start)."""
    try:
        strata_manager.write_runtime(
            idle_unload_s=body.idle_unload_s, free_comfyui=body.free_comfyui
        )
        strata_manager.apply_runtime_keys()
        restarted = await strata_manager.restart_if_running()
    except strata_manager.StrataError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"ok": True, "restarted": restarted, "status": await strata_manager.status()}


@router.get("/access", dependencies=_admin)
async def strata_access() -> dict:
    """What another machine on the LAN needs: the published address and the key."""
    from app.config import settings

    base = (settings.strata_public_url or "").rstrip("/")
    return {
        "url": base or None,
        "openai_base_url": f"{base}/v1" if base else None,
        "api_key": settings.strata_api_key or None,
    }


@router.get("/logs", dependencies=_admin)
async def strata_logs(lines: int = 200) -> dict:
    try:
        return {"lines": await strata_manager.log_tail(max(10, min(lines, 2000)))}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, str(exc)[:300]) from exc
