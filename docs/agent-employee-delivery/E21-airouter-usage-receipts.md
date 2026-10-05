# E21.2b10 — Usage receipts для durable AIRouter Ollama attempts

Дата: 5 октября 2026. Исходный commit: `80ac7a45` (E21.2b9).
Исполнитель и приёмка: Claude Code (root); codex-исполнители в этом срезе не
назначались.

## Ограниченный контракт

Durable AIRouter (E21.2b6) уже резервирует и списывает каждую physical Ollama
попытку, но списывал её legacy `settle_budget` без usage evidence. Срез переводит
его на атомарную границу E21.2b9: charge и `llm_usage_receipt.v1` пишутся одной
транзакцией.

Evidence снимается на HTTP-границе `OllamaProvider.chat/structured_extract` из
сырого JSON body (`observe_ollama_response`), а не из `AIResponse.usage`:
Pydantic `AIUsage` в lax-режиме приводит `"3"` → 3 и `True` → 1 и не хранит
`done`. Router открывает task-local `capture_ollama_usage()` ровно на один
budgeted dispatch и передаёт evidence в `charge_provider_call`, в том числе при
исключении и отмене. Без активного capture поведение провайдера прежнее.

Бюджетированный AIRouter по-прежнему допускает только `provider=ollama`,
текстовые задачи без изображений (`begin_airouter_call`), то есть один POST на
dispatch. Второй ответ под одним capture делает receipt unknown навсегда.
Провайдер без HTTP-границы (fake) получает явный `response_not_observed`, не 0.

Попутно исправлен дефект провайдера, найденный тестом: нецелый счётчик в теле
ронял `_sum_optional` (`TypeError`) и выбрасывал валидный ответ модели.
`AIUsage` теперь принимает только настоящие неотрицательные integer, иначе
`None` — без исключения и без приведения.

## Сценарии и тесты

`backend/tests/test_work_budget_usage_airouter.py` (реальный `OllamaProvider`,
подменён только HTTP-клиент):

| Сценарий | Наблюдение |
|---|---|
| Format retry, два POST | Два charged reservation, два known receipt, сводка 30 known |
| `"3"`/`True` в счётчиках | Ответ применён; receipt `invalid_type`; `AIUsage` = None |
| Нет `done` | `terminal_not_observed`, счётчики — только lower bound (partial) |
| HTTP 503 / invalid body / timeout | Charged + `http_error` / `response_body_invalid` / `response_not_observed` |
| Stale postflight после ответа | Наблюдённые 22 tokens сохранены, ответ не применён |
| Отмена во время POST | Charged + `response_not_observed` |
| Сбой settlement | Sticky `llm_budget_settlement_unavailable`, reservation `reserved`, без retry; сводка — missing receipt |
| Fake provider без HTTP | Unknown, не 0 |
| Capture вне dispatch | Не утекает; без capture семантика `raise_for_status/json` прежняя |

`test_work_budget_airouter.py`: инъекция отказа settlement перенесена на
`settle_llm_call_with_usage_receipt` (как в b9 для direct/planner/verifier),
assertions не сняты.

## Приёмка

Отдельные testcontainers PostgreSQL (`POSTGRES_HOST=127.0.0.1 POSTGRES_PORT=1`,
без `TEST_DATABASE_URL`), `-q --tb=short`:

- budget/durable профиль b9 + новый AIRouter usage: **363 passed**;
- router/provider/routing (format recovery, inference params, thinking, slot
  policy, confidentiality, registry, reranker, catalog/drawing routing): **179
  passed, 1 skipped**;
- legacy ledger/lease и employee fake corpus: **57 passed**;
- detached planner **23 passed**, detached verifier **17 passed**.

Итого **639 passed, 1 skipped**, 0 failed. Live-тесты моделей не запускались.
Ruff check/format и `git diff --check` чисты. Отдельного независимого
security review нет: автор и приёмка — один агент; review-граница — тот же
атомарный settlement E21.2b9, новая поверхность — task-local capture.

Выпуск из commit `ffcbec17`: `make prod-build` exit 0, backend/celery healthy,
`https://localhost/health` → `{"status":"ok"}`; SHA-256 `work_budget_usage.py`,
`router.py`, `providers/ollama.py` в `infra-backend-1` и `infra-celery-worker-1`
совпадают с HEAD. Health не доказывает живой расход токенов.

## Остаток

Streaming AgentSession (`agent_loop.py`, `_call_*_streaming`) списывает попытки
legacy `charge_provider_call` без receipt — в сводке это missing/unknown.
Standalone email/CAD/VLM и прочие HTTP recipients не мигрированы. Стоимость без
versioned tariff — unknown. Конечные token/cost caps без pre-dispatch upper
bound остаются fail-closed. Active time, replans, legacy reconciliation и вся
E21 не завершены.
