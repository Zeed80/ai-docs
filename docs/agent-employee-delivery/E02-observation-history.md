# E02 — полная история наблюдений

Дата: 15 сентября 2026. Исходный commit: `0db67157`.
Статус: REVIEWED и DEPLOYED.

## Делегирование

- Сеньор: главный агент сессии; постановка контракта, независимый review,
  проверка и выпуск.
- Исполнитель: `gpt-5.6-terra`, reasoning high.
- Разрешённые файлы: `backend/app/api/chat_runs.py`,
  `backend/tests/test_chat_action_journal.py`,
  `frontend/components/chat/action-journal.tsx`,
  `frontend/tests/unit/action-journal.test.tsx`,
  `frontend/tests/e2e/chat-action-journal.spec.ts`.
- Циклов замечаний: 0. Сеньор прочитал фактический diff и выполнил независимые
  backend/frontend/Chromium проверки. Commit, deploy и push исполнителю не
  поручались.
- Учёт расхода по моделям недоступен; экономия лимитов не измерена.

## Реализованный контракт

- `GET /api/agent/chat-runs/{run_id}/actions/{action_id}/observations` доступен
  только владельцу запуска. Он проверяет связь action с run/work order, принимает
  `cursor >= 0` и `limit` от 1 до 100 и возвращает записи строго по `sequence`.
- Ответ является whitelist-представлением: автор, время, outcome, note и
  reference; всегда `verified=false` и `can_replay=false`. Внутренний payload не
  раскрывается, ссылка остаётся строкой и сервер её не читает.
- Маршрут использует `no_autoflush`, не создаёт событий и не меняет order, action,
  step или attempt. Старый идемпотентный POST наблюдения не менялся.
- UI отделяет последнее наблюдение от истории, загружает прежние записи только по
  кнопке, защищён от поздних ответов/смены карточки, убирает повторы по sequence и
  рендерит текст React-экранированием без raw HTML. Он не создаёт replay/resume.

## Проверки

Независимый прогон сеньора:

```bash
python3 -m pytest backend/tests/test_chat_action_journal.py backend/tests/test_action_receipts.py backend/tests/test_chat_checkpoints.py backend/tests/test_durable_chat.py -q
cd frontend
npm test
npm run typecheck
PLAYWRIGHT_MOCK_API=1 npx playwright test tests/e2e/chat-action-journal.spec.ts --project=chromium
```

Результат: 85 backend-тестов, 75 frontend unit-тестов и 4 Chromium mock-API
сценария прошли; typecheck прошёл. Покрыты cursor pagination без дублей, одинаковые
timestamps, чужие run/action, bounds limit, malformed UUID, XSS-подобная строка,
отсутствие записи через GET и отсутствие POST/replay в UI. Mock API не является
live E2E. Сохранилось известное предупреждение pytest о `asyncio_loop_scope`.

## Выкладка и ограничения

`make prod-build` завершён; backend/frontend/workers healthy,
`curl -k --fail https://localhost/health` → `{"status":"ok"}`. Хеш
`backend/app/api/chat_runs.py` совпал с backend и worker; production frontend
bundle содержит «Показать предыдущие наблюдения». Миграция не нужна. Push
накопленной ветки остаётся заблокированным автопроверкой из-за несвязанных
CAD-коммитов и не обходился.

Следующая карточка: E03, инвентаризация эффектов и транзакционных границ.
