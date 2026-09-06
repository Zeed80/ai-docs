"""Слоты с конфиденциальными задачами не открываются облаку ничем.

Флаг ``local_only`` в ``_SLOTS`` значил две разные вещи сразу: «облако можно
включить осознанно» (planner, аудитор, письма — CLAUDE.md это разрешает) и
«облако закрыто наглухо» (всё, что читает содержимое документов и чертежей).
Из-за этого экран назначений предлагал облачные модели для чтения чертежа:
черновик проходил валидацию с одними предупреждениями, применялся, а на живой
задаче роутер отвергал модель как неконфиденциальную — и чтение падало
целиком, потому что ошибка политики намеренно не считается сбоем конкретной
модели и локальный фолбэк не пробуется.

Проверяем оба рубежа: интерфейсный (слот не предлагает облако и валидация
даёт ошибку, а не предупреждение) и запрет на выдачу разрешения.
"""

from __future__ import annotations

import asyncio

import pytest

from app.ai.schemas import ModelStatus
from app.ai.task_routing import CONFIDENTIAL_TASKS
from app.api import providers_api as pa


def _confidential_values() -> set[str]:
    return {t.value for t in CONFIDENTIAL_TASKS}


ALL_SLOTS = [slot for slot, *_ in pa._SLOTS]


@pytest.mark.parametrize("slot", ALL_SLOTS)
def test_hard_confidential_matches_task_list(slot: str) -> None:
    """Признак слота выводится из CONFIDENTIAL_TASKS, а не из отдельного списка."""
    expected = any(item.split(" ")[0] in _confidential_values() for item in pa._slot_affected(slot))
    assert pa._slot_hard_confidential(slot) is expected


def test_drawing_and_document_slots_are_closed() -> None:
    """Именно эти слоты и приводили к отказу на живой задаче."""
    for slot in ("cad_spec_read", "cad_text_ocr", "cad_spec_draft", "ocr_fast", "embedding"):
        assert pa._slot_hard_confidential(slot), slot
        assert pa._slot_effective_local_only(slot) is True


def test_agent_slots_stay_cloud_optionable() -> None:
    """Planner/аудитор/письма облако разрешают — запрет не должен задеть их."""
    for slot in ("agent_orchestrator", "agent_auditor", "agent_email", "agent_compression"):
        assert pa._slot_hard_confidential(slot) is False, slot


def test_cloud_opt_in_cannot_open_a_closed_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    """Даже разрешение, уже лежащее в Redis, не открывает такой слот."""
    monkeypatch.setattr(pa, "_cloud_allowed_slots", lambda: {"cad_spec_read"})
    assert pa._slot_effective_local_only("cad_spec_read") is True


class _Cap:
    """Минимальная облачная модель каталога."""

    provider_model = "vendor/model:free"
    local_only = False
    status = ModelStatus.PRODUCTION
    capability_source = "curated"
    capabilities_unknown = False
    modalities: tuple = ()

    class provider:  # noqa: N801 — имитация enum-поля каталога
        value = "openrouter"


class _Registry:
    def __init__(self) -> None:
        self.models = {"openrouter_test": _Cap()}


def test_cloud_model_on_closed_slot_is_an_error_not_a_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pa, "_assignment_snapshot", lambda registry: {"slots": {}})
    monkeypatch.setattr(pa, "_loaded_index", lambda: asyncio.sleep(0, result={}))

    _diff, warnings, errors = asyncio.run(
        pa._validate_assignment_draft(
            _Registry(),
            {"cad_spec_read": "openrouter_test"},
            loaded={},
            # ровно то, что шлёт экран: «пользователь выбрал облачного провайдера»
            cloud_overrides={"cad_spec_read": True},
        )
    )

    codes = {e.code for e in errors}
    assert "cloud_for_confidential" in codes, (
        f"облачная модель на закрытом слоте прошла как предупреждение: {[w.code for w in warnings]}"
    )
    message = next(e.message for e in errors if e.code == "cloud_for_confidential")
    # Совет «разрешите отдельно» здесь был бы неправдой — разрешать нечем.
    assert "разрешить" not in message


def test_slot_out_does_not_offer_cloud_for_closed_slot() -> None:
    meta = pa._slot_meta("cad_spec_read")
    assert meta is not None
    out = pa._build_slot_out(
        meta[0],
        meta[1],
        meta[2],
        meta[3],
        meta[4],
        model=None,
        registry=None,
        cloud_slots={"cad_spec_read"},
        current_model=None,
    )
    assert out.local_only is True
    assert out.cloud_optionable is False
    assert out.cloud_allowed is False
