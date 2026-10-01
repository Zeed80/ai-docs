# E20 — вывод WS lifecycle из эксплуатации

Карточка / статус / дата / base commit / commit результата:
E20 / REVIEWED / DEPLOYED / 2026-10-01 / `142ffd3b` / commit результата фиксируется в Git вместе с этим отчётом.

Исполнитель / цикл review / учёт:
`gpt-5.6-sol`, продолжение WIP после остановки предыдущего исполнителя; второй цикл реализации по замечаниям сеньора. Экономия не измерена.

## Изменено (пути и контракт)

- `backend/app/api/agent.py`, `backend/app/main.py`: `/ws/chat` больше не принимает соединение, сообщение или token. При поддержке ASGI denial extension handshake получает HTTP 410 с replacement; иначе до `accept` выполняется policy close 1008. Короткая fallback reason укладывается в лимит RFC 6455; модель, история и WorkOrder этим handler не создаются и не изменяются.
- `backend/tests/test_agent_ws_retirement.py`: проверены 410 и отсутствие конструктора модели, denial в полном приложении, fallback без `accept` и размер close reason.
- `frontend/components/chat/assistant-panel.tsx`, `frontend/lib/durable-chat.ts`: основной UI использует durable HTTP intake/poll/reconnect/cancel. Удалены команды старого `approve/reject` и авто-подтверждение: старый `approval_request` отображается read-only, а решение durable-задачи отправляется только по server-issued checkpoint через `/resume`.
- `frontend/lib/agent-ws.ts`, `scripts/check_agent_ws_adapter.js`: удалены неиспользуемые WS adapter и его smoke script; из `Makefile` удалена цель `agent-ws-smoke`.
- `backend/tests/test_agent_live_realistic.py`: live client переведён на durable HTTP и требует `LIVE_HUMAN_BEARER_TOKEN`. Service key не используется для human-only intake. Approval regression требует терминальный `blocked` и owner-resumable checkpoint; это не считается ошибкой выполнения.
- `aiagent/config/gateway.yml`: удалён нечитавшийся исторический `channels.websocket` block, а не оставлен ложным feature flag.
- `DEVPLAN.md`, `AGENT_EMPLOYEE_IMPLEMENTATION_PLAN.md`, `docs/todo.md`, `docs/refact-implementation-status.md`, `docs/agent-scenarios-runbook.md`, `infra/traefik/prod/routes.yml.template`: действующий transport описан как durable HTTP. Runbook отражает, что document ingest ставит классификацию только при `auto_process=true`, а результат читается через document/processing-job API, не обещанное chat event.

## Инвентарь потребителей и остаточных путей

- Поддерживаемые интерактивные клиенты карточки: AssistantPanel и live regression используют `/api/agent/chat-runs`; Telegram и новый cron intake были переведены в E15/E17 ранее. Новый UI не открывает `/ws/chat`.
- Compatibility boundary `/ws/chat` сохранён только ради явного отказа старым клиентам. Другие WebSocket endpoints (`notifications`, rooms, ComfyUI proxy, Authentik) не являются старым agent-chat lifecycle и не менялись.
- `AgentSession` остаётся внутренним executor в `backend/app/ai/orchestrator.py`, который durable worker запускает внутри сохранённого WorkOrder. Это не connection-owned WS lifecycle.
- Два отдельных остатка намеренно не мигрированы широко в E20: прямой headless email compose (`backend/app/domain/email_compose.py`) и compatibility `agent_turn` для старых WorkOrder (`backend/app/tasks/agent_cron.py`, вызывается из `backend/app/tasks/work_orders.py`). Поэтому E20 доказывает единый поддерживаемый lifecycle интерактивного чата и переключённых каналов, но **не** единственный `AgentSession` runtime всей системы.
- Прямые `AgentSession` в `backend/tests/` — unit-level проверки executor, не production consumers.

## Проверки

Все команды запущены из `/home/project/document-invoices-ai_codex`, кроме явно указанного `frontend/`.

- `python3 -m pytest backend/tests/test_agent_ws_retirement.py -q` — 3 passed, 0 failed, 0 skipped; одно известное предупреждение `asyncio_loop_scope`.
- `python3 -m pytest backend/tests/test_durable_chat.py backend/tests/test_work_order_checkpoint.py backend/tests/test_chat_checkpoints.py backend/tests/test_agent_ws_retirement.py -q` — 115 passed, 0 failed, 0 skipped; то же предупреждение.
- `python3 -m pytest backend/tests/test_agent_live_realistic.py -q` — 0 passed, 0 failed, 16 skipped: `LIVE_STACK!=1`; это проверка collection/syntax, не live-доказательство модели.
- `python3 -m ruff check backend/app/api/agent.py backend/tests/test_agent_ws_retirement.py backend/tests/test_agent_live_realistic.py` — passed.
- `python3 scripts/check_aiagent_contract.py` — passed: 259 registry tools, 243 exposed, 31 approval gates, 23 capabilities, 323 actions; существующие warnings перечислены выводом команды.
- `npm test -- --run` в `frontend/` — 13 files, 103 passed, 0 failed.
- `npm run typecheck` в `frontend/` — passed.
- `PLAYWRIGHT_MOCK_API=1 npx playwright test tests/e2e/durable-chat.spec.ts --project=chromium` в `frontend/` — первоначальный sandbox run не поднял local server (`listen EPERM`); повтор с разрешённым локальным listener — 3 passed, 0 failed.
- `git diff --check` — passed.

## Конкурентность / безопасность

- Full-app test доказывает, что реально смонтированный `/ws/chat` отвечает 410 до `accept`, а source-level patch исторического `AgentOrchestrator` не вызывается.
- Unit/UI/E2E тесты доказывают восстановление persisted run после reload без второго intake POST, WorkOrder cancel вместо закрытия transport, read-only старого approval и resume только по точному checkpoint.
- Live approval тест больше не принимает текст «подтвердите» за gate: ожидаются `blocked` и доступный владельцу checkpoint. Сам live run не выполнялся без человеческого bearer token.

## Production / live evidence

Главный агент независимо прочитал diff и выполнил:

- `python3 -m pytest backend/tests/test_agent_ws_retirement.py backend/tests/test_durable_chat.py backend/tests/test_chat_archive_import.py backend/tests/test_chat_sessions.py -q --tb=short` — 46 passed.
- `npm run typecheck` и `npx vitest run tests/unit/durable-chat.test.ts tests/unit/assistant-panel-durable.test.tsx` в `frontend/` — passed, 21 tests passed.
- `make prod-build` — exit 0, стек пересобран и перезапущен 2026-10-01.
- `/health` — `{"status":"ok"}`; backend/frontend/celery-worker healthy, celery-beat up.
- Production `alembic current` — `20260930_0001 (head)`; новых миграций в E20 нет.
- Реальный WebSocket Upgrade через `https://localhost/ws/chat` — HTTP 410, `code=agent_ws_retired`, `Deprecation: true`, replacement `/api/agent/chat-runs`. Сообщение и деловые действия не отправлялись.

Сеньор 2026-10-01 проверил текущий backend container после E19 recreation:
`docker compose ... logs --since 24h --no-color backend | rg 'ws_chat_connected' | wc -l` → `0`. Это доказывает отсутствие таких событий только в доступных логах текущего контейнера. Логи до recreation отсутствуют, поэтому отсутствие неизвестных внешних WS-клиентов этим не доказано.

## Известные ограничения / rollback

- Старые клиенты получают отказ и должны перейти на durable HTTP; автоматического protocol bridge нет.
- Fallback ASGI без denial extension видит close 1008 вместо JSON 410, но также до `accept` и без исполнения.
- Live durable regression требует отдельного bearer token реального тестового человека. Service-account bypass намеренно отсутствует.
- Для rollback к прежнему WS недостаточно вернуть frontend adapter: удалён сам connection-owned handler. Такой rollback восстановил бы второй lifecycle и противоречил E20; безопасный rollback — вернуть предыдущий deployment целиком после отдельного решения.
- Проверки реального LLM/деловых действий не подменяются успешным health/handshake: live suite не запускался с `LIVE_STACK=1`.

Замечания сеньора закрыты: исправлена длина fallback reason; Traefik comment; ingest documentation; legacy approve/reject UI; human-only live auth и blocked checkpoint expectation; остаточные email/headless WorkOrder consumers перечислены без ложного заявления о единственном runtime всей системы.
