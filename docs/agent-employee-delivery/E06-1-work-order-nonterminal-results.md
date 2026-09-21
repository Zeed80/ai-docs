# E06.1 — nonterminal ToolResult в WorkOrder

Дата: 21 сентября 2026. Статус: **REVIEWED / DEPLOYED**.

## Изменение

WorkOrder consumer различает валидный ToolResult v1. `partial` и
`outcome_unknown` больше не попадают в legacy `PartialProgressError` и не
создают повторную попытку.

Полный envelope, checkpoint и evidence сохраняются в attempt, tool call,
step output, blocker и event. Attempt/tool call сохраняют semantic status;
существующие FSM-состояния step `failed` и order `blocked` удерживают заказ вне
claim, verifier, dependent unlock и replan paths. Выполняется один capability
call.

Versioned `succeeded` сохраняет прежний success path. Versioned `failed`
обрабатывается консервативно и не получает retry только из `retryable=true`.
Raw legacy payloads намеренно сохраняют прежний контракт: E05 scoped closure
оставила недоказанные handlers без ToolResult adapter.

## Проверка

Исполнитель: 45 focused tests. Независимый расширенный WorkOrder-набор: 78
passed; сохранено известное предупреждение `asyncio_loop_scope`. Проверены
отсутствие retry/replan/verifier/dependent execution и сохранность
checkpoint/evidence. Ruff check, format check и `git diff --check` прошли.

Production-стек пересобран и перезапущен через `make prod-build`.
`https://localhost/health` вернул `{"status":"ok"}`; backend и Celery beat,
обычный, GPU- и LoRA-workers запущены. SHA-256 `domain/work_orders.py` и
`tasks/work_orders.py` совпали на host, backend и обычном Celery worker.

E06 остаётся **IN PROGRESS**. E06.2 должна обработать exact
`waiting_approval`; последующий срез — остановка durable/non-durable chat после
всех nonterminal statuses.
