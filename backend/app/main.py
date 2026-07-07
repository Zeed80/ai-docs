import asyncio
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

# Sentry — optional; gracefully skipped when DSN is not set
_SENTRY_DSN = os.environ.get("SENTRY_DSN", "")
if _SENTRY_DSN:
    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration

        sentry_sdk.init(
            dsn=_SENTRY_DSN,
            environment=os.environ.get("ENVIRONMENT", "production"),
            traces_sample_rate=0.1,
            integrations=[FastApiIntegration(), SqlalchemyIntegration()],
            send_default_pii=False,
        )
    except ImportError:
        pass

from app.middleware.csrf import CSRFMiddleware
from app.middleware.rate_limit import RateLimitMiddleware
from app.middleware.security import SecurityHeadersMiddleware
from app.middleware.prometheus import PrometheusMiddleware

from app.api import (
    agent,
    agent_actions,
    agent_control_plane,
    ai_settings,
    anomalies,
    approvals,
    audit,
    auth,
    auto_approval,
    boms,
    calendar,
    canonical,
    canvas,
    chat_sessions,
    collections,
    comments as comments_api,
    compare,
    dashboard,
    documents,
    draft_email,
    drawings,
    email,
    email_templates,
    export,
    graph,
    health,
    invoices,
    mailbox,
    memory,
    normalization,
    ntd,
    payments,
    procurement,
    quarantine,
    scenarios,
    search,
    suppliers,
    tables,
    technology,
    telegram,
    tool_catalog,
    cases,
    warehouse,
    web_search,
    workspace,
    workspace_export,
    spec_tables,
    sheets,
)
from app.api import admin as admin_api
from app.api import devices as devices_api
from app.api import mobile_build as mobile_build_api
from app.api import image_generation as image_generation_api
from app.api import lora_training as lora_training_api
from app.api import studio_queue as studio_queue_api
from app.api import comfyui_admin as comfyui_admin_api
from app.api import comfyui_proxy as comfyui_proxy_api
from app.api import admin_graph as admin_graph_api
from app.api import maintenance as maintenance_api
from app.api import dynamic_skill_runner
from app.api import handovers, notifications, rooms
from app.api import setup as setup_api
from app.api.capability_router import router as capability_router
from app.api import local_models_api
from app.api import providers_api
from app.auth.jwt import get_current_user as _get_current_user
from app.config import settings
from app.db.session import engine  # lazy proxy
import app.core.metrics  # noqa: F401 — registers Prometheus metrics at startup

structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        (
            structlog.dev.ConsoleRenderer()
            if settings.app_debug
            else structlog.processors.JSONRenderer()
        ),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(
        logging.getLevelName(settings.app_log_level.upper())
    ),
)

logger = structlog.get_logger()


def _reject_silent_production_schema_create() -> None:
    app_env = os.getenv("APP_ENV", settings.app_env).lower()
    auto_create_schema = os.getenv("AUTO_CREATE_SCHEMA", "false").lower()
    if app_env == "production" and auto_create_schema in {"1", "true", "yes", "on"}:
        raise RuntimeError(
            "AUTO_CREATE_SCHEMA=true is not allowed in production; use Alembic migrations"
        )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    logger.info("starting", env=settings.app_env)

    # Auto-create schema for dev/e2e environments (SQLite or fresh PG without migrations)
    if os.getenv("AUTO_CREATE_SCHEMA", "false").lower() in {"1", "true", "yes", "on"}:
        try:
            import app.db.models  # noqa: F401 — ensure all models are registered
            from app.db.base import Base
            from app.db.session import _get_engine
            async with _get_engine().begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            logger.info("auto_schema_created")
        except Exception as exc:
            logger.warning("auto_schema_create_failed", error=str(exc))

    # Start Redis pub/sub subscriber for cross-worker chat events
    _redis_sub_task: asyncio.Task | None = None
    _idle_sweep_task: asyncio.Task | None = None
    try:
        from app.core.chat_bus import start_redis_subscriber
        _redis_sub_task = await start_redis_subscriber()
    except Exception as exc:
        logger.warning("chat_bus_redis_subscriber_start_failed", error=str(exc))

    try:
        from app.api.telegram import bot_manager
        err = await bot_manager.start()
        if err:
            logger.warning("telegram bot failed to start at startup", error=err)
    except Exception as exc:
        logger.warning("telegram bot init error", error=str(exc))

    try:
        from app.db.seeds.email_templates import seed_builtin_templates
        from app.db.session import _get_session_factory
        async with _get_session_factory()() as db:
            await seed_builtin_templates(db)
    except Exception as exc:
        logger.warning("email_templates_seed_failed", error=str(exc))

    try:
        from app.db.seeds.comfyui_workflows import seed_builtin_workflows
        from app.db.session import _get_session_factory
        async with _get_session_factory()() as db:
            await seed_builtin_workflows(db)
    except Exception as exc:
        logger.warning("comfyui_workflows_seed_failed", error=str(exc))

    if not settings.auth_enabled:
        logger.warning(
            "auth_disabled",
            msg="AUTH_ENABLED=false — all requests are treated as admin (dev mode). "
            "Never run this configuration on a network-exposed deployment.",
            app_env=settings.app_env,
        )
        try:
            from app.auth.jwt import _DEV_USER
            from app.auth.user_service import upsert_user
            from app.db.session import _get_session_factory
            async with _get_session_factory()() as db:
                await upsert_user(db, _DEV_USER)
                await db.commit()
            logger.info("dev_user_seeded", sub=_DEV_USER.sub)
        except Exception as exc:
            logger.warning("dev_user_seed_failed", error=str(exc))

    # One-time migration of legacy ai_config (model_ocr/vlm/reasoning) into the
    # unified task_routing store. Idempotent — no-op once routing exists.
    try:
        from app.ai.task_routing import migrate_from_ai_config
        result = migrate_from_ai_config()
        if result.get("migrated"):
            logger.info("task_routing_migration_done", **{k: v for k, v in result.items() if k != "migrated"})
    except Exception as exc:
        logger.warning("task_routing_migration_failed", error=str(exc))

    # Seed provider_instances from the YAML registry (one node per kind) on first
    # run, then refresh the Redis cache used by the AI router. Idempotent.
    try:
        from app.ai.provider_bootstrap import seed_and_refresh_providers
        from app.ai.model_runtime_store import hydrate_runtime_cache
        from app.db.session import _get_session_factory
        async with _get_session_factory()() as db:
            await seed_and_refresh_providers(db)
            await hydrate_runtime_cache(db)
    except Exception as exc:
        logger.warning("provider_instances_bootstrap_failed", error=str(exc))

    # Warm the pinned orchestrator model so the agent has an instant first
    # response; other models load on demand and free VRAM when idle.
    try:
        from app.ai.model_lifecycle import warm_pinned
        warmed = await warm_pinned()
        if warmed:
            logger.info("pinned_models_warmed", models=warmed)
    except Exception as exc:
        logger.warning("pinned_models_warm_failed", error=str(exc))

    # Background sweep: stop idle vLLM/llama.cpp servers so only the pinned
    # orchestrator holds VRAM. Runs in the backend (which has the Docker socket).
    async def _idle_server_sweep() -> None:
        from app.ai import server_lifecycle

        while True:
            await asyncio.sleep(120.0)
            try:
                await server_lifecycle.stop_idle_servers()
            except Exception as exc:
                logger.debug("idle_server_sweep_error", error=str(exc))

    try:
        _idle_sweep_task = asyncio.create_task(_idle_server_sweep())
    except Exception as exc:
        logger.warning("idle_server_sweep_start_failed", error=str(exc))

    try:
        from app.api.health import ai_health
        result = await ai_health()
        for provider, status in result.get("providers", {}).items():
            if status.get("skipped"):
                logger.info("ai_provider_skipped_no_key", provider=provider)
            elif status.get("ok"):
                logger.info("ai_provider_healthy", provider=provider,
                            latency_ms=status.get("latency_ms"))
            else:
                logger.warning("ai_provider_unreachable", provider=provider,
                               error=status.get("error"), status=status.get("status"))
    except Exception as exc:
        logger.warning("ai_provider_health_check_failed", error=str(exc))

    yield

    if _redis_sub_task and not _redis_sub_task.done():
        _redis_sub_task.cancel()
        try:
            await _redis_sub_task
        except asyncio.CancelledError:
            pass

    if _idle_sweep_task and not _idle_sweep_task.done():
        _idle_sweep_task.cancel()
        try:
            await _idle_sweep_task
        except asyncio.CancelledError:
            pass

    try:
        from app.api.telegram import bot_manager
        await bot_manager.stop()
    except Exception:
        pass

    try:
        from app.utils.redis_client import close_pools
        await close_pools()
    except Exception:
        pass

    await engine.dispose()
    logger.info("shutdown")


def create_app() -> FastAPI:
    _reject_silent_production_schema_create()

    app = FastAPI(
        title="AI Manufacturing Workspace",
        description="Backend API for AI-powered document processing",
        version="0.1.0",
        lifespan=lifespan,
    )

    # Middleware order: CORS → SecurityHeaders → RateLimit → CSRF → Prometheus
    # (Starlette applies middleware in reverse registration order)
    app.add_middleware(PrometheusMiddleware)
    app.add_middleware(CSRFMiddleware)
    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "Authorization", "X-CSRF-Token", "X-Request-ID", "X-API-Key"],
        expose_headers=["X-Request-ID"],
    )

    # Auth dependency applied to every protected router.
    # In dev mode (AUTH_ENABLED=false) it's a no-op that returns _DEV_USER.
    from fastapi import Depends as _Depends
    from app.auth.jwt import get_current_user as _get_current_user
    _auth = [_Depends(_get_current_user)]

    # ── Public routers (no auth required) ─────────────────────────────────────
    app.include_router(health.router, tags=["health"])
    app.include_router(auth.router, prefix="/api/auth", tags=["auth"])
    # agent.router contains WebSocket endpoints — WS handler validates token internally
    app.include_router(agent.router, tags=["agent"])

    # ── Protected routers ──────────────────────────────────────────────────────
    app.include_router(documents.router, prefix="/api/documents", tags=["documents"], dependencies=_auth)
    app.include_router(invoices.router, prefix="/api/invoices", tags=["invoices"], dependencies=_auth)
    app.include_router(email.router, prefix="/api/email", tags=["email"], dependencies=_auth)
    app.include_router(approvals.router, prefix="/api/approvals", tags=["approvals"], dependencies=_auth)
    app.include_router(audit.router, prefix="/api/audit", tags=["audit"], dependencies=_auth)
    app.include_router(auto_approval.router, prefix="/api/auto-approval-rules", tags=["auto-approval"], dependencies=_auth)
    app.include_router(search.router, prefix="/api/search", tags=["search"], dependencies=_auth)
    app.include_router(web_search.router, prefix="/api/web-search", tags=["web-search"], dependencies=_auth)
    app.include_router(normalization.router, prefix="/api/normalization", tags=["normalization"], dependencies=_auth)
    app.include_router(tables.router, prefix="/api/tables", tags=["tables"], dependencies=_auth)
    app.include_router(suppliers.router, prefix="/api/suppliers", tags=["suppliers"], dependencies=_auth)
    app.include_router(canonical.router, prefix="/api/canonical", tags=["canonical"], dependencies=_auth)
    app.include_router(collections.router, prefix="/api/collections", tags=["collections"], dependencies=_auth)
    app.include_router(anomalies.router, prefix="/api/anomalies", tags=["anomalies"], dependencies=_auth)
    app.include_router(compare.router, prefix="/api/compare", tags=["compare"], dependencies=_auth)
    app.include_router(calendar.router, prefix="/api/calendar", tags=["calendar"], dependencies=_auth)
    app.include_router(
        agent_control_plane.router,
        prefix="/api/agent",
        tags=["agent-control-plane"],
        dependencies=_auth,
    )
    app.include_router(ai_settings.router, prefix="/api/ai", tags=["ai"], dependencies=_auth)
    app.include_router(local_models_api.router, prefix="/api/local-models", tags=["local-models"], dependencies=_auth)
    app.include_router(providers_api.router, prefix="/api/providers", tags=["providers"], dependencies=_auth)
    app.include_router(agent_actions.router, prefix="/api/agent-actions", tags=["agent"], dependencies=_auth)
    app.include_router(export.router, prefix="/api", tags=["export"], dependencies=_auth)
    app.include_router(draft_email.router, prefix="/api/draft-emails", tags=["email"], dependencies=_auth)
    app.include_router(quarantine.router, prefix="/api/quarantine", tags=["quarantine"], dependencies=_auth)
    app.include_router(dashboard.router, prefix="/api/dashboard", tags=["dashboard"], dependencies=_auth)
    app.include_router(warehouse.router, prefix="/api/warehouse", tags=["warehouse"], dependencies=_auth)
    app.include_router(procurement.router, prefix="/api", tags=["procurement"], dependencies=_auth)
    app.include_router(payments.router, prefix="/api", tags=["payments"], dependencies=_auth)
    app.include_router(boms.router, prefix="/api", tags=["boms"], dependencies=_auth)
    app.include_router(scenarios.router, prefix="/api/scenarios", tags=["agent"], dependencies=_auth)
    app.include_router(graph.router, prefix="/api/graph", tags=["graph"], dependencies=_auth)
    app.include_router(memory.router, prefix="/api/memory", tags=["memory"], dependencies=_auth)
    app.include_router(technology.router, prefix="/api/technology", tags=["technology"], dependencies=_auth)
    app.include_router(ntd.router, prefix="/api", tags=["ntd"], dependencies=_auth)
    app.include_router(telegram.router, prefix="/api/telegram", tags=["telegram"], dependencies=_auth)
    app.include_router(canvas.router, prefix="/api/canvas", tags=["canvas"], dependencies=_auth)
    app.include_router(workspace.router, prefix="/api/workspace", tags=["workspace"], dependencies=_auth)
    app.include_router(workspace_export.router, prefix="/api/workspace", tags=["workspace"], dependencies=_auth)
    app.include_router(spec_tables.router, prefix="/api/workspace", tags=["workspace"], dependencies=_auth)
    app.include_router(sheets.router, prefix="/api/workspace", tags=["workspace"], dependencies=_auth)
    app.include_router(mailbox.router, prefix="/api/mailbox", tags=["mailbox"], dependencies=_auth)
    app.include_router(
        email_templates.router,
        prefix="/api/email-templates",
        tags=["email-templates"],
        dependencies=_auth,
    )
    app.include_router(drawings.router, prefix="/api/drawings", tags=["drawings"], dependencies=_auth)
    app.include_router(tool_catalog.router, prefix="/api/tool-catalog", tags=["tool-catalog"], dependencies=_auth)
    app.include_router(cases.router, tags=["cases"], dependencies=_auth)
    app.include_router(chat_sessions.router, prefix="/api/chat", tags=["chat"], dependencies=_auth)
    app.include_router(dynamic_skill_runner.router, tags=["agent-generated"], dependencies=_auth)
    app.include_router(capability_router, prefix="/api/agent", tags=["capabilities"], dependencies=_auth)
    app.include_router(admin_api.router, dependencies=_auth)
    app.include_router(maintenance_api.router, dependencies=_auth)
    app.include_router(admin_graph_api.router, dependencies=_auth)
    app.include_router(rooms.router, prefix="/api/rooms", tags=["rooms"], dependencies=_auth)
    app.include_router(notifications.router, prefix="/api/notifications", tags=["notifications"], dependencies=_auth)
    app.include_router(handovers.router, prefix="/api/handovers", tags=["handovers"], dependencies=_auth)
    app.include_router(setup_api.router, prefix="/api/setup", tags=["setup"], dependencies=_auth)
    app.include_router(comments_api.router, prefix="/api/comments", tags=["comments"], dependencies=_auth)
    app.include_router(devices_api.router, prefix="/api/devices", tags=["devices"], dependencies=_auth)
    app.include_router(mobile_build_api.router, prefix="/api/mobile-build", tags=["mobile-build"], dependencies=_auth)
    app.include_router(image_generation_api.router, prefix="/api/image-gen", tags=["image-studio"], dependencies=_auth)
    app.include_router(lora_training_api.router, prefix="/api/lora", tags=["lora-training"], dependencies=_auth)
    app.include_router(studio_queue_api.router, prefix="/api/studio", tags=["image-studio"], dependencies=_auth)
    app.include_router(comfyui_admin_api.router, prefix="/api/comfyui-admin", tags=["image-studio"], dependencies=_auth)
    # No router-level `dependencies=_auth` here: the proxy's own routes already
    # call `get_current_user` explicitly (HTTP route via Depends, WS route via
    # Depends too) — this avoids resolving the dependency twice per request
    # and keeps the module self-contained/testable without relying on how
    # it happens to be registered.
    app.include_router(comfyui_proxy_api.router, prefix=comfyui_proxy_api.PROXY_MOUNT, tags=["image-studio"])

    # ── Public mobile-app distribution (no auth) ──────────────────────────────
    # APK + version.json are served at /download; assetlinks.json for Android App Links.
    # Traefik routes /download and /.well-known/assetlinks.json directly to the backend.
    from fastapi.responses import FileResponse, JSONResponse
    from fastapi.staticfiles import StaticFiles

    _releases_dir = settings.releases_dir
    try:
        os.makedirs(_releases_dir, exist_ok=True)
    except OSError:
        pass
    app.mount("/download", StaticFiles(directory=_releases_dir, check_dir=False), name="download")

    @app.get("/.well-known/assetlinks.json", include_in_schema=False)
    async def assetlinks():  # noqa: ANN202
        path = os.path.join(_releases_dir, ".well-known", "assetlinks.json")
        if os.path.isfile(path):
            return FileResponse(path, media_type="application/json")
        return JSONResponse([], status_code=200)

    return app


app = create_app()


@app.get("/metrics", include_in_schema=False,
         dependencies=[Depends(_get_current_user)])
async def metrics_endpoint():
    """Prometheus metrics endpoint."""
    try:
        from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
        from fastapi.responses import Response as _Response
        return _Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
    except ImportError:
        from fastapi.responses import PlainTextResponse
        return PlainTextResponse("# prometheus_client not installed\n")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/ai")
async def health_ai() -> dict:
    """Check Ollama health and available models."""
    from app.ai.ollama_client import check_health
    return await check_health()


@app.get("/api/tasks/{task_id}", dependencies=[Depends(_get_current_user)])
@app.get("/api/tasks/{task_id}/status", dependencies=[Depends(_get_current_user)])
async def get_task_status(task_id: str) -> dict:
    """Check Celery task status."""
    from app.tasks.celery_app import celery_app as celery

    result = celery.AsyncResult(task_id)
    response: dict = {
        "task_id": task_id,
        "status": result.status,
    }
    if result.ready():
        try:
            response["result"] = result.result
        except Exception:
            response["result"] = None
    return response


@app.get("/api/metrics")
async def get_metrics() -> dict:
    """System metrics: queue depth, document counts, approval wait times, workspace."""
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import func, select
    from app.db.session import _get_session_factory
    from app.db.models import Document, Invoice, Approval, AnomalyCard, DocumentStatus, InvoiceStatus, ApprovalStatus, AnomalyStatus

    metrics: dict = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "documents": {},
        "invoices": {},
        "approvals": {},
        "anomalies": {},
        "workspace": {},
        "queue": {},
    }

    try:
        async with _get_session_factory()() as db:
            # Documents
            doc_total = await db.scalar(select(func.count()).select_from(Document))
            doc_processing = await db.scalar(
                select(func.count()).select_from(Document).where(
                    Document.status.in_([DocumentStatus.extracting, DocumentStatus.classifying])
                )
            )
            doc_needs_review = await db.scalar(
                select(func.count()).select_from(Document).where(
                    Document.status == DocumentStatus.needs_review
                )
            )
            metrics["documents"] = {
                "total": doc_total or 0,
                "processing": doc_processing or 0,
                "needs_review": doc_needs_review or 0,
            }

            # Invoices
            inv_needs_review = await db.scalar(
                select(func.count()).select_from(Invoice).where(
                    Invoice.status == InvoiceStatus.needs_review
                )
            )
            metrics["invoices"] = {"needs_review": inv_needs_review or 0}

            # Approvals — pending + avg wait time
            now = datetime.now(timezone.utc)
            pending_approvals = await db.scalar(
                select(func.count()).select_from(Approval).where(
                    Approval.status == ApprovalStatus.pending
                )
            )
            overdue_approvals = await db.scalar(
                select(func.count()).select_from(Approval).where(
                    Approval.status == ApprovalStatus.pending,
                    Approval.expires_at != None,  # noqa: E711
                    Approval.expires_at < now,
                )
            )
            metrics["approvals"] = {
                "pending": pending_approvals or 0,
                "overdue": overdue_approvals or 0,
            }

            # Open anomalies
            open_anomalies = await db.scalar(
                select(func.count()).select_from(AnomalyCard).where(
                    AnomalyCard.status == AnomalyStatus.open
                )
            )
            metrics["anomalies"] = {"open": open_anomalies or 0}
    except Exception as e:
        metrics["db_error"] = str(e)

    # Workspace blocks (Redis)
    try:
        from app.utils.redis_client import get_async_redis
        redis = get_async_redis()
        blocks = await redis.hgetall("workspace:blocks")
        metrics["workspace"]["block_count"] = len(blocks)
    except Exception:
        metrics["workspace"]["block_count"] = -1

    # Celery queue depth
    try:
        from app.utils.redis_client import get_async_redis
        redis = get_async_redis()
        metrics["queue"]["ingest"] = await redis.llen("ingest") or 0
        metrics["queue"]["extraction"] = await redis.llen("extraction") or 0
        metrics["queue"]["celery"] = await redis.llen("celery") or 0
    except Exception:
        pass

    return metrics
