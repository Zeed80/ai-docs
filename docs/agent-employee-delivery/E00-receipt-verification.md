# E00 — Read-only сверка квитанции и текущего AgentTask

Дата: 15 сентября 2026. Исходный commit: `143dbdf1`.
Статус: REVIEWED и DEPLOYED; независимые проверки и production-приёмка пройдены.

## Делегирование

- Сеньор: главный агент сессии, независимое чтение контракта/diff/тестов.
- Исполнитель: `gpt-5.6-luna`, reasoning high, короткий контекст без fork истории.
- Задание: только E00 и три файла — `domain/action_receipts.py`, `api/chat_runs.py`,
  `backend/tests/test_action_receipts.py` (первые два с префиксом `backend/app/`).
- Начальная реализация verification уже была незавершённым WIP до делегирования.
  Исполнитель завершает и проверяет её; не приписываем ему весь прежний пилот.
- Production/commit/push не делегированы; автоматическая передача карточки и
  замечаний выполнена сеньором без участия пользователя в этих шагах.
- Данные расхода по классам моделей недоступны в отчёте: экономия не измерена.

## Предварительные замечания сеньора

Первый цикл review:

1. Проверки числа WorkEvent и неизменности AgentTask недостаточно: нужно проверить
   также WorkOrder, ChatLogicalAction и checkpoint WorkStepAttempt вокруг GET.
2. Corrupt response hash не покрывает ошибочный artifact binding с корректным hash;
   нужны отдельные случаи отсутствующего/невалидного ID, ожидаемый HTTP 409.
3. Нужен настоящий ASGI GET с проверкой владельца и пониженной текущей роли.

Второй цикл review:

1. Полный сохранённый ответ с invalid UUID и полный ответ без ID теперь проверены
   раздельно; прежняя fixture падала до проверки UUID из-за других отсутствующих полей.
2. SQL listener вокруг настоящего GET подтверждает отсутствие INSERT/UPDATE/DELETE;
   дополнительно проверены dirty/new/deleted клиентской DB-сессии, так как другое
   соединение не видит незакоммиченные записи.
3. Частичный no_autoflush вокруг чтения AgentTask заменён единой границей endpoint,
   включающей owner/action/receipt/task SELECT. Общий write-path receipt не изменён.

Замечания закрыты исполнителем. Сеньор прочитал окончательный diff и тесты,
проверил caller/owner boundary и выполнил независимый объединённый прогон.

## Контракт и границы гарантии

- GET verification — read-only, owner-bound, текущая роль admin для AgentTask.
- matched/changed/missing/inconclusive — снимок явно перечисленных полей,
  а не гарантия, что объект никогда не менялся.
- Нет квитанции — inconclusive, не доказательство отсутствия эффекта.
- Во всех исходах can_replay=false, can_resume=false.
- Нет автоматического открытия references, запуска tools или записи verdict в БД.
- UI сверки — следующая карточка E01; этой карточкой не реализуется.

## Итоговые проверки

Исполнитель: 23 passed в `test_action_receipts.py`, 11 passed в
`test_chat_action_journal.py`; Ruff и format-check прошли.

Независимый прогон сеньора:

```bash
python3 -m pytest backend/tests/test_action_receipts.py backend/tests/test_chat_action_journal.py backend/tests/test_agent_execution_boundary.py backend/tests/test_agent_delegations.py backend/tests/test_durable_chat.py backend/tests/test_chat_checkpoints.py -q
```

Результат: **110 passed**, отдельный тестовый PostgreSQL; существующее предупреждение
pytest `asyncio_loop_scope`. Проверены штатные pre-commit hooks для изменённых файлов.
Frontend не менялся: отдельный повтор его тестов не выполнялся.

## Выкладка и ограничения

`make prod-build` завершён; backend/frontend/обычный, GPU и LoRA workers healthy.
`curl -k --fail https://localhost/health` → `{"status":"ok"}`.
GET verification без авторизации → 401. Хеши `action_receipts.py` и `chat_runs.py`
совпали в checkout, backend и celery-worker.
Миграция не нужна. Реальные внешние действия и production-задачи для теста не создаются.
Push накопленной истории по-прежнему запрещён; обход не предпринимался.
Следующая карточка: E01, UI текущей сверки. Режим делегирования не является фоновым
демоном и не гарантирует работу после завершения сессии/исчерпания лимитов.
