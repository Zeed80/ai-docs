"""Hardware presets — one-click bundles of task routing + VRAM limits.

A preset is a curated mapping suited to a GPU tier (e.g. "RTX 3090 24GB —
balance"). Applying it writes the listed task routings (via ``task_routing``)
and per-provider VRAM soft limits (via ``gpu_manager``). Tasks not listed keep
their current routing. Defined in ``config/presets.yaml``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import structlog
import yaml

from app.ai.schemas import AITask

logger = structlog.get_logger()

_PRESETS_PATH = "backend/app/ai/config/presets.yaml"


def _load_raw() -> dict[str, Any]:
    path = Path(_PRESETS_PATH)
    if not path.exists() and str(path).startswith("backend/"):
        path = Path(str(path).removeprefix("backend/"))
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")).get("presets", {})
    except Exception as exc:
        logger.warning("presets_load_failed", error=str(exc))
        return {}


def list_presets() -> list[dict[str, Any]]:
    return [
        {
            "name": name,
            "label": p.get("label", name),
            "description": p.get("description", ""),
            "vram_limits": p.get("vram_limits", {}),
            "tasks": sorted((p.get("routing") or {}).keys()),
        }
        for name, p in _load_raw().items()
    ]


def get_preset(name: str) -> dict[str, Any] | None:
    return _load_raw().get(name)


def apply_preset(name: str) -> dict[str, Any]:
    """Apply a preset: write task routings + VRAM limits. Returns a summary."""
    preset = get_preset(name)
    if preset is None:
        raise ValueError(f"Unknown preset: {name}")

    from app.ai import gpu_manager
    from app.ai.task_routing import get_routing_for, save_task_routing

    applied: list[str] = []
    skipped: list[str] = []

    for task_value, cfg in (preset.get("routing") or {}).items():
        try:
            task = AITask(task_value)
        except ValueError:
            skipped.append(task_value)
            continue
        base = get_routing_for(task)
        routing = base.model_copy(
            update={
                "models": cfg.get("models", base.models),
                "profile": cfg.get("profile", base.profile),
            }
        )
        try:
            save_task_routing(task, routing)  # validates + enforces confidentiality
            applied.append(task_value)
        except ValueError as exc:
            logger.warning("preset_routing_invalid", task=task_value, error=str(exc))
            skipped.append(task_value)

    vram = preset.get("vram_limits") or {}
    if vram:
        try:
            current = gpu_manager._load_vram_limits()
            current.update({k: float(v) for k, v in vram.items()})
            gpu_manager.save_vram_limits(current)
        except Exception as exc:
            logger.warning("preset_vram_apply_failed", error=str(exc))

    logger.info("preset_applied", name=name, applied=applied, skipped=skipped)
    return {"name": name, "applied": applied, "skipped": skipped, "vram_limits": vram}
