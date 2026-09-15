# E01 — UI текущей сверки квитанции

Дата: 15 сентября 2026. Исходный commit: `2f0c8f2f`.
Статус: REVIEWED и DEPLOYED.

## Делегирование

- Сеньор: главный агент сессии; постановка контракта и независимый review.
- Исполнитель: `gpt-5.6-terra`, reasoning high.
- Разрешённые файлы: `frontend/components/chat/action-journal.tsx`,
  `frontend/tests/unit/action-journal.test.tsx`,
  `frontend/tests/e2e/chat-action-journal.spec.ts`.
- Циклов замечаний: 0. Сеньор проверил фактический diff и независимо повторил
  unit/typecheck/Chromium проверки. Commit/deploy не делегировались.
- Учёт расхода по моделям недоступен; экономия лимитов не измерена.

## Реализованный контракт

- В секции квитанции есть GET-only кнопка «Проверить текущее состояние».
- Отдельно видны ответ worker, квитанция прошлого database commit и текущий
  read-only snapshot. Сверка не вызывает tool, replay, resume или POST.
- Старый verdict очищается до каждого нового GET. AbortController, номер запроса
  и ключ карточки не позволяют позднему ответу попасть в другую карточку.
- 403 сообщает об отсутствии текущего admin-доступа; 409 — об ошибке целостности;
  500 остаётся явной ошибкой. Ни один из этих путей не оставляет прежний matched.
- `changed`/`missing` не добавляют кнопок выполнения; человеческое наблюдение
  остаётся отдельным непроверенным действием.

## Проверки

Независимый прогон сеньора:

```bash
cd frontend
npm test -- --run tests/unit/action-journal.test.tsx
npm run typecheck
PLAYWRIGHT_MOCK_API=1 npx playwright test tests/e2e/chat-action-journal.spec.ts --project=chromium
```

Результат: 18 unit-тестов и 3 Chromium mock-API сценария прошли; typecheck прошёл.
Покрыты matched/changed/missing, 403/409/500, повторная сверка, поздний ответ,
смена карточки, отсутствие POST/resume/replay. Mock API не является live E2E.

## Выкладка и ограничения

`make prod-build` завершён; backend/frontend/workers healthy,
`curl -k --fail https://localhost/health` → `{"status":"ok"}`. Новый текст
«Проверить текущее состояние» найден в production frontend bundle
`.next/server/app/work-orders/chat-journal/page.js`. Backend и миграции не менялись.
Существующие необязательные сервисы без healthcheck не выданы за healthy.
Следующая карточка: E02, история наблюдений.
