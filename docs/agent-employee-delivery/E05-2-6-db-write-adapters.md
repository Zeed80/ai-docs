# E05.2.6 — шестой срез простых DB write-адаптеров ToolResult

Дата: 16 сентября 2026. Статус: REVIEWED, DEPLOYED.

## Принятый срез и неизменный контракт

E05.2.6 добавляет на агентской HTTP-границе ровно три операции:

- `documents.link` — точный уникальный `POST /api/documents/{document_id}/links`;
- `email.draft` — точный уникальный `POST /api/email/drafts`;
- `payments.create_schedule` — точный уникальный `POST /api/payment-schedules`.

Вместе с E05.2.1–E05.2.5 cumulative allowlist содержит ровно 19 операций:
`analytics.calendar_create_reminder`, `analytics.collection_add_item`,
`analytics.collection_close`, `analytics.collection_create`,
`analytics.compare_align`, `analytics.compare_create`,
`analytics.table_create_view`, `analytics.table_inline_edit`, `documents.link`,
`email.draft`, `email.templates.create`, `email.templates.update`,
`payments.create_schedule`, `procurement.create_request`, `suppliers.update`,
`warehouse.adjust_stock`, `warehouse.create_item`, `warehouse.create_receipt`
и `warehouse.update_item`.

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

У каждого выбранного handler-а один прямой безусловный `db.commit()` на success
path. `link_document` вызывает `log_action`, который только добавляет audit-row и
выполняет `flush()`. `create_draft` вызывает `create_reply_draft`: он выполняет
только выборку ящика при необходимости, добавляет draft и выполняет `flush()`.
`create_payment_schedule` создаёт связанные calendar/reminder rows и вызывает
`log_action`; дополнительных commit-границ нет. Во всех трёх handler-ах нет AI,
network/external dispatch или enqueue.

Все три catalog operations имеют `admin_only=false` и не approval-gated. Это не
ослабляет прочие endpoint-policy проверки и не расширяет права пользователя.

Намеренно fail-closed остаются, в частности:

- `email.compose`, `email.reply` и `email.templates.from_message` — AI path;
- `sheets.create` — публикует в `chat_bus` после commit;
- send-операции с внешним эффектом, включая `email.send`;
- delete/status/render и approval-gated actions, а также все прочие
  `one-db-commit` строки без отдельного принятого среза.

## Независимая проверка

Принятый независимый целевой набор завершился: 216 passed, 0 failed,
0 skipped. Сохранено известное предупреждение pytest о неизвестной настройке
`asyncio_loop_scope`. Проверены точная cumulative allowlist из 19 операций,
три уникальные route/action-пары, raw success payload, исходные arguments,
double wrapping, domain/HTTP error, отказ до dispatch и `outcome_unknown` после
возможного dispatch. Дополнительно проверены fail-closed exclusions.

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
