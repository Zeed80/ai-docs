# E21.2b5 — retirement bare headless и AgentTask intake

Дата: 4–5 октября 2026. Исходный commit: `f34d5561`.
Исполнитель: `gpt-5.6-sol`, high; root — независимая приёмка и выпуск.
Статус: TESTED / REVIEWED / DEPLOYED; production-проверка приведена ниже.

## Контракт ограниченного среза

Свежий одобренный human-owned AgentTask входит через `submit_agent_intake`:
один owner-bound DurableChatRun/WorkOrder/ledger/plan, уникальная task-связь и
состояние AgentTask фиксируются атомарно. Повтор POST не запускает второй executor.
Historic/foreign/corrupt binding не мигрируется автоматически и не получает
выдуманный нулевой baseline. Изменённое после intake поручение не подменяет
предыдущее исполнение под тем же task ID.

Non-durable WorkStep `agent_turn` блокируется до model/tool dispatch без retry,
replan или fallback. Старый call-marker имеет приоритет над новым guard: нельзя
стереть evidence/unknown outcome и объявить прошлый эффект обычной ошибкой.
Неперенесённые API/planner/decompose/email agent_turn намеренно становятся
недоступными; это safety retirement, не полная миграция этих входов.
Долговечные HTTP/Telegram/cron каналы остаются на прежнем accepted runtime.
Waiting approval/blocked/unknown не объявляются completed и не разрешают replay.

## Caller inventory и дальнейшие контракты

| Caller | Граница | Scope |
|---|---|---|
| HTTP/Telegram/cron common intake | durable worker → AgentOrchestrator → AgentSession | Сохраняется; AgentSession provider/nested HTTP hooks уже приняты |
| Fresh legacy AgentTask run | API → common intake → тот же durable runtime | Миграция этого среза |
| API/model plan/decompose/email agent_turn | generic executor → bare headless | Блокируется; нужны отдельные entry/lifecycle adapters |
| Generic capability WorkStep | physical HTTP + recipient evidence | Ранее принят E21.2b2 |
| Detached planner/verifier | direct Ollama + snapshot/fence/postflight | Ранее приняты E21.2b4/E21.2b3 |
| Durable Orchestrator decision/audit/refinement; conditional capability draft | AIRouter physical provider attempts | Остаётся следующий E21 контракт; не считать ledger-bound по наличию AgentSession hooks |
| Direct email compose | bare AgentSession/context/single-shot | Не мигрируется здесь |
| Telegram voice preprocessing | AIRouter до intake | Не входит в ledger этого среза |

Основные symbols: `tasks/work_orders.py::execute_claimed_step/_execute_step_kind`,
`api/agent_control_plane.py::run_agent_task`,
`domain/work_email_ingress.py::create_email_work_order`,
`tasks/email_triage.py::rule_create_work_order`,
`ai/orchestrator.py::_decide_turn/_run_semantic_audit/_maybe_refine_answer`,
`domain/email_compose.py::_run_agent_email_turn`,
`integrations/telegram_bot.py::_transcribe`.
Инвентаризация основана на call sites, не live tracing; условные draft paths не
означают разрешённую активацию generated skills. CAD/document-processing вне
employee scope здесь не перерабатывается.

## Требования независимого review

- Task row lock, атомарный intake/link/state и проверенный owner на повторе.
- Целостность ledger, run/task identity и frozen objective; historic fail-closed.
- Один executor при duplicate/concurrent POST; no reset/replacement order.
- Headless guard сохраняет существующий call-marker и unknown output.
- Ноль provider/tool attempts и отсутствие retry/replan при новом blocked шаге.
- Durable approval остаётся nonterminal и видим через run/order metadata.
- Профильные тесты на отдельной testcontainers DB, не production.
- Принятый frozen diff → Ruff format/check → production build/health → commit.

## Независимая приёмка

Root проверил frozen runtime diff и выполнил совместный профиль: headless,
control-plane, cron, work_orders/decompose, durable_chat, channel parity,
provider budget и generic WorkOrder budget — **160 passed**, 2 warnings
(pytest config и Qdrant compatibility), без живой LLM/внешних получателей.
Отдельный root-прогон execution boundary, delegations, budget ledger и chat
checkpoints — **96 passed**, 1 pytest-config warning. Итого **256 passed**.
Ruff check/format и `git diff --check` прошли.

Первый cron-прогон исполнителя выявил FK-ошибку старого fixture cleanup.
Исправлен порядок reservations → ledger unbind/delete → orders и snapshot/delta
по ID изолированного тестового мира. Regression сохраняет предсуществующий
чужой durable graph; общий прогон подтвердил отсутствие загрязнения профилей.
Это не универсальный cleanup для общей production БД.

## Production-проверка 5 октября

`make prod-build` завершился успешно; backend и Celery workers пересозданы.
Compose: backend/frontend/worker healthy; HTTPS `/health` → `{"status":"ok"}`.
SHA256 трёх runtime-файлов совпадает на хосте, backend и celery-worker:

- `agent_control_plane.py`: `5227922a49d107f7f2f7464226f6ca1fdaed2c996aa7836056389488ba87309e`
- `work_orders.py`: `8d5a5a9f961a08ceab95b583e832d44a4120df4b5a61febfb393046acbc53fbd`
- `agent_cron.py`: `e497fd9d54682c820d8aae0e8f85f99f8528c75680294d03ef79528da5027808`

Новых миграций и изменений реальных данных не выполнялось. Предупреждение
Compose о ранее существующем volume technical-vectorizer не помешало выпуску;
volume и чужие Docker-объекты не удалялись. Health/hash не заменяют live LLM eval.

Full E21, AIRouter accounting, token/cost bounds, legacy reconciliation,
active-time/replan остаются незавершёнными. Модельная экономия не измерена.
