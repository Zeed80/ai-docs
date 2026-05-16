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
    task_soft_time_limit=270,  # 4.5 minutes — soft limit raises exception
    task_time_limit=300,       # 5 minutes — hard kill
    worker_concurrency=1,      # sequential processing (safe for GPU)
    task_routes={
        "app.tasks.ingest.*": {"queue": "ingest"},
        "app.tasks.extraction.*": {"queue": "extraction"},
        "app.tasks.scheduler.*": {"queue": "scheduler"},
        # name= on @celery_app.task in drawing_analysis.py
        "drawing_analysis.*": {"queue": "extraction"},
    },
)

_imap_cron = crontab(minute=f"*/{settings.imap_poll_interval_minutes}")

celery_app.conf.beat_schedule = {
    "poll-email-procurement": {
        "task": "app.tasks.ingest.poll_imap_mailbox",
        "schedule": _imap_cron,
        "args": ("procurement",),
    },
    "poll-email-accounting": {
        "task": "app.tasks.ingest.poll_imap_mailbox",
        "schedule": _imap_cron,
        "args": ("accounting",),
    },
    "poll-email-general": {
        "task": "app.tasks.ingest.poll_imap_mailbox",
        "schedule": _imap_cron,
        "args": ("general",),
    },
    "escalate-expired-approvals": {
        "task": "approval.escalate_expired",
        "schedule": float(settings.approval_escalation_interval_seconds),
    },
    # Skill self-improvement: evolve failing skills every 2 hours
    "evolve-failing-skills": {
        "task": "skill.evolve_failing_skills",
        "schedule": 7_200.0,
    },
    # A/B shadow test evaluation every 30 minutes
    "evaluate-shadow-tests": {
        "task": "skill.evaluate_shadow_tests",
        "schedule": 1_800.0,
    },
}

celery_app.autodiscover_tasks([
    "app.tasks.extraction",
    "app.tasks.ingest",
    "app.tasks.email_triage",
    "app.tasks.embedding",
    "app.tasks.email_sender",
])

# Flat module — not discovered by autodiscover_tasks(related_name="tasks").
from app.tasks import drawing_analysis as _drawing_analysis  # noqa: F401
from app.tasks import approval_escalation as _approval_escalation  # noqa: F401
from app.tasks import skill_evolution as _skill_evolution  # noqa: F401
