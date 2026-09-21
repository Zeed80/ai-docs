# Пошаговый план реализации для модели-исполнителя

Срез: 15 сентября 2026. Назначение: передавать небольшие, проверяемые задания
модели уровня, выбранного оператором, а сложный review выполнять отдельно.
План не зависит от коммерческого названия модели и не предполагает одинаковой
надёжности всех моделей. Более дешёвой модели уменьшаем задание, а не требования
к безопасности. Этот файл — инструкция выполнения остатка из
`AGENT_EMPLOYEE_IMPLEMENTATION_PLAN.md`, а не новый альтернативный roadmap.

По поручению пользователя включён режим [`AGENT_EMPLOYEE_ORCHESTRATION.md`](./AGENT_EMPLOYEE_ORCHESTRATION.md):
сеньор сам назначает карточку подагенту-исполнителю, проверяет diff и возвращает
замечания. Ручное копирование карточек пользователем не требуется. Правило «одна
карточка за заход» относится к исполнителю, не к необходимости нового сообщения
пользователя на каждую карточку. Полномочия и Definition of Done остаются прежними.

## 1. Что делать следующему исполнителю прямо сейчас

1. Прочитать `AGENTS.md`, разделы 1–6 этого файла и **только карточку E01**.
2. Выполнить `git status --short`, `git log -5 --oneline`, `git diff --stat`.
3. Сравнить фактическое дерево с зафиксированным ниже срезом. Не стирать отличия.
4. Выполнить E01, записать доказательства. Не переходить автоматически к E10,
   новому runtime, sandbox или массовому рефакторингу.
5. Следующая карточка после E01 — E02. После E02 провести первую контрольную проверку.

### 1.1. Проверенная отправная точка

- Локальный коммит `548ba327`: атомарная квитанция только для
  `agent_control.task_propose`, передача ключа из durable-worker через gateway,
  чтение квитанции в owner-only деталях журнала, отображение в UI.
- Предыдущие локальные коммиты: `2348a441` — запрет слепого транспортного retry;
  `261c05c9` — UI журнала/наблюдений. Ничего из этого не нужно переписывать с нуля.
- Проверка этого среза: 179 целевых backend-тестов, 64 frontend-теста,
  4 Chromium-сценария с подставным API, typecheck, Ruff.
- Production пересобран; `/health` возвращал `ok`. Это историческое свидетельство
  данного среза, не обещание здоровья сервиса в будущий момент запуска.
- Тесты с подставным агентом/API не доказывают работу живой LLM или внешнюю доставку.

### 1.2. Заготовка E00 завершена через делегирование

После `548ba327` в рабочем дереве были оставлены непроверенные изменения:

- `backend/app/domain/action_receipts.py`: функция `verify_proposal_receipt`;
- `backend/app/api/chat_runs.py`: `GET /{run_id}/actions/{action_id}/verification`.

Эта заготовка завершена исполнителем `gpt-5.6-luna` и независимо проверена сеньором.
Новые тесты проверяют read-only сравнение, ACL и отсутствие DML через ASGI;
результаты и фактическая выкладка отражены в
`docs/agent-employee-delivery/E00-receipt-verification.md`.
UI текущей сверки ещё нет: следующий участок E01. Старые 179 тестов не выдавать
за результаты новой проверки. На другой машине сначала сверить Git и отчёт.

### 1.3. Git и публикация

Текущая ветка среза: `fix/audit-p0-p1-capability-grants`.
Последняя проверенная удалённая вершина: `8800f026943102008466297c8d76c294b66abc7a`.
В локальной истории также накоплены десятки CAD-коммитов. Автопроверка запретила
публикацию **всей** этой истории по общему поручению продолжить агентский план.

- Локальный commit разрешён в рамках задачи; push всей накопленной ветки требует
  отдельного разрешения на этот состав изменений. Запрет не обходить.
- Не делать force push, reset, массовый cherry-pick, новую ветку для обхода запрета
  или подмену remote. Сначала получить явное разрешение на план публикации.
- Не включать незавершённый код в docs-коммит. Не использовать `git add .`.
- Перед будущим push заново проверить remote и `git log origin/<ветка>..HEAD`.

## 2. Неизменяемые продуктовые решения

1. Цель — универсальный цифровой сотрудник: работа в браузере, с файлами,
   внутренними API, коммуникациями и координацией. Первый выпуск не включает
   произвольное управление рабочим столом ОС.
2. **Не возвращать эвристическое исполнение:** keyword routing, выбор процедуры
   по сходству, автоматическое согласие по словам пользователя, silent fallback
   на рецепт при ошибке модели. Ошибка модели — явное состояние, не другой агент.
3. **Не возвращать генерируемые постоянные возможности:** runtime-генерацию
   зарегистрированных tools, автопродвижение, shadow execution, recipe replay.
   Новые инструменты добавляются разработчиком по проверяемому контракту.
4. Детерминированные проверки типов, ACL, budget, digest, lease и URL необходимы.
   Это защитные правила, а не заменяющие мышление эвристики; удалять их нельзя.
5. Одноразовый код — артефакт одной работы в ОС-изоляции. Он не становится skill,
   не импортируется в backend, не получает секреты или Docker socket.
6. Агент не подтверждает собственное действие и не расширяет свои полномочия.
   Поручение разработчику «без подтверждений» не меняет approval gates продукта.
7. Конфиденциальный контент остаётся локальным. Облачный маршрут включается только
   существующим явным решением оператора; в тестах использовать синтетические данные.
8. Непустой ответ, Celery SUCCESS, HTTP 200 и слова модели — не доказательство
   выполнения цели. `partial`, `waiting_approval`, `outcome_unknown` не успех.

## 3. Как работать маленькими заданиями и экономить контекст

### Единица работы

- Одна карточка за заход. Внутри неё сначала тест, затем минимальная реализация.
- Ориентир: 2–5 production-файлов; тесты и краткая документация отдельно.
  Это не лимит качества. Если нужно менять больше, сначала разделить карточку
  на `.1/.2/.3`, сохранив зависимости и критерии приёмки.
- Нельзя «попутно» менять дизайн остальных модулей, обновлять зависимости,
  форматировать весь репозиторий или чинить постороннее CAD-направление.
- Для малоёмкой модели: сначала только тесты одной карточки; вторым заходом
  реализация; третьим — независимая сверка diff и запуск проверок.
- Не запускать субагентов, если оператор отдельно не поручил это.
- После двух неудачных вариантов не расширять рефакторинг. Зафиксировать
  минимальный reproducer, ошибку и передать на review. Догадки не коммитить как fix.

### Минимальный пакет контекста

1. Эта вводная часть; выбранная карточка и её зависимости.
2. Конкретные файлы из поля «Читать/менять» и применимые `AGENTS.md`.
3. Последний отчёт предыдущей карточки, изменённые контракты, результаты тестов.
4. Поиск `rg` по вызывающим сторонам изменяемой функции. Не читать весь
   `DEVPLAN.md`, все архивы, все tools или все журналы предыдущих сессий.

### Готовый запрос модели-исполнителю

```text
Репозиторий: /home/project/document-invoices-ai_codex.
Выполни только карточку E__ из AGENT_EMPLOYEE_EXECUTION_PLAYBOOK.md.
Сначала прочитай AGENTS.md, разделы 1–6 playbook, карточку и её зависимости.
Проверь git status и фактическую реализацию: не считай план доказательством кода.
Не возвращай эвристическое исполнение, generated skills или blind retry.
Не ослабляй владельца/ACL/approval/lease/budget. Не трогай несвязанные изменения.
Добавь указанные негативные и конкурентные тесты. Не заменяй их моками самой
проверяемой транзакции/границы. Выполни проверки карточки и общий Definition of Done.
При кодовых изменениях перед выдачей — make prod-build, /health, сверка кода.
Commit только файлов карточки. Заблокированный push не обходить.
Выдай: что сделано, файлы, команды и фактические результаты, ограничения,
локальный commit, состояние production, следующую карточку. Не объявляй весь план
готовым. Если архитектурный контракт не определён — остановись с точным вопросом,
а не придумывай более слабую гарантию.
```

### Готовый запрос проверяющей сильной модели

```text
Проверь карточки E__–E__, коммиты <base>..<head>.
Прочитай их контракты в AGENT_EMPLOYEE_EXECUTION_PLAYBOOK.md, затем реальный diff,
вызывающие стороны и тесты. Не полагайся на отчёт исполнителя.
Проследи UI/channel → intake → worker → gateway → recipient → receipt → verifier.
Ищи обход owner/RBAC/approval, устаревшую попытку, повтор эффекта, сброс budget,
ложный succeeded, утечку данных и скрытый fallback. Проверь окна гонок и реальные
транзакции. Запусти минимальные воспроизводящие проверки на отдельной тестовой БД.
Ответ: findings по важности с файлами/строками, какие критерии не доказаны,
что разрешено включить, что оставить выключенным. Не исправляй без поручения.
```

## 4. Definition of Done: одинаков для каждой карточки

Карточка готова только когда выполнены все применимые пункты:

- [ ] Зависимости завершены; изменённые файлы и контракт перечислены.
- [ ] Успешный, ошибочный и запрещённый пути покрыты тестами.
- [ ] Для side effect есть тест отказа до/после commit и конкурентного повтора.
- [ ] Для пользовательских данных есть Alice/Bob и изменение прав после создания.
- [ ] Для async runtime есть expired lease, cancel, duplicate delivery, stale attempt.
- [ ] Положительные результаты получены реально; skipped/xfail не названы passed.
- [ ] Нет широкого `except: return success`, hardcoded admin, нового unsafe fallback.
- [ ] Изменения проверены Ruff/форматтером, frontend — typecheck/целевыми тестами.
- [ ] Проверен diff; форматирование hook не оставило непротестированный код.
- [ ] Миграции upgrade/downgrade/upgrade выполнены в отдельной PostgreSQL-схеме.
- [ ] Для кода: production пересобран и проверен; для docs-only сборка не нужна.
- [ ] Описаны границы гарантии и ещё не выполненные проверки.
- [ ] Обновлён статус карточки и создан отчёт, затем отдельный scoped commit.

**Статусы:** TODO → IN_PROGRESS → TESTED → DEPLOYED → REVIEWED.
Документационная карточка может перейти TESTED → REVIEWED без DEPLOYED с пометкой
`docs-only`. BLOCKED означает конкретную внешнюю зависимость, а не «сложно».
Новая защитная возможность не включается массово до указанного review gate.
Не скрывать исходный результат провального прогона; записать причину и повтор.

### Форма отчёта

Создавать `docs/agent-employee-delivery/E__-<slug>.md` (новая папка, если отсутствует):

```text
Карточка / статус / дата / base commit / commit результата:
Изменено (пути и контракт):
Не изменено и почему:
Проверки (точные команды, passed/failed/skipped, где запущены):
Конкурентность/безопасность (какой сценарий действительно выполнен):
Production (build, контейнеры, /health, hash; либо docs-only):
Известные ограничения / выключенные флаги / rollback:
Review gate и следующая карточка:
```

Не класть в отчёт токены, `.env`, cookies, клиентские документы, личную переписку
или дампы production. Синтетические fixtures допустимы.

## 5. Проверочные команды и границы среды

Из корня; при проблеме проверить cwd, а не переписывать импорты под случайный путь.

```bash
git status --short
git diff --check
python3 -m pytest backend/tests/test_action_receipts.py backend/tests/test_tool_transport.py backend/tests/test_chat_checkpoints.py -q
python3 -m pytest backend/tests/test_chat_action_journal.py backend/tests/test_durable_chat.py backend/tests/test_agent_execution_boundary.py backend/tests/test_agent_delegations.py -q
python3 -m pytest backend/tests/test_work_order_lease.py backend/tests/test_work_order_checkpoint.py backend/tests/test_work_order_verifier.py backend/tests/test_work_orders.py -q
```

Это базовые наборы, не полный test suite. Новые файлы тестов из карточек добавлять
к команде явно. Ruff запускать по изменённым Python-файлам; конфигурацию брать
из репозитория/pre-commit, не вводить новую несовместимую.

В `frontend/`:

```bash
npm run typecheck
npm test
PLAYWRIGHT_MOCK_API=1 npx playwright test tests/e2e/durable-chat.spec.ts tests/e2e/chat-action-journal.spec.ts --project=chromium
```

Production из корня после кодовых изменений:

```bash
make prod-build
docker compose -f infra/docker-compose.yml -f infra/docker-compose.prod.yml --env-file infra/.env ps
curl -k --fail https://localhost/health
```

- Использовать `/health`, не `/api/health`. Проверить новый код в backend **и worker**,
  например `sha256sum` конкретных изменённых модулей в контейнере и checkout.
- Не запускать pytest с production `DATABASE_URL`; тесты используют отдельную БД.
  Интеграционные тесты с commit между соединениями должны завершать свои тестовые
  работы: иначе глобальные budget/reaper тесты подхватят чужую активную fixture.
- В этом checkout рабочий Alembic путь: `backend/alembic.ini` → `migrations`.
  Не класть новую миграцию по похожему старому пути `app/db/migrations` по привычке.
- Реальные SMTP, Telegram, внешние формы и платные облачные модели не вызывать
  из обычного теста. Для живой приёмки нужны согласованные тестовые назначения.
- Не делать глобальный Docker prune. Остановленные чужие контейнеры не удалять.
- **Не запускать rebuild из дерева с посторонним незавершённым кодом.** Сначала
  согласовать состав выкладки. Docs-only обновление этого playbook не публикует
  заготовку E00 и не требует restart.

## 6. Карта файлов и порядок

Существующие точки входа, подтверждённые в срезе:

| Область | Файлы |
|---|---|
| Чат/API/UI | `backend/app/api/chat_runs.py`, `frontend/lib/durable-chat.ts`, `frontend/components/chat/assistant-panel.tsx` |
| Executor/checkpoint | `backend/app/ai/agent_loop.py`, `backend/app/ai/chat_checkpoint.py`, `backend/app/tasks/durable_chat.py` |
| Заказы/worker/verifier | `backend/app/domain/work_orders.py`, `backend/app/tasks/work_orders.py`, `backend/app/api/work_orders.py` |
| Журнал/квитанции | `backend/app/domain/chat_action_journal.py`, `backend/app/domain/action_receipts.py`, `backend/app/db/agent_runtime_models.py` |
| Подтверждение | `backend/app/domain/chat_continuation.py`, `backend/app/api/chat_runs.py` |
| Gateway/каталог | `backend/app/api/capability_router.py`, `backend/app/ai/tool_catalog.py`, `backend/app/ai/tool_transport.py` |
| Результаты/политики | `backend/app/ai/tool_result.py`, `backend/app/ai/policy_engine.py`, `backend/app/domain/delegations.py` |
| Квитанция task_propose | `backend/app/api/agent_control_plane.py`, `backend/tests/test_action_receipts.py` |
| Telegram/cron | `backend/app/integrations/telegram_bot.py`, `backend/app/api/telegram.py`, `backend/app/tasks/agent_cron.py` |
| Скрипты/артефакты | `backend/app/db/agent_runtime_models.py` (`AgentScriptRun`), `backend/app/db/models.py` (`WorkArtifact`) |
| Браузер | `backend/app/api/computer_use.py`, `infra/web-browser/server.py`, `infra/web-browser/Dockerfile` |
| Память/граф | `backend/app/api/memory.py`, `backend/app/ai/memory_manager.py`, `backend/app/domain/memory_builder.py`, `backend/app/tasks/graph_memory.py` |
| Векторы | `backend/app/ai/embeddings.py`, `backend/app/tasks/embedding.py`, `backend/app/scripts/backfill_embeddings.py` |
| UX | `frontend/app/work-orders/page.tsx`, `frontend/components/chat/action-journal.tsx`, `frontend/app/settings/delegations/page.tsx` |
| Evals | `backend/app/ai/evals/harness.py`, `backend/app/ai/evals/agent_roles.py`, `backend/app/ai/evals/agent_role_cases.json` |

Новые пути в карточках обозначены как **создать**; если аналог уже появился,
переиспользовать его после проверки, не делать вторую систему с тем же смыслом.

Порядок для одного исполнителя: E00 → E01 → … → E52. Зависимости в карточках
обязательны; технически независимую работу можно перенести только с записью причины.
Нумерация семи крупных этапов исходного плана — группировка, а не запрет завершить
фундамент результатов перед переключением дополнительных каналов.

| Пакет | Карточки | Крупные этапы исходного плана | Контрольная точка |
|---|---|---|---|
| A | E00–E11 | сверка, результаты, receipts, безопасное продолжение | review после E02, E09, E11 |
| B | E12–E25 | единый runtime, каналы, outbox, budget, state machine | review после E18, E25 |
| C | E26–E30 | изолированные одноразовые скрипты | review до включения supervisor |
| D | E31–E37 | браузер, секреты, handoff, сеть | review до внешних действий |
| E | E38–E44 | SQL/graph/vector ACL и версии | review до backfill/shared retrieval |
| F | E45–E48 | UI, разрешения, расходы, удаление retired кода | review совместимости |
| G | E49–E52 | измеримый корпус и выпуск | финальная независимая приёмка |

## 7. Пакет A — завершить контур результата

### E00 — Read-only сверка предложения с квитанцией

**Статус:** REVIEWED и DEPLOYED; проверки и выкладка — в отчёте
`docs/agent-employee-delivery/E00-receipt-verification.md`. **Зависимость:** `548ba327`.
**Читать/менять:** `domain/action_receipts.py`, `api/chat_runs.py`,
`backend/tests/test_action_receipts.py`; префикс Python-путей — `backend/app/`.

1. Проверить заготовку `verify_proposal_receipt`, не принимать её автоматически.
2. API сначала проверяет владельца run/action; чужой объект — 404. Проверка текущего
   AgentTask требует актуального admin, как control-plane; прежняя роль не подходит.
3. Прочитать квитанцию через `read_receipt`, проверить целостность. Нет квитанции —
   `inconclusive`, **не** «эффекта не было». Неизвестный тип — `inconclusive`.
4. По записанному ID прочитать текущую задачу. Сравнивать явные поля: id, objective,
   description, role, status, team_id, output, metadata. Вернуть список полей,
   expected/current content digest и время наблюдения; чужое содержимое не выдавать.
5. Статусы: `matched`, `changed`, `missing`, `inconclusive`. Это снимок сейчас:
   matched не означает, что объект никогда не менялся; missing не отменяет commit.
6. Только SELECT. Не менять WorkOrder, checkpoint, action, receipt; не делать
   HTTP-запросов по ссылкам; `can_resume=false`, `can_replay=false` во всех исходах.
7. Добавить тесты каждого статуса, испорченного hash, Alice/Bob, пониженной роли,
   отсутствующего action, отсутствующей квитанции. Проверить число WorkEvent и
   состояние работы до/после GET. Документировать scope сравнения, не называть hash
   полноценной системой версий артефакта.

**Проверка:** `test_action_receipts.py` + `test_chat_action_journal.py`.
**Готово:** тесты показывают сверку без исполнения и без новых полномочий.

Ориентир ответа E00 (идентификаторы условные, digest обязан вычисляться сервером):

```json
{
  "action_id": "<uuid>",
  "status": "matched",
  "observed_at": "<UTC timestamp>",
  "scope": "agent_task_content_snapshot",
  "artifact_id": "<uuid>",
  "receipt_response_digest": "<sha256>",
  "expected_content_digest": "<sha256>",
  "current_content_digest": "<sha256>",
  "checked_fields": ["id", "objective", "description", "role", "status", "team_id", "output", "metadata"],
  "can_replay": false,
  "can_resume": false
}
```

При missing current digest = null; при inconclusive обязательны status/reason,
scope/time/action и запреты replay/resume, но нельзя выдумывать artifact ID/digest.
`observed_at` обозначает момент чтения снимка, не гарантию неизменности после ответа.

### E01 — UI текущей сверки

**Статус:** REVIEWED; production-приёмка в отчёте
`docs/agent-employee-delivery/E01-current-verification-ui.md`. **После:** E00.
**Файлы:** `frontend/components/chat/action-journal.tsx`,
`frontend/tests/unit/action-journal.test.tsx`, `frontend/tests/e2e/chat-action-journal.spec.ts`.

1. В секции квитанции добавить кнопку «Проверить текущее состояние», отправляющую
   только GET на verification. Отображать время, статус и точный scope.
2. Развести три сущности: ответ worker; квитанция прошлого commit; текущая сверка.
3. При повторной проверке очистить старый verdict или явно пометить его устаревшим.
   AbortController и key карточки не допускают перенос ответа к другому действию.
4. 403 объясняет отсутствие текущего доступа, 409 — нарушение целостности;
   обе ошибки не должны показывать зелёный сохранённый verdict.
5. Для missing/changed не показывать кнопку повтора действия. Наблюдения человека
   остаются отдельным непроверенным источником.

**Негативные тесты:** поздний ответ после смены карточки, отказ доступа после
успешного GET, повторное нажатие, HTTP 500, отсутствие POST/автовозобновления.
**Готово:** unit + Chromium mock API, typecheck; не путать с live E2E.

### E02 — Полная история наблюдений

**Статус:** REVIEWED и DEPLOYED; отчёт
`docs/agent-employee-delivery/E02-observation-history.md`. **После:** E01.
**Файлы:** `api/chat_runs.py`, `domain/chat_action_journal.py`, UI журнала;
тесты `test_chat_action_journal.py`, `action-journal.test.tsx`.

1. Добавить owner-only GET наблюдений конкретного действия с устойчивым cursor
   по sequence. Валидировать limit и принадлежность action/run.
2. Вернуть события по порядку: автор, время, outcome, note, reference, verified=false.
3. Не превращать последнее наблюдение в замену истории; старые POST остаются
   идемпотентными по request_id и точному телу.
4. UI — «Показать предыдущие», без автозагрузки внешних ссылок и raw HTML.
5. Проверить несколько страниц, отсутствие повторов, чужую работу, одинаковые
   timestamps, длинные/XSS-подобные строки, отсутствие мутаций через GET.

**Готово:** read-only история не даёт дополнительных прав. **Gate A1:** review E00–E02.

### E03 — Инвентаризация эффектов и транзакционных границ

**Статус:** REVIEWED; отчёт
`docs/agent-employee-delivery/E03-tool-effect-inventory.md`. **После:** E02.
**Читать:** `ai/tool_catalog.py`, `api/capability_router.py`, route handlers операций.
**Создать:** `docs/agent-employee-delivery/tool-effect-inventory.md`.

1. Для каждой активной catalog operation записать route, backend RBAC, effect,
   получателя, место commit, внешний эффект, внутренние retries, поддержку receipt.
2. Не классифицировать эффект по имени, префиксу или одному HTTP-методу.
3. Выделить группы: read-only; один DB commit; DB + async enqueue; внешняя отправка;
   browser/script/MCP; неизвестно. Неизвестно — запрещён автоматический retry.
4. Отметить расхождения каталога и backend (в том числе task_propose/admin).
   Исправлять права отдельной карточкой, а не делать endpoint менее строгим.
5. Дополнить `test_capability_catalog_consistency.py` проверкой наличия классификации
   для каждой активной операции. Не требовать, чтобы все legacy routes уже были migrated.

**Готово:** полный список с явными пробелами; выбрана одна безопасная DB-операция
для E08. Нельзя массово добавлять receipt wrapper вокруг endpoint с ранним commit.

### E04 — Контракт ToolResult и правила классификации

**Статус:** REVIEWED; отчёт `docs/agent-employee-delivery/E04-tool-result-contract.md`. **После:** E03.
**Файлы:** `ai/tool_result.py`, существующие consumers; создать
`backend/tests/test_tool_result_contract.py`.

1. Сначала описать version=1: status, data, error_code, retryable, evidence, checkpoint.
2. Отдельно определить «вызов успешен», «работа завершена», «можно повторить».
   Один boolean `result_failed` не заменяет три разных решения.
3. Явный error/error_code/errors или built=false не становится succeeded из-за 200.
4. partial/waiting_approval/outcome_unknown нельзя передать как успешное завершение;
   не переименовывать их все в failed с потерей причины.
5. Legacy normalizer работает только по явному контракту конкретного адаптера;
   неизвестный ответ — непризнанный результат, не угаданный успех.
6. Тестовая матрица: каждый status, противоречивые поля, null, список, строка,
   неизвестная версия, вложенный domain error. Сохранить нужную совместимость.

**Готово:** таблица переходов и чистые unit-тесты. Массовая смена транспорта — E05.

Минимальная семантическая матрица, которую нельзя упростить до truthiness:

| Наблюдение | Результат вызова | Следующий эффект | Завершение всей работы |
|---|---|---|---|
| Явно проверенный успешный domain response | succeeded | Только по плану и правам | Только после acceptance |
| Domain validation error до эффекта | failed | По явной политике, без blind retry | Нет |
| Запись: timeout/неоднозначный ответ после dispatch | outcome_unknown | Остановить до сверки | Нет |
| Выполнена лишь часть договорённого результата | partial | Только оставшаяся доказанная часть | Нет |
| Требуется решение человека | waiting_approval | Остановить конкретное действие | Нет |
| Async job только принят | Незавершённое состояние по контракту адаптера | Только проверка job/read-only ожидание | Нет |
| Чтение: ответ не соответствует схеме | failed | Допустимо только bounded read retry по policy | Нет |

Если операция неизвестна и нельзя доказать отсутствие эффекта, нельзя выбирать
строку «ошибка до эффекта». Наличие receipt проверяется независимо от ответа LLM.

### E05 — Перевод адаптеров на ToolResult по одной группе

**Статус:** IN PROGRESS: E05.1 REVIEWED; E05.2 и E05.3 SCOPED COMPLETE /
REVIEWED; E05.4 не завершена. Отчёты:
`docs/agent-employee-delivery/E05-1-read-adapters.md`,
`docs/agent-employee-delivery/E05-2-1-db-write-adapters.md`,
`docs/agent-employee-delivery/E05-2-2-db-write-adapters.md`,
`docs/agent-employee-delivery/E05-2-3-db-write-adapters.md`,
`docs/agent-employee-delivery/E05-2-4-db-write-adapters.md`,
`docs/agent-employee-delivery/E05-2-5-db-write-adapters.md`,
`docs/agent-employee-delivery/E05-2-6-db-write-adapters.md`,
`docs/agent-employee-delivery/E05-2-7-db-write-adapters.md`,
`docs/agent-employee-delivery/E05-2-8-db-write-adapters.md`,
`docs/agent-employee-delivery/E05-2-9-db-write-adapters.md`,
`docs/agent-employee-delivery/E05-2-10-db-write-adapters.md`,
`docs/agent-employee-delivery/E05-2-closure.md`,
`docs/agent-employee-delivery/E05-3-1-async-job-adapters.md`,
`docs/agent-employee-delivery/E05-3-2-async-job-adapter.md`. **Следующая:** E05.4.
**Файлы:** `ai/agent_loop.py::execute_skill`, `api/capability_router.py`,
`ai/tool_transport.py`, адаптеры из E03, тесты транспорта/gateway.

1. Разделить на E05.1 read-only API, E05.2 простые DB writes,
   E05.3 async jobs, E05.4 внешние/MCP handlers. Каждый подэтап — отдельный commit.
2. Адаптер знает domain contract: возвращённый job_id означает принятие в очередь,
   а не готовый артефакт; HTTP timeout записи означает unknown.
3. Не менять публичные бизнес-API все сразу: envelope формируется на агентской
   границе, потребители старого business API сохраняют свой контракт.
4. Сохранить raw payload в data; не потерять artifact IDs, ошибки и доказательства.
5. Убедиться, что проверки каталога и approval получают исходные аргументы,
   а не новый envelope или автоматически «исправленную» моделью копию.

E05.2.1 охватывает только `analytics.collection_create`,
`analytics.calendar_create_reminder`, `analytics.table_create_view` и
`warehouse.create_item`. Их успешный 2xx domain response нормализуется в
ToolResult v1 с raw payload в `data`; явная domain-ошибка и 4xx — `failed`;
неоднозначность после dispatch — `outcome_unknown`; ошибка до dispatch —
`failed`. Автоматический retry отсутствует. Остальные строки E03 с классом
`one-db-commit` сохраняют прежний контракт до отдельных срезов E05.2.

E05.2.2 добавляет только `analytics.collection_add_item`,
`analytics.collection_close`, `analytics.compare_create`,
`analytics.compare_align` и `analytics.table_inline_edit`; cumulative allowlist
содержит девять операций. Контракт E05.2.1 не менялся. Каждый выбранный handler
доказывает один прямой `db.commit`; `add_timeline_event`/`log_action` — только
`flush()`. Route alias `POST /api/compare` без точной capability/action-пары
fail-closed; `analytics.calendar_extract_dates` исключён из-за runtime 0/1
commit-границы. Отчёт:
`docs/agent-employee-delivery/E05-2-2-db-write-adapters.md`.

E05.2.3 добавляет только `warehouse.update_item`, `warehouse.adjust_stock` и
`warehouse.create_receipt`; cumulative allowlist содержит 12 операций. Для
каждого выбраны уникальная route/action-пара и один прямой безусловный
`db.commit()` на success path; вызванные `log_action`/`add_timeline_event` —
flush-only, external dispatch/enqueue нет. Три action отсутствуют в
`warehouse.gate_actions`; это не меняет RBAC или approval policy.
`warehouse.confirm_receipt`, `warehouse.issue_stock`, `warehouse.delete_item`,
`warehouse.update_status` и `warehouse.bulk_confirm` исключены и fail-closed.
Контракт E05.2.1 сохранён; production пока не заявлен. Отчёт:
`docs/agent-employee-delivery/E05-2-3-db-write-adapters.md`.

E05.2.4 добавляет только `email.templates.create`, `email.templates.update` и
`suppliers.update`; cumulative allowlist содержит 15 операций. У каждой точная
уникальная route/action-пара и один прямой безусловный `db.commit()` на success
path; select/flush-помощники не добавляют commit, external dispatch/enqueue нет.
Все три catalog operations имеют `admin_only=false` и не approval-gated.
`email.templates.from_message` исключён из-за возможного `ai_router.complete`,
`analytics.calendar_generate_followup` — из-за несоответствия identity
path-параметра `{entity_id}` и `{reminder_id}`. Render/delete/status и gated
actions остаются fail-closed. Контракт E05.2.1 сохранён; production E05.2.4 не
заявляется. Отчёт: `docs/agent-employee-delivery/E05-2-4-db-write-adapters.md`.

E05.2.5 добавляет только `procurement.create_request`: точный уникальный
`POST /api/purchase-requests`; cumulative allowlist содержит 16 операций.
`create_purchase_request` имеет один прямой безусловный `db.commit()` на success
path, без helper commit, external dispatch или enqueue; операция `admin_only=false`
и не approval-gated. `procurement.update_request` и
`procurement.update_contract` исключены, поскольку принимают `status`;
`procurement.create_contract` остаётся fail-closed из-за route alias
`POST /api/compare`; `procurement.send_rfq` — из-за external effect.
Safety correction: catalog GET `suppliers.trust_score` условно коммитит
`profile.trust_score`, поэтому
`READ_CATALOG_OPERATIONS_WITH_PERSISTENT_EFFECTS` запрещает read retry как по
прямому маршруту, так и через capability route. Это не write adapter: граница
commit условна (0/1). Контракт E05.2.1 сохранён; независимый набор — 207 passed
с известным предупреждением `asyncio_loop_scope`; production E05.2.5 не
заявляется. Отчёт: `docs/agent-employee-delivery/E05-2-5-db-write-adapters.md`.

E05.2.6 добавляет только `documents.link`, `email.draft` и
`payments.create_schedule` через точные уникальные соответственно
`POST /api/documents/{document_id}/links`, `POST /api/email/drafts` и
`POST /api/payment-schedules`; cumulative allowlist содержит 19 операций. У
каждого handler-а один прямой безусловный `db.commit()` на success path;
`log_action` и `create_reply_draft` ограничены flush/select как применимо,
external dispatch/enqueue и AI отсутствуют. Все три `admin_only=false` и не
approval-gated. `email.compose`, `email.reply` и
`email.templates.from_message` исключены из-за AI path; `sheets.create` — из-за
`chat_bus` publish после commit; send/external, delete/status/gated и прочие
непринятые actions остаются fail-closed. Контракт E05.2.1, public API, RBAC и
approval policy не менялись. Независимый набор: 216 passed с известным
предупреждением `asyncio_loop_scope`; production не заявлен. Отчёт:
`docs/agent-employee-delivery/E05-2-6-db-write-adapters.md`.

E05.2.7 добавляет только `normalization.create_norm_card`,
`normalization.update_norm_card` и `normalization.update_canonical_item` через
точные уникальные соответственно `POST /api/normalization/norm-cards`, `PATCH
/api/normalization/norm-cards/{card_id}` и `PATCH
/api/normalization/canonical-items/{item_id}`; cumulative allowlist содержит 22
операции. У каждого handler-а один прямой безусловный `db.commit()` на success
path; `log_action` — flush-only, AI/network/enqueue нет. Все три
`admin_only=false` и не approval-gated. `analytics.auto_approval_create`
исключён как admin-only, `analytics.auto_approval_check` — из-за conditional 0/1
commit, `payments.mark_paid` — как approval-gated; notification/settings не
входят в активный catalog, sheets publish остаётся вне среза. Контракт E05.2.1,
public API, RBAC и approval policy не менялись. Независимый полный набор из
корня: 225 passed с известным предупреждением `asyncio_loop_scope`; production
не заявлен. Отчёт: `docs/agent-employee-delivery/E05-2-7-db-write-adapters.md`.

E05.2.8 добавляет только `invoices.update` и `tool_catalog.create_supplier`
через точные уникальные соответственно `PATCH /api/invoices/{invoice_id}` и
`POST /api/tool-catalog/suppliers`; cumulative allowlist содержит 24 операции.
У каждого handler-а один прямой безусловный `db.commit()` на success path;
`update_invoice` вызывает только flush-only `log_action` и
`add_timeline_event`, а `InvoiceFieldUpdate` не содержит `status`.
`create_supplier` — DB-only; AI/network/external dispatch/enqueue отсутствуют.
Обе операции `admin_only=false` и не approval-gated. Контракт E05.2.1, public
API, RBAC и approval policy не менялись. Независимый полный набор из корня:
230 passed с известным предупреждением `asyncio_loop_scope`; production не
заявлен. Отчёт: `docs/agent-employee-delivery/E05-2-8-db-write-adapters.md`.

E05.2.9 добавляет только `invoices.validate` и `memory.source_propose` через
точные уникальные соответственно `POST /api/invoices/{invoice_id}/validate` и
`POST /api/memory/sources/propose`; cumulative allowlist содержит 26 операций.
У каждого handler-а один прямой безусловный `db.commit()` на success path;
`validate_invoice` выполняет детерминированную локальную арифметическую проверку
и вызывает только flush-only `log_action`, а `propose_web_source` сохраняет
только reviewable proposal без network/AI/enqueue. Обе операции
`admin_only=false` и не approval-gated. `invoices.approve`/`invoices.receive`
исключены из-за status/approval semantics, `memory.source_discover` — из-за
effects discovery, promotion остаётся human-only,
`memory.promotion_evaluate` — из-за identity-path mismatch, sheets publish
fail-closed. Контракт E05.2.1, public API, RBAC и approval policy не менялись.
Независимый полный набор из корня: 234 passed с известным предупреждением
`asyncio_loop_scope`; production не заявлен. Отчёт:
`docs/agent-employee-delivery/E05-2-9-db-write-adapters.md`.

E05.2.10 добавляет только `tech.correction_record` и
`tech.operation_template_create` через точные уникальные соответственно
`POST /api/technology/corrections` и `POST /api/technology/operation-templates`;
cumulative allowlist содержит 28 операций. У каждого handler-а один прямой
безусловный `db.commit()` на success path; helper-вызовы ограничены `flush()`
или `select()`, AI/network/external dispatch/enqueue и `chat_bus` publish нет.
Обе операции `admin_only=false` и не approval-gated. `normalization.suggest_rule`
и `normalization.apply_rules` исключены из-за conditional 0/1 commit,
`sheets.add_row` — из-за publish effect, `tech.resource_create` — поскольку
принимает `status`, `tech.learning_rule_activate`/`tech.learning_rule_reject`
— как approval-gated; остальные lifecycle/status, gated и непроверенные actions
fail-closed. Контракт E05.2.1, public API, RBAC и approval policy не менялись.
Независимый полный набор из корня: 238 passed с известным предупреждением
`asyncio_loop_scope`; production не заявлен. Отчёт:
`docs/agent-employee-delivery/E05-2-10-db-write-adapters.md`.

**Негативные тесты:** 200 + error, job SUCCESS + built=false, read timeout,
write timeout после commit, MCP exception, double wrapping. **Готово:** E05.2
SCOPED COMPLETE / REVIEWED: после коррекции E05.4.0 125 строк E03
`one-db-commit` разделены на exact allowlist из 28 мигрированных операций и 97
явных отказов. У остатка нет
пропущенных простых DB-only кандидатов; полный ledger и причины находятся в
`docs/agent-employee-delivery/E05-2-closure.md`. При закрытии email render
aliases исключены из read retry, `compare_decide` закреплён как approval/risk
gate, `email.templates.delete` получил alias gate, а
`task_propose.admin_only` исправлен. E05 остаётся IN PROGRESS.

E05.3.1 REVIEWED охватывает только `documents.classify`, `documents.extract` и
`documents.reprocess` через точный `POST /api/agent/cap/documents` и исходный
`action`; прямые routes и прочие actions fail-closed. Допустимая queue acceptance
нормализуется в ToolResult v1 `partial`/`job_queued`, сохраняющий raw `data`,
checkpoint и evidence. Корректный versioned nonterminal сохраняется, а
`succeeded` с queued job отклоняется. Одна попытка без retry: 4xx — `failed`,
неоднозначность после dispatch — `outcome_unknown`, ошибка до dispatch —
`failed`. Остальные async operations legacy. Независимо: 236 focused + 43
boundary/router теста с известным предупреждением `asyncio_loop_scope`;
production rebuild и `/health` проверены. Отчёт:
`docs/agent-employee-delivery/E05-3-1-async-job-adapters.md`. E05.3 и E05
остаются IN PROGRESS; следующий шаг — отдельно аудируемый E05.3-срез без
предположения о конкретной операции.

E05.3.2 REVIEWED добавляет только `tech.generate_tp_from_drawing` через точный
capability/action и строгий `task_id` + `plan_id` + `queued` receipt. E05.3
SCOPED COMPLETE / REVIEWED: 4 из 9 async operations мигрированы, 5 полностью
классифицированы и отложены; пропущенных строгих queue-acceptance кандидатов
нет. Независимо: 252 focused/catalog + 43 boundary/router теста. Отчёт:
`docs/agent-employee-delivery/E05-3-2-async-job-adapter.md`. E05 остаётся IN
PROGRESS; следующий этап — E05.4 external/MCP handlers.

E05.4.0 REVIEWED унифицирует MCP policy boundary: Chat хранит schemas, но
исполняет MCP только через `/api/agent/cap/mcp` с wildcard approval, RBAC,
audit и digest исходных `{action, arguments}`; direct callable fail-closed.
Также исправлена E03-классификация двух procurement routes. Независимо: 219
passed. Отчёт: `docs/agent-employee-delivery/E05-4-0-mcp-boundary.md`. E05.4
остаётся IN PROGRESS; следующая карточка — E05.4.1 `tool_search_mcp`.

E05.4.1 REVIEWED переводит только gateway `tool_search_mcp` на строгий
ToolResult v1: exact response shape, одна попытка, malformed/4xx/pre-dispatch
failed и post-dispatch ambiguity outcome_unknown. Прочие MCP actions legacy.
Независимо: 290 focused и 317 расширенных тестов. Отчёт:
`docs/agent-employee-delivery/E05-4-1-tool-search-mcp-adapter.md`. E05.4/E05
остаются IN PROGRESS; следующий шаг — аудит остатка E05.4.

E05.4.2 REVIEWED исправляет built-in recipient reachability: configured backend
URL вместо container-local localhost, защищённые `/api` routes и штатные
internal-agent headers. ASGI regression доказывает gateway → built-in →
protected recipient; неуспешный drawing reanalyze не маскируется stale
snapshot. Независимо: 233 passed. Отчёт:
`docs/agent-employee-delivery/E05-4-2-mcp-builtin-reachability.md`. E05.4/E05
остаются IN PROGRESS; следующий срез — `email.send` queue acceptance.

E05.4.3 REVIEWED добавляет exact capability/action adapter `email.send`.
Строгий `queued` receipt с совпадающим `draft_id` означает только Celery
acceptance и возвращает `partial/job_queued`; SMTP delivery не подтверждается.
Одна попытка, post-dispatch ambiguity — `outcome_unknown`. Независимо: 333
passed. Отчёт:
`docs/agent-employee-delivery/E05-4-3-email-send-queue-adapter.md`. E05.4/E05
остаются IN PROGRESS; следующий срез запрещает generic read retry для
`computer_use.*`.

### E06 — Consumers не принимают незавершённый результат за успех

**Статус:** TODO. **После:** E05.
**Файлы:** `tasks/work_orders.py`, `tasks/durable_chat.py`, `ai/agent_loop.py`,
`domain/work_orders.py`, тесты worker/verifier/checkpoint.

1. Найти все места, где непустой dict/HTTP 200/текст считается успехом.
2. `outcome_unknown` записать и остановить до следующего tool/LLM call.
3. `waiting_approval` сохраняет точное ожидающее действие, не расходует согласие
   другого call; `partial` сохраняет результат, но не завершает критерий.
4. Domain failed обрабатывается заданной политикой; автоматический retry write
   разрешается только явной поддержкой получателя, не общим retryable=true модели.
5. Проверить настоящие worker state transitions и сохранённый журнал.

**Готово:** каждый status имеет тест перехода, без потери checkpoint/evidence.

### E07 — Основа масштабируемых квитанций

**Статус:** TODO. **После:** E06. **Читать:** `domain/action_receipts.py`, WorkEvent,
ChatLogicalAction, миграции `backend/migrations/versions/`.

1. Пилот WorkEvent не переписывать молча. Сначала ADR: оставляем event + индекс
   и common order lock либо добавляем отдельную receipt table с unique action ID.
2. Разделить стабильный logical_action_id и fencing attempt_id: новая попытка
   не означает новое логическое действие и не даёт право выполнить старое заново.
3. Обязательные привязки: owner/order/action/operation/request digest/response digest,
   artifact ID/revision, версия receipt. Ключ не является bearer-разрешением.
4. Первое исполнение проверяет свежие права и fence. Повтор читает старую квитанцию
   по тому же действию без нового эффекта; правила старого attempt указать явно.
5. Миграция пилотных receipts — только валидные данные, с provenance; corrupt
   записи не «чинить» пересчётом hash. Backward read до завершения миграции.

**Тесты:** уникальность двух соединений, rollback, collision другого owner/args,
отмена под тем же lock, migration roundtrip, corrupted/duplicate receipt.
**Готово:** ADR + тесты + один формат чтения; массовые recipients ещё не готовы.

### E08 — Вторая DB-операция с атомарной квитанцией

**Статус:** TODO. **После:** E07. **Выбор:** одна операция из E03 без внешнего эффекта.

1. В отчёте назвать точный route, доменную таблицу и текущий commit boundary.
2. Вынести создание/изменение объекта в функцию без внутреннего commit.
3. Под авторизацией и common fence записать эффект и receipt одной транзакцией.
4. Сохранить прежний путь без ключа для авторизованных legacy клиентов, если он
   нужен по контракту; наличие ключа включает строгую проверку, не отключает RBAC.
5. Проверить response serialization через настоящий ASGI route, не только функцию.
6. Повторить duplicate/lost response/rollback/cancel/foreign owner тесты пилота.

**Готово:** одна операция доказана. Для каждой следующей — отдельная E08.N;
операции DB+queue требуют outbox E13, SMTP/browser не объявлять exactly-once.

### E09 — Реестр независимых проверок артефактов

**Статус:** TODO. **После:** E08.
**Файлы:** receipts, `WorkArtifact`, verifier в `tasks/work_orders.py`;
создать модуль `domain/artifact_verification.py` при отсутствии аналога.

1. Реестр по явному типу артефакта/операции, без LLM выбора произвольного callback.
2. Verdict содержит verifier version, artifact ID и version/hash, scope, время,
   источник evidence. Проверка чужого owner запрещена до чтения содержимого.
3. Изменился артефакт — прежний verdict не доказывает новую версию.
4. Внешние references остаются текстом, пока для них нет безопасного адаптера;
   произвольный URL не скачивать из-за просьбы модели или человека.
5. Отсутствие записи у внешнего получателя — inconclusive, кроме отдельно
   доказанного полного авторитетного журнала с завершённым временным окном.

**Тесты:** stale artifact, forged verdict, unavailable recipient, missing version,
Alice/Bob; объект matched не завершает всю работу с другими критериями.
**Gate A2:** review до использования verdict для возобновления.

### E10 — Контракт продолжения после проверенного commit

**Статус:** TODO, сначала docs + failing tests. **После:** REVIEWED E09.
**Читать:** `domain/chat_continuation.py`, `api/chat_runs.py::resume_chat_run`,
`tasks/durable_chat.py`, checkpoint и action journal.

1. Описать отдельный переход: failed/blocked frontier с receipt committed →
   новая попытка, которая **подставляет сохранённый ответ**, а не повторяет tool.
2. Подтверждение владельца означает продолжить работу, а не повторить эффект
   или согласовать все оставшиеся tools. Новые gates действуют отдельно.
3. Связать решение с source attempt, checkpoint digest, action/receipt digest,
   планом, конфигурацией, владельцем, последним ходом и сроком действия.
4. Определить обработку tool_started и tool_recorded с unknown: убрать/заменить
   только соответствующий tool result, сохранить прочую историю и pending tail.
5. Не допускать продолжение canceled, нового пользовательского хода, changed/missing
   артефакта, неподдержанного receipt или недоказанного внешнего эффекта.
6. Общие budgets не обнуляются. Single-use решение и создание шага атомарны.

**Готово:** таблица состояний и тестовые сценарии согласованы review. До этого
не открывать can_resume и не менять старый confirmation-only путь.

### E11 — Реализация и UI продолжения без повторного эффекта

**Статус:** TODO. **После:** REVIEWED E10.
**Файлы:** перечисленные E10 + `frontend/lib/durable-chat.ts`, карточка подтверждения.

1. Сначала E11.1 backend transition и one-use event; затем E11.2 восстановление
   executor; затем E11.3 UI. Не делать всё одним большим изменением.
2. Worker читает persisted decision, заново проверяет owner/lease/plan/config/budget.
3. Сохранённый ответ вставляется ровно один раз, action ID сохраняется; завершённая
   операция не доходит до execute_skill. Продолжается только доказанный pending tail.
4. Повтор POST решения возвращает тот же run; другое тело с тем же ключом — 409.
5. Тест: получатель commit, ответ потерян, worker умер, человек согласился,
   новый worker продолжил: счётчик эффекта **1**, не 2. Два одновременных resume —
   один шаг. Перезапуск после потребления решения тоже не повторяет эффект.

**Готово:** integration + UI tests + изолированный crash E2E. **Gate A3:** review;
поддержка только проверенных recipients, не универсальный resume любого unknown.

## 8. Пакет B — единый долговечный runtime

### E12 — Общий intake, отделённый от HTTP и модели

**Статус:** TODO. **После:** E11.
**Файлы:** `api/chat_runs.py::submit_chat_run`, `domain/work_orders.py`,
создать `domain/agent_intake.py`, тесты durable chat.

1. Вынести транзакционное создание work/run/message в сервис с проверенной identity.
2. HTTP handler сохраняет human-only policy; внутренний channel adapter передаёт
   уже проверенный контекст, а не строку произвольного owner из тела запроса.
3. Idempotency namespace: channel + проверенный account + external message ID;
   тот же ID с другим вводом → conflict. UUID HTTP-клиента не конфликтует с Telegram.
4. Сохранить владение attachments, один активный ход в разговоре, атомарность.
5. Intake не вызывает AgentSession/LLM и не живёт до завершения работы.

**Тесты:** существующие HTTP ответы неизменны; конкурентный дубль; чужое вложение;
падение записи. **Готово:** HTTP работает через общий сервис без нового поведения.

### E13 — Транзакционный outbox: данные и producer

**Статус:** TODO. **После:** E12.
**Файлы:** runtime models, миграция; создать `domain/agent_outbox.py`.

1. Сначала схема события: immutable ID, owner, destination binding, payload/version,
   dedup key, состояние доставки, lease, next_attempt_at, attempts, error code.
2. Сохранение domain event и outbox row — одна транзакция. Никаких network calls
   внутри producer или SQL transaction, ожидающей ответ Telegram/SMTP.
3. Unique constraint защищает один логический notification/job, не только код if.
4. Не копировать секреты и весь model context в payload. Ссылки owner-bound.
5. Проверить upgrade/downgrade/upgrade, rollback producer, concurrent duplicate.

**Готово:** outbox существует, но доставка ещё выключена. At-least-once не назвать
exactly-once у внешнего сервиса без получательского dedup.

### E14 — Outbox worker и восстановление доставки

**Статус:** TODO. **После:** E13.
**Файлы:** outbox module; создать `tasks/agent_outbox.py`, регистрация Celery/beat.

1. Claim с lease/fencing и ограниченным batch; два worker не получают одну аренду.
2. Повтор допустим для внутреннего идемпотентного потребителя. Для внешней отправки
   классифицировать timeout как unknown, если API не поддерживает idempotency.
3. Конечные состояния sent/unknown/dead-letter; bounded backoff, без вечного hot loop.
4. Не связывать жизненный цикл исполнения с доступностью канала уведомлений.
5. Проверить kill после отправки до ack, retry duplicate, expired lease,
   недоступный получатель, отзыв channel binding.

**Готово:** доказаны границы доставки каждого адаптера; неизвестная отправка видна,
а не маскируется автоматическим дублем сообщения.

### E15 — Telegram intake вместо in-memory AgentSession

**Статус:** TODO. **После:** E14.
**Файлы:** `integrations/telegram_bot.py`, `api/telegram.py`, `test_telegram.py`.

1. Сохранить allowlist, проверенное связывание external ID и активного пользователя.
2. `_process_message` передаёт сообщение в E12, не запускает `_sessions` executor.
3. Личный контент только в личном чате; external update ID устойчив при redelivery.
4. Ответы и прогресс берутся из persisted events/outbox, не из closure старого bot.
5. Тесты: повтор update, restart bot, Bob чужой Telegram ID, групповое сообщение,
   перепривязка пользователя без переноса старой истории.

**Готово:** Telegram-работа продолжает жить после отключения bot; запрещённые
сообщения не создают WorkOrder. Не отправлять тестовые сообщения реальным людям.

### E16 — Telegram approval/handoff

**Статус:** TODO. **После:** E15.
**Файлы:** callback handlers Telegram, continuation service, тесты канала.

1. Callback ссылается на сохранённое решение и конкретный pending action;
   короткая opaque ссылка, не полные args/секреты в callback_data.
2. Перед потреблением заново проверить владельца, binding, актуальные права,
   точные args, срок и одноразовость. Нельзя доверять только user_id из callback.
3. Rejected/expired/consumed решения видны пользователю, не создают новый шаг.
4. Не делать Telegram endpoint сервисным обходом human-only решения.
5. Тесты: forwarded button, stale callback, double click, revoked binding,
   изменение args между показом и нажатием.

**Готово:** те же гарантии, что HTTP approval, без собственного второго runtime.

### E17 — Cron и внутренние фоновые поручения

**Статус:** TODO. **После:** E16.
**Файлы:** `tasks/agent_cron.py`, `test_agent_cron_dispatch.py`, общий intake.

1. Удалить активный headless AgentSession путь только после переключения на intake.
2. Идемпотентный occurrence key = schedule ID + запланированный момент, а не now.
3. У каждого расписания доказанный активный owner и явные полномочия; unknown owner
   блокирует запуск, не заменяется admin/service user.
4. Настройки не дают cron права согласовать external action самому себе.
5. Тесты: два beat, restart, просроченное расписание, revoked owner/grant,
   одинаковое расписание в следующую дату создаёт новую работу.

**Готово:** cron использует общий учёт и runtime, а не отдельный агент в памяти.

### E18 — Матрица каналов и паритет контрактов

**Статус:** TODO. **После:** E17. **Создать:** `backend/tests/test_agent_channel_parity.py`.

1. Один синтетический workflow запустить HTTP/Telegram/cron fixtures.
2. Сравнить owner, work identity, budgets, logical action, receipt, approval,
   cancellation, result status; различается только channel adapter.
3. Сбой канала уведомления не меняет domain result. Отзыв прав применяется ко всем.
4. Проверить, что модель не вызывается в intake/HTTP/Telegram обработчике.

**Готово:** нет активного параллельного agent lifecycle в переключённых каналах.
**Gate B1:** review прежде, чем отключать совместимость.

### E19 — Безопасный перенос архивного разговора

**Статус:** TODO. **После:** REVIEWED E18.
**Файлы:** ChatSession API/store, `api/chat_runs.py`, AssistantPanel.

1. Не возобновлять старый WS checkpoint. Предложить новый durable conversation
   с явно выбранным owner-readable контекстом архивного разговора.
2. Импортированная история — данные/provenance, не очередь tool calls и не approvals.
3. Source session сохраняется read-only. Повтор операции переноса идемпотентен.
4. Тесты: чужой чат, tool_call внутри архива, старые разрешения, revoked attachment,
   double import, отсутствие повторного исполнения архивных действий.

**Готово:** оператор может продолжить работу с контекстом без replay старой сессии.

### E20 — Вывод WS lifecycle из эксплуатации

**Статус:** TODO. **После:** E19.
**Читать:** `rg 'ws/chat|AgentSession\(' backend frontend aiagent` и найденных клиентов.

1. Составить список потребителей, feature flags и документации. Сначала проверить
   реальное использование endpoint, не считать отсутствие UI достаточным.
2. Перевести поддерживаемых клиентов; для остальных — явный deprecated/410 ответ.
3. Удалить исполнение из compatibility handler, не удалять пользовательскую историю.
4. Тесты: новый UI не открывает WS; старый вызов не запускает модель;
   reconnect/cancel/read-only history продолжают работать.

**Готово:** один runtime; не делать две системы «на всякий случай».

### E21 — Единый budget ledger

**Статус:** TODO. **После:** E20.
**Файлы:** WorkOrder budgets, `tasks/work_orders.py`, executor/provider adapters.

1. Сначала точные единицы: tool attempts, LLM calls, tokens, cost, active time,
   replan count. Ожидание человека не смешивать с активным временем без ADR.
2. Целевые defaults: 2 часа active, 200 tools, 50 LLM calls, 3 replans. Не включать
   новые replans для неизвестных эффектов только потому, что default теперь 3.
3. Резервировать лимит до вложенного вызова атомарно; charge после результата;
   crash/unknown не возвращает уже потраченный ресурс как «не было вызова».
4. Общий parent budget для child work. Resume/replan/channel switch не обнуляют его.
5. Тесты: два конкурентных последних токена/вызова, crash между reserve/charge,
   provider error, child fan-out, long approval wait, unset legacy budget migration.

**Готово:** все каналы имеют один ledger и объяснимый blocker; не выдумывать цены
провайдеров и не подставлять нулевую цену вместо неизвестной.

### E22 — Pause на безопасной границе

**Статус:** TODO. **После:** E21.
**Файлы:** WorkOrder state machine/API, durable worker/checkpoint, UI works.

1. Задать отдельный intent pause_requested и persisted safe-boundary acknowledgement.
2. Не считать отправленное внешнее действие отменённым. Остановить новые вызовы,
   закончить/сверить уже начатое и записать frontier.
3. Resume допускается только по валидному snapshot и свежим правам, без reset budget.
4. Тесты: pause до tool, во время read, во время write/unknown, два pause,
   cancel после pause, restart до acknowledgement.

**Готово:** UI различает «пауза запрошена» и «безопасно приостановлено».

### E23 — Replan без повтора завершённых эффектов

**Статус:** TODO. **После:** E22.
**Файлы:** `domain/work_orders.py`, `tasks/work_orders.py`,
`test_work_order_replanning.py`, `test_work_order_decompose.py`.

1. Новая ревизия связывает сохранённые результаты и критерии, не создаёт повтор
   старой записи/отправки под новым случайным logical ID.
2. Unknown frontier блокирует replan, пока не выбран безопасный E10-путь.
3. Старые workers/decisions/acceptance не применяются к новой ревизии автоматически.
4. Тесты: restart planner, duplicate instruction, limit=3, stale verdict,
   completed child artifact, changed objective с новыми критериями.

**Готово:** изменяется план будущего исполнения, а не переписывается история.

### E24 — Fencing вложенных инструментов и получателей

**Статус:** TODO. **После:** E23, E07.
**Файлы:** gateway, domain recipients из E03, вложенные worker/tool adapters.

1. Найти прямые обходы gateway и вложенные calls, которые не несут identity/fence.
2. Перед каждым эффектом проверить текущую попытку и полномочия на фактической
   границе; для DB — common lock/transaction, для внешнего сервиса — его контракт.
3. Child execution получает ограниченный context, не родительский admin token.
4. Тесты: stale worker после lease transfer, cancellation между подготовкой и
   отправкой, revoke grant, parent canceled при child pending.

**Готово:** перечислены доказанные и неподдержанные recipients; внешнюю отправку
нельзя отозвать задним числом декларацией «fencing есть в gateway».

### E25 — Аварийная приёмка runtime

**Статус:** TODO. **После:** E24.
**Создать:** изолированный crash harness в `backend/tests/` или `tests/integration/`
с отдельным Compose project и синтетическим recipient.

1. Поставить barriers: перед dispatch, после recipient commit, перед receipt read,
   перед checkpoint commit, после решения человека, перед notification ack.
2. Реально завершать тестовый worker/process, а не только бросать Exception.
3. Перезапускать и проверять таблицы, action IDs, счётчик эффектов и final status.
4. Повторить для HTTP, Telegram fixture и cron; закрыть UI во время исполнения.
5. Никаких kill/restart production worker ради теста без отдельного разрешения.

**Готово:** сохранён reproducible отчёт; unknown остаётся unknown, эффекты не
дублируются. **Gate B2:** review перед снятием ограничений durable-пилота.

## 9. Пакет C — одноразовый код, не генерируемые skills

### E26 — Контракт ScriptRun и threat model

**Статус:** TODO. **После:** REVIEWED E25.
**Читать:** AgentScriptRun, WorkArtifact, старый disabled runner, Compose.
**Создать:** `docs/agent-employee-delivery/script-isolation-contract.md`.

1. Указать вход: owner/work/action, code artifact hash, allowlisted input artifacts,
   runtime image digest, resource limits. Выход: status, bounded logs, artifacts.
2. Default: 1 CPU, 1 GB RAM, 64 PID, 512 MB tmp, 60 секунд; до 600 секунд только
   явное разрешённое увеличение внутри общего budget, не просьба кода.
3. Выбрать конкретный механизм ОС-изоляции и проверить доступность на хосте.
   AST/timeout/subprocess внутри backend не являются альтернативой sandbox.
4. Отдельный trusted supervisor; runner без секретов, сети, Docker socket,
   общей ФС приложения, root/capabilities. Рассмотреть rootless/cgroup ограничения.
5. Зафиксировать отказ при недоступной изоляции, а не fallback в backend.

**Готово:** ADR reviewed до реализации привилегированного supervisor.

### E27 — Изолированный supervisor

**Статус:** TODO. **После:** REVIEWED E26.
**Создать:** `infra/agent-script-supervisor/` и явно ограниченный API-клиент;
не расширять старый runner «разрешёнными» shell-командами.

1. Один запуск — отдельная изоляция, immutable image, read-only root, controlled tmp.
2. Никаких raw mount path/command/image от модели. Сервер выбирает runtime из allowlist.
3. Resource limits обеспечиваются ядром; kill уничтожает всю группу/контейнер.
4. Supervisor API аутентифицирован, owner/work/action-bound, не shell-as-a-service.
5. Тесты: отсутствующий runtime, неподдержанные cgroups, supervisor restart,
   несовпадение digest, чужой run ID.

**Готово:** сервис отдельно проверен, но вызов из агента ещё выключен.

### E28 — Брокер входных и выходных артефактов

**Статус:** TODO. **После:** E27.
**Файлы:** artifact storage API/domain; новый script broker по контракту E26.

1. Входы материализуются только из owner-readable immutable artifact versions.
2. Модель не задаёт host path. Проверять canonical path, symlink/hardlink,
   traversal, archive extraction, число файлов и суммарный объём.
3. Выходы сначала в quarantine, затем digest/size/MIME validation и регистрация;
   исполняемый файл не становится зарегистрированным tool.
4. Тесты: `../`, абсолютный путь, symlink наружу, zip bomb, много маленьких файлов,
   подмена input после принятия работы, Bob artifact.

**Готово:** ноль чтений за пределами объявленных входов; нет перезаписи артефакта.

### E29 — Жизненный цикл ScriptRun

**Статус:** TODO. **После:** E28.
**Файлы:** AgentScriptRun, WorkOrder worker, ToolResult, broker/supervisor.

1. State transitions queued/running/succeeded/failed/timed_out/canceled/unknown
   описать отдельно от domain-success задачи пользователя.
2. Persist start intent до запуска; logical key и receipt предотвращают дубль job.
3. После crash supervisor сверяет фактический job, не запускает код заново вслепую.
4. stdout/stderr ограничены по байтам; ошибки не раскрывают host paths/secrets.
5. Cancel снимает новые действия и завершает все descendants; outputs публикуются
   только из подтверждённого run и под текущими правами.

**Готово:** тестовые scripts работают через общий budget/journal, без catalog promotion.

### E30 — Adversarial sandbox suite

**Статус:** TODO. **После:** E29. **Создать:** отдельный security integration набор.

1. Проверить бесконечный цикл/import, fork bomb, orphan process, disk fill,
   огромный stdout, сигнал, crash runtime, timeout при зависшем child.
2. Проверить IPv4/IPv6/DNS/raw socket, host metadata endpoint, чтение `.env`,
   `/proc` чужого процесса, Docker socket, symlink race, архивный выход из каталога.
3. Ожидание: эффект предотвращён ядром, лимит соблюдён, соседний run не затронут.
4. Недоступный sandbox даёт явный blocked, никогда выполнение в backend.

**Gate C:** независимый review и проверка реальной ОС-изоляции до включения tool.
Наличие unit mock supervisor не закрывает эту карточку.

## 10. Пакет D — браузерный сотрудник

### E31 — Владелец браузерной сессии и ресурсная модель

**Статус:** TODO. **После:** E30.
**Файлы:** `api/computer_use.py`, `infra/web-browser/server.py`,
`test_computer_use_grants.py`; создать schema/migration сессий при необходимости.

1. session/tab/download принадлежит owner/work; новый пользователь не получает
   прежний context/cookie jar. Session ID сам по себе не даёт полномочий.
2. Определить TTL, завершение, лимит вкладок, lease и связь с cancel/budget.
3. Проверить каждый browser endpoint, не только создание сессии.
4. Тесты: Alice/Bob по известному ID, expired session, restart, revoke owner,
   одновременное закрытие и действие.

**Готово:** ни cookie, ни screenshot, ни DOM чужой сессии не читаются.

### E32 — DOM/accessibility и адресуемые действия

**Статус:** TODO. **После:** E31.
**Файлы:** browser service/API, явный catalog contract.

1. Снимок страницы содержит ограниченное accessibility/DOM представление с revision.
2. Действие указывает tab/snapshot/element reference; stale revision → conflict,
   не координатный fallback в неизвестное место.
3. Tabs, navigate, read, fill, click имеют явные аргументы, результаты и эффекты.
4. Текст страницы — недоверенные данные; он не меняет owner, policy или system prompt.
5. Тесты на локальном синтетическом сайте: SPA rerender, исчезнувший элемент,
   iframe, новая вкладка, disabled element, prompt injection в DOM.

**Готово:** воспроизводимые действия без keyword-generated сценариев.

### E33 — SSRF и сетевой выход браузера

**Статус:** TODO. **После:** E32.
**Файлы:** browser service, networking/Compose; создать network security тесты.

1. Проверять не только стартовый URL, но redirects, subresources, XHR/fetch,
   WebSocket, service workers, downloads и DNS resolution.
2. Блокировать loopback/link-local/private ranges IPv4/IPv6 по явной политике;
   разрешённые внутренние сайты — отдельные scoped grants, не глобальный bypass.
3. Учитывать DNS rebinding, alternate IP notation, userinfo, schemes, proxy bypass.
4. Там, где application hooks недостаточны, enforce egress на proxy/network layer.
5. Тестовый web server пытается выйти в запрещённые сети разными способами;
   assert отсутствия соединения, а не только текста отказа в API.

**Gate D1:** review до подключения браузера к произвольным внешним сайтам.

### E34 — Login/MFA/CAPTCHA и брокер секретов

**Статус:** TODO. **После:** REVIEWED E33.
**Файлы:** browser session API/service, secret storage integration, UI handoff.

1. Предпочтительно human handoff в owner session. CAPTCHA не обходить автоматически.
2. Если нужен broker, LLM передаёт opaque secret reference, а значение получает
   только доверенный компонент для конкретного origin/form в разрешённой сессии.
3. Не возвращать password/token в DOM snapshot, screenshot, events, receipts,
   exception, trace или model context. Редактирование логов — не единственная защита.
4. Разрешение имеет срок, owner/origin binding, одноразовость, отзыв.
5. Тесты: форма сменила origin, скрытая копия поля, redirect после login,
   утечка через DOM/console/ошибку, другой пользователь, повторный secret use.

**Gate D2:** review до настоящих учётных записей; тестовые credentials не секреты prod.

### E35 — Файлы браузера через artifact broker

**Статус:** TODO. **После:** E34, E28.

1. Upload только конкретной разрешённой версии артефакта, не arbitrary host path.
2. Download — ограничение размера/числа, quarantine, digest, MIME и provenance.
3. Имя из Content-Disposition не становится локальным путём. Запретить traversal,
   symlink, zip bomb; обработка потенциально опасного файла не запускает его.
4. Тесты: revoked input, чужой download ID, redirect на private address,
   бесконечная загрузка, дубликат, отмена посреди передачи.

**Готово:** artifact ACL сохранён от источника до выдачи пользователю.

### E36 — Подтверждение браузерных действий с эффектом

**Статус:** TODO. **После:** E35.

1. Формы отправки/публикации/заказа получают явную action card: origin, адресат,
   нормализованные значения, версия страницы и hash.
2. После одобрения заново проверить форму. Изменённые значения/origin/snapshot
   требуют нового решения, а не best-effort click.
3. Read-only navigation не выдаёт полномочий на submit. DOM-инструкция «нажми
   approve» не является сообщением владельца.
4. Timeout после submit — unknown + ограниченная read-only сверка; не второй click.
5. Тест: synthetic form increment; потерян ответ → счётчик 1, blocked; изменённая
   форма → 0. Решение нельзя применить в другой вкладке/работе.

**Готово:** разрешённость и эффект проверены раздельно; общего exactly-once нет.

### E37 — Браузерная аварийная приёмка

**Статус:** TODO. **После:** E36.

1. Локальный сайт: таблицы/поиск, многошаговая форма, login handoff, upload/download,
   смена вкладок, SPA, ошибка сервера, медленный ответ, session expiry.
2. Реальный browser process crash до/после submit; restart worker; UI disconnect.
3. Проверить действия и серверный журнал сайта, а не только красивый screenshot.
4. Prompt injection просит секреты/права/чужие файлы: ноль фактических утечек/записей.

**Gate D3:** review перед ограниченным живым пилотом на разрешённых test accounts.

## 11. Пакет E — знания и артефакты без межпользовательских утечек

### E38 — Единый ACL contract

**Статус:** TODO. **После:** E37.
**Читать:** memory API/manager/builder, models MemoryFact/KnowledgeNode/KnowledgeEdge,
DocumentChunk, embeddings, documents/mail/artifacts и существующие scope tests.

1. Таблица scope: owner, session, department, project, explicitly shared, global.
   Для каждого — источник права, отзыв, derived data, разрешённость агентского чтения.
2. `global` не значит «любой текст, который модель назвала общим».
3. Производный объект не может иметь более широкий доступ, чем его источники.
   Несколько источников — безопасное пересечение или раздельные owner projections.
4. Unknown provenance/owner → quarantine, а не автоматическое присвоение admin.
5. Описать единую policy-функцию и точки enforcement: SQL, graph, vector, cache,
   download, background builder, reindex, delete.

**Готово:** ADR + Alice/Bob/department/project матрица. **Gate E1:** review до backfill.

### E39 — SQL и кэши

**Статус:** TODO. **После:** REVIEWED E38.
**Файлы:** `api/memory.py`, `ai/memory_manager.py`, реальные callers, scope tests.

1. Вынести согласованные SQL predicates без изменения смысла доменных прав.
2. Применить до выдачи результатов, snippets, counts, pagination, identifiers.
3. Cache key включает owner/ACL epoch или права перепроверяются перед выдачей;
   отзыв не ждёт бесконечного старого кеша.
4. Тесты: чужие counts/snippets, повтор cache lookup другим user, scope injection,
   архивная сессия, inactive user, revoked document.

**Готово:** нельзя утечь даже через метаданные; поиск остаётся поиском, не replay.

### E40 — Графовые обходы

**Статус:** TODO. **После:** E39.
**Файлы:** `domain/memory_builder.py`, `tasks/graph_memory.py`, `test_graph_memory.py`.

1. Проверять доступ к стартовому узлу, каждому ребру, соседу и evidence source.
2. Общий entity name не объединяет частные факты двух владельцев в общедоступный узел.
3. Background build сохраняет provenance и scope; service identity не значит global.
4. Тесты: Alice → shared node → Bob secret; недоступный evidence span;
   удалённый источник; cross-department aggregation.

**Готово:** путь через разрешённый узел не обходит ACL закрытого соседа.

### E41 — Vector retrieval и reindex

**Статус:** TODO. **После:** E40.
**Файлы:** embeddings modules/tasks/backfill; реальные vector client callers.

1. Данные индекса имеют owner/provenance/version payload; фильтр на стороне поиска
   плюс авторитетная перепроверка найденных IDs перед текстом/reranking.
2. Запрещённый текст не передавать reranker/LLM «для последующей фильтрации».
3. Старые записи без ACL quarantine; новая запись и смена прав имеют надёжный
   reindex/outbox, а чтение fail-closed пока индекс отстаёт.
4. Тесты: foreign hit первым в top-k, stale ACL payload, revoked source, повтор
   backfill, смена embedding profile без потери owner.

**Готово:** метрика recall не оправдывает утечку; reindex не делает legacy global.

### E42 — Неизменяемые версии блоков и артефактов

**Статус:** TODO. **После:** E41.
**Файлы:** OwnedWorkspaceBlock, WorkArtifact, API blocks/artifacts, миграции.

1. Новая версия вместо перезаписи; expected_revision предотвращает lost update.
2. Проверки и approvals привязаны к immutable version/hash, не только artifact ID.
3. Явный share/revoke с audit, owner и областью; агент не расширяет sharing сам.
4. Тесты: concurrent edit, stale approval, old version lookup, revoke share,
   artifact replacement после verifier, duplicate create.

**Готово:** UI/worker/verifier видят одну конкретную версию и её ACL.

### E43 — Перенос старой общей памяти

**Статус:** TODO. **После:** E42.
**Создать:** dry-run migration tool + отчёт по категориям provenance.

1. Инвентаризация без удаления: доказанный owner, доказанный shared, ambiguous.
2. Для каждого переноса сохранить source ID/hash и правило определения владельца.
3. Ambiguous не присваивать по последнему читателю/ближайшему имени/LLM guess;
   оставить quarantine и запросить решение владельца данных.
4. Идемпотентность, dry-run, batch limits, resume cursor, backup/restore rehearsal.
5. Перенос production данных — отдельное разрешение и reviewed отчёт, не обычный
   запуск после unit-теста. Старую общую Redis-память не удалять по wildcard.

**Готово:** перенесены только доказанные категории; остальные явно перечислены.

### E44 — Удаление и отзыв во всех производных хранилищах

**Статус:** TODO. **После:** E43.

1. Tombstone/revoke авторитетного источника немедленно запрещает чтение.
2. Outbox удаляет/обновляет derived SQL, graph, vector, caches, artifacts по policy.
3. Повтор job безопасен; partial cleanup наблюдаем, не восстанавливает доступ.
4. Audit содержит минимальные метаданные, не копию удаляемого секретного текста.
5. Тесты: worker down при отзыве, stale cache/vector, double delete,
   restore старого backup не возвращает отозванные права незаметно.

**Gate E2:** сквозная Alice/Bob приёмка до общего включения knowledge retrieval.

## 12. Пакет F — понятный рабочий интерфейс и эксплуатация

### E45 — Единая страница работы

**Статус:** TODO. **После:** E44.
**Файлы:** `frontend/app/work-orders/page.tsx`, chat journal, соответствующие APIs.

1. Показать objective, текущую ревизию/шаг, channel, owner, budgets, blocker,
   результаты, provenance, receipts, verification и историю решений.
2. Разные labels для running/pause_requested/paused/waiting/blocked/failed/completed.
   `chat.done` не даёт completed до приёмки WorkOrder.
3. Кнопки зависят от серверного контракта доступных переходов, не только local state.
4. Cursor/reload и late response не дублируют историю или решения.
5. Accessibility/keyboard, длинные тексты, мобильная ширина, empty/error states.

**Готово:** один экран позволяет понять, что сделано, что неизвестно и кто должен решить.

### E46 — Конструктор постоянных разрешений

**Статус:** TODO. **После:** E45.
**Файлы:** `frontend/app/settings/delegations/page.tsx`, catalog/schema, delegation API.

1. Вместо свободного JSON — явная операция и типизированные ограничения:
   адресат/объект/проект/предел суммы, срок, число попыток — только поддержанные поля.
2. UI показывает область разрешения человеческим текстом и точный итоговый JSON.
3. Сервер повторно валидирует; пустой/wildcard scope не становится all-access.
4. Admin/execute/unknown операции остаются неделегируемыми согласно policy.
5. Тесты: несовместимый тип, неизвестное поле, истёкший срок, revoke vs dispatch,
   две конкурентные последние попытки, чужое разрешение.

**Готово:** удобство UI не расширило существующий безопасный контракт.

### E47 — Расходы и эксплуатационные сигналы

**Статус:** TODO. **После:** E46, E21.
**Файлы:** budget ledger, provider adapters, works UI, metrics endpoints.

1. Usage по provider/model/request/attempt, включая failed/retried requests.
2. Цена — версионированный тариф с источником/датой, либо unknown. Ноль только
   когда действительно известно отсутствие денежной цены, не отсутствие данных.
3. Метрики: queue age, unknown actions, awaiting human, outbox lag/dead-letter,
   expired leases, verification failures, budget blocks.
4. Логи без prompt/secrets по умолчанию; correlation IDs вместо дампов контекста.
5. Тесты: duplicate usage event, streaming interruption, provider no-usage,
   child cost aggregation, price version change.

**Готово:** затраты и зависания можно объяснить по журналу, не предположением модели.

### E48 — Удаление retired и эвристического мусора

**Статус:** TODO. **После:** E47.
**Читать:** manifest/catalog/settings/UI, `capability_builder.py`,
`capability_sandbox.py`, архивные recipe/skill helpers, все callers через `rg`.

1. Таблица: активно используется / deprecated API / архив / можно удалить.
2. Удалить UI-переключатели, обещающие generated skills, shadow promotion,
   keyword approval или отключённый runner; сохранить объяснение миграции.
3. Код удалять только после проверки imports, runtime registrations, DB consumers,
   документации и контрактов старых клиентов.
4. Историю и пользовательские данные не удалять вместе с Python-модулем.
5. Тесты: retired API явно отказывает; новый runtime не импортирует удалённое;
   policy/digest/ACL checks остались; feature unavailable не вызывает fallback.

**Готово:** меньше поверхностей, без потери защитных детерминированных правил.

## 13. Пакет G — доказать пригодность сотрудника

### E49 — Схема приёмочных заданий и runner

**Статус:** TODO. **После:** E48.
**Читать:** существующие `ai/evals/`; создать отдельный employee corpus, не смешивать
его показатели с CAD pixel/geometry метриками или тестами роли.

1. Case: ID/group, начальное состояние, owner/roles/grants, задача, fixtures,
   разрешённые эффекты, acceptance predicates, forbidden effects, budgets.
2. Предикаты проверяют фактические DB/recipient/artifact результаты, а не текст ответа.
3. Runner создаёт изолированный мир, вызывает реальный intake/runtime, сохраняет
   model/config/code/fixture versions, trace и machine-readable результат.
4. Cleanup только своих fixture IDs. Повтор запуска не наследует успешный артефакт.
5. Dry-run с fake LLM проверяет harness, но отдельно маркируется и не входит в
   показатель производительности живой модели.

**Готово:** reproducible runner + два демонстрационных случая, pass и fail.

### E50 — 120 синтетических задач

**Статус:** TODO. **После:** E49.

1. Пять групп по 24: браузер, файлы, данные, коммуникации, координация.
2. В каждой группе 8 простых, 8 многошаговых, 8 с recovery/неполным контекстом.
3. Для каждого case до запуска определены правильный результат, допустимые
   уточнения/handoff, forbidden effects и максимальный budget.
4. Коммуникации — тестовые получатели/серверы, не реальные клиентские адреса.
5. Кодовые fixtures и expected outputs проходят review: не подгонять критерии
   после того, как увидели слабый результат модели.
6. Для нового типа задач делать сначала 2 проверенных примера, затем оставшиеся
   22 в отдельных небольших commits; не генерировать 120 пустых YAML ради количества.

**Готово:** все 120 случаев исполнимы и содержат независимую проверку.

### E51 — Отдельный security/chaos корпус

**Статус:** TODO. **После:** E50; использовать сценарии E25/E30/E37/E44.

1. Prompt injection в DOM, файле, письме, retrieved memory, tool error.
2. Чужие scopes/IDs, revoked role/grant/session, stale approval, replay callback.
3. Duplicate delivery, kill после effect, stale worker, dropped HTTP response,
   restart browser/supervisor, недоступные DB/Redis/provider/recipient.
4. Запрос «получи больше прав», «отправь секрет», «создай постоянный skill».
5. Для каждого атака имеет наблюдаемый forbidden effect counter; отказ в тексте
   при фактической записи считается провалом, а не защитой.

**Готово:** безопасность оценивается отдельно от полезности и не усредняется с ней.

### E52 — Три прогона, отчёт и ограниченный выпуск

**Статус:** TODO. **После:** E51 и review gates A–F.

1. До платных/live запусков согласовать модель/маршрут, разрешённые назначения,
   максимальные расходы и лимит тестовой среды. Не переносить реальные документы.
2. По три независимых запуска каждого case: 360 измерений; сохранить неудачи,
   версии и consumed budgets. Не выбирать «лучший из трёх» как итог кейса.
3. Зафиксировать формулу до запуска: run-level success = passed/360;
   по группе = passed/72. Дополнительно case robustness = cases с 3/3 успехами /120.
4. Цели исходного плана: ≥90% run-level в целом, ≥85% в каждой группе;
   ноль неразрешённых внешних действий и межпользовательских утечек. В отчёте
   отдельно показать robustness и объём проверенного security corpus.
5. Любая утечка/unauthorized effect блокирует выпуск независимо от среднего score.
   Ноль найденных утечек в корпусе не объявлять математическим доказательством
   безопасности всех будущих задач.
6. Независимый review проверяет traces и случайную выборку успешных кейсов;
   модель-исполнитель не утверждает сама свой выпуск.
7. После приёмки — ограниченный rollout с наблюдением и kill switch; rollback
   прекращает новые эффекты, не пытается «отменить» уже отправленное письмо.

**Готово:** опубликован отчёт с фактическими цифрами, ограничениями и решением
review. Только здесь разрешена формулировка «план выполнен» в рамках принятого scope.

## 14. Где обязательно нужна сильная проверка

Недорогая модель может писать код и тесты этих карточек, но не должна сама
объявлять архитектурные/security gates пройденными:

- E07–E11: дедупликация, fencing и продолжение после неизвестного исхода.
- E13–E18, E21–E25: outbox, channel identity, общие budgets и аварийный runtime.
- E26–E30: привилегированный supervisor и обеспеченная ядром изоляция.
- E33–E36: egress, секреты и внешние действия браузера.
- E38–E44: производные ACL, миграция старой памяти и отзыв доступа.
- E49–E52: независимость predicates, статистика и решение о выпуске.

Сильный review не означает переписывание всего. Передавать небольшую серию
проверенных commits, diff, reproducer и отчёты. Если review недоступен, оставить
рискованную новую возможность выключенной и выполнять следующие независимые
docs/tests-карточки, а не обходить gate.

## 15. Правила актуализации этого плана

1. Карточка остаётся TODO/IN_PROGRESS, пока нет ссылок на код и проверки.
2. После каждого пакета обновлять короткий фактический срез в
   `AGENT_EMPLOYEE_IMPLEMENTATION_PLAN.md`; сохранять ссылки в `CLAUDE.md`,
   `PLAN.md`, `DEVPLAN.md`, `AGENTS.md`. Не дублировать все карточки в пяти файлах.
3. При новой находке добавить E__.N с причиной и зависимостями; не переписывать
   завершённую историю так, будто проверка всегда была успешной.
4. Не обещать, что модель конкретного класса обеспечит нужные проценты.
   Этот план уменьшает размер решений исполнителя; пригодность продукта измеряет E52.
5. Оптимизация стоимости разработки: короткий контекст, reuse fixtures, точечный
   тест сначала, общая регрессия на границе пакета, review маленьких diff.
   Оптимизация не отменяет обязательную production-проверку кодовой выкладки.
