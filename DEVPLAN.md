# План разработки и To Do — AI Manufacturing Workspace

## Контекст

**Источник требований**: `plan_claude.md` (v2.0, 20 разделов, §1–§20).
**Стек**: `PLAN.md`.
**Проект**: greenfield, кода нет. Разработка: **vibe coding** (solo + AI-ассистенты).

**Принятые решения** (из обсуждения):
1. **OpenClaw** (`v2026.4.5` stable, 349K stars, MIT, TS) — не чат-шлюз, а **AI-сотрудник «Света»** (agent orchestrator). Получает задачи из любого канала, планирует multi-step workflows, вызывает FastAPI как tools, останавливается на approval gates.
2. **Deployment**: on-prem сервер; **не критичные для безопасности AI-задачи могут быть на внешнем API** (Claude API / Groq) — например, reasoning (gemma4:26b замена), style matching, NL-query. OCR/extraction (gemma4:e4b) — только локально (документы конфиденциальны).
3. **Почта**: поддержка **нескольких IMAP-ящиков** (per department / per function).
4. **1С**: экспорт в формат 1С **обязателен** (помимо Excel).
5. **i18n**: **сразу** i18n-ready (русский по умолчанию, архитектура под EN и другие).
6. **Auth**: **Authentik** (self-hosted SSO, OAuth2/OIDC, SCIM, lightweight) — OpenClaw поддерживает OAuth.
7. **AI-сотрудник**: имя **Света** (Sveta).

**Что это означает для архитектуры**:
- OpenClaw — ядро продукта, а не вспомогательный слой. Имя агента: Света.
- FastAPI — «руки» Светы: самодостаточный REST API для всех операций с данными.
- Next.js — UI, который работает **двумя путями**: REST (CRUD, таблицы, review) и WebSocket к OpenClaw (чат, агентные сценарии).
- **Degraded mode**: если OpenClaw недоступен, UI работает через REST — без Светы, но с полным ручным функционалом.
- **Dual AI strategy**: gemma4:e4b локально (конфиденциальные документы), reasoning-модель — **локально или через API** (настраиваемо per task). В проде можно гибко переключать.
- Несколько IMAP-ящиков → routing по ящику (закупки / бухгалтерия / общий).
- 1С-экспорт → дополнительный формат в таблицах и отдельный endpoint.
- i18n → next-intl с русским по умолчанию, все строки через ключи с первого дня.

---

## Технологический стек

| Слой | Технология | Роль |
|---|---|---|
| **Agent (мозг)** | OpenClaw Gateway v2026.4.x (Node.js/TS) | AI-сотрудник «Света»: оркестрация, tool calling, memory, sessions, approval gates, multi-channel |
| **Backend (руки)** | Python / FastAPI | REST API, доменная логика, валидации, все CRUD, audit |
| **Async tasks** | Celery + Redis | Ingest, OCR pipeline, anomaly detection, bulk import, Excel/1C gen |
| **Auth** | Authentik | Self-hosted SSO, OAuth2/OIDC, RBAC |
| **DB** | PostgreSQL | Документы, сущности, audit, normalization rules, price history |
| **Vector store** | Qdrant | Гибридный RAG (dense + BM25), семантический поиск, авто-кластеризация |
| **Object store** | MinIO | Оригиналы файлов, рендеры, версии |
| **AI local** | Ollama + gemma4:e4b | OCR/классификация/извлечение (конфиденциальные данные — только локально) |
| **AI reasoning** | gemma4:26b (local) **или** Claude API (remote) | Reasoning, чат, письма, NL-query. Настраиваемо per task |
| **Frontend** | Next.js (PWA) + next-intl | UI: Inbox, Review, Tables, Chat widget, Command Palette. i18n-ready |
| **Ingress** | Traefik | HTTPS, routing (staging/prod) |
| **PDF** | PyMuPDF | Текст + bbox extraction, рендер страниц |
| **Excel/1C** | openpyxl + 1C-формат | Export/import Excel + выгрузка в 1С |
| **Tests** | pytest + Playwright | Backend + E2E keyboard-first |

---

## Архитектура: OpenClaw как AI-сотрудник

### Потоки данных

```
┌─────────────────────────────────────────────────────────┐
│                     КАНАЛЫ ВХОДА                        │
│  Web UI (chat)  │  Telegram Bot  │  Email (auto-ingest) │
└────────┬────────┴───────┬────────┴──────────┬───────────┘
         │                │                   │
         ▼                ▼                   ▼
┌─────────────────────────────────────────────────────────┐
│              OPENCLAW GATEWAY (Agent Brain)              │
│                                                         │
│  Session Manager ─── Memory Store ─── Approval Gates    │
│        │                                    │           │
│  Agent Loop:  plan → tool_call → observe → next_step   │
│        │                                                │
│  System Prompts (per role, per scenario)                │
│  Skills Registry (FastAPI tools)                        │
│  Channel Router (web / tg / email)                      │
└────────┬────────────────────────────────────────────────┘
         │ HTTP (tool calls)
         ▼
┌─────────────────────────────────────────────────────────┐
│              FASTAPI (Hands — Self-sufficient API)       │
│                                                         │
│  /api/documents/*    /api/invoices/*    /api/email/*    │
│  /api/suppliers/*    /api/anomalies/*   /api/search/*   │
│  /api/approvals/*    /api/tables/*      /api/calendar/* │
│  /api/normalization/* /api/collections/* /api/audit/*   │
│        │                    │                           │
│   Celery Tasks         Ollama Client                    │
│   (heavy async)        (AI inference)                   │
└────────┬────────────────┬───────────────────────────────┘
         │                │
    ┌────▼────┐    ┌──────▼──────┐
    │Postgres │    │ Qdrant│MinIO│
    │ + Redis │    │             │
    └─────────┘    └─────────────┘

┌─────────────────────────────────────────────────────────┐
│              NEXT.JS PWA (Eyes — UI)                     │
│                                                         │
│  REST ──────→ FastAPI  (CRUD, tables, review, export)   │
│  WebSocket ──→ OpenClaw (chat, agent scenarios)         │
│                                                         │
│  Degraded mode: REST-only when OpenClaw is down         │
└─────────────────────────────────────────────────────────┘
```

### Принцип разделения

| Вопрос | Кто отвечает |
|---|---|
| «Что делать дальше?» (планирование, reasoning) | OpenClaw (gemma4:26b) |
| «Как это сделать?» (CRUD, валидация, расчёт) | FastAPI |
| «Тяжёлая работа» (OCR, PDF parse, Excel gen) | Celery |
| «Показать пользователю» | Next.js |
| «Можно ли это сделать?» (approval gate) | OpenClaw спрашивает → человек отвечает через любой канал |

### Degraded mode (FastAPI самодостаточен)

Когда OpenClaw недоступен:

| Функция | Работает? | Через что |
|---|---|---|
| Inbox, карточки, таблицы | Да | REST → FastAPI |
| Side-by-side review | Да | REST → FastAPI |
| Поиск | Да | REST → FastAPI |
| Экспорт/импорт Excel | Да | REST → FastAPI |
| Approve/reject | Да | REST → FastAPI |
| Ручная загрузка файлов | Да | REST → FastAPI |
| Email ingest | Да | Celery (не зависит от OpenClaw) |
| Чат | Нет | — |
| Агентные workflows | Нет | — |
| Auto-draft писем | Нет | — |
| Telegram | Нет | — |
| Proactive alerts | Нет | — |

**Правило**: всё, что пользователь может сделать руками в UI, работает через REST без OpenClaw. OpenClaw добавляет **автоматизацию и интеллект**, но не является единственным путём.

---

## OpenClaw Skills Manifest

Skills — это FastAPI endpoints, зарегистрированные в OpenClaw как tools. Агент вызывает их в рамках multi-step workflows. Каждый skill описан Pydantic-схемой (input/output), что позволяет заменить OpenClaw на другой agent framework без переписывания бизнес-логики.

### Категория: Documents

| Skill ID | Endpoint | Описание | Approval gate |
|---|---|---|---|
| `doc.ingest` | `POST /api/documents/ingest` | Принять файл, сохранить в MinIO, создать Document | Нет |
| `doc.classify` | `POST /api/documents/{id}/classify` | Классифицировать тип документа (gemma4:e4b) | Нет |
| `doc.extract` | `POST /api/documents/{id}/extract` | Извлечь structured data из документа | Нет |
| `doc.get` | `GET /api/documents/{id}` | Получить карточку документа со всеми связями | Нет |
| `doc.list` | `GET /api/documents` | Список документов с фильтрами | Нет |
| `doc.search` | `POST /api/search/documents` | Гибридный поиск (Qdrant + FTS) | Нет |
| `doc.update` | `PATCH /api/documents/{id}` | Обновить поля документа | Нет (audit) |
| `doc.link` | `POST /api/documents/{id}/links` | Связать документ с сущностью | Нет |
| `doc.summarize` | `POST /api/documents/{id}/summarize` | Краткое резюме документа (gemma4:26b) | Нет |

### Категория: Invoices

| Skill ID | Endpoint | Описание | Approval gate |
|---|---|---|---|
| `invoice.extract` | `POST /api/invoices/extract` | Извлечь поля счёта с confidence + bbox | Нет |
| `invoice.validate` | `POST /api/invoices/{id}/validate` | Валидация (арифметика, форматы, checksum) | Нет |
| `invoice.approve` | `POST /api/invoices/{id}/approve` | Утвердить счёт | **Да** |
| `invoice.reject` | `POST /api/invoices/{id}/reject` | Отклонить счёт | **Да** |
| `invoice.compare_prices` | `GET /api/invoices/{id}/price-check` | Сравнить позиции с price history | Нет |

### Категория: Email

| Skill ID | Endpoint | Описание | Approval gate |
|---|---|---|---|
| `email.fetch_new` | `POST /api/email/fetch` | Проверить новые письма (IMAP) | Нет |
| `email.read` | `GET /api/email/{id}` | Прочитать письмо с вложениями | Нет |
| `email.search` | `POST /api/search/emails` | Поиск по письмам | Нет |
| `email.draft` | `POST /api/email/drafts` | Создать черновик письма | Нет |
| `email.style_match` | `POST /api/email/style-analyze` | Проанализировать тон переписки с контрагентом (gemma4:26b) | Нет |
| `email.risk_check` | `POST /api/email/drafts/{id}/risk-check` | Проверить риски перед отправкой | Нет |
| `email.send` | `POST /api/email/drafts/{id}/send` | Отправить письмо (SMTP) | **Да** |
| `email.suggest_template` | `POST /api/email/suggest-template` | Предложить шаблон по контексту | Нет |

### Категория: Suppliers

| Skill ID | Endpoint | Описание | Approval gate |
|---|---|---|---|
| `supplier.get` | `GET /api/suppliers/{id}` | Профиль поставщика со всеми метриками | Нет |
| `supplier.search` | `POST /api/search/suppliers` | Поиск поставщиков (fuzzy) | Нет |
| `supplier.price_history` | `GET /api/suppliers/{id}/price-history` | Price history по позициям | Нет |
| `supplier.check_requisites` | `POST /api/suppliers/{id}/check-requisites` | Сверить реквизиты с последним счётом | Нет |
| `supplier.trust_score` | `GET /api/suppliers/{id}/trust-score` | Получить trust score | Нет |
| `supplier.alerts` | `GET /api/suppliers/{id}/alerts` | Активные алерты (неактивность, рост цен) | Нет |

### Категория: Anomalies

| Skill ID | Endpoint | Описание | Approval gate |
|---|---|---|---|
| `anomaly.check_all` | `POST /api/anomalies/check` | Запустить все детекторы для документа | Нет |
| `anomaly.create_card` | `POST /api/anomalies` | Создать AnomalyCard | Нет |
| `anomaly.resolve` | `POST /api/anomalies/{id}/resolve` | Зарезолвить (accept/reject/false-positive) | **Да** |
| `anomaly.explain` | `GET /api/anomalies/{id}/explain` | Получить объяснение аномалии с контекстом | Нет |

### Категория: Tables & Export

| Skill ID | Endpoint | Описание | Approval gate |
|---|---|---|---|
| `table.query` | `POST /api/tables/query` | Построить выборку по фильтрам | Нет |
| `table.export_excel` | `POST /api/tables/export` | Экспортировать в xlsx | Нет (audit) |
| `table.import_excel` | `POST /api/tables/import` | Начать round-trip import (возвращает diff) | Нет |
| `table.apply_diff` | `POST /api/tables/import/{id}/apply` | Применить diff из round-trip | **Да** |

### Категория: Approvals

| Skill ID | Endpoint | Описание | Approval gate |
|---|---|---|---|
| `approval.request` | `POST /api/approvals` | Запросить подтверждение человеком | **Блокирует агента** |
| `approval.status` | `GET /api/approvals/{id}` | Проверить статус | Нет |
| `approval.list_pending` | `GET /api/approvals/pending` | Список ожидающих | Нет |
| `approval.delegate` | `POST /api/approvals/{id}/delegate` | Делегировать | Нет |

### Категория: Calendar & Reminders

| Skill ID | Endpoint | Описание | Approval gate |
|---|---|---|---|
| `calendar.extract_dates` | `POST /api/calendar/extract` | Извлечь даты из документа | Нет |
| `calendar.set_reminder` | `POST /api/calendar/reminders` | Установить напоминание | Нет |
| `calendar.upcoming` | `GET /api/calendar/upcoming` | Ближайшие события | Нет |

### Категория: Collections

| Skill ID | Endpoint | Описание | Approval gate |
|---|---|---|---|
| `collection.create` | `POST /api/collections` | Создать коллекцию/кейс | Нет |
| `collection.add` | `POST /api/collections/{id}/items` | Добавить документ | Нет |
| `collection.summarize` | `POST /api/collections/{id}/summarize` | Closure summary (gemma4:26b) | Нет |
| `collection.timeline` | `GET /api/collections/{id}/timeline` | Timeline кейса | Нет |

### Категория: Normalization

| Skill ID | Endpoint | Описание | Approval gate |
|---|---|---|---|
| `norm.suggest_rule` | `POST /api/normalization/suggest` | Предложить правило из повторяющихся правок | Нет |
| `norm.apply_rules` | `POST /api/normalization/apply` | Применить активные правила к документу | Нет |
| `norm.list_rules` | `GET /api/normalization/rules` | Список правил | Нет |
| `norm.activate_rule` | `POST /api/normalization/rules/{id}/activate` | Активировать правило | **Да** |

### Категория: NL & Search

| Skill ID | Endpoint | Описание | Approval gate |
|---|---|---|---|
| `search.nl_to_query` | `POST /api/search/nl` | NL → structured query (gemma4:26b) | Нет |
| `search.hybrid` | `POST /api/search/hybrid` | Гибридный поиск (vector + FTS + fuzzy) | Нет |
| `search.similar` | `POST /api/search/similar/{id}` | Похожие документы | Нет |

### Категория: Compare (КП)

| Skill ID | Endpoint | Описание | Approval gate |
|---|---|---|---|
| `compare.create` | `POST /api/compare` | Создать сравнение КП | Нет |
| `compare.align` | `POST /api/compare/{id}/align` | Выровнять позиции по canonical items | Нет |
| `compare.decide` | `POST /api/compare/{id}/decide` | Зафиксировать выбор поставщика + draft писем | **Да** |

### Категория: Canonical Items

| Skill ID | Endpoint | Описание | Approval gate |
|---|---|---|---|
| `canonical.suggest_mapping` | `POST /api/canonical/suggest` | Предложить маппинг InvoiceLine → CanonicalItem | Нет |
| `canonical.confirm_mapping` | `POST /api/canonical/confirm` | Подтвердить маппинг | Нет |
| `canonical.create` | `POST /api/canonical` | Создать новый canonical item | Нет |

**Итого: 52 skills**, из которых **9 имеют approval gates** (блокируют агента до подтверждения человеком).

---

## Agent Scenarios (Multi-step Workflows)

Это ключевые «программы», которые AI-сотрудник выполняет автономно, останавливаясь на approval gates.

### Scenario 1: Email Triage (автоматический, trigger: новое письмо)

```
[trigger: Celery IMAP poll обнаружил новое письмо]
    │
    ▼
1. email.fetch_new → получить письмо + вложения
2. doc.ingest → сохранить вложения как Document
3. doc.classify → определить тип каждого вложения
4. Для каждого invoice:
   ├── invoice.extract → извлечь поля с confidence + bbox
   ├── norm.apply_rules → применить NormalizationRule
   ├── invoice.validate → арифметика, форматы, checksums
   ├── supplier.check_requisites → сверить реквизиты
   ├── anomaly.check_all → дубликаты, цены, реквизиты
   ├── IF anomalies → anomaly.create_card
   └── doc.link → привязать к supplier, project, email thread
5. Роутинг в Inbox:
   ├── IF high confidence + no anomalies → «Требует проверки» (зелёный)
   ├── IF low confidence OR anomalies → «Требует проверки» (красный)
   └── IF duplicate → «Дубликат — проверить»
6. Уведомить ответственного (push / digest)
```

**Время**: 5-15с на один счёт (зависит от сложности).
**Approval gates**: нет (только routing в Inbox для human review).

### Scenario 2: Assisted Review (trigger: пользователь открыл документ в review)

```
[trigger: пользователь открыл side-by-side review]
    │
    ▼
1. doc.get → загрузить документ + extraction
2. invoice.compare_prices → сравнить с price history
3. supplier.get → профиль поставщика (для diff с типовым)
4. Подготовить контекст для review UI:
   ├── auto-focus на слабом поле
   ├── причины низкой уверенности
   ├── diff с типовым счётом
   ├── price sparklines рядом с позициями
   └── anomaly badges если есть
5. [Пользователь проверяет, правит, approve/reject]
6. IF approve:
   ├── norm.suggest_rule → проверить, не пора ли предложить правило
   ├── canonical.suggest_mapping → для новых позиций
   └── Обновить price history
```

**OpenClaw роль**: подготовка контекста. Само review — в UI через REST.

### Scenario 3: Draft Email (trigger: пользователь говорит «подготовь письмо поставщику X»)

```
[trigger: чат-команда или action chip]
    │
    ▼
1. supplier.get → профиль, реквизиты, тон
2. email.search → последние письма с этим контрагентом
3. email.style_match → определить тон (формальный/дружеский)
4. doc.search → связанные документы (счета, КП)
5. email.suggest_template → предложить шаблон по контексту
6. email.draft → сгенерировать черновик (gemma4:26b)
   ├── заполнить переменные из контекста
   ├── применить style matching
   └── прикрепить ссылки на документы
7. email.risk_check → проверить риски
   ├── IF risks found → показать предупреждения, ждать override
8. ══════ APPROVAL GATE ══════
   │ approval.request → «Отправить письмо?»
   │ [показать черновик + risk-check результат]
   │ Каналы: Web UI (inline), Telegram (inline keyboard), push
   │ ╔══════════════════════════╗
   │ ║ Человек: ✅ Утвердить    ║
   │ ║          ✏️ Редактировать ║
   │ ║          ❌ Отклонить     ║
   │ ╚══════════════════════════╝
   ▼
9. IF approved → email.send → отправить через SMTP
10. Записать в audit timeline всех связанных документов
```

**Время**: 3-8с на генерацию + ожидание human approval.
**Работает через**: Web chat, Telegram, Command Palette (trigger).

### Scenario 4: Compare КП (trigger: «сравни предложения по заказу X»)

```
[trigger: чат-команда или UI action]
    │
    ▼
1. doc.search → найти КП по заказу/треду
2. Для каждого КП:
   ├── invoice.extract → извлечь позиции
   └── canonical.suggest_mapping → маппинг к canonical items
3. compare.create → создать объект сравнения
4. compare.align → выровнять позиции
5. supplier.price_history → для каждой позиции × поставщик
6. Показать Compare View в UI (или summary в Telegram)
7. [Пользователь выбирает поставщика]
8. ══════ APPROVAL GATE ══════
   │ compare.decide → зафиксировать выбор
   │ + draft письма (accept + reject per supplier)
   │ + approval.request
   ▼
9. Для каждого draft → email.send
10. Решение + обоснование → audit + collection
```

### Scenario 5: Proactive Follow-up (trigger: Celery beat, приближается срок)

```
[trigger: calendar.upcoming обнаружил approaching deadline]
    │
    ▼
1. calendar.upcoming → «счёт #123, оплата через 3 дня, без движения»
2. doc.get → контекст счёта
3. supplier.get → профиль поставщика
4. email.search → была ли уже переписка по этому счёту?
5. IF нет переписки → email.draft → follow-up напоминание
6. ══════ APPROVAL GATE ══════
   │ approval.request → «Отправить напоминание об оплате?»
   │ [показать черновик + контекст]
   ▼
7. IF approved → email.send
8. calendar.set_reminder → повтор через N дней если всё ещё без движения
```

### Scenario 6: Anomaly Resolution (trigger: пользователь открыл AnomalyCard)

```
[trigger: пользователь спрашивает «что с этой аномалией?»]
    │
    ▼
1. anomaly.explain → развёрнутое объяснение с контекстом:
   ├── «Цена на болт М8 выросла на 35%»
   ├── supplier.price_history → история цен
   ├── doc.search → прошлые счета
   └── email.search → были ли договорённости о цене
2. Предложить действия:
   ├── «Запросить разъяснение у поставщика» → Scenario 3
   ├── «Принять с обоснованием» → anomaly.resolve
   ├── «Отклонить счёт» → invoice.reject
   └── «Ложное срабатывание» → anomaly.resolve (false positive)
3. ══════ APPROVAL GATE ══════
   │ anomaly.resolve → требует подтверждения
   ▼
4. Обновить пороги если false positive
```

### Scenario 7: NL Query + Action (trigger: command palette или чат)

```
[trigger: «счета от акме за март неоплаченные»]
    │
    ▼
1. search.nl_to_query → парсинг:
   {supplier: "Акме*", date: "2025-03", status: "unpaid"}
2. Показать чипы для редактирования
3. table.query → выборка
4. Показать результат + action chips:
   ├── 📊 «Экспорт в Excel» → table.export_excel
   ├── ✉️ «Напоминание всем» → Scenario 3 per supplier
   ├── 📁 «В коллекцию» → collection.add
   └── ✅ «Bulk approve» → batch approval.request
```

### Scenario 8: Smart Ingest (trigger: пользователь скинул файл в чат/Telegram)

```
[trigger: файл в чат / Telegram / drag-drop]
    │
    ▼
1. doc.ingest → сохранить
2. doc.classify → определить тип
3. IF invoice → запустить Scenario 1 (без email части)
4. IF letter → doc.link → связать с последним контекстом чата
5. IF drawing → doc.link + пометить для будущей обработки
6. IF unknown → спросить пользователя «Что это?»
7. Уведомить: «Загружен счёт от ACME №123, отправлен на извлечение. Будет в Inbox через ~10с»
```

---

## Channel Matrix

Что доступно через каждый канал. Определяет scope работы агента.

| Функция | Web UI (REST) | Web Chat (OpenClaw) | Telegram (OpenClaw) | Email (auto) |
|---|---|---|---|---|
| Просмотр документов | ✅ full | ✅ widget + link | 📎 summary + link | — |
| Side-by-side review | ✅ full | — (redirect to UI) | — (redirect to UI) | — |
| Keyboard triage | ✅ full | — | — | — |
| Approve / reject | ✅ full | ✅ inline | ✅ inline keyboard | — |
| Поиск | ✅ full | ✅ NL + chips | ✅ NL (text reply) | — |
| Таблицы + export | ✅ full | ✅ trigger + file | 📎 trigger + file | — |
| Round-trip Excel | ✅ full (diff wizard) | ✅ trigger | — | — |
| Draft email | ✅ editor | ✅ generate + preview | ✅ generate + preview | — |
| Approve email send | ✅ button | ✅ inline | ✅ inline keyboard | — |
| Compare КП | ✅ full view | ✅ trigger + summary | 📎 summary + link | — |
| Upload документ | ✅ drag/drop/paste | ✅ attach file | ✅ attach file | ✅ auto-ingest |
| Anomaly cards | ✅ full | ✅ widget | ✅ summary + actions | — |
| Calendar / reminders | ✅ full | ✅ widget | 📎 list | — |
| Command palette | ✅ Ctrl+K | — (is chat) | — | — |
| Proactive alerts | ✅ push + badge | ✅ message | ✅ message | — |
| Supplier profile | ✅ full page | ✅ widget | 📎 summary | — |
| Collections | ✅ full | ✅ trigger | — | — |
| Settings / admin | ✅ full | — | — | — |
| NormalizationRule | ✅ full | ✅ trigger | — | — |

**Легенда**: ✅ = полная поддержка, 📎 = упрощённый вид + ссылка на web, — = недоступно.

**Telegram-бот** — это тот же OpenClaw agent, но с Telegram-адаптером канала. Он имеет тот же tool set, те же approval gates, ту же memory. Разница только в UI capabilities (нет таблиц, нет split-view).

---

## System Prompts для OpenClaw Agent

### Base system prompt (все каналы, все роли)

```
Ты — AI-ассистент производственного предприятия. Твоё имя: Света.

ПРИНЦИПЫ:
- Ты работаешь с документами, счетами, письмами и поставщиками.
- Ты НИКОГДА не выполняешь внешние действия (отправка писем, изменение данных) без явного подтверждения человеком.
- Ты ВСЕГДА указываешь источники: номера документов, ссылки на карточки, конкретные поля.
- При низкой уверенности ты честно говоришь «я не уверен» и объясняешь почему.
- Ты предпочитаешь строгие данные из БД генеративному тексту.

ДОСТУПНЫЕ ИНСТРУМЕНТЫ:
[автогенерация из Skills Manifest]

APPROVAL GATES:
Действия с пометкой [ТРЕБУЕТ ПОДТВЕРЖДЕНИЯ] выполняются только после
явного «да» / «утвердить» / «отправить» от пользователя.
Ты показываешь превью действия и ждёшь.

ФОРМАТ ОТВЕТОВ:
- Когда показываешь документы → используй виджет карточки с action-кнопками.
- Когда показываешь списки → используй виджет таблицы.
- Когда предлагаешь действие → покажи превью + кнопки [Утвердить] [Редактировать] [Отклонить].
- В Telegram: текст + inline keyboard для действий.
```

### Role-specific additions

**Закупщик:**
```
Ты помогаешь закупщику работать со счетами, КП, поставщиками.
Приоритет: скорость обработки, выявление ценовых аномалий, сравнение предложений.
Всегда показывай price history при обсуждении цен.
При подготовке писем используй деловой тон, принятый в переписке с конкретным поставщиком.
```

**Бухгалтер:**
```
Ты помогаешь бухгалтеру проверять счета, готовить выгрузки, отслеживать сроки оплаты.
Приоритет: точность цифр, соответствие реквизитов, сроки.
РЕЖИМ: строгие данные. Никогда не генерируй числа — только из БД.
При упоминании сумм всегда указывай источник (номер счёта, поле).
```

**Руководитель:**
```
Ты помогаешь руководителю контролировать процесс.
Приоритет: сводки, аномалии, SLA, риски.
Показывай dashboard-виджеты: pending approvals, anomalies, aging.
```

**Технолог:**
```
Ты помогаешь технологу работать с чертежами, техкартами, связанными документами.
Приоритет: точность ссылок на версии, связи между документами.
```

### Scenario-specific prompts (добавляются к base при активации workflow)

**Email drafting:**
```
Сейчас ты готовишь письмо. Правила:
1. Проанализируй тон прошлой переписки (email.style_match).
2. Используй тот же уровень формальности.
3. Заполни все переменные из контекста.
4. После генерации обязательно запусти risk_check.
5. Покажи превью и жди approval.
```

**Anomaly explanation:**
```
Сейчас ты объясняешь аномалию. Правила:
1. Начни с факта: что именно обнаружено.
2. Покажи контекст: price history, прошлые счета, переписку.
3. Предложи 2-3 конкретных действия.
4. Не принимай решение за человека.
```

---

## Roadmap: 7 эпиков

| Эпик | Название | Результат |
|---|---|---|
| 0 | Validation & spikes | Валидация gemma4, прототип review, прототип OpenClaw skill call |
| 1 | Foundation & Ingest | Инфраструктура, модель данных, Inbox keyboard-first, ingest, OpenClaw с базовыми skills |
| 2 | AI Extraction & Review | Extraction pipeline, side-by-side review, review streak, NormalizationRule, OpenClaw Scenario 1+2 |
| 3 | Actions | Draft emails (Scenario 3), tables, round-trip Excel, command palette, risk-check |
| 4 | Search, Suppliers, Price History | Hybrid RAG, supplier profile, canonical items, collections, chat context (Scenario 7+8) |
| 5 | Anomalies, Compare, Calendar | AnomalyCard (Scenario 6), Compare КП (Scenario 4), calendar (Scenario 5), trust score |
| 6 | Expansion | Telegram channel, mobile, proactive agent, CAD, localization |

MVP = эпики 0–3 + часть 4 (supplier profile + price history).

---

## Эпик 0 — Validation & Spikes

**Цель**: снять технические риски до начала продуктива.

**Deliverables**:
- Dataset ≥100 реальных счетов с разметкой полей (ground truth).
- Оценка gemma4:e4b: precision/recall по полям, стабильность bbox.
- Сайзинг: e4b + 26b в 24GB VRAM, параллельная нагрузка.
- **Spike OpenClaw**: развернуть gateway, зарегистрировать 3 mock-skills (doc.get, doc.search, email.draft), выполнить agent loop через chat.
- **Spike approval gate**: agent вызывает skill → останавливается → получает approve через WebSocket → продолжает.
- UX-прототип keyboard review (статический Next.js, 5-10 документов).
- Прототип command palette с mock-данными.
- Решение по каналу OpenClaw (stable/beta), зафиксировать в ADR.
- Документ «подтверждённые и опровергнутые гипотезы».

**Выходные критерии**:
- ≥85% полей извлекаются корректно, bbox стабильны в ≥90% случаев.
- Латентность extraction ≤10с на целевом железе.
- OpenClaw agent loop работает: chat → tool call → observe → approval gate → continue.
- Review управляется только с клавиатуры.

**To Do**:
- [ ] Собрать dataset ≥100 счетов, разметить поля.
- [ ] Ollama: развернуть gemma4:e4b + gemma4:26b, замерить VRAM.
- [ ] **Spike dual AI: тот же extraction prompt через gemma4:26b локально vs Claude API — сравнить quality + latency.**
- [ ] Spike: extraction скрипт → structured output → оценка precision/recall.
- [ ] Spike: bbox привязка → визуальная проверка стабильности.
- [ ] Spike: latency под параллельной нагрузкой (2 запроса одновременно).
- [ ] **Spike: OpenClaw v2026.4.x onboard → зарегистрировать 3 mock skills → agent loop через chat.**
- [ ] **Spike: approval gate в OpenClaw → agent stops → user approves → agent continues.**
- [ ] **Spike: OpenClaw Telegram extension → отправить файл → agent получает.**
- [ ] Прототип keyboard-review (Next.js, статика, 5-10 docs).
- [ ] Прототип command palette (mock).
- [ ] ADR: канал OpenClaw, способ регистрации skills.
- [ ] Документ гипотез и результатов.

---

## Эпик 1 — Foundation & Ingest

**Цель**: инфраструктура, данные, Inbox, ingest, OpenClaw с базовым agent loop.

### 1.1 Инфраструктура
- `docker-compose.yml`: Postgres, Redis, Qdrant, MinIO, Ollama, Traefik, FastAPI, Celery, **OpenClaw Gateway**, Next.js.
- Traefik + HTTPS.
- `.env.example`, healthchecks, Alembic миграции.
- Базовый CI.

### 1.2 Модель данных (PostgreSQL)
MVP-набор:
- `Document`, `DocumentVersion`, `DocumentExtraction`, `ExtractionField`.
- `Invoice`, `InvoiceLine`.
- `Party`, `SupplierProfile`.
- `EmailThread`, `EmailMessage`.
- `AuditLog`, `AuditTimelineEvent`.
- `DraftAction`, `Approval`.
- `Snooze`, `Handover`, `Comment`.
- `SavedView`, `SavedQuery`.

Зарезервировать в миграциях: `CanonicalItem`, `PriceHistoryEntry`, `AnomalyCard`, `NormalizationRule`, `Reminder`, `TrustScore`, `CalendarEvent`, `AutoApprovalRule`, `CapabilityEntry`.

### 1.3 FastAPI — базовые endpoints (первые skills)
Реализовать endpoints, которые станут первыми OpenClaw skills:
- `doc.ingest` — POST /api/documents/ingest
- `doc.get` — GET /api/documents/{id}
- `doc.list` — GET /api/documents
- `doc.update` — PATCH /api/documents/{id}
- `doc.link` — POST /api/documents/{id}/links
- `email.fetch_new` — POST /api/email/fetch
- `email.read` — GET /api/email/{id}
- `approval.request` — POST /api/approvals
- `approval.status` — GET /api/approvals/{id}
- `approval.list_pending` — GET /api/approvals/pending

Каждый endpoint: Pydantic input/output, audit log, тесты.

### 1.4 Ingest pipeline (Celery)
- **Multi-IMAP poller**: несколько ящиков (закупки, бухгалтерия, общий), каждый с настраиваемым routing (тип документа, ответственная роль).
- HTTP upload (multi), URL ingest, paste, folder drop, Outlook drop.
- SHA-256 dedup.
- Auto-linking (thread, subject, hash, mention).
- Статус → `queued` для AI (эпик 2) или `needs_review`.

### 1.4a Auth (Authentik)
- Authentik контейнер в docker-compose.
- OAuth2/OIDC flow: Authentik → OpenClaw + FastAPI + Next.js.
- Маппинг ролей Authentik → системные роли (закупщик, бухгалтер, руководитель, технолог, администратор).
- JWT validation в FastAPI middleware.
- RBAC middleware на endpoints.

### 1.5 OpenClaw Gateway — agent foundation
- Deploy OpenClaw в docker-compose.
- **Зарегистрировать базовые skills** из §1.3 как OpenClaw tools (JSON schema из Pydantic).
- **Base system prompt** (см. секцию System Prompts).
- WebSocket endpoint для Next.js chat widget.
- Session management + memory store (Redis-backed).
- **Approval gate mechanism**: agent вызывает `approval.request` → OpenClaw приостанавливает execution → ждёт callback от FastAPI (user approved/rejected) → продолжает.
- Каталог skills UI (список зарегистрированных tools, их статусы).
- **Stub Scenario 8** (Smart Ingest): файл в чат → doc.ingest → doc.classify (stub) → уведомление.

### 1.6 Inbox UI (Next.js) — keyboard-first
- Layout, sidebar, Inbox list.
- «Моя» / «Команды», фильтры, smart batching.
- Keyboard: j/k/Enter/e/r/c/a/s/x/?
- Snooze, empty state, SLA.
- **Chat widget** (sidebar или panel): WebSocket → OpenClaw.
- **Degraded mode indicator**: если OpenClaw недоступен → badge «Автоматические функции временно недоступны», chat widget disabled.

### 1.7 Карточка документа
- PDF viewer (pdf.js), метаданные, timeline, комментарии, связи, quick actions.

**Критерии приёмки**:
- Письмо с вложением → Inbox за ≤30с с auto-linking.
- Все ingest-каналы работают.
- Дубликаты помечаются.
- Inbox проходится с клавиатуры.
- **Через чат можно сказать «покажи документ #123» → агент вызывает doc.get → показывает виджет.**
- **Через чат можно загрузить файл → agent выполняет Scenario 8 (stub).**
- **Approval gate работает: agent останавливается, ждёт, продолжает.**
- При отключении OpenClaw — UI работает через REST (degraded mode).

**To Do**:

Инфраструктура:
- [ ] docker-compose.yml (все сервисы: Postgres, Redis, Qdrant, MinIO, Ollama, OpenClaw, FastAPI, Celery, Next.js, Traefik, **Authentik**).
- [ ] Traefik + HTTPS.
- [ ] .env.example.
- [ ] **Authentik: контейнер, начальная конфигурация, OAuth2 flow.**
- [ ] **Authentik → FastAPI JWT middleware.**
- [ ] **Authentik → OpenClaw OAuth integration.**
- [ ] **Authentik → Next.js auth (next-auth или аналог).**
- [ ] **Маппинг ролей Authentik → системные роли.**
- [ ] FastAPI скелет (health, settings, structured logging).
- [ ] Celery worker + beat.
- [ ] Alembic init.
- [ ] Next.js скелет (layout, routing, **next-intl с RU по умолчанию**).
- [ ] CI pipeline (lint + tests + build).
- [ ] Pre-commit hooks.

Модель данных:
- [ ] Миграция: Document, DocumentVersion, DocumentExtraction, ExtractionField.
- [ ] Миграция: Invoice, InvoiceLine.
- [ ] Миграция: Party, SupplierProfile.
- [ ] Миграция: EmailThread, EmailMessage.
- [ ] Миграция: AuditLog, AuditTimelineEvent.
- [ ] Миграция: DraftAction, Approval.
- [ ] Миграция: Snooze, Handover, Comment.
- [ ] Миграция: SavedView, SavedQuery.
- [ ] SQLAlchemy models + Pydantic schemas.

FastAPI endpoints (первые skills):
- [ ] POST /api/documents/ingest (doc.ingest).
- [ ] GET /api/documents/{id} (doc.get).
- [ ] GET /api/documents (doc.list).
- [ ] PATCH /api/documents/{id} (doc.update).
- [ ] POST /api/documents/{id}/links (doc.link).
- [ ] POST /api/email/fetch (email.fetch_new).
- [ ] GET /api/email/{id} (email.read).
- [ ] POST /api/approvals (approval.request).
- [ ] GET /api/approvals/{id} (approval.status).
- [ ] GET /api/approvals/pending (approval.list_pending).
- [ ] Pydantic schemas для всех (= tool contracts).
- [ ] Unit + API тесты.

Ingest:
- [ ] **Multi-IMAP poller (несколько ящиков, routing по ящику → тип/роль).**
- [ ] **Настройка IMAP ящиков через admin UI.**
- [ ] HTTP upload (single + multi).
- [ ] URL ingest.
- [ ] Paste endpoint.
- [ ] Folder drop (client + batch endpoint).
- [ ] Outlook drop (client MIME parser).
- [ ] SHA-256 dedup.
- [ ] Auto-linking эвристики.
- [ ] Идемпотентность + retry.

OpenClaw:
- [ ] **Deploy OpenClaw Gateway в compose.**
- [ ] **Зарегистрировать 10 базовых skills (из §1.3) как tools.**
- [ ] **Base system prompt.**
- [ ] **WebSocket endpoint + фронт chat widget.**
- [ ] **Session + memory (Redis).**
- [ ] **Approval gate: pause → callback → resume.**
- [ ] **Каталог skills UI.**
- [ ] **Stub Scenario 8 (Smart Ingest через чат).**

Frontend:
- [ ] Layout + sidebar.
- [ ] Inbox list (virtualized).
- [ ] Фильтры, «Моя»/«Команды», smart batching.
- [ ] Keyboard shortcuts.
- [ ] Snooze, empty state, SLA.
- [ ] Chat widget (WebSocket → OpenClaw).
- [ ] Degraded mode indicator.
- [ ] Карточка документа (PDF, metadata, timeline, comments, links).

Тесты:
- [ ] Unit: hash dedup, auto-linking.
- [ ] API: все endpoints.
- [ ] E2E: drop файла → Inbox → карточка.
- [ ] E2E: keyboard Inbox навигация.
- [ ] **E2E: чат «покажи документ» → widget.**
- [ ] **E2E: approval gate end-to-end.**
- [ ] **E2E: degraded mode (OpenClaw down → UI через REST).**

---

## Эпик 2 — AI Extraction & Review

**Цель**: extraction pipeline, side-by-side review, review streak, NormalizationRule, **Scenario 1 (Email Triage) и Scenario 2 (Assisted Review) полностью работают**.

### 2.1 Ollama pipeline
- Контейнер с pre-pull моделей.
- Python client с retry/timeout/circuit breaker.
- VRAM monitoring.

### 2.2 Extraction (Celery task + новые skills)
- PyMuPDF preprocessing (текст + bbox + PNG рендер).
- Классификация: gemma4:e4b → doc.classify skill.
- Извлечение: gemma4:e4b structured output → invoice.extract skill.
- Confidence: self-report + детерминированные проверки → invoice.validate skill.
- NormalizationRule применение → norm.apply_rules skill.
- Bbox binding.

**Новые skills для регистрации в OpenClaw**:
- `doc.classify`
- `doc.extract`
- `doc.summarize`
- `invoice.extract`
- `invoice.validate`
- `invoice.compare_prices`
- `norm.apply_rules`
- `norm.suggest_rule`
- `norm.list_rules`
- `norm.activate_rule`

### 2.3 Side-by-side Review UI
- Split-view, двусторонняя подсветка, auto-focus, inline confidence reasons.
- Confidence heatmap, undo/redo, per-field comments.
- Keyboard: Tab, Shift+Enter, Shift+Backspace, Ctrl+Z/Y, g, ?

### 2.4 Review streak
- Prefetch + auto-next + counter.

### 2.5 Diff с типовым счётом поставщика.

### 2.6 NormalizationRule (trust loop)
- Детектор повторяющихся правок → proposed rule.
- UI «сделать правилом», settings.
- Применение в pipeline до AI.
- Rollback.

### 2.7 OpenClaw: Scenario 1 + 2
- **Scenario 1 (Email Triage)** полностью: email.fetch_new → doc.ingest → doc.classify → invoice.extract → norm.apply_rules → invoice.validate → anomaly.check_all (stub) → doc.link → route to Inbox.
- **Scenario 2 (Assisted Review)** контекст: при открытии review OpenClaw подготавливает контекст (price comparison, supplier diff).

**Критерии приёмки**:
- ≥80% полей корректно, bbox стабильны.
- Двусторонняя подсветка работает.
- Review streak: 10+ счетов без мыши.
- NormalizationRule: тройная правка → предложение → accept → применение.
- **Scenario 1**: новое письмо → через 15с документ в Inbox с извлечёнными полями, confidence, и причинами.
- **Scenario 2**: при открытии review — поле с price history и supplier diff уже загружены.

**To Do**:

Ollama & extraction:
- [ ] Ollama контейнер + pre-pull.
- [ ] Python client (retry, timeout, circuit breaker).
- [ ] VRAM мониторинг.
- [ ] PyMuPDF preprocessing.
- [ ] Классификация (gemma4:e4b).
- [ ] Извлечение счёта (structured output).
- [ ] Confidence (self-report + правила).
- [ ] confidence_reason enum и правила.
- [ ] Арифметические проверки.
- [ ] Формат-проверки (ИНН, IBAN, дата).
- [ ] Bbox binding.
- [ ] NormalizationRule применение в pipeline.
- [ ] Celery task end-to-end.

Новые FastAPI endpoints (skills):
- [ ] POST /api/documents/{id}/classify (doc.classify).
- [ ] POST /api/documents/{id}/extract (doc.extract).
- [ ] POST /api/documents/{id}/summarize (doc.summarize).
- [ ] POST /api/invoices/extract (invoice.extract).
- [ ] POST /api/invoices/{id}/validate (invoice.validate).
- [ ] GET /api/invoices/{id}/price-check (invoice.compare_prices).
- [ ] POST /api/normalization/apply (norm.apply_rules).
- [ ] POST /api/normalization/suggest (norm.suggest_rule).
- [ ] GET /api/normalization/rules (norm.list_rules).
- [ ] POST /api/normalization/rules/{id}/activate (norm.activate_rule).
- [ ] **Зарегистрировать все новые skills в OpenClaw.**

Review UI:
- [ ] Split-view layout.
- [ ] Двусторонняя подсветка поле ↔ bbox.
- [ ] Auto-focus на слабом поле.
- [ ] Inline confidence_reason.
- [ ] Confidence heatmap (toggle).
- [ ] Undo/redo.
- [ ] Per-field комментарии.
- [ ] Keyboard shortcuts.
- [ ] Review streak (prefetch + counter).
- [ ] Diff с типовым счётом.

NormalizationRule:
- [ ] Миграция таблицы.
- [ ] Детектор повторяющихся правок.
- [ ] Генерация proposed rule.
- [ ] UI «сделать правилом».
- [ ] Settings: управление правилами.
- [ ] Применение в pipeline (до AI).
- [ ] Метрика обучения виджет.
- [ ] Rollback + история.

OpenClaw scenarios:
- [ ] **Scenario 1 (Email Triage): полная цепочка skills.**
- [ ] **Scenario 2 (Assisted Review): контекстная подготовка.**
- [ ] **Тесты: E2E scenario 1 end-to-end.**

Тесты:
- [ ] Regression на dataset эпика 0.
- [ ] E2E: ingest → review → approve → next.
- [ ] E2E: тройная правка → rule → accept → apply.
- [ ] **E2E: письмо пришло → Scenario 1 → документ в Inbox с полями за ≤15с.**

---

## Эпик 3 — Actions: Emails, Tables, Excel, Command Palette

**Цель**: рабочий инструмент. **Scenario 3 (Draft Email) полностью работает**.

### 3.1 Command Palette (Ctrl+K)
- Global overlay, 3 режима (nav / search / NL).
- Action chips, история, registry.

### 3.2 NL → structured query
- search.nl_to_query skill (gemma4:26b).
- Preview-чипы, fallback на fuzzy.

### 3.3 Таблицы
- TanStack Table, pinned columns, группировки, формулы.
- SavedView, inline-edit, пакетные действия, NL-фильтр.

### 3.4 Excel/1С Export + Round-trip
- Excel export: openpyxl, скрытые ID, audit.
- **1С-экспорт**: отдельный endpoint, формат CommerceML XML (или JSON — уточнить по ходу). Маппинг полей Invoice → 1С-номенклатура.
- Excel import: diff builder, Diff Wizard UI, через approval.

### 3.5 Email Workspace & Draft Flow

**Новые skills для OpenClaw**:
- `email.draft`
- `email.style_match`
- `email.risk_check`
- `email.send`
- `email.suggest_template`
- `email.search`
- `search.nl_to_query`
- `search.hybrid`
- `table.query`
- `table.export_excel`
- `table.import_excel`
- `table.apply_diff`

**Scenario 3 (Draft Email)** полностью:
- supplier.get → email.search → email.style_match → email.suggest_template → email.draft → email.risk_check → approval.request → email.send.
- Risk-check детекторы: внешний домен, сумма без вложения, получатель вне карточки, язык, ключевые слова.
- Template library (отраслевые шаблоны).

**Scenario 7 (NL Query + Action)** через command palette:
- search.nl_to_query → table.query → action chips.

### 3.6 Audit Timeline расширение
- Человекочитаемые события, аватары, фильтры, clickable переходы.

**Критерии приёмки**:
- Ctrl+K → NL → чипсы → результат → action.
- Inline-edit с audit.
- Round-trip Excel: export → edit → import → diff → apply.
- **Scenario 3**: «подготовь письмо ACME» в чате → черновик со style matching → risk-check → approve → send.
- Risk-check блокирует при рисках.

**To Do**:

Command Palette:
- [ ] Overlay + global hotkey.
- [ ] Registry действий.
- [ ] Три режима.
- [ ] Fuzzy match.
- [ ] Action chips.
- [ ] История.

NL query:
- [ ] POST /api/search/nl (search.nl_to_query) endpoint.
- [ ] POST /api/search/hybrid (search.hybrid) endpoint.
- [ ] Pydantic schema фильтров.
- [ ] Frontend чипы.
- [ ] Fallback.
- [ ] **Зарегистрировать skills в OpenClaw.**

Таблицы:
- [ ] TanStack Table.
- [ ] Колонки, сортировка, фильтрация.
- [ ] Pinned, группировки, формулы.
- [ ] SavedView CRUD + share-link.
- [ ] Inline-edit через PATCH.
- [ ] Пакетные действия.
- [ ] NL-фильтр.
- [ ] Hover-preview.
- [ ] POST /api/tables/query (table.query) endpoint.

Excel & 1С:
- [ ] POST /api/tables/export (table.export_excel) — Celery task.
- [ ] **POST /api/tables/export-1c — экспорт в формат 1С (CommerceML XML).**
- [ ] **Маппинг Invoice полей → 1С-номенклатура.**
- [ ] POST /api/tables/import (table.import_excel) endpoint.
- [ ] POST /api/tables/import/{id}/apply (table.apply_diff) endpoint.
- [ ] Diff builder.
- [ ] Diff Wizard UI.
- [ ] Audit записи.
- [ ] **Зарегистрировать skills в OpenClaw (включая 1С export).**

Email workspace:
- [ ] Thread viewer.
- [ ] Attachment preview inline.
- [ ] Rich text editor (TipTap).
- [ ] POST /api/email/drafts (email.draft) endpoint.
- [ ] POST /api/email/style-analyze (email.style_match) endpoint.
- [ ] POST /api/email/drafts/{id}/risk-check (email.risk_check) endpoint.
- [ ] POST /api/email/drafts/{id}/send (email.send) endpoint.
- [ ] POST /api/email/suggest-template (email.suggest_template) endpoint.
- [ ] POST /api/search/emails (email.search) endpoint.
- [ ] Template library (шаблоны с переменными).
- [ ] Risk-check детекторы (все 5).
- [ ] Risk-check UI с override + audit.
- [ ] Сверка расхождений счёт↔письмо.
- [ ] **Зарегистрировать все email skills в OpenClaw.**
- [ ] **Scenario 3 (Draft Email): полная цепочка.**
- [ ] **Email drafting system prompt.**

Scenario 7 (NL Query):
- [ ] **Через command palette: NL → skills → action chips.**

Тесты:
- [ ] E2E: Ctrl+K → NL → чипсы → результат.
- [ ] E2E: round-trip Excel.
- [ ] E2E: risk-check блокирует → override → send.
- [ ] **E2E: Scenario 3 через чат end-to-end.**
- [ ] Визуальная оценка style matching (5 кейсов).

---

## Эпик 4 — Search, Suppliers, Price History, Collections, Chat Context

**Цель**: связи, рабочий кабинет закупщика, **Scenario 7 (NL Query) и Scenario 8 (Smart Ingest) полностью**.

### 4.1 Hybrid Search
- Qdrant embeddings + Postgres FTS + fusion ranking.
- Fuzzy (trigram), SavedQuery + алерты, контекстный scope.

### 4.2 Supplier Profile
- Все блоки §8.2.H plan_claude.md.
- Aggregation endpoints (cached).

### 4.3 Canonical Items & Price History
- CanonicalItem, авто-кластеризация (embeddings), UI mapping.
- PriceHistoryEntry на approve.
- Sparklines в карточке и профиле.

**Новые skills**:
- `supplier.get`, `supplier.search`, `supplier.price_history`, `supplier.check_requisites`, `supplier.trust_score`, `supplier.alerts`
- `canonical.suggest_mapping`, `canonical.confirm_mapping`, `canonical.create`
- `search.similar`
- `collection.create`, `collection.add`, `collection.summarize`, `collection.timeline`

### 4.4 Collections
- CRUD, auto-suggest, timeline, closure summary, scoped search.

### 4.5 Chat context improvements
- Pin context, action chips, strict/reasoning toggle, shareable link.

### 4.6 OpenClaw Scenarios
- **Scenario 7 (NL Query + Action)**: полная версия с supplier search, price history.
- **Scenario 8 (Smart Ingest)**: полная версия с классификацией и auto-routing.

**Критерии приёмки**:
- Поиск с опечатками работает.
- Профиль поставщика — все блоки с реальными данными.
- Price history обновляется при approve.
- Canonical items авто-кластеризуются.
- **«Покажи price history болта М8 у ACME» в чате → sparkline + контекст.**
- **Файл в Telegram → Scenario 8 → уведомление «счёт от ACME загружен».**
- Collection timeline + closure summary.
- Chat share-link работает.

**To Do**:

Search:
- [ ] Qdrant коллекции + embeddings pipeline.
- [ ] Postgres FTS.
- [ ] Fusion ranking.
- [ ] Trigram fuzzy.
- [ ] SavedQuery + алерты (Celery beat).
- [ ] Контекстный scope.
- [ ] POST /api/search/similar/{id} (search.similar).

Supplier:
- [ ] GET /api/suppliers/{id} (supplier.get).
- [ ] POST /api/search/suppliers (supplier.search).
- [ ] GET /api/suppliers/{id}/price-history (supplier.price_history).
- [ ] POST /api/suppliers/{id}/check-requisites (supplier.check_requisites).
- [ ] GET /api/suppliers/{id}/trust-score (supplier.trust_score).
- [ ] GET /api/suppliers/{id}/alerts (supplier.alerts).
- [ ] UI профиля (все блоки).
- [ ] Aggregation endpoints (cached).
- [ ] **Зарегистрировать supplier skills в OpenClaw.**

Canonical & Price History:
- [ ] Миграции CanonicalItem, PriceHistoryEntry.
- [ ] Embeddings pipeline.
- [ ] Авто-кластеризация.
- [ ] POST /api/canonical/suggest (canonical.suggest_mapping).
- [ ] POST /api/canonical/confirm (canonical.confirm_mapping).
- [ ] POST /api/canonical (canonical.create).
- [ ] PriceHistoryEntry на approve.
- [ ] Sparkline renderer.
- [ ] **Зарегистрировать canonical skills в OpenClaw.**

Collections:
- [ ] POST /api/collections (collection.create).
- [ ] POST /api/collections/{id}/items (collection.add).
- [ ] POST /api/collections/{id}/summarize (collection.summarize).
- [ ] GET /api/collections/{id}/timeline (collection.timeline).
- [ ] Auto-suggest.
- [ ] UI (CRUD, timeline, closure).
- [ ] Scoped search.
- [ ] **Зарегистрировать collection skills.**

Chat:
- [ ] Pin context mechanism.
- [ ] Action chips в ответах.
- [ ] Strict/reasoning toggle + system prompt switch.
- [ ] Shareable link.

OpenClaw scenarios:
- [ ] **Scenario 7 полностью (NL → search → supplier → price history → actions).**
- [ ] **Scenario 8 полностью (Smart Ingest с classify + routing).**

Тесты:
- [ ] Regression: поиск с опечатками.
- [ ] E2E: canonical autoclustering → confirm → price history.
- [ ] E2E: collection → closure → summary.
- [ ] **E2E: Scenario 7 через чат.**
- [ ] **E2E: Scenario 8 через chat file upload.**

---

## Эпик 5 — Anomalies, Compare КП, Calendar, Trust Score

**Цель**: проактивная защита. **Scenario 4 (Compare), 5 (Follow-up), 6 (Anomaly) полностью работают**.

### 5.1 Anomaly Detection
Все детекторы + AnomalyCard как объект Inbox.

**Новые skills**:
- `anomaly.check_all`, `anomaly.create_card`, `anomaly.resolve`, `anomaly.explain`
- `compare.create`, `compare.align`, `compare.decide`
- `calendar.extract_dates`, `calendar.set_reminder`, `calendar.upcoming`

### 5.2 Compare View (КП) + Scenario 4.

### 5.3 Calendar & Reminders + Scenario 5 (Proactive Follow-up).

### 5.4 Trust Score & Auto Approval.

### 5.5 Approval improvements (bulk, delegation, SLA, escalation).

### 5.6 Scenario 6 (Anomaly Resolution) полностью.

**Обновление Scenario 1**: теперь anomaly.check_all → реальные детекторы (не stub).

**Критерии приёмки**:
- Anomaly card в Inbox при тест-сценариях.
- **Scenario 4**: «сравни КП по заказу-42» → compare view → выбор → drafts.
- **Scenario 5**: напоминание за 3 дня → follow-up draft → approve → send.
- **Scenario 6**: «объясни аномалию» → контекст + действия.
- Compare view с sparklines.
- Trust score и auto-approval (opt-in).
- Bulk-approve с diff.

**To Do**:

Anomaly:
- [ ] Миграция AnomalyCard.
- [ ] POST /api/anomalies/check (anomaly.check_all).
- [ ] POST /api/anomalies (anomaly.create_card).
- [ ] POST /api/anomalies/{id}/resolve (anomaly.resolve).
- [ ] GET /api/anomalies/{id}/explain (anomaly.explain).
- [ ] Детектор дубликатов (hash + key).
- [ ] Детектор нового поставщика.
- [ ] Детектор смены реквизитов.
- [ ] Детектор ценового скачка.
- [ ] Детектор расхождений счёт↔письмо.
- [ ] Детектор «не в каноническом справочнике».
- [ ] Celery task после extraction.
- [ ] UI AnomalyCard в Inbox.
- [ ] False positive → update пороги.
- [ ] **Зарегистрировать anomaly skills в OpenClaw.**
- [ ] **Scenario 6 полностью.**
- [ ] **Обновить Scenario 1: реальные anomaly детекторы.**

Compare КП:
- [ ] POST /api/compare (compare.create).
- [ ] POST /api/compare/{id}/align (compare.align).
- [ ] POST /api/compare/{id}/decide (compare.decide).
- [ ] UI compare view (выравнивание, подсветка, sparklines).
- [ ] «Лучшая комбинация».
- [ ] One-click → draft писем.
- [ ] Audit + collection.
- [ ] **Зарегистрировать compare skills.**
- [ ] **Scenario 4 полностью.**

Calendar:
- [ ] Миграции CalendarEvent, Reminder.
- [ ] POST /api/calendar/extract (calendar.extract_dates).
- [ ] POST /api/calendar/reminders (calendar.set_reminder).
- [ ] GET /api/calendar/upcoming (calendar.upcoming).
- [ ] Date extraction из счетов/писем.
- [ ] Calendar UI.
- [ ] Reminder scheduler (Celery beat).
- [ ] Auto follow-up draft.
- [ ] **Зарегистрировать calendar skills.**
- [ ] **Scenario 5 полностью.**

Trust Score & Auto Approval:
- [ ] Миграции TrustScore, AutoApprovalRule.
- [ ] Расчёт trust-score.
- [ ] UI в профиле.
- [ ] Settings для AutoApprovalRule.
- [ ] Применение в pipeline + audit.

Approval improvements:
- [ ] Bulk-approve с diff UI.
- [ ] Delegation model + UI.
- [ ] SLA / aging.
- [ ] Escalation (Celery beat).

Тесты:
- [ ] Anomaly test fixtures (подмена реквизитов, скачок, дубликат).
- [ ] E2E: compare КП → выбор → drafts.
- [ ] E2E: reminder → follow-up draft.
- [ ] **E2E: Scenario 4 через чат.**
- [ ] **E2E: Scenario 5 end-to-end.**
- [ ] **E2E: Scenario 6 через чат.**

---

## Эпик 6 — Expansion

**Не MVP**. Детализация по готовности.

### 6.1 Telegram channel
- Telegram Bot API adapter для OpenClaw.
- Тот же agent, те же skills, другой rendering (текст + inline keyboard).
- File upload через Telegram → Scenario 8.
- Approve через inline keyboard.
- Proactive alerts в Telegram.

### 6.2 Proactive agent
- Celery beat job: периодически проверяет anomalies, approaching deadlines, stale approvals.
- Генерирует proactive messages через OpenClaw → push в Telegram / web notification.

### 6.3 Остальное
- CAD preview (STEP/DWG).
- TechCard / RouteCard.
- Similar cases.
- Mobile PWA (offline queue, biometric, voice).
- i18n + multi-currency.
- iCal export.
- Skills marketplace.

**To Do**:
- [ ] Telegram Bot adapter для OpenClaw.
- [ ] Telegram rendering (text + inline keyboard).
- [ ] File upload через Telegram.
- [ ] Approve через inline keyboard.
- [ ] Proactive agent (Celery beat + OpenClaw).
- [ ] CAD preview.
- [ ] TechCard / RouteCard расширение.
- [ ] Similar cases.
- [ ] Mobile PWA.
- [ ] i18n + multi-currency.
- [ ] iCal export.
- [ ] Skills marketplace UI.

---

## Cross-cutting (все эпики)

### Безопасность
- **Authentik** SSO: OAuth2/OIDC → JWT → FastAPI validates, RBAC по ролям.
- OpenClaw auth через тот же Authentik OAuth flow.
- Field-level permissions.
- **Dual AI security**: конфиденциальные документы (OCR/extraction) — только локальный Ollama. Reasoning/NL-задачи без конфиденциальных данных — можно на API. Конфигурация per task type.
- Secrets: env / vault only.
- Rate limiting, CSP, HTTPS, secure cookies.
- ClamAV для вложений.
- Append-only audit.

### Observability
- Structured logs (JSON).
- Prometheus: extraction latency, queue depth, VRAM, **OpenClaw agent step count**, approval wait time.
- Grafana dashboards.
- Sentry (frontend + backend).
- **OpenClaw agent trace**: каждый scenario execution логируется с tool calls, durations, results.

### Testing
- Unit (pytest, Jest): domain logic, rules, diff parser.
- API (httpx + pytest): все endpoints.
- Integration: Celery + Postgres/Redis.
- **Agent integration**: OpenClaw scenario execution на mock skills → verify tool call sequence.
- E2E (Playwright): keyboard-first scenarios.
- Extraction regression: dataset из эпика 0.
- Accessibility: axe-core.

### Documentation
- README.
- ADRs.
- **OpenClaw Skills API Reference** (auto-generated from Pydantic schemas).
- **Agent Scenarios runbook** (how each scenario works, what can fail, how to debug).
- FastAPI OpenAPI docs.
- UX shortcut guide.

### DevEx
- `make dev` — весь стек.
- Hot-reload (FastAPI + Next.js).
- Seeded data (тестовые поставщики, счета, письма, threads).
- Pre-commit hooks.
- **`make agent-test`** — прогнать все agent scenarios на mock skills.

---

## Целевая структура файлов

```
document-invoices-ai/
├── backend/
│   ├── app/
│   │   ├── main.py
│   │   ├── config.py
│   │   ├── db/                           # SQLAlchemy + Alembic
│   │   ├── domain/                       # Pydantic schemas, бизнес-логика
│   │   │   ├── documents.py
│   │   │   ├── invoices.py
│   │   │   ├── suppliers.py
│   │   │   ├── anomalies.py
│   │   │   ├── normalization.py
│   │   │   ├── approvals.py
│   │   │   ├── canonical.py
│   │   │   ├── calendar.py
│   │   │   ├── compare.py
│   │   │   └── collections.py
│   │   ├── api/                          # FastAPI routers (= skill endpoints)
│   │   │   ├── documents.py
│   │   │   ├── invoices.py
│   │   │   ├── email.py
│   │   │   ├── suppliers.py
│   │   │   ├── anomalies.py
│   │   │   ├── search.py
│   │   │   ├── tables.py
│   │   │   ├── approvals.py
│   │   │   ├── normalization.py
│   │   │   ├── canonical.py
│   │   │   ├── calendar.py
│   │   │   ├── compare.py
│   │   │   └── collections.py
│   │   ├── tasks/                        # Celery
│   │   │   ├── ingest.py
│   │   │   ├── extraction.py
│   │   │   ├── anomaly.py
│   │   │   ├── excel.py
│   │   │   └── scheduler.py              # beat tasks (reminders, proactive)
│   │   ├── ai/                           # Ollama client, prompts
│   │   │   ├── ollama_client.py
│   │   │   ├── extraction_prompts.py
│   │   │   ├── confidence.py
│   │   │   ├── style_matching.py
│   │   │   └── nl_query.py
│   │   └── audit/
│   └── tests/
├── frontend/
│   ├── app/
│   │   ├── inbox/
│   │   ├── documents/[id]/
│   │   ├── review/[id]/
│   │   ├── search/
│   │   ├── suppliers/[id]/
│   │   ├── collections/[id]/
│   │   ├── compare/[id]/
│   │   ├── calendar/
│   │   ├── approvals/
│   │   └── settings/
│   ├── components/
│   │   ├── inbox/
│   │   ├── review/
│   │   ├── command-palette/
│   │   ├── tables/
│   │   ├── chat/                         # OpenClaw WebSocket client
│   │   ├── timeline/
│   │   ├── compare/
│   │   ├── supplier/
│   │   └── calendar/
│   ├── lib/
│   │   ├── keyboard-context.tsx
│   │   ├── api-client.ts                 # REST → FastAPI
│   │   ├── openclaw-ws.ts                # WebSocket → OpenClaw
│   │   └── degraded-mode.ts              # fallback when OpenClaw down
│   └── tests/
├── openclaw/
│   ├── config/
│   │   ├── gateway.yml                   # OpenClaw gateway config
│   │   └── channels/
│   │       ├── web.yml
│   │       └── telegram.yml
│   ├── prompts/
│   │   ├── base.md                       # Base system prompt
│   │   ├── role-buyer.md                 # Закупщик
│   │   ├── role-accountant.md            # Бухгалтер
│   │   ├── role-manager.md               # Руководитель
│   │   ├── role-engineer.md              # Технолог
│   │   ├── scenario-email-draft.md       # Email drafting additions
│   │   ├── scenario-anomaly.md           # Anomaly explanation
│   │   └── scenario-compare.md           # КП comparison
│   ├── skills/
│   │   ├── _registry.yml                 # Manifest всех skills
│   │   ├── documents.yml                 # doc.* skills (auto-gen from Pydantic)
│   │   ├── invoices.yml
│   │   ├── email.yml
│   │   ├── suppliers.yml
│   │   ├── anomalies.yml
│   │   ├── search.yml
│   │   ├── tables.yml
│   │   ├── approvals.yml
│   │   ├── normalization.yml
│   │   ├── canonical.yml
│   │   ├── calendar.yml
│   │   ├── compare.yml
│   │   └── collections.yml
│   ├── scenarios/                        # Documented workflows
│   │   ├── email-triage.md
│   │   ├── assisted-review.md
│   │   ├── draft-email.md
│   │   ├── compare-kp.md
│   │   ├── proactive-followup.md
│   │   ├── anomaly-resolution.md
│   │   ├── nl-query.md
│   │   └── smart-ingest.md
│   └── hooks/                            # OpenClaw event hooks
│       ├── on-approval-resolved.js
│       └── on-new-document.js
├── infra/
│   ├── docker-compose.yml
│   ├── docker-compose.dev.yml
│   ├── traefik/
│   ├── ollama/
│   └── scripts/
│       ├── seed-data.py
│       ├── generate-skill-registry.py    # Pydantic → OpenClaw skill YAML
│       └── run-agent-tests.py
├── datasets/
├── docs/
│   ├── adrs/
│   ├── api/
│   ├── skills-reference.md               # Auto-generated
│   └── scenarios-runbook.md
├── plan_claude.md
├── PLAN.md
├── Makefile
└── README.md
```

**Ключевой скрипт**: `scripts/generate-skill-registry.py` — читает Pydantic schemas из FastAPI endpoints и генерирует `openclaw/skills/*.yml`. Это **единый источник правды**: контракт описан один раз в Python, OpenClaw и документация генерируются автоматически.

---

## Верификация

### По эпикам (основные тесты)

| Эпик | Ключевой тест |
|---|---|
| 0 | gemma4 extraction ≥85%, OpenClaw agent loop works, approval gate works |
| 1 | Email → Inbox ≤30с, keyboard навигация, **чат «покажи документ» → widget**, degraded mode |
| 2 | Extraction ≥80%, review streak 10+ без мыши, NormRule, **Scenario 1 end-to-end** |
| 3 | Ctrl+K → NL → result, round-trip Excel, **Scenario 3 (draft email) end-to-end** |
| 4 | Fuzzy search, price history, canonical items, **Scenario 7+8 through chat** |
| 5 | Anomaly cards, compare КП, calendar, **Scenarios 4+5+6 end-to-end** |
| 6 | Telegram file → Scenario 8, proactive alerts |

### Общие команды
- `make test` — unit + API + integration.
- `make e2e` — Playwright.
- `make regression` — extraction quality.
- `make agent-test` — OpenClaw scenarios на mock skills.
- CI блокирует merge при падении.

---

## Риски и митигации

| Риск | Митигация |
|---|---|
| gemma4:e4b bbox нестабилен | Эпик 0 валидирует; fallback: PyMuPDF bbox + Tesseract для сканов |
| 24GB VRAM tight | Эпик 0 замеряет; model swapping; fallback: API-модель для 26b |
| OpenClaw нестабилен | Pin stable version; staging; compatibility matrix; **degraded mode** (UI через REST) |
| OpenClaw skill registration сложна | generate-skill-registry.py из Pydantic; один источник правды |
| Approval gate latency (agent ждёт долго) | Timeout + escalation; agent может параллельно обрабатывать другие задачи |
| Round-trip Excel конфликты | Optimistic locking; diff wizard показывает конфликты |
| Скоуп MVP раздут | Эпик 0 финализирует; OpenClaw skills добавляются инкрементально |
| Telegram + Web разный UX | Channel matrix фиксирует scope; Telegram = упрощённый, не full |

---

## Закрытые вопросы

| # | Вопрос | Решение |
|---|---|---|
| 1 | Команда | Vibe coding (solo + AI) |
| 2 | Deployment | On-prem сервер, docker-compose |
| 3 | Почта | Несколько IMAP-ящиков (routing по отделам) |
| 4 | 1С | Обязателен экспорт в формат 1С |
| 5 | Язык | i18n-ready сразу (next-intl, RU по умолчанию) |
| 6 | Auth | Authentik (self-hosted SSO, OAuth2/OIDC) |
| 7 | OpenClaw | v2026.4.5 stable. Подтверждено: custom skills, approval gates, Telegram, memory, WebSocket, 15+ каналов |
| 8 | Имя AI | **Света** (Sveta) |

## Оставшиеся технические вопросы (уточнять по ходу)

- Конкретный формат 1С-экспорта (XML CommerceML / JSON / CSV для загрузки в Бухгалтерию/УТ).
- Маппинг ролей Authentik → роли системы (закупщик, бухгалтер, руководитель, технолог, администратор).
- Конкретные IMAP-ящики и правила routing по ящикам.
- Лимиты VRAM: при переключении reasoning на API — какие задачи остаются локальными, какие уходят на API.
