"""Celery task: run one ComfyUI image generation/edit job end-to-end.

Flow (mirrors the drawing_analysis task shape — async body via run_async):
    record → running
    upload source/mask images to ComfyUI
    build graph from the workflow template (inject prompt/image/mask/seed)
    queue → poll → fetch result
    store result + thumbnail in MinIO
    record → done, push "готово" (or → failed, push error)

Generation is draft-first: the result is a version node the human keeps or
re-iterates; there is no approval gate.
"""

from __future__ import annotations

import io
import random
import uuid

import structlog

from app.tasks.async_runner import run_async
from app.tasks.celery_app import celery_app

logger = structlog.get_logger()

_RESULT_BUCKET_PREFIX = "image-gen"


class StudioResourceBusy(RuntimeError):
    """A shared studio resource is busy; retry without marking the job failed."""


def _progress_key(gen_id: str) -> str:
    return f"studio:progress:{gen_id}"


def _write_progress(gen_id: str, value, maximum, node=None) -> None:
    """Store live sampling progress in Redis (TTL 3m) for the UI to poll —
    lighter than a DB write per step. Best-effort."""
    import json

    try:
        from app.utils.redis_client import get_sync_redis

        pct = int(round(100 * value / maximum)) if value and maximum else 0
        get_sync_redis().set(
            _progress_key(gen_id),
            json.dumps({"value": value, "max": maximum, "pct": pct,
                        "node": node, "ts": __import__("time").time()}),
            ex=180,
        )
    except Exception:  # noqa: BLE001
        pass


def _clear_progress(gen_id: str) -> None:
    try:
        from app.utils.redis_client import get_sync_redis

        get_sync_redis().delete(_progress_key(gen_id))
    except Exception:  # noqa: BLE001
        pass


def read_progress(gen_id: str) -> dict | None:
    """Used by the API's _gen_out to surface progress for a running gen."""
    import json

    try:
        from app.utils.redis_client import get_sync_redis

        raw = get_sync_redis().get(_progress_key(gen_id))
        return json.loads(raw) if raw else None
    except Exception:  # noqa: BLE001
        return None

# Applied to every diffusion prompt (generate/edit/cleanup/inpaint — everything
# that isn't the deterministic `techdraw` path). Two things diffusion won't do
# on its own: (1) draw in ЕСКД line conventions rather than a generic "blueprint"
# look, (2) leave off a sheet frame/title block — which it can't render legibly
# anyway (see text_preserve.py: diffusion garbles text every time), so asking it
# not to attempt one avoids wasted/corrupted content instead of just tolerating it.
_ESKD_STYLE_SUFFIX = (
    ", технический чертёж по ЕСКД: чёрно-белая линейная графика на белом фоне, "
    "сплошные основные линии контура, тонкие сплошные линии для размеров, "
    "штрихпунктирные осевые и центровые линии, штриховка сечений под 45°, "
    "без рамки листа, без углового штампа, без основной надписи, без таблицы"
)
_ESKD_NEGATIVE_SUFFIX = (
    "рамка листа, угловой штамп, основная надпись, таблица спецификации, "
    "цветной фон, размытие, водяной знак"
)


def _apply_eskd_style(prompt: str | None, negative: str | None) -> tuple[str, str]:
    p = (prompt or "").strip()
    n = (negative or "").strip()
    style_marker = "технический чертёж по ЕСКД"
    negative_marker = "рамка листа"
    if p and style_marker not in p:
        p = f"{p}{_ESKD_STYLE_SUFFIX}"
    elif not p:
        p = _ESKD_STYLE_SUFFIX.lstrip(", ")
    if n and negative_marker not in n:
        n = f"{n}, {_ESKD_NEGATIVE_SUFFIX}"
    elif not n:
        n = _ESKD_NEGATIVE_SUFFIX
    return p, n


# "fast" (default, unchanged): Lightning-4steps LoRA at full strength, cfg=1
# — the values every builtin edit/cleanup/inpaint workflow already ships
# with. "quality": no Lightning LoRA (strength 0 — LoraLoaderModelOnly at 0
# is a no-op passthrough of the base model, no graph restructuring needed),
# real step count and CFG. Measured live across 6+ same-instruction runs at
# different seeds ("remove this chamfer" on a real shaft drawing): "fast"
# never once performed the requested edit (0/6); "quality" performed it in
# roughly half its runs. Diffusion sampling isn't seed-deterministic on this
# server even at a fixed seed, so "quality" meaningfully raises the odds of
# the model actually doing what was asked — it does not guarantee it. Not
# worth defaulting on for every request given the ~7-8x time cost; exposed
# as an explicit studio toggle instead.
_QUALITY_PRESETS: dict[str, dict[str, float]] = {
    "fast": {"steps": 4, "cfg": 1.0, "lora_strength": 1.0},
    "quality": {"steps": 25, "cfg": 3.0, "lora_strength": 0.0},
}


def _apply_quality_preset(values: dict, quality: str | None) -> None:
    """Fill in steps/cfg/lora_strength from the named preset — but never
    override a value the caller already set explicitly (a user-chosen
    numeric steps/cfg/lora_strength always wins over the named preset)."""
    preset = _QUALITY_PRESETS.get(quality or "")
    if not preset:
        return
    for key, val in preset.items():
        values.setdefault(key, val)


@celery_app.task(
    bind=True,
    name="image_generation.run_image_generation",
    max_retries=3,
    soft_time_limit=2400,  # HD tiled cleanup runs 4-6 diffusions (~10-20 min)
    time_limit=2460,
)
def run_image_generation(self, generation_id: str) -> dict:
    from app.ai.comfyui_client import ComfyUITransientError

    try:
        return run_async(_run(generation_id, self.request.id))
    except StudioResourceBusy as exc:
        backoff = 90 + random.randint(0, 30)
        logger.info(
            "image_gen_waiting_resource_retry",
            generation_id=generation_id,
            retry=self.request.retries + 1,
            countdown=backoff,
            error=str(exc),
        )
        raise self.retry(exc=exc, countdown=backoff, max_retries=None)
    except ComfyUITransientError as exc:
        if self.request.retries < self.max_retries:
            backoff = min(120, (2**self.request.retries) * 10) + random.randint(0, 5)
            logger.warning(
                "image_gen_transient_retry",
                generation_id=generation_id,
                retry=self.request.retries + 1,
                countdown=backoff,
                error=str(exc),
            )
            raise self.retry(exc=exc, countdown=backoff)
        logger.warning("image_gen_retries_exhausted", generation_id=generation_id, error=str(exc))
        run_async(
            _mark_failed(
                uuid.UUID(generation_id),
                f"ComfyUI недоступен после нескольких попыток: {exc}",
            )
        )
        return {"error": str(exc)}


def _pick_upscale_model(object_info: dict) -> str | None:
    """Choose an upscale model available on the node. Prefer a sharp
    line-art-friendly one (UltraSharp / 4x) — ideal for technical drawings."""
    try:
        mn = object_info["UpscaleModelLoader"]["input"]["required"]["model_name"]
        # ComfyUI COMBO shapes vary by version: [[opt, ...], {...}] (older) or
        # ["COMBO", {"options": [...]}] (newer).
        if mn and isinstance(mn[0], list):
            models = mn[0]
        elif len(mn) > 1 and isinstance(mn[1], dict):
            models = mn[1].get("options") or []
        else:
            models = []
    except Exception:  # noqa: BLE001
        return None
    if not models:
        return None
    for pref in ("ultrasharp", "4x", "esrgan"):
        for m in models:
            if pref in m.lower():
                return m
    return models[0]


async def _run_upscale(client, image_bytes: bytes, factor: int,
                       object_info: dict, generation_id: str) -> bytes:
    """Model-based high-quality upscale of the FINAL result — mode-agnostic
    (runs on the produced image, so it works for generate/edit/cleanup/
    inpaint/techdraw alike). The ESRGAN model upscales at its native factor
    (usually 4x); a Lanczos ImageScale then hits the exact requested factor.
    Best-effort: any failure keeps the un-upscaled result."""
    from PIL import Image as _PILImage

    model_name = _pick_upscale_model(object_info)
    if not model_name:
        logger.warning("upscale_no_model", generation_id=generation_id)
        return image_bytes

    w, h = _PILImage.open(io.BytesIO(image_bytes)).size
    target_w, target_h = w * factor, h * factor

    _write_progress(generation_id, None, None, node="upscale")
    name = await client.upload_image(image_bytes, f"upscale_{generation_id}.png")
    graph = {
        "1": {"class_type": "LoadImage", "inputs": {"image": name}},
        "2": {"class_type": "UpscaleModelLoader", "inputs": {"model_name": model_name}},
        "3": {"class_type": "ImageUpscaleWithModel",
              "inputs": {"upscale_model": ["2", 0], "image": ["1", 0]}},
        # Lanczos-resize the model output to the EXACT requested size (the
        # model's native factor may be 4x; this lands 2x/3x precisely and
        # trims any rounding).
        "4": {"class_type": "ImageScale",
              "inputs": {"image": ["3", 0], "upscale_method": "lanczos",
                         "width": target_w, "height": target_h, "crop": "disabled"}},
        "5": {"class_type": "SaveImage",
              "inputs": {"images": ["4", 0], "filename_prefix": "upscaled"}},
    }
    prompt_id = await client.queue_workflow(graph)
    outputs = await client.wait_for_result(prompt_id)
    upscaled = await client.fetch_image(outputs[0])
    logger.info("upscaled", generation_id=generation_id, model=model_name,
                factor=factor, from_size=[w, h], to=[target_w, target_h])
    return upscaled


def _make_thumbnail(content: bytes, max_px: int = 480) -> bytes | None:
    try:
        from PIL import Image

        img = Image.open(io.BytesIO(content))
        img = img.convert("RGB")
        img.thumbnail((max_px, max_px))
        out = io.BytesIO()
        img.save(out, format="PNG")
        return out.getvalue()
    except Exception as exc:  # noqa: BLE001
        logger.warning("image_gen_thumbnail_failed", error=str(exc))
        return None


async def _resolve_workflow(db, gen):
    """Return the ComfyWorkflow for this job (pinned id, else enabled builtin)."""
    from sqlalchemy import select

    from app.db.models import ComfyWorkflow

    if gen.workflow_id:
        wf = await db.get(ComfyWorkflow, gen.workflow_id)
        if wf and (wf.is_builtin or wf.owner_sub in (None, gen.owner_sub)):
            return wf
        return None
    # Fall back to the first enabled workflow matching the operation.
    row = (
        await db.execute(
            select(ComfyWorkflow)
            .where(
                ComfyWorkflow.operation == gen.operation,
                ComfyWorkflow.enabled.is_(True),
            )
            .order_by(ComfyWorkflow.is_builtin.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return row


async def _mark_failed(gen_uuid: uuid.UUID, err: str, owner_sub: str | None = None) -> None:
    """Persist a final (non-retryable) failure + best-effort push notification."""
    from app.db.models import ImageGeneration, ImageGenStatus
    from app.db.session import _get_session_factory
    from app.services import studio_queue
    from app.services import push

    factory = _get_session_factory()
    async with factory() as db:
        gen = await db.get(ImageGeneration, gen_uuid)
        if not gen:
            return
        gen.status = ImageGenStatus.failed
        gen.error = err[:2000]
        job = await studio_queue.job_for_generation(db, gen_uuid)
        await studio_queue.mark_job_failed(db, job, error=err)
        await db.commit()
        _clear_progress(str(gen_uuid))
        target_owner = owner_sub or gen.owner_sub
        if target_owner:
            try:
                await push.push_to_user(
                    db=db,
                    user_sub=target_owner,
                    title="Ошибка генерации изображения",
                    body=err[:200],
                    action_url=f"/studio?job={job.id}" if job else f"/studio?id={gen_uuid}",
                    notification_type="image_failed",
                )
            except Exception:  # noqa: BLE001
                pass


_HD_MAX_LONG_SIDE = 2600  # ~4 tiles for A4/A3 sheets; keeps runtime sane


async def _is_cancelled(gen_uuid: uuid.UUID) -> bool:
    from app.db.models import ImageGeneration, ImageGenStatus, StudioJobStatus
    from app.db.session import _get_session_factory
    from app.services import studio_queue

    factory = _get_session_factory()
    async with factory() as db:
        gen = await db.get(ImageGeneration, gen_uuid)
        if gen and gen.status == ImageGenStatus.cancelled:
            return True
        job = await studio_queue.job_for_generation(db, gen_uuid)
        return bool(job and job.status in {StudioJobStatus.cancel_requested, StudioJobStatus.cancelled})


async def _mark_cancelled(gen_uuid: uuid.UUID, reason: str = "Задача отменена пользователем.") -> None:
    from app.db.models import ImageGeneration, ImageGenStatus
    from app.db.session import _get_session_factory
    from app.services import studio_queue

    factory = _get_session_factory()
    async with factory() as db:
        gen = await db.get(ImageGeneration, gen_uuid)
        if gen:
            gen.status = ImageGenStatus.cancelled
            gen.error = reason
        job = await studio_queue.job_for_generation(db, gen_uuid)
        await studio_queue.mark_job_cancelled(db, job, error=reason)
        await db.commit()
        _clear_progress(str(gen_uuid))


async def _run_hd_tiles(client, graph_template: dict, inject_map: dict, values: dict,
                        source_bytes: bytes, object_info: dict,
                        generation_id: str) -> tuple[bytes, str]:
    """Upscale the sheet, diffuse it tile by tile through the SAME workflow
    graph and stitch with seam blending. Returns (png_bytes, last_prompt_id)."""
    import io as _io

    from PIL import Image as _PILImage

    from app.ai.comfyui_models import auto_resolve_models
    from app.ai.comfyui_client import build_workflow
    from app.ai.hd_tiles import split_tiles, stitch_tiles

    src = _PILImage.open(_io.BytesIO(source_bytes)).convert("RGB")
    scale = min(2.0, _HD_MAX_LONG_SIDE / max(src.size))
    if scale > 1.0:
        src = src.resize((round(src.width * scale), round(src.height * scale)),
                         _PILImage.LANCZOS)
    width, height = src.size
    boxes = split_tiles(width, height)
    logger.info("hd_tiles_start", generation_id=generation_id,
                size=[width, height], tiles=len(boxes))

    rendered = []
    prompt_id = ""
    for idx, box in enumerate(boxes):
        crop = src.crop(box)
        buf = _io.BytesIO()
        crop.save(buf, format="PNG")
        tile_name = await client.upload_image(buf.getvalue(),
                                              f"hd_{generation_id}_{idx}.png")
        tile_values = dict(values)
        tile_values["image"] = tile_name
        graph = build_workflow(graph_template, inject_map, tile_values)
        graph, missing = auto_resolve_models(graph, object_info)
        if missing:
            raise RuntimeError(f"HD: не хватает моделей: {missing}")
        prompt_id = await client.queue_workflow(graph)
        outputs = await client.wait_for_result(prompt_id)
        tile_png = await client.fetch_image(outputs[0])
        rendered.append((box, _PILImage.open(_io.BytesIO(tile_png))))
        logger.info("hd_tile_done", generation_id=generation_id,
                    tile=idx + 1, total=len(boxes))

    stitched = stitch_tiles(width, height, rendered)
    out = _io.BytesIO()
    stitched.save(out, format="PNG")
    return out.getvalue(), prompt_id


async def _run(generation_id: str, task_id: str | None) -> dict:
    from app.ai.comfyui_client import (
        ComfyUIClient,
        ComfyUIError,
        ComfyUITransientError,
        build_workflow,
    )
    from app.db.models import ImageGeneration, ImageGenStatus
    from app.db.session import _get_session_factory
    from app.services import studio_queue
    from app.storage import download_file, upload_file

    factory = _get_session_factory()
    gen_uuid = uuid.UUID(generation_id)

    # ── Load job + mark running ──────────────────────────────────────────────
    async with factory() as db:
        gen = await db.get(ImageGeneration, gen_uuid)
        if not gen:
            return {"error": "generation not found"}
        job = await studio_queue.job_for_generation(db, gen_uuid)
        job_status = job.status.value if job and hasattr(job.status, "value") else (str(job.status) if job else None)
        if gen.status == ImageGenStatus.cancelled or (
            job_status in {"cancel_requested", "cancelled"}
        ):
            await _mark_cancelled(gen_uuid)
            return {"cancelled": True}
        wf = await _resolve_workflow(db, gen)
        if not wf:
            gen.status = ImageGenStatus.failed
            gen.error = "Не найден воркфлоу для операции."
            await studio_queue.mark_job_failed(db, job, error=gen.error)
            await db.commit()
            return {"error": "no workflow"}

        try:
            from app.ai import gpu_lock

            if gpu_lock.is_locked():
                gen.status = ImageGenStatus.queued
                await studio_queue.mark_job_waiting(db, job, reason=gpu_lock.LOCK_MESSAGE)
                await db.commit()
                raise StudioResourceBusy(gpu_lock.LOCK_MESSAGE)
        except StudioResourceBusy:
            raise
        except Exception:  # noqa: BLE001 — Redis hiccup must not block generation
            pass

        gen.status = ImageGenStatus.running
        gen.celery_task_id = task_id
        gen.workflow_snapshot = {
            "id": str(wf.id),
            "key": wf.key,
            "title": wf.title,
            "operation": wf.operation,
            "base_family": wf.base_family,
            "is_builtin": wf.is_builtin,
            "graph": wf.graph or {},
            "inject_map": wf.inject_map or {},
            "params_schema": wf.params_schema or {},
        }
        await studio_queue.mark_job_running(db, job, task_id=task_id)
        await db.commit()

        owner_sub = gen.owner_sub
        operation = gen.operation
        prompt = gen.prompt
        negative = gen.negative_prompt
        params = dict(gen.params or {})
        # Custom workflows (trained-LoRA clones) ship their own tuned
        # steps/cfg/strengths — the generic fast/quality preset would
        # override them (confirmed live: "Быстро" re-enabled the Lightning
        # LoRA node in a v2-LoRA clone via the inherited inject_map).
        if not wf.is_builtin:
            params.pop("quality", None)
        # Workflow-declared parameter defaults (params_schema.*.default) fill
        # anything the caller didn't set — this is how custom workflows (e.g.
        # trained-LoRA clones) carry their own tuned behaviour, like
        # postprocess="text_only", without frontend changes.
        for key, spec in (wf.params_schema or {}).items():
            if isinstance(spec, dict) and "default" in spec:
                params.setdefault(key, spec["default"])
        source_paths = list(gen.source_image_paths or [])
        mask_path = gen.mask_path
        graph_template = dict(wf.graph or {})
        inject_map = dict(wf.inject_map or {})

    # ── Run on ComfyUI ───────────────────────────────────────────────────────
    try:
        client = ComfyUIClient.from_registry()
        if not await client.health():
            raise ComfyUITransientError(
                "ComfyUI сервер сейчас недоступен. Попробуйте ещё раз через минуту "
                "или обратитесь к администратору."
            )

        # Ollama and ComfyUI share the one GPU. A resident LLM (Ollama keeps a
        # model warm after agent/OCR calls — ~20GB for gemma4:31b) leaves no
        # room for the diffusion model and the run OOMs. Evict Ollama weights
        # first; they reload on the next inference request. Best-effort.
        try:
            from app.ai import gpu_lock

            gpu_lock.unload_ollama()
        except Exception:  # noqa: BLE001
            pass

        uploaded: list[str] = []
        enhanced_source: bytes | None = None
        for idx, path in enumerate(source_paths):
            content = download_file(path)
            if operation == "cleanup":
                # Give diffusion a better-conditioned starting point for a
                # poor-quality photo (dewarp/deskew/denoise/contrast) —
                # classical CV, not diffusion, is what can actually promise
                # this. See drawing_cleanup.py for why the split exists at all.
                try:
                    from app.ai.drawing_cleanup import enhance_source_for_diffusion

                    content = enhance_source_for_diffusion(content)
                except Exception as exc:  # noqa: BLE001 — best-effort
                    logger.warning("enhance_source_failed", generation_id=generation_id, error=str(exc))
                if idx == 0:
                    # Text preservation below must OCR THIS image, not the raw
                    # original: dewarp/deskew change the geometry, so boxes
                    # computed against the original would land in the wrong
                    # place on the diffusion output.
                    enhanced_source = content
            server_name = await client.upload_image(content, f"src_{generation_id}_{idx}.png")
            uploaded.append(server_name)

        mask_name = None
        if mask_path:
            mask_content = download_file(mask_path)
            mask_name = await client.upload_image(mask_content, f"mask_{generation_id}.png")

        # ControlNet conditioning image (optional): only for workflows that
        # declare it in inject_map. Preprocessing (canny) happens here, not as
        # a ComfyUI custom node — see drawing_preprocessor.canny_edge_map.
        controlnet_name = None
        if "controlnet_image" in inject_map and uploaded:
            from app.ai.drawing_preprocessor import canny_edge_map

            source_content = download_file(source_paths[0])
            edge_png = canny_edge_map(source_content)
            controlnet_name = await client.upload_image(edge_png, f"controlnet_{generation_id}.png")

        seed = params.get("seed")
        if not seed:  # 0 / None → random so repeated runs differ
            seed = random.randint(1, 2**31 - 1)

        if operation in ("generate", "eskd"):
            # "generate"/"eskd" build a drawing from nothing, where a broad
            # style paragraph is the bulk of what the model has to go on —
            # for edit/inpaint the user's instruction is inherently narrow
            # and specific ("remove this chamfer"), and confirmed live: the
            # appended style paragraph measurably drowns it out (steps=25,
            # cfg=3 with no Lightning LoRA still failed to perform the edit
            # once the suffix was appended, but succeeded on the same seed
            # with the bare instruction). Cleanup's prompt is baked into its
            # own workflow template and never goes through this at all.
            prompt, negative = _apply_eskd_style(prompt, negative)

        values: dict = {
            "prompt": prompt,
            "negative": negative,
            "seed": seed,
        }
        if uploaded:
            values["image"] = uploaded[0]
        if mask_name:
            values["mask"] = mask_name
        if controlnet_name:
            values["controlnet_image"] = controlnet_name
        for key in ("width", "height", "steps", "cfg", "denoise",
                    "lora_strength", "guidance"):
            if params.get(key) is not None:
                values[key] = params[key]
        if params.get("controlnet_strength") is not None:
            values["controlnet_strength"] = params["controlnet_strength"]

        _apply_quality_preset(values, params.get("quality"))

        graph = build_workflow(graph_template, inject_map, values)

        # Make the graph runnable on this server: swap in installed model files
        # for any preferred names that aren't present. Fail clearly if a required
        # model is missing (so the user knows what to download in settings).
        from app.ai.comfyui_models import auto_resolve_models

        object_info = await client.object_info()
        graph, missing = auto_resolve_models(graph, object_info)
        if missing:
            names = ", ".join(f"{m.requested} ({m.category})" for m in missing)
            raise ComfyUIError(
                "На сервере ComfyUI не хватает моделей: "
                f"{names}. Скачайте их в Настройки → ComfyUI → Модели."
            )

        if params.get("hd") and operation == "cleanup" and uploaded:
            # HD mode: 2x upscale + per-tile diffusion + seam-blended stitch.
            # Small text/thin lines become 2x bigger for the model — detail
            # no single ~1MP pass can render (VAE 8x latent compression).
            hd_source = enhanced_source or download_file(source_paths[0])
            result_bytes, prompt_id = await _run_hd_tiles(
                client, graph_template, inject_map, dict(values), hd_source,
                object_info, generation_id,
            )
        else:
            import asyncio as _asyncio

            prompt_id = await client.queue_workflow(graph)
            async with factory() as db:
                gen = await db.get(ImageGeneration, gen_uuid)
                if gen:
                    gen.comfyui_prompt_id = prompt_id
                    await db.commit()
            # Live progress via WS runs alongside the authoritative HTTP poll.
            progress_task = _asyncio.create_task(
                client.stream_progress(
                    prompt_id,
                    lambda p: _write_progress(generation_id, p["value"], p["max"], p["node"]),
                )
            )
            try:
                outputs = await client.wait_for_result(prompt_id)
            finally:
                progress_task.cancel()
            result_bytes = await client.fetch_image(outputs[0])

        if await _is_cancelled(gen_uuid):
            await _mark_cancelled(gen_uuid)
            return {"cancelled": True}

        # Size reconciliation (image-to-image ops): FluxKontextImageScale
        # snaps the input to the model's resolution buckets, so the output
        # comes back at a DIFFERENT size and aspect (+~4% measured live) —
        # the user sees a "cropped" result next to their source, and every
        # proportional mapping downstream (text paste) drifts. Resize the
        # output back to the (enhanced) source frame; Flux stretches rather
        # than crops, so the inverse resize restores geometry 1:1.
        if operation in ("cleanup", "edit", "inpaint") and (enhanced_source or source_paths):
            try:
                from PIL import Image as _PILImage

                ref_bytes = enhanced_source or download_file(source_paths[0])
                ref_w, ref_h = _PILImage.open(io.BytesIO(ref_bytes)).size
                out_img = _PILImage.open(io.BytesIO(result_bytes))
                # Match the source's ASPECT at the diffusion's own resolution
                # (never downscale to a small source: that visibly degraded a
                # dense sheet — the user compared against raw ComfyUI).
                scale = max(max(out_img.size) / max(ref_w, ref_h), 1.0)
                target = (round(ref_w * scale), round(ref_h * scale))
                if out_img.size != target:
                    resized = out_img.convert("RGB").resize(target, _PILImage.LANCZOS)
                    buf = io.BytesIO()
                    resized.save(buf, format="PNG")
                    result_bytes = buf.getvalue()
                    logger.info("result_resized_to_source", generation_id=generation_id,
                                out=list(out_img.size), target=list(target))
            except Exception as exc:  # noqa: BLE001 — best-effort
                logger.warning("result_resize_failed", generation_id=generation_id,
                               error=str(exc))

        # Affine layout estimation (cleanup only): diffusion re-layouts the
        # sheet slightly, while the proportional text paste below assumes the
        # output's layout matches the source's. Estimate the drift and AIM
        # the pastes through it — never warp the clean result itself (that
        # drags it onto the source photo's residual tilt; confirmed live:
        # straight windows became parallelograms). Best-effort: None keeps
        # plain proportional mapping.
        source_to_result = None
        _pp = str(params.get("postprocess") or "none")
        if operation == "cleanup" and enhanced_source and _pp != "none":
            try:
                from app.ai.image_align import estimate_source_to_result

                source_to_result = estimate_source_to_result(result_bytes, enhanced_source)
            except Exception as exc:  # noqa: BLE001
                logger.warning("align_failed", generation_id=generation_id, error=str(exc))

        # Post-processing mode. "none" (DEFAULT) = the raw ComfyUI result,
        # untouched — the safe default: on real busy sheets the classic
        # pipeline (binarize+vectorize) and the OCR text-paste add more
        # artifacts than they remove (user-confirmed: "куча мусора"). "full" =
        # binarize + vector reconstruction. "text_only" = gentle autocontrast
        # + text paste (trained-LoRA workflows opt into this via their
        # params_schema default, which survives because the composer only
        # sends postprocess when the user explicitly overrides "auto").
        postprocess = str(params.get("postprocess") or "none")
        if operation == "cleanup" and postprocess == "full":
            try:
                from app.ai.drawing_cleanup import regularize_technical_drawing

                result_bytes = regularize_technical_drawing(
                    result_bytes, vectorize=params.get("vectorize") is not False
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("regularize_drawing_failed", generation_id=generation_id, error=str(exc))
        elif operation == "cleanup" and postprocess == "text_only":
            try:
                from PIL import Image as _PILImage
                from PIL import ImageOps as _PILImageOps

                img = _PILImage.open(io.BytesIO(result_bytes)).convert("RGB")
                img = _PILImageOps.autocontrast(img, cutoff=1)
                buf = io.BytesIO()
                img.save(buf, format="PNG")
                result_bytes = buf.getvalue()
            except Exception as exc:  # noqa: BLE001
                logger.warning("autocontrast_failed", generation_id=generation_id, error=str(exc))

        # Text preservation (edit/cleanup only): diffusion garbles existing
        # dimension/label text on every pass (confirmed live, with or without
        # ControlNet) — paste the original ink back at its OCR-detected
        # location instead of trusting the model to reproduce it. Best-effort:
        # never fail the generation over this.
        if operation in ("edit", "cleanup") and source_paths and postprocess != "none":
            try:
                from app.ai.text_preserve import composite_text_regions, detect_text_regions

                source_for_ocr = enhanced_source or download_file(source_paths[0])
                regions = detect_text_regions(source_for_ocr)
                if regions:
                    from PIL import Image as _PILImage

                    src_w, src_h = _PILImage.open(io.BytesIO(source_for_ocr)).size
                    result_bytes = composite_text_regions(
                        result_bytes, source_for_ocr, regions, src_w, src_h,
                        # Always crisp for cleanup: soft photo-toned pastes on
                        # a dense sheet read as dozens of dirty gray patches
                        # (user-confirmed "франкенштейн"); after autocontrast
                        # the background is near-white, so black-on-white
                        # pastes blend fine in text_only too.
                        binarize_ink=(operation == "cleanup"),
                        source_to_result=source_to_result,
                    )
                    logger.info(
                        "text_preserve_applied", generation_id=generation_id, regions=len(regions)
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning("text_preserve_failed", generation_id=generation_id, error=str(exc))

        # High-quality upscale of the final image (any mode). Applied last so
        # it enlarges the fully post-processed result. best-effort.
        try:
            factor = int(params.get("upscale", 1) or 1)
        except (TypeError, ValueError):
            factor = 1
        if factor > 1:
            try:
                result_bytes = await _run_upscale(
                    client, result_bytes, min(factor, 4), object_info, generation_id)
            except Exception as exc:  # noqa: BLE001 — keep the base result
                logger.warning("upscale_failed", generation_id=generation_id,
                               error=str(exc)[:200])

        result_path = f"{_RESULT_BUCKET_PREFIX}/{owner_sub or 'shared'}/{generation_id}.png"
        upload_file(result_bytes, result_path, "image/png")

        thumb_path = None
        thumb_bytes = _make_thumbnail(result_bytes)
        if thumb_bytes:
            thumb_path = f"{_RESULT_BUCKET_PREFIX}/{owner_sub or 'shared'}/{generation_id}_thumb.png"
            upload_file(thumb_bytes, thumb_path, "image/png")

    except ComfyUITransientError:
        # Node unreachable right now — let the Celery task wrapper decide
        # retry-vs-give-up; do NOT mark the record failed yet (it may still
        # succeed on the next attempt).
        raise
    except Exception as exc:  # noqa: BLE001
        if await _is_cancelled(gen_uuid):
            await _mark_cancelled(gen_uuid)
            return {"cancelled": True}
        err = str(exc) if isinstance(exc, ComfyUIError) else f"{type(exc).__name__}: {exc}"
        logger.warning("image_gen_failed", generation_id=generation_id, error=err)
        await _mark_failed(gen_uuid, err, owner_sub)
        return {"error": err}

    # ── Persist result + notify ──────────────────────────────────────────────
    from app.services import push

    async with factory() as db:
        gen = await db.get(ImageGeneration, gen_uuid)
        if gen:
            if gen.status == ImageGenStatus.cancelled:
                job = await studio_queue.job_for_generation(db, gen_uuid)
                await studio_queue.mark_job_cancelled(db, job, error="Задача отменена пользователем.")
                await db.commit()
                return {"cancelled": True}
            gen.status = ImageGenStatus.done
            gen.result_path = result_path
            gen.thumbnail_path = thumb_path
            gen.comfyui_prompt_id = prompt_id
            job = await studio_queue.job_for_generation(db, gen_uuid)
            await studio_queue.mark_job_done(db, job)
            await db.commit()
            _clear_progress(str(gen_uuid))
            if owner_sub:
                try:
                    await push.push_to_user(
                        db=db,
                        user_sub=owner_sub,
                        title="Изображение готово",
                        body="Результат доступен в Графической студии.",
                        action_url=f"/studio?job={job.id}" if job else f"/studio?id={generation_id}",
                        notification_type="image_ready",
                    )
                except Exception:  # noqa: BLE001
                    pass

    return {"ok": True, "generation_id": generation_id, "result_path": result_path}
