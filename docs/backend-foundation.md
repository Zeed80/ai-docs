# Backend foundation

## Что уже реализовано

Первый backend-срез создает основу для производственного cockpit:

- `ManufacturingCase` — центральный рабочий кейс заказа/детали/документов.
- `Document` — загруженный файл внутри кейса.
- `DocumentVersion` — первая версия файла с hash и путем хранения.
- `DocumentProcessingJob` — запуск обработки документа со статусом, parser name, ошибкой/fallback и JSON-результатом.
- `DocumentArtifact` — preview/render artifacts для OCR и просмотра.
- `Drawing` и `DrawingFeature` — результат AI-анализа чертежа/технического документа.
- `Material`, `Machine`, `Tool`, `Operation`, `ProcessPlan`, `NormEstimate` — базовая модель инженерного кейса технолога.
- `Supplier`, `Quote`, `Invoice`, `InvoiceLine`, `PriceHistoryEntry` — базовая модель закупочных документов и истории цен.
- `EmailThread`, `EmailMessage`, `EmailAttachment`, `DraftEmail` — базовая модель email workspace.
- `AgentAction`, `ApprovalGate` — журналируемые шаги OpenClaw-сценариев и блокировки внешних/опасных действий до approval.
- `AuditEvent` — человекочитаемая история действий.
- `AIRouter` подключен к endpoints классификации и извлечения.

## Запуск

Backend:

```bash
python3 -m uvicorn backend.app.main:app --reload --host 127.0.0.1 --port 8000
```

Или, если установлен `make`:

```bash
make dev
```

В `Makefile` используется `PYTHON ?= python3`; при необходимости можно переопределить интерпретатор: `make test PYTHON=python`.

Frontend cockpit:

```bash
cd frontend
npm install
npm run dev
```

По умолчанию frontend обращается к backend по `NEXT_PUBLIC_API_BASE_URL=http://127.0.0.1:8000`.

По умолчанию используется SQLite:

```text
APP_ENV=development
AUTO_CREATE_SCHEMA=true
DATABASE_URL=sqlite:///./data/app.db
STORAGE_ROOT=./data/storage
```

В dev-режиме backend может автоматически создать схему через SQLAlchemy `create_all`, чтобы сохранять быстрый локальный цикл.

Для production silent `create_all` запрещен. Нужно запускать миграции Alembic:

```bash
APP_ENV=production
AUTO_CREATE_SCHEMA=false
DATABASE_URL=postgresql+psycopg://user:password@host:5432/workspace
alembic upgrade head
```

Или через Makefile:

```bash
make db-upgrade
```

Если `APP_ENV=production` и `AUTO_CREATE_SCHEMA=true`, приложение падает при старте с явной ошибкой.

Encrypted local backup:

```bash
BACKUP_ENCRYPTION_KEY='change-this-secret' make backup
```

Backup script архивирует `data/app.db` и `data/storage`, затем шифрует архив через AES-256-GCM в `data/backups/*.tar.gz.aesgcm.json`. Без `BACKUP_ENCRYPTION_KEY` backup не создается. Для production нужно хранить ключ вне репозитория и ротацию/restore-процедуру описать в infra runbook.

Regression dataset manifests:

- `example-invoices/manifest.json` описывает локальный набор счетов без хранения expected банковских/клиентских значений;
- `docs/drawing-samples-manifest.json` фиксирует placeholder для будущих чертежей без коммита клиентских файлов;
- `make regression` проверяет, что manifest globs находят локальные файлы и expected fields не пустые.
- `AI_EVAL_MOCK=true make ai-eval` используется в CI как deterministic smoke без обращения к Ollama/vLLM.

CI:

- `.github/workflows/ci.yml` устанавливает backend dependencies на Ubuntu;
- запускает `make test`;
- запускает `make regression`;
- запускает `AI_EVAL_MOCK=true make ai-eval`.
- frontend Playwright e2e запускается через `make e2e` и покрывает создание кейса, upload, безопасный process-block для quarantined файла, audit timeline и approval gate через agent scenario.

OIDC/Auth settings:

```text
AUTH_LOCAL_BYPASS=true
OIDC_ISSUER_URL=https://auth.example.com/application/o/workspace/
OIDC_AUDIENCE=ai-manufacturing-workspace
OIDC_JWKS_URL=
```

В dev-режиме `AUTH_LOCAL_BYPASS=true` endpoint `GET /api/auth/me` возвращает локального admin-пользователя без внешнего Authentik. Для staging/production нужно выставить `AUTH_LOCAL_BYPASS=false`, `OIDC_ISSUER_URL` и `OIDC_AUDIENCE`; JWT проверяется по JWKS через `PyJWT[crypto]`. Если `OIDC_JWKS_URL` не задан, используется стандартный Authentik/Keycloak-style путь `.../protocol/openid-connect/certs`.

RBAC:

- `admin` получает `*`;
- `technologist` получает доступ к кейсам, документам, анализу чертежей, черновикам писем и agent scenarios;
- `accountant` получает чтение кейсов/документов, работу со счетами, export и черновики писем.

Матрица прав находится в `backend/app/auth.py`. Сейчас enforcement включен точечно на agent endpoints; rollout на все бизнес-endpoints нужно делать отдельным изменением, чтобы не смешивать security policy с уже реализованными production flows.

## Основные endpoints

- `GET /health`
- `GET /api/auth/me`
- `GET /api/auth/permissions`
- `POST /api/cases`
- `GET /api/cases`
- `GET /api/cases/{case_id}`
- `PATCH /api/cases/{case_id}`
- `POST /api/cases/{case_id}/documents`
- `GET /api/cases/{case_id}/documents`
- `GET /api/documents/{document_id}`
- `POST /api/documents/{document_id}/download-url`
- `POST /api/artifacts/{artifact_id}/download-url`
- `GET /api/files/signed/{token}`
- `POST /api/documents/{document_id}/process`
- `GET /api/tasks`
- `POST /api/tasks/{task_id}/run`
- `POST /api/tasks/run-next`
- `POST /api/documents/{document_id}/drawing-analysis`
- `POST /api/drawings/{drawing_id}/customer-question-draft`
- `POST /api/documents/{document_id}/invoice-extraction`
- `POST /api/invoices/{invoice_id}/anomaly-card`
- `POST /api/invoices/{invoice_id}/export.xlsx`
- `POST /api/invoices/{invoice_id}/1c-export`
- `POST /api/email/threads`
- `POST /api/email/imap/poll`
- `POST /api/email/drafts`
- `POST /api/email/drafts/{draft_id}/send`
- `GET /api/agent/tools`
- `POST /api/agent/scenarios/{scenario_name}/run`
- `GET /api/approvals`
- `POST /api/approvals/{gate_id}/approve`
- `POST /api/approvals/{gate_id}/reject`
- `GET /api/cases/{case_id}/audit`
- `POST /api/documents/{document_id}/classify`
- `POST /api/documents/{document_id}/extract`

## Обработка документов

`POST /api/documents/{document_id}/process` теперь работает queue-first: endpoint создает `TaskJob(type=document.process)` и возвращает задачу. Фактическое выполнение запускается через `POST /api/tasks/{task_id}/run` или `POST /api/tasks/run-next`. Внутри выполнения по-прежнему создается `DocumentProcessingJob`, поэтому история обработки документа не потеряна.

Upload защищен extension allowlist. Разрешенные расширения задаются через `UPLOAD_EXTENSION_ALLOWLIST`; по умолчанию разрешены производственные и офисные форматы: PDF, JPG/PNG, TXT/MD/CSV/JSON/XML, DOCX/XLSX, DXF, STEP/STP, IGES/IGS. Если расширение не разрешено, файл сохраняется в quarantine-зону, документ получает статус `suspicious`, обработка `process` возвращает `409`, а audit получает событие `document_quarantined`.

Поддержано:

- `.txt`, `.md`, `.csv`, `.json`, `.xml` как безопасное text extraction.
- PDF text layer через PyMuPDF, если установлен модуль `fitz`.
- DOCX через `python-docx`, если зависимость установлена.
- XLSX через `openpyxl`, если зависимость установлена.
- PDF page previews через PyMuPDF, если зависимость установлена.
- Image preview/normalization через Pillow, если зависимость установлена; без Pillow сохраняется безопасный preview artifact исходного изображения.
- OCR fallback через vision route `AITask.INVOICE_OCR`; AI вызывается только через `AIRouter`.
- Безопасный fallback для PDF без PyMuPDF/text layer и CAD-файлов.
- Structured extraction через `AIRouter` с Pydantic-схемой `StructuredDocumentExtraction`.
- Хранение результата в `Document.extraction_result_json` и `DocumentProcessingJob.result_json`, а не только в `ai_summary`.
- Хранение preview artifacts в `DocumentArtifact`.
- Audit events: `document_processing_started`, `document_processing_completed`, `document_processing_failed`, `document_artifact_created`.

Структурированный результат содержит `document_type`, `summary`, список полей `fields`, а у каждого поля есть `confidence` и `reason`. Конфиденциальность сохраняется: AI-запросы выполняются с `confidential=true` и не обходят `AIRouter`.

Optional зависимости для полного Ubuntu document processing:

```bash
python3 -m pip install ".[processing]"
```

## Signed File URLs

Файлы не раздаются напрямую по `storage_path`. Для скачивания backend выдает короткоживущую HMAC-подписанную ссылку:

- `POST /api/documents/{document_id}/download-url`;
- `POST /api/artifacts/{artifact_id}/download-url`;
- `GET /api/files/signed/{token}`.

Настройки:

```text
FILE_URL_SIGNING_SECRET=change-me
SIGNED_FILE_URL_TTL_SECONDS=300
```

Создание signed URL требует permission `document:read` и пишет audit event `signed_file_url_created`. Token содержит путь локального файла, имя файла, content type и expiry; при изменении подписи или истечении срока endpoint возвращает `403`.

## Анализ чертежей

`POST /api/documents/{document_id}/drawing-analysis` запускает AI-анализ технического документа через `AIRouter` и route `AITask.DRAWING_ANALYSIS`. Результат валидируется Pydantic-схемой `DrawingAnalysisResult`, затем сохраняется как `Drawing` и набор `DrawingFeature`.

Endpoint возвращает:

- основные атрибуты чертежа: title, drawing number, revision, material hint;
- features с dimensions, tolerance, confidence и reason;
- unclear items, risks и questions для технолога;
- audit event `drawing_analyzed`.

Если у документа уже есть image/PDF preview artifacts, они передаются в vision route как изображения. Если artifacts нет, endpoint использует text preview/extracted text и все равно сохраняет структурированный результат.

DXF/STEP:

- `.dxf` извлекается через `ezdxf`, если пакет установлен.
- `.step`/`.stp` сейчас читается как ISO-10303 header/entity counts; полноценная геометрия оставлена за FreeCAD/pythonOCC backend.
- DWG/IGES пока возвращают безопасный fallback.

`POST /api/drawings/{drawing_id}/customer-question-draft` формирует черновик письма/вопросов заказчику по неясностям чертежа через `AIRouter` и `AITask.EMAIL_DRAFTING`. Черновик не отправляется, всегда помечается `approval_required=true` и пишет audit event `customer_question_drafted`.

## Счета и поставщики

`POST /api/documents/{document_id}/invoice-extraction` извлекает счет через `AIRouter` и Pydantic-схему `InvoiceExtractionResult`, затем сохраняет:

- поставщика в `Supplier` с ИНН/КПП и банковскими реквизитами;
- счет в `Invoice`;
- строки счета в `InvoiceLine`;
- историю цен в `PriceHistoryEntry`.

После AI extraction backend выполняет строгие проверки кодом:

- `quantity * unit_price == line_total`;
- сумма строк совпадает с `subtotal_amount`;
- `subtotal_amount + tax_amount == total_amount`;
- duplicate по hash документа;
- duplicate по паре supplier + invoice number.

Результат пишет audit event `invoice_extracted`. Папка `example-invoices/` используется как локальный Ubuntu regression dataset: smoke-тест проверяет PDF text layer на реальном счете без сохранения expected customer data в fixtures.

Дополнительно реализовано:

- базовая anomaly card с severity, signals, recommended action и `approval_required=true`;
- supplier requisites diff по ИНН/КПП/банковским реквизитам;
- Excel export как локальный `DocumentArtifact` типа `invoice_excel_export`;
- 1С export placeholder как JSON payload без внешней отправки и с `approval_required=true`.

Связанные audit events:

- `invoice_anomaly_created`;
- `supplier_requisites_diff_detected`;
- `invoice_excel_exported`;
- `onec_export_prepared`.

## Email workspace

Email workspace пока реализован как безопасный backend-срез без внешних сетевых действий:

- `POST /api/email/threads` создает email thread, связывает его с `ManufacturingCase` и может сразу сохранить входящее сообщение.
- `POST /api/email/imap/poll` — placeholder IMAP polling adapter: пишет audit, но не подключается к внешнему серверу.
- `POST /api/email/drafts` создает черновик письма с deterministic risk-check.
- `POST /api/email/drafts/{draft_id}/send` всегда блокирует отправку до approval gate и пишет audit.

Risk-check сейчас отмечает отсутствующих получателей, финансовые/реквизитные термины и слишком длинное тело письма. Реальная SMTP-отправка должна появиться только после auth/RBAC и явного approval flow.

Связанные audit events:

- `email_thread_created`;
- `email_message_ingested`;
- `email_draft_created`;
- `email_send_blocked_for_approval`.

## GUI cockpit

Создано Next.js-приложение в `frontend/`:

- главный экран “Моя работа сегодня” с метриками, очередью кейсов и быстрым созданием `ManufacturingCase`;
- экран кейса `/cases/{caseId}` с вкладками: обзор, документы, чертеж, техпроцесс, инструмент, нормы, письма, закупка, история;
- upload документов в FastAPI backend;
- действия документа: process, drawing analysis, invoice extraction;
- audit timeline;
- правая AI-панель “Света”;
- command palette `Ctrl+K`.

Визуальная система сделана как производственный cockpit: теплый инженерный фон, сетка, glass-панели, выразительная serif-типографика для иерархии и компактная mono-навигация для статусов.

## Agent/OpenClaw

Этап OpenClaw добавляет безопасный слой agent tools поверх уже существующего API. Агент не вызывает backend произвольно: доступные tools описаны в `openclaw/skills/registry.json`, а backend заново проверяет имя tool при запуске сценария.

Registry генерируется из FastAPI OpenAPI/Pydantic schemas:

```bash
python3 scripts/generate_openclaw_registry.py
```

Или:

```bash
make openclaw-registry
```

Поддержанные tools:

- `case.create`;
- `document.upload`;
- `document.process`;
- `document.drawing_analysis`;
- `document.invoice_extraction`;
- `email.draft`;
- `email.send.request_approval`;
- `invoice.export.xlsx`;
- `invoice.export.1c.prepare`.

Сценарии хранятся в `openclaw/scenarios/`:

- `smart_ingest` — план обработки документа с ветками invoice/drawing и step limit.
- `drawing_review` — анализ чертежа и подготовка вопросов заказчику.
- `process_plan_draft` — безопасный placeholder без внешних действий.
- `draft_email` — подготовка письма и блокировка отправки до approval.

`POST /api/agent/scenarios/{scenario_name}/run` планирует allowlisted tools, отклоняет неизвестные tools, создает `TaskJob` для safe tools при наличии валидных локальных сущностей, создает `ApprovalGate` для approval-required tools и пишет audit events:

- `agent_scenario_started`;
- `agent_action_recorded`;
- `approval_gate_created`;
- `agent_scenario_completed`.

Правило безопасности остается жестким: email send, 1С export, external connectors и destructive changes останавливаются на approval gate. После approve создается `TaskJob`, который сейчас выполняет безопасный placeholder для SMTP/1С и пишет audit/result.

## Task Queue и Approval Cockpit

Для dev/prod v1 выбран DB-backed queue adapter без Redis/Celery. Это позволяет запускать production-подобный flow на текущем Ubuntu-сервере без дополнительного сервиса, а позже заменить executor на Celery/Redis без изменения REST-контракта.

`TaskJob` хранит:

- тип задачи;
- статус `pending/running/completed/failed/retry_scheduled/dead_letter/cancelled`;
- связи с кейсом, документом, agent action и approval gate;
- attempts/max attempts;
- payload/result/error;
- timestamps.

Approval flow:

- `GET /api/approvals` показывает pending gates;
- `POST /api/approvals/{gate_id}/approve` сохраняет actor/reason и создает execution task для approval-required внешнего действия;
- `POST /api/approvals/{gate_id}/reject` отклоняет gate и связанный agent action;
- выполнение approved task переводит gate в `executed`.

Связанные audit events:

- `task_job_created`;
- `task_job_started`;
- `task_job_completed`;
- `task_job_retry_scheduled`;
- `task_job_dead_lettered`;
- `approval_gate_approved`;
- `approval_gate_rejected`;
- `approval_gate_executed`.

GUI cockpit показывает approvals, task queue, quarantine status, signed download action, risk/anomaly panel и запуск OpenClaw scenarios из карточки кейса.

## Ограничения текущего среза

- Файлы хранятся локально, MinIO будет следующим storage backend.
- PDF render, PDF text layer, DOCX, XLSX и image normalization зависят от optional Python-пакетов и при их отсутствии возвращают безопасный fallback.
- DXF extraction зависит от optional `ezdxf`.
- STEP extraction пока не строит геометрию и не считает технологичность по телу модели.
- Тестовый и dev workflow ориентирован на Ubuntu-сервер: используйте `python3` или `make test`; Windows-specific test path не поддерживается.
- Excel export зависит от optional `openpyxl`.
- 1С export пока только готовит payload и имеет post-approval execution placeholder; реальный обмен должен идти через отдельный adapter.
- IMAP/SMTP adapters пока placeholders без внешнего подключения.
- Agent/OpenClaw scenarios уже создают tasks/gates; реальные внешние adapters должны добавляться по одному с отдельными тестами.
- Auth/OIDC и RBAC foundation добавлены; RBAC enforcement пока включен точечно, полный rollout по бизнес-endpoints будет следующим security hardening шагом.
- Signed file URLs работают для локального storage; при переходе на MinIO нужно заменить выдачу на presigned object URLs с тем же API-контрактом.
- Quarantine сейчас основан на extension allowlist; сигнатурный/mime scan и антивирусный adapter нужно добавить отдельным hardening шагом.
- Encrypted backup script есть для локального SQLite/storage; production backup policy должна быть привязана к PostgreSQL/MinIO после выбора deployment topology.
- Таблицы создаются через `create_all` только в dev-режиме при `AUTO_CREATE_SCHEMA=true`; для production используется Alembic.
