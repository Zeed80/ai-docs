# AI Docs

> Статус: проект находится на стадии активной разработки и пока не является работоспособным продуктом для реального использования.

AI Docs — локальная платформа для обработки технических, закупочных и учётных документов с AI-памятью, нормоконтролем, графом связей, графической студией чертежей и встроенным агентом-сотрудником **Светой**.

Проект ориентирован на производственные задачи: счета и закупки, документы НТД, чертежи (анализ, генерация и правка через ComfyUI, обучение собственных LoRA), техпроцессы по ГОСТ ЕСТД, оснастку, инструмент, станки, складские операции, нормы и инженерные взаимосвязи между документами. Конфиденциальные OCR, извлечение данных и анализ по умолчанию рассчитаны на локальную инфраструктуру (Dual AI).

## Возможности

### Документооборот
- Загрузка документов через веб-интерфейс и API.
- OCR и извлечение текста из PDF, изображений и офисных документов.
- Классификация документов и постановка задач на асинхронную обработку.
- Управление загруженными документами: просмотр, выборочное удаление, массовое удаление, очистка dev-данных.
- Нормоконтроль НТД в автоматическом режиме или по кнопке пользователя.
- Статусный поток документа: `Ingested → Needs Review → Approved / Rejected`; аномалии создают `AnomalyCard`, требующую решения руководителя.
- Видимость и владение документами (`owner_sub`, отделы, иерархия), draft-first: внешние действия только через approval gates.

### Счета, закупки и извлечение данных
- Извлечение реквизитов счетов и накладных локальным OCR (`qwen3.5:9b` в проде — 0.99 достоверности на русском) с checksum-валидаторами ИНН/КПП/БИК/расчётного счёта.
- Полный набор извлечённых полей в редактируемой таблице «Счета»: даты, КПП, адрес, покупатель, идентификатор платежа, позиции (line items), спецотметки; изменяемая ширина столбцов.
- Confidence gate: пользовательский порог авто-утверждения по значимым полям; `auto_verify` по умолчанию.
- Сравнение коммерческих предложений (КП), детекция аномалий по поставщикам.
- Экспорт в Excel (openpyxl) **и** в формат 1С (обязательно).

### Чертежи и НТД: анализ (раздел «Чертежи»)
- Мультивидовой VLM-анализ чертежей (`qwen3-vl`, `qwen3.5:27b` и др.): состав видов, элементы, спецификация сборки (Assembly BOM).
- Извлечение геометрии и данных из DXF/DWG (рендер через ezdxf/LibreDWG), PDF-альбомов, растровых сканов.
- Граф связей между чертежами, документами НТД и техпроцессами.

### Графическая студия (раздел «Студия»)
- Генерация и правка технических чертежей через внешний **ComfyUI** (provider-узел on-prem, draft-first): создание по описанию, редактирование, inpaint, «Очистить чертёж» (фото → чистый чертёж), техрисунки ЕСКД.
- Три вкладки: **Студия** (генерация/правка), **Воркфлоу** (встроенный живой ComfyUI, клик грузит граф на канвас), **Обучение LoRA**.
- Переключатель «Быстро/Качественно» для edit/cleanup/inpaint; classical-CV регуляризация cleanup (deskew/denoise/binarize); OCR-anchored сохранение текста при генерации.
- **Качественный апскейл** результата (2×/3×/4×) во всех диффузионных режимах — model-based ESRGAN (4x-UltraSharp) на узле ComfyUI + точная кратность через Lanczos; применяется к финальному изображению.
- **Прогресс генерации** живьём (полоска по WebSocket ComfyUI) и удобное удаление любых генераций, включая ошибочные (массовая очистка).
- ЕСКД-стиль по умолчанию (техрисунки без рамки/штампа с автообрезкой), справочники допусков/резьбы/материалов, сборки и разрезы.
- Graceful degradation: UI работает и без ComfyUI; approval gate `accept_techdraw`.

### Обучение LoRA (в студии)
- Полный цикл: загрузка исходников (PNG/JPG/DXF/DWG/PDF) → подготовка датасета (рендер + синтетические листы ЕСКД + деградация под фото + капшены) → обучение → чекпойнты → деплой на узел ComfyUI одним кликом (`make-workflow`).
- Два пресета: «Очистка чертежей» (фото → чистый чертёж, VLM-капшены) и «Правки чертежей» (инструкция → правка, синтетические пары).
- **Дообучение (fine-tune)**: продолжить обучение любой LoRA — своего чекпойнта, задеплоенной на узле ComfyUI или загруженного стороннего `.safetensors`. Перед запуском — автоматическая проверка совместимости по метаданным (семейство базовой модели, rank): несовместимую (например qwen-LoRA на FLUX.2) блокирует, rank выравнивается автоматически.
- Базовые модели: **Qwen-Image-Edit** (uint3+ARA, влезает в 24 ГБ) и **FLUX.2** (klein-4B — открытая, без токена; klein-9B и dev-32B — gated, нужен `HF_TOKEN`).
- Мягкий GPU-lock: на время обучения локальные ИИ-функции получают понятный отказ (Ollama/ComfyUI выгружаются), облачные маршруты продолжают работать; баннер в студии.
- Живой прогресс: шаг/loss/ETA/история (sparkline с EMA), сетка эволюции сэмплов по шагам, детерминированные spec-капшены синтетики, honest holdout для валидации.
- Запуск сразу по кнопке с подтверждением в панели (подготовка датасета и обучение — выделенная очередь Celery `lora`, тренер в изолированном контейнере через docker SDK).

### Техпроцессы (ГОСТ ЕСТД)
- Генерация технологических процессов по ГОСТ ЕСТД (`tp_generator`), нормоконтроль (`normcontrol_agent`), формы ГОСТ.
- Фронтенд с вкладками маршрута/операций/оснастки; approval-гейты и трассировка сценариев (ScenarioTrace).

### Таблицы и экспорт (Excel-аналог)
- Единый DataGrid: сортировка, фильтры, изменяемая ширина, sticky/zebra.
- **Spec-таблицы** («таблица = спецификация, данные = SQL»): LLM задаёт только TableSpec из whitelisted-каталога, движок отдаёт полный датасет (cap 5000). Правки («добавь столбец НДС», «отсортируй…», «покажи только…») — детерминированные патчи оркестратора без LLM.
- Черновые листы (WorkspaceSheet) и движок формул; правки ячеек через approval gate `table.apply_diff`.

### Память и поиск
- Гибридная память: SQL, векторный поиск (Qdrant), граф знаний — работают совместно с автоматическим fusion-reranking.
- Эпизодическая память chat-turn: агент помнит историю диалогов и способен ссылаться на неё.
- Фильтрация chat-истории по конкретному документу (`document_id`) — результаты из других контекстов не примешиваются.
- Управляемое удаление устаревших эпизодических фактов: `POST /api/memory/prune` с параметрами `scope`, `kinds`, `older_than_days`; pinned-факты защищены.
- Pinned memory facts — постоянные проверенные знания с приоритетным рангом в поиске.

### AI-агент «Света»
- Оркестратор с планированием, распределением ролей (invoice_specialist, warehouse_specialist, procurement_specialist, engineer и др.) и аудитом результата.
- **Самообучающийся routing**: после каждого turn записывает исходы инструментов в Redis (`orchestrator:skill:{name}`, TTL 30 дней); при следующем аналогичном запросе инжектирует hint о предпочтительных/проблемных скиллах в LLM-плановщик.
- **Approval gates**: инструменты `invoice.approve`, `email.send`, `anomaly.resolve`, `table.apply_diff` и другие требуют явного подтверждения; оркестратор не выполняет их в обход approval-проверки.
- **Exponential backoff**: HTTP-вызовы к backend делают до 3 попыток (1 с → 2 с → 4 с) перед отказом.
- **Hot-reload skill registry**: при изменении `aiagent/skills/_registry.yml` без рестарта пересобирается карта скиллов.
- Approval timeout с retry: 2 повторные попытки вместо немедленного отклонения.
- Configurable autonomy modes: agent предлагает изменения настроек, защищённые параметры требуют подтверждения.
- MCP-инструменты: при сбое init — WebSocket event `system_warning`, агент не предлагает недоступные инструменты.

### Agent Control Plane
- `GET /api/agent/control-plane/status` — здоровье автономии, плагины, задачи, cron, факты памяти.
- `POST /api/agent/config/proposals` — предложения изменений; protected settings требуют решения пользователя.
- **Capability lifecycle**: `POST /capabilities/{id}/sandbox-apply` → `POST /capabilities/{id}/decide` → `POST /capabilities/{id}/promote`. Promote копирует sandbox-артефакты в staging и добавляет скилл в `gateway.yml` exposed list с немедленным hot-reload.
- `/api/agent/tasks`, `/api/agent/teams`, `/api/agent/cron` — реестр автономной работы; `PATCH /cron/{id}` для enable/disable.
- `/api/agent/plugins` — manifest-based плагины с enable/disable.
- GUI-панели Tasks, Plugins, Cron, Teams в разделе Settings.

### AI-маршрутизация и провайдеры
- **Dual AI**: конфиденциальные документы — строго локальный Ollama; reasoning и письма — настраиваемо (local / cloud).
- `gemma4:26b` поддерживает tool-calling и является первым fallback в маршруте `tool_calling`.
- Circuit breaker для Ollama персистентен в Redis (TTL 300 с, авто-восстановление при старте).
- JSON-ошибки при стриминге Ollama ретраются так же, как сетевые таймауты.
- **`GET /health/ai`** — проверяет каждый провайдер при старте и по запросу; cloud-провайдеры без ключа отмечаются как `skipped`, не как ошибка.

### Skill Registry
- Скрипт `infra/scripts/generate-skill-registry.py` генерирует `aiagent/skills/_registry.yml` из FastAPI/Pydantic-схем: все body params (merged union), path params, approval gates из `gateway_config` (не по ключевым словам).
- Конфигурация canvas-маппингов вынесена в `backend/data/canvas_skill_map.json` — canvas→skill, skill→path+args, intent_routing, fallback_canvas_rules; без перезапуска обновляется через mtime-кэш.

### Управление провайдерами и моделями
- Чёткое разделение: провайдеры (инфраструктура) → модели (каталог) → назначение (task/role → model). GUI: `/settings/models` (провайдер-центричный) и `/settings/models/advanced`.
- **Provider instances**: несколько узлов на один kind (Ollama/vLLM/llama.cpp на разных машинах сети); выбор узла по pin модели/живости. Облачные ключи шифруются в БД (Fernet), `.env` — fallback.
- **Режим рассуждения (thinking)**: per-модель в каталоге, прокидывается в провайдеры (Ollama `think`, Anthropic extended); роли агента — tri-state override.
- API `/api/providers/*`: CRUD узлов, `/test`, `/refresh-models` (авто-подтягивание облачных моделей), единая таблица назначений.
- Бэкенды локального AI: Ollama, vLLM, llama.cpp — управляются compose-профилями и API поиска/загрузки моделей (HuggingFace/ModelScope).

### Самообучение: рецепты
- Успешный многошаговый ход агента → черновой `RecipeSkill` (Postgres + Qdrant `recipe_triggers`); при похожем триггере выполняется replay'ем без LLM-планирования.
- Precision-gate и пост-проверка пустого результата; explainable-replay спрашивает подтверждение, пока рецепт не станет доверенным. Approval-gated действия в рецепты не попадают.
- Изолированный кодоген: сгенерированный Python исполняется только в контейнере `skill-runner` (non-root, read-only, без секретов), активация через proposal → decide → promote.

### Автономная работа отдела ИИ
- `AgentCron`: beat-задача ежеминутно выполняет due-расписания headless-ходом агента, результат — в `AgentTask`.
- Реестры `AgentTask`/`AgentTeam`/`AgentCron` управляются через typed API и GUI-панели.

### Почта, интеграции и уведомления
- Несколько IMAP-ящиков с маршрутизацией по назначению (закупки / бухгалтерия / общий); вложения `.eml` разбираются.
- Push-уведомления через ntfy + `DeviceRegistration`; уведомления о решениях по согласованиям и завершении фоновых задач (в т.ч. обучение LoRA).
- Telegram-интеграция.

### Мобильное приложение (Android)
- Capacitor live-WebView (`server.url` на прод-домен: изменения фронта без переустановки APK); native-bridge через `window.Capacitor`.
- Сборка/релиз через GitHub Actions (`.github/workflows/android.yml`), лендинг `/get-app`, скачивание APK с `/download`.

### Наблюдаемость и эксплуатация
- GPU-телеметрия в UI: privileged sidecar (NVML + junction temp GDDR6), `GET /api/local-models/gpu-telemetry`, статус-бар над шапкой агента.
- Prometheus-метрики (в т.ч. время ожидания согласований), health-эндпоинты сервисов и AI-провайдеров.
- Установщик/обновление/бэкап-восстановление (CLI + админ-GUI), авто-миграции в entrypoint.

## Архитектура

```
backend/app/ai/    — агент (orchestrator, роли, память), AI router, студия/LoRA (lora_*, image_generation), парсеры
backend/app/       — FastAPI, SQLAlchemy, Alembic, Celery, OCR/extraction, API, домен
frontend/app/      — Next.js pages (документы, счета, чертежи, студия, таблицы, настройки)
frontend/components/ — React компоненты (в т.ч. studio/LoraTrainingPanel)
aiagent/           — config, prompts, skills, scenarios, routes.yml, comfyui_workflows
backend/data/      — runtime-конфиги: canvas_skill_map.json, agent_config.json, sandbox/staging
infra/             — Docker Compose, Traefik, skill-runner, lora-trainer, generate-skill-registry.py
mobile/android/    — Capacitor-приложение
tools/lora-dataset/ — research-CLI подготовки датасетов LoRA
docs/              — планы, аудит, регрессионные манифесты
scripts/           — проверки, генераторы registry, benchmark tooling
```

Инфраструктурные сервисы:

| Сервис | Роль |
|--------|------|
| PostgreSQL | основная SQL-база, быстрый текстовый слой |
| Redis | брокер Celery, circuit breaker, orchestrator feedback memory, GPU-lock |
| MinIO | объектное хранилище исходных файлов и превью |
| Qdrant | векторное хранилище (эмбеддинги, recipe_triggers) |
| Ollama / vLLM / llama.cpp | локальные LLM: OCR/vision, embeddings, reasoning (compose-профили) |
| ComfyUI | внешний узел генерации/правки изображений (студия), on-prem |
| Celery workers | воркеры: общий (IO/CPU), `gpu` (-c 1, OCR/extract/VLM), `lora` (-c 1, подготовка датасетов и обучение) |
| lora-trainer | изолированный контейнер обучения LoRA (ai-toolkit, запускается по требованию через docker SDK) |
| skill-runner | изолированный executor сгенерированного агентом кода (non-root, read-only) |
| Authentik | self-hosted SSO (OIDC/OAuth2) |
| Traefik | reverse-proxy, TLS |

## Требования

- Linux, macOS или Windows с WSL2.
- Docker и Docker Compose.
- Node.js 20+ для локальной разработки frontend без Docker.
- Python 3.11+ для локальной разработки backend без Docker.
- Ollama, если планируется локальный AI вне контейнера.
- GPU желателен для OCR/vision и больших локальных моделей.

Рекомендуемые локальные модели задаются в `.env`:

```env
OLLAMA_MODEL_OCR=gemma4:e4b
OLLAMA_MODEL_REASONING=gemma4:26b
```

Графическая студия и обучение LoRA (опционально):

```env
COMFYUI_URL=http://host-gateway:8188   # узел ComfyUI для генерации/правки чертежей
```

Токен HuggingFace для обучения на gated-моделях FLUX.2 (klein-9B, dev) берётся
из общего токена **Настройки → Модели → 🤗 HuggingFace** (тот же, что для
загрузки моделей) — задаётся через GUI, не в `.env`. klein-4B — открытая, без
токена.

Студия доступна и без ComfyUI (graceful degradation). Обучение LoRA требует GPU
(проверено на RTX 3090 24 ГБ: Qwen-Image-Edit и FLUX.2 klein-4B) и запускает
изолированный контейнер-тренер по требованию.

## Быстрый старт

### Установка одной строкой (Linux / macOS)

```bash
curl -fsSL https://raw.githubusercontent.com/Zeed80/ai-docs/main/install.sh | bash
```

Установщик сам: определит ОС, проверит Docker/Compose/git **и свободное место**,
клонирует репозиторий, сгенерирует секреты и `infra/.env`, спросит, какие
локальные AI-движки запускать (Ollama / vLLM / llama.cpp — сохраняется в
`COMPOSE_PROFILES`), поднимет стек (миграции БД применяются автоматически в
entrypoint), проверит здоровье **всех** сервисов и ревизию БД, при желании
загрузит модели Ollama и применит дефолты агента. Домен/email/ключи — через TUI
(whiptail) или флаги.

```bash
# Интерактивно из клона репозитория:
./install.sh

# Неинтерактивно (прод):
./install.sh --mode prod --domain example.com --email me@example.com --yes

# Полезные флаги: --mode dev|prod  --no-ai  --reconfigure  --branch <b>  --dir <path>
```

**Локальные AI-движки** управляются compose-профилями (`COMPOSE_PROFILES` в
`infra/.env`): `embedded-ollama`, `embedded-vllm`, `embedded-llamacpp`. Если
Ollama/vLLM запущены отдельно (вне стека) — оставьте профиль пустым. `install.sh`
задаёт значение, `update.sh`/`backup.sh` используют тот же набор.

**Первый админ.** В проде (`AUTH_ENABLED=true`) роли берутся из групп Authentik,
а пользователь с email `INITIAL_ADMIN_EMAIL` автоматически повышается до `admin`
при первом входе, пока ни одного админа ещё нет (роль из БД учитывается при
авторизации каждого запроса). Без админ-роли все `/api/providers/*`,
`/api/admin/*` и т.п. вернут `403` — это видно, например, как «модели нельзя
назначить».

### Обновление

```bash
./update.sh            # авто-бэкап → git pull → пересборка → миграции → рестарт → health-проверка всех сервисов
./update.sh --no-backup --yes
```

Обновление выполняет: бэкап перед обновлением (PostgreSQL + Authentik DB + MinIO +
Qdrant + Redis + `.env`), `git pull --ff-only`, пересборку образов, перезапуск
(alembic-миграции применяются автоматически в entrypoint backend), затем проверку
здоровья backend, **сверку ревизии БД с head** и проверку, что все сервисы
запущены/healthy. При неуспехе печатает команды отката кода и данных.

### Бэкап и восстановление

```bash
bash infra/installer/backup.sh                       # PG + MinIO + Qdrant + Redis + .env → backups/*.tar.gz
bash infra/installer/restore.sh backups/<архив>.tar.gz   # восстановление (деструктивно, с подтверждением)
```

Бэкап доступен и из админ-GUI: `POST /api/admin/maintenance/backup`,
`GET /api/admin/maintenance/backups`, `…/{name}/download` (роль admin).

### Ручная настройка (альтернатива установщику)

```bash
git clone https://github.com/Zeed80/ai-docs.git
cd ai-docs
```

Конфигурация хранится в двух файлах:

| Файл | Назначение |
|------|-----------|
| `.env` (корень) | локальная разработка без Docker |
| `infra/.env` | **Docker Compose** (читается при `docker compose -f infra/docker-compose.yml ...`) |

Перед первым запуском отредактируйте `infra/.env`:

- замените `APP_SECRET_KEY`, `POSTGRES_PASSWORD`, `MINIO_SECRET_KEY`;
- задайте `OLLAMA_URL` (по умолчанию — хост по `host-gateway`);
- сгенерируйте `OAUTH_CLIENT_SECRET` и задайте `AUTH_ENABLED=true` для включения SSO;
- не добавляйте реальные ключи и пароли в git.

Запуск стека через Traefik (основной режим):

```bash
docker compose -f infra/docker-compose.yml up -d
```

Или через Makefile:

```bash
make dev          # запустить
make dev-build    # пересобрать и запустить
make down         # остановить
```

Основные адреса по умолчанию (через Traefik на порту 80):

| Сервис | URL |
|--------|-----|
| Frontend | http://localhost |
| Backend API | http://localhost/api |
| OpenAPI docs | http://localhost/api/docs |
| AI health | http://localhost/api/health/ai |
| Authentik SSO | http://localhost:9100 |
| MinIO Console | http://localhost:9001 |
| Qdrant | http://localhost:6333 |
| Ollama | http://localhost:11434 |

Если frontend открывается с другого компьютера в локальной сети, в `infra/.env` укажите IP машины:

```env
NEXT_PUBLIC_API_URL=http://192.168.1.10
NEXT_PUBLIC_WS_URL=ws://192.168.1.10
AUTHENTIK_EXTERNAL_URL=http://192.168.1.10:9100
OAUTH_REDIRECT_URI_1=http://192.168.1.10/auth/callback
OAUTH_REDIRECT_URI_2=http://192.168.1.10:8000/api/auth/callback
```

## Аутентификация (Authentik SSO)

Система использует [Authentik](https://goauthentik.io/) как self-hosted SSO с OIDC/OAuth2.

### Быстрый старт с аутентификацией

1. Сгенерируйте `OAUTH_CLIENT_SECRET` (один раз):
   ```bash
   python3 -c "import secrets; print(secrets.token_urlsafe(40))"
   ```

2. Добавьте/обновите следующие переменные в `infra/.env`:
   ```env
   AUTH_ENABLED=true
   AUTHENTIK_URL=http://authentik-server:9000
   AUTHENTIK_EXTERNAL_URL=http://localhost:9100
   AUTHENTIK_SLUG=ai-workspace
   OAUTH_CLIENT_ID=ai-workspace
   OAUTH_CLIENT_SECRET=<generated-secret>
   OAUTH_REDIRECT_URI_1=http://localhost/auth/callback
   OAUTH_REDIRECT_URI_2=http://localhost:8000/api/auth/callback
   AUTHENTIK_BOOTSTRAP_EMAIL=admin@company.com
   AUTHENTIK_BOOTSTRAP_PASSWORD=<strong-password>
   ```

3. Запустите стек:
   ```bash
   docker compose -f infra/docker-compose.yml up -d
   ```

4. При первом старте Authentik-воркер автоматически применяет blueprint `infra/authentik/blueprints/ai-workspace.yaml`, который создаёт:
   - группы: `admins`, `managers`, `accountants`, `buyers`, `engineers`, `technologists`
   - OAuth2/OIDC провайдер с JWT-клеймом `groups`
   - приложение `AI Workspace`

5. Войдите в Authentik Admin (`http://localhost:9100`) под учётными данными bootstrap и создайте пользователей.

### Роли пользователей (RBAC)

Роль определяется первой группой Authentik пользователя:

| Группа Authentik | Роль в системе | Права |
|-----------------|---------------|-------|
| `admins` | admin | полный доступ, управление пользователями |
| `managers` | manager | утверждение документов, cases, assignment |
| `accountants` | accountant | счета, финансовые документы |
| `buyers` | buyer | закупки, КП |
| `engineers` | engineer | чертежи, НТД |
| `technologists` | technologist | техпроцессы, нормоконтроль |
| (без группы) | viewer | чтение |

### OAuth2 flow

```
Браузер → GET /api/auth/login → Authentik /authorize/
       → Пользователь логинится в Authentik
       → Authentik redirect → /auth/callback (frontend)
       → Frontend → POST /api/auth/callback (через proxy)
       → Backend обменивает code на token
       → Backend устанавливает httpOnly cookie access_token
       → Redirect на исходную страницу
```

### Важные замечания

- **`infra/.env`** — основной файл конфигурации при запуске через `docker compose -f infra/docker-compose.yml`. Docker Compose берёт `.env` из директории первого `-f` файла (`infra/`), а не из корня проекта.
- **Корневой `.env`** используется только для локальной разработки без Docker.
- В dev-режиме (`AUTH_ENABLED=false`) все запросы автоматически выполняются под admin-пользователем без SSO — удобно для разработки.
- `AUTHENTIK_EXTERNAL_URL` — URL для браузера (может отличаться от `AUTHENTIK_URL`, который используется контейнером backend для JWKS/token).

## Миграции

```bash
make migrate
# или
cd backend && alembic upgrade head
```

Миграции применяются автоматически при старте backend (entrypoint:
`alembic upgrade heads`). Активная цепочка — `backend/migrations/`. Baseline
(`20260424_0001`) строит полную текущую схему через `Base.metadata.create_all`,
поэтому **каждая новая миграция должна быть идемпотентной** (guard через
`inspect(conn).has_table/get_columns/get_indexes` или `IF NOT EXISTS`) — иначе
чистая установка упадёт на «уже существует». Чистая установка проверяется так:
`DROP SCHEMA public CASCADE; CREATE SCHEMA public;` → перезапуск backend → вся
цепочка должна пройти до head без ошибок.

## Проверки

```bash
make test          # unit + API тесты
make e2e           # Playwright
make regression    # регрессия качества извлечения и ролей агента
make agent-test    # AiAgent scenarios на mock skills
make lint          # ruff (backend) + ESLint (frontend)
```

Регенерация skill registry после изменения API:

```bash
make skills
# или
cd backend && python3 ../infra/scripts/generate-skill-registry.py
```

## Работа с документами

1. Загрузите документ в разделе документов или inbox.
2. Система сохранит исходный файл, создаст задачу обработки и запустит OCR/classification/extraction.
3. После обработки документ появится в управлении документами с типом, статусом, извлечениями и связями.
4. Зависимости можно искать, просматривать и редактировать.
5. Удаление документа очищает исходный файл, SQL-записи, связанные извлечения, graph/memory-записи и векторные данные.

## Нормоконтроль НТД

Два режима:

- **автоматическая** проверка после обработки документа;
- **ручная** проверка по кнопке «Проверить на соответствие НТД».

Модуль проверяет документы на соответствие НТД, фиксирует замечания, связывает их с источниками и сохраняет результат в аудитируемой истории.

## Управление памятью агента

```bash
# Найти устаревшие факты (dry_run — без удаления)
POST /api/memory/prune
{
  "scope": "project",
  "kinds": ["chat_turn"],
  "older_than_days": 90,
  "dry_run": true
}

# Закрепить важный факт
POST /api/memory/pin
{
  "title": "Ключевой поставщик",
  "summary": "ООО Ромашка — основной поставщик крепежа",
  "scope": "project"
}
```

## Capability Lifecycle

Агент может предложить новый инструмент:

```
Propose → Sandbox → Decide → Promote
```

1. `POST /api/agent/capabilities/propose` — черновик нового инструмента.
2. `POST /api/agent/capabilities/{id}/sandbox-apply` — валидация, генерация артефактов.
3. `POST /api/agent/capabilities/{id}/decide` — подтверждение или отклонение человеком.
4. `POST /api/agent/capabilities/{id}/promote` — копирование в staging, регистрация скилла в `gateway.yml`.

## Безопасность

- Не коммитьте `.env`, реальные документы, базы данных, дампы, токены и production credentials.
- Для production обязательно смените все значения `changeme`, `dev-secret-key`, `AUTHENTIK_SECRET_KEY`.
- **API полностью защищён** — все эндпоинты кроме `/health`, `/api/auth/*` и WebSocket требуют валидный JWT в httpOnly cookie. В dev-режиме (`AUTH_ENABLED=false`) используется автоматический admin-bypass.
- В production всегда устанавливайте `AUTH_ENABLED=true` и `APP_ENV=production`.
- CORS по умолчанию разрешает только `http://localhost:3000`; для других доменов задайте `CORS_ORIGINS`.
- Решения об утверждении (`cases`, счета, email.send, anomaly.resolve) требуют роль `manager` или `admin`.
- Проверяйте политику хранения документов и журналов аудита.
- Dev-endpoint полной очистки данных не должен быть доступен без защиты в production.
- Protected settings (личность агента, approval gates, autonomy_mode, system prompt и др.) требуют явного подтверждения через proposals — агент не меняет их молча.

## Лицензия

Проект распространяется под лицензией MIT. См. [LICENSE](./LICENSE).
