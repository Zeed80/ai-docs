# R0 — десять outcome-сценариев и изоляция

Дата: 4 октября 2026. Исполнитель: `gpt-5.6-sol`, high. Приёмка: root.
Статус: REVIEWED / DEPLOYED в ограниченном R0 scope.
Предыдущий принятый foundation: `b521fc3a`, `R0-eval-foundation.md`.

## Принятый результат

Versioned employee corpus содержит десять содержательных local-fake cases:
read outcome, обнаруживаемый forbidden write, concurrent duplicate common intake,
отказ foreign attachment, обязательный approval до эффекта, lost-response atomic
receipt, shared last budget slot, provider error без fallback, cancel перед
следующим tool, сохранённый recipient outcome при settlement failure.

Сценарии используют production intake/dispatcher/durable worker и существующие
approval/receipt/budget/cancel boundaries, не новую policy или keyword routing.
Positive verdict означает выполнение заранее заданных predicates: безопасный
отказ может быть ожидаемым outcome. Намеренно нарушающий demo остаётся failed.
Case6 явно объявляет admin fixture; direct recipient route проверяет receipt
fencing, но не HTTP dependency-RBAC. Обычные роли/grants не расширяются молча.

Проверены concurrent runs с разными owners/namespaces и общим сериализованным
test-adapter patch lifecycle, cleanup после потерянного replay-response и
отрицательный predicate при второй namespace AgentTask. Cleanup не удаляет
произвольные orphan задачи по одному совпавшему metadata tag.

## Изоляция ledger

Исполнитель зафиксировал red-run legacy unscoped assertion: winner и foreign
reservation дали `2 != 1`. Финальный тест выбирает exact shared ledger и
получает один reservation; foreign row остаётся нетронутой. Contamination witness
сам ограничен captured reservation IDs, чтобы повторно не создать global query.
Runtime ledger/policy не изменялись; исправлены только test fixture/query.

## Независимые проверки

`python3 -m pytest backend/tests/test_employee_eval_harness.py backend/tests/test_work_budget_ledger.py backend/tests/test_work_order_lease.py backend/tests/test_action_receipts.py backend/tests/test_work_budget_tools.py backend/tests/test_chat_checkpoints.py -q`

**180 passed**, 1 существующее предупреждение `asyncio_loop_scope`, 59.91 s.
БД: отдельный testcontainers PostgreSQL; никаких production/customer fixtures.
Ruff scoped и `git diff --check` — passed.
После форматирующего pre-commit hook и удаления неисполняемых дубликатов имён
импортов повторный независимый профиль employee harness + ledger — **43 passed**,
1 прежнее предупреждение; Ruff format/check — passed. Сборка/хеши повторены
на окончательном дереве, не на версии до hook.
Исполнитель отдельно: employee harness **22 passed**, ledger **21 passed**,
финальный regression **1 passed**; эти результаты не складываются со 180.

## Production evidence

`make prod-build` завершён успешно. Backend и workers пересозданы, core services
healthy/running, `curl -ksS --fail https://localhost/health` → `{"status":"ok"}`.
SHA-256 schema, harness и corpus совпали между checkout и backend container.
Для localhost/Docker проверок потребовался выход из sandbox; ограничения не
обходились. Ни eval run в production, ни live LLM/effect не запускались.

## Границы приёмки

R0 принят только как fake harness + начальные десять cases и указанный isolation
regression. E50 остаётся 10/120; полноценные browser/script/knowledge scenarios
добавляются после реализации capabilities. E49/P8 целиком не закрываются этим
срезом. Весь backend suite не запускался и зелёным не объявляется.

Verification dispatch выключен test adapter; WorkOrder completion не заявляется.
Physical attempt accounting exercised local patched provider path не является
живым provider run. Model success rate, autonomy и реальная стоимость не измерены;
fake results исключены из live metrics. Никаких production migration, live/paid
providers, внешних получателей и rollout не разрешалось и не выполнялось.

Следующая delivery-задача: P1.1 caller inventory и оставшиеся E21 headless paths;
контрактные срезы E38/E42 доступны по DAG. Новая возможность не включается до gate.
