# Миграция на официальный AiAgent

## Цель

Заменить кастомный agent loop в `backend/app/ai/agent_loop.py` официальным open-source AiAgent Gateway, не теряя гарантий `refact.md`: FastAPI остаётся источником истины, внешние действия требуют approval, неизвестные tools запрещены, каждый шаг агента аудируется.

Актуальные источники для реализации:

- `https://github.com/aiagent/aiagent`
- `https://docs.aiagent.ai/gateway`
- `https://github.com/aiagent/aiagent/blob/main/docs/gateway/configuration.md`

## Целевой контракт

- AiAgent Gateway работает отдельным процессом/control plane.
- FastAPI предоставляет tools через OpenAPI/Pydantic и generated registry.
- Next.js подключается к AiAgent для агентного чата и к FastAPI REST для degraded mode.
- Confidential OCR/extraction остаётся local-only через FastAPI `AIRouter`.
- Gateway bind/auth настраиваются так, чтобы control plane не был открыт наружу без auth.

## Этапы

1. Зафиксировать текущий baseline:
   - `python3 -m pytest backend/tests/ai backend/tests/domain -q`
   - registry generation через `scripts/generate_aiagent_registry.py`
   - сценарии `smart-ingest`, `email-triage`, `assisted-review`, `anomaly-resolution`.

2. Поднять официальный AiAgent параллельно:
   - Node 24 или 22.14+.
   - `npm install -g aiagent@latest`.
   - `aiagent onboard --install-daemon` для локальной проверки.
   - локальный Gateway порт оставить внутренним; внешний доступ только через защищённый route.

3. Сгенерировать tools из FastAPI:
   - использовать `scripts/generate_aiagent_registry.py` как machine-readable policy registry;
   - `default=deny`, `unknown_tools=deny`;
   - external actions: `email.send`, `invoice.export.1c.prepare`, `invoice.approve`, `invoice.reject`, `anomaly.resolve`, `norm.activate_rule`, payment/warehouse/procurement gates.

4. Перенести agent runtime:
   - системный prompt из `aiagent/prompts/base.md`;
   - роли accountant/buyer/manager из `aiagent/prompts/role-*.md`;
   - сценарии из `aiagent/scenarios/*.yml`;
   - session/audit события писать в FastAPI `AgentAction`.

5. Оставить fallback:
   - `backend/app/ai/agent_loop.py` не удалять до прохождения сценарных тестов;
   - включать его только feature flag для rescue/dev;
   - после стабильного Gateway удалить кастомный loop отдельным PR.

## Acceptance criteria

- AiAgent не может вызвать tool вне generated allowlist.
- Вызов approval-gated tool создаёт approval и останавливает агент до решения человека.
- Отклонённый approval не выполняет доменное действие.
- Confidential document tasks не уходят в cloud model.
- Degraded mode UI продолжает работать через FastAPI REST при остановленном AiAgent.

## Локальный контракт перед запуском Gateway

Добавлена проверка:

```bash
make aiagent-contract
```

Она сверяет `aiagent/config/gateway.yml`, `aiagent/skills/_registry.yml` и YAML-сценарии:

- generated approval-required tools должны быть в `approval_gates`;
- `approval_gates` не должны помечать generated non-approval tools как gated;
- registry должен читаться как официальный YAML-контракт;
- пока не реализованные legacy skills выводятся предупреждениями, а не ошибками.

Перед финальным переключением на официальный AiAgent нужно запустить strict-режим:

```bash
python3 scripts/check_aiagent_contract.py --strict
```

Strict должен стать зелёным после удаления или реализации оставшихся legacy skills из `gateway.yml` и сценариев.

Для запуска официального Gateway без legacy-предупреждений добавлен generated strict-конфиг:

```bash
make aiagent-strict
```

Команда генерирует `aiagent/config/gateway.strict.yml` из `aiagent/skills/_registry.yml`.
В strict-конфиг попадают только реализованные FastAPI tools, только реальные `approval_required`
gates и только сценарии, все skills которых есть в generated registry.

Важно: официальный AiAgent читает JSON5 config из `~/.aiagent/aiagent.json` и строго
валидирует схему. Поэтому `gateway.yml`/`gateway.strict.yml` остаются контрактом проекта,
а не прямой заменой официального config. Для переноса allowlist в официальный формат
генерируется пример:

```bash
python3 scripts/generate_aiagent_official_sample.py
```

Результат: `aiagent/config/aiagent.official.sample.json`. Его нужно использовать как
основу для `~/.aiagent/aiagent.json` после подключения FastAPI tools через официальный
адаптер skills/plugin.

## Pause/resume approval callbacks

Для официального Gateway добавлен control-plane API:

- `POST /api/aiagent/approvals/request` — создать approval для approval-gated tool call и записать `AgentAction`;
- `GET /api/aiagent/approvals/{approval_id}/resume` — проверить решение и вернуть Gateway данные для продолжения или остановки.

Эти endpoints не являются agent skills и не добавляются в AiAgent tool registry. Они нужны самому Gateway.
Внутри используется общий тип approval `agent.tool_call`, а исходный tool name и args сохраняются в `Approval.context`.
