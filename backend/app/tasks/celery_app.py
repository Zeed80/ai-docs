"""Celery application configuration."""

import os
from datetime import timedelta

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
        "engineering_model_reader.*": {"queue": "gpu"},
        # ComfyUI studio jobs are GPU/VRAM-bound too. They get their own logical
        # queue for product-level queueing, but the single GPU worker consumes it
        # alongside gpu/gpu_priority so heavy image work never lands on the
        # general-purpose Celery worker.
        "image_generation.*": {"queue": "studio"},
        # Vectorize (scan → CAD IR → DXF) is CPU-only — no GPU/ComfyUI, so it
        # must not sit behind diffusion jobs in the studio queue.
        "cad_trace.*": {"queue": "celery"},
        # ── LoRA lane (worker-lora, -c 1) ────────────────────────────────────
        # Training supervises a container for up to 48h and preparation
        # captions for hours — neither may occupy a general-purpose slot, and
        # a single-slot queue naturally serializes GPU-hungry jobs.
        "lora.*": {"queue": "lora"},
        # ── CPU / IO lanes (parallel is fine — no GPU) ──────────────────────
        "app.tasks.ingest.*": {"queue": "ingest"},
        # Remaining extraction-module tasks are DB/CPU only
        # (process_approved_document, check_invoice_anomalies, …).
        "app.tasks.extraction.*": {"queue": "extraction"},
        "app.tasks.scheduler.*": {"queue": "scheduler"},
        # Supplier catalogs: minutes of parsing plus an embedding per row. They
        # used to route to "gpu" through the drawing_analysis.* glob and blocked
        # document recognition for the duration; own queue, own worker.
        # Rendering/OCR is CPU work and must not occupy the LLM slot; the parse
        # step takes gpu_single_flight per page so invoices interleave.
        "catalog.render_page_batch": {"queue": "catalog_render"},
        "catalog.prepare_pages": {"queue": "catalog_render"},
        "catalog.*": {"queue": "catalog"},
        "tp_generation.*": {"queue": "celery"},
    },
)

# timedelta, not crontab("*/N"): an interval that does not divide 60 (7, 8, 45)
# produces uneven gaps at the top of every hour.
_imap_cron = timedelta(minutes=max(1, settings.imap_poll_interval_minutes))

celery_app.conf.beat_schedule = {
    # DB-driven mailbox polling: one dispatcher fans out poll_imap_mailbox for
    # every active MailboxConfig row. Replaces the old hard-coded
    # procurement/accounting/general entries — a mailbox added through the UI
    # with any other name was never polled, which is why the client kept
    # reporting "IMAP not configured".
    "dispatch-mailbox-polls": {
        "task": "app.tasks.email_triage.dispatch_mailbox_polls",
        "schedule": _imap_cron,
    },
    # Ф2 — the other half of the loop. Push first (our changes are the ones a
    # person is waiting to see land), then read back what changed on the server.
    "email-push-sync-ops": {
        "task": "email.push_ops",
        "schedule": timedelta(minutes=1),
    },
    "email-sync-flags": {
        "task": "email.sync_flags_all",
        "schedule": timedelta(minutes=10),
    },
    # Ф2.3 — keep an IDLE watcher alive per mailbox. A no-op when imapclient
    # is unavailable; polling above stays the safety net either way.
    "email-idle-dispatch": {
        "task": "email.idle_dispatch",
        "schedule": timedelta(minutes=5),
    },
    "email-discover-folders": {
        "task": "email.discover_folders_all",
        "schedule": timedelta(hours=6),
    },
    # Ф6.5 — scenarios declared `trigger: {type: schedule}` in gateway.yml and
    # nothing ever read it, so low_stock_alert and memory_maintenance had never
    # run once. Ticks every minute; the task itself decides what is due.
    "scenario-cron-dispatch": {
        "task": "scenario.cron_dispatch",
        "schedule": timedelta(minutes=1),
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
    # Durable autonomous runtime: reclaim stale leases and dispatch ready steps.
    "work-order-dispatch": {
        "task": "work.dispatch_ready",
        "schedule": 5.0,
    },
    # Durable learning outbox: recover memory/recipe extraction after transient
    # Redis, model, or vector-store failures without reopening completed work.
    "work-order-learning": {
        "task": "work.learn_pending",
        "schedule": 30.0,
    },
    "work-order-memory-expiry": {
        "task": "work.expire_memory",
        "schedule": 3_600.0,
    },
    # Б12: repeated same-shape WorkStepAttempt failures -> a draft
    # CapabilityProposal for a human to review. Batched hourly, never per
    # failed attempt.
    "work-order-gap-detection": {
        "task": "work.detect_gaps",
        "schedule": 3_600.0,
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
    # Ф6 (AGENT_AUTONOMY_ROADMAP.md): idle-reflection "subconscious" —
    # consolidates duplicate pending memory proposals and revalidates
    # overdue connector strategies, but only while no user has been active
    # recently and not more often than settings.min_interval_seconds; see
    # app.domain.idle_reflection for the actual throttle logic. The beat
    # tick itself stays fixed at 20 min (same self-throttle split as
    # memory-graph-analytics above), most ticks are a cheap no-op.
    "catalog-resume-stalled": {
        "task": "catalog.resume_stalled",
        "schedule": 300.0,
    },
    "idle-reflection": {
        "task": "agent.idle_reflection",
        "schedule": 1_200.0,
    },
    # Email attachment retention — daily.
    "email-prune-attachments": {
        "task": "app.tasks.email_triage.prune_attachments",
        "schedule": 86_400.0,
    },
    # Ф8 — message-body retention, per mailbox. A no-op unless a mailbox has a
    # window configured (default 0 = keep forever).
    "email-prune-bodies": {
        "task": "app.tasks.email_triage.prune_message_bodies",
        "schedule": 86_400.0,
    },
}

celery_app.autodiscover_tasks([
    "app.tasks.extraction",
    "app.tasks.ingest",
    "app.tasks.email_triage",
    "app.tasks.email_sync",
    "app.tasks.email_idle",
    "app.tasks.scenario_cron",
    "app.tasks.embedding",
    "app.tasks.email_sender",
])

# Flat module — not discovered by autodiscover_tasks(related_name="tasks").
from app.tasks import drawing_analysis as _drawing_analysis  # noqa: F401
from app.tasks import catalog_ingest as _catalog_ingest  # noqa: F401
from app.tasks import catalog_archive as _catalog_archive  # noqa: F401
from app.tasks import catalog_crawl as _catalog_crawl  # noqa: F401
from app.tasks import catalog_pages as _catalog_pages  # noqa: F401
from app.tasks import catalog_visual as _catalog_visual  # noqa: F401
from app.tasks import approval_escalation as _approval_escalation  # noqa: F401
from app.tasks import email_compose_task as _email_compose_task  # noqa: F401
from app.tasks import skill_evolution as _skill_evolution  # noqa: F401
from app.tasks import proactive as _proactive  # noqa: F401
from app.tasks import saved_query_alerts as _saved_query_alerts  # noqa: F401
from app.tasks import canonical_cluster as _canonical_cluster  # noqa: F401
from app.tasks import tp_generation as _tp_generation  # noqa: F401
from app.tasks import agent_cron as _agent_cron  # noqa: F401
from app.tasks import work_orders as _work_orders  # noqa: F401
from app.tasks import graph_memory as _graph_memory  # noqa: F401
from app.tasks import graph_analytics as _graph_analytics  # noqa: F401
from app.tasks import idle_reflection as _idle_reflection  # noqa: F401
from app.tasks import image_generation as _image_generation  # noqa: F401
from app.tasks import lora_training as _lora_training  # noqa: F401
from app.tasks import cad_trace as _cad_trace  # noqa: F401
from app.tasks import engineering_model_reader as _engineering_model_reader  # noqa: F401
