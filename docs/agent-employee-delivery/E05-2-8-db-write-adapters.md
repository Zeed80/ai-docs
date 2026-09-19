# E05.2.8 — восьмой срез простых DB write-адаптеров ToolResult

Дата: 19 сентября 2026. Статус: REVIEWED, DEPLOYED.

## Принятый срез и неизменный контракт

E05.2.8 добавляет на агентской HTTP-границе ровно две операции:

- `invoices.update` — точный уникальный `PATCH /api/invoices/{invoice_id}`;
- `tool_catalog.create_supplier` — точный уникальный `POST /api/tool-catalog/suppliers`.

Вместе с E05.2.1–E05.2.7 cumulative allowlist содержит ровно 24 операции:
`analytics.calendar_create_reminder`, `analytics.collection_add_item`,
`analytics.collection_close`, `analytics.collection_create`,
`analytics.compare_align`, `analytics.compare_create`,
`analytics.table_create_view`, `analytics.table_inline_edit`, `documents.link`,
`email.draft`, `email.templates.create`, `email.templates.update`,
`invoices.update`, `normalization.create_norm_card`,
`normalization.update_canonical_item`, `normalization.update_norm_card`,
`payments.create_schedule`, `procurement.create_request`, `suppliers.update`,
`tool_catalog.create_supplier`, `warehouse.adjust_stock`,
`warehouse.create_item`, `warehouse.create_receipt` и `warehouse.update_item`.

Контракт E05.2.1 не менялся. Только для точной capability/action-пары успешный
2xx domain response становится ToolResult v1 со статусом `succeeded` и исходным
payload в `data`; корректный v1 envelope повторно не оборачивается. Явная
domain-ошибка в 2xx response и 4xx дают `failed`; неterminal `partial`,
`waiting_approval` и `outcome_unknown` сохраняют свой статус. После возможного
dispatch 5xx, timeout, разрыв протокола или исключение дают `outcome_unknown`;
ошибка до dispatch — `failed` с `dispatch_attempted=false`. Автоматического retry
нет. Catalog, approval и отправка получают исходные arguments, а не envelope.
Публичные business API, схема БД, RBAC и approval policy не менялись.

## Доказательство границ и исключения

`update_invoice` в `backend/app/api/invoices.py` имеет один прямой безусловный
`db.commit()` на success path. Он вызывает `log_action` и `add_timeline_event`;
оба помощника только добавляют записи и выполняют `flush()`, поэтому второй
commit не создаётся. `InvoiceFieldUpdate` не содержит поля `status`, так что
срез не расширяет status-переходы или approval semantics.

`create_supplier` в `backend/app/api/tool_catalog.py` — DB-only handler с одним
прямым безусловным `db.commit()` на success path. В обоих обработчиках нет
network/external dispatch, AI или enqueue.

Обе catalog operations имеют `admin_only=false`, отсутствуют в соответствующих
`gate_actions` и не approval-gated. Это не ослабляет прочие endpoint-policy
проверки и не расширяет права пользователя. Все остальные `one-db-commit`
операции остаются fail-closed до отдельного принятого среза.

## Независимая проверка

Независимый полный набор из корня проекта завершился: 230 passed. Сохранено
известное предупреждение pytest о неизвестной настройке `asyncio_loop_scope`.
Проверены точная cumulative allowlist из 24 операций, две уникальные
route/action-пары, raw success payload, исходные arguments, double wrapping,
domain/HTTP error, отказ до dispatch и `outcome_unknown` после возможного
dispatch. Дополнительно проверены указанные fail-closed границы.

После документационных изменений выполнен `git diff --check`.

## Не завершено и production

E05.2 целиком остаётся IN PROGRESS: остальные `one-db-commit` операции не
мигрированы; E05.3 (async jobs), E05.4 (external/MCP handlers) и consumers E06
не затронуты. Следующий шаг — новый отдельно выбранный и независимо проверенный
срез E05.2, а не массовая миграция или переход к E05.3.

Production собран из отдельного clean worktree текущего HEAD, чтобы не включать
чужой незакоммиченный provider/frontend WIP. После сборки orchestration
восстановлен из основного checkout через `docker compose up -d --no-build`.
Backend, frontend и три worker-а имеют состояние `healthy`;
`curl -k --fail https://localhost/health` вернул `{"status":"ok"}`. SHA-256
`tool_transport.py` совпал в clean worktree, backend и `celery-worker`.
