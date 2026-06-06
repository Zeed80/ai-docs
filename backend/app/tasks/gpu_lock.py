"""Single-flight GPU lock — a belt-and-suspenders guarantee that only one
GPU-heavy pipeline step runs at any moment, even if the ``gpu`` Celery worker is
ever (mis)configured with concurrency > 1 or scaled to several replicas.

The dedicated ``-c 1`` worker already serialises GPU tasks; this distributed
Redis lock protects the same invariant across processes/hosts. It is
*best-effort*: if Redis is unavailable the step still runs (correctness of a
single document never depends on the lock — only the no-overlap property does).

Usage::

    from app.tasks.gpu_lock import gpu_single_flight

    with gpu_single_flight(f"extract:{document_id}"):
        ... # OCR / extraction / embedding work
"""

from __future__ import annotations

import os
import time
import uuid
from contextlib import contextmanager

import structlog

logger = structlog.get_logger()

_LOCK_KEY = "gpu:single_flight"
# Lock auto-expires a bit after Celery's hard task time limit (360s) so a crashed
# worker can never wedge the GPU lane forever.
_LOCK_TTL_SECONDS = int(os.getenv("GPU_LOCK_TTL_SECONDS", "420"))
# How long to wait for the lane to free up before giving up and running anyway.
_ACQUIRE_TIMEOUT_SECONDS = int(os.getenv("GPU_LOCK_WAIT_SECONDS", "600"))
_POLL_SECONDS = 0.5


@contextmanager
def gpu_single_flight(label: str = ""):
    """Acquire the global GPU lane for the duration of the block.

    Best-effort: yields immediately if Redis is unreachable. Always releases the
    lock it owns on exit (and never releases a lock owned by another worker —
    the token check guards against deleting a lock that expired and was retaken).
    """
    token = uuid.uuid4().hex
    redis = None
    acquired = False
    try:
        from app.utils.redis_client import get_sync_redis

        redis = get_sync_redis()
    except Exception as exc:  # noqa: BLE001
        logger.warning("gpu_lock_no_redis", label=label, error=str(exc))
        yield
        return

    deadline = time.monotonic() + _ACQUIRE_TIMEOUT_SECONDS
    try:
        while True:
            try:
                acquired = bool(
                    redis.set(_LOCK_KEY, token, nx=True, ex=_LOCK_TTL_SECONDS)
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("gpu_lock_redis_error", label=label, error=str(exc))
                acquired = False
                break  # degrade gracefully — run without the lock
            if acquired:
                break
            if time.monotonic() >= deadline:
                logger.warning("gpu_lock_timeout", label=label, waited_s=_ACQUIRE_TIMEOUT_SECONDS)
                break  # run anyway rather than drop the document
            time.sleep(_POLL_SECONDS)

        if acquired:
            logger.debug("gpu_lock_acquired", label=label)
        yield
    finally:
        if acquired and redis is not None:
            try:
                # Release only if we still own it (compare-and-delete).
                if redis.get(_LOCK_KEY) == token or redis.get(_LOCK_KEY) == token.encode():
                    redis.delete(_LOCK_KEY)
                    logger.debug("gpu_lock_released", label=label)
            except Exception as exc:  # noqa: BLE001
                logger.warning("gpu_lock_release_failed", label=label, error=str(exc))
