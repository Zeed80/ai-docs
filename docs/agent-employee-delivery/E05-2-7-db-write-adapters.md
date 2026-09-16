# E05.2.7 — седьмой срез простых DB write-адаптеров ToolResult

Дата: 16 сентября 2026. Статус: REVIEWED, DEPLOYED.

## Принятый срез и неизменный контракт

E05.2.7 добавляет на агентской HTTP-границе ровно три операции:

- `normalization.create_norm_card` — точный уникальный `POST /api/normalization/norm-cards`;
- `normalization.update_norm_card` — точный уникальный `PATCH /api/normalization/norm-cards/{card_id}`;
- `normalization.update_canonical_item` — точный уникальный `PATCH /api/normalization/canonical-items/{item_id}`.

Вместе с E05.2.1–E05.2.6 cumulative allowlist содержит ровно 22 операции:
`analytics.calendar_create_reminder`, `analytics.collection_add_item`,
`analytics.collection_close`, `analytics.collection_create`,
`analytics.compare_align`, `analytics.compare_create`,
`analytics.table_create_view`, `analytics.table_inline_edit`, `documents.link`,
`email.draft`, `email.templates.create`, `email.templates.update`,
`normalization.create_norm_card`, `normalization.update_canonical_item`,
`normalization.update_norm_card`, `payments.create_schedule`,
`procurement.create_request`, `suppliers.update`, `warehouse.adjust_stock`,
`warehouse.create_item`, `warehouse.create_receipt` и `warehouse.update_item`.

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

Каждому выбранному действию соответствует уникальный маршрут и один прямой
безусловный `db.commit()` на success path. `create_norm_card` и
`update_canonical_item` вызывают `log_action`; audit helper только добавляет
audit-row и выполняет `flush()`, поэтому второй commit не создаётся.
`update_norm_card` не вызывает audit helper. Во всех трёх handler-ах нет AI,
network/external dispatch или enqueue.

Все три catalog operations имеют `admin_only=false` и не approval-gated. Это не
ослабляет другие endpoint-policy проверки и не расширяет права пользователя.

Намеренно fail-closed остаются, в частности:

- `analytics.auto_approval_create` — `admin_only=true`;
- `analytics.auto_approval_check` — conditional runtime 0/1 commit;
- `payments.mark_paid` — approval-gated;
- notification/settings — не являются активным catalog operations;
- sheets publish — остаётся вне среза из-за publish-effect.

Остальные не принятые `one-db-commit` actions также не мигрированы этим срезом.

## Независимая проверка

Независимый полный набор из корня проекта завершился: 225 passed. Сохранено
известное предупреждение pytest о неизвестной настройке `asyncio_loop_scope`.
Проверены точная cumulative allowlist из 22 операций, три уникальные
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
