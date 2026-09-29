# E11 — продолжение после проверенного commit

Дата: 29 сентября 2026. Статус: **REVIEWED / TESTED / DEPLOYED**.

## Граница реализации

Добавлен отдельный `verified_commit` intent. Он не меняет pre-dispatch
`confirmation_required`: владелец разрешает продолжить после доказанного
результата, но не повторить исходный tool и не одобрить остальные действия.
Поддерживаются только `agent_control.task_propose` и `warehouse.update_item` с
атомарной квитанцией получателя и свежей точной сверкой артефакта. Внешние
непроверяемые эффекты остаются заблокированными.

Решение хранится отдельно с уникальностью source attempt/action/target step;
повтор идентичного POST идемпотентен, другое тело — конфликт. Source checkpoint,
receipt, action journal, owner, latest turn, план, config, срок и общие бюджеты
сверяются перед созданием шага и повторно worker-ом перед одноразовым
потреблением. GET checkpoint выдаёт UI отдельный offer только после тех же
read-only проверок; E09 verdict по-прежнему имеет `can_resume=false` и не
является полномочием на продолжение. GET и POST используют актуальную активную
роль владельца из БД, не доверяя устаревшим JWT-claims.

Worker атомарно сохраняет безопасный target checkpoint до продолжения. Для
`tool_started` он подставляет адаптированный ответ receipt, для
`tool_recorded/outcome_unknown` заменяет только соответствующий unknown-result.
Исходный action исключён из pending tail. При crash после потребления решение
может быть восстановлено только из точного pre-tail checkpoint предыдущей
abandoned attempt; после более позднего checkpoint повтор запрещён. Обычные
approval/RBAC/lease gates для последующих tools сохраняются.

UI показывает отдельную карточку «Продолжить после проверенного результата»
только по server-driven offer; verified-commit POST не повторяется автоматически
при ошибке сети или `409`. Старый confirmation UI и transport остаются прежними.

## Независимая приёмка

- Backend: 156 passed в связке contract, receipts, durable chat, checkpoints и
  lease tests. Проверены конкурентные решения, crash/reclaim, один receipt,
  changed artifact, revoked role, newer turn и несовпадающее тело решения.
- Frontend: TypeScript typecheck; 16 unit tests; 3 mock Playwright tests.
- Ruff, format, `git diff --check`: пройдены.
- Alembic: единственный head `20260927_0001`.

Production-стек пересобран и перезапущен через `make prod-build`; backend,
frontend и обычный Celery worker healthy. В контейнере backend применена
ревизия Alembic `20260927_0001 (head)`. HTTPS `https://localhost/health`
вернул `{"status":"ok"}`. SHA-256 `chat_runs.py` и `durable_chat.py` совпали
между host и backend container. Browser E2E выполнен с mock API, а не с
реальными клиентскими документами.

Изолированный crash-тест использует реальную тестовую PostgreSQL и
`reclaim_expired_leases → claim_ready_step → run_durable_chat` с инъекцией сбоя
после consume. Браузерный тест использует mock API и не заменяет live smoke.

Исполнители: `gpt-5.6-sol` (E11.1/E11.2/backend eligibility),
`gpt-5.6-terra` (E11.3 UI); root выполнял независимый review и исправление
изоляции тестов. Экономия токенов не измерена.
