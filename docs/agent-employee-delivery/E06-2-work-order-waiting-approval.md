# E06.2 — waiting_approval ToolResult в WorkOrder

Дата: 21 сентября 2026. Статус: **REVIEWED / DEPLOYED**.

## Изменение

Valid ToolResult v1 `waiting_approval` использует существующий безопасный
HTTP-423 settlement path. Approval связывается только с фактически отправленными
`capability`, `action` и `arguments`; digest из recipient envelope не считается
полномочием и не используется.

Полный envelope, checkpoint, evidence и error code сохраняются в attempt,
tool call, step output и approval context. Step/order стабильно переходят в
`waiting_approval`; retry, replan, verifier и dependent execution не
запускаются. Несовпадение identity fail-closed и не создаёт Approval.

HTTP 423 остаётся обратно совместимым. E06.1 `partial`/`outcome_unknown` не
изменены.

## Проверка

Исполнитель: 48 focused tests. Независимый расширенный WorkOrder-набор: 81
passed; сохранено известное предупреждение `asyncio_loop_scope`. Проверены один
call, persistence, exact digest, игнорирование recipient digest, несовместимость
approval A/call B и регрессия HTTP 423. Ruff/format/diff checks прошли.

Production-стек пересобран и перезапущен через `make prod-build`.
`https://localhost/health` вернул `{"status":"ok"}`; backend и Celery beat,
обычный, GPU- и LoRA-workers запущены. SHA-256 `tasks/work_orders.py` совпал на
host, backend и обычном Celery worker.

E06 остаётся **IN PROGRESS**. Следующий срез — durable и non-durable chat:
никакой следующий tool/LLM call после nonterminal result.
