"""Seed the builtin ComfyUI workflow library from JSON templates on startup.

Templates live in ``aiagent/config/comfyui_workflows/*.json`` (data, not code) so
they can be tuned without touching Python. Builtins are kept in sync on every
startup (insert if missing, refresh the builtin row otherwise). Users never edit
a builtin in place — they duplicate it (``is_builtin=False``), and those user
rows are left untouched here.
"""

from __future__ import annotations

import json
from pathlib import Path

import structlog

logger = structlog.get_logger()

_REL = "aiagent/config/comfyui_workflows"


def _workflow_dir() -> Path:
    """Locate the templates dir robustly, regardless of the process cwd.

    Runtime cwd is the repo root (like other config paths), but tests run from
    ``backend/``. Resolve from this module's location first
    (backend/app/db/seeds/… → repo root), then fall back to cwd-relative.
    """
    here = Path(__file__).resolve()
    # parents: seeds → db → app → backend → repo root
    candidates = [here.parents[4] / _REL, Path(_REL)]
    for cand in candidates:
        if cand.is_dir():
            return cand
    return candidates[0]

_FIELDS = (
    "title", "description", "category", "operation",
    "graph", "inject_map", "params_schema",
)


def _load_templates() -> list[dict]:
    templates: list[dict] = []
    wf_dir = _workflow_dir()
    if not wf_dir.is_dir():
        return templates
    for path in sorted(wf_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("key"):
                templates.append(data)
        except Exception as exc:  # noqa: BLE001
            logger.warning("comfyui_workflow_template_bad", file=str(path), error=str(exc))
    return templates


async def seed_builtin_workflows(db) -> None:
    """Insert/refresh builtin workflows. Safe to call on every startup."""
    from sqlalchemy import select

    from app.db.models import ComfyWorkflow

    templates = _load_templates()
    shipped_keys = {t["key"] for t in templates}

    # Prune builtin rows whose template was removed/renamed (keep user copies).
    stale = (
        await db.execute(
            select(ComfyWorkflow).where(ComfyWorkflow.is_builtin.is_(True))
        )
    ).scalars().all()
    changed = False
    for row in stale:
        if row.key not in shipped_keys:
            await db.delete(row)
            changed = True

    for tpl in templates:
        existing = (
            await db.execute(
                select(ComfyWorkflow).where(
                    ComfyWorkflow.key == tpl["key"],
                    ComfyWorkflow.is_builtin.is_(True),
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            db.add(
                ComfyWorkflow(
                    key=tpl["key"],
                    is_builtin=True,
                    enabled=True,
                    owner_sub=None,
                    title=tpl.get("title", tpl["key"]),
                    description=tpl.get("description"),
                    category=tpl.get("category", "edit"),
                    operation=tpl.get("operation", "edit"),
                    graph=tpl.get("graph", {}),
                    inject_map=tpl.get("inject_map", {}),
                    params_schema=tpl.get("params_schema", {}),
                )
            )
            changed = True
        else:
            # Keep the builtin in sync with the shipped template.
            for field in _FIELDS:
                if field in tpl and getattr(existing, field) != tpl[field]:
                    setattr(existing, field, tpl[field])
                    changed = True

    if changed:
        await db.commit()
        logger.info("comfyui_workflows_seeded")
