# E05.2.2 — второй срез простых DB write-адаптеров ToolResult

Дата: 16 сентября 2026. Статус: REVIEWED, DEPLOYED.

## Принятый срез и неизменный контракт

E05.2.2 добавляет на агентской HTTP-границе ровно пять операций:

- `analytics.collection_add_item`;
- `analytics.collection_close`;
- `analytics.compare_create`;
- `analytics.compare_align`;
- `analytics.table_inline_edit`.

Вместе с E05.2.1 cumulative allowlist содержит ровно девять операций:
`analytics.calendar_create_reminder`, `analytics.collection_add_item`,
`analytics.collection_close`, `analytics.collection_create`,
`analytics.compare_align`, `analytics.compare_create`,
`analytics.table_create_view`, `analytics.table_inline_edit` и
`warehouse.create_item`.

Контракт не менялся относительно E05.2.1. Только для точной
capability/action-пары успешный 2xx domain response становится ToolResult v1 со
статусом `succeeded` и исходным payload в `data`; корректный v1 envelope не
оборачивается повторно. Явная domain-ошибка в 2xx response и 4xx дают `failed`.
Неterminal `partial`, `waiting_approval` и `outcome_unknown` сохраняют исходный
статус. После возможного dispatch 5xx, timeout, разрыв протокола или исключение
дают `outcome_unknown`; ошибка до dispatch — `failed` с
`dispatch_attempted=false`. Автоматического retry нет. Catalog, approval и
отправка продолжают получать исходные arguments, а не новый envelope.

Публичные business API, RBAC и схема БД не менялись.

## Доказательство границ и fail-closed

В каждом recipient handler выбранной пятёрки есть ровно один прямой
`db.commit`. Вспомогательные `add_timeline_event` и `log_action` выполняют
только `flush()` и не создают дополнительной commit-границы.

`analytics.compare_create` разделяет прямой маршрут `POST /api/compare` с
`procurement.create_contract`. Поэтому один метод/route без точной
capability/action-идентичности не выбирает адаптер: route alias обрабатывается
direct fail-closed, без догадки о намерении вызывающего. Остальные route/action,
не входящие в allowlist, также сохраняют прежний контракт.

`analytics.calendar_extract_dates` намеренно исключён: в runtime у handler
наблюдается 0/1 commit-граница, поэтому его нельзя считать доказанным простым
`one-db-commit` срезом.

## Не завершено

E05.2 целиком остаётся IN PROGRESS. Наличие девяти операций в cumulative
allowlist не завершает остальные строки E03 класса `one-db-commit`; E05.3
(async jobs), E05.4 (external/MCP handlers) и consumers E06 этим срезом не
затронуты. Следующий шаг — новый отдельно выбранный и независимо проверенный
срез E05.2, а не массовая миграция или переход к E05.3.

## Независимая проверка

В корне репозитория принят целевой набор:

```bash
python3 -m pytest backend/tests/test_tool_transport.py backend/tests/test_tool_result_contract.py backend/tests/test_capability_catalog_consistency.py backend/tests/test_agent_execution_boundary.py backend/tests/test_work_order_checkpoint.py -q
```

Результат независимого прогона: 190 passed, 0 failed, 0 skipped. Сохранено
известное предупреждение pytest о неизвестной настройке `asyncio_loop_scope`.
Проверены точный cumulative allowlist из девяти операций, пять новых
route/action-пар, raw success payload, сохранение исходных args, double wrapping,
domain/HTTP error, отказ до dispatch, outcome unknown после dispatch и
fail-closed route alias `compare_create`.

После документационных изменений выполнен `git diff --check`.

## Production-проверка

Сеньор выполнил `make prod-build`. Backend, frontend, `celery-worker`,
`celery-worker-gpu` и `celery-worker-lora` перезапущены и имеют состояние
`healthy`; `curl -k --fail https://localhost/health` вернул `{"status":"ok"}`.
SHA-256 `agent_loop.py`, `tool_result.py` и `tool_transport.py` совпал между
checkout, backend и `celery-worker`. Миграций схемы данных нет.
