# E05.2.3 — третий срез простых DB write-адаптеров ToolResult

Дата: 16 сентября 2026. Статус: REVIEWED, DEPLOYED.

## Принятый срез и неизменный контракт

E05.2.3 добавляет на агентской HTTP-границе ровно три операции:

- `warehouse.update_item` — `PATCH /api/warehouse/inventory/{item_id}`;
- `warehouse.adjust_stock` — `POST /api/warehouse/inventory/{item_id}/adjust`;
- `warehouse.create_receipt` — `POST /api/warehouse/receipts`.

Все три маршрута уникальны для точной capability/action-пары, поэтому adapter
не полагается на неоднозначный route alias. Вместе с E05.2.1 и E05.2.2
cumulative allowlist содержит ровно 12 операций:
`analytics.calendar_create_reminder`, `analytics.collection_add_item`,
`analytics.collection_close`, `analytics.collection_create`,
`analytics.compare_align`, `analytics.compare_create`,
`analytics.table_create_view`, `analytics.table_inline_edit`,
`warehouse.adjust_stock`, `warehouse.create_item`,
`warehouse.create_receipt` и `warehouse.update_item`.

Контракт E05.2.1 не менялся. Только для точной capability/action-пары успешный
2xx domain response становится ToolResult v1 со статусом `succeeded` и
исходным payload в `data`; корректный v1 envelope повторно не оборачивается.
Явная domain-ошибка в 2xx response и 4xx дают `failed`; неterminal `partial`,
`waiting_approval` и `outcome_unknown` сохраняют свой статус. После возможного
dispatch 5xx, timeout, разрыв протокола или исключение дают `outcome_unknown`;
ошибка до dispatch — `failed` с `dispatch_attempted=false`. Автоматического
retry нет. Catalog, approval и отправка получают исходные arguments, а не
envelope. Public business API, RBAC и схема БД не менялись.

## Доказательство DB-границ и исключения

- `update_inventory_item` в `backend/app/api/warehouse.py` имеет один прямой
  безусловный `db.commit()` на success path.
- `adjust_stock` в том же модуле имеет один прямой безусловный `db.commit()` на
  success path; вызванный `log_action` добавляет запись и делает только
  `flush()`.
- `create_receipt` имеет один прямой безусловный `db.commit()` после создания
  ордера и строк; вызванные `log_action` и `add_timeline_event` делают только
  `flush()` и не образуют вторую commit-границу.

В этих трёх handler-ах нет внешнего dispatch или enqueue. Операции не входят в
`warehouse.gate_actions` манифеста. Это не отменяет иных policy/RBAC проверок и
не расширяет approval policy.

Намеренно исключены `warehouse.confirm_receipt`, `warehouse.issue_stock`,
`warehouse.delete_item`, `warehouse.update_status` и `warehouse.bulk_confirm`.
Они не получили ToolResult-адаптер в E05.2.3 и сохраняют прежний контракт до
отдельно выбранного fail-closed среза.

## Независимая проверка

Принятый независимый целевой набор завершился: 199 passed, 0 failed, 0 skipped.
Сохранено известное предупреждение pytest о неизвестной настройке
`asyncio_loop_scope`. Проверены точный cumulative allowlist из 12 операций,
три новые уникальные route/action-пары, raw success payload, исходные arguments,
double wrapping, domain/HTTP error, отказ до dispatch и `outcome_unknown` после
возможного dispatch; исключённые warehouse-операции остаются fail-closed.

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
