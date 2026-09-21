# ADR 005: отдельная таблица квитанций логических действий

**Статус**: Accepted  
**Дата**: 2026-09-21

## Контекст

Пилот `agent_control.task_propose` сохранял квитанцию как JSON-событие
`WorkEvent(chat.recipient_committed)`. Поиск по JSON не обеспечивал простой
DB-level uniqueness для логического действия, а конкурентная доставка могла
создать два эффекта до обнаружения дубликата. Для следующих recipients нужны
явные owner/order/action/fence/artifact bindings и версионируемый формат.

`ChatLogicalAction.id` является стабильным `logical_action_id`. Поле
`ChatLogicalAction.attempt_id` — изменяемый fencing attempt, поэтому его нельзя
использовать как идентичность эффекта.

## Решение

Добавить `action_receipts` с уникальным `logical_action_id`. Строка хранит:

- owner, work order, logical action и attempt, который реально зафиксировал эффект;
- operation, request/response digests и точный response;
- artifact ID и artifact revision (`updated_at` пилотного `AgentTask`);
- receipt version и provenance.

Первое исполнение по-прежнему берет общий row lock `WorkOrder`, проверяет текущие
RBAC, owner, запрос, живой lease/fence и budget, а затем фиксирует доменный эффект
и receipt одной транзакцией. Уникальное ограничение — последний защитный барьер,
но не замена общему lock.

Повторная доставка под тем же owner и текущими RBAC только читает receipt и не
повторяет эффект. Ключ `logical_action_id:attempt_id` не является bearer-token.
После commit принимается либо исходный attempt из receipt, либо текущий attempt
того же logical action; оба варианта возвращают существующую квитанцию. Любой
иной attempt отклоняется. До появления receipt выполнить эффект вправе только
текущий attempt с живым fence. Таким образом новый attempt не создает новое
логическое действие и не разрешает повтор эффекта.

Чтение сначала использует таблицу, затем старое событие. Backfill переносит
только единственную, криптографически и структурно согласованную пилотную
квитанцию, сохраняя event ID/sequence в provenance. Corrupt и duplicate events
остаются в `work_events` без пересчета digest и при чтении завершаются fail-closed.

## Рассмотренная альтернатива

Оставить `WorkEvent` и добавить expression/partial unique index по JSON action ID.
Это сохраняет event-only модель, но переносит обязательную схему в неявный JSON,
усложняет внешние ключи, эволюцию artifact binding, диагностику corrupt данных и
портируемость ORM. Поэтому вариант отклонен.

## Последствия

- Один формат runtime-чтения и DB-level uniqueness на logical action.
- Старые события остаются аудит-следом и временным backward-read источником.
- Квитанция доказывает атомарный DB commit, но не является текущей авторизацией,
  доказательством неизменности артефакта или разрешением replay/resume.
- Массовое подключение остальных recipients остается отдельной работой E08+.
