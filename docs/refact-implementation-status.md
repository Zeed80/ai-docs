# Статус реализации рефакторинга по `refact.md`

Дата: 2026-04-28.

## Выполнено

### P0. Контур безопасного ИИ-сотрудника

- [x] Единый AI-router с registry, local-only политикой для конфиденциальных задач и фильтрацией proposed tool calls.
- [x] Запрет прямых AI-вызовов из бизнес/API-слоя, кроме инфраструктурного health-check.
- [x] Production guard: запрет `AUTO_CREATE_SCHEMA=true` в production.
- [x] Генерация OpenClaw registry из FastAPI OpenAPI.
- [x] Документирован план перехода на официальный open-source OpenClaw.

### P0. Многоступенчатая память документов

- [x] Таблицы графовой памяти: `knowledge_nodes`, `knowledge_edges`, `document_chunks`, `evidence_spans`, `entity_mentions`.
- [x] API графа: узлы, связи, neighborhood, path, chunks, evidence, mentions.
- [x] API гибридной памяти: `memory.search`.
- [x] Детерминированная индексация документа в память при ingest/extraction.
- [x] Переиндексация архива: `memory.reindex`.
- [x] Идемпотентный rebuild автоматического слоя памяти без удаления ручных сущностей.
- [x] Очередь проверки графовых гипотез: `graph.review_list`, `graph.review_decide`.
- [x] Автофиксация потенциальных конфликтов `conflicts_with` по материалам/номерам документов.
- [x] OpenClaw сценарий обслуживания памяти `memory_maintenance`.

### P1. База универсального инженера-технолога

- [x] Таблицы технологического контура:
  - [x] `manufacturing_resources` — станки, инструмент, оснастка, измерительный инструмент, оборудование.
  - [x] `manufacturing_process_plans` — маршрутные техпроцессы по ЕСТД/внутренним стандартам.
  - [x] `manufacturing_operations` — операции, переходы, режимы, контроль, безопасность.
  - [x] `manufacturing_norm_estimates` — нормы времени и допущения.
- [x] API технолога:
  - [x] `tech.resource_list`
  - [x] `tech.resource_create`
  - [x] `tech.operation_template_list`
  - [x] `tech.operation_template_create`
  - [x] `tech.process_plan_list`
  - [x] `tech.process_plan_create`
  - [x] `tech.process_plan_get`
  - [x] `tech.operation_add`
  - [x] `tech.norm_estimate_create`
  - [x] `tech.process_plan_draft_from_document`
  - [x] `tech.process_plan_approve`
  - [x] `tech.process_plan_validate`
  - [x] `tech.norm_estimate_suggest`
  - [x] `tech.norm_estimate_approve`
  - [x] `tech.correction_record`
  - [x] `tech.learning_suggest`
  - [x] `tech.learning_rule_list`
  - [x] `tech.learning_rule_create`
  - [x] `tech.learning_rule_activate`
- [x] Автоматическое построение графа связей:
  - [x] `process_plan -> derived_from -> document`
  - [x] `process_plan -> contains -> operation`
  - [x] `operation -> uses_machine -> machine`
  - [x] `operation -> uses_tool -> tool`
  - [x] `operation -> uses_fixture -> fixture`
- [x] Детерминированный черновик техпроцесса из памяти документа: материал, станки, инструмент, стандарты.
- [x] Approval gate для утверждения техпроцесса.
- [x] Библиотека шаблонов технологических операций.
- [x] Детерминированная проверка технологичности/полноты маршрута с сохранением результатов.
- [x] Черновой расчет режимов резания и норм времени как проверяемые гипотезы с confidence/evidence.
- [x] Approval gate для утверждения норм времени.
- [x] Журнал правок технолога и графовой памяти для дальнейшего обучения.
- [x] Предложения по повторяющимся правкам без автоматического применения.
- [x] Отдельные proposed/active learning rules с approval-gate активацией.

## Ближайшие этапы

### P1. Улучшение качества технолога

- [x] Нормализовать базовые типы обработки: токарная, фрезерная, сверлильная, шлифовальная, механическая обработка, контроль.
- [x] Добавить библиотеку шаблонов операций по российским стандартам оформления техпроцессов.
- [x] Добавить базовую проверку технологичности: материал, операции, станок, инструмент, контроль, нормы.
- [x] Добавить расчет режимов резания и норм времени как отдельные проверяемые гипотезы с confidence/evidence.
- [x] Добавить approval gate для утверждения техпроцесса.
- [x] Добавить approval gate для утверждения норм.

### P1. Самообучение на правках

- [x] Логировать исправления технологических операций, норм, ресурсов и связей графа.
- [x] Генерировать предлагаемые правила из повторяющихся правок.
- [x] Разделить learning loop на предложения и правила, требующие approval для активации.
- [x] Добавить regression-набор документов/чертежей/техпроцессов для контроля качества.

### P2. Переход на официальный OpenClaw

- [x] Поднять официальный OpenClaw Gateway параллельно текущему agent loop.
- [x] Подключить generated registry и проверить deny unknown tools на уровне локального контракта.
- [x] Добавить `make openclaw-contract` для сверки `gateway.yml`, registry, scenarios и approval gates.
- [x] Добавить generated `openclaw/config/gateway.strict.yml` для запуска официального Gateway только с реализованными tools.
- [x] Добавить `openclaw/config/openclaw.official.sample.json` с allowlist в формате официального OpenClaw config.
- [x] Реализовать FastAPI pause/resume callbacks для approval-gated tool calls официального OpenClaw.
- [x] Добавить параллельный Docker Compose overlay и Make targets для официального OpenClaw Gateway на портах `18789/18790`.
- [x] Проверить официальный Gateway в Docker: контейнер `healthy`, `/healthz` возвращает `{"ok":true,"status":"live"}`, dashboard доступен на `http://127.0.0.1:18789/`.
- [ ] Проверить pause/resume на реально запущенном официальном OpenClaw Gateway. Блокер на 2026-04-28: `openclaw agent --json` через живой Gateway не завершился за 45 секунд в текущем окружении после установки runtime-зависимостей провайдеров, поэтому approval-flow нельзя валидировать без рабочего model/provider runtime.
- [x] Перенести WebSocket chat на переключаемый адаптер: legacy FastAPI `/ws/chat` по умолчанию, официальный OpenClaw Gateway через `NEXT_PUBLIC_AGENT_WS_MODE=openclaw`.
- [x] Добавить smoke-тест WebSocket-адаптера official/legacy и fallback-переключения.
- [x] Оставить FastAPI полностью самодостаточным для degraded mode.

### P2. Документная память следующего уровня

- [x] Встроить Qdrant-ready embedding records поверх текущих chunks/evidence.
- [x] Добавить ранжирование graph + text + vector.
- [x] Добавить версионирование знаний: какая версия документа породила узел, связь, chunk, evidence или mention.
- [x] Добавить explain API: ответ агента должен возвращать evidence spans и графовый контекст.

### P2.5. SQL-first retrieval и асинхронные графы

- [x] Зафиксировать SQL-first подход для текстовых документов: текст/структурные поля являются быстрым первичным слоем поиска и проверки.
- [x] Добавить статусный контур построения графов после загрузки/изменения документов: `graph_build_statuses`.
- [x] Добавить full-text search для PostgreSQL и fallback `ILIKE` для dev/sqlite.
- [x] Перевести `memory.search` на режимы `sql`, `sql_vector`, `sql_vector_rerank`, `graph`, `hybrid` с SQL-first default.
- [x] Строить компактный граф для обычных текстов и расширенный граф для чертежей/техпроцессов/НТД.

### P2.6. Универсальные embeddings/rerankers

- [x] Расширить `ModelCapability` параметрами embedding/reranker моделей.
- [x] Добавить базовый discovery возможностей Ollama и API списка capabilities registry.
- [x] Убрать жесткую привязку embedding pipeline к `nomic-embed-text` и размерности `768`.
- [x] Создавать Qdrant collections динамически под модель, размерность и метрику, сохранив legacy `documents` для 768-dim production profile.
- [x] Добавить active embedding profile API и stale marking embedding records при смене embedding-модели.
- [x] Добавить OpenAI-compatible reranker adapter и optional rerank stage для `memory.search`.
- [x] Добавить API/UI статистики и rebuild active embedding records.
- [x] Добавить API/UI реального индексирования queued/stale memory embedding records в Qdrant.
- [x] Добавить optional reranker поверх SQL/vector candidates.

### P2.7. TurboQuant

- [x] Принять архитектурное решение: TurboQuant использовать для vLLM long-context KV-cache, а не как основной механизм embeddings/rerankers.
- [x] Добавить optional vLLM TurboQuant profile с feature flag в AI config/UI.
- [x] Добавить benchmark command/report baseline vs TurboQuant по OpenAI-compatible vLLM endpoints.
- [x] Добавить quality benchmark smoke gate по инженерным regression cases с deterministic term recall и списком missing terms.

### P2.8. Нормоконтроль НТД

- [x] Добавить режим нормоконтроля `manual`/`auto`, default `manual`.
- [x] Добавить SQL-first модели НТД: нормативный документ, версия, пункт, требование.
- [x] Добавить модели запусков проверки и замечаний нормоконтроля.
- [x] Добавить API настройки режима: `GET/PATCH /api/settings/ntd-control`.
- [x] Добавить ручной запуск проверки: `POST /api/documents/{document_id}/ntd-check`.
- [x] Добавить API доступности проверки: `GET /api/documents/{document_id}/ntd-check/availability`.
- [x] Добавить API решения по замечанию: принять, отклонить, не применимо, задача на исправление.
- [x] Подключить auto mode к document extraction pipeline.
- [x] Добавить UI-настройку режима НТД и кнопку “Проверить на соответствие НТД” с backend disabled-причинами.
- [x] Добавить экран НТД для SQL-поиска требований и ручного наполнения базы нормативов.
- [x] Добавить ingestion/index НТД из уже загруженного текстового документа с разбором структуры пунктов.
- [x] Добавить создание карточки НТД из загруженного документа с автоопределением кода/версии и немедленной индексацией.
- [x] Добавить прямой upload PDF/DOCX/TXT НТД без ручного ввода document id через общий document ingest.
- [x] Добавить semantic AI-assisted проверки с evidence spans.
