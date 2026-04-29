from __future__ import annotations

from functools import lru_cache

from backend.app.ai import AIRouter, ModelRegistry
from backend.app.config import get_settings
from backend.app.domain.storage import LocalFileStorage


@lru_cache
def _get_storage_cached() -> LocalFileStorage:
    settings = get_settings()
    return LocalFileStorage(settings.storage_root)


@lru_cache
def _get_ai_router_cached() -> AIRouter:
    settings = get_settings()
    registry = ModelRegistry.from_yaml(settings.ai_registry_path)
    return AIRouter(registry)


async def get_storage() -> LocalFileStorage:
    return _get_storage_cached()


async def get_ai_router() -> AIRouter:
    return _get_ai_router_cached()
