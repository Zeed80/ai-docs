"""Рассуждение: одна полярность и честность про расхождение.

Поля ролей называются `*_disable_thinking` — единственное место в системе с
обратной полярностью: в каталоге моделей и в маршрутизации задач тот же смысл
выражен прямо. Двойное отрицание уже приводило к путанице, поэтому вызывающие
работают через аксессоры.

Отдельно: за одним переключателем стоит несколько полей (слот «Оркестратор»
пишет и в orchestrator, и в worker; «Извлечение полей» — в две задачи), а
читалось только первое. Разошедшиеся значения были невидимы.
"""

import pytest

from app.ai.agent_config import (
    BuiltinAgentConfig,
    thinking_enabled_for,
    thinking_level_for,
    with_thinking_enabled_for,
)
from app.api import providers_api as p

# ── Аксессоры без отрицания ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "stored, expected",
    [(None, None), (True, False), (False, True)],
)
def test_reading_a_role_never_requires_inverting_by_hand(stored, expected):
    cfg = BuiltinAgentConfig(orchestrator_disable_thinking=stored)
    assert thinking_enabled_for(cfg, "orchestrator") is expected


@pytest.mark.parametrize("enabled", [True, False, None])
def test_writing_a_role_round_trips(enabled):
    cfg = with_thinking_enabled_for(BuiltinAgentConfig(), "worker", enabled)
    assert thinking_enabled_for(cfg, "worker") is enabled


def test_unknown_role_is_ignored_rather_than_crashing():
    cfg = BuiltinAgentConfig()
    assert thinking_enabled_for(cfg, "нет-такой-роли") is None
    assert with_thinking_enabled_for(cfg, "нет-такой-роли", True) == cfg
    assert thinking_level_for(cfg, "нет-такой-роли") is None


def test_every_role_field_exists_on_the_config():
    """Опечатка в имени поля молча превращала бы переключатель в no-op."""
    from app.ai.agent_config import ROLE_THINKING_FIELDS, ROLE_THINKING_LEVEL_FIELDS

    fields = set(BuiltinAgentConfig.model_fields)
    for name in (*ROLE_THINKING_FIELDS.values(), *ROLE_THINKING_LEVEL_FIELDS.values()):
        assert name in fields, name


# ── Расхождение полей за одним переключателем ────────────────────────────────


def test_diverged_agent_roles_are_reported_not_hidden(monkeypatch):
    monkeypatch.setattr(
        "app.ai.agent_config.get_builtin_agent_config",
        lambda: BuiltinAgentConfig(
            orchestrator_disable_thinking=True,  # рассуждение выключено
            worker_disable_thinking=False,  # а здесь включено
        ),
    )
    assert p._slot_thinking_values("agent_orchestrator") == [False, True]
    assert p._slot_thinking_mixed("agent_orchestrator") is True
    # Переключатель по-прежнему показывает первое поле — но теперь рядом
    # стоит признак, что значения разошлись.
    assert p._slot_thinking_override("agent_orchestrator") is False


def test_agreeing_roles_are_not_flagged(monkeypatch):
    monkeypatch.setattr(
        "app.ai.agent_config.get_builtin_agent_config",
        lambda: BuiltinAgentConfig(
            orchestrator_disable_thinking=False,
            worker_disable_thinking=False,
        ),
    )
    assert p._slot_thinking_mixed("agent_orchestrator") is False


def test_a_slot_without_reasoning_has_nothing_to_diverge():
    """embedding/rerank рассуждения не поддерживают — переключателя нет."""
    assert p._slot_thinking_values("embedding") == []
    assert p._slot_thinking_mixed("embedding") is False


def test_slot_out_carries_the_divergence_flag(monkeypatch):
    monkeypatch.setattr(
        "app.ai.agent_config.get_builtin_agent_config",
        lambda: BuiltinAgentConfig(
            orchestrator_disable_thinking=True,
            worker_disable_thinking=False,
        ),
    )
    state = p._slot_thinking_state("agent_orchestrator", p._registry(), None)
    assert state["thinking_mixed"] is True
