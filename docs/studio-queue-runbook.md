# Studio Queue Runbook

Операционный слой графической студии находится в `/studio -> Очередь`.

## Когда включать Pause и Drain

- `Pause` запрещает новые задачи сразу. Используйте при аварии, неверной модели,
  миграции ComfyUI или нехватке диска.
- `Drain` запрещает новые задачи, но оставляет текущие в работе. Используйте
  перед деплоем, перезапуском GPU worker или обслуживанием ComfyUI.
- `Cancel pending` отменяет только ожидающие задачи. Running-задачи отменяйте
  адресно, чтобы не прерывать чужую долгую LoRA-тренировку без решения оператора.

## Быстрая проверка после деплоя

```bash
make prod-build
curl -k -fsS https://localhost/health
. infra/.env
API_URL=https://localhost API_KEY="$AGENT_SERVICE_KEY" make studio-queue-smoke
```

`make studio-queue-smoke` по умолчанию read-only: параллельно дергает
`/api/studio/queue` и `/api/studio/queue/stats`, но не ставит GPU-задачи.

## Нагрузочная постановка задач

Запускайте только когда GPU можно безопасно загрузить:

```bash
. infra/.env
API_URL=https://localhost API_KEY="$AGENT_SERVICE_KEY" \
  python3 scripts/studio_queue_load_smoke.py --enqueue --requests 12 --concurrency 4
```

Ожидаемые результаты:

- `200` или `201` — задача поставлена;
- `429` — сработал per-user limit;
- `503` — очередь на pause/drain или global backpressure.

`404` и `5xx` считаются ошибкой smoke.

## Что смотреть при сбоях

```bash
docker compose -f infra/docker-compose.yml -f infra/docker-compose.prod.yml --env-file infra/.env ps
docker compose -f infra/docker-compose.yml -f infra/docker-compose.prod.yml --env-file infra/.env logs --since=10m backend celery-worker-gpu celery-worker-lora --no-color
```

Проверочные API:

- `GET /api/studio/queue/stats` — depth, limits, среднее ожидание/выполнение;
- `GET /api/studio/queue?status=failed` — failed/dead-letter задачи;
- `POST /api/studio/queue/{job_id}/retry` — повторить failed-задачу;
- `PATCH /api/studio/queue/control` — pause/drain.

## Мобильное приложение

Мобильный WebView использует те же REST endpoints. Realtime идет через SSE
`/api/studio/queue/events`; если сеть/фон WebView закрывает поток, UI остается
рабочим за счет polling fallback.
