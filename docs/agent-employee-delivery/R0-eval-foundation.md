# R0 — foundation employee eval

Дата: 4 октября 2026. Исполнитель: `gpt-5.6-sol`, high; независимая приёмка: root.
Статус: REVIEWED / DEPLOYED в ограниченном foundation scope.

## Принятый объём

Строгая versioned схема employee cases/results, harness через common intake,
штатный dispatcher и durable worker с local fake agent, два reviewed demo cases.
Первый проверяет persisted domain read и digest значения fixture; второй
обнаруживает фактически записанный forbidden recipient event и выдаёт failed.
Это проверка harness, не успех/неуспех живой модели.

Guard до writes требует test environment, явно подтверждённую test DB и test-only
adapter. Неизвестные predicates, text-only acceptance, YAML callbacks,
неподдерживаемые роли и непустые grants отклоняются. Cleanup ограничен owner и
captured IDs; ошибки после fixture/intake/worker commit покрыты отдельно.
Unexpected effects входят в отрицательный verdict. Fake results исключены из
live metrics, неизвестные tokens/cost не заменяются измеренным нулём.

## Независимые проверки

- `python3 -m pytest backend/tests/test_employee_eval_harness.py backend/tests/test_work_budget_ledger.py backend/tests/test_work_order_lease.py -q`
  — **46 passed**, 1 существующее предупреждение `asyncio_loop_scope`.
- `ruff check backend/app/ai/evals/employee_cases.py backend/app/ai/evals/employee_harness.py backend/tests/test_employee_eval_harness.py`
  — passed.
- `git diff --check` — passed.

БД — отдельный `testcontainers:postgres:16-alpine`, не production. В review
проверены calls intake/claim/dispatch, settlement evidence, scope запросов и
cleanup, отрицательные verdicts и границы fake provider accounting.

## Production-проверка

`make prod-build` завершён успешно; backend и workers пересозданы. HTTPS
`curl -k --fail https://localhost/health` вне сетевого sandbox вернул
`{"status":"ok"}`. В sandbox localhost был недоступен; это не ошибка backend.
Compose core services healthy/running. Сборка ничего не запускает в eval-world:
новые modules не подключены к production routes и требуют test-only guard.

## Ограничения и следующий объём

Verification отключён тестовым adapter; WorkOrder не объявляется completed.
Settlement step/attempt/tool-call и employee predicates показаны раздельно.
Physical provider attempts в fake-world не доказывают budget для live paths.
Concurrency двух cases ещё не проверена; global patch существует только в
тестовом adapter, reusable harness globals не меняет.

R0 остаётся IN_PROGRESS: добавить concurrent isolation, воспроизводитель и
исправление глобальной выборки budget reservations, затем cases 3–10 с реальными
runtime границами. E49/E50 целиком не закрыты; 120-case corpus не создан.
