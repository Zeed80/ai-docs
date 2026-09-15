# E04 — контракт ToolResult v1

Дата: 15 сентября 2026. Исходный commit: `f0dd5076`.
Статус: REVIEWED и DEPLOYED.

## Реализация и приёмка

Исполнитель — `gpt-5.6-sol`, high; сеньор — главный агент. Изменены
`backend/app/ai/tool_result.py` и `backend/tests/test_tool_result_contract.py`.
Два цикла замечаний: сохранение неизвестного исхода и запрета повтора;
отказ от общего legacy success-normalizer; строгая версия; повторная валидация
model_copy и вложенных изменений; JSON-сериализация ошибок; вложенные статусы
незавершённого результата и malformed status. Закрыты регрессионными тестами.
Экономия лимитов не измерена.

ToolResult содержит version=1, status, data, error_code, retryable, evidence и
checkpoint. Классификация различает успешный вызов, принятую работу и повтор.
Приёмка задаётся отдельным boolean; сам successful вызов не завершает работу.
Повтор допустим только для failed с явно установленным retryable.
partial, waiting_approval и outcome_unknown сохраняются как отдельные статусы.

Неверная версия, противоречивый success и неизвестный legacy-ответ дают явный
неуспешный результат. Единственный legacy normalizer относится к конкретному
`tool_transport.unknown_outcome`; он всегда сохраняет запрет повтора.
Legacy `result_failed` остаётся отдельным compatibility predicate, включая
прежнее поведение non-dict=False. Перевод адаптеров и consumers — E05/E06.

## Независимые проверки

```bash
python3 -m pytest backend/tests/test_tool_result_contract.py backend/tests/test_agent_execution_boundary.py backend/tests/test_tool_transport.py backend/tests/test_work_order_checkpoint.py -q
```

110 passed. Первоначально Docker был недоступен в sandbox; после разрешённого
доступа к отдельной тестовой PostgreSQL весь набор прошёл. Существующее
предупреждение `asyncio_loop_scope` сохранено. Diff проверен; миграций нет.

## Production

`make prod-build` завершён с кодом 0. `/health` → `{"status":"ok"}`.
Хеш модуля tool_result совпал в checkout, backend и celery-worker:
`6acce50da78cc4a14a69ad44356f08c9051e35798b524fefee412d5ae3b1cfa0`.
Backend, frontend и основные workers healthy. Push накопленной ветки остаётся
заблокированным прежней автопроверкой; обход не выполнялся.

## Ограничения

Новый контракт ещё не подключён ко всем адаптерам. Legacy boolean не доказывает
завершение работы и не заменяет ToolResult; E05/E06 обязательны. Внешние эффекты
и живые LLM при приёмке не вызывались. Следующий этап — E05.1.
