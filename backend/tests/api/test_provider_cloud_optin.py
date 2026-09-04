"""Per-slot cloud opt-in + downloaded local-model visibility."""

import pytest

from app.api import providers_api as p


def test_confidential_slot_defaults_to_local_only(monkeypatch):
    monkeypatch.setattr(p, "_cloud_allowed_slots", lambda: set())
    # cad_spec_read is a confidential (base local_only) slot.
    assert p._slot_base_local_only("cad_spec_read") is True
    assert p._slot_effective_local_only("cad_spec_read") is True


def test_confidential_slot_opened_to_cloud(monkeypatch):
    monkeypatch.setattr(p, "_cloud_allowed_slots", lambda: {"cad_spec_read"})
    assert p._slot_effective_local_only("cad_spec_read") is False


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
    monkeypatch.setattr(p, "_cloud_allowed_slots", lambda: {"cad_spec_read"})
    registry = p._registry()
    out = p._build_slot_out(
        "cad_spec_read", "Оцифровка", "Чтение чертежа", "hint",
        True, None, registry, current_model=None, cloud_slots={"cad_spec_read"},
    )
    assert out.local_only is False       # effective: opened
    assert out.cloud_optionable is True  # base is confidential
    assert out.cloud_allowed is True


@pytest.mark.asyncio
async def test_allow_cloud_endpoint_toggles_confidential_slot(monkeypatch):
    calls = {}
    monkeypatch.setattr(p, "_set_slot_cloud_allowed", lambda s, a: calls.update(slot=s, allowed=a))
    res = await p.set_slot_allow_cloud("cad_spec_read", p.SlotCloudWrite(allowed=True))
    assert res["cloud_allowed"] is True
    assert calls == {"slot": "cad_spec_read", "allowed": True}


@pytest.mark.asyncio
async def test_allow_cloud_endpoint_records_the_choice_for_the_email_slot(monkeypatch):
    """Разрешение для писем теперь именно сохраняется, а не оказывается no-op:
    до этого слот и так пускал облако, поэтому фиксировать было нечего."""
    calls = {}
    monkeypatch.setattr(p, "_set_slot_cloud_allowed", lambda s, a: calls.update(slot=s, allowed=a))
    res = await p.set_slot_allow_cloud("agent_email", p.SlotCloudWrite(allowed=True))
    assert res["cloud_allowed"] is True
    assert calls == {"slot": "agent_email", "allowed": True}


@pytest.mark.asyncio
async def test_allow_cloud_endpoint_rejects_an_unknown_slot():
    with pytest.raises(Exception):
        await p.set_slot_allow_cloud("no_such_slot", p.SlotCloudWrite(allowed=True))
