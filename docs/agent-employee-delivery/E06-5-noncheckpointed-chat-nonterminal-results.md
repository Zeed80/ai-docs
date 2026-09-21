# E06.5 — nonterminal ToolResult в non-checkpointed chat

Дата: 21 сентября 2026. Статус: **REVIEWED / DEPLOYED**.

## Изменение

Non-checkpointed AgentSession в sequential пути сохраняет exact raw envelope
valid ToolResult v1 `partial`, `waiting_approval` или `outcome_unknown` в tool
history/output и останавливает текущий turn сразу после первого такого
результата. Tool tail, следующий LLM turn и replan не запускаются.

Requested-parallel путь намеренно serial-degraded с event: эффекты не собираются
eagerly, поэтому tail не может стартовать до распознавания первого результата.
Raw legacy dict с совпадающим `status` не получает новый control contract и
сохраняет прежнюю семантику. Валидные v1 `succeeded` и `failed` также продолжают
обычное исполнение.

## Проверка

Независимый релевантный набор: 135 passed. Двухвызовные sequential и
requested-parallel тесты покрывают все три nonterminal status, exact compact
v1 envelope, отсутствие tool tail/следующего LLM turn, serial-degrade event,
raw legacy compatibility и обычные v1 terminal statuses.

Production-стек пересобран и перезапущен через `make prod-build`.
`https://localhost/health` вернул `{"status":"ok"}`; backend и Celery workers
запущены. SHA-256 `agent_loop.py` совпал на host, backend и обычном
Celery worker.

## Closure E06

WorkOrder consumer закрывает `partial`/`outcome_unknown` (E06.1) и exact
`waiting_approval` (E06.2); checkpointed durable chat закрывает те же
nonterminal transitions (E06.3–E06.4); этот срез закрывает remaining
non-checkpointed sequential/requested-parallel paths. Каждый status имеет
проверенный consumer transition без потери checkpoint/evidence там, где они
существуют.

E06 — **SCOPED COMPLETE / REVIEWED**. Residual DB uniqueness race при
конкурентной записи Approval остаётся задачей E07; отдельная E06.6 не требуется.
