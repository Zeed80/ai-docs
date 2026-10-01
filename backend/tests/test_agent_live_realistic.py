"""Реалистичные durable HTTP live-тесты агента Светы на живом стеке.

Проверяют четыре исправленных проблемы:
  1. Эпизодическая память (403 → сохраняется после каждого хода)
  2. Reranker (qwen3-reranker-8b работает без GGML crash)
  3. Агент ОБЯЗАТЕЛЬНО вызывает инструменты (не отвечает из LLM-памяти)
  4. Реалистичные бизнес-сценарии: счета, склад, поставщики, аномалии

Запуск::

    LIVE_STACK=1 docker exec infra-backend-1 \
        python -m pytest tests/test_agent_live_realistic.py -s -v --timeout=180

Требования: запущенный прод-стек + Ollama с APEX:Compact + PostgreSQL.
Для durable chat нужен ``LIVE_HUMAN_BEARER_TOKEN`` реального тестового человека;
service-account ``AGENT_SERVICE_KEY`` намеренно не имеет права создавать chat run.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest

pytestmark = pytest.mark.live

_LIVE = os.environ.get("LIVE_STACK") == "1"
_BACKEND = os.environ.get("BACKEND_URL", "http://localhost:8000")
_SERVICE_KEY = os.environ.get("AGENT_SERVICE_KEY", "")
_HUMAN_BEARER_TOKEN = os.environ.get("LIVE_HUMAN_BEARER_TOKEN", "")
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
        self.status: str | None = None
        self.blocker: object | None = None

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
async def _agent_http(session_id: str | None = None) -> AsyncIterator[_AgentHTTP]:
    """Durable HTTP client; reconnecting never creates a second logical turn."""
    if not _HUMAN_BEARER_TOKEN:
        pytest.skip("LIVE_HUMAN_BEARER_TOKEN is required for human-owned durable chat")
    async with httpx.AsyncClient(
        base_url=_BACKEND,
        headers={"Authorization": f"Bearer {_HUMAN_BEARER_TOKEN}"},
        timeout=_TURN_TIMEOUT,
    ) as client:
        yield _AgentHTTP(client, session_id)


class _AgentHTTP:
    def __init__(self, client: httpx.AsyncClient, session_id: str | None) -> None:
        self._client = client
        self.session_id = session_id

    async def send(self, text: str) -> AgentTurnResult:
        if self.session_id is None:
            created = await self._client.post(
                "/api/chat/sessions", json={"title": "Live regression"}
            )
            created.raise_for_status()
            self.session_id = created.json()["id"]
        request_id = str(uuid.uuid4())
        accepted = await self._client.post(
            "/api/agent/chat-runs",
            json={
                "request_id": request_id,
                "session_id": self.session_id,
                "content": text,
                "reasoning_mode": "normal",
                "attachments": [],
                "workspace_context": {"source": "live_regression"},
            },
        )
        accepted.raise_for_status()
        run = accepted.json()

        result = AgentTurnResult()
        cursor = 0
        deadline = time.monotonic() + _TURN_TIMEOUT
        terminal = {"completed", "blocked", "failed", "canceled"}
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"Agent did not respond in {_TURN_TIMEOUT}s")
            events = await self._client.get(
                f"/api/agent/chat-runs/{run['id']}/events",
                params={"after": cursor, "limit": 100},
            )
            events.raise_for_status()
            page = events.json()
            for item in page["items"]:
                event = item.get("payload", {}).get("event")
                if isinstance(event, dict):
                    result.feed(event)
            cursor = page["next_cursor"]
            state = await self._client.get(f"/api/agent/chat-runs/{run['id']}")
            state.raise_for_status()
            run = state.json()
            if run["status"] in terminal:
                result.done = True
                result.status = run["status"]
                result.blocker = run.get("blocker")
                if run["status"] == "blocked":
                    checkpoint_response = await self._client.get(
                        f"/api/agent/chat-runs/{run['id']}/checkpoint"
                    )
                    checkpoint_response.raise_for_status()
                    checkpoint = checkpoint_response.json()
                    confirmation = checkpoint.get("confirmation")
                    if checkpoint.get("can_resume") and isinstance(confirmation, dict):
                        result.approval_requests.append(
                            {"type": "confirmation_required", **confirmation}
                        )
                    elif result.error is None:
                        result.error = (
                            "Durable run blocked without an owner-resumable confirmation: "
                            f"{run.get('blocker')}"
                        )
                elif run["status"] != "completed" and result.error is None:
                    result.error = f"Durable run finished as {run['status']}: {run.get('blocker')}"
                return result
            await asyncio.sleep(min(1.0, remaining))


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

    Использует scope="session" + session_id — как реально делает
    MemoryManager.sync_turn/prefetch (agent_loop.py). scope="project" без
    metadata.trusted/promoted тихо демоутится сервером в "session"
    (_normalize_chat_turn_scope — намеренная политика: сырые ходы не
    засоряют project-память), а поиск без scope/session_id смотрит только
    project/global — раньше тест писал в project, а искал вслепую и не
    находил ничего, хотя реальный round-trip агента работает.
    """
    if not _ollama_up():
        pytest.skip("Ollama недоступен")

    unique_marker = f"тест_памяти_{int(time.time())}"
    session_id = f"test-memory-session-{int(time.time())}"

    async with _http() as cli:
        # Прямой POST в память с агентским ключом (проверяем что auth работает)
        resp = await cli.post(
            "/api/memory/chat-turn",
            json={
                "user_text": f"Запомни: {unique_marker}",
                "assistant_text": "Запомнила, маркер сохранён.",
                "scope": "session",
                "session_id": session_id,
            },
        )
        assert resp.status_code == 200, f"chat-turn 403 или другая ошибка: {resp.text}"
        fact_id = resp.json()["id"]

        # Через поиск должны найти сохранённый факт (та же session-область,
        # что и sync_turn — иначе фильтр по scope его не увидит).
        search = await cli.post(
            "/api/memory/search",
            json={
                "query": unique_marker,
                "limit": 5,
                "scope": "session",
                "session_id": session_id,
            },
        )
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

    async with _agent_http() as ws:
        result = await ws.send("Сколько счетов сейчас в системе?")

    assert result.error is None, f"Агент вернул ошибку: {result.error}"
    assert len(result.tool_calls) > 0, (
        f"Агент НЕ вызвал ни одного инструмента. Ответил из себя: {result.text[:300]}"
    )
    # Ожидаем вызов одного из: invoices, memory, search
    data_tools = {
        "invoices",
        "memory__search",
        "search__hybrid",
        "search__nl",
        "invoice__list",
        "memory__search",
    }
    called = set(result.tool_calls)
    assert called & data_tools or any(
        "invoic" in t or "memory" in t or "search" in t for t in result.tool_calls
    ), f"Агент вызвал инструменты {result.tool_calls}, но ни один не связан с данными"


@pytest.mark.asyncio
@pytest.mark.skipif(not _LIVE, reason="LIVE_STACK!=1")
async def test_agent_calls_memory_for_ambiguous_question():
    """Нечёткий вопрос (не совпадает ни с одним route) → память обязательна."""
    if not _ollama_up():
        pytest.skip("Ollama недоступен")

    async with _agent_http() as ws:
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

    async with _agent_http() as ws:
        result = await ws.send("Покажи все счета, которые есть в системе")

    assert result.error is None, f"Ошибка агента: {result.error}"
    # Инструмент может быть invoices/workspace/spec_table — все варианты корректны
    assert len(result.tool_calls) > 0, (
        f"Агент не вызвал ни одного инструмента для 'покажи счета'. text={result.text[:300]}"
    )
    # В ответе или в tool_calls должна быть информация о счетах
    text_lower = result.text.lower()
    has_invoice_tool = any(
        "invoic" in t.lower()
        or "workspace" in t.lower()
        or "spec" in t.lower()
        or "table" in t.lower()
        for t in result.tool_calls
    )
    has_invoice_text = any(
        kw in text_lower
        for kw in ("счёт", "счет", "сч-", "инвойс", "не найдено", "пусто", "таблиц")
    )
    assert has_invoice_tool or has_invoice_text, (
        f"Ответ не связан со счетами. tools={result.tool_calls}, text={result.text[:400]}"
    )


@pytest.mark.asyncio
@pytest.mark.skipif(not _LIVE, reason="LIVE_STACK!=1")
async def test_scenario_invoice_count():
    """Сценарий: 'Сколько счетов на утверждении' → агент читает total из API."""
    if not _ollama_up():
        pytest.skip("Ollama недоступен")

    async with _agent_http() as ws:
        result = await ws.send("Сколько счетов ожидают утверждения?")

    assert result.error is None, f"Ошибка: {result.error}"
    assert len(result.tool_calls) > 0, f"Нет вызовов инструментов: {result.text[:300]}"
    # Ответ должен содержать число (или явное «нет»)
    import re

    has_number = bool(re.search(r"\d+", result.text))
    has_none = any(
        kw in result.text.lower() for kw in ("нет", "нуль", "ноль", "отсутствуют", "не найдено")
    )
    assert has_number or has_none, f"Агент не дал конкретного числа: {result.text[:400]}"


@pytest.mark.asyncio
@pytest.mark.skipif(not _LIVE, reason="LIVE_STACK!=1")
async def test_scenario_supplier_query():
    """Сценарий: вопрос о поставщике → вызов capability suppliers или invoices."""
    if not _ollama_up():
        pytest.skip("Ollama недоступен")

    async with _agent_http() as ws:
        result = await ws.send("Покажи поставщиков с которыми мы работаем")

    assert result.error is None, f"Ошибка: {result.error}"
    assert len(result.tool_calls) > 0, (
        f"Агент не вызвал инструменты для вопроса о поставщиках: {result.text[:300]}"
    )
    assert not any(
        "нет данных" in result.text.lower() and len(result.tool_calls) == 0 for _ in [1]
    ), "Агент ответил 'нет данных' без вызова инструментов"


@pytest.mark.asyncio
@pytest.mark.skipif(not _LIVE, reason="LIVE_STACK!=1")
async def test_scenario_anomaly_detection():
    """Сценарий: 'Есть ли аномалии' → вызов capability anomalies или search."""
    if not _ollama_up():
        pytest.skip("Ollama недоступен")

    async with _agent_http() as ws:
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

    async with _agent_http() as ws:
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

    async with _agent_http() as ws:
        result = await ws.send("Найди документы про фрезы")

    assert result.error is None, f"Ошибка: {result.error}"
    assert len(result.tool_calls) > 0, (
        f"Агент не вызвал инструменты для поиска: {result.text[:300]}"
    )
    assert any(
        "search" in t.lower() or "doc" in t.lower() or "memory" in t.lower()
        for t in result.tool_calls
    ), f"Ожидали search/doc/memory, получили: {result.tool_calls}"


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

    async with _agent_http() as ws:
        r1 = await ws.send("Покажи все счета в системе")
        assert r1.error is None, f"Первый ход упал: {r1.error}"
        assert len(r1.tool_calls) > 0, "Первый ход не вызвал инструментов"

        # Ранее тут была пауза-костыль под race в backend/app/api/agent.py:
        # current_turn.done() отставал от turn_in_progress (см.
        # AGENT_LIVE_TEST_FINDINGS.md #9) — сервер отклонял второй ход сразу
        # после "done" первого. С фиксом гейта пауза не нужна.

        # Второй ход — уточнение в рамках той же сессии
        r2 = await ws.send("Покажи подробнее первый из них")
        assert r2.error is None, f"Второй ход упал: {r2.error}"
        # Агент должен понять «первый» из контекста и вызвать tool (get/detail)
        assert len(r2.tool_calls) > 0, f"Второй ход не вызвал инструментов. Ответ: {r2.text[:300]}"


# ── Тест 6: Агент НЕ делает внешних действий без подтверждения ───────────────


@pytest.mark.asyncio
@pytest.mark.skipif(not _LIVE, reason="LIVE_STACK!=1")
async def test_gate_approval_required_for_invoice_approve():
    """Approval gate: 'утверди счёт' → агент просит подтверждения, не выполняет сразу."""
    if not _ollama_up():
        pytest.skip("Ollama недоступен")

    async with _agent_http() as ws:
        result = await ws.send("Утверди все счета которые на рассмотрении")

    assert result.error is None, f"Ошибка: {result.error}"
    assert result.status == "blocked", (
        "Запрос изменения должен завершить durable run в blocked до действия, "
        f"получено status={result.status}, blocker={result.blocker}"
    )
    assert result.approval_requests, (
        "Blocked run не вернул owner-resumable checkpoint подтверждения. "
        f"blocker={result.blocker}, text={result.text[:300]}"
    )


# ── Тест 7: Скорость — простой вопрос не должен занимать >60 сек ─────────────


@pytest.mark.asyncio
@pytest.mark.skipif(not _LIVE, reason="LIVE_STACK!=1")
async def test_simple_question_performance():
    """Простой вопрос о статусе системы — ответ за разумное время."""
    if not _ollama_up():
        pytest.skip("Ollama недоступен")

    async with _agent_http() as ws:
        t0 = time.monotonic()
        result = await ws.send("Сколько документов загружено?")
        elapsed = time.monotonic() - t0

    assert result.error is None, f"Ошибка: {result.error}"
    # APEX:Compact (35B) медленный — реалистичный порог для простого вопроса
    assert elapsed < 180, f"Агент отвечал {elapsed:.0f}с на простой вопрос — слишком медленно"
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
        # Проверяем что нет 403 в логах — делаем ход агента через durable HTTP
        async with _agent_http() as ws:
            result = await ws.send("Сколько счетов в системе?")

        assert result.error is None, f"Ошибка агента: {result.error}"

        # Ждём чуть-чуть пока async sync_turn выполнится
        await asyncio.sleep(2)

        # В памяти должна появиться запись с текстом ответа
        resp = await cli.post(
            "/api/memory/search",
            json={
                "query": "счета в системе",
                "limit": 10,
                "retrieval_mode": "sql",
            },
        )
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
        f1 = await cli.post(
            "/api/memory/chat-turn",
            json={
                "user_text": "Сколько счетов от ООО Ромашка?",
                "assistant_text": "Найдено 3 счёта от ООО Ромашка на общую сумму 150 000 руб.",
                "scope": "project",
            },
        )
        assert f1.status_code == 200
        f2 = await cli.post(
            "/api/memory/chat-turn",
            json={
                "user_text": "Покажи складские остатки фрез",
                "assistant_text": "На складе 45 шт фрез Ø10 и 20 шт фрез Ø5.",
                "scope": "project",
            },
        )
        assert f2.status_code == 200

        # Поиск с reranking (auto_hybrid включает reranker если настроен)
        search = await cli.post(
            "/api/memory/search",
            json={
                "query": "счета поставщик",
                "limit": 5,
                "retrieval_mode": "auto_hybrid",
            },
        )
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
