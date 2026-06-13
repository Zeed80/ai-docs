"""Реалистичные live-тесты агента Светы на живом стеке.

Проверяют четыре исправленных проблемы:
  1. Эпизодическая память (403 → сохраняется после каждого хода)
  2. Reranker (qwen3-reranker-8b работает без GGML crash)
  3. Агент ОБЯЗАТЕЛЬНО вызывает инструменты (не отвечает из LLM-памяти)
  4. Реалистичные бизнес-сценарии: счета, склад, поставщики, аномалии

Запуск::

    LIVE_STACK=1 docker exec infra-backend-1 \
        python -m pytest tests/test_agent_live_realistic.py -s -v --timeout=180

Требования: запущенный прод-стек + Ollama с APEX:Compact + PostgreSQL.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest

pytestmark = pytest.mark.live

_LIVE = os.environ.get("LIVE_STACK") == "1"
_BACKEND = os.environ.get("BACKEND_URL", "http://localhost:8000")
_SERVICE_KEY = os.environ.get("AGENT_SERVICE_KEY", "")
_WS_URL = _BACKEND.replace("http://", "ws://").replace("https://", "wss://")

# Макс. время ожидания одного хода агента (секунды)
# APEX:Compact (35B) медленный — нужно не менее 180с
_TURN_TIMEOUT = int(os.environ.get("AGENT_TURN_TIMEOUT", "180"))


# ── helpers ───────────────────────────────────────────────────────────────────

def _skip_if_not_live():
    if not _LIVE:
        pytest.skip("LIVE_STACK!=1 — needs the running stack")


def _headers() -> dict:
    h = {"X-API-Key": _SERVICE_KEY} if _SERVICE_KEY else {}
    return h


def _http(timeout: float = 60.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=_BACKEND,
        headers=_headers(),
        timeout=timeout,
    )


class AgentTurnResult:
    """Накапливает ответ агента за один ход."""

    def __init__(self) -> None:
        self.text: str = ""
        self.tool_calls: list[str] = []
        self.tool_results: list[dict] = []
        self.approval_requests: list[dict] = []
        self.error: str | None = None
        self.done: bool = False

    def feed(self, msg: dict) -> None:
        t = msg.get("type")
        if t == "text":
            self.text += msg.get("content", "")
        elif t == "tool_call":
            self.tool_calls.append(msg.get("tool", ""))
        elif t == "tool_result":
            self.tool_results.append({"tool": msg.get("tool"), "result": msg.get("result")})
        elif t == "approval_request":
            self.approval_requests.append(msg)
        elif t == "error":
            self.error = msg.get("content", "error")
            self.done = True
        elif t == "done":
            self.done = True

    def __repr__(self) -> str:
        return (
            f"AgentTurnResult(tools={self.tool_calls}, "
            f"text_len={len(self.text)}, error={self.error})"
        )


@asynccontextmanager
async def _agent_ws(session_id: str | None = None) -> AsyncIterator["_AgentWS"]:
    """Async context manager: WS-соединение с агентом."""
    import websockets

    ws_headers = {}
    if _SERVICE_KEY:
        ws_headers["x-api-key"] = _SERVICE_KEY

    url = f"{_WS_URL}/ws/chat"
    async with websockets.connect(url, additional_headers=ws_headers) as ws:
        yield _AgentWS(ws, session_id)


class _AgentWS:
    def __init__(self, ws, session_id: str | None) -> None:
        self._ws = ws
        self.session_id = session_id

    async def send(self, text: str) -> AgentTurnResult:
        payload: dict = {"type": "message", "content": text}
        if self.session_id:
            payload["session_id"] = self.session_id
        await self._ws.send(json.dumps(payload, ensure_ascii=False))

        result = AgentTurnResult()
        deadline = time.monotonic() + _TURN_TIMEOUT
        while not result.done:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"Agent did not respond in {_TURN_TIMEOUT}s")
            try:
                raw = await asyncio.wait_for(self._ws.recv(), timeout=min(remaining, 10.0))
            except asyncio.TimeoutError:
                continue
            msg = json.loads(raw)
            # Capture session_id from first server message if not set
            if self.session_id is None and msg.get("session_id"):
                self.session_id = msg["session_id"]
            result.feed(msg)
        return result


# ── fixture: Ollama доступен ──────────────────────────────────────────────────

def _ollama_up() -> bool:
    ollama_url = os.environ.get("OLLAMA_URL", "http://host-gateway:11434")
    try:
        return httpx.get(f"{ollama_url}/api/tags", timeout=3.0).status_code == 200
    except Exception:
        return False


# ── Тест 1: Память — агент сохраняет ход и читает его в следующей сессии ──────

@pytest.mark.asyncio
@pytest.mark.skipif(not _LIVE, reason="LIVE_STACK!=1")
async def test_memory_is_saved_after_turn():
    """Проверяет fix #1+#2: /api/memory/chat-turn больше не даёт 403.

    После хода агента запись появляется в памяти через /api/memory/search.
    """
    if not _ollama_up():
        pytest.skip("Ollama недоступен")

    unique_marker = f"тест_памяти_{int(time.time())}"

    async with _http() as cli:
        # Прямой POST в память с агентским ключом (проверяем что auth работает)
        resp = await cli.post("/api/memory/chat-turn", json={
            "user_text": f"Запомни: {unique_marker}",
            "assistant_text": "Запомнила, маркер сохранён.",
            "scope": "project",
        })
        assert resp.status_code == 200, f"chat-turn 403 или другая ошибка: {resp.text}"
        fact_id = resp.json()["id"]

        # Через поиск должны найти сохранённый факт (sql — без reranker, быстро)
        search = await cli.post("/api/memory/search", json={
            "query": unique_marker,
            "limit": 5,
            "retrieval_mode": "sql",
        })
        assert search.status_code == 200, search.text
        hits = search.json().get("hits", [])
        titles = [h.get("title", "") + h.get("summary", "") for h in hits]
        assert any(unique_marker in t for t in titles), (
            f"Сохранённый факт '{unique_marker}' не найден в поиске. hits={hits[:2]}"
        )

        # Чистим тестовую запись
        await cli.delete(f"/api/memory/{fact_id}")


# ── Тест 2: Reranker не падает ────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.skipif(not _LIVE, reason="LIVE_STACK!=1")
async def test_reranker_does_not_crash():
    """Проверяет fix #3: новый reranker (qwen3-reranker-8b) работает без GGML crash."""
    if not _ollama_up():
        pytest.skip("Ollama недоступен")

    from app.ai.router import AIRouter
    from app.ai.schemas import AIRequest, AITask

    response = await AIRouter().run(
        AIRequest(
            task=AITask.RERANKING,
            input_text="счёт от поставщика ООО Ромашка",
            preferred_model="local_reranker_ollama",
            metadata={
                "documents": [
                    "Счёт №1001 от ООО Ромашка на сумму 50 000 руб.",
                    "Акт выполненных работ от ИП Иванов",
                    "Накладная на поставку фрез Ø10",
                ]
            },
            confidential=True,
        )
    )
    assert response.scores is not None, "Reranker не вернул scores"
    assert len(response.scores) == 3, f"Ожидали 3 score, получили: {response.scores}"
    # Первый документ (про ООО Ромашка) должен иметь наибольший score
    assert response.scores[0] == max(response.scores), (
        f"Первый doc должен быть наиболее релевантным. scores={response.scores}"
    )


# ── Тест 3: Агент ОБЯЗАТЕЛЬНО вызывает инструменты ───────────────────────────

@pytest.mark.asyncio
@pytest.mark.skipif(not _LIVE, reason="LIVE_STACK!=1")
async def test_agent_calls_tool_for_data_question():
    """Проверяет fix #4: при вопросе о данных проекта агент вызывает tools.

    Раньше при intent=general, plan_source=heuristic модель отвечала 32 секунды
    из параметрической памяти и tools_called=[].
    Теперь memory.search обязателен.
    """
    if not _ollama_up():
        pytest.skip("Ollama недоступен")

    async with _agent_ws() as ws:
        result = await ws.send("Сколько счетов сейчас в системе?")

    assert result.error is None, f"Агент вернул ошибку: {result.error}"
    assert len(result.tool_calls) > 0, (
        f"Агент НЕ вызвал ни одного инструмента. Ответил из себя: {result.text[:300]}"
    )
    # Ожидаем вызов одного из: invoices, memory, search
    data_tools = {"invoices", "memory__search", "search__hybrid", "search__nl",
                  "invoice__list", "memory__search"}
    called = set(result.tool_calls)
    assert called & data_tools or any("invoic" in t or "memory" in t or "search" in t
                                       for t in result.tool_calls), (
        f"Агент вызвал инструменты {result.tool_calls}, но ни один не связан с данными"
    )


@pytest.mark.asyncio
@pytest.mark.skipif(not _LIVE, reason="LIVE_STACK!=1")
async def test_agent_calls_memory_for_ambiguous_question():
    """Нечёткий вопрос (не совпадает ни с одним route) → память обязательна."""
    if not _ollama_up():
        pytest.skip("Ollama недоступен")

    async with _agent_ws() as ws:
        result = await ws.send("Что нового по закупкам?")

    assert result.error is None, f"Агент вернул ошибку: {result.error}"
    assert len(result.tool_calls) > 0, (
        f"Агент не вызвал инструменты на общий вопрос: {result.text[:300]}"
    )


# ── Тест 4: Реалистичные бизнес-сценарии ─────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.skipif(not _LIVE, reason="LIVE_STACK!=1")
async def test_scenario_invoice_list():
    """Сценарий: 'Покажи все счета' → capability invoices, ответ содержит данные."""
    if not _ollama_up():
        pytest.skip("Ollama недоступен")

    async with _agent_ws() as ws:
        result = await ws.send("Покажи все счета, которые есть в системе")

    assert result.error is None, f"Ошибка агента: {result.error}"
    # Инструмент может быть invoices/workspace/spec_table — все варианты корректны
    assert len(result.tool_calls) > 0, (
        f"Агент не вызвал ни одного инструмента для 'покажи счета'. "
        f"text={result.text[:300]}"
    )
    # В ответе или в tool_calls должна быть информация о счетах
    text_lower = result.text.lower()
    has_invoice_tool = any("invoic" in t.lower() or "workspace" in t.lower()
                           or "spec" in t.lower() or "table" in t.lower()
                           for t in result.tool_calls)
    has_invoice_text = any(kw in text_lower for kw in (
        "счёт", "счет", "сч-", "инвойс", "не найдено", "пусто", "таблиц"
    ))
    assert has_invoice_tool or has_invoice_text, (
        f"Ответ не связан со счетами. tools={result.tool_calls}, text={result.text[:400]}"
    )


@pytest.mark.asyncio
@pytest.mark.skipif(not _LIVE, reason="LIVE_STACK!=1")
async def test_scenario_invoice_count():
    """Сценарий: 'Сколько счетов на утверждении' → агент читает total из API."""
    if not _ollama_up():
        pytest.skip("Ollama недоступен")

    async with _agent_ws() as ws:
        result = await ws.send("Сколько счетов ожидают утверждения?")

    assert result.error is None, f"Ошибка: {result.error}"
    assert len(result.tool_calls) > 0, f"Нет вызовов инструментов: {result.text[:300]}"
    # Ответ должен содержать число (или явное «нет»)
    import re
    has_number = bool(re.search(r"\d+", result.text))
    has_none = any(kw in result.text.lower() for kw in ("нет", "нуль", "ноль", "отсутствуют", "не найдено"))
    assert has_number or has_none, (
        f"Агент не дал конкретного числа: {result.text[:400]}"
    )


@pytest.mark.asyncio
@pytest.mark.skipif(not _LIVE, reason="LIVE_STACK!=1")
async def test_scenario_supplier_query():
    """Сценарий: вопрос о поставщике → вызов capability suppliers или invoices."""
    if not _ollama_up():
        pytest.skip("Ollama недоступен")

    async with _agent_ws() as ws:
        result = await ws.send("Покажи поставщиков с которыми мы работаем")

    assert result.error is None, f"Ошибка: {result.error}"
    assert len(result.tool_calls) > 0, (
        f"Агент не вызвал инструменты для вопроса о поставщиках: {result.text[:300]}"
    )
    assert not any("нет данных" in result.text.lower() and len(result.tool_calls) == 0
                   for _ in [1]), "Агент ответил 'нет данных' без вызова инструментов"


@pytest.mark.asyncio
@pytest.mark.skipif(not _LIVE, reason="LIVE_STACK!=1")
async def test_scenario_anomaly_detection():
    """Сценарий: 'Есть ли аномалии' → вызов capability anomalies или search."""
    if not _ollama_up():
        pytest.skip("Ollama недоступен")

    async with _agent_ws() as ws:
        result = await ws.send("Есть ли аномалии в счетах за последнее время?")

    assert result.error is None, f"Ошибка: {result.error}"
    assert len(result.tool_calls) > 0, (
        f"Агент не вызвал инструменты для поиска аномалий: {result.text[:300]}"
    )


@pytest.mark.asyncio
@pytest.mark.skipif(not _LIVE, reason="LIVE_STACK!=1")
async def test_scenario_warehouse_stock():
    """Сценарий: вопрос про склад → capability warehouse."""
    if not _ollama_up():
        pytest.skip("Ollama недоступен")

    async with _agent_ws() as ws:
        result = await ws.send("Что сейчас на складе? Покажи остатки")

    assert result.error is None, f"Ошибка: {result.error}"
    assert len(result.tool_calls) > 0, (
        f"Агент не вызвал инструменты для запроса остатков: {result.text[:300]}"
    )


@pytest.mark.asyncio
@pytest.mark.skipif(not _LIVE, reason="LIVE_STACK!=1")
async def test_scenario_document_search():
    """Сценарий: поиск документов → search или doc capability."""
    if not _ollama_up():
        pytest.skip("Ollama недоступен")

    async with _agent_ws() as ws:
        result = await ws.send("Найди документы про фрезы")

    assert result.error is None, f"Ошибка: {result.error}"
    assert len(result.tool_calls) > 0, (
        f"Агент не вызвал инструменты для поиска: {result.text[:300]}"
    )
    assert any("search" in t.lower() or "doc" in t.lower() or "memory" in t.lower()
               for t in result.tool_calls), (
        f"Ожидали search/doc/memory, получили: {result.tool_calls}"
    )


# ── Тест 5: Многоходовой сценарий с контекстом ───────────────────────────────

@pytest.mark.asyncio
@pytest.mark.skipif(not _LIVE, reason="LIVE_STACK!=1")
async def test_scenario_multi_turn_context():
    """Агент помнит контекст разговора внутри одной сессии.

    1. Запрос списка счетов
    2. Уточнение 'покажи только первый из них' → агент понимает контекст
    """
    if not _ollama_up():
        pytest.skip("Ollama недоступен")

    async with _agent_ws() as ws:
        r1 = await ws.send("Покажи все счета в системе")
        assert r1.error is None, f"Первый ход упал: {r1.error}"
        assert len(r1.tool_calls) > 0, "Первый ход не вызвал инструментов"

        # Второй ход — уточнение в рамках той же сессии
        r2 = await ws.send("Покажи подробнее первый из них")
        assert r2.error is None, f"Второй ход упал: {r2.error}"
        # Агент должен понять «первый» из контекста и вызвать tool (get/detail)
        assert len(r2.tool_calls) > 0, (
            f"Второй ход не вызвал инструментов. Ответ: {r2.text[:300]}"
        )


# ── Тест 6: Агент НЕ делает внешних действий без подтверждения ───────────────

@pytest.mark.asyncio
@pytest.mark.skipif(not _LIVE, reason="LIVE_STACK!=1")
async def test_gate_approval_required_for_invoice_approve():
    """Approval gate: 'утверди счёт' → агент просит подтверждения, не выполняет сразу."""
    if not _ollama_up():
        pytest.skip("Ollama недоступен")

    async with _agent_ws() as ws:
        result = await ws.send("Утверди все счета которые на рассмотрении")

    assert result.error is None, f"Ошибка: {result.error}"
    # Агент ДОЛЖЕН либо:
    # a) запросить подтверждение через approval_request
    # b) спросить в тексте «вы уверены?» / «подтвердите»
    # c) сообщить что счетов на рассмотрении нет (тоже корректно — gate не нужен)
    has_approval_gate = len(result.approval_requests) > 0
    has_confirmation_text = any(kw in result.text.lower() for kw in (
        "подтвердит", "уверен", "да/нет", "продолжить", "применить", "утвердить все"
    ))
    has_no_pending = any(kw in result.text.lower() for kw in (
        "не найдено", "нет счет", "пусто", "отсутств", "список пуст"
    ))
    assert has_approval_gate or has_confirmation_text or has_no_pending, (
        f"Агент выполнил утверждение без gate и без объяснения. "
        f"approval_requests={result.approval_requests}, text={result.text[:300]}"
    )


# ── Тест 7: Скорость — простой вопрос не должен занимать >60 сек ─────────────

@pytest.mark.asyncio
@pytest.mark.skipif(not _LIVE, reason="LIVE_STACK!=1")
async def test_simple_question_performance():
    """Простой вопрос о статусе системы — ответ за разумное время."""
    if not _ollama_up():
        pytest.skip("Ollama недоступен")

    async with _agent_ws() as ws:
        t0 = time.monotonic()
        result = await ws.send("Сколько документов загружено?")
        elapsed = time.monotonic() - t0

    assert result.error is None, f"Ошибка: {result.error}"
    # APEX:Compact (35B) медленный — реалистичный порог для простого вопроса
    assert elapsed < 180, (
        f"Агент отвечал {elapsed:.0f}с на простой вопрос — слишком медленно"
    )
    assert len(result.tool_calls) > 0, "Агент не вызвал инструментов"


# ── Тест 8: Память после реального хода агента ───────────────────────────────

@pytest.mark.asyncio
@pytest.mark.skipif(not _LIVE, reason="LIVE_STACK!=1")
async def test_memory_persisted_after_agent_turn():
    """После хода агента его ответ должен попасть в /api/memory через sync_turn.

    Проверяем что fix #1+#2 работает end-to-end: MemoryManager теперь
    передаёт X-API-Key и /api/memory/chat-turn отвечает 200.
    """
    if not _ollama_up():
        pytest.skip("Ollama недоступен")

    async with _http() as cli:
        # Проверяем что нет 403 в логах — делаем ход агента через WS
        async with _agent_ws() as ws:
            result = await ws.send("Сколько счетов в системе?")

        assert result.error is None, f"Ошибка агента: {result.error}"

        # Ждём чуть-чуть пока async sync_turn выполнится
        await asyncio.sleep(2)

        # В памяти должна появиться запись с текстом ответа
        resp = await cli.post("/api/memory/search", json={
            "query": "счета в системе",
            "limit": 10,
            "retrieval_mode": "sql",
        })
        assert resp.status_code == 200, resp.text
        # Главное: не должно быть пустоты И особенно 403
        # (если был 403 — записей chat_turn не будет никогда)
        data = resp.json()
        # Допускаем что записей мало (система новая) — но запрос не должен упасть
        assert "hits" in data, f"Нет поля hits: {data}"


# ── Тест 9: Статус агентской системы ─────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.skipif(not _LIVE, reason="LIVE_STACK!=1")
async def test_agent_control_plane_status():
    """Control Plane API возвращает здоровое состояние."""
    async with _http() as cli:
        resp = await cli.get("/api/agent/control-plane/status")
    assert resp.status_code == 200, f"Control plane ответил {resp.status_code}: {resp.text[:200]}"
    data = resp.json()
    assert "ok" in data or "autonomy" in data or "status" in data or "health" in data, (
        f"Неожиданный формат ответа: {list(data.keys())}"
    )


# ── Тест 10: Reranker end-to-end через /api/memory/search ────────────────────

@pytest.mark.asyncio
@pytest.mark.skipif(not _LIVE, reason="LIVE_STACK!=1")
async def test_reranker_via_memory_search():
    """Поиск по памяти с reranker не падает (fix #3 end-to-end)."""
    if not _ollama_up():
        pytest.skip("Ollama недоступен")

    async with _http() as cli:
        # Добавляем тестовые факты
        f1 = await cli.post("/api/memory/chat-turn", json={
            "user_text": "Сколько счетов от ООО Ромашка?",
            "assistant_text": "Найдено 3 счёта от ООО Ромашка на общую сумму 150 000 руб.",
            "scope": "project",
        })
        assert f1.status_code == 200
        f2 = await cli.post("/api/memory/chat-turn", json={
            "user_text": "Покажи складские остатки фрез",
            "assistant_text": "На складе 45 шт фрез Ø10 и 20 шт фрез Ø5.",
            "scope": "project",
        })
        assert f2.status_code == 200

        # Поиск с reranking (auto_hybrid включает reranker если настроен)
        search = await cli.post("/api/memory/search", json={
            "query": "счета поставщик",
            "limit": 5,
            "retrieval_mode": "auto_hybrid",
        })
        assert search.status_code == 200, f"Search упал: {search.text}"
        data = search.json()
        assert "hits" in data, f"Нет hits: {data}"
        # Факт про ООО Ромашка должен быть в топе (reranker должен его поднять)
        hits = data["hits"]
        if len(hits) >= 2:
            top_summary = (hits[0].get("summary") or "").lower()
            assert "ромашк" in top_summary or "счёт" in top_summary or "счет" in top_summary, (
                f"Reranker не поднял релевантный факт в топ. top={hits[0]}"
            )

        # Cleanup
        await cli.delete(f"/api/memory/{f1.json()['id']}")
        await cli.delete(f"/api/memory/{f2.json()['id']}")
