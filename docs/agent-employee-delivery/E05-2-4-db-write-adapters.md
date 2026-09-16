# E05.2.4 — четвёртый срез простых DB write-адаптеров ToolResult

Дата: 16 сентября 2026. Статус: REVIEWED, DEPLOYED.

## Принятый срез и неизменный контракт

E05.2.4 добавляет на агентской HTTP-границе ровно три операции:

- `email.templates.create` — `POST /api/email-templates/`;
- `email.templates.update` — `PATCH /api/email-templates/{template_id}`;
- `suppliers.update` — `PATCH /api/suppliers/{supplier_id}`.

Все три маршрута уникальны для точной capability/action-пары. Вместе с
E05.2.1–E05.2.3 cumulative allowlist содержит ровно 15 операций:
`analytics.calendar_create_reminder`, `analytics.collection_add_item`,
`analytics.collection_close`, `analytics.collection_create`,
`analytics.compare_align`, `analytics.compare_create`,
`analytics.table_create_view`, `analytics.table_inline_edit`,
`email.templates.create`, `email.templates.update`, `suppliers.update`,
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
envelope. Публичные business API, схема БД, RBAC и approval policy не менялись.

## Доказательство DB-границ и исключения

- `create_template` в `backend/app/api/email_templates.py` имеет один прямой
  безусловный `db.commit()` на success path. Вспомогательные проверки slug и
  переменных не создают дополнительной commit-границы.
- `update_template` в том же модуле имеет один прямой безусловный
  `db.commit()` на success path; чтение шаблона и вычисление переменных не
  добавляют commit.
- `update_supplier` в `backend/app/api/suppliers.py` имеет один прямой
  безусловный `db.commit()` на success path. Вызванный `log_action` делает
  только `flush()`; select/flush-помощники второй commit-границы не образуют.

В выбранных handler-ах нет external dispatch или enqueue. У трёх catalog
operations `admin_only=false`; они не входят в approval-gated actions. Это не
отменяет прочие endpoint-policy проверки и не расширяет права пользователя.

Намеренно исключён `email.templates.from_message`: при
`extract_variables=true` путь может вызвать `ai_router.complete`, поэтому его
нельзя выдавать за простой DB write без внешнего/AI эффекта. Также fail-closed
остаются `analytics.calendar_generate_followup`: catalog задаёт
`{entity_id}`, тогда как recipient route принимает `{reminder_id}`, то есть
identity path-параметра не доказана. Операции render/delete/status и действия с
approval gate в этот срез не входят и сохраняют прежний контракт до отдельного
проверенного решения.

## Независимая проверка

В корне репозитория принят целевой набор:

```bash
python3 -m pytest backend/tests/test_tool_transport.py backend/tests/test_tool_result_contract.py backend/tests/test_capability_catalog_consistency.py backend/tests/test_agent_execution_boundary.py backend/tests/test_work_order_checkpoint.py -q
```

Принятый независимый целевой набор завершился: 205 passed, 0 failed,
0 skipped. Сохранено известное предупреждение pytest о неизвестной настройке
`asyncio_loop_scope`. Проверены точный cumulative allowlist из 15 операций,
три новые уникальные route/action-пары, raw success payload, исходные
arguments, double wrapping, domain/HTTP error, отказ до dispatch и
`outcome_unknown` после возможного dispatch; исключённые операции остаются
fail-closed.

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
