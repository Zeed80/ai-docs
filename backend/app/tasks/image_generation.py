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


@celery_app.task(
    bind=True,
    name="image_generation.run_image_generation",
    max_retries=3,
    soft_time_limit=600,   # 10 min — diffusion on a busy GPU can be slow
    time_limit=660,
)
def run_image_generation(self, generation_id: str) -> dict:
    from app.ai.comfyui_client import ComfyUITransientError

    try:
        return run_async(_run(generation_id, self.request.id))
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
        if wf:
            return wf
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
    from app.services import push

    factory = _get_session_factory()
    async with factory() as db:
        gen = await db.get(ImageGeneration, gen_uuid)
        if not gen:
            return
        gen.status = ImageGenStatus.failed
        gen.error = err[:2000]
        await db.commit()
        target_owner = owner_sub or gen.owner_sub
        if target_owner:
            try:
                await push.push_to_user(
                    db=db,
                    user_sub=target_owner,
                    title="Ошибка генерации изображения",
                    body=err[:200],
                    action_url=f"/studio?id={gen_uuid}",
                    notification_type="image_failed",
                )
            except Exception:  # noqa: BLE001
                pass


async def _run(generation_id: str, task_id: str | None) -> dict:
    from app.ai.comfyui_client import (
        ComfyUIClient,
        ComfyUIError,
        ComfyUITransientError,
        build_workflow,
    )
    from app.db.models import ImageGeneration, ImageGenStatus
    from app.db.session import _get_session_factory
    from app.storage import download_file, upload_file

    factory = _get_session_factory()
    gen_uuid = uuid.UUID(generation_id)

    # ── Load job + mark running ──────────────────────────────────────────────
    async with factory() as db:
        gen = await db.get(ImageGeneration, gen_uuid)
        if not gen:
            return {"error": "generation not found"}
        wf = await _resolve_workflow(db, gen)
        if not wf:
            gen.status = ImageGenStatus.failed
            gen.error = "Не найден воркфлоу для операции."
            await db.commit()
            return {"error": "no workflow"}

        gen.status = ImageGenStatus.running
        gen.celery_task_id = task_id
        await db.commit()

        owner_sub = gen.owner_sub
        operation = gen.operation
        prompt = gen.prompt
        negative = gen.negative_prompt
        params = dict(gen.params or {})
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

        uploaded: list[str] = []
        for idx, path in enumerate(source_paths):
            content = download_file(path)
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
        for key in ("width", "height", "steps", "cfg", "denoise"):
            if params.get(key) is not None:
                values[key] = params[key]
        if params.get("controlnet_strength") is not None:
            values["controlnet_strength"] = params["controlnet_strength"]

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

        prompt_id = await client.queue_workflow(graph)
        outputs = await client.wait_for_result(prompt_id)
        result_bytes = await client.fetch_image(outputs[0])

        # Text preservation (edit/cleanup only): diffusion garbles existing
        # dimension/label text on every pass (confirmed live, with or without
        # ControlNet) — paste the original ink back at its OCR-detected
        # location instead of trusting the model to reproduce it. Best-effort:
        # never fail the generation over this.
        if operation in ("edit", "cleanup") and source_paths:
            try:
                from app.ai.text_preserve import composite_text_regions, detect_text_regions

                source_for_ocr = download_file(source_paths[0])
                regions = detect_text_regions(source_for_ocr)
                if regions:
                    from PIL import Image as _PILImage

                    src_w, src_h = _PILImage.open(io.BytesIO(source_for_ocr)).size
                    result_bytes = composite_text_regions(
                        result_bytes, source_for_ocr, regions, src_w, src_h
                    )
                    logger.info(
                        "text_preserve_applied", generation_id=generation_id, regions=len(regions)
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning("text_preserve_failed", generation_id=generation_id, error=str(exc))

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
        err = str(exc) if isinstance(exc, ComfyUIError) else f"{type(exc).__name__}: {exc}"
        logger.warning("image_gen_failed", generation_id=generation_id, error=err)
        await _mark_failed(gen_uuid, err, owner_sub)
        return {"error": err}

    # ── Persist result + notify ──────────────────────────────────────────────
    from app.services import push

    async with factory() as db:
        gen = await db.get(ImageGeneration, gen_uuid)
        if gen:
            gen.status = ImageGenStatus.done
            gen.result_path = result_path
            gen.thumbnail_path = thumb_path
            gen.comfyui_prompt_id = prompt_id
            await db.commit()
            if owner_sub:
                try:
                    await push.push_to_user(
                        db=db,
                        user_sub=owner_sub,
                        title="Изображение готово",
                        body="Результат доступен в Графической студии.",
                        action_url=f"/studio?id={generation_id}",
                        notification_type="image_ready",
                    )
                except Exception:  # noqa: BLE001
                    pass

    return {"ok": True, "generation_id": generation_id, "result_path": result_path}
