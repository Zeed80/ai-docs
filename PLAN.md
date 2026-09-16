---
name: AI-Assistant-Manufacturing
overview: Подробное ТЗ и план разработки единого рабочего пространства для документов (Счета, Чертежи, Письма). Основан на AiAgent + Python. Упор на Keyboard-first UX, Side-by-Side review, Round-trip Excel, Anomaly Detection и Human-in-the-Loop.
todos:
  - id: backend_core_db
    content: "Data Layer: PostgreSQL (Document, Invoice, CanonicalItem, AnomalyCard, NormalizationRule), Qdrant, MinIO. Настройка FastAPI."
    status: pending
  - id: llm_vlm_engine
    content: "AI Layer: Ollama локально с gemma4:e4b (роутинг/счета) и gemma4:26b (агент). Интеграция Structured Outputs и причин низкой уверенности."
    status: pending
  - id: smart_document_pipeline
    content: "Пайплайн входящих (Celery): Email/Upload Ingest -> пред-обработка -> VLM gemma4:e4b -> Детекция аномалий (дубликаты, цены) -> Статус 'Needs Review'."
    status: pending
  - id: aiagent_onboard
    content: "Агентный слой: развертывание AiAgent Gateway. Настройка каналов связи и системных промптов."
    status: pending
  - id: frontend_pwa_core
    content: "Frontend (Next.js): базовый Layout (Dashboard, Inbox с keyboard-first triage). Подключение по WebSocket к AiAgent Gateway. Command palette (Ctrl+K)."
    status: pending
  - id: frontend_side_by_side
    content: "Frontend (UX Проверки): Side-by-Side View (оригинал PDF / поля). Двусторонняя подсветка (поле <-> bbox). Review streak поток с клавиатуры."
    status: pending
  - id: frontend_tables_export
    content: "Frontend (Сводки): табличные представления с inline-edit. Экспорт в Excel (XLSX) и Round-trip импорт правок из Excel через diff-wizard."
    status: pending
  - id: supplier_and_compare
    content: "Бизнес-логика: Рабочий кабинет поставщика (Price History). Dedicated Compare View для сравнения коммерческих предложений (КП)."
    status: pending
  - id: draft_email_flow
    content: "Рабочий Workflow: 'Агент предлагает черновик письма -> Risk-check -> Человек правит -> Отправить -> Сохранение в user-facing Audit Timeline'."
    status: pending
isProject: false
---

# Единое рабочее пространство ИИ-документооборота для производства

> Актуальная переработка агента: [`AGENT_EMPLOYEE_IMPLEMENTATION_PLAN.md`](./AGENT_EMPLOYEE_IMPLEMENTATION_PLAN.md). Модель выбирает действия; generated skills/replay отключены; полномочия задаются явно. Долговечный чат и изолированные скрипты ещё не завершены.

Доступен HTTP-пилот долговечного чата `/api/agent/chat-runs`: атомарный приём,
идемпотентность и сохранённые события. При потере worker запуск блокируется, а не
повторяет внешние действия. Основной UI переключён на HTTP; старые чаты в нём
только для чтения. Карточка подтверждения и `POST /{id}/resume` продолжают
сохранённый ожидающий вызов после одноразового решения владельца. Аргументы
не редактируются, согласие действует 30 минут; неизвестный внешний эффект не
повторяется. Общий pause/resume после произвольного сбоя ещё не реализован.
Журнал `/api/agent/chat-runs/{id}/actions` сохраняет логический UUID и результат
атомарно с checkpoint. API наблюдений владельца фиксирует свидетельства неизвестного
исхода, но не разрешает повтор. UI `/work-orders/chat-journal?run_id=…` доступен
из чата: детали действий, пагинация, запись наблюдения и источник. Проверка:
`cd frontend && PLAYWRIGHT_MOCK_API=1 npx playwright test tests/e2e/chat-action-journal.spec.ts --project=chromium`.
Подробная последовательность остатка для модели-исполнителя:
[`AGENT_EMPLOYEE_EXECUTION_PLAYBOOK.md`](./AGENT_EMPLOYEE_EXECUTION_PLAYBOOK.md).
Карточки E00–E52 выполняются по одной, с указанными зависимостями и review gates.
E00 — read-only сверка текущего AgentTask — реализована моделью-исполнителем и
проверена сеньором. Фактические проверки/выкладка в
`docs/agent-employee-delivery/E00-receipt-verification.md`; далее E01 (UI).
Режим передачи работы без ручного участия: `AGENT_EMPLOYEE_ORCHESTRATION.md`.
E01 добавляет owner-facing UI текущей сверки: только GET, отдельный от квитанции
и ответа worker; ошибки и late response не оставляют старый verdict. Отчёт:
`docs/agent-employee-delivery/E01-current-verification-ui.md`. E02 завершена:
owner-only история наблюдений постранична по устойчивому `sequence`, отображается
отдельно от последнего наблюдения, не следует ссылкам и не открывает replay.
Отчёт: `docs/agent-employee-delivery/E02-observation-history.md`. Далее E03.
E03 завершена: полный evidence-based инвентарь 323 catalog operations и их effect/
commit boundaries защищён fail-closed тестом; 47 недоказанных границ остаются
`unknown` без automatic retry. Расхождение прав `task_propose` зафиксировано, но
не исправлялось этой карточкой. Отчёт:
`docs/agent-employee-delivery/E03-tool-effect-inventory.md`.
E04 завершена: строгий ToolResult v1, отдельные решения об успехе вызова,
приёмке работы и повторе; неизвестный legacy-ответ не нормализуется в успех.
Отчёт: `docs/agent-employee-delivery/E04-tool-result-contract.md`. Далее E05.1.
E05.1 завершена: доказанные catalog-read ответы нормализуются в ToolResult v1
на агентской границе с сохранением исходного payload. Отчёт:
`docs/agent-employee-delivery/E05-1-read-adapters.md`.
E05.2.1 завершена как первый узкий срез DB writes: только
`analytics.collection_create`, `analytics.calendar_create_reminder`,
`analytics.table_create_view` и `warehouse.create_item` получают ToolResult v1.
2xx domain success сохраняет raw payload; domain error/4xx — `failed`,
неоднозначность после dispatch — `outcome_unknown`, ошибка до dispatch —
`failed`; automatic retry нет. Отчёт:
`docs/agent-employee-delivery/E05-2-1-db-write-adapters.md`.
E05.2.2 REVIEWED добавляет `analytics.collection_add_item`,
`analytics.collection_close`, `analytics.compare_create`,
`analytics.compare_align` и `analytics.table_inline_edit`: cumulative allowlist
равен девяти операциям. Контракт E05.2.1, public API и RBAC не менялись;
выбранные handlers имеют один прямой commit, а
`add_timeline_event`/`log_action` flush-only. Route alias `compare_create`
fail-closed, `analytics.calendar_extract_dates` исключён из-за runtime 0/1
commit-границы. Независимая проверка: 190 passed с известным предупреждением
`asyncio_loop_scope`. Отчёт:
`docs/agent-employee-delivery/E05-2-2-db-write-adapters.md`.
E05.2.3 REVIEWED добавляет только `warehouse.update_item`,
`warehouse.adjust_stock` и `warehouse.create_receipt`: cumulative allowlist
равен 12 операциям. У трёх handler-ов уникальные route/action-пары и один
прямой безусловный commit на success path; `log_action`/`add_timeline_event`
flush-only, external dispatch/enqueue нет. Три action отсутствуют в
`warehouse.gate_actions`; public API, RBAC и approval policy не менялись.
Исключены `warehouse.confirm_receipt`, `warehouse.issue_stock`,
`warehouse.delete_item`, `warehouse.update_status` и `warehouse.bulk_confirm`.
Независимая проверка: 199 passed с известным предупреждением
`asyncio_loop_scope`; production пока не заявлен. Отчёт:
`docs/agent-employee-delivery/E05-2-3-db-write-adapters.md`.
E05.2.4 REVIEWED добавляет только `email.templates.create`,
`email.templates.update` и `suppliers.update`: cumulative allowlist равен 15
операциям. У трёх точные уникальные route/action-пары, по одному прямому
безусловному commit на success path, select/flush-помощники не добавляют commit,
external dispatch/enqueue нет; все три `admin_only=false` и не approval-gated.
`email.templates.from_message` исключён из-за возможного `ai_router.complete`,
`analytics.calendar_generate_followup` — из-за несовпадения identity
path-параметра `{entity_id}`/`{reminder_id}`; render/delete/status и gated
actions fail-closed. Контракт E05.2.1, public API, RBAC и approval policy не
менялись. Независимая проверка: 205 passed с известным предупреждением
`asyncio_loop_scope`; production пока не заявлен. Отчёт:
`docs/agent-employee-delivery/E05-2-4-db-write-adapters.md`.
E05.2.5 REVIEWED добавляет только `procurement.create_request` через точный
уникальный `POST /api/purchase-requests`: cumulative allowlist равен 16
операциям. `create_purchase_request` имеет один прямой безусловный commit на
success path без helper commit, external dispatch или enqueue; операция
`admin_only=false` и не approval-gated. `procurement.update_request` и
`procurement.update_contract` исключены, поскольку принимают `status`;
`procurement.create_contract` — из-за route alias, `procurement.send_rfq` —
из-за external effect. Safety correction: `suppliers.trust_score` является
catalog GET, но handler условно коммитит `profile.trust_score`; поэтому
`READ_CATALOG_OPERATIONS_WITH_PERSISTENT_EFFECTS` запрещает read retry для
прямого и capability route. Операция не входит в write adapter из-за conditional
0/1 commit. Контракт не менялся; независимая проверка: 207 passed с известным
предупреждением `asyncio_loop_scope`; production пока не заявлен. Отчёт:
`docs/agent-employee-delivery/E05-2-5-db-write-adapters.md`. E05.2 целиком
остаётся IN PROGRESS; дальше нужен новый отдельно проверенный срез E05.2.

Пилот `agent_control.task_propose` атомарно сохраняет задачу и квитанцию получателя
в WorkEvent, проверяет владельца/аргументы/попытку/lease. Детали журнала и UI
показывают квитанцию независимо от потерянного ответа worker. Другие операции
и продолжение по квитанциям ещё в плане. Проверка:
`python3 -m pytest backend/tests/test_action_receipts.py -q`.
Предварительно устранён слепой HTTP retry: повторяется только проверенное чтение;
неизвестный эффект сохраняется и блокирует durable-цикл. Проверка:
`python3 -m pytest backend/tests/test_tool_transport.py backend/tests/test_chat_checkpoints.py -q`.
Миграция `20260912_0001`; журнал проверяется `python3 -m pytest backend/tests/test_chat_action_journal.py -q`.
Проверка: `python3 -m pytest backend/tests/test_chat_checkpoints.py backend/tests/test_durable_chat.py backend/tests/test_work_order_checkpoint.py -q`.

> Подробный план и фактический статус инженерного направления «Оцифровка в DXF» находятся в [`DXF_CAD_DEVELOPMENT_PLAN.md`](./DXF_CAD_DEVELOPMENT_PLAN.md).
> Там же зафиксирован реализованный вертикальный срез `EngineeringModelGraph v1`; после mechanical live regression primary pipeline включён как production canary только для mechanical-профиля.
> Контракт основного метода «По описанию» и последовательность реализации полного координатного графа находятся в [`CAD_DRAWING_GRAPH_PLAN.md`](./docs/archive/CAD_DRAWING_GRAPH_PLAN.md).
> Последовательный TODO по точности режима «Перечертить по чертежу», 3D-first построению и прозрачности данных находится в [`CAD_REDRAW_ACCURACY_TODO.md`](./CAD_REDRAW_ACCURACY_TODO.md).
> Оставшийся универсальный roadmap по всем mechanical/construction классам и production-приёмке находится в [`CAD_UNIVERSAL_REMAINING_TODO.md`](./CAD_UNIVERSAL_REMAINING_TODO.md).
> Полный статус и последовательность развития автономного сотрудника находятся в [`AGENT_SYSTEM_DEVELOPMENT_PLAN.md`](./docs/archive/AGENT_SYSTEM_DEVELOPMENT_PLAN.md).

Данный документ представляет собой финальную архитектуру и техническое задание (ТЗ) на разработку системы. Она построена на слиянии агентского фреймворка **AiAgent**, кастомного Python-бэкенда и глубокого UX-подхода. 

Это не просто "чат с ИИ". Это профессиональный инструмент, где ИИ работает как надежный ассистент под контролем человека (Draft-First & Human Review), с упором на скорость работы с клавиатуры (Keyboard-first) и прозрачность решений.

---

## 1. Продуктовые принципы (Инварианты)
1. **One workspace:** Почта, документы, чат с ИИ, таблицы, поиск (Command Palette) и согласования находятся в одном интерфейсе (Next.js PWA).
2. **Draft-first (Сначала черновик):** Внешние действия (отправка писем, изменение БД) выполняются ИИ **только** в виде черновика. Отправка — только после клика `[Утвердить]` человеком.
3. **Explainability (Обоснованность):** Любой ответ агента или извлеченная цифра из счета подсвечивает свой источник в оригинале документа (BBox). Низкая уверенность ИИ (Low Confidence) всегда сопровождается человекочитаемой причиной.
4. **Keyboard-first для потока:** Все ежедневные действия (Triage в Inbox, Review счетов) выполняются с клавиатуры без использования мыши.
5. **Visible Trust Loop (Обучение на правках):** Система предлагает детерминированные правила (NormalizationRules) из повторяющихся ручных правок пользователя.
6. **Round-trip данных:** Если данные можно выгрузить в Excel, их можно загрузить обратно с применением правок через процесс Approval.

---

## 2. Архитектурный Стек (Multi-Model & AiAgent)

### 2.1. ИИ-движок (Ollama)
Балансировка скорости и точности (MoA) в рамках 24GB VRAM:
- **Gemma 4 E4B (`gemma4:e4b`):** Быстрый классификатор и OCR. Принимает документ, понимает что это, извлекает JSON с BBox координатами полей.
- **Gemma 4 26B MoE (`gemma4:26b`):** Основная "умная" модель (Core Agent) для генерации текстов писем (Style matching), логических рассуждений и работы со сложным контекстом.

### 2.2. Бэкенд и Инфраструктура
- **AiAgent Gateway:** WebSocket-шлюз, управление сессиями, контекстом чата, интеграция навыков (Skills/Plugins) через прозрачный Approval workflow.
- **Durable Work Runtime:** FastAPI/PostgreSQL-модель `WorkOrder → WorkPlan → WorkStep → Attempt`, append-only события, DAG-зависимости, lease/heartbeat/retry, отдельная верификация и evidence-gated completion. Чат и cron являются каналами постановки задач, а не владельцами жизненного цикла.
- **Autonomous Execution Runtime (реализовано 2026-08-17):** capability-grounded planner/replanner, типизированный dataflow между шагами, write-ahead журнал `WorkToolCall`, автоматический semantic verifier, least-privilege computer-use broker и операторская поверхность `/work-orders`. Неизвестные capabilities/actions и неподтверждённые OS/внешние действия блокируются fail-closed.
- **Python / FastAPI + Celery:** "Тяжелый" Data Layer. Парсинг почты, извлечение текста (PyMuPDF), конвертация CAD-файлов, генерация/парсинг Excel, детекция аномалий.
- **Базы данных:** `PostgreSQL` (все документы, AuditLog, сущности), `Qdrant` (векторный гибридный поиск), `MinIO` (хранилище файлов).

---

## 3. Информационная архитектура и UX

Система предоставляет **три равноправных интерфейса**:
1. **Классический UI** (списки, карточки, таблицы, дашборды).
2. **Command Palette (`Ctrl+K`)** — для быстрых действий (поиск + действие + экспорт в одно нажатие).
3. **Чат-ассистент** — для диалога с контекстом, выдающий результаты в виде виджетов с action-кнопками.

### Главные рабочие поверхности:
1. **Inbox (Входящие):** Единая очередь всех новых объектов. Поддерживает Keyboard-first triage (`j/k` для навигации, `e` для approve, `s` для snooze). Smart batching однотипных событий.
2. **Side-by-Side Review (Режим проверки):** Слева PDF, справа извлеченные поля. 
   - **Двусторонняя подсветка:** клик по полю -> подсвечивает область в PDF.
   - **Auto-focus:** при открытии фокус сразу на поле с наименьшим confidence.
   - **Review streak:** после нажатия `Enter` (Approve) мгновенно открывается следующий счет из очереди.
3. **Таблицы и Round-trip Excel:** Выборки счетов/писем. Inline-редактирование. Кнопка выгрузки в `.xlsx`. И главное — импорт измененного Excel обратно с показом Diff'а (что поменялось) и прохождением через Approval.
4. **Compare View (Сравнение КП):** Выравнивание позиций из разных коммерческих предложений по каноническим товарам (`CanonicalItem`), подсветка лучших цен, вывод `Price History` в виде спарклайнов.
5. **Профиль поставщика:** Рабочий кабинет с Price History по позициям, историей реквизитов, текущими открытыми счетами и Trust Score (рейтинг доверия).

---

## 4. Защита от ошибок (Safety Rails) & Аномалии

На многопрофильном производстве цена ошибки огромна.

1. **Детекция Аномалий (AnomalyCard):**
   При парсинге счета Python-бэкенд проверяет:
   - Не изменились ли реквизиты (ИНН/IBAN) по сравнению с базой?
   - Не выросла ли цена на деталь > 20% по сравнению с Price History?
   - Это не дубликат (проверка по Hash и сумме/номеру)?
   Если да, генерируется `AnomalyCard`, которая требует явного решения руководителя.
2. **Risk-Check писем:** Перед отправкой письма ИИ проверяет: нет ли чужих доменов в получателях, есть ли вложение, если в тексте упоминается счет.
3. **User-facing Audit Timeline:** На карточке документа отображается человекочитаемая лента действий ("Иван поправил сумму", "Система выявила аномалию цены").
4. **Structured Outputs & Hybrid RAG:** Строгая валидация JSON от ИИ через Pydantic. Гибридный поиск (Вектор + Точный текст BM25) для избежания галлюцинаций в марках сталей и ГОСТах.

---

## 5. Детальный план реализации (Поэтапный)

*Разделяйте логику: управление сессиями и чат — в AiAgent (Node.js/TS), тяжелая обработка файлов, БД и генерация Excel — в FastAPI (Python).*

### Этап 1: Инфраструктура и Единый контур входящих
1. **Data Layer:** Docker-Compose (PostgreSQL, Redis, Qdrant, MinIO). Traefik (HTTPS).
2. **FastAPI & DB:** Модели `Document`, `Invoice`, `CanonicalItem`, `AnomalyCard`, `AuditTimelineEvent`.
3. **AI Layer:** Развернуть Ollama с `gemma4:e4b` и `gemma4:26b`.
4. **Ingest (Celery):** Прием почты (IMAP) и файлов (Drag&Drop, URL, Paste). Создание объекта в статусе `Needs Review`.
5. **AiAgent Gateway:** Развернуть `aiagent onboard`.

### Этап 2: UX Проверки и AI-Извлечение
1. **PWA (Next.js):** Создать Layout, Inbox и Command Palette (`Ctrl+K`).
2. **Извлечение:** Модуль извлечения счетов через `gemma4:e4b` со Structured Outputs и обязательным расчетом Confidence (с BBox координатами).
3. **Side-by-Side Review:** Реализовать UI проверки. Двусторонняя подсветка поле <-> PDF. Поддержка горячих клавиш (Review streak).
4. **Аномалии:** Написать логику детекции дубликатов и создания `AnomalyCard`.

### Этап 3: Рабочие действия (Excel и Письма)
1. **Таблицы:** Реализовать табличные виды. Написать генератор Excel (export). 
2. **Round-trip Import:** Написать Diff-wizard для загрузки измененного Excel обратно в БД с маршрутизацией через Approval.
3. **Email Workflow:** Виджет редактора писем. Style matching (ИИ подстраивает тон под прошлую переписку). Risk-check перед отправкой. Плагин отправки для AiAgent.

### Этап 4: Бизнес-ценность (Compare КП и Справочники)
1. **Профиль поставщика:** UI с реквизитами, метриками (Trust score) и историей.
2. **Price History:** Логика привязки позиций счета (`InvoiceLine`) к каноническому справочнику (`CanonicalItem`) для построения графиков цен.
3. **Compare View:** Специальный UI для сравнения нескольких КП side-by-side.

### Этап 5: Поиск, Связи и Улучшения
1. **Гибридный RAG:** Настройка векторного + полнотекстового поиска в Qdrant/PostgreSQL. NL-поиск с превращением естественного языка в чипсы фильтров.
2. **Trust Loop (Обучение):** Логика генерации `NormalizationRule` (правил нормализации), если пользователь 3 раза подряд правит одно и то же поле у одного поставщика.
3. **Календарь:** Извлечение дат оплат/поставок в UI календаря с напоминаниями.
