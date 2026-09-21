# E06.3 — nonterminal ToolResult в durable chat

Дата: 21 сентября 2026. Статус: **REVIEWED / DEPLOYED**.

## Изменение

Checkpointed AgentSession после записи tool history, checkpoint и
ChatLogicalAction распознаёт valid ToolResult v1 `partial` и
`outcome_unknown`. Typed `ChatNonterminalToolResult` переносит полный envelope
через model recovery и durable-chat worker без оборачивания в `RuntimeError`.

WorkOrder ловит сигнал и использует E06.1 settlement: сохраняет checkpoint и
evidence, блокируется без retry/replan/verifier/dependents, не выполняет хвост
tool calls и не вызывает следующий LLM turn. ChatLogicalAction сохраняет
semantic `partial`/`outcome_unknown`, а не общий `result_recorded`.

Raw legacy maps не приобретают новый durable-control контракт. `succeeded` и
остальные существующие пути не изменены.

## Проверка

Исполнитель: 90 focused tests. Независимый расширенный chat/checkpoint/journal/
WorkOrder-набор: 115 passed; сохранено известное предупреждение
`asyncio_loop_scope`. Ruff/format/diff checks прошли.

Production-стек пересобран и перезапущен через `make prod-build`.
`https://localhost/health` вернул `{"status":"ok"}`; backend и Celery beat,
обычный, GPU- и LoRA-workers запущены. SHA-256 всех пяти runtime-модулей
совпали на host, backend и обычном Celery worker.

E06 остаётся **IN PROGRESS**. Следующий срез — checkpointed chat
`waiting_approval`, затем non-checkpointed sequential/parallel paths.
