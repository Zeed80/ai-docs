# E21.2b11 — Usage receipts для streaming AgentSession

Дата: 5 октября 2026. Исходный commit: `4f2b089d` (E21.2b10).
Исполнитель и приёмка: Claude Code (root).

## Ограниченный контракт

Основной путь чата (`_call_provider_streaming` → `_call_ollama_streaming`)
резервировал и списывал каждую physical попытку (E21.2a), но legacy-методом без
usage evidence. Теперь каждая попытка на Ollama-пути открывает task-local
`capture_ollama_usage()`, а поток записывает evidence на своей HTTP-границе:

- HTTP error status → `http_error`;
- финальный `done`-чанк → строгие `prompt_eval_count`/`eval_count` (тот же
  `ollama_usage_from_body`, что и для обычного ответа);
- поток оборван, отменён или закончился без `done` → `response_not_observed`.

Evidence передаётся в атомарный settlement E21.2b9 на обеих точках списания
(успех и исключение/отмена). Transient retry — отдельная reservation и отдельный
receipt.

Границы:

- Receipt v1 описывает только Ollama. OpenAI-совместимые и Anthropic попытки
  списываются прежним `settle_budget` без receipt — в сводке это missing, то есть
  явный unknown, не 0. Fallback «неизвестный provider → Ollama» идёт по Ollama-пути.
- Receipt привязан к frozen owner. `WorkBudgetContext` без `expected_owner_key`
  (capability-контекст `tasks/work_orders.py`) списывает попытку legacy-методом:
  receipt не создаётся, sticky stop не возникает. Раньше в этой ветке стоял
  `expected_owner_key or ""`; через AgentSession она стала достижима и
  останавливала бы каждую попытку.

Попутный дефект: телеметрия `_usage` делала `int(chunk.get(...) or 0)` — нечисловой
счётчик ронял `ValueError` и выбрасывал уже полученный потоковый ответ, а
отсутствующий превращался в 0. Теперь значения строгие, иначе `None`.

## Сценарии и тесты

`backend/tests/test_work_budget_usage_agent_session.py` (настоящий
`_call_ollama_streaming`, подменены HTTP-поток и выбор модели):

| Сценарий | Наблюдение |
|---|---|
| Поток с `done` и счётчиками | Charged, known receipt 22, `_usage` строгий |
| `"abc"`/`True` в счётчиках | Ответ сохранён, receipt `invalid_type`, `_usage` = None |
| Нет `done` | `response_not_observed`, сводка unknown |
| `ReadError` посреди потока → retry | 2 reservation, 2 receipt; сводка partial, lower bound 10 |
| HTTP 503 | Charged + `http_error` |
| Отмена посреди потока | Charged + `response_not_observed` |
| Сбой settlement | Sticky stop, без fallback, reservation `reserved`, без receipt |
| OpenAI-провайдер | Charged, receipt отсутствует, сводка unknown |

## Приёмка

Отдельные testcontainers PostgreSQL (`POSTGRES_HOST=127.0.0.1 POSTGRES_PORT=1`):

- budget/durable профиль + AIRouter и AgentSession usage: **371 passed**;
- агентские/approval/email-gate/transport/channel parity/control plane: **422
  passed, 1 failed** — `test_control_plane_status_does_not_count_rejected_tasks_as_open`
  (`tasks_open` 20 вместо 1, задачи соседних тестов в общей БД). Падает так же на
  `4f2b089d` без правок (worktree, 117 passed / 1 failed), отдельно файл —
  31 passed. Базовая линия, не регрессия;
- router/provider/routing: **168 passed, 1 skipped**;
- ledger/lease/employee corpus **57**, planner **23**, verifier **17 passed**.

Итого 1058 passed, 1 skipped, 1 базовое падение. Ruff чист. Live-модели не
вызывались; отдельного независимого security review нет.

## Остаток

Receipts для OpenAI-совместимых и Anthropic потоков (другая форма usage, нужна
схема receipt не только для Ollama); standalone email/CAD/VLM и прочие HTTP
recipients; доказанные pre-dispatch token/cost bounds; тарифы; active time,
replans, legacy reconciliation — E21 не закрыта.
