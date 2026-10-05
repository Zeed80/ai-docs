# E21.2b9 — Фактические provider usage receipts

Дата: 5 октября 2026. Исходный commit: `19c335c6`.
Исполнитель: `gpt-5.6-sol`, high; root — независимая приёмка и выпуск.
Статус: TESTED / REVIEWED / DEPLOYED (5 октября 2026).

## Ограниченный контракт

Atomic settlement физической `llm_calls` reservation сохраняет versioned usage
receipt в существующем WorkEvent, без schema migration и повторного reserve.
Receipt связывается с точной reservation, ledger, work owner, operation key и
request/binding digests; повтор не создаёт второй event и не меняет наблюдение.
Историческая charged reservation без receipt не превращается в доказанные usage.

Первый supported raw-count route — direct Ollama text helpers, включая
server-bound SQL recipient и explicit detached planner/verifier. Reasoning wrapper
не считается второй физической попыткой. Другие paths могут иметь только
explicit unknown или missing receipt; полный accounting этим не объявляется.

Missing/invalid input/output counts сохраняются как unknown. Стоимость без
versioned tariff — unknown, не 0. Aggregate общего parent/child ledger отличает
known lower bound от полной суммы и учитывает физические retry/error/cancel/crash
attempts с отсутствующим receipt консервативно. Zero-unit execution/recipient
fences в число physical attempts не входят. Owner/ledger binding обязателен.

Finite token/cost caps без pre-dispatch upper bound остаются fail-closed.
Receipt не разрешает dispatch, retry, replay, автоматическое legacy reconciliation
или новую ревизию работы. Observed usage не теряется при stale postflight;
settlement failure остаётся sticky budget stop.

## Независимые критерии root

| Сценарий | Ожидаемое наблюдение |
|---|---|
| Explicit provider integer counts 0/0 | Known observed zero при terminal proof |
| Missing/invalid/partial counts | Unknown total, known part остаётся lower bound |
| Model retry после timeout | Обе reservations учитываются; failed attempt не становится нулём |
| Валидные raw counts, невалидный generated JSON | Расход physical attempt сохраняется до format recovery |
| Stale/cancel на client close | Observed usage сохраняется, ответ не выдаётся дальше |
| Settlement commit failure | Reservation не освобождается, typed sticky stop, сводка unknown |
| Повтор с тем же receipt | Один event/charge, не новое разрешение на dispatch |
| Повтор с изменёнными counts | Conflict, прежнее наблюдение не меняется |
| Историческая charge без receipt | Unknown coverage, нет заднего известного расхода |
| Parent/child и чужой owner | Один общий ledger; чужой binding отклонён |
| Zero-unit execution/recipient fences | Не считаются physical model attempts |
| Unsupported caller/missing receipt | Явный unknown, не объявление полного accounting |

## Приёмка

Контракт и allowed paths переданы исполнителю: domain ledger/usage, budget context,
direct Ollama client и два новых тестовых профиля. Дополнительно разрешены только
charge-method/import отдельного planner_budget_context и перенос failure injection
в трёх прежних direct/planner/verifier tests на новую atomic boundary, без снятия
assertions. Прочие production-файлы не разрешены. Baseline root до правок: ledger/lease/employee fake corpus —
**57 passed** в отдельной testcontainers PostgreSQL; warning asyncio_loop_scope.
Это не заменяет приёмку окончательного diff. После frozen diff:
read-only security review, isolated testcontainers profiles, Ruff/diff checks,
production rebuild, HTTPS health и runtime hashes. Live model/paid provider,
production data migration и push не входят в scope. Existing WIP не проверен.

### Frozen diff: независимые проверки

Исполнитель остановил правки перед root-приёмкой. Его scoped профили: 97 passed.
Root повторил проверки в отдельных testcontainers PostgreSQL с
`POSTGRES_HOST=127.0.0.1 POSTGRES_PORT=1` и `-q --tb=short`:

- Usage/domain/direct, HTTP recipient, direct text, AIRouter, provider, tools,
  durable chat, checkpoints, execution boundary, SQL perimeter и generic work
  orders: **274 passed**.
- Legacy ledger/lease и employee fake corpus: **57 passed** отдельным процессом.
- Detached planner: **23 passed** отдельным процессом.
- Detached verifier: **17 passed** отдельным процессом.

Итого **371 passed**, 0 failed; известный `asyncio_loop_scope` config warning.
Первый широкий запуск завершился до collection из-за ошибочного имени SQL
perimeter test; исправленный `test_agent_sql_perimeter.py` входит в итоговый
успешный профиль. Ruff по десяти production/test files и `git diff --check` чисты.
Security review и production-выпуск ещё не завершены.

Первый независимый read-only review выявил medium: Python dict equality принимал
`total.units=True/1.0` за integer 1; low: idempotent charged branch недостаточно
проверял actual/unknown/settled_at. Root отдельно воспроизвёл TypeError при JSON
list в unknown reason. Исполнителю передан один ограниченный цикл исправлений
с negative regressions; прежние 371 tests относятся к первой frozen версии.

Доказаны: strict raw counts и explicit zero, exact/changed duplicate, concurrent
duplicate, rollback event flush вместе с charge, запрет legacy reconciliation,
frozen owner, общий parent/child ledger, повреждённые receipt/settlement,
transport retry, format retry после invalid generated JSON, HTTP/body errors,
client-close exception, stale postflight и cancellation после наблюдения counts.
Reasoning wrapper оставляет один leaf receipt; recipient и detached contexts
используют ту же atomic boundary.

### Цикл исправлений и окончательная приёмка

Исправления после первого review внесены: strict integer-проверка `total.units`
до сравнения словарей (regression `[True, 1.0]`), `isinstance(reason, str)` до
проверки членства во frozenset (regression JSON list в reason), charged-ветка
идемпотентного повтора сверяет `actual_units`, `actual_unknown`, `settled_at`
и наличие receipt. Повторный read-only review окончательного diff (root):
порядок блокировок WorkOrder → ledger → reservation совпадает с `append_event`
и не даёт гонки sequence; все budgeted paths пропускают только `provider=ollama`,
поэтому llama.cpp/cloud не получают receipt с чужим provider; fallback
`expected_owner_key or ""` недостижим (direct/AIRouter требуют frozen owner).
Новых findings нет.

Окончательные профили, отдельные testcontainers PostgreSQL
(`POSTGRES_HOST=127.0.0.1 POSTGRES_PORT=1`, без `TEST_DATABASE_URL`):

- usage/domain/direct, HTTP recipient, direct text, AIRouter, provider, tools,
  durable chat, checkpoints, SQL perimeter, work orders, headless, ToolResult:
  **350 passed**;
- legacy ledger/lease и employee fake corpus: **57 passed**;
- detached planner: **23 passed**; detached verifier: **17 passed**.

Итого **447 passed**, 0 failed; известный warning `asyncio_loop_scope`.
Ruff по десяти файлам и `git diff --check` чисты; pre-commit `ruff format` изменил
только раскладку строк в 8 файлах (AST идентичен), выпуск пересобран из коммита.

Выпуск: `make prod-build` (exit 0), backend/celery healthy,
`curl -k --fail https://localhost/health` → `{"status":"ok"}`. SHA-256 пяти
production-файлов в `infra-backend-1` и `infra-celery-worker-1` совпадают с
принятым деревом. Live model/paid provider не вызывались; health не доказывает
живой расход токенов.

### Остаток

Новый owner-bound domain aggregate не является public budget UI/API. Он сообщает
coverage только записанных physical LLM reservations. AIRouter и streaming
AgentSession пока вызывают legacy charge без usage receipt; их missing evidence
остаётся unknown. Standalone email/CAD/VLM и другие HTTP recipients не мигрированы.
Стоимость, конечные token/cost bounds, active time/replans и вся E21 не завершены.
