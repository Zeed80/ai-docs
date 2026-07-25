#!/usr/bin/env bash
#
# AI Workspace — backup. Produces a single timestamped archive containing:
#   - PostgreSQL dump (app DB + Authentik DB)   via pg_dump (online, consistent)
#   - MinIO object store                          (volume tar)
#   - Qdrant vector store                         (volume tar)
#   - Redis (agent config, caches)               (volume tar)
#   - infra/.env                                  (secrets/config)
#
#   - Mailcow (почта), если установлен            (helper-scripts/backup_and_restore.sh)
#
#   bash infra/installer/backup.sh [--label <name>] [--out <dir>] [--skip-mailcow]
#
# ВНИМАНИЕ про размер: mailcow-часть содержит vmail — почтовые ящики ВСЕХ
# сотрудников целиком, и это полный (не инкрементальный) снимок в каждом архиве.
# На ежедневном расписании держите отдельную ротацию или гоняйте общий бэкап с
# --skip-mailcow, а почту бэкапьте реже отдельным запуском.
#
# Default output: backups/aiw-backup-<UTC-timestamp>[-label].tar.gz
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
. "$SCRIPT_DIR/lib.sh"
cd "$REPO_DIR"

ENV_FILE="infra/.env"
PROJECT="${AIW_PROJECT:-infra}"
OUT_DIR="backups"
LABEL=""
SKIP_MAILCOW="${AIW_SKIP_MAILCOW:-0}"

while [ $# -gt 0 ]; do
  case "$1" in
    --label) LABEL="$2"; shift 2 ;;
    --out) OUT_DIR="$2"; shift 2 ;;
    --skip-mailcow) SKIP_MAILCOW=1; shift ;;
    -h|--help) grep -E '^#( |$)' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Неизвестный флаг: $1" >&2; exit 1 ;;
  esac
done

[ -f "$ENV_FILE" ] || die "$ENV_FILE не найден."
COMPOSE="${AIW_COMPOSE:-$(compose_cmd)}"
COMPOSE_ARGS="${AIW_COMPOSE_ARGS:--f infra/docker-compose.yml --env-file $ENV_FILE}"
# shellcheck disable=SC2086
run_compose() { $COMPOSE $COMPOSE_ARGS "$@"; }

PG_USER="$(get_env_var "$ENV_FILE" POSTGRES_USER)"; PG_USER="${PG_USER:-aiworkspace}"
PG_DB="$(get_env_var "$ENV_FILE" POSTGRES_DB)"; PG_DB="${PG_DB:-aiworkspace}"
AK_USER="$(get_env_var "$ENV_FILE" AUTHENTIK_DB_USER)"; AK_USER="${AK_USER:-authentik}"
AK_DB="$(get_env_var "$ENV_FILE" AUTHENTIK_DB_NAME)"; AK_DB="${AK_DB:-authentik}"

TS="$(date -u +%Y%m%dT%H%M%SZ)"
NAME="aiw-backup-$TS${LABEL:+-$LABEL}"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$OUT_DIR"

printf '\n%s\n' "${C_BOLD}${C_BLUE}AI Workspace — backup → $NAME.tar.gz${C_RESET}"

# ── PostgreSQL (app) ─────────────────────────────────────────────────────────
step "PostgreSQL (приложение)"
if run_compose exec -T postgres pg_dump -U "$PG_USER" -d "$PG_DB" --clean --if-exists > "$STAGE/postgres_app.sql" 2>/dev/null; then
  ok "Дамп $PG_DB: $(wc -c < "$STAGE/postgres_app.sql") байт"
else
  die "pg_dump приложения не удался."
fi

# ── PostgreSQL (Authentik) — separate container, optional ───────────────────
step "PostgreSQL (Authentik)"
if docker exec "${PROJECT}-authentik-db-1" pg_dump -U "$AK_USER" -d "$AK_DB" --clean --if-exists > "$STAGE/postgres_authentik.sql" 2>/dev/null; then
  ok "Дамп $AK_DB: $(wc -c < "$STAGE/postgres_authentik.sql") байт"
else
  warn "Authentik DB пропущена (контейнер не запущен / SSO выключен)."
  rm -f "$STAGE/postgres_authentik.sql"
fi

# ── Named volumes (MinIO / Qdrant / Redis) ──────────────────────────────────
tar_volume() {
  local vol="$1" out="$2"
  if ! docker volume inspect "$vol" >/dev/null 2>&1; then
    warn "Volume $vol не найден — пропускаю."; return 0
  fi
  docker run --rm -v "$vol":/src:ro -v "$STAGE":/dst alpine \
    tar czf "/dst/$out" -C /src . 2>/dev/null \
    && ok "$vol → $out ($(wc -c < "$STAGE/$out") байт)" \
    || warn "Не удалось заархивировать $vol."
}
step "MinIO / Qdrant / Redis"
tar_volume "${PROJECT}_minio_data"  minio_data.tar.gz
tar_volume "${PROJECT}_qdrant_data" qdrant_data.tar.gz
tar_volume "${PROJECT}_redis_data"  redis_data.tar.gz

# ── Mailcow (self-hosted mail server) — optional, separate compose project ──
MAILCOW_DIR="infra/mailcow"
MAILCOW_COMPONENT=""
if [ "$SKIP_MAILCOW" = 1 ]; then
  log "Mailcow пропущен (--skip-mailcow) — почта в этот архив не попадёт."
elif [ -d "$MAILCOW_DIR/.git" ] && [ -f "$MAILCOW_DIR/mailcow.conf" ]; then
  step "Mailcow (почтовый сервер)"
  MAILCOW_STAGE="$STAGE/mailcow"
  mkdir -p "$MAILCOW_STAGE"
  # vmail целиком — обычно самая большая часть архива. Показываем размер заранее,
  # чтобы «бэкап неожиданно на 200 ГБ» не стал открытием после запуска.
  MAILCOW_SIZE="$(docker run --rm -v "mailcowdockerized_vmail-vol-1:/vmail:ro" alpine:3.20 \
      du -sh /vmail 2>/dev/null | awk '{print $1}')"
  [ -n "${MAILCOW_SIZE:-}" ] && info "Объём почтовых ящиков (vmail): ${MAILCOW_SIZE} — попадёт в архив целиком."
  # Uses Mailcow's own helper script — matches its supported backup format so
  # infra/installer/update-mailcow.sh / a stock `restore.sh` from mailcow docs
  # can consume it directly. `yes ""` answers the interactive "keep how many
  # days" prompt with the default (keep everything in this one-shot dir).
  if ( cd "$MAILCOW_DIR" && MAILCOW_BACKUP_LOCATION="$MAILCOW_STAGE" \
        yes "" | ./helper-scripts/backup_and_restore.sh backup all >/dev/null 2>&1 ); then
    ok "Mailcow (mysql/vmail/crypt/redis/rspamd/postfix) заархивирован."
    MAILCOW_COMPONENT="mailcow"
  else
    warn "Бэкап Mailcow не удался — пропускаю (проверьте infra/mailcow вручную)."
    rm -rf "$MAILCOW_STAGE"
  fi
else
  log "Mailcow не установлен (infra/mailcow отсутствует) — пропускаю."
fi

# ── Config ───────────────────────────────────────────────────────────────────
cp "$ENV_FILE" "$STAGE/env"
cat > "$STAGE/manifest.json" <<JSON
{
  "name": "$NAME",
  "created_utc": "$TS",
  "label": "${LABEL:-}",
  "project": "$PROJECT",
  "git_commit": "$(git rev-parse HEAD 2>/dev/null || echo unknown)",
  "components": ["postgres_app", "postgres_authentik", "minio", "qdrant", "redis", "env"${MAILCOW_COMPONENT:+, \"$MAILCOW_COMPONENT\"}]
}
JSON

# ── Pack ──────────────────────────────────────────────────────────────────────
step "Упаковка"
tar czf "$OUT_DIR/$NAME.tar.gz" -C "$STAGE" .
ok "Готово: $OUT_DIR/$NAME.tar.gz ($(du -h "$OUT_DIR/$NAME.tar.gz" | cut -f1))"
log "  Восстановление: bash infra/installer/restore.sh $OUT_DIR/$NAME.tar.gz"
