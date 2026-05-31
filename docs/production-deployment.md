# Production Deployment & Security

Цель: web-доступ к системе по домену с валидным TLS, **наружу открыты только порты 80 и 443**.
Всё остальное (Postgres, Redis, Qdrant, MinIO, Authentik, ML-серверы) живёт во внутренней
Docker-сети `app` и недоступно снаружи.

## Архитектура доступа

```
Internet ──80──> Traefik ──redirect──> 443 (TLS, Let's Encrypt, HSTS, security-headers)
                    │
   yourdomain/            → frontend:3000
   yourdomain/api,/ws     → backend:8000      (аутентификация в FastAPI)
   yourdomain/application,/if,/flows,/.well-known,/api/v3  → authentik-server:9000 (OIDC)
                    │
   internal-only: postgres, redis, qdrant, minio, ollama/llama-server/vllm-server,
                  celery-*, authentik-db
```

## Запуск production-стека

```bash
make prod          # docker compose -f docker-compose.yml -f docker-compose.prod.yml up
make prod-build    # то же + пересборка образов
make prod-down
```

`docker-compose.yml` **secure-by-default**: Traefik публикует только `80/443`. Insecure-dashboard
(8080) и прямые порты сервисов (Authentik `9100/9443`, ML `1143x`) добавляются ТОЛЬКО в
`docker-compose.dev.yml`. Docker compose **складывает** `ports` между файлами — убрать порт
оверлеем нельзя, поэтому база остаётся минимальной, а удобные порты добавляет dev-оверлей.

`docker-compose.prod.yml` подменяет конфиг Traefik на `traefik/traefik.prod.yml` + `traefik/prod/`
(монтирование `volumes` сливается по target — prod-конфиг заменяет dev-конфиг).

## ⚠️ docker-compose.ports.yml — ТОЛЬКО для локальной отладки

Файл `infra/docker-compose.ports.yml` пробрасывает на хост порты Postgres/Redis/Qdrant/MinIO/
backend/frontend для подключения DB-клиентов и API-инструментов. **Никогда не использовать в
production.** Он не подключается ни `make dev`, ни `make prod` — только вручную:

```bash
# локальная отладка, не на проде:
docker compose -f docker-compose.yml -f docker-compose.dev.yml -f docker-compose.ports.yml up
```

## Обязательные переменные окружения (infra/.env)

Production-валидатор в `backend/app/config.py` не даст backend стартовать, если в `APP_ENV=production`
остались dev-дефолты. Перед запуском заполните (см. `infra/.env.example` и `infra/scripts/gen-secrets.sh`):

```env
APP_ENV=production
AUTH_ENABLED=true
CSP_ENABLED=true

TRAEFIK_DOMAIN=yourdomain.com           # домен, на который указывает A-запись
TRAEFIK_ACME_EMAIL=admin@yourdomain.com # email для Let's Encrypt

CORS_ORIGINS=https://yourdomain.com
AUTHENTIK_EXTERNAL_URL=https://yourdomain.com
NEXT_PUBLIC_API_URL=same-origin
NEXT_PUBLIC_WS_URL=same-origin

# Секреты — сгенерировать, НЕ коммитить:
POSTGRES_PASSWORD=...        APP_SECRET_KEY=...        CSRF_SECRET=...
MINIO_ACCESS_KEY=...         MINIO_SECRET_KEY=...      AGENT_SERVICE_KEY=...
AUTHENTIK_SECRET_KEY=...     AUTHENTIK_DB_PASSWORD=... AUTHENTIK_BOOTSTRAP_PASSWORD=...
OAUTH_CLIENT_ID=ai-workspace OAUTH_CLIENT_SECRET=...
```

Генерация секретов:

```bash
bash infra/scripts/gen-secrets.sh        # печатает готовые строки для .env
```

## Firewall (хост)

Открыть наружу только web-порты (и SSH для администрирования):

```bash
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp     # SSH (ограничьте источником, если возможно)
ufw allow 80/tcp     # HTTP → редирект на HTTPS
ufw allow 443/tcp    # HTTPS
ufw enable
```

Docker может писать правила в `iptables` в обход UFW при наличии `ports:`. Поскольку база
не публикует ничего кроме 80/443, дополнительный риск отсутствует; для жёсткой гарантии можно
выставить `"iptables": false` в `/etc/docker/daemon.json` и управлять NAT вручную.

## Authentik OIDC

- Прямой порт Authentik (9100/9443) в production закрыт — вход идёт через Traefik на 443.
- `redirect_uris` в `infra/authentik/blueprints/ai-workspace.yaml` для prod строго привязаны к
  `https://${TRAEFIK_DOMAIN}/auth/callback` (без wildcard — защита от OAuth open-redirect).
- Смените `AUTHENTIK_BOOTSTRAP_PASSWORD` при первом запуске.

### (Опционально) Защита инфра-админок через ForwardAuth

MinIO console / Grafana / Prometheus не входят в prod-стек по умолчанию. Если их понадобится
выставить — публикуйте на сабдомене и навешивайте middleware `authentik-forwardauth`
(см. `traefik/prod/routes.yml`). Для этого в Authentik нужно вручную создать **Proxy Provider**
(forward auth, single application) и привязать его к **embedded outpost** — это не автоматизировано
в blueprint намеренно, т.к. настройка зависит от версии и сабдоменов.

## Что НЕ заворачивается в ForwardAuth

`/`, `/api`, `/ws` обслуживаются приложением: аутентификация — единый источник правды в FastAPI
(`backend/app/auth/jwt.py`, `_auth` dependency). Forward-auth на этих путях сломал бы cookie-OIDC
SPA-флоу, WebSocket и сервис-аккаунты по `X-API-Key`.
