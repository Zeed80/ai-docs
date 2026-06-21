#!/usr/bin/env bash
#
# AI Workspace — updater. Pulls latest code, backs up, rebuilds, migrates, restarts.
#
#   ./update.sh                  # pull current branch, auto-backup, rebuild, migrate
#   ./update.sh --no-backup      # skip the pre-update backup
#   ./update.sh --branch main    # switch/pull a specific branch
#   ./update.sh --yes            # unattended
#
# DB migrations run automatically inside the backend entrypoint (alembic upgrade
# heads) on container start. This script ensures images are rebuilt and the new
# backend comes up healthy, rolling guidance if it doesn't.
#
set -euo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
. "$SELF_DIR/infra/installer/lib.sh"
cd "$SELF_DIR"

ENV_FILE="infra/.env"
DO_BACKUP=1
BRANCH=""
NONINTERACTIVE=0

while [ $# -gt 0 ]; do
  case "$1" in
    --no-backup) DO_BACKUP=0; shift ;;
    --branch) BRANCH="$2"; shift 2 ;;
    --yes|--non-interactive) NONINTERACTIVE=1; export AIW_NO_TUI=1; shift ;;
    -h|--help) grep -E '^#( |$)' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Неизвестный флаг: $1" >&2; exit 1 ;;
  esac
done

[ -f "$ENV_FILE" ] || die "$ENV_FILE не найден — сначала выполните установку (./install.sh)."

COMPOSE="$(compose_cmd)"; [ -z "$COMPOSE" ] && die "Docker Compose не найден."
APP_ENV="$(get_env_var "$ENV_FILE" APP_ENV)"
if [ "$APP_ENV" = "production" ]; then
  COMPOSE_ARGS="-f infra/docker-compose.yml -f infra/docker-compose.prod.yml --env-file $ENV_FILE"
else
  COMPOSE_ARGS="-f infra/docker-compose.yml -f infra/docker-compose.dev.yml --env-file $ENV_FILE"
fi
# Keep the same local-AI engine profiles the stack was installed with.
COMPOSE_ARGS="$COMPOSE_ARGS$(profile_args "$ENV_FILE")"
# shellcheck disable=SC2086
run_compose() { $COMPOSE $COMPOSE_ARGS "$@"; }

printf '\n%s\n' "${C_BOLD}${C_BLUE}AI Workspace — обновление (${APP_ENV:-dev})${C_RESET}"

# ── 1. Pre-update backup ─────────────────────────────────────────────────────
if [ "$DO_BACKUP" = 1 ]; then
  step "1/5  Бэкап перед обновлением"
  if AIW_COMPOSE="$COMPOSE" AIW_COMPOSE_ARGS="$COMPOSE_ARGS" \
       bash "$SELF_DIR/infra/installer/backup.sh" --label pre-update; then
    ok "Бэкап создан."
  else
    warn "Бэкап не удался."
    [ "$NONINTERACTIVE" = 1 ] || ask_yesno "Продолжить обновление без бэкапа?" no || die "Отменено."
  fi
else
  warn "Бэкап пропущен (--no-backup)."
fi

# ── 2. Pull latest code ──────────────────────────────────────────────────────
step "2/5  Получение кода"
CURRENT_BRANCH="$(git rev-parse --abbrev-ref HEAD)"
TARGET_BRANCH="${BRANCH:-$CURRENT_BRANCH}"
if [ -n "$(git status --porcelain)" ]; then
  warn "Есть незакоммиченные изменения в рабочем дереве:"
  git status --short | sed 's/^/    /'
  [ "$NONINTERACTIVE" = 1 ] || ask_yesno "Продолжить (изменения сохранятся, git pull --ff-only)?" yes || die "Отменено."
fi
BEFORE="$(git rev-parse HEAD)"
git fetch origin "$TARGET_BRANCH"
[ "$TARGET_BRANCH" != "$CURRENT_BRANCH" ] && git checkout "$TARGET_BRANCH"
git pull --ff-only origin "$TARGET_BRANCH"
AFTER="$(git rev-parse HEAD)"
if [ "$BEFORE" = "$AFTER" ] && [ "$TARGET_BRANCH" = "$CURRENT_BRANCH" ]; then
  ok "Уже последняя версия ($AFTER). Пересоберу на всякий случай."
else
  ok "Обновлено: $BEFORE → $AFTER"
fi

# Render Traefik prod routes from the (domain-free) template before rebuild/up.
[ "$APP_ENV" = "production" ] && render_traefik_routes "$ENV_FILE" "$SELF_DIR"

# ── 3. Rebuild images ────────────────────────────────────────────────────────
step "3/5  Пересборка образов"
check_disk_space 20 || { [ "$NONINTERACTIVE" = 1 ] || ask_yesno "Мало места — продолжить?" no || die "Отменено."; }
# Capture DB revision before the new backend applies migrations on start.
REV_BEFORE="$(run_compose exec -T backend sh -c 'cd /app && alembic current 2>/dev/null' | awk 'NF{print $1; exit}')"
run_compose build
ok "Образы собраны."

# ── 4. Restart (migrations run in entrypoint: alembic upgrade heads) ─────────
step "4/5  Перезапуск (миграции применятся автоматически)"
run_compose up -d --remove-orphans
ok "Контейнеры обновлены."

# ── 5. Health check + migration/stack verification ──────────────────────────
step "5/5  Проверка здоровья, миграций и сервисов"
if wait_for_backend run_compose; then
  [ -n "${REV_BEFORE:-}" ] && log "  Ревизия БД до обновления: $REV_BEFORE"
  report_migrations run_compose || warn "БД не на последней ревизии — смотрите логи backend."
  verify_stack run_compose || warn "Часть сервисов не здорова: $COMPOSE $COMPOSE_ARGS ps"
  # Optional, best-effort: refresh the mobile APK served at /download from the
  # latest GitHub Release. Enable with MOBILE_RELEASE_FETCH=1; never fails update.
  if [ "${MOBILE_RELEASE_FETCH:-0}" = 1 ]; then
    AIW_COMPOSE="$COMPOSE" AIW_COMPOSE_ARGS="$COMPOSE_ARGS" \
      bash "$SELF_DIR/infra/installer/fetch-mobile-release.sh" || true
  fi
  ok "Обновление успешно."
else
  err "Backend не стал здоровым после обновления."
  warn "Логи:          $COMPOSE $COMPOSE_ARGS logs --tail=80 backend"
  warn "Откат кода:    git reset --hard $BEFORE && ./update.sh --no-backup"
  warn "Откат данных:  bash infra/installer/restore.sh <последний-бэкап>"
  exit 1
fi
