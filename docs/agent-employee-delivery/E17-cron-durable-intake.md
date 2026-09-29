# E17 — cron через durable intake

Дата: 29 сентября 2026. Статус: **REVIEWED / TESTED / DEPLOYED**.

Beat больше не исполняет cron-поручение через отдельный headless
`AgentSession`/`execute_work_order_now`. Каждый наступивший слот создаёт ход
через E12 intake с ключом `<schedule UUID>:<UTC planned minute>`; два beat или
restart не создают дубль, а следующий день создаёт отдельную работу.
Просроченные слоты не догоняются задним числом. Исполнение остаётся в общем
WorkOrder runtime и проходит обычный human approval gate: cron не может
согласовать внешнее действие сам себе.

`AgentCron` получил owner и ссылку на standing grant. Создать расписание может
только действующий human admin со специальным `agent.cron.run` grant, чьи
точные constraints равны schedule и SHA-256 prompt. Обычный tool grant не
подходит. При каждом срабатывании заново проверяются активный admin,
неотозванный grant, срок по текущему UTC времени, точные ограничения и бюджет;
счётчик grant и durable intake коммитятся вместе. Старые расписания без owner
или grant не запускаются. Неверные/повреждённые grants блокируют только своё
расписание, не весь beat.

Независимая приёмка root: 82 backend-теста в связке cron, durable chat,
control plane и delegations; 124 теста в связке E12–E17, пройдены оба порядка
модулей после сужения тестовой очистки. Дополнительно проверен ownerless legacy
случай (19 cron-тестов). Ruff, format и diff check пройдены. Исполнитель:
`gpt-5.6-terra`; root выполнил два review-цикла и изолировал тестовую очистку.

Совместимая запись `AgentTask` создаётся для старого списка поручений, но её
`status=created` не отражает последующее завершение WorkOrder. Для результата
и аудита использовать связанный `work_order_id`; синхронизацию отображения
этой старой проекции проверить в E18/E20. Старый helper headless сохранён для
других существующих AgentTask API и не вызывается cron.

Production пересобран через `make prod-build`: backend, frontend и обычный
Celery worker healthy, beat запущен, `/health` вернул `{"status":"ok"}`,
Alembic в backend — `20260929_0005 (head)`.
