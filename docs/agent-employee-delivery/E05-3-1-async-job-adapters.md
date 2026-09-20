# E05.3.1 — адаптеры асинхронных document jobs ToolResult

Дата: 20 сентября 2026. Статус: REVIEWED, production verified.

## Принятый срез

E05.3.1 переводит на агентской HTTP-границе ровно три catalog operations:

- `documents.classify`;
- `documents.extract`;
- `documents.reprocess`.

Адаптер применяется только к точной паре `POST /api/agent/cap/documents` и
исходному `action` каждой операции. Прямые business routes и все иные actions
fail-closed: они не получают семантику E05.3.1. Остальные async operations
сохраняют legacy-контракт до отдельного доказанного среза E05.3.

## Контракт очереди и границы исхода

Допустимый legacy ответ получателя — mapping с непустыми `task_id` и
`document_id`, а также `status: "queued"`. Его принятие нормализуется в
ToolResult v1:

- `status: "partial"`, `error_code: "job_queued"`, `retryable: false`;
- исходный ответ целиком остаётся в `data`;
- `checkpoint` сохраняет `task_id`, `document_id` и `status: "queued"`;
- `evidence` сохраняет adapter contract, operation, `task_id` и подтверждение
  принятия получателем.

Тем самым HTTP-приёмка задачи отделена от завершения работы. Корректный
versioned nonterminal ToolResult сохраняется без повторного оборачивания.
Versioned `succeeded` с queued job отклоняется как противоречивый контракт, а
не превращает поставленную в очередь работу в завершённую.

Для этого среза выполняется ровно одна попытка без automatic retry. Явный 4xx
означает `failed`; redirect, 5xx, timeout, transport error или exception после
начала dispatch означают `outcome_unknown`; ошибка до dispatch означает
`failed`. Во всех случаях raw payload, когда он получен, не теряется.

## Независимая проверка

Независимо пройдены 236 focused тестов и 43 boundary/router теста. Сохранено
известное предупреждение pytest о неизвестной настройке `asyncio_loop_scope`.
Покрыты точный capability/action allowlist, fail-closed direct routes и другие
actions, queue acceptance, raw `data`, checkpoint и evidence, отсутствие retry,
4xx, pre-dispatch failure, неоднозначность после dispatch, сохранение
versioned nonterminal и отказ `succeeded` с queued job.

После документационных изменений выполнен `git diff --check`.

Старший агент выполнил `make prod-build`. Backend и Celery-контейнеры
пересозданы; `https://localhost/health` вернул `{"status":"ok"}`, backend и
workers healthy. SHA-256 `agent_loop.py`, `tool_result.py` и
`tool_transport.py` в checkout и production backend-контейнере совпали.

## Состояние следующей работы

E05.3 остаётся IN PROGRESS; E05 остаётся IN PROGRESS. Следующий шаг — отдельный
аудит нового E05.3-среза с выбором операции по доказательствам, без
предположения о конкретной следующей async operation. E05.4 и E06 не затронуты.
