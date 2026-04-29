"""Celery application configuration."""

from celery import Celery
from celery.schedules import crontab

from app.config import settings

celery_app = Celery(
    "ai_workspace",
    broker=settings.redis_url,
    backend=settings.redis_url,
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_routes={
        "app.tasks.ingest.*": {"queue": "ingest"},
        "app.tasks.extraction.*": {"queue": "extraction"},
        "app.tasks.scheduler.*": {"queue": "scheduler"},
    },
)

celery_app.conf.beat_schedule = {
    "poll-email-procurement": {
        "task": "app.tasks.ingest.poll_imap_mailbox",
        "schedule": crontab(minute="*/5"),
        "args": ("procurement",),
    },
    "poll-email-accounting": {
        "task": "app.tasks.ingest.poll_imap_mailbox",
        "schedule": crontab(minute="*/5"),
        "args": ("accounting",),
    },
    "poll-email-general": {
        "task": "app.tasks.ingest.poll_imap_mailbox",
        "schedule": crontab(minute="*/5"),
        "args": ("general",),
    },
}

celery_app.autodiscover_tasks([
    "app.tasks.extraction",
    "app.tasks.ingest",
    "app.tasks.email_triage",
    "app.tasks.embedding",
])
