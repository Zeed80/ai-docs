# E06.4 — waiting_approval в checkpointed durable chat

Дата: 21 сентября 2026. Статус: **REVIEWED / DEPLOYED**.

## Изменение

Checkpointed durable chat сохраняет exact raw envelope valid ToolResult v1
`waiting_approval` вместе с checkpoint и journal. Packed checkpoint/journal
binding криптографически верифицируется до settlement: только связанный с ним
ожидающий call может создать Approval.

Для одного источника допускается ровно один pending exact Approval. Повторное
создание, чужой Approval или несовпадающая привязка fail-closed: новое
согласование не создаётся и выполнение не продолжается.

Single и bulk approve/reject обрабатывают только exact pending Approval,
безопасно блокируют source и не запускают replay либо resume durable chat.
Допустимые статусы решения ограничены `approved` и `rejected`; прочие значения
отклоняются. После settlement не выполняются следующий tool/LLM call, retry,
replan, verifier или dependent execution.

## Проверка

Исполнитель: 409 passed. Независимый расширенный набор: 434 passed. Проверены
raw-envelope persistence, cryptographic checkpoint/journal binding, один pending
exact Approval, fail-closed duplicate/foreign cases, single и bulk
approve/reject, запрет replay/resume и ограничение статусов решения.

Остаётся residual DB uniqueness race при конкурентном создании Approval; это
явно передано в E07 как задача уникальности квитанций/связанных записей БД.

Production-стек пересобран и перезапущен через `make prod-build`.
`https://localhost/health` вернул `{"status":"ok"}`; backend и Celery workers
запущены. SHA-256 всех семи изменённых runtime-модулей совпали
на host, backend и обычном Celery worker.

E06 остаётся **IN PROGRESS**. Следующий срез — E06.5: non-checkpointed
sequential и requested-parallel paths.
