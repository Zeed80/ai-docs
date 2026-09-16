# E05.1 — read-only адаптеры ToolResult

Дата: 16 сентября 2026. Исходный commit: `a69f7549` с последующими независимыми
CAD-коммитами в общей ветке. Статус: REVIEWED и DEPLOYED.

## Делегирование и review

- Основной исполнитель: `gpt-5.6-terra`, high.
- Эскалация после двух циклов замечаний: `gpt-5.6-sol`, high.
- Сеньор проверил фактический diff и повторил тесты. Исполнители не выполняли
  commit, deploy или изменения CAD.
- Первый цикл исключил `job_id` как самостоятельный признак незавершённости.
  Второй ограничил поиск ошибок явными response wrappers вместо произвольных
  элементов списков. Sol исправил оставшуюся семантику: `queued/running` у
  прочитанного объекта является данными; command acceptance относится к E05.3.
- Учёт расхода по моделям недоступен; экономия лимитов не измерена.

## Контракт

- Только HTTP-вызовы, для которых каталог подтверждает `effect=read`, получают
  serialized ToolResult v1 в `execute_skill`.
- Успешный JSON или text сохраняется целиком в `data`. Artifact/job identifiers
  не теряются. Валидный ToolResult не оборачивается второй раз.
- Явные top-level и проверенные `result`/`domain` ошибки, `built=false`, неверный
  v1 envelope и HTTP errors дают failed. Данные коллекций и metadata не
  интерпретируются как response contract.
- Read transport retries ограничены текущим budget; после исчерпания возвращается
  failed с `retryable=false` и evidence о завершённом внутреннем retry budget.
- Write timeout остаётся outcome_unknown. Write и MCP success сохраняют прежний
  контракт до E05.2–E05.4. Оригинальные args используются для catalog, approval
  и dispatch без подмены envelope.

## Проверки

```bash
python3 -m pytest backend/tests/test_tool_transport.py backend/tests/test_tool_result_contract.py backend/tests/test_agent_execution_boundary.py backend/tests/test_work_order_checkpoint.py -q
```

Независимый результат: 134 passed. Ruff и `git diff --check` прошли. Сохранилось
известное предупреждение pytest о `asyncio_loop_scope`. Внешние сервисы, SMTP,
browser actions и живые LLM не вызывались.

## Production

`make prod-build` завершён; backend, frontend и основные workers healthy,
`/health` → `{"status":"ok"}`. Хеши `agent_loop.py`, `tool_result.py` и
`tool_transport.py` совпали в checkout, backend и celery-worker. Миграций нет.

## Ограничения

Consumers ещё не принимают решения по всем статусам ToolResult — это E06.
DB writes, async jobs и external/MCP adapters остаются E05.2–E05.4. Следующая
карточка — E05.2, простые DB writes.
