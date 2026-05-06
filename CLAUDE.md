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
- **Agent**: AiAgent Gateway v2026.4.x (TS) — AI-сотрудник «Света»
- **Backend**: Python / FastAPI + Celery + Redis
- **Frontend**: Next.js (PWA) + next-intl (RU по умолчанию)
- **DB**: PostgreSQL, Qdrant (vector), MinIO (files)
- **AI**: Ollama (gemma4:e4b локально для OCR, gemma4:26b или Claude API для reasoning)
- **Auth**: Authentik (self-hosted SSO)
- **Infra**: Docker Compose, Traefik

## Архитектурные принципы
- AiAgent = мозг (planning, reasoning), FastAPI = руки (CRUD, data, async tasks)
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
aiagent/          — config, prompts, skills, scenarios
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
