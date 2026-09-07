"""Возможности модели устанавливаются пробой, а не заявлением каталога.

Каталог заполняется автоматически из ответа провайдера на `/v1/models` и
ошибается в обе стороны — на этом стенде встретились обе ошибки сразу:

* `ollama_cloud deepseek-v3.1:671b` объявлен БЕЗ зрения и прочитал чертёж
  (его Ø15,7 / Ø24,5 / Ø21,7 независимо совпали с фрагментным чтением);
* `openrouter minimax-m3:free` объявлен пригодным кандидатом и строгую схему не
  держит — три прохода полного чтения из пяти на нём отвалились.

Поэтому гейт модальности строг только там, где возможность ПРОВЕРЕНА, и это
различие имеет смысл ровно постольку, поскольку проверка существует.

Главное правило пробы: `None` при сетевом сбое и НИКОГДА `False`.
Инфраструктурная икота не должна закешироваться как приговор модели.
"""

from __future__ import annotations

import asyncio

from app.ai.capability_probe import ProbeResult, apply_probe, probe_model
from app.ai.schemas import AIResponse, AITask, ProviderKind


class _Router:
    """Роутер-заглушка: отдаёт заготовленные ответы по порядку вызовов."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.requests = []

    async def run(self, request):
        self.requests.append(request)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return AIResponse(task=request.task, provider=ProviderKind.OLLAMA, model="m", text=outcome)


def _probe(router, checks=frozenset({"vision", "structured"})) -> ProbeResult:
    return asyncio.run(probe_model("k", checks=checks, router=router))


# ── Зрение ───────────────────────────────────────────────────────────────────


def test_a_model_that_reads_the_digit_can_see():
    result = _probe(_Router("7"), checks=frozenset({"vision"}))
    assert result.vision is True


def test_an_empty_answer_to_an_image_means_no_vision():
    """Слепая модель отвечает на картинку пустой строкой и HTTP 200.

    Роутер при этом не уходит к следующему кандидату — ответ формально есть, —
    поэтому «пусто» и есть тот отказ, ради которого проба нужна.
    """
    result = _probe(_Router(""), checks=frozenset({"vision"}))
    assert result.vision is False


def test_a_wrong_digit_means_no_vision():
    result = _probe(_Router("это чертёж вала"), checks=frozenset({"vision"}))
    assert result.vision is False


def test_a_network_failure_is_not_a_verdict():
    """«Не смогли спросить» — не «не умеет»."""
    result = _probe(_Router(RuntimeError("connection reset")), checks=frozenset({"vision"}))
    assert result.vision is None


# ── Строгая схема ────────────────────────────────────────────────────────────


def test_a_model_that_honours_the_schema_passes():
    result = _probe(_Router('{"answer": 42, "label": "ok"}'), checks=frozenset({"structured"}))
    assert result.structured is True


def test_a_missing_required_field_fails():
    result = _probe(_Router('{"answer": 42}'), checks=frozenset({"structured"}))
    assert result.structured is False


def test_a_wrong_type_fails():
    result = _probe(
        _Router('{"answer": "сорок два", "label": "ok"}'), checks=frozenset({"structured"})
    )
    assert result.structured is False


def test_prose_instead_of_json_fails():
    result = _probe(_Router("Конечно! Вот ответ: сорок два."), checks=frozenset({"structured"}))
    assert result.structured is False


def test_the_probe_measures_the_model_not_the_routers_safety_net():
    """С переспросами проба показала бы, чего модель добивается со ВТОРОЙ попытки."""
    router = _Router('{"answer": 42, "label": "ok"}')
    _probe(router, checks=frozenset({"structured"}))
    assert router.requests[0].metadata["format_max_reasks"] == 0


def test_the_probe_asks_the_named_model_and_nothing_else():
    router = _Router("7")
    _probe(router, checks=frozenset({"vision"}))
    request = router.requests[0]
    assert request.preferred_model == "k"
    assert request.images  # картинка действительно отправлена
    assert request.task is AITask.CAD_TEXT_OCR


# ── Запись результата ────────────────────────────────────────────────────────


def test_a_confirmed_vision_is_added_to_the_modalities(monkeypatch):
    written: dict = {}
    monkeypatch.setattr(
        "app.ai.model_registry.set_capability_override",
        lambda key, **kw: written.update(key=key, **kw),
    )

    apply_probe("k", ProbeResult(vision=True, checked_at="2026-09-07"), current_modalities={"text"})

    assert "vision" in written["modalities"]
    assert written["checked_at"] == "2026-09-07"


def test_a_refuted_vision_is_removed_from_the_modalities(monkeypatch):
    written: dict = {}
    monkeypatch.setattr(
        "app.ai.model_registry.set_capability_override",
        lambda key, **kw: written.update(key=key, **kw),
    )

    apply_probe(
        "k",
        ProbeResult(vision=False, checked_at="2026-09-07"),
        current_modalities={"text", "vision"},
    )

    assert "vision" not in written["modalities"]


def test_an_undetermined_probe_writes_nothing(monkeypatch):
    """Иначе модель получила бы штамп «проверено» без единого проверенного факта."""
    calls = []
    monkeypatch.setattr(
        "app.ai.model_registry.set_capability_override", lambda key, **kw: calls.append(key)
    )

    apply_probe("k", ProbeResult(checked_at="2026-09-07"), current_modalities={"text"})

    assert calls == []
