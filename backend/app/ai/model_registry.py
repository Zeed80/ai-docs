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


def _load_thinking_overrides() -> dict[str, bool]:
    try:
        from app.utils.redis_client import get_sync_redis

        raw = get_sync_redis().get(_THINKING_OVERLAY_KEY)
        return json.loads(raw) if raw else {}
    except Exception:
        return {}


def set_thinking_override(model_key: str, enabled: bool) -> None:
    """Persist a per-model thinking toggle (applied on every registry load)."""
    try:
        from app.utils.redis_client import get_sync_redis

        overrides = _load_thinking_overrides()
        overrides[model_key] = bool(enabled)
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
        # Apply per-model thinking toggles from the UI (override YAML defaults).
        for key, enabled in _load_thinking_overrides().items():
            if key in models:
                models[key] = models[key].model_copy(
                    update={"thinking_enabled": bool(enabled), "thinking_supported": True}
                )
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
