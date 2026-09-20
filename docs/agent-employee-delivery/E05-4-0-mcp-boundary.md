# E05.4.0 — единая policy/audit-граница MCP

Дата: 20 сентября 2026. Статус: **REVIEWED, production verified**.

## Проблема и исправление

Chat загружал MCP schemas вместе с `_handler` и вызывал callable напрямую,
минуя `/api/agent/cap/mcp`, wildcard approval, RBAC и capability audit.
WorkOrder и HTTP gateway при этом использовали другую policy-границу.

Теперь Chat сохраняет индивидуальные tool schemas, но `skill_map` содержит
только descriptor `POST /api/agent/cap/mcp` с immutable `_mcp_action`.
Execution body всегда имеет форму `{action, arguments}`; те же исходные данные
используются для policy, approval reuse и `X-Agent-Approval-Digest`. Wildcard
gate `*` сохранён, per-tool доверие не добавлено. Legacy MCP/builtin callable
entry fail-closed возвращает ToolResult v1 `direct_mcp_handler_disabled` и не
исполняет handler.

Это prerequisite, а не result adapter E05.4.1: успешный MCP payload пока
остаётся legacy и не объявляется semantic success автоматически.

## Коррекция E03

Аудит исправил две ошибочные строки inventory:

- `procurement.list_requests` — чистое DB-чтение, не external dispatch;
- `procurement.send_rfq` создаёт RFQ drafts и меняет DB status одним commit,
  но не отправляет SMTP; это gated `one-db-commit`, не external dispatch.

## Проверка

Независимый набор MCP/transport/router/policy/catalog: 219 passed, известное
предупреждение `asyncio_loop_scope`. Покрыты denial до handler, точный approval
digest, отказ несовпавшего digest, один approved dispatch, audit перед handler,
отсутствие `_handler` в Chat descriptor и fail-closed legacy callable.
Ruff check, format check и `git diff --check` прошли.

Старший агент выполнил `make prod-build`. Backend и Celery-контейнеры
пересозданы; `https://localhost/health` вернул `{"status":"ok"}`, backend и
workers healthy. SHA-256 `agent_loop.py` в checkout и production
backend-контейнере совпал.

E05 и E05.4 остаются **IN PROGRESS**. Следующая карточка — E05.4.1, узкий
gateway-only adapter built-in `tool_search_mcp`; внешние MCP servers,
`drawing_analysis_mcp`, browser/computer-use, SMTP и RFQ исключены.
