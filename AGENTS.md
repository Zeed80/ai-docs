# Repository Guidelines

## Project Structure & Module Organization
This repository is currently a planning workspace. The source of truth is in [`CLAUDE.md`](./CLAUDE.md), [`PLAN.md`](./PLAN.md), and [`DEVPLAN.md`](./DEVPLAN.md). Contributors should keep product, architecture, and delivery changes aligned across those files.

The target implementation structure is:
- `backend/app/` for FastAPI code (`api/`, `domain/`, `tasks/`, `ai/`, `db/`)
- `frontend/app/` for Next.js routes and pages
- `frontend/components/` for shared React UI
- `aiagent/` for prompts, skills, and scenarios
- `infra/` for `docker-compose`, Traefik, and deployment scripts

## Build, Test, and Development Commands

### Делегирование разработки

По прямому поручению пользователя выполнять агентский план в режиме
`AGENT_EMPLOYEE_ORCHESTRATION.md`: главный агент — сеньор, ограниченную реализацию
поручать более простой модели через штатных подагентов, затем независимо проверять.
По умолчанию исполнитель `gpt-5.6-luna` с явным model и `fork_turns=none`;
при недоступности модели не наследовать дорогую молча. Только главный агент
делегирует, принимает изменения, выполняет deploy и commit. Подагенты не создают
новых подагентов. Один пишущий исполнитель по умолчанию, максимум два цикла правок
до разбора причины/эскалации. Этот режим не расширяет доступ и не отменяет запрет push.

### Проверки

Актуальный агентский срез: `AGENT_EMPLOYEE_IMPLEMENTATION_PLAN.md`.
Подробный план передачи реализации: `AGENT_EMPLOYEE_EXECUTION_PLAYBOOK.md`.
При поручении продолжить по нему брать одну карточку E00–E52, проверять зависимости,
неизменяемые ограничения и Definition of Done. Не считать существующий WIP проверенным.
Атомарная квитанция получателя (пилот task_propose): `python3 -m pytest backend/tests/test_action_receipts.py -q`.
Read-only сверка AgentTask по квитанции (E00): тот же набор и `backend/tests/test_chat_action_journal.py`; отчёт в `docs/agent-employee-delivery/E00-receipt-verification.md`.
Проверки границ и разрешений: `python3 -m pytest backend/tests/test_agent_execution_boundary.py backend/tests/test_agent_delegations.py -q`.
Пилот долговечного чата: `python3 -m pytest backend/tests/test_durable_chat.py backend/tests/test_work_order_checkpoint.py -q`.
Снимки исполнения и одноразовое продолжение после подтверждения владельца: `python3 -m pytest backend/tests/test_chat_checkpoints.py -q`.
Журнал логических действий, наблюдения владельца и миграция: `python3 -m pytest backend/tests/test_chat_action_journal.py -q`.
Безопасность транспортных повторов: `python3 -m pytest backend/tests/test_tool_transport.py backend/tests/test_chat_checkpoints.py -q`.
UI долговечного чата: `npm test`; браузерная проверка с подставным API — `PLAYWRIGHT_MOCK_API=1 npx playwright test tests/e2e/durable-chat.spec.ts --project=chromium` в `frontend/`.
UI журнала и наблюдений: `npx vitest run tests/unit/action-journal.test.tsx`; `PLAYWRIGHT_MOCK_API=1 npx playwright test tests/e2e/chat-action-journal.spec.ts --project=chromium` в `frontend/`.
Проверка frontend: `npm run typecheck` в `frontend/`. Перед выдачей изменений:
`make prod-build`, затем `curl -k https://localhost/health` из корня проекта.
Planned local workflow:
- `make dev` starts the full stack
- `make test` runs unit, API, and integration tests
- `make e2e` runs Playwright end-to-end coverage
- `make regression` checks extraction quality
- `make agent-test` validates AiAgent scenarios against mock skills

If you add real code, keep command examples in this file and the planning docs synchronized.

## Coding Style & Naming Conventions
Write documentation and contributor discussion in Russian. Keep code, identifiers, and code comments in English. Use clear module boundaries: AiAgent for planning/orchestration, FastAPI for data and async work, Next.js for UI.

Prefer:
- `snake_case` for Python modules and functions
- `PascalCase` for React components and Pydantic models
- feature-focused filenames such as `style_matching.py` or `supplier_profile.tsx`

Add formatters and linters with the implementation. `DEVPLAN.md` already expects pre-commit hooks.

## Testing Guidelines
The planned stack is `pytest` for backend logic and API tests, plus Playwright for keyboard-first UI flows. Place backend tests under `backend/tests/` and frontend E2E specs under `frontend/tests/e2e/`.

Name tests after the behavior under test, for example `test_invoice_duplicate_detection.py` or `inbox-review-streak.spec.ts`.

## Commit & Pull Request Guidelines
No local `.git` history is present in this workspace, so commit conventions cannot be inferred from prior commits. Until history exists, use short imperative subjects such as `Add invoice anomaly schema` or `Document AiAgent skill registry flow`.

Pull requests should include:
- a concise problem/solution summary
- links to the relevant plan section or issue
- screenshots or terminal output for UI, API, or workflow changes
- notes on new commands, env vars, or approval gates

## Security & Configuration Tips
Follow the documented rule: confidential OCR and extraction stay local. Do not commit secrets, production credentials, or real customer documents. Treat approval gates and auditability as non-optional design constraints.
