# E05.2.1 — первый срез простых DB write-адаптеров ToolResult

Дата: 16 сентября 2026. Исходный commit: `ea5726a1`.
Статус: REVIEWED, DEPLOYED. Commit результата создаётся после этой финальной
проверки отчёта.

## Делегирование и приёмка

- Техническая реализация была принята сеньором до передачи документации.
  Исполнитель этого документа не менял runtime, тесты, RBAC, approval, lease,
  budget, commit, push или production stack.
- Идентификатор модели технического исполнителя и число его циклов review в
  передаче не зафиксированы; экономия лимитов не измерена. Для документационной
  части замечаний сеньора не было.

## Изменено и контракт

- На агентской HTTP-границе ToolResult v1 применяется ровно к
  `analytics.collection_create`, `analytics.calendar_create_reminder`,
  `analytics.table_create_view` и `warehouse.create_item`.
- Ровно для этих route/action-пар успешный 2xx domain response сериализуется как
  `succeeded`, сохраняя исходный payload в `data`; корректный v1 envelope не
  оборачивается повторно.
- Явная domain-ошибка в 2xx response и 4xx response дают `failed`.
  Неterminal `partial`, `waiting_approval` и `outcome_unknown` сохраняют свой
  статус, а не становятся успехом.
- Если dispatch уже мог произойти, 5xx, timeout, разрыв протокола или исключение
  дают `outcome_unknown`; автоматического retry нет. Ошибка до dispatch даёт
  `failed` с `dispatch_attempted=false`; автоматического retry также нет.
- Проверки каталога, approval и отправка получают исходные аргументы, а не новый
  envelope. Публичные business API и схема БД не менялись.

## Не изменено и почему

- E05.2 не завершена: остальные операции E03 с классом `one-db-commit` не
  мигрированы и сохраняют прежний контракт. Их количество здесь намеренно не
  оценивается без отдельного пересчёта инвентаря.
- E05.3 (async jobs) и E05.4 (external/MCP handlers) не начинались. Их ответы,
  включая acceptance job, не объявляются завершённым эффектом этим срезом.
- Consumers остаются задачей E06; этот срез не даёт им права считать каждый
  ToolResult завершением пользовательской работы.

## Проверки

В корне репозитория повторён принятый целевой набор:

```bash
python3 -m pytest backend/tests/test_tool_transport.py backend/tests/test_tool_result_contract.py backend/tests/test_capability_catalog_consistency.py backend/tests/test_agent_execution_boundary.py backend/tests/test_work_order_checkpoint.py -q
```

Результат: 180 passed, 0 failed, 0 skipped; сохранено одно существующее
предупреждение pytest: неизвестная настройка `asyncio_loop_scope`.

Покрыты точный allowlist четырёх операций, fail-closed для прочих route/action,
2xx domain error, 2xx success с raw IDs/status, double wrapping, 4xx, отказ до
dispatch, неоднозначность после dispatch без повторной отправки, неterminal
статусы и сохранение исходных args. Дополнительно `git diff --check` выполнен
после документационных изменений.

## Конкурентность, безопасность и production

Тесты подтверждают отсутствие автоматического транспортного retry для этого
write-среза; они не добавляют atomic receipt или exactly-once гарантию получателю.
Timeout/5xx после возможного dispatch остаётся `outcome_unknown` и требует
сверки, а не повторной записи.

Сеньор выполнил `make prod-build`; backend, frontend, `celery-worker`,
`celery-worker-gpu` и `celery-worker-lora` перезапущены и имеют состояние
`healthy`. `curl -k --fail https://localhost/health` вернул `{"status":"ok"}`.
SHA-256 файлов `agent_loop.py`, `tool_result.py` и `tool_transport.py` совпал в
checkout, контейнере backend и контейнере `celery-worker`. Миграций нет;
rollback — отдельная проверенная отмена allowlist-адаптеров, не откат данных.

## Review gate и следующий шаг

E05 остаётся IN PROGRESS. E05.2.1 — только первый узкий REVIEWED срез; остальные
`one-db-commit` операции не скрыты и не завершены. Следующая карточка — ещё один
отдельно выбранный срез E05.2 с теми же fail-closed границами, а не E05.3 и не
массовая миграция DB writes.
