# Аудит проекта по `refact.md`

Дата: 2026-04-27.

## Краткий вывод

Проект уже вышел за рамки планового workspace: есть FastAPI backend, Next.js UI, Celery, модели документов/счетов/поставщиков, quarantine, approvals, audit, email, export, warehouse/procurement/payment и собственный AiAgent-like agent loop.

Главный разрыв с `refact.md`: продукт всё ещё частично выглядит как широкий AI Manufacturing Workspace и содержит собственную реализацию агентного слоя. Целевое состояние — узкий, надёжный AI-сотрудник документооборота по счетам/закупкам/складу/финконтролю, где официальный open-source AiAgent является control plane, а FastAPI остаётся источником истины и единственным исполнителем доменных действий.

## Что уже есть

- `Document`, `DocumentVersion`, `DocumentExtraction`, `ExtractionField`, `DocumentArtifact`, `DocumentProcessingJob`, quarantine и signed URLs.
- `Invoice`, `InvoiceLine`, `Party`, `SupplierProfile`, `CanonicalItem`, `PriceHistoryEntry`, `AnomalyCard`, `Approval`, `AgentAction`, `ExportJob`, `DraftEmail`.
- Детерминированные проверки счетов, базовая проверка цен, дубликаты по hash, audit timeline, approval gates.
- AI-router/model registry с local-only политикой; API-слой больше не импортирует `ollama_client` напрямую.
- Skills registry из OpenAPI и whitelist/approval list в `aiagent/config/gateway.yml`.
- Графовая память документов: узлы, связи, чанки, evidence spans, mentions, поиск, переиндексация архива, очередь проверки гипотез.
- Базовый технологический контур: ресурсы производства, маршрутные техпроцессы, операции, нормы времени, черновик техпроцесса из памяти документа.

## Критичные разрывы

- `backend/app/ai/agent_loop.py` остаётся кастомным control plane. Его нужно заменить официальным AiAgent Gateway поэтапно, сохранив whitelist, approval gates, audit и step limits.
- Email send пока является stub-flow: risk-check есть, но production SMTP и hard approval execution нужно довести до безопасного состояния.
- `AnomalyCard` и invoice checks есть, но structured check results не стали отдельной полной сущностью с историей всех сигналов.
- Price history работает частично: хорошая автономность требует canonical item mapping, supplier alternatives и сравнение КП на ежедневном workflow.
- UI содержит cockpit-экраны, но часть API contracts расходится с backend, а review/approval/email flows требуют e2e-проверки.
- Документация (`PLAN.md`, `DEVPLAN.md`, `CLAUDE.md`) смешивает greenfield-описание, manufacturing scope и текущую реализацию.
- Технологический контур пока детерминированный: нужны библиотека операций, расчёты режимов резания, проверка технологичности и approval утверждения техпроцессов.

## Приоритеты

- P0: зелёный test harness, единый AI-router, запрет прямых AI-вызовов из business/API кода, production guard против silent schema create.
- P0: официальный AiAgent migration spike с реальным Gateway, generated tools, deny unknown tools, approval pause/resume и audit каждого tool call.
- P1: нормализовать email workspace, export prepare/send, structured invoice check results и anomaly lifecycle.
- P1: привести UI cockpit к backend contracts и закрыть Playwright сценарии keyboard-first review/approval/email/export.
- P1: развить `tech.*` skills до полноценного инженера-технолога: шаблоны ЕСТД, операции, нормы, оборудование, инструмент, оснастка, контроль качества.
- P1: включить feedback loop по правкам технолога и графовой памяти.
- P2: learning loop на правках, supplier trust score, продвинутый compare КП и мониторинг качества extraction.

Подробный статус реализации и следующий чеклист: `docs/refact-implementation-status.md`.
