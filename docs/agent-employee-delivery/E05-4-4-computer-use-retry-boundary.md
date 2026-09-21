# E05.4.4 — retry boundary для computer_use

Дата: 21 сентября 2026. Статус: **REVIEWED / DEPLOYED**.

## Исправление

Все 11 операций класса `browser-script-mcp` исключены из generic read retry на
точной границе `POST /api/agent/cap/computer_use`: `browser_fetch`,
`web_discover`, семь `desktop_*`, `file_read`, `file_write` и `shell`.

Даже внешне read-like вызов может до recipient action израсходовать grant и
записать audit/budget. Поэтому при 5xx или transport ambiguity выполняется
ровно один POST и сохраняется прежний legacy `outcome_unknown`; повторное
внешнее действие не запускается. Direct aliases и неизвестные actions не
получают новый контракт. Остальные catalog reads сохраняют существующий retry.

Срез не добавляет ToolResult adapter и не меняет RBAC, grants, approval, audit,
budget или recipient handlers.

## Проверка

Исполнитель: 214 focused tests. Независимый transport/catalog/router/policy
набор: 251 passed; сохранено известное предупреждение `asyncio_loop_scope`.
Все 11 actions параметризованы для 5xx и transport failure; проверен один POST,
отсутствие sleep и сохранение retry у несвязанного read. Ruff check, format
check и `git diff --check` прошли.

Production-стек пересобран и перезапущен через `make prod-build`.
`https://localhost/health` вернул `{"status":"ok"}`; backend и Celery beat,
обычный, GPU- и LoRA-workers запущены, health-enabled сервисы healthy. SHA-256
`tool_transport.py` на host совпал с backend-контейнером.

E05.4 остаётся **IN PROGRESS**. Следующий шаг — повторный узкий аудит
`drawing_analysis_mcp(reanalyze=false)` и закрытие остатка dynamic MCP,
reanalyze/write и computer-use без ложной нормализации.
