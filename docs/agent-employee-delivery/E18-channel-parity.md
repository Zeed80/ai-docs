# E18 — матрица каналов и паритет контрактов

Дата: 30 сентября 2026. Статус: **REVIEWED / TESTED / DEPLOYED** (intake и terminal result;
recipient E2E не доказан).

`backend/tests/test_agent_channel_parity.py` запускает один входной сценарий
через реальные адаптеры HTTP, Telegram и cron. Проверяются общий durable
owner, source/plan identity, budgets и отсутствие вызова `AgentSession` в
intake-обработчиках. После intake тест сопоставляет форму downstream-контракта:
logical action, receipt, approval, cancellation и initial result state.

Сравнение `initial_result_state=not_set` после intake доказывает только одинаковое
исходное состояние, а не паритет выполненного результата. Поэтому отдельный тест
пропускает все три созданных run через реальный `run_durable_chat` с fake terminal
model и сравнивает сохранённые `WorkOrder.status`, наличие `result_message_id` и
содержимое assistant message. Идентификаторы сообщений ожидаемо разные. На уровне
прямого `run_durable_chat` статус остаётся `running`: завершение WorkOrder принадлежит
внешнему work-step worker, а не функции сохранения terminal ответа.

Тест downstream-контракта намеренно синтетический: он записывает action,
receipt и approval после того, как реальный channel adapter создал durable
run. В текущем runtime нет одного безопасного recipient action, который
можно одинаково выполнить для HTTP, Telegram и cron. Поэтому E18 **не
доказывает** полный путь model -> recipient -> receipt -> approval; это
остаётся отдельной интеграционной задачей. Следовательно, этот тест не является
runtime-parity PASS для receipt/approval и не должен закрывать такой gate.

Общий `submit_agent_intake` теперь отказывает владельцу, для которого есть
локальная запись `User(is_active=False)`, причём строка владельца удерживается
`FOR UPDATE` до конца intake transaction. Блокирующее чтение получает скалярное
значение `User.is_active`, а не ORM-объект: ранее загруженное состояние identity
map не может скрыть отзыв, зафиксированный до этой проверки. Это закрывает
HTTP-обход: Telegram и cron уже проверяли active user самостоятельно. Отдельный
тест отзывает одного owner и подтверждает отсутствие DurableChatRun через все
три адаптера; ещё один тест воспроизводит устаревший ORM owner перед отзывом.
Если локальной записи owner нет, intake полагается на уже проверенный adapter:
это сохранённая совместимость для externally provisioned users и тестовых
dev identities, а не замена аутентификации.

Проверен и отказ Telegram outbox: `dead_letter` уведомления не откатывает
сохранённый response/result status WorkOrder. Тест арендует свой конкретный
outbox row, поэтому не зависит от чужих due rows в общей тестовой БД.

## Проверка исполнителя

- Независимый прогон root после последнего diff:
  `python3 -m pytest backend/tests/test_agent_channel_parity.py backend/tests/test_durable_chat.py backend/tests/test_agent_cron_dispatch.py -q --tb=short`
  — **58 passed**, одно предупреждение pytest о неизвестной настройке
  `asyncio_loop_scope`. Отдельно `test_telegram.py` — **38 passed**,
  `test_agent_outbox.py` — **20 passed**. Запуск в sandbox без Docker-доступа
  ранее остановился на setup errors, не на коде.
- `ruff check backend/app/domain/agent_intake.py backend/tests/test_agent_channel_parity.py`
  — **All checks passed**.
- `ruff format --check backend/tests/test_agent_channel_parity.py` —
  **1 file already formatted**.
- Диагностический объединённый прогон исполнителя: **107 passed, 4 failed**
  на глобальных assertions по числу строк в общей test DB. Он не считается
  зелёным regression gate; отдельные модули выше проверены root.

Проверяется в scoped наборе: реальные HTTP/Telegram/cron adapter intake, общий
owner/budget/work contract, отсутствие модели в intake, одинаковый отказ inactive
owner, сохранение одинакового terminal response реальным `run_durable_chat` и
сохранность terminal domain result при dead-letter Telegram notification.
Синтетически созданы и только структурно сопоставлены logical action, receipt и
approval. Поэтому статус **REVIEWED** может относиться к scoped intake/channel
contract после независимой приёмки root, но не к model -> recipient E2E и не к
runtime-доказательству receipt/approval parity.

Исполнитель: `gpt-5.6-sol`, два цикла замечаний сеньора. Закрыты замечания о race
inactive-owner check, проверке трёх revoked каналов, адресном выборе outbox row и
явной маркировке синтетической части. Экономия не измерена.

Production пересобран через `make prod-build`: backend, frontend и обычный
Celery worker healthy, beat запущен. `/health` вернул `{"status":"ok"}`,
Alembic в backend — `20260929_0005 (head)`. Отдельной миграции E18 нет.
Telegram delivery adapter остаётся выключенным; архивный WS lifecycle не
отключается по результатам E18. Для него и старой проекции `AgentTask.status`
остаются E19/E20 и отдельные проверки.
