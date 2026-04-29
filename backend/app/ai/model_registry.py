from __future__ import annotations

from pathlib import Path
from typing import Any

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
        models = {
            key: ModelCapability(name=key, **value)
            for key, value in raw.get("models", {}).items()
        }
        routes = {
            AITask(key): TaskRoute(task=AITask(key), **value)
            for key, value in raw.get("routes", {}).items()
        }
        return cls(providers=providers, models=models, routes=routes)

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
