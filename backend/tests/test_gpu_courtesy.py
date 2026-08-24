"""Фоновая работа уступает видеокарту агенту и студии.

Приоритет стенда задан человеком прямо: главное — агент, а студия при
генерации забирает карту целиком. Разбор каталога — работа на часы, и
отложить её на пять минут ничего не стоит; заставить человека ждать
перезагрузки 16-гигабайтной модели ради страницы каталога — стоит дорого.

До этого выбор был ручным: человек нажимал «Приостановить». На стенде это
кончилось тем, что 1016 отрендеренных страниц простояли двенадцать часов —
паузу сняли, а поставить обратно в очередь забыли.
"""

from __future__ import annotations

import pytest

from app.tasks import gpu_courtesy


@pytest.mark.asyncio
async def test_free_card_means_work_goes_on(monkeypatch):
    """Наша же модель в памяти — это не повод уступать самому себе."""
    monkeypatch.setattr(gpu_courtesy, "_redis", lambda: None)
    monkeypatch.setattr(gpu_courtesy, "is_interactive", lambda: False)
    monkeypatch.setattr("app.ai.gpu_lock.is_locked", lambda: False, raising=False)

    class _Response:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"models": [{"name": "qwen3.5:9b"}]}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url):
            return _Response()

    monkeypatch.setattr("httpx.AsyncClient", lambda **kwargs: _Client())

    assert await gpu_courtesy.gpu_yield_reason("qwen3.5:9b") is None


@pytest.mark.asyncio
async def test_agent_answering_a_person_wins(monkeypatch):
    """Пока агент отвечает человеку, страница каталога подождёт."""
    monkeypatch.setattr("app.ai.gpu_lock.is_locked", lambda: False, raising=False)
    monkeypatch.setattr(gpu_courtesy, "is_interactive", lambda: True)

    reason = await gpu_courtesy.gpu_yield_reason("qwen3.5:9b")
    assert reason == "агент отвечает человеку"


@pytest.mark.asyncio
async def test_pinned_agent_model_alone_is_not_a_reason_to_yield(monkeypatch):
    """Модель агента закреплена в памяти навсегда (её выгрузка назначена на
    2318 год ради мгновенного первого ответа). Считать её присутствие
    занятостью — значит остановить фоновую работу насовсем: именно так разбор
    уступал шесть раз подряд, пока никто ни о чём не спрашивал."""
    monkeypatch.setattr("app.ai.gpu_lock.is_locked", lambda: False, raising=False)
    monkeypatch.setattr(gpu_courtesy, "is_interactive", lambda: False)

    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"models": [{"name": "qwen3.8:27b-131072"}]}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url):
            return _Response()

    monkeypatch.setattr("httpx.AsyncClient", lambda **kwargs: _Client())

    assert await gpu_courtesy.gpu_yield_reason("qwen3.5:9b") is None


@pytest.mark.asyncio
async def test_training_run_beats_everything(monkeypatch):
    monkeypatch.setattr("app.ai.gpu_lock.is_locked", lambda: True, raising=False)

    reason = await gpu_courtesy.gpu_yield_reason("qwen3.5:9b")
    assert reason == "идёт обучение LoRA"


@pytest.mark.asyncio
async def test_no_room_for_our_model_means_yield(monkeypatch):
    """Ничего не загружено, а места нет — карту занял кто-то помимо Ollama
    (обычно студия, она забирает её целиком)."""
    monkeypatch.setattr("app.ai.gpu_lock.is_locked", lambda: False, raising=False)
    monkeypatch.setattr(gpu_courtesy, "is_interactive", lambda: False)

    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"models": []}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url):
            return _Response()

    class _Telemetry:
        vram_free_gb = 1.2

    async def _telemetry():
        return _Telemetry()

    monkeypatch.setattr("httpx.AsyncClient", lambda **kwargs: _Client())
    monkeypatch.setattr("app.ai.gpu_manager.get_gpu_telemetry", _telemetry, raising=False)

    reason = await gpu_courtesy.gpu_yield_reason("qwen3.5:9b")
    assert reason and "свободно" in reason


@pytest.mark.asyncio
async def test_broken_checks_never_stop_the_work(monkeypatch):
    """Недоступная телеметрия — не причина останавливать разбор.

    Проверка вежливости не должна становиться новой точкой отказа: если о
    состоянии карты ничего не известно, работаем.
    """

    def _boom():
        raise RuntimeError("нет связи")

    monkeypatch.setattr("app.ai.gpu_lock.is_locked", _boom, raising=False)
    monkeypatch.setattr(gpu_courtesy, "is_interactive", lambda: False)

    def _client(**kwargs):
        raise RuntimeError("ollama недоступна")

    async def _telemetry_boom():
        raise RuntimeError("сайдкар молчит")

    monkeypatch.setattr("httpx.AsyncClient", _client)
    monkeypatch.setattr("app.ai.gpu_manager.get_gpu_telemetry", _telemetry_boom, raising=False)

    assert await gpu_courtesy.gpu_yield_reason("qwen3.5:9b") is None


@pytest.mark.asyncio
async def test_silent_ollama_is_not_read_as_a_busy_card(monkeypatch):
    """«Ollama не ответила» — не то же, что «на карте нет места».

    Пока эти случаи были одним пустым списком, недоступность Ollama читалась
    как занятая карта: свободной памяти мало (её держит НАША же модель), и
    разбор уступал сам себе.
    """
    monkeypatch.setattr("app.ai.gpu_lock.is_locked", lambda: False, raising=False)
    monkeypatch.setattr(gpu_courtesy, "is_interactive", lambda: False)

    def _client(**kwargs):
        raise RuntimeError("ollama недоступна")

    async def _telemetry():
        class _T:
            vram_free_gb = 0.8

        return _T()

    monkeypatch.setattr("httpx.AsyncClient", _client)
    monkeypatch.setattr("app.ai.gpu_manager.get_gpu_telemetry", _telemetry, raising=False)

    assert await gpu_courtesy.gpu_yield_reason("qwen3.5:9b") is None


def test_yields_are_bounded(monkeypatch):
    """Уступать бесконечно — значит не сделать работу вовсе.

    Модель агента висит в памяти пять минут после любого обращения, так что
    редкой фоновой активности хватило бы, чтобы разбор не сдвинулся никогда.
    """
    store: dict[str, int] = {}

    class _FakeRedis:
        def setex(self, key, ttl, value):
            store[key] = 1

        def incr(self, key):
            store[key] = store.get(key, 0) + 1
            return store[key]

        def expire(self, key, ttl):
            return True

        def get(self, key):
            return store.get(key)

        def delete(self, key):
            store.pop(key, None)

        def exists(self, key):
            return 1 if key in store else 0

    monkeypatch.setattr(gpu_courtesy, "_redis", _FakeRedis)

    doc = "doc-1"
    for _ in range(gpu_courtesy.MAX_CONSECUTIVE_YIELDS - 1):
        gpu_courtesy.mark_yielded(doc)
    assert not gpu_courtesy.yields_exhausted(doc), "рано сдаваться"

    gpu_courtesy.mark_yielded(doc)
    assert gpu_courtesy.yields_exhausted(doc), "после получаса партия должна пройти"

    # Прошедшая партия обнуляет счёт: следующий раз снова уступаем.
    gpu_courtesy.clear_yield(doc)
    assert not gpu_courtesy.yields_exhausted(doc)
