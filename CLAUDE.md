# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

# AI Manufacturing Workspace

## Проект
Единое рабочее пространство ИИ-документооборота для промышленного предприятия.
AI-сотрудник **Света** (AiAgent agent) обрабатывает счета, письма, чертежи.

## Ключевые документы
- `plan_claude.md` — полное ТЗ v2.0 (20 разделов)
- `DEVPLAN.md` — план разработки с ToDo (~1530 строк, 7 эпиков, 52 skills, 8 scenarios)
- `PLAN.md` — краткий стек и ToDo

## Стек
- **Agent**: встроенный Python-агент в `backend/app/ai/` (orchestrator + AgentSession) — AI-сотрудник «Света»; `aiagent/` содержит только конфиги, промпты, реестры skills и сценарии
- **Backend**: Python / FastAPI + Celery + Redis
- **Frontend**: Next.js (PWA) + next-intl (RU по умолчанию)
- **DB**: PostgreSQL, Qdrant (vector), MinIO (files)
- **AI**: Ollama (gemma4:e4b локально для OCR, gemma4:26b или Claude API для reasoning)
- **Auth**: Authentik (self-hosted SSO)
- **Infra**: Docker Compose, Traefik

## Архитектурные принципы
- Агент (`backend/app/ai/`) = мозг (planning, reasoning), FastAPI endpoints = руки (CRUD, data, async tasks)
- **Agent Control Plane**: настройки, политики, skills/plugins, task/team/cron и память управляются через typed API + GUI, а не через ручное редактирование промптов
- **Degraded mode**: UI работает через REST без AiAgent
- **Draft-first**: внешние действия только через approval gates
- **Protected settings**: личность агента, system prompt, память, аудит, approval gates, режим прав и auto-apply не меняются молча; нужен risk diff и подтверждение
- **Dual AI**: конфиденциальные документы — только локальный Ollama. Задачи, читающие содержимое (`classification`, `long_context_summarization`, `engineering_reasoning`, OCR, извлечение, чертежи), закрыты жёстко — `CONFIDENTIAL_TASKS`. Planner/auditor и генерация писем — `CLOUD_OPT_IN_TASKS`: локальны по умолчанию, облако включается защищённым действием. Задача обязана быть в одном из двух списков, за полнотой следит `test_confidential_task_coverage.py`
- **Keyboard-first UX**: все ежедневные действия с клавиатуры
- **i18n: подключён, но не разложен.** `next-intl` и словари `messages/{ru,en}.json` (по 1335 ключей) работают, однако переводы используются в 24 файлах из 222 — остальные 2186 строк вписаны в код по-русски. Больше всего в `app/settings` (327), `components/models` (162), `app/documents` (123). Раскладывать весь интерфейс сейчас незачем: второго языка никто не спрашивал, а перевод ради перевода стареет быстрее кода. **Правило для нового кода:** там, где раздел уже переведён (`app/email`, `components/studio`, `components/cad`), новые строки идут через `useTranslations`; в остальных — по-русски в коде, чтобы не плодить полупереведённые экраны

## Структура проекта (целевая)
```
backend/app/       — FastAPI (api/, domain/, tasks/, ai/, db/)
frontend/app/      — Next.js pages
frontend/components/ — React компоненты
backend/app/ai/    — агент: orchestrator, agent_loop, capabilities, память
aiagent/          — config, prompts, skills (реестры), scenarios — данные, не код
infra/             — docker-compose, traefik, scripts
```

## Соглашения
- Язык общения и документации: **русский**
- Код, комментарии в коде, имена переменных: **английский**
- Pydantic schemas = единый источник правды для AiAgent skills (auto-gen YAML)
- 23 capability (21 маршрутизируемая + vault/mcp), 323 действия, 41 gate_action
- Все endpoints = AiAgent tools (через `infra/scripts/generate-skill-registry.py`)

## Команды (целевые)
```bash
make dev          # весь стек
make test         # unit + API + integration
make e2e          # Playwright
make regression   # extraction quality
make emg-regression       # deterministic four-domain EMG golden gate
make emg-live-regression  # live CAD/STEP/IFC/system production matrix
make agent-test   # AiAgent scenarios на mock skills
```

## Разделение ответственности

| Вопрос | Кто отвечает |
|---|---|
| «Что делать?» (planning, reasoning) | AiAgent (gemma4:26b или Claude API) |
| «Как это сделать?» (CRUD, валидация) | FastAPI |
| «Тяжёлая работа» (OCR, PDF, Excel gen) | Celery |
| «Показать пользователю» | Next.js |
| «Можно ли это сделать?» (approval gate) | AiAgent спрашивает → человек отвечает |

## AI-роутинг

- **gemma4:e4b** (Ollama, локально) — OCR, классификация, извлечение счетов. Только локально: документы конфиденциальны.
- **gemma4:26b или Claude API** — reasoning, генерация писем, NL-query. Настраиваемо per-task (on-prem или внешний API).
- **Облако для planner/auditor**: `orchestrator_model`/`auditor_model` могут указывать на cloud-модели из `model_registry.yaml` (например `claude_sonnet_anthropic`); по умолчанию всё локально. `auditor_allow_cloud` — protected setting. AI router жёстко блокирует confidential-контент от облачных маршрутов.

### Управление провайдерами и моделями (рефакторинг 2026-06-14)

- **Чёткое разделение**: провайдеры (инфраструктура) vs модели (каталог) vs назначение (task/role → model).
- **Provider instances** (`backend/app/ai/provider_registry.py`, таблица `provider_instances`): несколько узлов на один kind — Ollama/vLLM/llama.cpp на разных машинах сети. `select_instance(kind, model, preferred_instance)` выбирает узел (pin модели → узел с моделью → первый живой). Облачные ключи — зашифрованы в БД (`secret_box.py`, Fernet на app_secret_key), `.env` остаётся fallback. Резолв base_url/ключа: DB → YAML → env. Кэш в Redis (`provider_instances`), сидинг на старте (`provider_bootstrap.py`).
- **API**: `/api/providers/*` (CRUD узлов, `/test`, `/refresh-models` авто-подтягивает облачные модели), `/api/providers/models` (каталог + thinking), `/api/providers/assignments` (единая таблица + `/revisions` для отката), `/api/providers/slots/{slot}/candidates` (пригодность модели для слота считает бэкенд, не TS), `/api/providers/slots/health`, `/api/providers/cost-report`. GUI: `/settings/models` — вкладки «Назначение / Провайдеры / Библиотека / Параметры / GPU», по умолчанию «Назначение». Сам экран — оболочка на ~130 строк, вкладки живут в `frontend/components/models/{assignment,catalog,infra,picker,telemetry}`, общие типы и форматирование — в `frontend/lib/models/`.
- **Черновик назначения**: модель, рассуждение, узел и разрешение облака применяются одной кнопкой через `SlotDraft` — раньше модель ждала «Применить», а соседние переключатели срабатывали сразу. Предпросмотр — `POST /slots/{slot}/smoke {dry_run:true}`: резолвит каталог, политику, узел и параметры рассуждения, ничего не отправляя провайдеру. Каждое применение пишет ревизию (`model_assignment_revisions`), откатывается любая, а не только последняя.
- **Выбор модели — двухшаговый**: сначала провайдер, потом его модели (`components/models/picker/ProviderModelPicker`). Отдельного чекбокса «разрешить облако для слота» нет: выбор облачного провайдера и есть это решение. Слоты, через которые проходит содержимое документов и писем, локальны по умолчанию (`_SLOTS[*].local_only`) и переводятся в облако осознанным действием.
- **Рассуждение**: три уровня и одна полярность — модель («умеет» `thinking_supported` / «по умолчанию» `thinking_enabled`), слот («в этой роли», `TaskRouting.thinking` либо агентское поле), провайдер («можно ли выключить»). `AIRequest.thinking` прокидывается в провайдеры (Ollama `think`, Anthropic extended). Поля `agent_config.*_disable_thinking` хранят ОТРИЦАНИЕ и остаются единственным таким местом — работать с ними только через аксессоры `thinking_enabled_for()` / `with_thinking_enabled_for()`. В интерфейсе слова «thinking» нет: «рассуждение» и «усилие рассуждения», контрол сегментированный «Авто · Вкл · Выкл».
- **Каталог**: core-набор = production (6 локальных + 2 cloud Claude); дубли VLM/устаревшее → `disabled` (скрыты фильтром по `status`, не удалены). `task_routing` (Redis) — источник правды для *task→model* назначений (`model_resolver.py` читает только его). `ai_config` (`backend/data/ai_config.json` + Redis-зеркало, `api/ai_settings.py`) — второе хранилище тех же настроек, из которого **выбор модели больше не читает никто**: OCR-экстракция, reasoning-провайдер, embeddings, реранк, перепроверка и агент резолвят через `model_resolver`/`task_routing`. `_mirror_ai_config` остался только на запись, для внешних потребителей `/api/ai/config`; `task_routing.migrate_from_ai_config()` — one-time миграция при старте. Из настроек поведения store по-прежнему живой (`auto_verify_enabled`, порог автоодобрения).

## Агент: архитектура хода (после рефакторинга 2026-06)

- **Секретарь = оркестратор** (front-agent «Света», `backend/app/ai/orchestrator.py`): flow-status вопросы и детерминированные count-вопросы отвечает сам (0 LLM); остальное диспетчеризует специалистам (роли в `gateway.yml`: prompt + capability-allowlist).
- **Маршрутизация**: единая декларативная таблица `aiagent/config/routes.yml` (`backend/app/ai/route_table.py`) — keywords, fast-paths, canvas-правила, chips, prompt-секции. Не добавлять ключевые слова в код.
- **Аудит**: типизированные коды (`backend/app/ai/audit.py`, `AuditCode`); retry/repair/gap управляются кодами, не текстом сообщений. Бюджет вспомогательных LLM-вызовов на ход: `aux_quality_budget(tier)`.
- **Spec-таблицы**: «таблица = спецификация, данные = SQL». LLM передаёт только TableSpec (источник/колонки/фильтры/сортировка из whitelisted-каталога `backend/app/domain/table_spec.py`), движок отдаёт ПОЛНЫЙ датасет (true total, cap 5000). Spec хранится в workspace-блоке; правки («добавь столбец с НДС перед суммой», «отсортируй…», «покажи только…») — детерминированные патчи через fast-path оркестратора, 0 LLM. API: `/api/workspace/agent/spec-table(+/patch,/catalog)`; capability `workspace`, actions `spec_table*`. Smart-фильтр: стемминг + точные числа + canonical items.
- **Рецепты (self-learning)**: успешный многошаговый ход → draft `RecipeSkill` (Postgres + Qdrant `recipe_triggers`); активный рецепт с похожим триггером выполняется replay'ем без LLM-планирования (`backend/app/ai/recipes.py`, UI: /settings/recipes). Approval-gated действия в рецепты не попадают.
- **Кодоген под замком**: сгенерированный Python исполняется ТОЛЬКО в изолированном контейнере `skill-runner` (infra/skill-runner; non-root, read-only, без секретов); активация только через proposal → human decide → promote. Реестр promoted-скиллов: `aiagent/skills/capabilities.generated.yml` — создаётся при первом promote, в чистом репозитории его нет.
- **AgentCron**: beat-задача `agent.cron_dispatch` создаёт `WorkOrder` и совместимый `AgentTask`; выполнение идёт только через durable runtime. `AgentTeam` пока остаётся registry.

## Skills и endpoints

23 capability, 323 действия. Каждое действие = FastAPI endpoint, описанный Pydantic-схемой. Скрипт `infra/scripts/generate-skill-registry.py` генерирует YAML для AiAgent из Pydantic-схем автоматически. Pydantic схемы = единственный источник правды для AiAgent skills.

Категории skills: Documents, Invoices, Email, Suppliers, Anomalies, Tables & Export, Approvals, Calendar, Collections, Normalization, NL & Search, Compare (КП), Audit.

41 gate_action — только они блокируют агента и требуют явного подтверждения человеком. Источник правды — `gate_actions` в `aiagent/skills/capabilities.yml`, enforcement — HTTP-граница `capability_router` (ответ `423 approval_required`). Примеры: `invoices.approve`, `email.send`, `anomalies.resolve`, `memory.prune`.

Сверять реестр: `validate_capability_catalog()` и `validate_gateway_grants()` в `backend/app/api/capability_router.py` — оба должны возвращать пустой список.

## Agent Control Plane

Первый слой control plane реализован в `/api/agent/*`:
- `/api/agent/control-plane/status` — здоровье автономии, политики, plugins, tasks, cron, memory facts.
- `/api/agent/config/proposals` — предложения изменения настроек; protected settings требуют решения.
- `/api/agent/config/propose` — agent-facing alias для предложения изменений настроек.
- `/api/agent/tasks`, `/api/agent/teams`, `/api/agent/cron` — registry автономной работы отдела ИИ.
- `/api/agent/plugins` — manifest-based plugin drafts и enable/disable.
- `/api/agent/capabilities/*` — предложения новых tools/skills, статус lifecycle и sandbox validation skeleton.
- `/api/memory/chat-turn`, `/api/memory/pin` — episodic и pinned memory facts.

Durable runtime реализован в `/api/work-orders` и `backend/app/domain/work_orders.py`:
- `WorkOrder → WorkPlan → WorkStep → WorkStepAttempt` хранит цель, DAG-план, checkpoint, retry и lease независимо от WebSocket/worker.
- Beat `work.dispatch_ready` подбирает ready/retry шаги через `SKIP LOCKED`, восстанавливает истёкшие lease и отдельно запускает verifier.
- `agent_turn` обеспечивает совместимость, `capability` выполняет типизированный action через единый gateway; критический ответ `approval_required` атомарно переводит шаг в `waiting_approval`.
- Approval привязан digest к capability/action/точным аргументам. `completed` невозможен без passed-критериев и evidence; semantic-критерии закрываются независимым verifier endpoint.
- Старый `/api/agent/tasks/{id}/run` создаёт связанный WorkOrder и больше не имеет отдельного пути исполнения.
- Автопланировщик `domain/work_planning.py` строит DAG только по живому `capabilities.yml`; ссылки `${steps.<key>.output.<path>}` разрешаются перед запуском и сохраняются вместе с provenance.
- Каждый фактический вызов сначала фиксируется в `work_tool_calls` с точными аргументами, risk, digest и idempotency key. Исчерпание попыток запускает bounded replanning только незавершённого остатка.
- Неразрешённые semantic-критерии проверяет отдельный локальный verifier с fail-closed verdict; оператор видит план и журнал на `/work-orders`.
- `computer_use` работает только через короткоживущие `ComputerUseGrant`: allowlist действий, каталогов, хостов и argv-команд, лимит операций и отдельный audit/evidence для browser/files/shell/snapshot.
- Завершение атомарно создаёт `WorkLearning`; отдельный recoverable worker материализует owner-scoped `MemoryFact` с provenance по планам/tool calls/criteria/evidence и связывает безопасный draft-рецепт. Freshness, expiry, dispute и supersession не позволяют устаревшему опыту молча попадать в контекст.
- Для пустой БД `alembic upgrade head` атомарно создаёт актуальную metadata и stamp head, обходя исторический dynamic-baseline defect; существующие БД продолжают обычную последовательность миграций.

Целевой режим автономии — `max_autonomy`: агент может сам готовить и проверять изменения в sandbox, но продакшен-код, внешние действия, права, память/аудит/approval gates и личность агента применяются только через объяснимое подтверждение.

## Поиск по картинке в каталогах

Картинки и текст — в одном векторном пространстве (сайдкар `infra/vl-embedding`,
Qwen3-VL-Embedding-2B; коллекция Qdrant `tool_catalog_visual`, отдельно от
`tool_catalog`: другая модель и другая размерность). Через Ollama это
невозможно: её `/api/embed` принимает `images` и молча игнорирует их.

- Индексация: `catalog.visual_index_batch` — партии с чекпоинтом в записи
  (`metadata_.visual_indexed_at` + `visual_model`), смена модели = переиндексация.
- Поиск: `POST /api/catalogs/search-visual` (фото, слова, `entry_id` —
  «похожие по виду»), capability `tool_catalog.search_visual`.
- Сервис недоступен → `available=false` и пустой ответ; подмена текстовым
  поиском запрещена.

## Поддержка нескольких IMAP-ящиков

Routing по ящику (закупки / бухгалтерия / общий). Экспорт — и в Excel (openpyxl), и в формат 1С (обязательно).

## Статусы документа

Основной flow: `Ingested → Needs Review → Approved / Rejected`. AnomalyCard создаётся автоматически при детекции аномалии и требует решения руководителя.
