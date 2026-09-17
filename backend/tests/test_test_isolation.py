"""Тесты не пишут в чужие и боевые хранилища.

На хосте redis://localhost:6379 — Redis чужого стека (china-key-learning):
тесты настроек клали туда ai_config и agent_config. В контейнере REDIS_URL и
/app/data — боевые. Эти проверки держат изоляцию из conftest.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from httpx import AsyncClient

_REPO_DATA = Path(__file__).resolve().parents[1] / "data"


def test_redis_url_never_points_at_a_shared_redis():
    from app.config import settings

    assert "localhost:6379" not in settings.redis_url
    assert "redis:6379" not in settings.redis_url


def test_app_redis_clients_are_in_memory_and_fresh_per_test():
    from app.utils.redis_client import get_sync_redis

    client = get_sync_redis()
    assert type(client).__module__.startswith("fakeredis")
    assert client.get("isolation-probe") is None
    client.set("isolation-probe", "1")


def test_a_key_from_the_previous_test_is_gone():
    from app.utils.redis_client import get_sync_redis

    assert get_sync_redis().get("isolation-probe") is None


@pytest.mark.asyncio
async def test_config_patch_writes_only_the_test_copy(client: AsyncClient, tmp_path):
    from app.api import ai_settings
    from app.utils.redis_client import get_sync_redis

    resp = await client.patch("/api/ai/config", json={"auto_verify_enabled": False})

    assert resp.status_code == 200
    assert Path(ai_settings._CONFIG_FILE).is_relative_to(tmp_path)
    assert Path(ai_settings._CONFIG_FILE).exists()
    assert not Path(ai_settings._CONFIG_FILE).is_relative_to(_REPO_DATA)
    assert get_sync_redis().get("ai_config") is not None  # в памяти, не снаружи


def test_agent_config_and_sandbox_paths_are_in_tmp(tmp_path):
    from app.ai import agent_config, capability_sandbox

    for path in (
        agent_config._CONFIG_FILE,
        capability_sandbox._ROOT,
        capability_sandbox._STAGING_ROOT,
    ):
        assert Path(path).is_relative_to(tmp_path)
