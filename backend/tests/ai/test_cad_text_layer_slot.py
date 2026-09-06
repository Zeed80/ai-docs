"""Текстовый слой чертежа можно выключить — и выключение действительно работает.

Отдельный проход транскрибирует надписи листа маленькой документной моделью.
Способная vision-модель в слоте чтения делает это сама, и тогда второй проход
стоит времени и денег, не добавляя ничего.

Выключить его было нельзя. `_ocr_model_and_url` при пустом или неудачном
резолве подставляла зашитое `glm-ocr:latest` и локальный адрес Ollama, поэтому
стадия шла в любом случае: очищенный слот возвращался к дефолту реестра, а тот
снова назначал glm-ocr. Единственным способом остановить стадию было отсутствие
модели на узле — то есть отказ, а не настройка.
"""

from __future__ import annotations

import pytest

from app.ai.cad_recognize import spec_fragments
from app.ai.task_routing import TaskRouting


def _routing(**kwargs) -> TaskRouting:
    return TaskRouting(task="cad_text_ocr", models=["glm_ocr_ollama"], **kwargs)


def test_routing_returns_nothing_when_the_slot_is_off(monkeypatch):
    monkeypatch.setattr("app.ai.task_routing.get_routing_for", lambda _t: _routing(disabled=True))
    assert spec_fragments._ocr_model_and_url() is None


def test_an_enabled_slot_still_resolves_its_model(monkeypatch):
    monkeypatch.setattr("app.ai.task_routing.get_routing_for", lambda _t: _routing())
    monkeypatch.setattr(
        "app.ai.task_routing.resolve_model", lambda _t: ("glm-ocr:latest", "ollama")
    )

    assignment = spec_fragments._ocr_model_and_url()

    assert assignment is not None
    model, _url, provider = assignment
    assert (model, provider) == ("glm-ocr:latest", "ollama")


@pytest.mark.asyncio
async def test_disabled_slot_skips_the_stage_without_calling_a_model(monkeypatch):
    events: list[tuple[str, str]] = []

    async def _record(stage, status, message, details=None):
        events.append((stage, status))

    async def _must_not_be_called(*_a, **_k):  # pragma: no cover — вызов и есть провал
        raise AssertionError("выключенный слот не должен ходить к модели")

    monkeypatch.setattr(spec_fragments, "_ocr_model_and_url", lambda: None)
    monkeypatch.setattr("app.ai.cad_process_log.record_cad_process_event", _record)
    monkeypatch.setattr(spec_fragments, "_ocr_via_router", _must_not_be_called)

    result = await spec_fragments.read_callouts_with_ocr(object())

    assert result == {}
    # Пропуск должен быть ВИДЕН: молчаливо исчезнувшая стадия неотличима от сбоя.
    assert ("reader.text_ocr", "skipped") in events


@pytest.mark.asyncio
async def test_a_missing_assignment_still_falls_back_instead_of_skipping(monkeypatch):
    """Ненастроенный слот и выключенный слот — разные состояния.

    Пустая база — не решение оператора, поэтому там по-прежнему работает
    встроенный дефолт. Пропуск наступает только от явного «не использовать».
    """
    monkeypatch.setattr("app.ai.task_routing.get_routing_for", lambda _t: _routing(models=[]))
    monkeypatch.setattr("app.ai.task_routing.resolve_model", lambda _t: (None, None))

    assignment = spec_fragments._ocr_model_and_url()

    assert assignment is not None
    assert assignment[0] == spec_fragments._OCR_MODEL
