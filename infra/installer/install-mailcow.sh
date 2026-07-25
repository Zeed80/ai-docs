#!/usr/bin/env bash
#
# Install / re-configure the self-hosted mail server (Mailcow) that backs
# @<domain> mailboxes for the company. Runs as a SEPARATE docker-compose
# project next to the main stack (infra/mailcow/) — this is upstream's own
# deployment model, not something we can fold into infra/docker-compose.yml.
#
# What this script does:
#   1. Clones mailcow-dockerized pinned to a specific release tag (never
#      `master` — pin so upgrades are a deliberate, staged action via
#      update-mailcow.sh, not a silent `git pull`).
#   2. Generates mailcow.conf (hostname, timezone), disables mailcow's own
#      Let's Encrypt client (SKIP_LETS_ENCRYPT=y) — TLS is terminated by the
#      Traefik instance already running for the rest of the stack.
#   3. Writes docker-compose.override.yml so nginx-mailcow (webmail/admin/API)
#      joins the existing Traefik network — Traefik's file-provider routes.yml
#      reaches it by container name, same pattern as the `backend`/`frontend`
#      services.
#   4. Prints the exact DNS records and firewall ports still needed (this
#      script cannot manage DNS or provider firewalls) — see
#      infra/installer/mailcow.README for the full checklist.
#
# Mailcow's own compose internals (port variable names, network layout) can
# change between releases — this script only touches config that's stable
# across the documented Traefik reverse-proxy setup (docs.mailcow.email/post_installation/reverse-proxy).
# If a pinned-tag upgrade changes that layout, re-check the override below.
#
# Usage:
#   infra/installer/install-mailcow.sh [--domain mail.example.com] [--tag 2026-05c] [--yes]
#
set -euo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
ROOT_DIR="$(cd "$SELF_DIR/../.." && pwd)"
. "$SELF_DIR/lib.sh"
cd "$ROOT_DIR"

ENV_FILE="infra/.env"
MAILCOW_DIR="infra/mailcow"
# Single source of truth for the pinned release: MAILCOW_TAG in infra/.env
# (install and update scripts must never carry two copies that can drift).
# FALLBACK_TAG applies only to a fresh install with nothing in .env yet.
FALLBACK_TAG="2026-07"

MAIL_DOMAIN=""
MAILCOW_TAG=""
NONINTERACTIVE=0

while [ $# -gt 0 ]; do
  case "$1" in
    --domain) MAIL_DOMAIN="$2"; shift 2 ;;
    --tag) MAILCOW_TAG="$2"; shift 2 ;;
    --yes|--non-interactive) NONINTERACTIVE=1; export AIW_NO_TUI=1; shift ;;
    -h|--help) grep -E '^#( |$)' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "Неизвестный флаг: $1" ;;
  esac
done

[ -f "$ENV_FILE" ] || die "$ENV_FILE не найден. Сначала выполните основной install.sh."

OS="$(detect_os)"
check_dependencies "$OS" || die "Установите недостающие зависимости и повторите."

TRAEFIK_DOMAIN="$(get_env_var "$ENV_FILE" TRAEFIK_DOMAIN)"; TRAEFIK_DOMAIN="${TRAEFIK_DOMAIN:-localhost}"
[ -z "$MAIL_DOMAIN" ] && MAIL_DOMAIN="$(get_env_var "$ENV_FILE" MAIL_DOMAIN)"
[ -z "$MAIL_DOMAIN" ] && MAIL_DOMAIN="$(ask_input "Хост почтового сервера (webmail/admin/autodiscover)" "mail.$TRAEFIK_DOMAIN" "Mailcow")"

# Tag/ports: CLI flag > infra/.env > fallback. Whatever we end up using is
# written back to infra/.env so update-mailcow.sh and render_traefik_routes read
# exactly the same values.
[ -z "$MAILCOW_TAG" ] && MAILCOW_TAG="$(get_env_var "$ENV_FILE" MAILCOW_TAG)"
MAILCOW_TAG="${MAILCOW_TAG:-$FALLBACK_TAG}"
MC_HTTP_PORT="$(get_env_var "$ENV_FILE" MAILCOW_HTTP_PORT)";  MC_HTTP_PORT="${MC_HTTP_PORT:-8080}"
MC_HTTPS_PORT="$(get_env_var "$ENV_FILE" MAILCOW_HTTPS_PORT)"; MC_HTTPS_PORT="${MC_HTTPS_PORT:-8443}"
set_env_var "$ENV_FILE" MAIL_DOMAIN "$MAIL_DOMAIN"
set_env_var "$ENV_FILE" MAILCOW_TAG "$MAILCOW_TAG"
set_env_var "$ENV_FILE" MAILCOW_HTTP_PORT "$MC_HTTP_PORT"
set_env_var "$ENV_FILE" MAILCOW_HTTPS_PORT "$MC_HTTPS_PORT"

PROJECT="$(get_env_var "$ENV_FILE" COMPOSE_PROJECT_NAME)"; PROJECT="${PROJECT:-infra}"
TRAEFIK_NET="${PROJECT}_app"

step "Mailcow — установка почтового сервера для $MAIL_DOMAIN"
info "Traefik-сеть для подключения: $TRAEFIK_NET"
info "Пинованный релиз: $MAILCOW_TAG"

# ── 1. Клонирование / обновление до пинованного тега ───────────────────────
if [ -d "$MAILCOW_DIR/.git" ]; then
  info "infra/mailcow уже существует — обновляю метаданные тегов…"
  git -C "$MAILCOW_DIR" fetch --tags --quiet
else
  info "Клонирую mailcow-dockerized…"
  git clone --quiet https://github.com/mailcow/mailcow-dockerized.git "$MAILCOW_DIR"
fi
git -C "$MAILCOW_DIR" checkout --quiet "$MAILCOW_TAG" \
  || die "Тег $MAILCOW_TAG не найден в mailcow-dockerized. Проверьте https://github.com/mailcow/mailcow-dockerized/releases"
ok "mailcow-dockerized на $MAILCOW_TAG"

# ── 2. mailcow.conf ──────────────────────────────────────────────────────────
if [ -f "$MAILCOW_DIR/mailcow.conf" ]; then
  ok "mailcow.conf уже существует — не перезаписываю (отредактируйте вручную при необходимости)."
else
  info "Генерирую mailcow.conf (MAILCOW_HOSTNAME=$MAIL_DOMAIN)…"
  # MAILCOW_TZ может прийти из окружения (GUI-развёртывание через update-agent) —
  # тогда ничего не спрашиваем.
  MAILCOW_TZ="${MAILCOW_TZ:-$(ask_input "Часовой пояс контейнеров" "Europe/Moscow" "Mailcow")}"
  # generate_config.sh пропускает интерактивные вопросы, если переменные уже
  # экспортированы в окружении (актуально для пинованного тега выше; если
  # апстрим это поведение уберёт — впишите mailcow.conf вручную один раз).
  ( cd "$MAILCOW_DIR" && MAILCOW_HOSTNAME="$MAIL_DOMAIN" MAILCOW_TZ="$MAILCOW_TZ" ./generate_config.sh ) \
    || die "generate_config.sh не завершился успешно — запустите вручную: (cd $MAILCOW_DIR && ./generate_config.sh)"

  # TLS терминирует наш Traefik — отключаем встроенный ACME-клиент Mailcow и
  # разводим порты, чтобы не конфликтовать с Traefik на 80/443 хоста.
  set_env_var "$MAILCOW_DIR/mailcow.conf" SKIP_LETS_ENCRYPT y
  set_env_var "$MAILCOW_DIR/mailcow.conf" SKIP_HTTP_VERIFICATION y
  set_env_var "$MAILCOW_DIR/mailcow.conf" HTTP_PORT "$MC_HTTP_PORT"
  set_env_var "$MAILCOW_DIR/mailcow.conf" HTTPS_PORT "$MC_HTTPS_PORT"
  set_env_var "$MAILCOW_DIR/mailcow.conf" HTTP_BIND 127.0.0.1
  set_env_var "$MAILCOW_DIR/mailcow.conf" HTTPS_BIND 127.0.0.1
  # ClamAV + Solr вместе едят ~4-6 ГБ RAM. На этом хосте рядом живут Ollama/vLLM/
  # Qdrant, поэтому по умолчанию выключены; Rspamd (антиспам/DKIM) остаётся.
  # Включить обратно: SKIP_CLAMD=n / SKIP_SOLR=n в infra/mailcow/mailcow.conf.
  set_env_var "$MAILCOW_DIR/mailcow.conf" SKIP_CLAMD y
  set_env_var "$MAILCOW_DIR/mailcow.conf" SKIP_SOLR y
  ok "mailcow.conf создан и настроен под внешний Traefik (ClamAV/Solr выключены)."
fi

# ── 3. docker-compose.override.yml: подключение к сети Traefik ─────────────
OVERRIDE="$MAILCOW_DIR/docker-compose.override.yml"
if [ -f "$OVERRIDE" ] && grep -q "aiw-managed" "$OVERRIDE" 2>/dev/null; then
  ok "docker-compose.override.yml уже настроен."
else
  cat > "$OVERRIDE" <<YAML
# aiw-managed: сгенерировано infra/installer/install-mailcow.sh — не редактировать
# вручную, при повторном запуске файл перезаписывается.
#
# Подключает вебмейл/admin/API Mailcow (nginx-mailcow) к сети основного
# Traefik, чтобы routes.yml мог проксировать https://${MAIL_DOMAIN} без
# публикации портов Mailcow наружу напрямую (см. HTTP_BIND/HTTPS_BIND=127.0.0.1
# в mailcow.conf).
services:
  nginx-mailcow:
    networks:
      - default
      - traefik_net

networks:
  traefik_net:
    external: true
    name: ${TRAEFIK_NET}
YAML
  ok "docker-compose.override.yml записан (сеть: $TRAEFIK_NET)."
fi

# ── 4. Поднять стек ──────────────────────────────────────────────────────────
COMPOSE="$(compose_cmd)"
if [ "$NONINTERACTIVE" = 1 ] || ask_yesno "Поднять Mailcow сейчас ($COMPOSE up -d в $MAILCOW_DIR)?" yes; then
  ( cd "$MAILCOW_DIR" && $COMPOSE up -d )
  ok "Mailcow запущен."
else
  log "Пропущено. Запустите позже: (cd $MAILCOW_DIR && $COMPOSE up -d)"
fi

# ── 5. Traefik-роут + сертификат для почтовых портов ────────────────────────
# Роут рендерится только когда infra/mailcow существует (см. render_traefik_routes),
# поэтому его нужно перегенерировать именно сейчас, после клонирования.
render_traefik_routes "$ENV_FILE" "$ROOT_DIR"
COMPOSE_MAIN="$(compose_cmd)"
info "Перечитываю конфиг Traefik…"
( $COMPOSE_MAIN -f infra/docker-compose.yml -f infra/docker-compose.prod.yml --env-file "$ENV_FILE"     restart traefik >/dev/null 2>&1 ) || warn "Не удалось перезапустить Traefik — сделайте вручную."

# Почтовые демоны (Postfix/Dovecot) не участвуют в TLS-терминации Traefik и без
# этого шага отдают самоподписанный сертификат — клиенты не подключатся.
info "Синхронизирую сертификат Let's Encrypt в Mailcow…"
if bash "$SELF_DIR/mailcow-certdump.sh"; then
  ok "Сертификат для SMTP/IMAP на месте."
else
  warn "Сертификат пока не выпущен (нужна DNS A-запись для $MAIL_DOMAIN)."
  warn "После появления DNS выполните: infra/installer/mailcow-certdump.sh"
fi

step "Готово"
ok "Mailcow развёрнут для $MAIL_DOMAIN."
warn "Не забудьте:"
log "  1. Включить ежедневную синхронизацию сертификата (mailcow-certdump.timer) — см. mailcow.README."
log "  2. Прописать DNS/SPF/DKIM/DMARC/PTR/autoconfig — см. infra/installer/mailcow.README."
log "  3. Открыть на хосте порты 25/465/587/143/993/110/995 в фаерволе (публикуются Mailcow напрямую, не через Traefik)."
log "  4. Создать домен и первый ящик в Mailcow admin UI (https://$MAIL_DOMAIN)."
