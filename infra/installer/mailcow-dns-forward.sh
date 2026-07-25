#!/usr/bin/env bash
#
# Обход перехвата DNS провайдером для Mailcow.
#
# Mailcow поднимает собственный рекурсивный резолвер (unbound): он сам ходит от
# корневых серверов к авторитетным. Это нужно не для красоты — на нём держатся
# проверки DNSBL, DANE/DNSSEC и резолв MX контрагентов.
#
# Часть провайдеров (особенно на PPPoE/бытовых каналах) перехватывает весь
# исходящий трафик на порт 53 и отвечает своим резолвером. Такой резолвер
# отвечает только на рекурсивные запросы, а итеративные (как раз те, что шлёт
# unbound) отбивает REFUSED. Симптом: unbound-mailcow вечно unhealthy, и вся
# установка встаёт с «dependency failed to start: unbound-mailcow is unhealthy».
#
# Скрипт определяет это и, если перехват есть, переводит unbound из рекурсивного
# режима в режим форвардинга по зашифрованному каналу:
#
#   unbound → (внутренняя docker-сеть) → dnscrypt-proxy → DoH/DoT наружу
#
# Порядок предпочтения апстрима: DoT (853) напрямую из unbound — самый простой
# вариант; если 853 тоже закрыт, поднимается sidecar dnscrypt-proxy с DoH (443),
# который закрыть уже нельзя, не сломав HTTPS.
#
# Использование:
#   infra/installer/mailcow-dns-forward.sh --check   # только диагностика
#   infra/installer/mailcow-dns-forward.sh           # диагностика + настройка
#   infra/installer/mailcow-dns-forward.sh --revert  # вернуть штатную рекурсию
#
set -euo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
ROOT_DIR="$(cd "$SELF_DIR/../.." && pwd)"
. "$SELF_DIR/lib.sh"
cd "$ROOT_DIR"

ENV_FILE="infra/.env"
MAILCOW_DIR="infra/mailcow"
AIW_DIR="$MAILCOW_DIR/aiw-dns"          # наши файлы, вне git-дерева mailcow
UNBOUND_CONF="$AIW_DIR/unbound.conf"
DNSCRYPT_CONF="$AIW_DIR/dnscrypt-proxy.toml"
OVERRIDE="$MAILCOW_DIR/docker-compose.override.yml"

CHECK_ONLY=0
REVERT=0
while [ $# -gt 0 ]; do
  case "$1" in
    --check) CHECK_ONLY=1; shift ;;
    --revert) REVERT=1; shift ;;
    -h|--help) grep -E '^#( |$)' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "Неизвестный флаг: $1" ;;
  esac
done

[ -d "$MAILCOW_DIR/.git" ] || { log "Mailcow не установлен — нечего настраивать."; exit 0; }

IPV4_NETWORK="$(get_env_var "$MAILCOW_DIR/mailcow.conf" IPV4_NETWORK)"
IPV4_NETWORK="${IPV4_NETWORK:-172.22.1}"
# Статические адреса mailcow в этой сети: .248, .249, .250, .253 (postfix),
# .254 (unbound). Берём .247 — ниже занятого диапазона, чтобы не столкнуться
# с сервисом апстрима (симптом столкновения: postfix не стартует с
# «failed to set up container networking: Address already in use»).
FORWARDER_IP="${IPV4_NETWORK}.247"

# ── Диагностика ─────────────────────────────────────────────────────────────
# Признак перехвата: корневой сервер обязан отвечать авторитетно (флаг aa) и БЕЗ
# «recursion available». Если на итеративный запрос приходит REFUSED или ответ с
# ra — значит отвечает не корень, а чей-то резолвер посередине.
dns_hijacked() {
  local out
  out="$(dig +norecurse +time=4 +tries=1 @198.41.0.4 ru. NS 2>/dev/null || true)"
  [ -n "$out" ] || return 0                       # нет ответа вовсе — тоже не рекурсируем
  printf '%s' "$out" | grep -q "status: REFUSED" && return 0
  printf '%s' "$out" | grep -qE "^;; flags:.* aa" || return 0
  return 1
}

port_open() {
  timeout 6 nc -z "$1" "$2" >/dev/null 2>&1
}

step "Mailcow — проверка исходящего DNS"

if [ "$REVERT" = 1 ]; then
  info "Возвращаю штатный рекурсивный режим unbound…"
  rm -f "$UNBOUND_CONF" "$DNSCRYPT_CONF" 2>/dev/null || true
  python3 - "$OVERRIDE" <<'PY'
import pathlib, sys, yaml

p = pathlib.Path(sys.argv[1])
if p.exists():
    doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    services = doc.get("services", {})
    services.pop("aiw-dns-forwarder", None)
    unbound = services.get("unbound-mailcow", {})
    if isinstance(unbound, dict):
        keep = [v for v in unbound.get("volumes", []) if "aiw-dns" not in str(v)]
        if keep:
            unbound["volumes"] = keep
        else:
            unbound.pop("volumes", None)
        if not unbound:
            services.pop("unbound-mailcow", None)
    if isinstance(doc.get("volumes"), dict):
        doc["volumes"].pop("aiw-dns-cache", None)
        if not doc["volumes"]:
            doc.pop("volumes")
    p.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8")
PY
  ok "Настройки форвардинга удалены. Перезапустите Mailcow: (cd $MAILCOW_DIR && docker compose up -d)"
  exit 0
fi

if ! dns_hijacked; then
  ok "Исходящий DNS не перехватывается — unbound может рекурсировать сам, ничего менять не нужно."
  exit 0
fi

warn "Провайдер перехватывает исходящий DNS (порт 53): итеративные запросы отбиваются."
warn "Собственная рекурсия unbound в такой сети работать не будет."

MODE=""
if port_open 1.1.1.1 853 || port_open 9.9.9.9 853; then
  MODE="dot"
  info "Доступен DNS-over-TLS (853) — unbound будет форвардить напрямую по TLS."
elif curl -fsS --max-time 8 -H 'accept: application/dns-json' \
      'https://1.1.1.1/dns-query?name=example.com&type=A' >/dev/null 2>&1; then
  MODE="doh"
  info "DoT закрыт, но DoH (443) работает — поднимаю локальный DoH-прокси."
else
  err "Ни 53 (рекурсия), ни 853 (DoT), ни 443 (DoH) не дают рабочего DNS."
  die "Почтовый сервер в такой сети работать не сможет — нужен провайдер без перехвата DNS либо VPN/туннель."
fi

if [ "$CHECK_ONLY" = 1 ]; then
  log "Режим --check: изменения не вносились. Выбранный способ: $MODE."
  exit 0
fi

mkdir -p "$AIW_DIR"

# ── unbound.conf: форвардинг вместо рекурсии ────────────────────────────────
# Копируем штатный конфиг mailcow и дописываем forward-zone, чтобы не потерять
# его настройки (access-control, размеры буферов и т.д.) и не трогать файл под
# контролем git — иначе следующий `git checkout` при обновлении встанет колом.
cp "$MAILCOW_DIR/data/conf/unbound/unbound.conf" "$UNBOUND_CONF"

if [ "$MODE" = "dot" ]; then
  cat >> "$UNBOUND_CONF" <<'CFG'

# ── aiw: форвардинг по DNS-over-TLS ────────────────────────────────────────
# Провайдер перехватывает порт 53, поэтому собственная рекурсия невозможна.
# DNSSEC-валидация сохраняется: апстримы отдают подписи, unbound их проверяет.
  tls-cert-bundle: "/etc/ssl/certs/ca-certificates.crt"

forward-zone:
  name: "."
  forward-tls-upstream: yes
  forward-addr: 1.1.1.1@853#cloudflare-dns.com
  forward-addr: 9.9.9.9@853#dns.quad9.net
CFG
else
  cat >> "$UNBOUND_CONF" <<CFG

# ── aiw: форвардинг на локальный DoH-прокси ────────────────────────────────
# Провайдер перехватывает 53 и закрывает 853, поэтому наружу ходим по HTTPS
# через dnscrypt-proxy в этой же docker-сети (перехват её не касается).
forward-zone:
  name: "."
  forward-addr: ${FORWARDER_IP}@5300
CFG

  cat > "$DNSCRYPT_CONF" <<'TOML'
# aiw-managed: DoH-прокси для unbound (см. infra/installer/mailcow-dns-forward.sh)
listen_addresses = ['0.0.0.0:5300']
server_names = ['cloudflare', 'google', 'quad9-doh-ip4-port443-nofilter-pri']
doh_servers = true
dnscrypt_servers = false
require_dnssec = false
ipv6_servers = false
timeout = 5000
keepalive = 30
bootstrap_resolvers = ['1.1.1.1:53', '8.8.8.8:53']
ignore_system_dns = true
# Сеть уже проверена скриптом; собственная проба dnscrypt-proxy при перехвате
# DNS может ложно не пройти и задержать старт.
netprobe_timeout = 0
cache = true
cache_size = 4096

[sources.public-resolvers]
urls = ['https://raw.githubusercontent.com/DNSCrypt/dnscrypt-resolvers/master/v3/public-resolvers.md']
# Кэш списка лежит в томе: после первой загрузки рестарты не зависят от сети.
cache_file = '/cache/public-resolvers.md'
minisign_key = 'RWQf6LRCGA9i53mlYecO4IzT51TGPpvWucNSCh1CBM0QTaLn73Y7GFO3'
refresh_delay = 72
TOML
fi

# ── docker-compose.override.yml ─────────────────────────────────────────────
# Мержим по YAML, а не дописыванием текста: в override уже лежит подключение
# nginx-mailcow к сети Traefik, и наивная дозапись в конец файла кладёт наши
# сервисы внутрь секции networks:.
python3 - "$OVERRIDE" "$MODE" "$FORWARDER_IP" <<'PY'
import pathlib, sys, yaml

path, mode, forwarder_ip = sys.argv[1], sys.argv[2], sys.argv[3]
p = pathlib.Path(path)
doc = {}
if p.exists():
    doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}

services = doc.setdefault("services", {})
unbound = services.setdefault("unbound-mailcow", {})
volumes = [v for v in unbound.get("volumes", []) if "unbound.conf" not in str(v)]
volumes.append("./aiw-dns/unbound.conf:/etc/unbound/unbound.conf:ro,Z")
unbound["volumes"] = volumes

if mode == "doh":
    services["aiw-dns-forwarder"] = {
        "image": "klutchell/dnscrypt-proxy:latest",
        "restart": "always",
        "volumes": [
            "./aiw-dns/dnscrypt-proxy.toml:/config/dnscrypt-proxy.toml:ro,Z",
            "aiw-dns-cache:/cache",
        ],
        "networks": {"mailcow-network": {"ipv4_address": forwarder_ip}},
    }
    doc.setdefault("volumes", {})["aiw-dns-cache"] = None
else:
    services.pop("aiw-dns-forwarder", None)
    if isinstance(doc.get("volumes"), dict):
        doc["volumes"].pop("aiw-dns-cache", None)
        if not doc["volumes"]:
            doc.pop("volumes")

header = (
    "# aiw-managed: сгенерировано infra/installer/install-mailcow.sh и\n"
    "# infra/installer/mailcow-dns-forward.sh — не редактировать вручную.\n"
)
p.write_text(header + yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8")
PY

ok "Конфигурация записана (режим: $MODE)."

COMPOSE="$(compose_cmd)"
info "Перезапускаю DNS-компоненты Mailcow…"
if [ "$MODE" = "doh" ]; then
  ( cd "$MAILCOW_DIR" && $COMPOSE up -d aiw-dns-forwarder ) || die "Не удалось поднять DoH-прокси."
  sleep 8
fi
( cd "$MAILCOW_DIR" && $COMPOSE up -d --force-recreate unbound-mailcow ) || die "Не удалось перезапустить unbound."

info "Жду, пока unbound станет healthy…"
tries=0
while [ $tries -lt 30 ]; do
  state="$(docker inspect -f '{{.State.Health.Status}}' "$(docker ps -aqf name=unbound-mailcow | head -1)" 2>/dev/null || echo unknown)"
  [ "$state" = "healthy" ] && { ok "unbound healthy — DNS работает."; break; }
  tries=$((tries+1)); sleep 5
done
[ "${state:-}" = "healthy" ] || warn "unbound всё ещё не healthy — смотрите: docker logs \$(docker ps -qf name=unbound-mailcow)"

step "Готово"
log "Поднять оставшиеся сервисы: (cd $MAILCOW_DIR && $COMPOSE up -d)"
warn "Учтите: DNSBL-проверки (Spamhaus и т.п.) с публичных резолверов не работают —"
warn "антиспам потеряет часть сигналов. Это цена работы за перехватывающим провайдером."
