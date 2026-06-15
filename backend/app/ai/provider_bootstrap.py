"""Seed provider_instances from the YAML registry on first run.

Creates one node per provider kind defined in ``model_registry.yaml`` so the UI
shows every provider ready to configure (local nodes with their default URL,
cloud providers awaiting an API key). Existing rows are never overwritten, so a
user's edits and extra nodes survive restarts. Cloud API keys present in the
environment are migrated into the encrypted store on the first seed only.
"""

from __future__ import annotations

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import provider_registry
from app.ai.model_registry import ModelRegistry
from app.ai.schemas import ProviderKind
from app.ai.secret_box import encrypt
from app.db.models import ProviderInstance

logger = structlog.get_logger()

# Placeholder kinds that should not be seeded as real nodes.
_SKIP_KINDS = {ProviderKind.CLOUD_PROVIDER}


async def seed_and_refresh_providers(db: AsyncSession) -> dict:
    registry = ModelRegistry.from_yaml("backend/app/ai/config/model_registry.yaml")
    existing = await db.execute(select(ProviderInstance.kind))
    existing_kinds = {row[0] for row in existing.all()}

    created: list[str] = []
    for kind, cfg in registry.providers.items():
        if kind in _SKIP_KINDS or kind.value in existing_kinds:
            continue
        # Migrate an env-provided cloud key into the encrypted store once.
        env_key = provider_registry._env_key_for(kind)  # noqa: SLF001
        inst = ProviderInstance(
            kind=kind.value,
            name=f"{kind.value} (default)",
            base_url=None,  # inherit YAML/env default at resolve time
            enabled=True,
            is_local=cfg.is_local,
            api_key_encrypted=encrypt(env_key) if (env_key and not cfg.is_local) else None,
        )
        db.add(inst)
        created.append(kind.value)

    if created:
        await db.commit()
        logger.info("provider_instances_seeded", kinds=created)

    await provider_registry.refresh_cache_from_db(db)
    return {"created": created}
