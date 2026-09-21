# E05.4.2 — достижимость built-in MCP recipients

Дата: 21 сентября 2026. Статус: **REVIEWED / DEPLOYED**.

## Исправление

Built-in `tool_search_mcp` и `drawing_analysis_mcp` больше не обращаются к
container-local `localhost` и маршрутам без `/api`. Recipient URL строится от
`BuiltinAgentConfig.backend_url` (`FASTAPI_URL=http://backend:8000` в Compose),
а вложенный запрос получает штатные internal-agent headers: service key и
подписанный acting-user context, если он присутствует.

`tool_search_mcp` вызывает фактический `GET /api/tool-catalog/search` с
параметрами `query` и `page_size`. Drawing snapshot использует защищённые
`/api/drawings/...` routes. `reanalyze=true` не получает ToolResult-success:
ошибка POST теперь останавливает handler до чтения потенциально устаревшего
snapshot. Контракты approval, RBAC, audit, digest и retry не ослаблены.

## Проверка

ASGI regression проходит полный gateway `POST /api/agent/cap/mcp`, настоящий
built-in handler и защищённый recipient с service-key auth. Отдельно проверены
Docker service URL и запрет stale snapshot после неуспешного reanalyze.

Исполнитель: 206 focused tests. Независимый расширенный набор транспорта, MCP,
router и policy: 233 passed; сохранено известное предупреждение
`asyncio_loop_scope`. Ruff check, format check и `git diff --check` прошли.

Production-стек пересобран и перезапущен через `make prod-build`.
`https://localhost/health` вернул `{"status":"ok"}`; backend и Celery beat,
обычный, GPU- и LoRA-workers запущены, health-enabled сервисы healthy. SHA-256
`mcp_client.py` на host совпал с `/app/app/ai/mcp_client.py` в backend.

E05.4 остаётся **IN PROGRESS**. Следующий безопасный кандидат — отдельный
E05.4.3 для queue acceptance `email.send`; фактическую SMTP-доставку нельзя
объявлять завершённой по receipt Celery.
