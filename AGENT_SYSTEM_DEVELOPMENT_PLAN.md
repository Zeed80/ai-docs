# План развития автономной агентской системы

Дата актуализации: 2026-08-17. Цель: проверяемый автономный сотрудник, способный
принимать долгосрочные поручения из разных каналов, безопасно планировать,
исполнять, проверять результат, учиться и передавать человеку только решения,
которые действительно требуют полномочий или суждения.

Обозначения: `[x]` выполнено и проверено; `[~]` реализовано частично; `[ ]` не выполнено.

## 1. Контракт поручения

- [x] `WorkOrder`: владелец, цель, источник, приоритет, риск, ограничения и metadata.
- [x] Критерии приёмки, artifacts, evidence, parent/child и отмена.
- [~] Дедлайны и budgets хранятся, но ещё не полностью исполняются как policy.
- [ ] Формальные SLA и типовые контракты завершения по классам поручений.

## 2. Durable runtime

- [x] `WorkOrder → WorkPlan → WorkStep → WorkStepAttempt` в PostgreSQL.
- [x] FSM, DAG, checkpoints, lease/heartbeat, retry и recovery.
- [x] Bounded replanning и fail-closed completion.
- [ ] Межузловой failover и disaster recovery активных задач.

## 3. Планирование

- [x] Capability-grounded planner, manifest validation и fallback.
- [x] Типизированный dataflow `${steps.<key>.output.<path>}` с provenance.
- [~] Risk-aware replanning реализован на базовом уровне.
- [ ] Cost/latency-aware альтернативные планы, симуляция, ветвления и циклы.

## 4. Исполнение инструментов

- [x] Единый capability gateway и write-ahead `WorkToolCall`.
- [x] Точные аргументы, digest, result/error и downstream idempotency key.
- [~] Не все внешние endpoints гарантируют идемпотентность.
- [ ] Transactional outbox, compensation, rollback и versioned contracts.

## 5. Верификация

- [x] Независимый verifier, критерии, evidence и fail-closed verdict.
- [x] Celery `SUCCESS` не считается доказательством бизнес-результата.
- [ ] Ансамбль независимых verifier, adversarial и domain-specific проверки.

## 6. Approval gates

- [x] Approval связан с WorkOrder, step и digest точного действия.
- [x] Pause/resume, expiry и audit.
- [ ] Многосторонние согласования, delegation, separation of duties и emergency stop.

## 7. Computer use

- [x] Короткоживущие grants с allowlist hosts/roots/commands/actions.
- [x] Browser start/click/type/read/screenshot/close, file и argv-only shell.
- [ ] Универсальный visual desktop, downloads, dialogs, iframe, VM и session recovery.

## 8. Безопасность

- [x] Owner scope, auth, fail-closed tools и least-privilege grants.
- [ ] Sandbox/VM на WorkOrder, scoped credentials, DLP, prompt-injection defense.
- [ ] Malware scan, egress proxy, tenant keys, secret scan и security red-team.

## 9. Память и обучение

- [x] Episodic chat memory, pinned facts и recipe lifecycle.
- [~] Durable WorkOrder learning: transactional outbox, verified memory и recipe link.
- [ ] Актуальность, противоречия, supersession, retention и owner-aware retrieval.
- [ ] UI управления learned memory и автоматическое извлечение причин ошибок.

## 10. Проактивность

- [x] AgentCron и durable dispatcher через Celery beat.
- [ ] Универсальный event ingress, ожидание событий, SLA escalation и quiet hours.

## 11. Команды агентов

- [~] `AgentTeam` существует как registry; WorkOrder поддерживает дочерние задачи.
- [ ] Team manager, role boundaries, delegation, peer review и supervisor recovery.

## 12. Саморасширение

- [x] Plugin registry, capability proposals и sandbox validation skeleton.
- [ ] Gap detection, generation с тестами, static/security analysis и shadow eval.
- [ ] Versioning, promotion, rollback и автоматическая демоция деградировавших skills.

## 13. Интерфейс оператора

- [x] `/work-orders`, навигация, RU/EN, создание, список и детали.
- [ ] DAG, attempts, tool calls, evidence, realtime, pause/resume/replan и emergency stop.

## 14. Наблюдаемость

- [x] Events, attempts, tool calls, computer-use audit и control-plane status.
- [ ] Метрики/SLO, distributed tracing, dashboards, alerts и tamper-evident audit.

## 15. Бюджеты

- [~] Поля budgets, max attempts, timeout, max replans и computer-use limits.
- [ ] Token/API/compute accounting, hard limits, forecasts и budget approvals.

## 16. Каналы

- [x] API, AgentTask и Cron.
- [~] Chat, email и Telegram интегрированы не полностью с единым WorkOrder ingress.
- [ ] Унифицированные identity, reply-to-source, clarification и corporate messengers.

## 17. Интеграции

- [x] Capability manifest и внутренние бизнес-инструменты.
- [ ] MCP/connectors, credential vault, OAuth lifecycle и health/recovery connectors.

## 18. Тестирование и evals

- [x] Unit/API/E2E, PostgreSQL migrations и production live-smoke первого runtime.
- [ ] Scenario corpus, prompt-injection, chaos, load, soak, cost и red-team evals.

## 19. Production rollout

- [x] Миграции, Compose rebuild, health, live API, Celery и browser smoke.
- [ ] Staging, canary, automatic rollback, backup/restore drill и incident runbook.

## 20. Документация

- [x] `CLAUDE.md`, `PLAN.md`, `DEVPLAN.md` описывают durable runtime.
- [~] Этот файл является единым roadmap и журналом фактического статуса.
- [ ] Диаграммы FSM, operator guide, capability guide, security handbook и SLO runbook.

## Последовательность оставшейся реализации

1. [~] WorkOrder learning, memory provenance, freshness, conflicts и retention.
2. [ ] Capability builder и безопасное саморазвитие.
3. [ ] Унифицированные каналы и проактивные события.
4. [ ] Команды специализированных агентов.
5. [ ] Универсальный visual computer use.
6. [ ] Бюджеты и resource governance.
7. [ ] Enterprise security hardening.
8. [ ] Autonomous evals, chaos, load и red-team.
9. [ ] Staging, canary, rollback и SLO.
10. [ ] Доказательная приёмка на широком наборе реальных поручений.

Каждый пункт переводится в `[x]` только после миграции, автоматических тестов,
production rebuild и проверки соответствующего живого контракта.
