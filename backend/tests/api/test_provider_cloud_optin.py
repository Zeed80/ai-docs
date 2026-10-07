"""Per-slot cloud opt-in + downloaded local-model visibility."""

import pytest

from app.api import providers_api as p


class _NoDb:
    async def commit(self):
        return None


def _no_persist(monkeypatch):
    async def persist(_db):
        return None

    monkeypatch.setattr(p, "_persist_agent_config", persist)


def test_confidential_slot_defaults_to_local_only(monkeypatch):
    monkeypatch.setattr(p, "_cloud_allowed_slots", lambda: set())
    # cad_spec_read is a confidential (base local_only) slot.
    assert p._slot_base_local_only("cad_spec_read") is True
    assert p._slot_effective_local_only("cad_spec_read") is True


def test_confidential_slot_opened_to_cloud(monkeypatch):
    """Разрешение открывает слот — любой, включая читающие содержимое.

    Что при этом уходит наружу и как решение доходит до вызова, проверяет
    test_slot_cloud_policy.py.
    """
    monkeypatch.setattr(p, "_cloud_allowed_slots", lambda: {"agent_orchestrator"})
    assert p._slot_effective_local_only("agent_orchestrator") is False


def test_every_content_bearing_slot_is_local_until_opened(monkeypatch):
    """Раньше agent_email не считался конфиденциальным, и облачная модель
    попадала на генерацию деловых писем обычным выбором из списка — без
    подтверждения и без следа. Теперь письма, как и остальные слоты, через
    которые проходит содержимое документов, локальны до явного разрешения."""
    monkeypatch.setattr(p, "_cloud_allowed_slots", lambda: set())
    for slot in ("agent_email", "agent_orchestrator", "agent_fast", "agent_large"):
        assert p._slot_base_local_only(slot) is True, slot
        assert p._slot_effective_local_only(slot) is True, slot


def test_email_slot_can_still_be_opened_to_cloud(monkeypatch):
    """Это не запрет: CLAUDE.md разрешает cloud-модели для planner/auditor и
    генерации писем. Возможность сохраняется, но включается осознанно."""
    monkeypatch.setattr(p, "_cloud_allowed_slots", lambda: {"agent_email"})
    assert p._slot_effective_local_only("agent_email") is False


def test_slot_out_reports_effective_policy_and_flags(monkeypatch):
    monkeypatch.setattr(p, "_cloud_allowed_slots", lambda: {"agent_orchestrator"})
    registry = p._registry()
    out = p._build_slot_out(
        "agent_orchestrator",
        "Агент",
        "Оркестратор",
        "hint",
        True,
        None,
        registry,
        current_model=None,
        cloud_slots={"agent_orchestrator"},
    )
    assert out.local_only is False  # effective: opened
    assert out.cloud_optionable is True  # base is confidential
    assert out.cloud_allowed is True


@pytest.mark.asyncio
async def test_allow_cloud_endpoint_toggles_confidential_slot(monkeypatch):
    calls = {}
    monkeypatch.setattr(p, "_set_slot_cloud_allowed", lambda s, a: calls.update(slot=s, allowed=a))
    _no_persist(monkeypatch)
    res = await p.set_slot_allow_cloud(
        "agent_orchestrator", p.SlotCloudWrite(allowed=True), db=_NoDb()
    )
    assert res["cloud_allowed"] is True
    assert calls == {"slot": "agent_orchestrator", "allowed": True}


@pytest.mark.asyncio
async def test_allow_cloud_endpoint_records_the_choice_for_the_email_slot(monkeypatch):
    """Разрешение для писем теперь именно сохраняется, а не оказывается no-op:
    до этого слот и так пускал облако, поэтому фиксировать было нечего."""
    calls = {}
    monkeypatch.setattr(p, "_set_slot_cloud_allowed", lambda s, a: calls.update(slot=s, allowed=a))
    _no_persist(monkeypatch)
    res = await p.set_slot_allow_cloud("agent_email", p.SlotCloudWrite(allowed=True), db=_NoDb())
    assert res["cloud_allowed"] is True
    assert calls == {"slot": "agent_email", "allowed": True}


@pytest.mark.asyncio
async def test_allow_cloud_endpoint_rejects_an_unknown_slot():
    with pytest.raises(Exception):
        await p.set_slot_allow_cloud("no_such_slot", p.SlotCloudWrite(allowed=True), db=_NoDb())


def test_the_choice_and_the_auditor_mirror_live_in_the_agent_config():
    from app.ai.agent_config import get_builtin_agent_config

    p._set_slot_cloud_allowed("agent_auditor", True)
    config = get_builtin_agent_config()
    assert "agent_auditor" in config.cloud_allowed_slots
    assert config.auditor_allow_cloud is True
    assert p._cloud_allowed_slots() == set(config.cloud_allowed_slots)

    p._set_slot_cloud_allowed("agent_auditor", False)
    config = get_builtin_agent_config()
    assert "agent_auditor" not in config.cloud_allowed_slots
    assert config.auditor_allow_cloud is False


@pytest.mark.asyncio
async def test_the_choice_survives_the_startup_hydrate(db_session):
    """auditor_allow_cloud lived in Redis only and the startup hydrate put the
    stale Postgres copy back, so the auditor silently went local again; the
    slot set itself had no durable copy at all (found 2026-10-07)."""
    from app.ai.agent_config import get_builtin_agent_config
    from app.ai.model_runtime_store import hydrate_runtime_cache, persist_agent_config

    stale = get_builtin_agent_config().model_dump(mode="json")
    await persist_agent_config(db_session, config=stale)
    await db_session.flush()

    await p.set_slot_allow_cloud("agent_auditor", p.SlotCloudWrite(allowed=True), db=db_session)
    await hydrate_runtime_cache(db_session)

    config = get_builtin_agent_config()
    assert config.auditor_allow_cloud is True
    assert "agent_auditor" in p._cloud_allowed_slots()
    p._set_slot_cloud_allowed("agent_auditor", False)


@pytest.mark.asyncio
async def test_a_choice_kept_only_in_redis_is_carried_over_once(db_session):
    from app.ai.agent_config import get_builtin_agent_config
    from app.utils.redis_client import get_sync_redis

    get_sync_redis().sadd(p._SLOT_CLOUD_KEY, "ocr_large")
    assert await p.migrate_slot_cloud_choice(db_session) == ["ocr_large"]
    assert "ocr_large" in get_builtin_agent_config().cloud_allowed_slots
    assert not get_sync_redis().exists(p._SLOT_CLOUD_KEY)
    assert await p.migrate_slot_cloud_choice(db_session) == []
    p._set_slot_cloud_allowed("ocr_large", False)
