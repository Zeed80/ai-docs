# E05.2.9 — девятый срез простых DB write-адаптеров ToolResult

Дата: 19 сентября 2026. Статус: REVIEWED, DEPLOYED.

## Принятый срез и неизменный контракт

E05.2.9 добавляет на агентской HTTP-границе ровно две операции:

- `invoices.validate` — точный уникальный `POST /api/invoices/{invoice_id}/validate`;
- `memory.source_propose` — точный уникальный `POST /api/memory/sources/propose`.

Вместе с E05.2.1–E05.2.8 cumulative allowlist содержит ровно 26 операций:
`analytics.calendar_create_reminder`, `analytics.collection_add_item`,
`analytics.collection_close`, `analytics.collection_create`,
`analytics.compare_align`, `analytics.compare_create`,
`analytics.table_create_view`, `analytics.table_inline_edit`, `documents.link`,
`email.draft`, `email.templates.create`, `email.templates.update`,
`invoices.update`, `invoices.validate`, `memory.source_propose`,
`normalization.create_norm_card`, `normalization.update_canonical_item`,
`normalization.update_norm_card`, `payments.create_schedule`,
`procurement.create_request`, `suppliers.update`, `tool_catalog.create_supplier`,
`warehouse.adjust_stock`, `warehouse.create_item`, `warehouse.create_receipt` и
`warehouse.update_item`.

Контракт E05.2.1 не менялся. Только для точной capability/action-пары успешный
2xx domain response становится ToolResult v1 со статусом `succeeded` и исходным
payload в `data`; корректный v1 envelope повторно не оборачивается. Явная
domain-ошибка в 2xx response и 4xx дают `failed`; неterminal `partial`,
`waiting_approval` и `outcome_unknown` сохраняют свой статус. После возможного
dispatch 5xx, timeout, разрыв протокола или исключение дают `outcome_unknown`;
ошибка до dispatch — `failed` с `dispatch_attempted=false`. Автоматического
retry нет. Catalog, approval и отправка получают исходные arguments, а не
envelope. Публичные business API, схема БД, RBAC и approval policy не менялись.

## Доказательство границ и исключения

`validate_invoice` в `backend/app/api/invoices.py` имеет один прямой
безусловный `db.commit()` на success path. Арифметическая проверка
детерминирована и выполняется локально; `log_action` остаётся flush-only, поэтому
второй commit не создаётся.

`propose_web_source` в `backend/app/api/memory.py` имеет один прямой
безусловный `db.commit()` на success path и сохраняет только reviewable proposal
источника. В нём нет network/AI/external dispatch или enqueue. Обе catalog
operations имеют `admin_only=false` и не approval-gated. Это не ослабляет прочие
endpoint-policy проверки и не расширяет права пользователя.

Намеренно fail-closed остаются, в частности:

- `invoices.approve` и `invoices.receive` — status/approval semantics;
- `memory.source_discover` — имеет effects discovery;
- promotion — human-only; `memory.promotion_evaluate` — identity-path mismatch;
- sheets publish — publish-effect.

Остальные не принятые `one-db-commit` actions также не мигрированы этим срезом.

## Независимая проверка

Независимый полный набор из корня проекта завершился: 234 passed. Сохранено
известное предупреждение pytest о неизвестной настройке `asyncio_loop_scope`.
Проверены точная cumulative allowlist из 26 операций, две уникальные
route/action-пары, raw success payload, исходные arguments, double wrapping,
domain/HTTP error, отказ до dispatch и `outcome_unknown` после возможного
dispatch. Дополнительно проверены указанные fail-closed exclusions.

После документационных изменений выполнен `git diff --check`.

## Не завершено и production

E05.2 целиком остаётся IN PROGRESS: остальные `one-db-commit` операции не
мигрированы; E05.3 (async jobs), E05.4 (external/MCP handlers) и consumers E06
не затронуты. Следующий шаг — новый отдельно выбранный и независимо проверенный
срез E05.2, а не массовая миграция или переход к E05.3.

Сеньор выполнил `make prod-build`. Backend, frontend, `celery-worker`,
`celery-worker-gpu` и `celery-worker-lora` перезапущены и имеют состояние
`healthy`; `curl -k --fail https://localhost/health` вернул `{"status":"ok"}`.
SHA-256 `tool_transport.py` совпал в checkout, backend и `celery-worker`.
