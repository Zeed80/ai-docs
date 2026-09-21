# E05.4.1 — ToolResult adapter для tool_search_mcp

Дата: 21 сентября 2026. Статус: **REVIEWED / DEPLOYED**.

## Срез

E05.4.1 охватывает только точный `POST /api/agent/cap/mcp` с
`action=tool_search_mcp`. External MCP servers, `drawing_analysis_mcp`,
computer-use/browser, email и RFQ не получают этот контракт и сохраняют
legacy/fail-closed поведение.

Успех подтверждается только mapping с `results: list`, `total: int >= 0`
(`bool` запрещён) и `query: str`, без domain-error markers. ToolResult v1 имеет
`status=succeeded`, `retryable=false`, сохраняет полный raw payload в `data` и
evidence `mcp_builtin_tool_search_v1`, exact action/gateway и confirmed outcome.
Versioned success повторно валидируется; корректный versioned nonterminal
сохраняется без double wrapping.

Полученный malformed 2xx — `failed/invalid_mcp_builtin_tool_search_contract`.
Gateway 4xx — `failed`; ошибка до HTTP dispatch — `failed/mcp_dispatch_failed`;
3xx/5xx, timeout, transport или exception после начала dispatch —
`outcome_unknown`. Выполняется ровно одна попытка без retry. Wildcard approval,
RBAC, audit и digest `{action, arguments}` из E05.4.0 не менялись.

## Проверка

Независимо прошли 290 focused и 317 расширенных MCP/router/contract тестов;
известное предупреждение `asyncio_loop_scope` сохранено. Ruff check, format
check и `git diff --check` прошли.

Production-стек пересобран и перезапущен командой `make prod-build`.
`https://localhost/health` вернул `{"status":"ok"}`; backend и Celery beat,
обычный, GPU- и LoRA-workers запущены, health-enabled сервисы healthy. SHA-256
`agent_loop.py`, `tool_result.py` и `tool_transport.py` на host совпали с
файлами в `infra-backend-1`.

E05.4 и E05 остаются **IN PROGRESS**. Следующий шаг — отдельный аудит остатка
E05.4; этот срез не разрешает массовую нормализацию произвольных MCP payloads.
