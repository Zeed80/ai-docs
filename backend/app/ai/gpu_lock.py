"""Soft GPU lock for long exclusive GPU jobs (LoRA training).

Design (user decision, 2026-07-03): no service stops. Before training we ask
Ollama/ComfyUI to unload their models via their own APIs, then set a Redis
flag that the AI router honours — local GPU inference requests fail fast
with a clear Russian message instead of OOM-ing the training job. Cloud
routes keep working. The flag carries a TTL and the training task refreshes
it, so a crashed trainer can never wedge the whole AI stack.

The lock is EXCLUSIVE (SET NX): two approved runs must never both believe
they own the card — a second trainer container OOMs the first (confirmed
live before the redelivery guard existed). ``refresh``/``release`` verify
ownership so a finishing run cannot drop or extend a lock that a different
run now holds.
"""

from __future__ import annotations

import json
import urllib.request

import structlog

from app.config import settings

logger = structlog.get_logger()

GPU_LOCK_KEY = "gpu:training_lock"
GPU_LOCK_TTL_S = 15 * 60  # refreshed by the training task every few minutes

LOCK_MESSAGE = (
    "Локальный GPU занят обучением LoRA (см. Студия → Обучение LoRA). "
    "Локальные модели временно недоступны; облачные маршруты работают."
)

def _redis():
    import redis

    return redis.Redis.from_url(settings.redis_url)


def _value(run_id: str) -> bytes:
    return json.dumps({"run_id": run_id}).encode()


def acquire(run_id: str) -> bool:
    """Try to take the lock. True = we own it (idempotent for the same
    run_id: re-acquiring our own lock refreshes it and succeeds)."""
    r = _redis()
    if r.set(GPU_LOCK_KEY, _value(run_id), ex=GPU_LOCK_TTL_S, nx=True):
        logger.info("gpu_lock_acquired", run_id=run_id)
        return True
    holder_ = holder()
    if holder_ and holder_.get("run_id") == run_id:
        refresh(run_id)
        return True
    logger.info("gpu_lock_busy", run_id=run_id, holder=holder_)
    return False


def refresh(run_id: str) -> bool:
    """Extend the TTL — only if this run still holds the lock. GET+compare
    (not atomic, but the race window is harmless: a competing holder can only
    exist after a full TTL lapse, and the serialized lora queue means there
    is normally only one writer)."""
    try:
        if (holder() or {}).get("run_id") == run_id:
            _redis().expire(GPU_LOCK_KEY, GPU_LOCK_TTL_S)
            return True
    except Exception:  # noqa: BLE001 — Redis hiccup; next refresh retries
        pass
    return False


def release(run_id: str) -> None:
    """Drop the lock — only if this run holds it (a stale finisher must not
    release the lock a newer run now owns)."""
    released = False
    try:
        if (holder() or {}).get("run_id") == run_id:
            _redis().delete(GPU_LOCK_KEY)
            released = True
    except Exception:  # noqa: BLE001
        pass
    logger.info("gpu_lock_released", run_id=run_id, released=released)


def holder() -> dict | None:
    raw = _redis().get(GPU_LOCK_KEY)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:  # noqa: BLE001
        return {"run_id": "unknown"}


def is_locked() -> bool:
    return holder() is not None


def run_heartbeat_key(run_id: str) -> str:
    """Supervisor liveness for a training run (see tasks/lora_training.py)."""
    return f"lora:run_heartbeat:{run_id}"


def dataset_heartbeat_key(dataset_id: str) -> str:
    """Liveness of a dataset-preparation task (same watchdog idea)."""
    return f"lora:ds_heartbeat:{dataset_id}"


def stop_key(run_id: str) -> str:
    """Redis flag asking the training supervisor to stop the container.

    The DB ``stopping`` status is only noticed while trainer logs flow; this
    flag is polled by the supervisor's refresher thread (fires every 60s
    regardless of log activity), so a stop lands within a minute even during
    silent phases like model quantization."""
    return f"lora:stop:{run_id}"


def request_stop(run_id: str) -> None:
    _redis().set(stop_key(run_id), "1", ex=48 * 3600)


def stop_requested(run_id: str) -> bool:
    try:
        return _redis().get(stop_key(run_id)) is not None
    except Exception:  # noqa: BLE001
        return False


def clear_stop(run_id: str) -> None:
    try:
        _redis().delete(stop_key(run_id))
    except Exception:  # noqa: BLE001
        pass


def unload_gpu_consumers() -> None:
    """Politely evict models from VRAM: Ollama keep_alive=0 per loaded model,
    ComfyUI /free. Best-effort — services stay up, only their weights leave
    the GPU."""
    ollama = settings.ollama_url.rstrip("/")
    try:
        loaded = json.loads(
            urllib.request.urlopen(f"{ollama}/api/ps", timeout=15).read()
        ).get("models", [])
        for m in loaded:
            body = json.dumps({"model": m["name"], "keep_alive": 0}).encode()
            req = urllib.request.Request(
                f"{ollama}/api/generate", data=body,
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=60).read()
            logger.info("gpu_lock_unloaded_ollama_model", model=m["name"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("gpu_lock_ollama_unload_failed", error=str(exc)[:120])

    try:
        comfy = settings.comfyui_url.rstrip("/")
        req = urllib.request.Request(
            f"{comfy}/free",
            data=json.dumps({"unload_models": True, "free_memory": True}).encode(),
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=30).read()
        logger.info("gpu_lock_comfyui_freed")
    except Exception as exc:  # noqa: BLE001
        logger.warning("gpu_lock_comfyui_free_failed", error=str(exc)[:120])
