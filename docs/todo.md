# Подробный TODO проекта

## Цель ближайшего этапа

Собрать первый полезный вертикальный срез: инженер создает производственный кейс, загружает документ, получает AI-классификацию/извлечение, видит audit timeline и может дальше использовать этот кейс как основу для чертежей, техпроцесса, инструмента, норм, писем и закупки.

## Этап 1. Backend foundation

Статус: начато, базовый срез реализован.

- [x] Создать FastAPI-приложение.
- [x] Добавить настройки через env: `DATABASE_URL`, `STORAGE_ROOT`, `AI_REGISTRY_PATH`.
- [x] Добавить SQLAlchemy foundation.
- [x] Добавить модели `ManufacturingCase`, `Document`, `DocumentVersion`, `AuditEvent`.
- [x] Добавить локальное файловое хранилище для dev-режима.
- [x] Добавить REST API:
  - [x] `POST /api/cases`
  - [x] `GET /api/cases`
  - [x] `GET /api/cases/{case_id}`
  - [x] `PATCH /api/cases/{case_id}`
  - [x] `POST /api/cases/{case_id}/documents`
  - [x] `GET /api/cases/{case_id}/documents`
  - [x] `GET /api/documents/{document_id}`
  - [x] `GET /api/cases/{case_id}/audit`
- [x] Подключить `AIRouter` к backend endpoints:
  - [x] `POST /api/documents/{document_id}/classify`
  - [x] `POST /api/documents/{document_id}/extract`
- [x] Покрыть flow тестами без реального Ollama.

## Этап 2. Реальная обработка документов

- [x] Добавить `DocumentProcessingJob` и статусы фоновой обработки.
- [x] Добавить text extraction:
  - [x] `.txt`, `.md`, `.csv`, `.json`, `.xml`;
  - [x] PDF text layer через PyMuPDF, если зависимость доступна;
  - [x] DOCX через python-docx, если зависимость доступна;
  - [x] XLSX через openpyxl, если зависимость доступна.
- [x] Добавить безопасный fallback для PDF без text layer/PyMuPDF, image и CAD.
- [x] Добавить preview/render pipeline:
  - [x] PDF page render через PyMuPDF, если зависимость доступна;
  - [x] image normalization через Pillow, если зависимость доступна;
  - [x] хранение preview artifacts.
- [x] Добавить OCR fallback через AI vision route.
- [x] Добавить field-level extraction result вместо одного `ai_summary`.
- [x] Добавить confidence и reason для каждого поля.
- [x] Добавить API endpoint запуска обработки документа: `POST /api/documents/{document_id}/process`.
- [x] Добавить audit events для старта, завершения и fallback/failure обработки.

## Этап 3. Инженерный кейс технолога

- [x] Добавить модели:
  - [x] `Drawing`;
  - [x] `DrawingFeature`;
  - [x] `Material`;
  - [x] `Machine`;
  - [x] `Tool`;
  - [x] `Operation`;
  - [x] `ProcessPlan`;
  - [x] `NormEstimate`.
- [x] Добавить `DrawingAnalysis` endpoint.
- [x] Для DXF добавить `ezdxf` extractor, если зависимость доступна.
- [x] Для STEP добавить задел под FreeCAD/pythonOCC pipeline через header/entity extraction.
- [x] Добавить AI prompt для “что понятно / что рискованно / какие вопросы задать”.
- [x] Добавить черновик письма заказчику по неясностям без отправки, с approval-required и audit.

## Этап 4. Счета, КП, поставщики

- [x] Использовать `example-invoices/` как локальный regression dataset для PDF/JPG счетов без коммита реальных клиентских документов.
- [x] Добавить модели `Supplier`, `Quote`, `Invoice`, `InvoiceLine`, `PriceHistoryEntry`.
- [x] Добавить invoice extraction schema.
- [x] Добавить проверку арифметики счета строгим кодом.
- [x] Добавить hash/sum/number duplicate detection.
- [x] Добавить price history.
- [x] Добавить базовую anomaly card.
- [x] Добавить supplier requisites diff.
- [x] Добавить Excel export.
- [x] Добавить 1С export placeholder/interface.

## Этап 5. Email workspace

- [x] Добавить модели `EmailThread`, `EmailMessage`, `EmailAttachment`, `DraftEmail`.
- [x] Добавить IMAP polling adapter placeholder без внешнего подключения.
- [x] Добавить SMTP sender placeholder с approval gate.
- [x] Добавить risk-check перед отправкой.
- [x] Связывать email thread с `ManufacturingCase`.

## Этап 6. GUI cockpit

- [x] Создать Next.js приложение.
- [x] Главный экран: “Моя работа сегодня”.
- [x] Экран кейса с вкладками:
  - [x] обзор;
  - [x] документы;
  - [x] чертеж;
  - [x] техпроцесс;
  - [x] инструмент;
  - [x] нормы;
  - [x] письма;
  - [x] закупка;
  - [x] история.
- [x] Upload документов.
- [x] Audit timeline.
- [x] Панель “Света” справа.
- [x] Command palette `Ctrl+K`.

## Этап 7. Agent/AiAgent

- [x] Сгенерировать skills из FastAPI OpenAPI/Pydantic schemas:
  - [x] `case.create`;
  - [x] `document.upload`;
  - [x] `document.process`;
  - [x] `document.drawing_analysis`;
  - [x] `document.invoice_extraction`;
  - [x] `email.draft`;
  - [x] `email.send.request_approval`;
  - [x] `invoice.export.xlsx`;
  - [x] `invoice.export.1c.prepare`.
- [x] Добавить allowlist tools:
  - [x] хранить registry в `aiagent/skills/registry.json`;
  - [x] backend enforcement по имени tool;
  - [x] запрет всех неизвестных tool calls.
- [x] Добавить scenario `smart_ingest`:
  - [x] step limit;
  - [x] audit каждого шага;
  - [x] document processing;
  - [x] invoice/drawing branch;
  - [x] safe fallback.
- [x] Добавить scenario `drawing_review`:
  - [x] drawing analysis;
  - [x] questions draft;
  - [x] approval-required result.
- [x] Добавить scenario `process_plan_draft`:
  - [x] placeholder без внешних действий;
  - [x] audit и step limit.
- [x] Добавить scenario `draft_email`:
  - [x] draft email;
  - [x] risk-check;
  - [x] send blocked until approval.
- [x] Добавить approval gates:
  - [x] email send;
  - [x] 1С export;
  - [x] external connectors;
  - [x] destructive changes.
- [x] Добавить step limits и audit для agent actions:
  - [x] модель `AgentAction`;
  - [x] модель `ApprovalGate`;
  - [x] endpoints запуска scenarios;
  - [x] tests на allowlist/approval/step limit.

## Этап 8. Качество и безопасность

- [x] Alembic migrations:
  - [x] baseline migration;
  - [x] dev/prod migration docs;
  - [x] запрет silent `create_all` для production.
- [x] Authentik OIDC:
  - [x] settings;
  - [x] JWT validation;
  - [x] local dev bypass.
- [x] RBAC:
  - [x] роли technologist/accountant/admin;
  - [x] permissions matrix;
  - [x] tests.
- [x] Signed file URLs.
- [x] Quarantine для опасных вложений:
  - [x] extension allowlist;
  - [x] suspicious file status;
  - [x] audit.
- [x] Encrypted backups.
- [x] Regression dataset для AI:
  - [x] `example-invoices/` manifest;
  - [x] expected fields без секретов;
  - [x] drawing samples manifest.
- [x] `make ai-eval` в CI.
- [x] Playwright e2e для главных flows:
  - [x] create case;
  - [x] upload document;
  - [x] process;
  - [x] audit timeline;
  - [x] approval gate.

## Этап 9. Production execution и approval cockpit

- [x] Очереди задач:
  - [x] выбрать backend queue adapter для dev/prod;
  - [x] вынести document processing из синхронного запроса;
  - [x] добавить retry/backoff и dead-letter status;
  - [x] audit для retry/failure/manual review.
- [x] Approval cockpit:
  - [x] endpoint списка pending approval gates;
  - [x] approve/reject endpoints с actor и reason;
  - [x] GUI-панель approvals в правом rail и/или отдельной вкладке;
  - [x] Playwright e2e approve/reject flow.
- [x] Безопасное выполнение agent tools:
  - [x] executor для allowlisted safe tools;
  - [x] execution только после RBAC permission;
  - [x] external tools только после approval;
  - [x] audit payload/result/error для каждого execution.
- [x] Production adapters:
  - [x] SMTP sender placeholder после approval;
  - [x] IMAP polling с quarantine attachments;
  - [x] 1C export placeholder после approval;
  - [x] MinIO storage adapter и presigned URLs.
- [x] GUI hardening:
  - [x] отображение suspicious/quarantine статусов;
  - [x] signed download action;
  - [x] invoice anomaly card UI;
  - [x] AiAgent scenario launch UI.

## Текущий следующий шаг

Следующий технический шаг: усилить встроенного агента как основной рабочий
контур до возврата к official AiAgent: сценарии многошагового выполнения,
расширенные approval gates, проверка качества памяти и регрессионные E2E.

## Этап 10. SQL-first память, НТД и нормоконтроль

- [x] Принять SQL-first стратегию для текстовых документов: быстрый поиск и проверки идут по SQL, граф используется как асинхронный слой связей.
- [x] Добавить статусный контур построения графов после загрузки/изменения документов.
- [x] Разделить построение графа на compact для обычных текстов и extended для чертежей/техпроцессов/НТД.
- [x] Добавить настройки нормоконтроля:
  - [x] режим `manual`;
  - [x] режим `auto`;
  - [x] default `manual`.
- [x] Добавить backend API:
  - [x] `GET /api/settings/ntd-control`;
  - [x] `PATCH /api/settings/ntd-control`;
  - [x] `POST /api/documents/{document_id}/ntd-check`;
  - [x] `GET /api/documents/{document_id}/ntd-check/availability`;
  - [x] `GET /api/documents/{document_id}/ntd-checks`;
  - [x] `GET /api/ntd/documents`;
  - [x] `POST /api/ntd/documents`;
  - [x] `POST /api/ntd/requirements`;
  - [x] `GET /api/ntd/requirements/search`;
  - [x] `POST /api/ntd/checks/{check_id}/findings/{finding_id}/decide`.
- [x] Добавить SQL-first сущности НТД:
  - [x] нормативный документ;
  - [x] версия;
  - [x] пункт;
  - [x] требование;
  - [x] запуск проверки;
  - [x] замечание.
- [x] Добавить детерминированную проверку:
  - [x] запрет проверки quarantined документов;
  - [x] проверка наличия текста;
  - [x] поиск применимых требований;
  - [x] создание findings с severity, evidence, recommendation, confidence;
  - [x] решение пользователя по finding.
- [x] Подключить auto-mode к document extraction pipeline.
- [x] Добавить UI-кнопку “Проверить на соответствие НТД”.
- [x] Добавить backend disabled-причины кнопки: quarantine, нет текста, нет активных требований НТД.
- [x] Добавить UI-настройку режима нормоконтроля: ручной/автоматический.
- [x] Добавить SQL-first режимы поиска памяти: `sql`, `sql_vector`, `sql_vector_rerank`, `graph`, `hybrid`.
- [x] Добавить weighted ranking для общего пула `graph + text + vector`.
- [x] Добавить PostgreSQL full-text search по документам и требованиям НТД с sqlite/dev fallback через `ILIKE`.
- [x] Расширить AI registry параметрами embedding/reranker capabilities.
- [x] Добавить UI-выбор active embedding/reranker модели в настройках.
- [x] Добавить active embedding profile для document embeddings и dynamic Qdrant collections.
- [x] Добавить active embedding profile API и stale marking при смене embedding-модели.
- [x] Добавить OpenAI-compatible reranker adapter и optional rerank stage поверх SQL/vector candidates.
- [x] Добавить reindex UI для active/stale embedding records.
- [x] Добавить индексирование active memory embedding records в Qdrant.
- [x] Добавить экран SQL-first НТД: список нормативов, поиск требований, ручное создание НТД и требований.
- [x] Добавить индексирование НТД в clauses/requirements из текста загруженного документа.
- [x] Добавить создание НТД из загруженного документа с автоопределением кода/версии.
- [x] Добавить прямой upload НТД PDF/DOCX/TXT без ручного ввода document id.
- [x] Добавить optional semantic AI-assisted нормоконтроль с evidence spans.
- [x] Добавить параллельный compose/Make-контур для официального AiAgent Gateway без отключения FastAPI degraded mode.
- [x] Поднять официальный AiAgent Gateway в Docker и проверить `healthy`/`healthz`.
- [x] Добавить переключаемый WebSocket-адаптер для legacy FastAPI и официального AiAgent Gateway.
- [x] Оставить legacy FastAPI WebSocket дефолтным самодостаточным degraded mode.
- [x] Добавить optional TurboQuant profile для vLLM long-context reasoning.
- [x] Добавить TurboQuant benchmark command/report.
- [x] Добавить TurboQuant quality benchmark на инженерных regression cases с term recall/missing terms.

## Этап 11. Завершение перехода на официальный AiAgent

- [ ] Проверить официальный pause/resume flow на живом Gateway:
  - [ ] запустить сценарий с approval-gated tool call;
  - [ ] получить pause/approval request;
  - [ ] подтвердить через FastAPI callback;
  - [ ] убедиться, что выполнение корректно продолжается;
  - [ ] проверить audit событий pause, approve/reject, resume.
- [x] Переключить безопасный UI-контур чата на `NEXT_PUBLIC_AGENT_WS_MODE=aiagent` через WebSocket-адаптер без удаления legacy режима.
- [x] Добавить smoke-тест WebSocket-адаптера official/legacy.
- [x] Зафиксировать fallback-процедуру в коде: при недоступности Gateway браузерная сессия возвращается к legacy FastAPI agent loop.

Блокер live-проверки: 2026-04-28 `aiagent agent --session-id codex-smoke --message "Reply with OK only" --json --timeout 30` через живой Gateway не вернул JSON за 45 секунд. Gateway health при этом OK, значит следующий шаг требует рабочего provider/model runtime для официального agent turn.

## Этап 12. Встроенный AI-сотрудник как основной контур

- [x] Отложить official AiAgent как optional integration, не блокирующую работу UI.
- [x] Добавить отдельный runtime config встроенного агента:
  - [x] включение/отключение агента;
  - [x] имя сотрудника;
  - [x] модель tool-calling;
  - [x] Ollama URL;
  - [x] backend URL;
  - [x] temperature;
  - [x] max steps;
  - [x] LLM/backend/approval timeouts;
  - [x] режим долговременной памяти;
  - [x] top-K памяти;
  - [x] лимит истории;
  - [x] allowlist инструментов;
  - [x] approval-gated tools;
  - [x] optional system prompt override.
- [x] Добавить backend API:
  - [x] `GET /api/ai/agent-config`;
  - [x] `PATCH /api/ai/agent-config`;
  - [x] `POST /api/ai/agent-config/reset`;
  - [x] `GET /api/ai/agent-skills`.
- [x] Подключить runtime config к встроенному `AgentSession`.
- [x] Подключить SQL-first память перед ответом агента:
  - [x] default `sql`;
  - [x] optional `sql_vector`;
  - [x] optional `sql_vector_rerank`;
  - [x] optional `hybrid`;
  - [x] optional `graph`.
- [x] Исправить чтение актуального registry key `tools` вместо старого `skills`.
- [x] Добавить UI-раздел “Встроенный агент” в настройках.
- [x] Добавить тесты на сохранение runtime config и чтение registry `tools`.
- [x] Сделать UI управления инструментами не textarea, а таблицей с поиском, чекбоксами allowlist и approval.
- [x] Расширить DB enum/API approval для опасных действий, которые сейчас подтверждаются только через WebSocket gate:
  - [x] `invoice.bulk_delete`;
  - [x] `warehouse.confirm_receipt`;
  - [x] `payment.mark_paid`;
  - [x] `procurement.send_rfq`;
  - [x] `bom.approve`;
  - [x] `bom.create_purchase_request`;
  - [x] `tech.process_plan_approve`;
  - [x] `tech.norm_estimate_approve`;
  - [x] `tech.learning_rule_activate`.
- [x] Добавить smoke/E2E реального chat turn через встроенный агент с mock Ollama/tool response.
- [x] Добавить сценарный regression набор: технолог, конструктор, нормировщик, кладовщик, закупщик.
- [x] Подключить role regression к реальному multi-step agent runner с mock Ollama на уровне WebSocket.
- [ ] Добавить live smoke встроенного агента против реальной локальной Ollama модели с коротким безопасным сценарием без внешних действий.
