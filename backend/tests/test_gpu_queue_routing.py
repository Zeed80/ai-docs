"""Locks in Stage-2 routing: every GPU-bound task lands on the dedicated ``gpu``
queue (served by a strictly sequential -c 1 worker), while CPU/IO tasks stay on
their parallel queues. This is what makes "one document at a time, steps never
overlap on the GPU" enforceable — the old single ``extraction`` queue let a -c 4
worker run GPU steps of different documents in parallel.
"""

import pytest

from app.tasks.celery_app import celery_app


def _queue_for(task_name: str) -> str:
    route = celery_app.amqp.router.route({}, task_name) or {}
    q = route.get("queue")
    # Celery may hand back a Queue object or a plain string depending on version.
    return getattr(q, "name", q)


@pytest.mark.parametrize("task_name", [
    "app.tasks.extraction.classify_document",
    "app.tasks.extraction.extract_invoice",
    "app.tasks.extraction.extract_generic_fields",
    "app.tasks.extraction.auto_verify_document",
    "app.tasks.embedding.embed_document",
])
def test_gpu_tasks_route_to_gpu_queue(task_name):
    assert _queue_for(task_name) == "gpu", f"{task_name} must run on the gpu lane"


@pytest.mark.parametrize("task_name,expected", [
    # CPU/DB-only extraction-module tasks stay on the parallel extraction queue
    ("app.tasks.extraction.process_approved_document", "extraction"),
    ("app.tasks.extraction.check_invoice_anomalies", "extraction"),
    # IMAP / file storage
    ("app.tasks.ingest.poll_imap_mailbox", "ingest"),
])
def test_non_gpu_tasks_stay_off_gpu_lane(task_name, expected):
    assert _queue_for(task_name) == expected


def test_worker_config_defaults_sequential():
    # Defensive default in the app config (CLI -c on the gpu worker is the real
    # guarantee, but the config default should never silently allow parallelism).
    assert celery_app.conf.worker_concurrency == 1
    assert celery_app.conf.worker_prefetch_multiplier == 1
