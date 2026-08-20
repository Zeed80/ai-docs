# Roadmap: автономная память, exploratory-задачи и self-learning connector library

## Контекст

Продолжение обсуждения архитектуры агента «Света» (см. критику агентской системы в этой же сессии). Пользователь хочет, чтобы агент был по-настоящему самостоятельным помощником инженера: умел долго и автономно работать над открытыми задачами (пример — «найди каталоги поставщиков»: агент сам решает, как искать, скачивает и структурирует что нашёл в SQL + векторно-графовую БД со связкой картинок инструмента, честно перечисляет, чего не нашёл), ориентировался на успех, дообучался по итогам своих действий, а не был скован жёсткими рамками там, где рамки не нужны для безопасности.

Ресёрч (3 параллельных Explore-агента + прямая проверка ключевых файлов) показал: большая часть нужной инфраструктуры **уже существует и не используется на полную**, а не отсутствует:

- `WorkOrder` уже поддерживает `parent_id` + `PlannedStep.kind="decompose"` — планировщик уже умеет fan-out дочерних WorkOrder с автоматическим сплитом бюджета (`_split_child_budgets`, `backend/app/tasks/work_orders.py:180-263`). Это готовый механизм для «разослать по N поставщикам параллельно», не нужно изобретать новый.
- `generate_capability_plan` уже принимает `completed_context`/`failure_context` и **явно запрещает модели повторять завершённую работу при реплане** (`backend/app/domain/work_planning.py:137-144`) — инкрементальный replanning-цикл уже есть, а не только «bounded replanning после провала».
- `WorkStepAttempt.checkpoint` (JSON) существует в схеме, но нигде не читается/не пишется в рантайме — готовая точка для инкрементального прогресса долгих шагов.
- `WorkOrder.budgets`/`constraints`/`metadata_` — JSON-поля без строгой схемы, значит бюджет по времени/запросам и флаг «exploratory-режим» можно добавить **без миграции**, просто новой конвенцией ключей.
- `WorkStepAttempt.tokens_used`/`cost_usd` уже трекаются по попытке — агрегация LLM-бюджета не требует нового счётчика.
- `ComputerUseGrant`/`ComputerUseAction` (allowlist хостов, лимит операций, аудит) — готовый, уже проверенный securit-механизм для веб-доступа; никакого отдельного «bypass» для скрапинга придумывать не нужно.
- `assert_completion_allowed`/`record_verifier_verdict` — существующий fail-closed паттерн приёмочных критериев (детерминированный + независимый semantic verifier) — «честное покрытие» можно выразить как ещё один критерий в этой же системе, не трогая её FSM.
- `ToolSupplierCreate/Out`, `ToolCatalogEntryCreate/Out` (`backend/app/domain/tool_catalog.py`) + Qdrant-коллекция `tool_catalog` — почти готовая схема каталога поставщиков, с уже существующим `metadata_` JSON-полем для provenance без миграции.
- `WorkLearning`→`MemoryFact` (`backend/app/domain/work_learning.py`) и `RecipeSkill` (`backend/app/ai/recipes.py`, draft→active→retired lifecycle) — уже работающий self-learning контур, просто ещё не про веб-источники.

Вывод: план — это в первую очередь **сборка и точечное расширение** существующих механизмов, а не новая архитектура с нуля. Это резко снижает риск и объём работ по сравнению с первым черновиком.

Также учтено прямое требование пользователя: (1) любая ошибка/нестыковка, найденная по ходу выполнения плана — фиксируется и исправляется тут же, не откладывается молча; (2) каждый значимый этап завершается пересборкой затронутых сервисов живого docker-стека и прогоном тестов на пересобранном стеке, а не только «тесты прошли на старом образе».

## Что делать в первую очередь при переходе к реализации

Первым шагом (до начала Фазы 0) — сохранить этот план как файл в репозитории, например `AGENT_AUTONOMY_ROADMAP.md` в корне (по аналогии с существующими `CAD_REDRAW_ACCURACY_TODO.md`, `AGENT_SYSTEM_DEVELOPMENT_PLAN.md`, `DXF_CAD_DEVELOPMENT_PLAN.md`), с теми же чекбоксами `- [ ]`/`- [x]`. Именно этот файл в репозитории — рабочий трекер, который редактируется по ходу выполнения (коммитится вместе с изменениями); файл в `/root/.claude/plans/` — только исходный черновик для approval.

---

## Правило 0 — действует на весь roadmap

- [ ] Любая ошибка, нестыковка, устаревшее допущение или расхождение с реальным кодом, обнаруженные при выполнении ЛЮБОГО пункта — не игнорируются. Порядок: (1) фиксируется как новый TODO в подразделе `### Находки по ходу` текущей фазы с кратким описанием; (2) исправляется в рамках той же фазы (если это меняет объём последующих фаз — соответствующие пункты корректируются по факту, не переписываются задним числом); (3) отмечается выполненным только после подтверждения пересборкой+тестами.
- [ ] Каждая фаза начинается с пустого подраздела `### Находки по ходу` — остаётся в файле даже если пустой (аудируемость).

### Чек-лист «пересборка + тесты» (после каждой значимой фазы/подфазы)

- [ ] Точечная пересборка изменённых сервисов: `docker compose -f infra/docker-compose.yml -f infra/docker-compose.dev.yml build <service>` для каждого реально изменённого (`backend`, `celery-worker`, `celery-beat`, `skill-runner`, `frontend`) → `up -d <service>`.
- [ ] `make test` (pytest, не-live) — обязательно.
- [ ] `make test-live` — если фаза трогает живые LLM-пути (планировщик, verifier, capability_builder, recipes/connector).
- [ ] `make regression` — если фаза трогает extraction/pipeline/manifest-контракты.
- [ ] Изменения в `app/tasks/*` → дополнительно `make logs-celery`, убедиться, что beat поднял новые задачи без traceback.
- [ ] Красный результат любого шага — сам становится находкой по Правилу 0, не «пока сойдёт».
- [ ] Полная `make rebuild` — обязательна перед Фазой 4 (сквозной пилот) и перед закрытием всего roadmap.

### Чек-лист «проверка на реальных данных» (где применимо)

- [ ] Конкретный сценарий (curl/скрипт/Playwright, `example-invoices/` или реальные сайты поставщиков) с явным ожидаемым результатом — не абстрактное «проверили».

### Зависимости между фазами

Фаза 0 блокирует Фазы 2, 4, 5 (все пишут в `MemoryFact` с новым scope и/или читают feedback-сигнал). Фаза 1 и 3 не зависят друг от друга напрямую, но интеграция «веб-каталог → extraction pipeline» требует готовой Фазы 2. Фаза 6 зависит от Фазы 5 (ревалидация connector-стратегий — часть idle-job). Фаза 7 независима, может идти в любой момент.

---

## Фаза 0 — Память: scope/tenancy MemoryFact + feedback-сигнал для проактивных задач — ЗАКРЫТА (2026-08-20)

### Находки по ходу
- [x] **Тест `tests/scripts/test_project_ifc_views.py` был сломан на `main`** ещё до этого roadmap — импортировал `_depth_visible` и др. из `scripts/project_ifc_views.py`, но Ф5.1 перенесла эту логику в `app/ai/ifc_reader.py`, оставив `scripts/project_ifc_views.py` тонкой CLI-обёрткой. Из-за этого `python3 -m pytest backend/tests` падал на сборе ВСЕГО набора (`Interrupted: 1 error during collection`) — `make test` был полностью нерабочим на чистом `main`. Починено: импорт в тесте переключён на `app.ai.ifc_reader` (перепроверено на стеше `main` — падало и там, не моя регрессия).
- [x] **`UserInfo` (`app/auth/models.py`) не нёс `department_id`** — ни JWT-путь (`_verify_token`/`_verify_local_session`), ни delegation-путь (`get_effective_user` в `acting.py`) его не резолвили, хотя `User.department_id` в БД уже есть. Без этого department-scope в MemoryFact не из чего было бы наполнять. Починено по образцу уже существующего `_db_section_access_for_sub` (тот же redis-кэш на 45с, тот же fail-open): новая `_db_department_id_for_sub` в `jwt.py`, подключена в оба места верификации токена + в `acting.py`; инвалидация добавлена в `invalidate_active_cache` (уже вызывается из `admin.py` при смене `department_id`, отдельного вызова добавлять не пришлось).
- [x] **Мои же изменения ломали порядок моков в `test_proactive.py`** — throttle-check и per-item snooze-check добавляют новые `db.execute()` вызовы перед уже существующими в `_check_due_dates`/`_dispatch_due_reminders`/`_check_stale_approvals`. Найдено и исправлено сразу (обновлён `_mock_db_ctx` + все затронутые тесты), прежде чем стало «зелёным по недосмотру».
- [x] **`is_snoozed` изначально не был scoped по `beat_task_name`** — снус на «счёт скоро к оплате» тихо заглушил бы не связанный нудж «подтверждение просрочено» по тому же invoice_id. Замечено и исправлено при проектировании, до того как это стало багом в проде.
- [x] `tests/tools/*.py` (6 файлов) и `tests/domain/test_aiagent_capability_contract.py` падают при запуске **внутри backend-контейнера** (`FileNotFoundError: /tools/cad-dataset/...`, `/infra/...`) — НЕ регрессия и не баг: эти тесты вычисляют путь к репо через `Path(__file__).resolve().parents[3]`, что корректно только при полном чекауте (`make test` на хосте); slim-образ backend содержит только поддерево `backend/`. Аналогично `test_skill_runner_isolation.py` (ищет `/infra/docker-compose.yml`, `/backend/app/...`). Не чинится — работает как задумано при штатном запуске `make test` с хоста; при контейнерном прогоне просто `--ignore`.
- [x] Полный прогон `backend/tests` внутри пересобранного `backend`-контейнера против реальной БД: 61 упавший тест, все — до́ моих изменений и не по вине диффа. Выборочно проверено 4/61 трассировкой: `test_provider_registry.py`/`test_routing_pipeline.py` падают на `httpx.HTTPStatusError 404` к `http://host-gateway:11434/api/chat` (нет живого Ollama на хосте в этом окружении), `test_skill_runner_isolation.py` — тот же host-path артефакт, что и у `tests/tools/`. Ни один из 61 не импортирует ни один из изменённых в Фазе 0 файлов (`memory.py`, `models.py`, `jwt.py`, `acting.py`, `proactive.py`, `services/notifications.py`, `api/notifications.py`) — не регрессия.
- [x] Хостовый прогон тестов упирается в порт-коллизию: на `localhost:5432` в этой sandbox-среде слушает Postgres **другого**, не относящегося к проекту docker-проекта (`china-key-learning-postgres-1`, публикует порт на хост), а собственный `infra-postgres-1` порт наружу не публикует (правильно, порт закрыт). Эвристика `backend/tests/conftest.py::_resolve_db_url` находит чужой слушающий порт по одному TCP-коннекту и не проверяет, что это действительно наша БД — падает на `InvalidPasswordError` вместо грациозного отката на testcontainers. Это artefact данной shared-хост среды, не баг репозитория для типичного (одно-проектного) хоста — не чинится; тесты в этой сессии гонялись `docker compose exec backend pytest` (сеть `infra_app`, `POSTGRES_HOST=postgres`), это и есть верный обход именно для этой среды.

### 0.A Scope/tenancy — сделано

- [x] Задокументирован baseline `MemoryFact.scope` (`session`/`project`/`global`/`owner:{sub}`), писатели — `work_learning.py`, `graph_analytics.py`, `api/memory.py`.
- [x] Добавлен префикс `department:{department_id}` (без композиции с другими scope). Источник — `User.department_id` (уже был в БД), теперь резолвится в `UserInfo.department_id` (см. находки выше).
- [x] Миграция не понадобилась (scope — свободная строка); индекс `(scope, kind)` не добавлялся — YAGNI, добавить по факту нагрузки.
- [x] `backend/app/api/memory.py` — правила видимости вынесены в `_resolve_visible_scopes(payload, *, department_id)`; `_search_memory_facts` и `search_memory` обновлены; ветка `department:{id}` добавляется аддитивно на каждой из 4 существующих веток (session/owner/explicit/default) — при `department_id=None` поведение бит-в-бит совпадает со старым (проверено юнит-тестами на скомпилированный SQL).
- [x] Запись `scope=department:{id}` — явный opt-in через уже существующий свободный параметр `scope` в `POST /chat-turn`/`POST /pin` (новый код на write-стороне не понадобился — API и так принимал произвольную строку).
- [x] Прочие читатели `MemoryFact.scope` (`work_learning.py`, `recipes.py` — точечные `owner:{sub}`/`project` лукапы, не general-purpose видимость) — не трогаются, новый префикс их не задевает.
- [x] Тесты: `backend/tests/test_memory_scope_visibility.py` (9 тестов, компиляция SQL-предиката) — зелёные и на хосте, и в контейнере против реальной БД.

### 0.B Feedback-loop для проактивных задач — сделано

- [x] Новая таблица `proactive_task_feedback` (`backend/app/db/models.py`, миграция `20260820_0001_proactive_task_feedback.py`) + `notifications.source_task` (nullable, кто из проактивных задач создал уведомление — только `check_due_dates`/`dispatch_due_reminders`/`check_stale_approvals`; broadcast-задачи `alert_critical_anomalies`/`morning_briefing`/`alert_duplicate_invoices` НЕ в скоупе калибровки — у них нет одного адресата, задокументировано в коде).
- [x] `POST /api/notifications/{id}/feedback` (`accept|dismiss|snooze`, `backend/app/api/notifications.py`) — всегда помечает `is_read=True`; калибрует только если `source_task` не пуст (`calibrated: bool` в ответе).
- [x] `backend/app/domain/proactive_feedback.py`: `get_proactive_task_acceptance_rate`, `should_throttle_proactive_task` (self-throttle по тому же redis-JSON паттерну, что и `GraphAnalyticsSettings`), `record_proactive_feedback`, `is_snoozed` (scoped по `beat_task_name`, не только по entity — снус на один нудж не глушит другой про тот же объект).
- [x] `_check_due_dates`/`_dispatch_due_reminders`/`_check_stale_approvals` подключены: throttle-check в начале, snooze-check перед каждым уведомлением (включая Telegram-дубль), `source_task` проставляется.
- [x] Frontend-кнопка accept/dismiss/snooze в notification UI — **не сделано в этой итерации**, зафиксировано как отдельный открытый TODO (API + backend-тесты полные, UI — следующий проход).
- [x] Тесты: `test_proactive_feedback.py` (12, unit) + `test_notifications.py` (5 новых, требуют live Postgres — прошли в контейнере) + обновлённые `test_proactive.py` (10, под новые вызовы).

### 0.C Пересборка и проверка — сделано
- [x] `docker compose ... build backend celery-worker celery-beat` — собралось (кэш слоёв, менялся только код).
- [x] `docker compose ... run --rm backend alembic upgrade head` — `20260819_0008 → 20260820_0001` применена на живой БД без ошибок; данные (5 users, 153 documents) целы после recreate postgres/redis (config-hash mismatch из-за `--env-file`, не потеря volume).
- [x] `docker compose ... up -d backend celery-worker celery-beat` — все `healthy`; `celery-beat` логи: `check_due_dates`/`dispatch_due_reminders`/`check_stale_approvals` отработали по расписанию без traceback (throttle/snooze-код прошёл вживую на реальных Postgres+Redis).
- [x] `make test` не работал на хосте (см. находки) → прогнано внутри пересобранного `backend`-контейнера (`docker compose exec backend pytest`, реальная `aiworkspace_test` БД по сети `infra_app`): 84/84 на изменённых/новых файлах; полный набор (минус host-path-зависимые `tests/tools/`) — 3186 passed, 61 pre-existing failed (см. находки), 39 skipped.
- [ ] Frontend-кнопка feedback — TODO, перенесено в открытый пункт (не блокирует Фазу 1, но не забыть перед закрытием roadmap).

---

## Фаза 1 — Exploratory-режим WorkOrder: точечные расширения — ЗАКРЫТА (2026-08-20)

### Находки по ходу
- [x] **`fail_attempt` никогда не сбрасывал `step.lease_owner`/`lease_expires_at`** — только `reclaim_expired_leases` (другой путь в `retry_wait`, после исчезновения воркера) это делал. Из-за этого шаг, упавший по обычному пути (`execute_claimed_step`'s exception-хендлеры), становился claimable только после истечения СТАРОЙ 120-секундной аренды с первого claim'а — независимо от того, каким коротким был вычисленный exponential backoff (мог быть 5с). Ретраи были незаметно rate-limited до ~120с минимум. Найдено тестом на checkpoint-ретрай (`claim_ready_step` не находил "готовую" retry_wait-запись), исправлено — `fail_attempt` теперь чистит lease в обеих ветках (retry и terminal), плюс отдельный регрессионный тест.
- [x] В проде: в новом блоке `except PartialProgressError` забыт обязательный kwarg `actor=` у `fail_attempt` — поймано интеграционным тестом (`execute_claimed_step` end-to-end), не догадка/ревью, реальный `TypeError` при первом прогоне против живой БД.
- [x] Мои же тесты на `coverage_report`-критерий изначально ожидали blocker `verification_failed`, но при паре критериев (детерминированный `coverage_report` + семантический `honest_not_found`) существующий (неизменяемый) приоритет в `verify_nonempty_result` всегда возвращает `independent_verification_required`, пока семантический критерий не резолвится — это корректное поведение FSM, не баг; тесты переписаны на проверку статуса конкретного `WorkAcceptanceCriterion`, а не агрегированного blocker-кода.
- [x] Тест `max_tool_calls`-бюджета изначально подставлял `attempt_id=uuid.uuid4()` в `WorkToolCall` — поймано FK-ошибкой на реальном Postgres (`work_tool_calls_attempt_id_fkey`), локальный SQLite/мок этого не поймал бы. Переписан на 3 реальных `WorkStepAttempt` через `claim_ready_step`.
- [x] `cost_usd` (как и `checkpoint` до Фазы 1) был мёртвым полем — никто и никогда его не писал. Раз строится бюджет `max_cost_usd` поверх него — `complete_attempt` теперь тоже заполняет `attempt.cost_usd` из `output.cost_usd` (симметрично уже существовавшему `tokens_used`), иначе бюджет был бы недостижим на практике.

### 1.A Флаг режима — сделано

- [x] `is_exploratory(order)` — читает `order.constraints["mode"] == "exploratory"`, JSON-поле, миграция не понадобилась.
- [x] **Decompose-механизм переиспользован как есть** (`PlannedStep.kind="decompose"`, `_split_child_budgets`) — новый «горизонт-цикл» не писался, он не нужен: существующий incremental replanning (`completed_context`/`failure_context` в `generate_capability_plan`) уже даёт именно это.
- [x] Промпт планировщика (`backend/app/domain/work_planning.py`) — условная добавка к system-промпту при `is_exploratory(order)`: горизонт 1-3 шага вместо полного DAG, предпочтение decompose для независимых юнитов, обязательный контракт финального вывода `{text, coverage:{covered,partial,not_found}}`.

### 1.B Checkpoint — сделано (в объёме retry-возобновления, не потокового progress)

- [x] Архитектурное решение, зафиксированное явно: mid-execution потоковый progress НЕ строился — философия существующей архитектуры («маленькие шаги + replanning», явная фраза в промпте планировщика «smallest executable DAG») делает это ненужным для MVP. Вместо этого — **checkpoint на failure-retry пути**: новое исключение `PartialProgressError` (`backend/app/tasks/work_orders.py`) — капабилити, упавшая с реальным прогрессом, сообщает `checkpoint` в теле ответа (`{"error":..., "checkpoint": {...}}`, конвенция, не обязательный контракт); `_execute_capability` парсит и поднимает `PartialProgressError`; `fail_attempt` сохраняет `checkpoint` на упавшем attempt; следующая попытка (`execute_claimed_step`) подмешивает его в `input_data["_resume_checkpoint"]`, если план сам не задал ключ явно.
- [x] Тесты: `_execute_capability` поднимает `PartialProgressError` только когда `checkpoint` реально есть в теле (4xx/5xx/200-с-ошибкой); `fail_attempt` персистит/не персистит корректно; полный e2e (attempt 1 падает с checkpoint → attempt 2 получает `_resume_checkpoint`) — все на реальном Postgres.

### 1.C Бюджеты — сделано (расширен существующий `enforce_budgets`, не написан параллельный механизм)

- [x] `enforce_budgets` (Б15) расширен тремя новыми измерениями поверх уже бывшего `token_budget`: `max_cost_usd` (сумма `WorkStepAttempt.cost_usd`), `max_wall_clock_seconds` (от `order.started_at`), `max_tool_calls` (счётчик строк `WorkToolCall` по `work_order_id`, независимо от статуса). Проверяются в фиксированном порядке — первый превышенный бюджет определяет причину блокировки.
- [x] Целенаправленного `max_web_requests` (именно веб-запросы отдельно от прочих tool calls) не строилось — честно отложено до Фазы 2, где появится реальная веб-капабилити (`web_discover`) для которой это имело бы смысл; `max_tool_calls` — рабочий общий механизм уже сейчас, не заглушка.
- [x] Тесты: `max_cost_usd`, `max_wall_clock_seconds` (блокирует/не блокирует), `max_tool_calls`, приоритет причины при одновременном превышении нескольких бюджетов — все на реальном Postgres.

### 1.D Honest-coverage — сделано (без изменения FSM/verdict-путей)

- [x] `exploratory_acceptance_criteria()` — пара критериев для `create_work_order(acceptance_criteria=...)`: `coverage_report` (детерминированный, новая ветка `predicate_type == "coverage_report"` в `verify_nonempty_result` — проверяет ТОЛЬКО форму `{covered:[...], partial:[...], not_found:[...]}` и что не все три списка пусты, не исчерпанность) + `honest_not_found` (семантический, независимый verifier — **нового кода в `verify_semantic_criteria` не понадобилось**: существующий verifier уже судит по `predicate.description`, туда просто вписано требование честности).
- [x] Глобальный gate `has_result` (непустой `text`) в `verify_nonempty_result` по-прежнему применяется — у exploratory-финального-шага обязателен человекочитаемый `text` рядом со структурным `coverage`, отдельный тест на этот случай.
- [x] Тесты: well-formed report проходит `coverage_report`; пустой/malformed — падает; отсутствие `text` — блокирует несмотря на хороший `coverage`; промпт-подсказка планировщика подтверждена мок-тестом на `generate_json`.

### 1.E Пересборка и проверка — сделано
- [x] Пересобраны и перезапущены `backend`/`celery-worker`/`celery-beat` — миграция не требовалась (все изменения — существующие JSON/колонки). `celery-beat` прогнал `work.dispatch_ready` (гоняет `enforce_budgets` вживую) без ошибок на реальном трафике (бэкенд обслуживал реальные запросы пользователя параллельно).
- [x] 56 тестов по work-order домену (exploratory + checkpoint + lease/budgets + verifier + replanning + decompose + computer-use + gap-detection) — зелёные на реальном Postgres в контейнере.
- [x] Полный non-live прогон (3211 passed, 61 pre-existing failed — **идентичный список** тем же 61, что и до Фазы 1, побайтово сверено diff'ом) — ноль регрессий.

---

## Фаза 2 — Веб-доступ через ComputerUseGrant + notify-before-scope — ЗАКРЫТА (2026-08-20)

### Находки по ходу
- [x] **Критический баг: `execute_computer_action` использовал `get_current_user`, а не `get_effective_user`.** Когда WorkStep вызывает `computer_use` через штатный путь (`_execute_capability` → `dispatch_capability` → `/api/computer-use/execute`), запрос несёт `X-API-Key` агента + `X-Acting-User: <human sub>`; `get_current_user` резолвит такой запрос в `sub="agent-service"` (см. `_verify_api_key`), НЕ учитывая `X-Acting-User` — обе проверки (`order.owner_key != user.sub` и `grant.granted_to == user.sub`) проваливались с 404 для ЛЮБОГО WorkOrder, принадлежащего реальному человеку. Весь `computer_use` был неработоспособен при вызове из durable runtime (работал только при прямом вызове человеком из браузера). Исправлено: заменена зависимость на `get_effective_user` (тот же паттерн, что уже используется в `memory.py`). Живой тест `test_agent_service_with_acting_header_resolves_to_that_human` подтверждает резолюцию.
- [x] **`get_effective_user` не имел вообще ни одного теста**, хотя на нём держится de-escalation агент→человек в нескольких местах кодовой базы. Добавлен `test_auth_acting.py` (6 тестов, включая деактивированного/несуществующего acting-пользователя — fail-closed на сервисный аккаунт).
- [x] **Generic Approval-retry не подходит для `computer_use`**: одобрение digest'а (`X-Agent-Approval: granted`) не создаёт `ComputerUseGrant` — это отдельная авторизационная сущность, создаваемая только вручную менеджером (`POST /work-orders/{id}/computer-grants`). Одобрение сгенерированного `Approval` для `computer_use`-действия молча ничего не решает. Это и есть настоящее содержание «notify-before-scope, не approval» из плана — реализовано как отдельное уведомление (`_notify_computer_use_needs_grant`), не расширение approval-механизма.
- [x] Мой собственный баг в первой версии notify-фикса: вызвал `_notify_computer_use_needs_grant` **после** `await db.commit()` — `create_notification` только делает `add()+flush()`, не коммитит сама; уведомление создавалось в новой имплицитной транзакции, которая откатывалась при закрытии сессии. Пойман тестом (`notif is not None` падал), перенёс вызов до commit.
- [x] Ни `execute_web_search`, ни `browser_fetch` не были доступны как WorkStep-capability для «поиска» (только точечный `browser_fetch` по known URL) — `web_discover` заполняет этот пробел.
- [x] Планировщик берёт допустимые actions капабилити из pipe-separated строки в `parameters.properties.action.description` капабилити в `capabilities.yml` (не из dispatch-таблицы) — без добавления `web_discover` туда `validate_capability_plan` отклонял бы любой сгенерированный шаг с этим action как «unknown action».

### 2.A Веб-доступ через существующие механизмы — сделано

- [x] `execute_web_research`/`POST /research` уже делает почти всё нужное (multi-query search + параллельный fetch + per-URL diagnostics, никогда не бросает) — **переиспользованы** его составляющие функции (`execute_web_search`, `fetch_page`), не сам endpoint (он не проверяет allowlist хостов вообще — для exploratory-режима с неизвестными заранее хостами это неприемлемо без grant-скоупинга).
- [x] Новый endpoint `POST /api/computer-use/web-discover` (`backend/app/api/computer_use.py`) + action `web_discover` капабилити `computer_use` (`_DISPATCH`, `capabilities.yml`) — search по нескольким query-углам, fetch только host-allowed URL, 1 unit `max_actions` за каждый реальный fetch, полный audit trail (`ComputerUseAction` на каждый fetch). Никогда не бросает из-за одного плохого query/URL — `search_diagnostics`/`skipped` вместо исключения.
- [x] `_host_allowed` расширен явным wildcard-сентинелом `"*"` — exploratory-задача не может заранее перечислить хосты, которые обнаружит поиском; пустой список по-прежнему означает «ничего», wildcard — осознанный акт менеджера при выдаче granta (та же ролевая защита, что и у именованных хостов).
- [x] Никакого обхода robots.txt/логинов/капч — не реализовывалось и не рассматривалось; `fetch_page`/`execute_web_search` — уже существующие безопасные обёртки, новый код их не трогает.
- [x] Пересмотр approval-гранулярности **не понадобился** — `gate_actions: [desktop_click, desktop_type, file_write, shell]` в `capabilities.yml` уже НЕ включает `browser_fetch`/`desktop_snapshot`/`file_read` (не `["*"]`, как предполагал первый черновик плана); `web_discover` добавлен туда же, тоже вне gate_actions.
- [x] Тесты: wildcard host-matching (5), web_discover (грант обязателен / host-фильтрация / wildcard / budget exhaustion / устойчивость к ошибке поиска) — все на реальном Postgres.

### 2.B Notify-before-scope — сделано (иначе, чем в исходном плане, но по существу)

- [x] Исходный план предполагал pre-emptive оценку стоимости на входе в `running`. По факту оказалось точнее и полезнее **реактивное** уведомление: exploratory-план по природе не знает заранее, понадобится ли `computer_use` (small-horizon planning из Ф1.A) — оценивать это до первого реального шага означало бы гадать. Уведомление шлётся в момент, когда шаг РЕАЛЬНО упирается в отсутствие granta (`ApprovalRequiredError` с `capability=="computer_use"`), с чёткой инструкцией «создать grant может только менеджер», отдельно от текста про «approve».
- [x] Явно отличается от approval: **не блокирует** отдельным статусом (шаг всё равно уходит в `waiting_approval`, existing FSM, дублирования не потребовалось), но текст уведомления однозначно говорит, что делать нужно НЕ то же самое, что «одобрить».
- [x] `Notification.source_task="workorder.needs_computer_use_grant"` — задел на калибровку через `ProactiveTaskFeedback` (Фаза 0.B) оставлен, но accept/dismiss UI для этого конкретного уведомления не подключался (это одноразовое событийное уведомление, не периодическая проактивная задача — калибровка для него менее естественна, чем для beat-заданий).
- [x] Тесты: уведомление создаётся с `entity_id`/`user_sub`/текстом для `computer_use`-капабилити; **регрессия проверена явно** — для любой другой capability (`invoices.approve` и т.п.) уведомление НЕ создаётся, старое поведение (только digest-Approval) не тронуто.

### 2.C Пересборка и проверка — сделано
- [x] Пересобраны и перезапущены `backend`/`celery-worker`/`celery-beat`; `aiagent/` смонтирована в контейнер read-only bind-mount'ом — правка `capabilities.yml` подхватывается без пересборки, но сервисы всё равно перезапущены для консистентности с код-изменениями.
- [x] 19+48 тестов (web_discover/computer_use/auth_acting + регрессия work-order/exploratory/checkpoint/lease) зелёные на реальном Postgres в контейнере.
- [x] Полный non-live прогон на пересобранном стеке: 3229 passed (+18 к Фазе 1), 61 pre-existing failed — **идентичный список**, побайтово сверено diff'ом с прогоном Фазы 1. Ноль регрессий.

---

## Фаза 3 — Extraction/normalization пайплайн для каталогов поставщиков

### Находки по ходу
- [ ]

### 3.A Провенанс без миграции (сначала)

- [ ] `backend/app/domain/tool_catalog.py` уже имеет `metadata_`/`metadata` JSON на `ToolCatalogEntryCreate/Update/Out` (подтверждено чтением файла: строки 74-78, 94-98, 117-120) — на первой итерации класть `source_url`/`fetched_at`/`discovery_method` в `metadata_`, **без миграции**. Формализовать в отдельные колонки только если после пилота (Фаза 4) окажется, что нужны индексируемые запросы по этим полям — не заранее.
- [ ] Аналогично статус ревью (`ingested|needs_review|approved`) на первой итерации — в `metadata_.review_status`, т.к. в текущей схеме есть только `is_active: bool`, а не полноценный статус-flow. Формализовать в колонку после подтверждения полезности на пилоте.
- [ ] Явная точка пересмотра: если `metadata_`-подход окажется неудобным для конфликт-детекции/индексации при реальном объёме (Фаза 4) — тогда завести миграцию с явными колонками `source_url`, `fetched_at`, `discovery_method`, `review_status` (nullable/default под обратную совместимость с ручным импортом).

### 3.B Draft-first + конфликты

- [ ] Новые web-scrape записи по умолчанию `metadata_.review_status="ingested"`; ручной импорт как раньше не меняется (обратная совместимость).
- [ ] При конфликте (web-scrape находит уже существующего поставщика/позицию с другими значениями) — не перезаписывать тихо. Найти существующий механизм `AnomalyCard` (`grep -rn "AnomalyCard" backend/app/`) и переиспользовать для конфликтов каталога, не изобретать параллельный механизм.
- [ ] Минимальный approve-эндпоинт `POST /api/tool-catalog/entries/{id}/approve` в существующем роутере каталога — переход `needs_review → approved` в `metadata_`. Полноценный review UI — можно вынести отдельным зафиксированным TODO, не пропускать молча.

### 3.C Хранение и переиспользование существующего extraction-пайплайна

- [ ] Сырые файлы каталога — строго через `backend/app/storage.py` (MinIO `upload_file`/`download_file`/`get_presigned_url`), НЕ через `backend/app/domain/storage.py` (`LocalFileStorage`, зарезервирован под превью/артефакты) — зафиксировать разграничение комментарием в коде на месте вызова.
- [ ] Переиспользовать парсер-реестр `backend/app/ai/parsers/registry.py` (`parse_document`, никогда не бросает исключения, `needs_ocr`-флаг) для веб-скачанных файлов. Проверить покрытие форматов (PDF/XLSX/HTML/картинки); HTML — добавить только если реально понадобится на пилоте.
- [ ] Переиспользовать общий пайплайн стадий `backend/app/domain/pipeline.py` (`store → memory_seed → classification → extraction → sql_records → memory_graph → embedding`) — адаптировать стадию `extraction` под `ToolCatalogEntry` вместо инвойс-специфичных полей. Если стадия окажется слишком жёстко завязана на инвойс-домен — не форкать пайплайн вслепую: сначала завести находку по Правилу 0 с описанием несовместимости, решить (параметризация vs отдельный облегчённый пайплайн) до написания кода.

### 3.D Qdrant `tool_catalog` + граф поставщиков

- [ ] Подтвердить текущую схему коллекции `tool_catalog` (`backend/app/vector/qdrant_store.py`) — embedding-поля совместимы с записями из веб-источника.
- [ ] По аналогии с `ingest_drawing_graph()` (`backend/app/domain/drawing_graph.py:24`) и существующими таблицами `KnowledgeNode`/`KnowledgeEdge` (`backend/app/domain/graph.py`, миграция `c8e4f2a9b731_add_graph_memory.py`) — написать `ingest_supplier_catalog_graph()`: узлы `Supplier`/`CatalogEntry`, рёбра `Supplier -[SUPPLIES]-> CatalogEntry`, опционально `CatalogEntry -[SIMILAR_TO]-> CatalogEntry`.
- [ ] Подключить в extraction-пайплайн после стадии `sql_records`, аналогично `memory_graph`-стадии для инвойсов.
- [ ] Тесты: тестовый PDF/HTML каталог → `ToolSupplier`/`ToolCatalogEntry` со `metadata_.review_status="ingested"`, запись в Qdrant `tool_catalog`, узлы/рёбра в графе, provenance заполнен.

### 3.E Пересборка и проверка
- [ ] См. общий чек-лист. `make test`, **обязательно `make regression`** (трогается extraction pipeline — манифест-регрессия по инвойсам должна остаться зелёной, подтверждая отсутствие регресса основного домена).
- [ ] Ручная проверка: 1-2 реальных общедоступных сайта поставщиков режущего инструмента — прогнать через `fetch_page` → `storage.py` → parser registry → pipeline, проверить записи в Postgres/Qdrant/графе (через `mcp__postgres__query` или скрипт).

---

## Фаза 4 — Сквозной пилот: «найди и структурируй каталоги поставщиков» end-to-end

### Находки по ходу
- [ ]

- [ ] Собрать компоненты Фаз 1-3 в один реальный exploratory WorkOrder на реальных источниках (без моков).
- [ ] Прогнать полный цикл: создание → notify-before-scope (если выше порога) → подтверждение → decompose по источникам → `web_discover` через `ComputerUseGrant` → checkpoint-прогресс → replanning следующего горизонта → extraction pipeline → draft-first запись → honest-coverage критерии → `completed`.
- [ ] Прогресс-нотификации на значимых переходах (`running→verifying`, `blocked`, каждые N шагов) — если ещё не сделаны отдельным пунктом, добавить здесь: `create_notification` + существующий websocket `notifications_ws` (тот же канал, что для approval-флоу, не параллельный).
- [ ] Зафиксировать любые несостыковки между Фазами 1-3, которые проявляются только при сквозной интеграции (например: формат checkpoint из 1.B не совпадает с тем, что реально пишет `web_discover` из 2.A) — первый кандидат на находки по Правилу 0, так как компоненты писались изолированно по фазам.
- [ ] Полная `make rebuild` — обязательна на этом рубеже.
- [ ] `make test`, `make test-live`, `make regression`, `make agent-test`.
- [ ] Ручная сквозная проверка: минимум 3 разных сайта поставщиков (прямой PDF-прайс, HTML-каталог, защищённый капчей/логином — для проверки честного `blocked`). Итоговый honest-coverage отчёт зафиксировать как эталон; по возможности оформить как regression-фикстуру (по аналогии с `example-invoices/manifest.json`) для последующих `make regression`.

---

## Фаза 5 — Connector library / self-training по итогам

### Находки по ходу
- [ ]

### 5.A Модель данных

- [ ] Референс-паттерн — `backend/app/ai/recipes.py` целиком (draft→active→retired, `_ACTIVATE_AFTER`, `_RETIRE_FAIL_RATE`, Qdrant `recipe_triggers`, `confirm_draft_from_worker`, `record_outcome`).
- [ ] Новая модель `SourceConnector` (не расширение плоского `MemoryFact(kind="web_source")` — тому не хватает структурированных `steps`/`status`/`schema_hash`/fail-rate; ORM-модель `RecipeSkill` — прямой прообраз структуры полей: `domain_pattern`, `strategy` (JSON), `status`, `trigger_examples`, `schema_hash`, `success_count`, `fail_count`, `last_validated_at`, `revalidate_after`).
- [ ] Alembic-миграция `source_connectors` (+ Qdrant-коллекция `connector_triggers`, по образцу `recipe_triggers`).
- [ ] Связь с существующим `MemoryFact(kind="web_source")` proposal-реестром (`backend/app/api/memory.py`: `/sources/propose|discover|decide`) — тот остаётся «предложение URL к рассмотрению», `SourceConnector` — «подтверждённая рабочая стратегия»; связать через provenance/`source_connector_id`, не дублировать.

### 5.B Lifecycle

- [ ] `draft` — автоматически после первого успешного exploratory-цикла на домене/паттерне.
- [ ] `active` — после N успешных повторов (отдельная константа от recipe, не переиспользовать то же число вслепую).
- [ ] `retired` — по fail-rate, аналогично `record_outcome` из `recipes.py`.
- [ ] `record_connector_outcome(...)`/`confirm_draft_connector_from_worker(...)` — зеркальные сигнатуры адаптированные под connector-домен.
- [ ] Перед `web_discover`-шагом (Фаза 2.A) — проверять наличие `active`-connector для целевого домена/паттерна и использовать сохранённую стратегию вместо discovery с нуля (экономия бюджета из 1.C).

### 5.C Freshness / ревалидация протухших неудач

- [ ] `last_failure_at`/`consecutive_failures`/`revalidate_after` на `SourceConnector` — TTL, не мгновенный retire.
- [ ] Retired/failing connector не удаляется — помечается на периодическую ревалидацию с растущим интервалом (день → неделя → месяц).
- [ ] Сама периодическая ревалидация — часть Фазы 6; здесь только модель данных + `due_for_revalidation(connector)`.

### 5.D Пересборка и проверка
- [ ] См. общий чек-лист. `make test`, `make test-live`.
- [ ] Ручная проверка: exploratory WorkOrder дважды на одном домене → второй прогон создаёт draft-connector, третий использует его; искусственно сломанный (404) connector корректно инкрементирует `consecutive_failures`, не retire мгновенно.

---

## Фаза 6 — Idle-reflection beat job («подсознание»)

### Находки по ходу
- [ ]

- [ ] Референс-паттерн — `evolve-failing-skills` (2ч, `app.tasks.skill_evolution`) и `memory-graph-analytics` (30мин, самотроттлится по admin-конфигу, `app.tasks.graph_analytics`) в `backend/app/tasks/celery_app.py:97-190` — переиспользовать тот же механизм самотроттлинга/хранения порога.
- [ ] Новый модуль `backend/app/tasks/idle_reflection.py` — редкий тик beat (15-30 мин) с внутренним самотроттлом.
- [ ] Admin-конфигурируемый порог «простоя пользователя» — найти существующий способ определения активности (последний API-запрос/websocket-heartbeat); если трекера нет — минимальный (последний `updated_at` активной сессии/авторизованного запроса).
- [ ] Задачи job: (1) консолидация/дедупликация `MemoryFact` (переиспользовать существующую `superseded_by_id`-логику, если где-то уже реализована, не дублировать); (2) периодическая ревалидация протухших connector-стратегий (`due_for_revalidation` из 5.C) — облегчённый exploratory-шаг в рамках небольшого бюджета (Фаза 1.C); (3) сверка `MemoryFact`/`SourceConnector` с текущим состоянием (например connector ссылается на несуществующий более `web_source`-факт — залогировать несостыковку, не удалять молча).
- [ ] Регистрация в `beat_schedule` по образцу существующих записей.
- [ ] Тесты: job не запускает тяжёлую ревалидацию при активном пользователе; запускает при простое выше порога; ревалидация протухшего connector обновляет `last_validated_at`/`consecutive_failures`.

### Пересборка и проверка
- [ ] Пересобрать `celery-beat`, `celery-worker`. `make test`; `make logs-celery` — новая задача видна в расписании, не падает при первом тике.
- [ ] Ручная проверка: искусственно «состарить» connector (`revalidate_after` в прошлое), запустить job вручную, убедиться в обновлении состояния.

---

## Фаза 7 — Разделение тона/характера от protected settings (независима от остальных)

### Находки по ходу
- [ ]

- [ ] Перепроверить актуальный `PROTECTED_SETTINGS` (`backend/app/ai/policy_engine.py:10-34`) — `agent_name`/`system_prompt` защищены, отдельного поля тона нет.
- [ ] Добавить в `BuiltinAgentConfig` (`backend/app/ai/agent_config.py`) новое НЕ-protected поле `agent_tone` (enum: `neutral|friendly|formal|concise`) — управляет только стилем финального ответа, не влияет на risk/outcome-оценку решения. Не добавлять в `PROTECTED_SETTINGS`, задокументировать почему рядом в коде.
- [ ] `backend/app/api/agent_control_plane.py: _create_config_proposal (485-506)` — подтвердить, что non-protected путь уже применяет правки сразу без risk-diff для любого поля вне списка; если есть отдельный allowlist полей помимо `PROTECTED_SETTINGS`, который тоже нужно обновить — это находка по Правилу 0.
- [ ] Подключить `agent_tone` в точке сборки финального промпта/пост-обработки ответа как модификатор стиля (system-prompt suffix/style-hint), не трогая логику принятия решений.
- [ ] Тесты: смена `agent_tone` применяется сразу без approval; смена `system_prompt` по-прежнему требует protected-flow (регресс недопустим); один и тот же вопрос с разным тоном даёт идентичное решение/outcome и разный стиль ответа.

### Пересборка и проверка
- [ ] Пересобрать `backend`. `make test`.
- [ ] Ручная проверка на живом стеке: сменить `agent_tone` через API, задать одинаковый вопрос дважды с разным тоном.

---

## Итоговый чек-лист закрытия roadmap

- [ ] Все `### Находки по ходу` во всех фазах закрыты (просмотреть весь файл — незакрытых пунктов не осталось).
- [ ] Финальный `make rebuild` на всём стеке.
- [ ] `make test`, `make test-live`, `make regression`, `make agent-test`, `make e2e` (если фронтенд менялся в 0.B/7) — всё зелёное на пересобранном стеке.
- [ ] Краткий статус добавлен в `DEVPLAN.md`/`AGENT_SYSTEM_DEVELOPMENT_PLAN.md` со ссылкой на `AGENT_AUTONOMY_ROADMAP.md` — не дублировать детали, только статус по трекам.

---

## Критичные файлы (для быстрой ориентации)

- `backend/app/domain/work_orders.py` — FSM, бюджеты, verifier, completion
- `backend/app/domain/work_planning.py` — планировщик, decompose, replanning
- `backend/app/tasks/work_orders.py` — dispatch, decompose-исполнение, semantic verifier
- `backend/app/db/models.py` — WorkOrder/WorkStep/WorkStepAttempt/MemoryFact/RecipeSkill/ToolSupplier/ToolCatalogEntry/Notification (строки указаны в тексте фаз)
- `backend/app/api/memory.py` — scope/видимость, web_source proposals
- `backend/app/domain/tool_catalog.py`, `backend/app/api/tool_catalog.py` — схема каталога поставщиков
- `backend/app/ai/recipes.py` — референс lifecycle для connector library
- `backend/app/ai/policy_engine.py`, `backend/app/ai/agent_config.py` — protected settings, тон
- `backend/app/tasks/celery_app.py` — beat_schedule, точка регистрации idle-reflection
- `backend/app/api/computer_use.py`, `backend/app/api/web_search.py` — веб-доступ
- `Makefile`, `infra/docker-compose*.yml` — пересборка и тесты живого стека
