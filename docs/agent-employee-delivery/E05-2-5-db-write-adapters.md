# E05.2.5 — пятый срез простых DB write-адаптеров ToolResult

Дата: 16 сентября 2026. Статус: REVIEWED, DEPLOYED.

## Принятый срез и неизменный контракт

E05.2.5 добавляет на агентской HTTP-границе ровно одну операцию:

- `procurement.create_request` — точный уникальный `POST /api/purchase-requests`.

Вместе с E05.2.1–E05.2.4 cumulative allowlist содержит ровно 16 операций:
`analytics.calendar_create_reminder`, `analytics.collection_add_item`,
`analytics.collection_close`, `analytics.collection_create`,
`analytics.compare_align`, `analytics.compare_create`,
`analytics.table_create_view`, `analytics.table_inline_edit`,
`email.templates.create`, `email.templates.update`,
`procurement.create_request`, `suppliers.update`, `warehouse.adjust_stock`,
`warehouse.create_item`, `warehouse.create_receipt` и `warehouse.update_item`.

Контракт E05.2.1 не менялся. Только для точной capability/action-пары успешный
2xx domain response становится ToolResult v1 со статусом `succeeded` и
исходным payload в `data`; корректный v1 envelope повторно не оборачивается.
Явная domain-ошибка в 2xx response и 4xx дают `failed`; неterminal `partial`,
`waiting_approval` и `outcome_unknown` сохраняют свой статус. После возможного
dispatch 5xx, timeout, разрыв протокола или исключение дают `outcome_unknown`;
ошибка до dispatch — `failed` с `dispatch_attempted=false`. Автоматического
retry нет. Catalog, approval и отправка получают исходные arguments, а не
envelope. Публичные business API, схема БД, RBAC и approval policy не менялись.

## Доказательство границ и исключения

`create_purchase_request` в `backend/app/api/procurement.py` имеет один прямой
безусловный `db.commit()` на success path. В этом handler-е нет помощников с
дополнительной commit-границей, external dispatch или enqueue.
`procurement.create_request` имеет `admin_only=false`, отсутствует в
`procurement.gate_actions` и не входит в approval gates. Это не отменяет прочие
endpoint-policy проверки и не расширяет права пользователя.

Намеренно fail-closed остаются:

- `procurement.update_request` и `procurement.update_contract`: оба принимают
  поле `status`, поэтому не относятся к узкому create-only срезу;
- `procurement.create_contract`: route alias `POST /api/compare` не доказывает
  уникальную capability/action-пару;
- `procurement.send_rfq`: имеет внешний эффект (`external-dispatch`).

## Safety correction: read-каталог с persistent effect

`suppliers.trust_score` остаётся catalog `GET`, но
`get_trust_score` условно сохраняет вычисленный `profile.trust_score` через
`db.commit()`. Поэтому операция внесена в
`READ_CATALOG_OPERATIONS_WITH_PERSISTENT_EFFECTS`: read retry запрещён как для
прямого маршрута `GET /api/suppliers/{supplier_id}/trust-score`, так и для
capability route `POST /api/agent/cap/suppliers` с `action=trust_score`.

Эта коррекция не превращает `suppliers.trust_score` в write adapter E05.2. Его
commit-граница условна (runtime 0/1 commit), а не один безусловный commit на
success path, поэтому операция остаётся вне cumulative allowlist и fail-closed.

## Независимая проверка

Принятый независимый целевой набор завершился: 207 passed, 0 failed,
0 skipped. Сохранено известное предупреждение pytest о неизвестной настройке
`asyncio_loop_scope`. Проверены точная cumulative allowlist из 16 операций,
единственная новая route/action-пара, raw success payload, исходные arguments,
double wrapping, domain/HTTP error, отказ до dispatch и `outcome_unknown` после
возможного dispatch. Дополнительно проверены fail-closed exclusions и запрет
read retry для обоих путей `suppliers.trust_score`.

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
