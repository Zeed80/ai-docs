"""Celery application configuration."""

import os

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
    # Short broker timeouts so dev/tests don't hang when Redis is unavailable
    broker_connection_timeout=2.0,
    broker_connection_retry=False,
    broker_transport_options={"socket_timeout": 2, "socket_connect_timeout": 2},
    # When CELERY_TASK_ALWAYS_EAGER=true (tests), run tasks in-process without broker
    task_always_eager=os.getenv("CELERY_TASK_ALWAYS_EAGER", "false").lower() == "true",
    task_eager_propagates=False,  # swallow task errors in eager mode
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_soft_time_limit=300,  # 5 minutes — soft limit raises exception
    task_time_limit=360,       # 6 minutes — hard kill
    worker_concurrency=1,      # sequential processing (safe for GPU)
    task_routes={
        # ── GPU lane (strictly sequential) ──────────────────────────────────
        # Every step that loads an Ollama model (OCR / extraction / verify /
        # embedding / drawing VLM) goes to the dedicated ``gpu`` queue, served
        # by a single -c 1 worker so documents are processed strictly one at a
        # time and pipeline steps never overlap on the GPU. These specific
        # entries MUST precede the "app.tasks.extraction.*" glob below — Celery
        # returns the first matching route in insertion order.
        "app.tasks.extraction.classify_document": {"queue": "gpu"},
        "app.tasks.extraction.extract_invoice": {"queue": "gpu"},
        "app.tasks.extraction.extract_generic_fields": {"queue": "gpu"},
        "app.tasks.extraction.auto_verify_document": {"queue": "gpu"},
        "app.tasks.embedding.embed_document": {"queue": "gpu"},
        # name= on @celery_app.task in drawing_analysis.py — VLM, GPU-bound
        "drawing_analysis.*": {"queue": "gpu"},
        # ── CPU / IO lanes (parallel is fine — no GPU) ──────────────────────
        "app.tasks.ingest.*": {"queue": "ingest"},
        # Remaining extraction-module tasks are DB/CPU only
        # (process_approved_document, check_invoice_anomalies, …).
        "app.tasks.extraction.*": {"queue": "extraction"},
        "app.tasks.scheduler.*": {"queue": "scheduler"},
        "tp_generation.*": {"queue": "celery"},
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
    # Proactive: check approaching invoice due dates every 6 hours
    "proactive-due-dates": {
        "task": "proactive.check_due_dates",
        "schedule": 21_600.0,
    },
    # Proactive: alert on stale critical anomalies every hour
    "proactive-critical-anomalies": {
        "task": "proactive.alert_critical_anomalies",
        "schedule": 3_600.0,
    },
    # Dispatch due reminders every 5 minutes
    "dispatch-due-reminders": {
        "task": "proactive.dispatch_due_reminders",
        "schedule": 300.0,
    },
    # Proactive: alert on stale (>24h) pending approvals every 2 hours
    "proactive-stale-approvals": {
        "task": "proactive.check_stale_approvals",
        "schedule": 7_200.0,
    },
    # Secretary morning briefing — once a day at the configured hour
    "proactive-morning-briefing": {
        "task": "proactive.morning_briefing",
        "schedule": crontab(hour=settings.morning_briefing_hour, minute=0),
    },
    # Draft-first alert on freshly-ingested duplicate invoices — hourly
    "proactive-duplicate-invoices": {
        "task": "proactive.alert_duplicate_invoices",
        "schedule": 3_600.0,
    },
    # Check saved-query alerts every hour
    "check-saved-query-alerts": {
        "task": "search.check_saved_query_alerts",
        "schedule": 3_600.0,
    },
    # Auto-cluster canonical items every 4 hours
    "canonical-auto-cluster": {
        "task": "canonical.auto_cluster",
        "schedule": 14_400.0,
    },
    # Watchdog: reset documents stuck in 'extracting' status every 5 minutes
    "watchdog-stuck-documents": {
        "task": "app.tasks.extraction.watchdog_stuck_documents",
        "schedule": 300.0,
    },
    # AgentCron executor: run due scheduled agent prompts (headless turns)
    "agent-cron-dispatch": {
        "task": "agent.cron_dispatch",
        "schedule": 60.0,
    },
    # Safety net: sweep business-entity graph nodes/edges left orphaned by
    # any path that bypassed the memory_builder hooks — every 30 minutes.
    "memory-reconcile-graph": {
        "task": "memory.reconcile_graph",
        "schedule": 1_800.0,
    },
    # Background graph analytics (god nodes/clusters/surprising connections).
    # The actual cadence is admin-configurable (GraphAnalyticsSettings in
    # Redis, /api/admin/graph/settings) — celery-beat itself only ticks every
    # 30 min, the task self-throttles against the configured interval, so
    # most ticks are a cheap no-op regardless of how low the interval is set.
    "memory-graph-analytics": {
        "task": "memory.run_graph_analytics",
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
from app.tasks import proactive as _proactive  # noqa: F401
from app.tasks import saved_query_alerts as _saved_query_alerts  # noqa: F401
from app.tasks import canonical_cluster as _canonical_cluster  # noqa: F401
from app.tasks import tp_generation as _tp_generation  # noqa: F401
from app.tasks import agent_cron as _agent_cron  # noqa: F401
from app.tasks import graph_memory as _graph_memory  # noqa: F401
from app.tasks import graph_analytics as _graph_analytics  # noqa: F401
