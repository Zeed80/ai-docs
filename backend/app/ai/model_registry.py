from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import structlog
import yaml

from app.ai.schemas import (
    AITask,
    ModelCapability,
    ModelStatus,
    ProviderConfig,
    ProviderKind,
    RegistrySnapshot,
    TaskRoute,
)

logger = structlog.get_logger()

# Redis overlay key for models added at runtime (downloaded/registered via the
# Библиотека UI). Merged on top of the YAML catalog so runtime models become
# selectable in routing without editing the file.
_CATALOG_OVERLAY_KEY = "model_catalog_overlay"
# Per-model thinking toggle overrides ({model_key: bool}). Kept separate from the
# full-model overlay so a YAML model's CoT flag can be flipped from the UI without
# shadowing the rest of its (canonical) YAML definition.
_THINKING_OVERLAY_KEY = "model_thinking_overrides"


def _load_catalog_overlay() -> dict[str, dict[str, Any]]:
    """Return runtime-added model entries keyed by model name."""
    try:
        from app.utils.redis_client import get_sync_redis

        raw = get_sync_redis().get(_CATALOG_OVERLAY_KEY)
        return json.loads(raw) if raw else {}
    except Exception:
        return {}


def _save_catalog_overlay(overlay: dict[str, dict[str, Any]]) -> None:
    try:
        from app.utils.redis_client import get_sync_redis

        get_sync_redis().set(_CATALOG_OVERLAY_KEY, json.dumps(overlay, ensure_ascii=False))
    except Exception as exc:
        logger.warning("model_catalog_overlay_write_failed", error=str(exc))


def _load_thinking_overrides() -> dict[str, dict[str, Any]]:
    """Return per-model thinking overrides, normalized to
    ``{enabled, level, levels}``.

    Legacy entries are a plain ``bool`` (pre-level format); mid-generation
    entries are ``{"enabled": bool, "level": str|None}``; current entries
    add ``"levels": list[str]|None`` — the live-probed/manually-determined
    reasoning-effort level LIST (distinct from ``level``, the single
    default-pick). ``levels`` is the mechanism that lets a probe result
    apply to a ``model_registry.yaml``-defined model too (unlike the
    catalog overlay, which YAML always wins over) — see
    ``ModelCapability.thinking_levels_probed`` / the apply-loop below.
    All shapes must keep loading forever — this overlay is rehydrated from
    Postgres on every startup, so old rows never get a backfill pass.
    """
    try:
        from app.utils.redis_client import get_sync_redis

        raw = get_sync_redis().get(_THINKING_OVERLAY_KEY)
        data = json.loads(raw) if raw else {}
    except Exception:
        return {}
    normalized: dict[str, dict[str, Any]] = {}
    for key, value in data.items():
        if isinstance(value, bool):
            normalized[key] = {"enabled": value, "level": None, "levels": None}
        elif isinstance(value, dict):
            normalized[key] = {
                "enabled": value.get("enabled"),
                "level": value.get("level"),
                "levels": value.get("levels"),
            }
    return normalized


def set_thinking_override(
    model_key: str,
    enabled: bool | None = None,
    level: str | None = None,
    levels: list[str] | None = None,
) -> None:
    """Persist per-model thinking-override fields (applied on every registry
    load). Any of ``enabled``/``level``/``levels`` may be omitted (``None``)
    to leave that aspect untouched — e.g. the discovery loop's automatic
    level probe writes only ``levels``, never touching a human's
    enabled/level choice for the same model.
    """
    try:
        from app.utils.redis_client import get_sync_redis

        overrides = _load_thinking_overrides()
        current = overrides.get(model_key, {"enabled": None, "level": None, "levels": None})
        if enabled is not None:
            current["enabled"] = bool(enabled)
        if level is not None:
            current["level"] = level
        if levels is not None:
            current["levels"] = levels
        overrides[model_key] = current
        get_sync_redis().set(_THINKING_OVERLAY_KEY, json.dumps(overrides, ensure_ascii=False))
    except Exception as exc:
        logger.warning("model_thinking_override_write_failed", error=str(exc))


# Per-model pin to a provider node ({model_key: instance_name|""}).
_PREFERRED_INSTANCE_KEY = "model_preferred_instances"


def _load_preferred_instances() -> dict[str, str]:
    try:
        from app.utils.redis_client import get_sync_redis

        raw = get_sync_redis().get(_PREFERRED_INSTANCE_KEY)
        return json.loads(raw) if raw else {}
    except Exception:
        return {}


def set_preferred_instance(model_key: str, instance_name: str | None) -> None:
    """Pin a model to a provider node (instance name), or clear with None/""."""
    try:
        from app.utils.redis_client import get_sync_redis

        prefs = _load_preferred_instances()
        if instance_name:
            prefs[model_key] = instance_name
        else:
            prefs.pop(model_key, None)
        get_sync_redis().set(_PREFERRED_INSTANCE_KEY, json.dumps(prefs, ensure_ascii=False))
    except Exception as exc:
        logger.warning("model_preferred_instance_write_failed", error=str(exc))


class ModelRegistry:
    def __init__(
        self,
        providers: dict[ProviderKind, ProviderConfig],
        models: dict[str, ModelCapability],
        routes: dict[AITask, TaskRoute],
    ) -> None:
        self.providers = providers
        self.models = models
        self.routes = routes

    @classmethod
    def from_yaml(cls, path: str | Path) -> "ModelRegistry":
        registry_path = Path(path)
        if not registry_path.exists() and str(registry_path).startswith("backend/"):
            registry_path = Path(str(registry_path).removeprefix("backend/"))
        raw = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
        providers = {
            ProviderKind(key): ProviderConfig(kind=ProviderKind(key), **value)
            for key, value in raw.get("providers", {}).items()
        }
        raw_models = dict(raw.get("models", {}))
        # Merge runtime-added models (downloaded/registered via the Библиотека UI)
        # on top of the YAML catalog. YAML stays the canonical defaults source.
        for key, value in _load_catalog_overlay().items():
            raw_models.setdefault(key, value)
        models = {
            key: ModelCapability(name=key, **value)
            for key, value in raw_models.items()
        }
        # Apply per-model thinking toggles/levels from the UI or the
        # discovery loop's live probe (override YAML defaults). This is the
        # ONLY mechanism that can attach a level determination to a
        # model_registry.yaml-defined entry — the catalog overlay above
        # (_load_catalog_overlay) uses setdefault, so it can never touch an
        # already-YAML-defined key; a curated model's levels can only ever
        # be updated through this override path.
        for key, override in _load_thinking_overrides().items():
            if key not in models:
                continue
            update: dict[str, Any] = {}
            if override.get("enabled") is not None:
                update["thinking_enabled"] = bool(override["enabled"])
                update["thinking_supported"] = True
            # levels is not None → a determination (positive or negative)
            # has been made, live-probed or manually curated; None → never
            # determined via this path, defer to the catalog/YAML value.
            effective_levels = (
                override["levels"] if override.get("levels") is not None else models[key].thinking_levels
            )
            if override.get("levels") is not None:
                update["thinking_levels"] = override["levels"]
                update["thinking_levels_probed"] = True
            # An explicit default-level pick only takes effect if the model
            # actually supports it — be defensive against stale/hand-edited data.
            if override.get("level") and override["level"] in effective_levels:
                update["thinking_level_default"] = override["level"]
            if update:
                models[key] = models[key].model_copy(update=update)
        # Apply per-model node pins from the UI.
        for key, inst in _load_preferred_instances().items():
            if key in models:
                models[key] = models[key].model_copy(update={"preferred_instance": inst})
        routes = {
            AITask(key): TaskRoute(task=AITask(key), **value)
            for key, value in raw.get("routes", {}).items()
        }
        return cls(providers=providers, models=models, routes=routes)

    def add_model(self, key: str, capability: ModelCapability, *, persist: bool = True) -> None:
        """Register a model in the catalog at runtime and persist it to the overlay.

        Used when a model is downloaded/activated in the Библиотека UI so it
        becomes selectable in task routing without editing the YAML file.
        """
        self.models[key] = capability
        if persist:
            overlay = _load_catalog_overlay()
            overlay[key] = capability.model_dump(mode="json", exclude={"name"})
            _save_catalog_overlay(overlay)

    def snapshot(self) -> RegistrySnapshot:
        return RegistrySnapshot(providers=self.providers, models=self.models, routes=self.routes)

    def get_route(self, task: AITask) -> TaskRoute:
        try:
            return self.routes[task]
        except KeyError as exc:
            raise KeyError(f"No AI route configured for task {task.value}") from exc

    def get_model(self, model_name: str) -> ModelCapability:
        try:
            return self.models[model_name]
        except KeyError as exc:
            raise KeyError(f"Unknown model {model_name}") from exc

    def production_models_for_task(self, task: AITask) -> list[ModelCapability]:
        route = self.get_route(task)
        return [
            self.models[name]
            for name in route.fallback_chain
            if name in self.models and self.models[name].status == ModelStatus.PRODUCTION
        ]

    def promote_model(self, model_name: str, status: ModelStatus) -> None:
        model = self.get_model(model_name)
        self.models[model_name] = model.model_copy(update={"status": status})

    def as_yaml_dict(self) -> dict[str, Any]:
        return {
            "providers": {
                key.value: value.model_dump(mode="json", exclude={"kind"})
                for key, value in self.providers.items()
            },
            "models": {
                key: value.model_dump(mode="json", exclude={"name"})
                for key, value in self.models.items()
            },
            "routes": {
                key.value: value.model_dump(mode="json", exclude={"task"})
                for key, value in self.routes.items()
            },
        }
