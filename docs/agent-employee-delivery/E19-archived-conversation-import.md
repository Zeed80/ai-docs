# E19 — безопасный перенос архивного разговора

Дата: 30 сентября 2026. Статус: **REVIEWED / TESTED / DEPLOYED**.
Base commit: `c926f771`. Commit результата: scoped commit root после production-проверки.

## Изменено

- `backend/app/db/agent_runtime_models.py` и
  `backend/migrations/versions/20260930_0001_archived_conversation_imports.py`:
  отдельная immutable/inert provenance-запись с owner, source/target session,
  request id/digest, очищенными snapshot records и идентификаторами выбранных
  вложений. Это не `ChatMessage`, checkpoint, approval или очередь tool calls.
- `backend/app/api/chat_runs.py`: owner-only human endpoint явного выбора
  user/assistant сообщений и вложений. Source не изменяется. Одинаковый
  owner/request id сериализуется advisory lock и возвращает тот же target;
  другое тело с тем же request id получает 409. Чужой, удалённый или durable
  source, role tool/approval, orphaned/foreign/reassigned attachment отклоняются.
  Metadata исходного сообщения не копируется.
- `backend/app/domain/agent_intake.py`: server-owned import id определяется только
  по owner+target session и записывается рядом, а не берётся из клиентского
  `workspace_context`. Обычный legacy guard не ослаблен. До первого intake новая
  target session не содержит `ChatMessage`.
- `backend/app/tasks/durable_chat.py`: worker заново проверяет owner и soft-delete
  source/target и текущее ownership каждого выбранного документа. Архив передаётся
  модели как JSON-quoted untrusted data с явным запретом считать старые роли,
  approvals, permissions или tool-like текст полномочием и повторять действие.
  Revoked attachment полностью исключается из model prompt.
- `frontend/components/chat/assistant-panel.tsx`, `frontend/lib/api.ts`: оператор
  выбирает конкретные сообщения checkbox-ами и создаёт новый разговор. Request id
  сохраняется в `localStorage` до подтверждённого ответа, поэтому повтор после
  потерянного ответа или reload использует ту же идемпотентную операцию. Если
  браузер не может сохранить ключ, mutation не начинается.
- `backend/tests/test_chat_archive_import.py` и
  `frontend/tests/unit/assistant-panel-durable.test.tsx`: позитивные и негативные
  сценарии E19, конкурентный double import и migration round trip.

## Не изменено

- Архивный WS checkpoint не возобновляется; E20 и удаление compatibility lifecycle
  не входят в E19.
- Source `ChatSession` и его `ChatMessage` не переписываются и не удаляются.
- Existing approval/RBAC/tool execution contracts не расширены. Архивная запись
  не создаёт `ChatLogicalAction`, `Approval`, checkpoint или receipt.
- Deploy, production rebuild, health check, commit и push исполнителем не делались
  по прямой границе поручения.

## Проверки

- `python3 -m pytest backend/tests/test_chat_archive_import.py backend/tests/test_durable_chat.py -q --tb=short`
  — **40 passed**, одно известное предупреждение о неизвестной настройке
  `asyncio_loop_scope`. Запущено с отдельным PostgreSQL testcontainer, не с
  production `DATABASE_URL`.
- `npx vitest run tests/unit/assistant-panel-durable.test.tsx` — **5 passed**.
- `npm run typecheck` — **passed**.
- `ruff format --check ...` — **6 files already formatted**.
- `ruff check ...` — **All checks passed**.
- `git diff --check` — **passed**.
- Независимый прогон root после последнего diff:
  `python3 -m pytest backend/tests/test_agent_channel_parity.py backend/tests/test_chat_archive_import.py backend/tests/test_durable_chat.py backend/tests/test_agent_cron_dispatch.py -q --tb=short`
  — **64 passed**; E19 + chat sessions + durable chat — **43 passed**.
  Frontend unit — **5 passed**, typecheck — **passed**.
- Mock-API browser regression `PLAYWRIGHT_MOCK_API=1 npx playwright test tests/e2e/durable-chat.spec.ts --project=chromium`
  — **3 passed** после разрешения локального webServer; первый запуск в sandbox
  не смог запустить webServer. Во втором прогоне proxy выдавал шумные
  `ECONNREFUSED` на неиспользуемый localhost:8000, но все три mock-теста прошли.
- Отдельный первый повтор backend после остановки прошлого testcontainer не дошёл
  до тестов: sandbox запретил Docker socket. Повтор с разрешённым изолированным
  testcontainer завершился 6 passed; итоговый объединённый прогон — 40 passed.

## Конкурентность и безопасность

Два независимых DB session одновременно отправляют одинаковый owner/request id.
Оба получают один target session, один ответ `created=true`, второй
`created=false`; в БД остаётся одна import row. Sequential retry с изменённым
телом получает 409.

Тест worker-а подтверждает, что архив не попадает в `hydrate_history`, metadata с
старым tool call/approval/permission и секретным аргументом отсутствует в snapshot
и prompt, а `chat.tool_call` не создаётся. После reassignment вложения его имя и
descriptor не доходят до модели. После soft-delete source worker останавливается
до создания модели. Foreign session и service account получают отказ.

## Ограничения и rollback

- Выбранные attachments намеренно принимаются только при текущем точном
  `Document.owner_sub == owner`; department/shared legacy visibility не переносится.
  Это более узкая, fail-closed граница, а не полный перенос всех читаемых документов.
- Архивный текст остаётся недоверенным model input. Prompt маркирует его данными,
  но защита от повторного эффекта обеспечивается прежде всего отсутствием старых
  executable records и сохранением обычных tool/approval gates для любого нового
  решения модели, а не обещанием абсолютной устойчивости LLM к prompt injection.
- Snapshot ограничен 48 KB, 100 сообщениями и 20 вложениями; бинарные payload,
  storage path, extraction text, токены и старые grants не сохраняются.
- Rollback: downgrade удаляет только новую inert import table. Созданные target
  chat sessions отдельно не удаляются автоматически, чтобы downgrade не стирал
  пользовательские данные.

Исполнитель: `gpt-5.6-sol`. Цикл review:
первый scoped cycle, замечания root о attachment revalidation, service account,
soft-delete, устойчивом request id и конкурентном импорте закрыты тестами.
Экономия не измерена.

Review gate: root принял E19 после независимой проверки. E20 может начинаться
после production build/health и scoped commit E19. Старый WS остаётся включённым
до E20, архивные checkpoint/approvals не возобновляются.

Production пересобран через `make prod-build`: backend, frontend и обычный
Celery worker healthy, beat запущен; `/health` вернул `{"status":"ok"}`.
Alembic в backend — `20260930_0001 (head)`. Режим live Telegram delivery
этой карточкой не включался.
