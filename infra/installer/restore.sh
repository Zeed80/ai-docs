#!/usr/bin/env bash
#
# AI Workspace — restore from a backup archive made by backup.sh.
#
#   bash infra/installer/restore.sh backups/aiw-backup-<ts>.tar.gz [--yes] [--with-env]
#
# DESTRUCTIVE: overwrites the current database and volumes. Stops dependent
# containers during volume restore, then brings the stack back up.
#
#   --with-env   also restore infra/.env from the archive (secrets!)
#   --yes        skip the confirmation prompt
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
. "$SCRIPT_DIR/lib.sh"
cd "$REPO_DIR"

ENV_FILE="infra/.env"
PROJECT="${AIW_PROJECT:-infra}"
ARCHIVE=""
WITH_ENV=0
NONINTERACTIVE=0

while [ $# -gt 0 ]; do
  case "$1" in
    --with-env) WITH_ENV=1; shift ;;
    --yes|--non-interactive) NONINTERACTIVE=1; export AIW_NO_TUI=1; shift ;;
    -h|--help) grep -E '^#( |$)' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    -*) echo "Неизвестный флаг: $1" >&2; exit 1 ;;
    *) ARCHIVE="$1"; shift ;;
  esac
done

[ -n "$ARCHIVE" ] || die "Укажите архив: restore.sh <backup.tar.gz>"
[ -f "$ARCHIVE" ] || die "Архив не найден: $ARCHIVE"
COMPOSE="$(compose_cmd)"; [ -z "$COMPOSE" ] && die "Docker Compose не найден."
APP_ENV="$(get_env_var "$ENV_FILE" APP_ENV 2>/dev/null || echo development)"
if [ "$APP_ENV" = "production" ]; then
  COMPOSE_ARGS="-f infra/docker-compose.yml -f infra/docker-compose.prod.yml --env-file $ENV_FILE"
else
  COMPOSE_ARGS="-f infra/docker-compose.yml -f infra/docker-compose.dev.yml"
fi
# shellcheck disable=SC2086
run_compose() { $COMPOSE $COMPOSE_ARGS "$@"; }

STAGE="$(mktemp -d)"; trap 'rm -rf "$STAGE"' EXIT
tar xzf "$ARCHIVE" -C "$STAGE"
[ -f "$STAGE/manifest.json" ] || warn "Нет manifest.json — возможно, несовместимый архив."

printf '\n%s\n' "${C_BOLD}${C_YELLOW}AI Workspace — ВОССТАНОВЛЕНИЕ из $ARCHIVE${C_RESET}"
[ -f "$STAGE/manifest.json" ] && sed 's/^/  /' "$STAGE/manifest.json"
warn "Это ПЕРЕЗАПИШЕТ текущую БД и данные (MinIO/Qdrant/Redis)."
if [ "$NONINTERACTIVE" != 1 ]; then
  ask_yesno "Продолжить восстановление?" no || die "Отменено."
fi

PG_USER="$(get_env_var "$ENV_FILE" POSTGRES_USER)"; PG_USER="${PG_USER:-aiworkspace}"
PG_DB="$(get_env_var "$ENV_FILE" POSTGRES_DB)"; PG_DB="${PG_DB:-aiworkspace}"
AK_USER="$(get_env_var "$ENV_FILE" AUTHENTIK_DB_USER)"; AK_USER="${AK_USER:-authentik}"
AK_DB="$(get_env_var "$ENV_FILE" AUTHENTIK_DB_NAME)"; AK_DB="${AK_DB:-authentik}"

# ── 1. .env (optional) ───────────────────────────────────────────────────────
if [ "$WITH_ENV" = 1 ] && [ -f "$STAGE/env" ]; then
  step "Восстановление infra/.env"
  cp "$ENV_FILE" "$ENV_FILE.bak.$(date +%s)" 2>/dev/null || true
  cp "$STAGE/env" "$ENV_FILE"
  ok ".env восстановлен (старый сохранён в .bak)."
fi

# ── 2. Volumes (stop dependents, extract, restart) ──────────────────────────
restore_volume() {
  local vol="$1" file="$2"
  [ -f "$STAGE/$file" ] || { warn "$file нет в архиве — пропускаю."; return 0; }
  docker volume inspect "$vol" >/dev/null 2>&1 || docker volume create "$vol" >/dev/null
  docker run --rm -v "$vol":/dst -v "$STAGE":/src alpine sh -c \
    "rm -rf /dst/* /dst/..?* 2>/dev/null; tar xzf /src/$file -C /dst" \
    && ok "$vol ← $file" || warn "Не удалось восстановить $vol."
}
step "Остановка стека для восстановления данных"
run_compose down
step "Восстановление volumes (MinIO / Qdrant / Redis)"
restore_volume "${PROJECT}_minio_data"  minio_data.tar.gz
restore_volume "${PROJECT}_qdrant_data" qdrant_data.tar.gz
restore_volume "${PROJECT}_redis_data"  redis_data.tar.gz

# ── 3. PostgreSQL (start only DB, load dumps) ───────────────────────────────
step "Восстановление PostgreSQL"
run_compose up -d postgres
sleep 5
# wait for postgres
tries=0; until run_compose exec -T postgres pg_isready -U "$PG_USER" >/dev/null 2>&1 || [ $tries -ge 30 ]; do
  tries=$((tries+1)); sleep 2
done
if [ -f "$STAGE/postgres_app.sql" ]; then
  run_compose exec -T postgres psql -U "$PG_USER" -d "$PG_DB" < "$STAGE/postgres_app.sql" >/dev/null 2>&1 \
    && ok "БД приложения восстановлена." || die "Не удалось восстановить БД приложения."
fi
if [ -f "$STAGE/postgres_authentik.sql" ]; then
  run_compose up -d authentik-db >/dev/null 2>&1 || true
  sleep 5
  docker exec -i "${PROJECT}-authentik-db-1" psql -U "$AK_USER" -d "$AK_DB" < "$STAGE/postgres_authentik.sql" >/dev/null 2>&1 \
    && ok "БД Authentik восстановлена." || warn "Authentik DB не восстановлена."
fi

# ── 4. Bring the stack back up ──────────────────────────────────────────────
step "Запуск стека"
run_compose up -d
if wait_for_backend run_compose; then
  ok "Восстановление завершено успешно."
else
  err "Backend не стал здоровым — проверьте логи."
  exit 1
fi
