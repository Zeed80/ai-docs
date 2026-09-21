# E07 — масштабируемые квитанции логических действий

Дата: 21 сентября 2026. Статус: **SCOPED COMPLETE / REVIEWED / DEPLOYED**.

## Решение

ADR 005 выбирает dedicated таблицу `action_receipts` вместо уникальности в JSON
`WorkEvent`: `logical_action_id` имеет DB-level unique constraint и остаётся
стабильной идентичностью эффекта, тогда как `attempt_id` — изменяемый fencing
контекст.

Квитанция обязана связывать owner, WorkOrder, logical action, operation,
request/response digests, artifact ID/revision, receipt version и provenance.
Idempotency key не является bearer authorization. До первого effect повторно
проверяются current auth, common order lock, текущий attempt/lease/fence и
budget; effect и квитанция коммитятся одной транзакцией.

После commit source attempt квитанции либо текущий attempt того же logical action
могут только прочитать существующую квитанцию. Это receipt-only replay, не
разрешение исполнить эффект повторно; иной attempt отклоняется.

## Миграция и совместимость

Migration `20260913_0001` — единственный Alembic head. Она переносит только
криптографически и структурно valid, однозначные пилотные `WorkEvent` receipts:
включая правильные digests, action/attempt binding и artifact binding. Corrupt,
duplicate и неоднозначные события сохраняются в журнале без пересчёта hash и
остаются fail-closed. Runtime сначала читает таблицу, затем валидный legacy event
до завершения миграции.

## Проверка

Исполнитель: 29 focused и 76 expanded passed. Независимо: 29 focused passed,
один Alembic head, Ruff/format/diff clean. Покрыты два независимых соединения и
unique constraint, rollback, owner/arguments collision, cancel под common lock,
migration roundtrip, corrupt/duplicate/foreign-attempt receipt и backward read.

Root combined legacy journal/checkpoint run был прерван после 74 passed из-за
async teardown hang; он не считается успешным прогоном и не включён в результаты.

Production-стек пересобран и перезапущен через `make prod-build`.
Alembic применил `20260913_0001 (head)`; production PostgreSQL содержит
`action_receipts` и `uq_action_receipts_logical_action`. `/health` вернул
`{"status":"ok"}`; SHA-256 трёх runtime-модулей совпали на host, backend и
обычном Celery worker.

E07 закрывает только пилотный `agent_control.task_propose`. Другие recipients
остаются E08+.
