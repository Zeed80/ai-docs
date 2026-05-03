import logging
import os

import structlog
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.db.session import engine  # lazy proxy
from app.api import (
    documents, invoices, email, approvals, search, normalization, tables,
    suppliers, collections, anomalies, compare, calendar, agent, auth, ai_settings,
    agent_actions, export, draft_email, quarantine, dashboard, warehouse,
    procurement, payments, boms, scenarios,
    graph, memory, technology, ntd, telegram,
    canvas, mailbox, email_templates,
    drawings, tool_catalog,
)


structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.dev.ConsoleRenderer() if settings.app_debug else structlog.processors.JSONRenderer(),
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

    try:
        from app.api.telegram import bot_manager
        err = await bot_manager.start()
        if err:
            logger.warning("telegram bot failed to start at startup", error=err)
    except Exception as exc:
        logger.warning("telegram bot init error", error=str(exc))

    try:
        from app.db.session import _get_session_factory
        from app.db.seeds.email_templates import seed_builtin_templates
        async with _get_session_factory()() as db:
            await seed_builtin_templates(db)
    except Exception as exc:
        logger.warning("email_templates_seed_failed", error=str(exc))

    yield

    try:
        from app.api.telegram import bot_manager
        await bot_manager.stop()
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

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Routers
    app.include_router(documents.router, prefix="/api/documents", tags=["documents"])
    app.include_router(invoices.router, prefix="/api/invoices", tags=["invoices"])
    app.include_router(email.router, prefix="/api/email", tags=["email"])
    app.include_router(approvals.router, prefix="/api/approvals", tags=["approvals"])
    app.include_router(search.router, prefix="/api/search", tags=["search"])
    app.include_router(normalization.router, prefix="/api/normalization", tags=["normalization"])
    app.include_router(tables.router, prefix="/api/tables", tags=["tables"])
    app.include_router(suppliers.router, prefix="/api/suppliers", tags=["suppliers"])
    app.include_router(collections.router, prefix="/api/collections", tags=["collections"])
    app.include_router(anomalies.router, prefix="/api/anomalies", tags=["anomalies"])
    app.include_router(compare.router, prefix="/api/compare", tags=["compare"])
    app.include_router(calendar.router, prefix="/api/calendar", tags=["calendar"])
    app.include_router(agent.router, tags=["agent"])
    app.include_router(auth.router, prefix="/api/auth", tags=["auth"])
    app.include_router(ai_settings.router, prefix="/api/ai", tags=["ai"])
    app.include_router(agent_actions.router, prefix="/api/agent-actions", tags=["agent"])
    app.include_router(export.router, prefix="/api", tags=["export"])
    app.include_router(draft_email.router, prefix="/api/draft-emails", tags=["email"])
    app.include_router(quarantine.router, prefix="/api/quarantine", tags=["quarantine"])
    app.include_router(dashboard.router, prefix="/api/dashboard", tags=["dashboard"])
    app.include_router(warehouse.router, prefix="/api/warehouse", tags=["warehouse"])
    app.include_router(procurement.router, prefix="/api", tags=["procurement"])
    app.include_router(payments.router, prefix="/api", tags=["payments"])
    app.include_router(boms.router, prefix="/api", tags=["boms"])
    app.include_router(scenarios.router, prefix="/api/scenarios", tags=["agent"])
    app.include_router(graph.router, prefix="/api/graph", tags=["graph"])
    app.include_router(memory.router, prefix="/api/memory", tags=["memory"])
    app.include_router(technology.router, prefix="/api/technology", tags=["technology"])
    app.include_router(ntd.router, prefix="/api", tags=["ntd"])
    app.include_router(telegram.router, prefix="/api/telegram", tags=["telegram"])
    app.include_router(canvas.router, prefix="/api/canvas", tags=["canvas"])
    app.include_router(mailbox.router, prefix="/api/mailbox", tags=["mailbox"])
    app.include_router(email_templates.router, prefix="/api/email-templates", tags=["email-templates"])
    app.include_router(drawings.router, prefix="/api/drawings", tags=["drawings"])
    app.include_router(tool_catalog.router, prefix="/api/tool-catalog", tags=["tool-catalog"])

    return app


app = create_app()


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/ai")
async def health_ai() -> dict:
    """Check Ollama health and available models."""
    from app.ai.ollama_client import check_health
    return await check_health()


@app.get("/api/tasks/{task_id}")
async def get_task_status(task_id: str) -> dict:
    """Check Celery task status."""
    from app.tasks.celery_app import celery_app as celery

    result = celery.AsyncResult(task_id)
    response = {
        "task_id": task_id,
        "status": result.status,
    }
    if result.ready():
        response["result"] = result.result
    return response
