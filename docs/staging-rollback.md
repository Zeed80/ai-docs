# Staging: деплой и откат (Б19)

## Первый запуск

1. Скопировать `infra/.env.example` → `infra/.env.staging`, задать как
   минимум: `TRAEFIK_DOMAIN` (staging-поддомен, не совпадает с prod),
   `TRAEFIK_ACME_EMAIL`, `POSTGRES_PASSWORD`, `APP_SECRET_KEY`,
   `CSRF_SECRET` — независимые от prod значения (staging никогда не должен
   делить БД, секрет или TLS-сертификат с production).
2. `make staging-build` — собрать и поднять.
3. Проверить: `curl -sf https://$TRAEFIK_DOMAIN/health` и живой
   Playwright-смоук (`make e2e` с `NEXT_PUBLIC_API_BASE_URL`, указывающим
   на staging).

## Обновление (новый релиз на staging)

```bash
git fetch && git checkout <новый tag/commit>
make staging-build
```

## Откат

```bash
git checkout <предыдущий рабочий tag/commit>
make staging-build
```

После отката проверить:

1. `docker compose -f infra/docker-compose.yml -f infra/docker-compose.staging.yml --env-file infra/.env.staging -p infra-staging ps` — все сервисы `running`, ни один не в цикле рестартов (`restart: on-failure` — если сервис не поднимается, он остановится и будет ВИДЕН как `Exited`, не будет маскироваться бесконечным перезапуском, как на prod).
2. `/health` эндпоинт backend отвечает 200.
3. Миграции БД: если новый релиз добавлял alembic-миграцию, откат кода НЕ откатывает схему автоматически — проверить `alembic current` внутри контейнера `backend`, при необходимости `alembic downgrade` вручную ДО отката кода на более старую ревизию, которая ждёт более старую схему.
4. Один живой smoke-сценарий через UI (создать `WorkOrder` на `/work-orders`, убедиться, что план строится) — не только health-check процесса.

## Почему отдельный `infra/docker-compose.staging.yml`, а не флаг на prod-конфиге

См. комментарий в самом файле — `restart: on-failure` вместо
`unless-stopped` (сломанный staging-деплой должен быть виден как
остановленный, не маскироваться бесконечным перезапуском) и обязательный
отдельный `--env-file`/`--project-name`, чтобы staging физически не мог
задеть данные/секреты/сертификат prod на одном хосте.
