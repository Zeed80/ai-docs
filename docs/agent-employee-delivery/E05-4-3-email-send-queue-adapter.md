# E05.4.3 — queue-acceptance adapter для email.send

Дата: 21 сентября 2026. Статус: **REVIEWED / DEPLOYED**.

## Срез

Адаптер выбирает только точную пару `POST /api/agent/cap/email` и
`action=send`, разрешающуюся в catalog operation `email.send`. Прямой route и
прочие email actions сохраняют прежнее поведение.

Recipient receipt допустим только при `status=queued`, непустых `task_id` и
`draft_id`, совпадающем с identity запроса. Результат — ToolResult v1
`partial`/`job_queued`, `retryable=false`, полный raw payload, checkpoint и
evidence с `smtp_delivery=not_confirmed`. Это подтверждает только принятие
Celery-задачи, но не доставку письма SMTP.

Malformed 2xx и 4xx дают `failed`; ошибка до dispatch — `failed`; 3xx/5xx,
timeout, transport или exception после dispatch — `outcome_unknown`. Всегда
ровно одна попытка. Корректный versioned nonterminal сохраняется, а versioned
`succeeded` с queued work отклоняется. Public email API, RBAC, approval/content
digest и SMTP worker retries не менялись.

## Проверка

Исполнитель: 255 focused tests. Независимый расширенный набор result,
transport, MCP, router, policy и catalog consistency: 333 passed; сохранено
известное предупреждение `asyncio_loop_scope`. Ruff check, format check и
`git diff --check` прошли.

Production-стек пересобран и перезапущен через `make prod-build`.
`https://localhost/health` вернул `{"status":"ok"}`; backend и Celery beat,
обычный, GPU- и LoRA-workers запущены, health-enabled сервисы healthy. SHA-256
`agent_loop.py`, `tool_result.py` и `tool_transport.py` на host совпали с
файлами backend-контейнера.

E05.4 остаётся **IN PROGRESS**: перед scoped closure нужен отдельный safety-срез,
запрещающий generic read retry для всех `computer_use.*`, затем повторная
оценка read-only `drawing_analysis_mcp` и явная фиксация остатка.
