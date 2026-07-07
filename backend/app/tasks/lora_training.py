"""Celery tasks for the studio's LoRA feature: dataset preparation (CPU +
local VLM captions) and training-run supervision (dedicated trainer
container via docker SDK, live progress parsing, exclusive GPU lock).

Shared storage: ``settings.lora_data_dir`` (a compose volume mounted into
both the worker and the trainer containers) holds uploads, prepared datasets
and training outputs. MinIO keeps only UI artifacts (pair previews, training
samples) — the heavy data never leaves the volume.

Both tasks run on the dedicated ``lora`` queue (worker-lora, ``-c 1``) so a
48-hour training never occupies a general-purpose worker slot and two GPU
jobs are naturally serialized.

Preparation is RESUMABLE: every artifact (render, caption, pair) is skipped
when it already exists, so a retry after the 4h soft time limit finishes the
tail instead of re-burning hours of VLM captioning.
"""

from __future__ import annotations

import io
import json
import pathlib
import re
import time
import uuid
import zlib

import structlog

from app.tasks.async_runner import run_async
from app.tasks.celery_app import celery_app

logger = structlog.get_logger()

_PROGRESS_RE = re.compile(r"(\d+)/(\d+)\s+\[([\d:]+)<([\d:]+),.*?loss:\s*([\d.eE+-]+)")
_SAVE_RE = re.compile(r"Saving at step (\d+)")

# Every ~10th accepted pair goes to holdout/ (validation samples come from
# there, not from train data — the model must not have seen them). Keyed by
# NAME hash, not a counter, so resumed preparations assign identically.
_HOLDOUT_MOD = 10


def _data_dir() -> pathlib.Path:
    from app.config import settings

    return pathlib.Path(getattr(settings, "lora_data_dir", "/lora-data"))


_HEARTBEAT_TTL_S = 180


def _heartbeat_touch(key: str) -> None:
    from app.ai.gpu_lock import _redis

    _redis().set(key, "1", ex=_HEARTBEAT_TTL_S)


def _heartbeat_alive(key: str) -> bool:
    """Is another supervisor instance alive right now? Its refresher thread
    touches the key every 60s regardless of trainer log activity (DB
    updated_at is NOT a valid proxy: it freezes during silent phases like
    model quantization)."""
    try:
        from app.ai.gpu_lock import _redis

        return _redis().get(key) is not None
    except Exception:  # noqa: BLE001 — no Redis → assume dead, resume
        return False


def _name_seed(seed: int, stem: str, variant: int) -> int:
    """Deterministic per-image seed. zlib.crc32, NOT hash(): str hashes are
    salted per process, which silently broke dataset reproducibility."""
    return seed + zlib.crc32(stem.encode("utf-8")) % 10_000_000 + variant


def _is_holdout(name: str) -> bool:
    return zlib.crc32(f"holdout:{name}".encode("utf-8")) % _HOLDOUT_MOD == 0


# ── Dataset preparation ──────────────────────────────────────────────────────


@celery_app.task(name="lora.prepare_dataset", soft_time_limit=4 * 3600, time_limit=4 * 3600 + 60)
def prepare_dataset(dataset_id: str) -> dict:
    return run_async(_prepare(dataset_id))


def _resolve_sources(source_paths: list[str]) -> tuple[list[pathlib.Path], list[str]]:
    """Worker-side containment: sources may only live under the uploads/
    area of the shared volume (the API already restricts them to the owner's
    folder; this is the defense-in-depth re-check — the worker is the one
    actually reading files)."""
    uploads_root = (_data_dir() / "uploads").resolve()
    ok: list[pathlib.Path] = []
    rejected: list[str] = []
    for src in source_paths:
        p = pathlib.PurePosixPath(src)
        if p.is_absolute() or ".." in p.parts:
            rejected.append(f"{src}: недопустимый путь")
            continue
        resolved = (_data_dir() / src).resolve()
        if not resolved.is_relative_to(uploads_root):
            rejected.append(f"{src}: вне каталога загрузок")
            continue
        ok.append(resolved)
    return ok, rejected


async def _prepare(dataset_id: str) -> dict:
    import threading

    from app.ai import gpu_lock
    from app.ai import lora_dataset as core
    from app.config import settings
    from app.db.models import LoraDataset, LoraDatasetStatus
    from app.db.session import _get_session_factory
    from app.storage import upload_file

    factory = _get_session_factory()
    ds_uuid = uuid.UUID(dataset_id)
    async with factory() as db:
        ds = await db.get(LoraDataset, ds_uuid)
        if not ds:
            return {"error": "dataset not found"}
        params = dict(ds.params or {})
        source_paths = list(ds.source_paths or [])
        preset = ds.preset
        preset_instruction = params.get("instruction") or core.DEFAULT_INSTRUCTION

    root = _data_dir() / "datasets" / dataset_id
    targets_dir = root / "targets"
    controls_dir = root / "controls"
    images_dir = root / "images"
    control_dir = root / "control"
    holdout_images_dir = root / "holdout" / "images"
    holdout_control_dir = root / "holdout" / "control"
    targets_dir.mkdir(parents=True, exist_ok=True)
    controls_dir.mkdir(parents=True, exist_ok=True)

    # Liveness for the API watchdog: without it a worker restart leaves the
    # dataset spinning in "preparing" forever.
    hb_key = gpu_lock.dataset_heartbeat_key(dataset_id)
    _heartbeat_touch(hb_key)
    stop_hb = threading.Event()

    def _hb_refresher() -> None:
        while not stop_hb.wait(60):
            try:
                _heartbeat_touch(hb_key)
            except Exception:  # noqa: BLE001
                pass

    threading.Thread(target=_hb_refresher, daemon=True).start()

    stats: dict = {"sources": len(source_paths), "rendered": 0, "synthetic": 0,
                   "captioned": 0, "caption_rejected": 0, "pairs": 0,
                   "holdout": 0, "page_skipped": 0,
                   "pair_rejected": [], "render_failed": []}
    try:
        if preset == "drawing_edit":
            return await _prepare_edit_preset(
                factory, ds_uuid, root, targets_dir, controls_dir,
                images_dir, control_dir, holdout_images_dir, holdout_control_dir,
                params, stats,
            )
        if preset not in (None, "", "drawing_cleanup"):
            raise ValueError(f"Пресет «{preset}» пока не поддерживается "
                             "(доступны: drawing_cleanup, drawing_edit).")

        # ── drawing_cleanup preset ───────────────────────────────────────────
        # 1. Targets from uploaded sources.
        # Render at the future TRAINING resolution (v3): a 2048px target
        # downscaled to a 768px training bucket blurs thick strokes into
        # double edges — and the model learns to draw them that way.
        target_side = int(params.get("target_long_side", 1024))
        sources, rejected = _resolve_sources(source_paths)
        stats["render_failed"].extend(rejected)
        for p in sources:
            if p.suffix.lower() == ".pdf":
                # A scanned album: every page is its own target (framed
                # sheets already — no ЕСКД wrapping needed). Resumable and
                # with the non-drawing-page filter inside.
                pages, skipped = core.render_pdf_targets(
                    p, targets_dir, long_side=target_side)
                stats["rendered"] += pages
                stats["page_skipped"] += skipped
                if not pages and not skipped:
                    stats["render_failed"].append(p.name)
                continue
            out = targets_dir / (p.stem + ".png")
            if out.exists():
                stats["rendered"] += 1  # resumed: already rendered
                continue
            reason = core.render_target(p, out, long_side=target_side)
            if reason is None:
                stats["rendered"] += 1
                if params.get("eskd_sheet", True) and p.suffix.lower() in (".dxf", ".dwg"):
                    # DWG/DXF renders come frameless; real photos are of
                    # PRINTED sheets — dress them in ГОСТ furniture so the
                    # model learns to preserve it. Synthetics already have it.
                    try:
                        core.wrap_in_eskd_sheet(out, {"name": p.stem})
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("lora_dataset_wrap_failed", file=p.name,
                                       error=str(exc)[:120])
            else:
                stats["render_failed"].append(f"{p.name}: {reason}")

        # 2. Synthetic targets (spec JSON saved next to each PNG — captions
        # for synthetics are built from the spec, no VLM needed).
        synth_count = int(params.get("synth_count", 0))
        if synth_count > 0:
            stats["synthetic"] = core.generate_synthetic_targets(
                targets_dir, synth_count, seed=int(params.get("seed", 42)),
                long_side=target_side,
            )

        # 3. Captions. Synthetic targets: deterministic from the saved spec
        # (instant, exact). User uploads: local VLM ladder + QA. The VLM
        # path checks the GPU lock before EVERY call — captioning runs for
        # hours, and a training run approved mid-way must not fight the
        # captioner for VRAM (reloading an unloaded Ollama model OOMs the
        # trainer).
        caption_model = params.get("caption_model") or "qwen3.6:35b"
        fallback = params.get("caption_fallback") or "gemma4:31b"
        for target in sorted(targets_dir.glob("*.png")):
            caption_path = target.with_suffix(".txt")
            if caption_path.exists():
                stats["captioned"] += 1  # resumed
                continue
            spec_path = target.with_suffix(".spec.json")
            if spec_path.exists():
                from app.ai import lora_synth_specs as specs

                caption = specs.spec_caption(json.loads(
                    spec_path.read_text(encoding="utf-8")))
            else:
                try:
                    if gpu_lock.is_locked():
                        raise RuntimeError(gpu_lock.LOCK_MESSAGE)
                except RuntimeError:
                    raise
                except Exception:  # noqa: BLE001 — Redis hiccup must not block
                    pass
                caption = core.caption_image(target, caption_model,
                                             settings.ollama_url, fallback)
            if caption:
                caption_path.write_text(caption, encoding="utf-8")
                stats["captioned"] += 1
            else:
                stats["caption_rejected"] += 1

        # 4. Degrade + assemble with QA. Holdout split is name-keyed (stable
        # across resumes); previews are sampled AFTER the loop so they cover
        # the whole dataset, not the alphabet's head.
        per_image = int(params.get("per_image", 2))
        seed = int(params.get("seed", 42))
        accepted: list[tuple[pathlib.Path, pathlib.Path]] = []  # (control, target)
        for target in sorted(targets_dir.glob("*.png")):
            caption_path = target.with_suffix(".txt")
            if not caption_path.exists():
                continue
            caption = caption_path.read_text(encoding="utf-8").strip()
            for i in range(per_image):
                name = f"{target.stem}__v{i}"
                holdout = _is_holdout(name)
                img_dir = holdout_images_dir if holdout else images_dir
                ctl_dir = holdout_control_dir if holdout else control_dir
                control = controls_dir / f"{name}.png"
                if (img_dir / f"{name}.png").exists() and (ctl_dir / f"{name}.png").exists():
                    stats["holdout" if holdout else "pairs"] += 1  # resumed
                    accepted.append((control, target))
                    continue
                if not control.exists() and not core.degrade_target(
                        target, control, _name_seed(seed, target.stem, i)):
                    continue
                reason = core.build_pair(target, control, caption, img_dir, ctl_dir,
                                         name, preset_instruction)
                if reason:
                    stats["pair_rejected"].append(f"{name}: {reason}")
                else:
                    stats["holdout" if holdout else "pairs"] += 1
                    accepted.append((control, target))

        previews = _make_previews(core, upload_file, accepted, dataset_id, seed)

        stats["pair_rejected"] = stats["pair_rejected"][:50]
        stats["render_failed"] = stats["render_failed"][:50]
        ok = stats["pairs"] > 0
        async with factory() as db:
            ds = await db.get(LoraDataset, ds_uuid)
            ds.status = LoraDatasetStatus.ready if ok else LoraDatasetStatus.failed
            ds.dataset_dir = str(root)
            ds.stats = stats
            ds.preview_paths = previews
            if not ok:
                ds.error = "Не удалось собрать ни одной пары — см. статистику."
            await db.commit()
        return {"ok": ok, "pairs": stats["pairs"]}
    except Exception as exc:  # noqa: BLE001
        logger.exception("lora_prepare_failed", dataset_id=dataset_id)
        stats["pair_rejected"] = stats["pair_rejected"][:50]
        stats["render_failed"] = stats["render_failed"][:50]
        async with factory() as db:
            ds = await db.get(LoraDataset, ds_uuid)
            if ds:
                ds.status = LoraDatasetStatus.failed
                ds.error = str(exc)[:1000]
                ds.stats = stats
                await db.commit()
        return {"error": str(exc)}
    finally:
        stop_hb.set()
        try:
            from app.ai.gpu_lock import _redis

            _redis().delete(hb_key)
        except Exception:  # noqa: BLE001
            pass


def _make_previews(core, upload_file, accepted: list, dataset_id, seed: int,
                   count: int = 6) -> list[str]:
    """Random sample over ALL accepted pairs — the first-6-alphabetical
    previews only ever showed one homogeneous slice of the dataset."""
    import random

    if not accepted:
        return []
    rng = random.Random(seed)
    picks = rng.sample(accepted, min(count, len(accepted)))
    previews: list[str] = []
    for control, target in picks:
        try:
            path = f"lora/{dataset_id}/preview_{len(previews)}.jpg"
            upload_file(core.make_preview(control, target), path, "image/jpeg")
            previews.append(path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("lora_preview_failed", error=str(exc)[:120])
    return previews


async def _prepare_edit_preset(factory, ds_uuid, root, targets_dir, controls_dir,
                               images_dir, control_dir, holdout_images_dir,
                               holdout_control_dir, params: dict, stats: dict) -> dict:
    """"drawing_edit": synthetic (before → after-edit) pairs; the exact RU
    edit instruction is the training prompt, no VLM captions needed."""
    from app.ai import lora_dataset as core
    from app.db.models import LoraDataset, LoraDatasetStatus
    from app.storage import upload_file

    count = int(params.get("synth_count", 200))
    seed = int(params.get("seed", 42))
    made = core.generate_edit_pairs(targets_dir, controls_dir, count, seed=seed)
    stats["synthetic"] = made

    accepted: list[tuple[pathlib.Path, pathlib.Path]] = []
    for target in sorted(targets_dir.glob("*.png")):
        name = target.stem
        control = controls_dir / f"{name}.png"
        holdout = _is_holdout(name)
        img_dir = holdout_images_dir if holdout else images_dir
        ctl_dir = holdout_control_dir if holdout else control_dir
        if (img_dir / f"{name}.png").exists() and (ctl_dir / f"{name}.png").exists():
            stats["holdout" if holdout else "pairs"] += 1
            accepted.append((control, target))
            continue
        instruction = target.with_suffix(".txt").read_text(encoding="utf-8").strip()
        # The instruction IS the whole prompt (no cleanup-style prefix).
        reason = core.build_pair(target, control, instruction, img_dir, ctl_dir,
                                 name, "{caption}")
        if reason:
            stats["pair_rejected"].append(f"{name}: {reason}")
        else:
            stats["holdout" if holdout else "pairs"] += 1
            accepted.append((control, target))

    previews = _make_previews(core, upload_file, accepted, ds_uuid, seed)
    stats["pair_rejected"] = stats["pair_rejected"][:50]
    ok = stats["pairs"] > 0
    async with factory() as db:
        ds = await db.get(LoraDataset, ds_uuid)
        ds.status = LoraDatasetStatus.ready if ok else LoraDatasetStatus.failed
        ds.dataset_dir = str(root)
        ds.stats = stats
        ds.preview_paths = previews
        if not ok:
            ds.error = "Не удалось собрать ни одной пары правок."
        await db.commit()
    return {"ok": ok, "pairs": stats["pairs"]}


def _seed_checkpoint_with_reset_step(src: pathlib.Path, dest: pathlib.Path) -> None:
    """Copy a LoRA checkpoint for resume with its training_info step reset to
    0. ai-toolkit reads the step from safetensors METADATA (not the
    filename) — seeding an unmodified 2500-step checkpoint into a 1500-step
    run made it declare the job finished instantly (confirmed live). Pure
    header rewrite: tensors are byte-copied untouched."""
    import json as _json
    import struct

    with src.open("rb") as fh:
        header_len = struct.unpack("<Q", fh.read(8))[0]
        header = _json.loads(fh.read(header_len))
        payload = fh.read()

    meta = header.get("__metadata__") or {}
    meta["training_info"] = _json.dumps({"step": 0, "epoch": 0})
    header["__metadata__"] = meta
    raw = _json.dumps(header, separators=(",", ":")).encode()
    with dest.open("wb") as fh:
        fh.write(struct.pack("<Q", len(raw)))
        fh.write(raw)
        fh.write(payload)


# ── Training run ─────────────────────────────────────────────────────────────


@celery_app.task(name="lora.run_training", soft_time_limit=48 * 3600, time_limit=48 * 3600 + 120)
def run_training(run_id: str) -> dict:
    return run_async(_train(run_id))


def _friendly_error(tail: str, base_model: str | None) -> str:
    """Turn a raw trainer traceback tail into an actionable message for the
    common, recognizable failures; otherwise keep the raw tail (still useful
    for debugging in the UI's expandable error block)."""
    from app.ai.lora_base_models import HF_GATED_HELP, base_model_info

    low = tail.lower()
    if "gatedrepoerror" in low or ("gated repo" in low) or (
        "401" in tail and "huggingface" in low
    ):
        hf = base_model_info(base_model).get("hf", base_model or "")
        return HF_GATED_HELP.format(hf=hf) + "\n\n— — —\n" + tail[-800:]
    if "out of memory" in low or "cuda out of memory" in low:
        return ("Недостаточно памяти GPU для этой модели/разрешения. Попробуйте "
                "меньшее разрешение, модель поменьше (например FLUX.2 klein 4B) "
                "или меньший rank.\n\n— — —\n" + tail[-800:])
    return tail[-2000:]


def _build_train_config(run_id: str, dataset_dir: str, config: dict) -> dict:
    """ai-toolkit YAML, branched per base-model family. The non-obvious qwen
    values are hard-won (see project memory project-lora-training-run):
    uint3+ARA quantization (fp8 OOMs), cached text embeddings, cosine lr.
    FLUX.2 (arch "flux2", control images supported upstream) starts from the
    BFL/ai-toolkit example: qfloat8 quantization, same flowmatch/cosine
    setup — to be tuned on the first live smoke run."""
    from app.ai.lora_base_models import base_model_info

    info = base_model_info(config.get("base_model"))
    steps = int(config.get("steps", 2500))
    return {
        "job": "extension",
        "config": {
            "name": f"run_{run_id}",
            "process": [{
                "type": "sd_trainer",
                "training_folder": f"/lora-data/runs/{run_id}/output",
                "device": "cuda:0",
                "network": {
                    "type": "lora",
                    "linear": int(config.get("rank", 32)),
                    "linear_alpha": int(config.get("rank", 32)),
                },
                "save": {"dtype": "float16",
                         "save_every": int(config.get("save_every", 500)),
                         "max_step_saves_to_keep": 6},
                "datasets": [{
                    "folder_path": f"{dataset_dir}/images",
                    "control_path": f"{dataset_dir}/control",
                    "caption_ext": "txt",
                    "caption_dropout_rate": 0.05,
                    "cache_latents_to_disk": True,
                    "resolution": [int(config.get("resolution", 768))],
                }],
                "train": {
                    "batch_size": 1,
                    "steps": steps,
                    "gradient_accumulation_steps": 2,
                    "gradient_checkpointing": True,
                    "train_unet": True,
                    "train_text_encoder": False,
                    "noise_scheduler": "flowmatch",
                    "optimizer": "adamw8bit",
                    "lr": float(config.get("lr", 1e-4)),
                    "lr_scheduler": "cosine",
                    "dtype": "bf16",
                    "cache_text_embeddings": True,
                },
                "model": {
                    "name_or_path": info["hf"],
                    "arch": info["arch"],
                    "low_vram": True,
                    **info["quantize"],
                },
                "sample": {
                    "sampler": "flowmatch",
                    "sample_every": int(config.get("sample_every", 250)),
                    "width": 768, "height": 576, "seed": 42,
                    "neg": "",  # bool default upstream crashes edit_plus encode
                    "guidance_scale": 1.0,
                    "samples": config.get("samples", []),
                },
            }],
        },
    }


def _pick_sample_controls(dataset_dir: str, limit: int = 2) -> list[pathlib.Path]:
    """Validation controls: prefer the holdout split — samples generated
    from train controls measure memorization, not generalization (the whole
    v1/v2 peak-vs-degradation reading was done on train controls; holdout
    makes that reading honest). Older datasets have no holdout — fall back."""
    ds = pathlib.Path(dataset_dir)
    for control_dir, images_dir in (
        (ds / "holdout" / "control", ds / "holdout" / "images"),
        (ds / "control", ds / "images"),
    ):
        if not control_dir.exists():
            continue
        picked = [c for c in sorted(control_dir.glob("*.png"))
                  if (images_dir / f"{c.stem}.txt").exists()][:limit]
        if picked:
            return picked
    return []


async def _train(run_id: str) -> dict:
    import datetime as _dt
    import threading

    import yaml

    from app.ai import gpu_lock
    from app.db.models import LoraDataset, LoraRunStatus, LoraTrainingRun
    from app.db.session import _get_session_factory
    from app.services import studio_queue
    from app.storage import upload_file

    factory = _get_session_factory()
    run_uuid = uuid.UUID(run_id)
    hb_key = gpu_lock.run_heartbeat_key(run_id)

    async with factory() as db:
        run = await db.get(LoraTrainingRun, run_uuid)
        if not run:
            return {"error": "run not found"}
        if run.status in (LoraRunStatus.cancelled, LoraRunStatus.done,
                          LoraRunStatus.failed):
            # Stopped/decided while queued (stop_run revokes best-effort; this
            # is the second line of defense) or a stale redelivery.
            job = await studio_queue.job_for_lora_run(db, run_uuid)
            if run.status == LoraRunStatus.cancelled:
                await studio_queue.mark_job_cancelled(db, job, error="Задача отменена пользователем.")
            elif run.status == LoraRunStatus.done:
                await studio_queue.mark_job_done(db, job)
            else:
                await studio_queue.mark_job_failed(db, job, error=run.error or "LoRA training failed")
            await db.commit()
            return {"skipped": run.status.value}
        if run.status == LoraRunStatus.stopping:
            run.status = LoraRunStatus.cancelled
            job = await studio_queue.job_for_lora_run(db, run_uuid)
            await studio_queue.mark_job_cancelled(db, job, error="Задача отменена до запуска.")
            await db.commit()
            return {"skipped": "stopped before start"}
        # Redelivery guard: with a long-running task the Redis broker's
        # visibility timeout (1h) re-delivers this very task while the first
        # instance is still supervising — confirmed live: a second trainer
        # container appeared an hour in and fought the first for the GPU.
        if run.status == LoraRunStatus.running:
            if _heartbeat_alive(hb_key):
                logger.info("lora_training_redelivery_skipped", run_id=run_id)
                return {"skipped": "already supervised"}
            logger.warning("lora_training_resuming_orphaned_run", run_id=run_id)
        ds = await db.get(LoraDataset, run.dataset_id)
        if not ds or not ds.dataset_dir:
            run.status = LoraRunStatus.failed
            run.error = "Датасет не готов."
            job = await studio_queue.job_for_lora_run(db, run_uuid)
            await studio_queue.mark_job_failed(db, job, error=run.error)
            await db.commit()
            return {"error": "dataset not ready"}
        config = dict(run.config or {})
        dataset_dir = ds.dataset_dir

    # Exclusive lock BEFORE flipping to running: two runs must never both
    # own the card. With the serialized lora queue this only trips when the
    # lock is held by something outside the queue — requeue and retry.
    if not gpu_lock.acquire(run_id):
        async with factory() as db:
            run = await db.get(LoraTrainingRun, run_uuid)
            if run and run.status == LoraRunStatus.queued:
                progress = dict(run.progress or {})
                progress.update(phase="ожидание GPU", ts=time.time())
                run.progress = progress
                job = await studio_queue.job_for_lora_run(db, run_uuid)
                await studio_queue.mark_job_waiting(db, job, reason=gpu_lock.LOCK_MESSAGE)
                await db.commit()
        celery_app.send_task("lora.run_training", args=[run_id], countdown=120)
        logger.info("lora_training_gpu_busy_requeued", run_id=run_id)
        return {"requeued": "gpu busy"}

    gpu_lock.clear_stop(run_id)  # a leftover flag must not kill the new run
    _heartbeat_touch(hb_key)

    async with factory() as db:
        run = await db.get(LoraTrainingRun, run_uuid)
        run.status = LoraRunStatus.running
        run.started_at = run.started_at or _dt.datetime.now(tz=_dt.timezone.utc)
        run.output_dir = str(_data_dir() / "runs" / run_id / "output")
        job = await studio_queue.job_for_lora_run(db, run_uuid)
        await studio_queue.mark_job_running(db, job, task_id=run.celery_task_id)
        await db.commit()

    run_dir = _data_dir() / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    # Fine-tune continuation: seed the output dir with an existing LoRA so
    # ai-toolkit resumes from it (it picks up the newest matching checkpoint
    # in the training folder) instead of starting from scratch.
    resume_from = config.get("resume_from")
    if resume_from:
        src = pathlib.Path(resume_from)
        if not src.is_absolute():
            src = _data_dir() / resume_from
        if src.exists():
            out_dir = run_dir / "output" / f"run_{run_id}"
            out_dir.mkdir(parents=True, exist_ok=True)
            _seed_checkpoint_with_reset_step(src, out_dir / f"run_{run_id}_000000000.safetensors")
            logger.info("lora_training_resume_seeded", run_id=run_id, source=str(src))
        else:
            logger.warning("lora_training_resume_missing", run_id=run_id, source=str(src))

    # Default validation samples: holdout controls with their prompts (see
    # _pick_sample_controls). Their thumbnails go to MinIO so the UI can show
    # the source next to the sample-evolution grid.
    control_paths: list[str] = []
    if not config.get("samples"):
        samples = []
        for ctrl in _pick_sample_controls(dataset_dir):
            # holdout/control/x.png → holdout/images/x.txt; control/x.png →
            # images/x.txt — both are parent.parent/images.
            prompt_file = ctrl.parent.parent / "images" / f"{ctrl.stem}.txt"
            if prompt_file.exists():
                samples.append({
                    "prompt": prompt_file.read_text(encoding="utf-8").strip(),
                    "ctrl_img_1": str(ctrl),
                })
        config["samples"] = samples
    for i, sample in enumerate(config.get("samples") or []):
        ctrl = pathlib.Path(str(sample.get("ctrl_img_1", "")))
        if not ctrl.exists():
            continue
        try:
            from PIL import Image

            img = Image.open(ctrl)
            img.thumbnail((640, 640))
            buf = io.BytesIO()
            img.convert("RGB").save(buf, format="JPEG", quality=80)
            path = f"lora/runs/{run_id}/controls/ctrl_{i}.jpg"
            upload_file(buf.getvalue(), path, "image/jpeg")
            control_paths.append(path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("lora_control_upload_failed", error=str(exc)[:120])
    if control_paths:
        async with factory() as db:
            run = await db.get(LoraTrainingRun, run_uuid)
            run.control_paths = control_paths
            await db.commit()

    cfg_path = run_dir / "config.yaml"
    cfg_path.write_text(
        yaml.safe_dump(_build_train_config(run_id, dataset_dir, config),
                       allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    total_steps = int(config.get("steps", 2500))
    gpu_lock.unload_gpu_consumers()

    # Dedicated lock refresher: the log-stream loop below stalls whenever the
    # trainer is silent (model quantization runs ~10 min without a single
    # line — confirmed live: the TTL expired and the lock vanished mid-run),
    # so the refresh must not depend on log activity. The same thread honours
    # stop requests (Redis flag) — a stop pressed during a silent phase used
    # to hang until the logs resumed.
    stop_refresh = threading.Event()
    container_box: dict = {"c": None}

    def _refresher() -> None:
        while not stop_refresh.wait(60):
            try:
                gpu_lock.refresh(run_id)
                _heartbeat_touch(hb_key)
                if gpu_lock.stop_requested(run_id) and container_box["c"] is not None:
                    logger.info("lora_training_stop_via_flag", run_id=run_id)
                    container_box["c"].stop(timeout=60)
            except Exception:  # noqa: BLE001
                pass

    refresher = threading.Thread(target=_refresher, daemon=True)
    refresher.start()

    container = None
    try:
        import docker

        client = docker.from_env()
        # Idempotency: a worker restart re-queues this task (acks-late) while
        # the previous attempt's container keeps running unsupervised —
        # confirmed live: two trainers ended up fighting for one GPU. Reap any
        # container belonging to this run before starting a fresh one.
        for stale in client.containers.list(
            all=True, filters={"label": f"aidocs.lora_run={run_id}"}
        ):
            logger.warning("lora_training_reaping_stale_container",
                           run_id=run_id, container=stale.id[:12])
            try:
                stale.remove(force=True)
            except Exception:  # noqa: BLE001
                pass

        environment = {"PYTORCH_CUDA_ALLOC_CONF": "expandable_segments:True",
                       "HF_HUB_OFFLINE": "0",
                       "PYTHONUNBUFFERED": "1"}
        # Token resolved from the encrypted UI setting (Redis) with .env
        # fallback — never hardcoded in the container definition.
        from app.ai.lora_base_models import get_hf_token

        hf_token = get_hf_token()
        if hf_token:
            environment["HF_TOKEN"] = hf_token
        container = client.containers.run(
            image="infra-lora-trainer",
            command=["python", "run.py", f"/lora-data/runs/{run_id}/config.yaml"],
            volumes={
                "infra_lora_data": {"bind": "/lora-data", "mode": "rw"},
                "infra_hf_cache": {"bind": "/root/.cache/huggingface", "mode": "rw"},
            },
            environment=environment,
            # Docker's default /dev/shm is 64MB — torch DataLoader workers
            # crash with "unable to allocate shared memory" (confirmed live).
            shm_size="8g",
            device_requests=[docker.types.DeviceRequest(count=-1, capabilities=[["gpu"]])],
            detach=True,
            labels={"aidocs.lora_run": run_id},
        )
        container_box["c"] = container
        async with factory() as db:
            run = await db.get(LoraTrainingRun, run_uuid)
            run.container_id = container.id[:12]
            await db.commit()

        progress: dict = {"step": 0, "total": total_steps, "loss": None,
                          "eta": None, "phase": "загрузка модели", "history": [],
                          "ts": time.time()}
        known_samples: set[str] = set()
        history_stride = max(1, total_steps // 200)  # ≤~200 sparkline points

        def _parse_chunk(chunk: str) -> None:
            """Progress parsing MUST be bulletproof: docker's log buffering
            tears lines mid-number ('2.385e-02' → '2.385e'), and one такой
            обрывок once crashed the supervisor — which then force-removed a
            healthy trainer 3 hours in (confirmed live). Malformed lines are
            simply skipped."""
            try:
                m = _PROGRESS_RE.search(chunk)
                if m:
                    step = int(m.group(1))
                    loss = float(m.group(5))
                    progress.update(step=step, total=int(m.group(2)),
                                    eta=m.group(4), loss=loss,
                                    phase="обучение")
                    hist = progress["history"]
                    # Monotonic-by-step: tail polling re-reads old lines, so
                    # only append genuinely NEW steps (never re-add / reorder).
                    if step % history_stride == 0 and (not hist or step > hist[-1][0]):
                        hist.append([step, loss])
                elif "Saving latents" in chunk:
                    progress["phase"] = "кэширование латентов"
                elif "text embeddings" in chunk:
                    progress["phase"] = "кэширование эмбеддингов"
                elif "Generating" in chunk and "amples" in chunk:
                    progress["phase"] = "генерация сэмплов"
            except Exception:  # noqa: BLE001 — a torn line is noise, not an error
                pass

        # Progress is read by POLLING the stored logs, not by a streaming
        # follow. ai-toolkit's tqdm bar updates in place with \r and no
        # newline; docker's json-file driver buffers such a line and does NOT
        # emit it over `logs(stream=True, follow=True)` until a newline
        # arrives — so step/loss/eta froze at 0 for a whole run while only the
        # newline-terminated phase prints showed up (confirmed live
        # 2026-07-05, twice; tty=True made it worse). But the \r content IS
        # present in the STORED log, so a periodic `logs(tail=N)` reads it
        # reliably. tail is generous so a burst between polls isn't missed.
        import asyncio as _asyncio

        while True:
            try:
                raw = container.logs(tail=400, timestamps=False)
                text = raw.decode("utf-8", "replace")
                for chunk in text.replace("\r", "\n").splitlines():
                    _parse_chunk(chunk)
                gpu_lock.refresh(run_id)
                await _flush_progress(factory, run_uuid, run_dir, progress,
                                      known_samples, upload_file, run_id)
                async with factory() as db:
                    run = await db.get(LoraTrainingRun, run_uuid)
                    if run and run.status == LoraRunStatus.stopping:
                        container.stop(timeout=60)
            except Exception as exc:  # noqa: BLE001 — a poll hiccup, not job failure
                logger.info("lora_training_log_poll_hiccup", run_id=run_id,
                            error=str(exc)[:100])
            container.reload()
            if container.status != "running":
                break
            await _asyncio.sleep(15)

        result = container.wait(timeout=600)
        exit_code = int(result.get("StatusCode", 1))
        await _flush_progress(factory, run_uuid, run_dir, progress,
                              known_samples, upload_file, run_id)
        final_status = None
        job_id: str | None = None
        async with factory() as db:
            run = await db.get(LoraTrainingRun, run_uuid)
            if run.status == LoraRunStatus.stopping or gpu_lock.stop_requested(run_id):
                run.status = LoraRunStatus.cancelled
            elif exit_code == 0:
                run.status = LoraRunStatus.done
            else:
                run.status = LoraRunStatus.failed
                tail = container.logs(tail=30).decode("utf-8", "replace")
                run.error = _friendly_error(tail, config.get("base_model"))
            run.finished_at = _dt.datetime.now(tz=_dt.timezone.utc)
            job = await studio_queue.job_for_lora_run(db, run_uuid)
            job_id = str(job.id) if job else None
            if run.status == LoraRunStatus.done:
                await studio_queue.mark_job_done(db, job)
            elif run.status == LoraRunStatus.cancelled:
                await studio_queue.mark_job_cancelled(db, job, error="Задача отменена пользователем.")
            else:
                await studio_queue.mark_job_failed(db, job, error=run.error or "LoRA training failed")
            final_status = run.status
            owner = run.owner_sub
            run_name = run.name
            await db.commit()

        if owner:
            try:
                from app.services import push

                async with factory() as db:
                    await push.push_to_user(
                        db=db,
                        user_sub=owner,
                        title="Обучение LoRA завершено"
                        if final_status == LoraRunStatus.done
                        else f"Обучение LoRA: {final_status.value}",
                        body=f"«{run_name}»: {final_status.value}. Чекпойнты — в студии.",
                        action_url=f"/studio?job={job_id}" if job_id else "/studio",
                        notification_type="lora_training",
                    )
            except Exception:  # noqa: BLE001
                pass
        return {"ok": exit_code == 0}
    except Exception as exc:  # noqa: BLE001
        logger.exception("lora_training_failed", run_id=run_id)
        async with factory() as db:
            run = await db.get(LoraTrainingRun, run_uuid)
            if run:
                run.status = LoraRunStatus.failed
                run.error = str(exc)[:2000]
                run.finished_at = _dt.datetime.now(tz=_dt.timezone.utc)
                job = await studio_queue.job_for_lora_run(db, run_uuid)
                await studio_queue.mark_job_failed(db, job, error=run.error)
                await db.commit()
        return {"error": str(exc)}
    finally:
        stop_refresh.set()
        if container is not None:
            try:
                container.remove(force=True)
            except Exception:  # noqa: BLE001
                pass
        gpu_lock.release(run_id)
        gpu_lock.clear_stop(run_id)
        try:
            from app.ai.gpu_lock import _redis

            _redis().delete(hb_key)
        except Exception:  # noqa: BLE001
            pass


async def _flush_progress(factory, run_uuid, run_dir: pathlib.Path, progress: dict,
                          known_samples: set, upload_file, run_id: str) -> None:
    from app.db.models import LoraTrainingRun
    from app.services import studio_queue

    output = run_dir / "output" / f"run_{run_id}"
    checkpoints = sorted(p.name for p in output.glob("*.safetensors")) if output.exists() else []
    new_sample_paths: list[str] = []
    samples_dir = output / "samples"
    if samples_dir.exists():
        for sample in sorted(samples_dir.glob("*.jpg")):
            if sample.name in known_samples:
                continue
            known_samples.add(sample.name)
            try:
                from PIL import Image

                img = Image.open(sample)
                img.thumbnail((640, 640))
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=80)
                path = f"lora/runs/{run_id}/samples/{sample.name}"
                upload_file(buf.getvalue(), path, "image/jpeg")
                new_sample_paths.append(path)
            except Exception:  # noqa: BLE001
                pass

    progress["ts"] = time.time()  # "updated N seconds ago" in the UI
    async with factory() as db:
        run = await db.get(LoraTrainingRun, run_uuid)
        if not run:
            return
        run.progress = dict(progress)
        run.checkpoints = checkpoints
        if new_sample_paths:
            run.sample_paths = list(run.sample_paths or []) + new_sample_paths
        job = await studio_queue.job_for_lora_run(db, run_uuid)
        if job:
            job.progress = dict(progress)
        await db.commit()
