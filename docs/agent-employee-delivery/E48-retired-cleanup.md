# E48 — удаление списанных поверхностей

Дата: 10 октября 2026.

Генерация инструментов агентом, их «эволюция» с теневым A/B-продвижением,
кэш результатов скиллов, песочница предложений и контейнер `skill-runner`
были отключены раньше: `sandbox-apply`/`decide`/`promote` уже отвечали 410,
`allow_capability_builder` принудительно выключался валидатором конфигурации.
Код при этом остался и продолжал работать впустую. Beat каждые 2 часа
запускал эволюцию, и та записывала в журнал «пропущено». Builder писал
`.py`-черновики, которые нельзя было активировать. В чат приходила
карточка «Новая capability» с кнопкой «Разрешить», хотя сервер на неё
отвечал 410, а интерфейс всё равно показывал «одобрено».

## Инвентаризация

| Поверхность | Кто вызывал | Статус | Решение |
|---|---|---|---|
| `app/ai/capability_builder.py` | оркестратор при `allow_capability_builder` (всегда False) | мёртвый | удалён |
| ветка «нехватка возможности → черновик → предложение» в оркестраторе (`_publish_capability_gap` и 7 помощников) | `_should_report_capability_gap` — всегда False | мёртвый | удалена |
| `app/ai/skill_evolver.py`, `app/tasks/skill_evolution.py` | beat `evolve-failing-skills` (2 ч), `evaluate-shadow-tests` (30 мин) | работал впустую | удалены вместе с расписанием |
| `app/ai/capability_sandbox.py` | только тесты | архив | удалён |
| `app/ai/skill_cache.py` | только эндпоинт статистики | мёртвый | удалён |
| `app/ai/generated_skills/` | builder | архив | удалён |
| `self_refine.refine_code` | builder | мёртвый | удалён (`refine`, `revise_with_issues` остались) |
| `infra/skill-runner`, сервис compose, `SKILL_RUNNER_URL`, `settings.skill_runner_url` | `/generated-skill` (уже 410) | мёртвый | удалены |
| `/api/agent/generated-skill/*`, `/generated-skills`, `/skill-evolution/*`, `/skill-cache/stats` | старые клиенты | deprecated API | 410 с объяснением, скрыты из схемы |
| `/api/agent/capabilities/{id}/sandbox-apply`, `/decide`, `/promote` | агент (`capability.sandbox_apply` в gateway), настройки, чат | deprecated API | 410, скрыты из схемы; инструмент снят с агента и из реестров |
| карточка одобрения capability в чат (`_publish_capability_approval_request`) и автоодобрение (`_safe_auto_approval_reason` с помощниками) | `POST /capabilities/propose` | обещание без исполнения | удалены: предложение — это отчёт |
| `POST /capabilities/propose`, `GET /capabilities`, `/status` | агент, настройки | используется | оставлены: это отчёт о нехватке |
| переключатели «Разрешить выявлять недостающие tools/skills» и «Требовать подтверждение перед builder-режимом», кнопки Sandbox/Разрешить/Отклонить | `/settings` | ложные обещания | убраны; список отчётов только для чтения, с пояснением |
| поля `allow_capability_builder`, `capability_builder_requires_approval` | сохранённые конфиги, старые клиенты | deprecated | оставлены, чтобы конфиги загружались; валидатор держит их выключенными, по умолчанию False; остаются protected settings |

Одобрения по ключевым словам и теневого продвижения в интерфейсе, кроме
перечисленного, не нашлось.

## Что не удалялось

- Таблица `capability_proposals` и её строки — это история. Список на
  `/settings` по-прежнему показывает и открытые отчёты, и закрытые.
- `backend/data/agent_sandbox` (744 каталога, в `.gitignore`) — данные
  стенда. Код больше не пишет туда.
- Записи журнала эволюции в Redis (`skill_evolver:*`) — их никто не читает,
  со временем истекут.

## Миграция

Недостающую возможность добавляет разработчик обычным выпуском: endpoint,
Pydantic-схема, `capabilities.yml`, регенерация реестра. Разовая работа с
кодом — это шаг `script` рабочего заказа (E26–E30). Старая карточка
capability в истории чата теперь закрывается как «устаревший запрос» и ничего
не отправляет.

Генератор реестра (`infra/scripts/generate-skill-registry.py`) пропускает
маршруты с `include_in_schema=False`: списанная поверхность продолжает
отвечать 410, но инструментом агента не становится.

## Проверки

- `python3 -m pytest backend/tests/test_e48_retired_surfaces.py -q`:
  - 410 на каждом списанном маршруте;
  - удалённые модули не находятся, и ни один модуль `app/` их не импортирует;
  - в beat нет задач `skill.*`;
  - ни в gateway, ни в реестрах нет инструментов со списанными путями;
  - эти пути отсутствуют в OpenAPI;
  - в compose нет `skill-runner`;
  - конфиг не включает builder;
  - `propose` не шлёт карточку одобрения.
- `test_orchestrator.py`, `test_agent_control_plane.py`,
  `test_capability_catalog_consistency.py`, `test_agent_execution_boundary.py`.
- `PLAYWRIGHT_MOCK_API=1 npx playwright test tests/e2e/agent-control-plane.spec.ts --project=chromium`:
  отчёты только для чтения, без кнопок и переключателей builder. Попутно
  починен мок `/api/auth/me` (`roles: []` уводил с админского экрана, и два
  старых теста падали).

## Выкладка (11 октября 2026)

Пересобраны backend, celery-* и frontend; контейнер `infra-skill-runner-1` удалён.
На ptsai.ru под тестовым аккаунтом:

- шесть списанных маршрутов отвечают 410 с пояснением;
- список отчётов (26) читается;
- на `/settings` нет переключателя builder и кнопки Sandbox, пояснение видно;
- живой ход «Сколько счетов в системе?» завершён ответом «Всего счетов: 153», в `invoices` 153 строки;
- в логах воркеров, beat и API нет ImportError и unregistered task.
