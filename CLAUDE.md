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
- **Dual AI**: конфиденциальные документы — только локальный Ollama
- **Keyboard-first UX**: все ежедневные действия с клавиатуры
- **i18n-ready** с первого дня

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
- 52 skills в 13 категориях, 9 approval gates
- Все endpoints = AiAgent tools (через `generate-skill-registry.py`)

## Команды (целевые)
```bash
make dev          # весь стек
make test         # unit + API + integration
make e2e          # Playwright
make regression   # extraction quality
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
- **API**: `/api/providers/*` (CRUD узлов, `/test`, `/refresh-models` авто-подтягивает облачные модели), `/api/providers/models` (каталог + thinking), `/api/providers/assignments` (единая таблица). GUI: `/settings/models` (провайдер-центричный); рич-функции (библиотека/загрузка/GPU) — `/settings/models/advanced`.
- **Thinking (режим рассуждения)**: per-модель в каталоге (`thinking_supported`/`thinking_enabled`) + `AIRequest.thinking` прокидывается в провайдеры (Ollama `think`, Anthropic extended). Роли агента — tri-state override (`*_disable_thinking: None` → дефолт модели). UI: галочка у каждой локальной модели.
- **Каталог**: core-набор = production (6 локальных + 2 cloud Claude); дубли VLM/устаревшее → `disabled` (скрыты фильтром по `status`, не удалены). `task_routing` (Redis) — источник правды назначений; `ai_config` — зеркало для legacy-вызовов `model_resolver.py`.

## Агент: архитектура хода (после рефакторинга 2026-06)

- **Секретарь = оркестратор** (front-agent «Света», `backend/app/ai/orchestrator.py`): flow-status вопросы и детерминированные count-вопросы отвечает сам (0 LLM); остальное диспетчеризует специалистам (роли в `gateway.yml`: prompt + capability-allowlist).
- **Маршрутизация**: единая декларативная таблица `aiagent/config/routes.yml` (`backend/app/ai/route_table.py`) — keywords, fast-paths, canvas-правила, chips, prompt-секции. Не добавлять ключевые слова в код.
- **Аудит**: типизированные коды (`backend/app/ai/audit.py`, `AuditCode`); retry/repair/gap управляются кодами, не текстом сообщений. Бюджет вспомогательных LLM-вызовов на ход: `aux_quality_budget(tier)`.
- **Spec-таблицы**: «таблица = спецификация, данные = SQL». LLM передаёт только TableSpec (источник/колонки/фильтры/сортировка из whitelisted-каталога `backend/app/domain/table_spec.py`), движок отдаёт ПОЛНЫЙ датасет (true total, cap 5000). Spec хранится в workspace-блоке; правки («добавь столбец с НДС перед суммой», «отсортируй…», «покажи только…») — детерминированные патчи через fast-path оркестратора, 0 LLM. API: `/api/workspace/agent/spec-table(+/patch,/catalog)`; capability `workspace`, actions `spec_table*`. Smart-фильтр: стемминг + точные числа + canonical items.
- **Рецепты (self-learning)**: успешный многошаговый ход → draft `RecipeSkill` (Postgres + Qdrant `recipe_triggers`); активный рецепт с похожим триггером выполняется replay'ем без LLM-планирования (`backend/app/ai/recipes.py`, UI: /settings/recipes). Approval-gated действия в рецепты не попадают.
- **Кодоген под замком**: сгенерированный Python исполняется ТОЛЬКО в изолированном контейнере `skill-runner` (infra/skill-runner; non-root, read-only, без секретов); активация только через proposal → human decide → promote. Реестр promoted-скиллов: `aiagent/skills/capabilities.generated.yml`.
- **AgentCron**: beat-задача `agent.cron_dispatch` ежеминутно выполняет due-расписания headless-ходом агента, результат — в `AgentTask`. `AgentTeam` — stored-only (исполнителя нет, осознанно).

## Skills и endpoints

52 skills в 13 категориях. Каждый skill = FastAPI endpoint, описанный Pydantic-схемой. Скрипт `generate-skill-registry.py` генерирует YAML для AiAgent из Pydantic-схем автоматически. Pydantic схемы = единственный источник правды для AiAgent skills.

Категории skills: Documents, Invoices, Email, Suppliers, Anomalies, Tables & Export, Approvals, Calendar, Collections, Normalization, NL & Search, Compare (КП), Audit.

9 approval gates — только они блокируют агента и требуют явного подтверждения человеком. Примеры: `invoice.approve`, `email.send`, `anomaly.resolve`, `table.apply_diff`.

## Agent Control Plane

Первый слой control plane реализован в `/api/agent/*`:
- `/api/agent/control-plane/status` — здоровье автономии, политики, plugins, tasks, cron, memory facts.
- `/api/agent/config/proposals` — предложения изменения настроек; protected settings требуют решения.
- `/api/agent/config/propose` — agent-facing alias для предложения изменений настроек.
- `/api/agent/tasks`, `/api/agent/teams`, `/api/agent/cron` — registry автономной работы отдела ИИ.
- `/api/agent/plugins` — manifest-based plugin drafts и enable/disable.
- `/api/agent/capabilities/*` — предложения новых tools/skills, статус lifecycle и sandbox validation skeleton.
- `/api/memory/chat-turn`, `/api/memory/pin` — episodic и pinned memory facts.

Целевой режим автономии — `max_autonomy`: агент может сам готовить и проверять изменения в sandbox, но продакшен-код, внешние действия, права, память/аудит/approval gates и личность агента применяются только через объяснимое подтверждение.

## Поддержка нескольких IMAP-ящиков

Routing по ящику (закупки / бухгалтерия / общий). Экспорт — и в Excel (openpyxl), и в формат 1С (обязательно).

## Статусы документа

Основной flow: `Ingested → Needs Review → Approved / Rejected`. AnomalyCard создаётся автоматически при детекции аномалии и требует решения руководителя.
