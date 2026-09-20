# E05.2.10 — десятый срез простых DB write-адаптеров ToolResult

Дата: 19 сентября 2026. Статус: REVIEWED, DEPLOYED.

## Принятый срез и неизменный контракт

E05.2.10 добавляет на агентской HTTP-границе ровно две операции:

- `tech.correction_record` — точный уникальный `POST /api/technology/corrections`;
- `tech.operation_template_create` — точный уникальный
  `POST /api/technology/operation-templates`.

Вместе с E05.2.1–E05.2.9 cumulative allowlist содержит ровно 28 операций:
`analytics.calendar_create_reminder`, `analytics.collection_add_item`,
`analytics.collection_close`, `analytics.collection_create`,
`analytics.compare_align`, `analytics.compare_create`,
`analytics.table_create_view`, `analytics.table_inline_edit`, `documents.link`,
`email.draft`, `email.templates.create`, `email.templates.update`,
`invoices.update`, `invoices.validate`, `memory.source_propose`,
`normalization.create_norm_card`, `normalization.update_canonical_item`,
`normalization.update_norm_card`, `payments.create_schedule`,
`procurement.create_request`, `suppliers.update`, `tech.correction_record`,
`tech.operation_template_create`, `tool_catalog.create_supplier`,
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

`record_correction` и `create_operation_template` в
`backend/app/api/technology.py` имеют по одному прямому безусловному
`db.commit()` на success path и точную уникальную POST route/action-пару.
Предшествующие helper-вызовы ограничены `flush()` или `select()`; второго commit
они не создают. В выбранных handlers нет AI, network/external dispatch, enqueue
или `chat_bus` publish.

Обе catalog operations имеют `admin_only=false`, отсутствуют в approval-gated
наборах и не расширяют права пользователя. Неразрешёнными и fail-closed остаются:

- `normalization.suggest_rule` и `normalization.apply_rules` — условная граница
  commit 0/1;
- `sheets.add_row` — publish effect;
- `tech.resource_create` — входная модель принимает `status`;
- `tech.learning_rule_activate` и `tech.learning_rule_reject` — approval-gated;
- прочие lifecycle/status, approval-gated, external и не доказанные отдельно
  `one-db-commit` actions.

## Независимая проверка

Независимый полный набор из корня проекта завершился: 238 passed. Сохранено
известное предупреждение pytest о неизвестной настройке `asyncio_loop_scope`.
Проверены точная cumulative allowlist из 28 операций, две уникальные
route/action-пары, raw success payload, исходные arguments, double wrapping,
domain/HTTP error, отказ до dispatch и `outcome_unknown` после возможного
dispatch. Дополнительно проверены указанные fail-closed границы.

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
