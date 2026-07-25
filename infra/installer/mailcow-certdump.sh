#!/usr/bin/env bash
#
# Отдать Mailcow сертификат, который выпустил наш Traefik.
#
# Зачем: install-mailcow.sh ставит SKIP_LETS_ENCRYPT=y, потому что TLS на 443
# терминирует Traefik. Но ACME при этом отключён ЦЕЛИКОМ, а Postfix/Dovecot/SOGo
# слушают почтовые порты сами (25/465/587/143/993/110/995) и без файлов в
# data/assets/ssl/ отдают самоподписанный snakeoil. Итог: почтовый клиент,
# который по autoconfig нашёл mail.<домен>:993, упирается в невалидный
# сертификат — «настройка по email+паролю» не работает.
#
# Что делает скрипт (по докам mailcow, post_installation/firststeps-ssl):
#   1. Достаёт cert+key для MAIL_DOMAIN из acme.json Traefik (том traefik_acme).
#   2. КОПИРУЕТ (не симлинк — mailcow это явно запрещает) в
#      infra/mailcow/data/assets/ssl/{cert.pem,key.pem}.
#   3. Если содержимое изменилось — перезапускает postfix/dovecot/nginx Mailcow,
#      чтобы демоны перечитали сертификат. Без изменений не трогает ничего
#      (скрипт рассчитан на ежедневный таймер).
#
# Использование:
#   infra/installer/mailcow-certdump.sh            # обычный прогон (тихий, если нечего менять)
#   infra/installer/mailcow-certdump.sh --force    # перезаписать и перезапустить принудительно
#   infra/installer/mailcow-certdump.sh --check    # только показать, что бы сделал
#
set -euo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
ROOT_DIR="$(cd "$SELF_DIR/../.." && pwd)"
. "$SELF_DIR/lib.sh"
cd "$ROOT_DIR"

ENV_FILE="infra/.env"
MAILCOW_DIR="infra/mailcow"
SSL_DIR="$MAILCOW_DIR/data/assets/ssl"

FORCE=0
CHECK_ONLY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --force) FORCE=1; shift ;;
    --check) CHECK_ONLY=1; shift ;;
    -h|--help) grep -E '^#( |$)' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "Неизвестный флаг: $1" ;;
  esac
done

[ -f "$ENV_FILE" ] || die "$ENV_FILE не найден."
[ -d "$MAILCOW_DIR/.git" ] || { log "Mailcow не установлен — нечего делать."; exit 0; }

TRAEFIK_DOMAIN="$(get_env_var "$ENV_FILE" TRAEFIK_DOMAIN)"; TRAEFIK_DOMAIN="${TRAEFIK_DOMAIN:-localhost}"
MAIL_DOMAIN="$(get_env_var "$ENV_FILE" MAIL_DOMAIN)"; MAIL_DOMAIN="${MAIL_DOMAIN:-mail.$TRAEFIK_DOMAIN}"
PROJECT="$(get_env_var "$ENV_FILE" COMPOSE_PROJECT_NAME)"; PROJECT="${PROJECT:-infra}"
ACME_VOLUME="${PROJECT}_traefik_acme"

command -v jq >/dev/null 2>&1 || die "Нужен jq (разбор acme.json). Установите: apt-get install -y jq"

step "Mailcow — сертификат из Traefik для $MAIL_DOMAIN"

# ── 1. Достаём cert/key из тома Traefik ─────────────────────────────────────
# acme.json лежит в docker-томе и принадлежит root'у, поэтому читаем его
# одноразовым alpine-контейнером, а не с хоста.
TMP_DIR="$(mktemp -d /tmp/mailcow-cert-XXXX)"
trap 'rm -rf "$TMP_DIR"' EXIT

read_acme() {
  docker run --rm -v "${ACME_VOLUME}:/acme:ro" alpine:3.20 cat /acme/acme.json 2>/dev/null
}

ACME_JSON="$(read_acme || true)"
[ -n "$ACME_JSON" ] || die "Не удалось прочитать acme.json из тома $ACME_VOLUME (Traefik запускался в prod-режиме?)."

# Формат acme.json: {"letsencrypt":{"Certificates":[{"domain":{"main":"..."},"certificate":"<b64>","key":"<b64>"}]}}
# Берём сертификат, у которого main == MAIL_DOMAIN либо MAIL_DOMAIN есть в sans.
CERT_B64="$(printf '%s' "$ACME_JSON" | jq -r --arg d "$MAIL_DOMAIN" '
  [.. | objects | select(has("certificate") and has("domain"))
      | select(.domain.main == $d or ((.domain.sans // []) | index($d)))][0].certificate // empty')"
KEY_B64="$(printf '%s' "$ACME_JSON" | jq -r --arg d "$MAIL_DOMAIN" '
  [.. | objects | select(has("certificate") and has("domain"))
      | select(.domain.main == $d or ((.domain.sans // []) | index($d)))][0].key // empty')"

if [ -z "$CERT_B64" ] || [ -z "$KEY_B64" ]; then
  die "В acme.json нет сертификата для $MAIL_DOMAIN.
  Проверьте: DNS A-запись существует, роут mailcow есть в infra/traefik/prod/routes.yml
  (перерендерить: render_traefik_routes), Traefik успел получить сертификат."
fi

printf '%s' "$CERT_B64" | base64 -d > "$TMP_DIR/cert.pem"
printf '%s' "$KEY_B64"  | base64 -d > "$TMP_DIR/key.pem"

openssl x509 -in "$TMP_DIR/cert.pem" -noout -subject -enddate >/dev/null 2>&1 \
  || die "Из acme.json извлёкся не сертификат — прерываю, ничего не меняю."
NOT_AFTER="$(openssl x509 -in "$TMP_DIR/cert.pem" -noout -enddate | cut -d= -f2)"
info "Сертификат из Traefik действителен до: $NOT_AFTER"

# ── 2. Сравниваем с тем, что уже лежит у Mailcow ────────────────────────────
mkdir -p "$SSL_DIR"
CHANGED=0
if [ ! -f "$SSL_DIR/cert.pem" ] || ! cmp -s "$TMP_DIR/cert.pem" "$SSL_DIR/cert.pem"; then CHANGED=1; fi
if [ ! -f "$SSL_DIR/key.pem" ]  || ! cmp -s "$TMP_DIR/key.pem"  "$SSL_DIR/key.pem";  then CHANGED=1; fi

if [ "$CHANGED" = 0 ] && [ "$FORCE" = 0 ]; then
  ok "Сертификат Mailcow уже актуален — ничего не меняю."
  exit 0
fi

if [ "$CHECK_ONLY" = 1 ]; then
  warn "Сертификат отличается — требуется обновление (запустите без --check)."
  exit 0
fi

# ── 3. Копируем + перезапускаем демонов ─────────────────────────────────────
info "Обновляю $SSL_DIR/{cert.pem,key.pem}…"
cp "$TMP_DIR/cert.pem" "$SSL_DIR/cert.pem"
cp "$TMP_DIR/key.pem"  "$SSL_DIR/key.pem"
chmod 600 "$SSL_DIR/key.pem"
chmod 644 "$SSL_DIR/cert.pem"
ok "Сертификат скопирован."

COMPOSE="$(compose_cmd)"
info "Перезапускаю postfix/dovecot/nginx Mailcow, чтобы подхватили сертификат…"
( cd "$MAILCOW_DIR" && $COMPOSE restart postfix-mailcow dovecot-mailcow nginx-mailcow ) \
  || warn "Не удалось перезапустить контейнеры Mailcow — сделайте вручную: (cd $MAILCOW_DIR && $COMPOSE restart postfix-mailcow dovecot-mailcow nginx-mailcow)"

step "Готово"
ok "Mailcow отдаёт сертификат Let's Encrypt на почтовых портах."
log "Проверка:"
log "  openssl s_client -starttls smtp -connect $MAIL_DOMAIN:587 -servername $MAIL_DOMAIN </dev/null 2>/dev/null | openssl x509 -noout -issuer -subject"
log "  openssl s_client -connect $MAIL_DOMAIN:993 -servername $MAIL_DOMAIN </dev/null 2>/dev/null | openssl x509 -noout -issuer -subject"
