#!/usr/bin/env bash
#
# Staged Authentik upgrader.
#
# Authentik does NOT support skipping major versions — its DB migrations must run
# once per major release in order (see docs.goauthentik.io/install-config/upgrade).
# This walks the ladder one major at a time, and for every hop:
#   1. takes a full backup (backup.sh — includes the Authentik DB) unless --no-backup
#   2. dumps the Authentik DB separately for a fast, targeted rollback
#   3. bumps AUTHENTIK_TAG in infra/.env, pulls, recreates authentik-server/worker
#   4. waits for the readiness probe (204 = migrations done, app ready)
#   5. on failure: restores the Authentik DB + reverts the tag, then stops
#
# Usage:
#   upgrade-authentik.sh --check              # show current / ladder / next hop, do nothing
#   upgrade-authentik.sh --next [--yes]       # advance exactly ONE major
#   upgrade-authentik.sh --to 2026.5 [--yes]  # advance up to and including this major
#   upgrade-authentik.sh [--yes]              # advance to the latest known major
#     --no-backup   skip the full backup.sh at each hop (the fast rollback dump is always taken)
#
set -euo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
ROOT_DIR="$(cd "$SELF_DIR/../.." && pwd)"
. "$SELF_DIR/lib.sh"
cd "$ROOT_DIR"

ENV_FILE="infra/.env"

# Ordered Authentik major ladder. Floating major.minor tags resolve to the latest
# patch (e.g. 2026.5 → 2026.5.6). Never skip; add new majors to the END as they ship.
LADDER="2025.2 2025.4 2025.6 2025.8 2025.10 2025.12 2026.2 2026.5"

DO_BACKUP=1
NONINTERACTIVE=0
MODE="latest"        # latest | next | to
TARGET=""
CHECK_ONLY=0

while [ $# -gt 0 ]; do
  case "$1" in
    --check) CHECK_ONLY=1; shift ;;
    --next) MODE="next"; shift ;;
    --to) MODE="to"; TARGET="$2"; shift 2 ;;
    --no-backup) DO_BACKUP=0; shift ;;
    --yes|--non-interactive) NONINTERACTIVE=1; export AIW_NO_TUI=1; shift ;;
    -h|--help) grep -E '^#( |$)' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "Неизвестный флаг: $1" ;;
  esac
done

[ -f "$ENV_FILE" ] || die "$ENV_FILE не найден."

COMPOSE="$(compose_cmd)"; [ -z "$COMPOSE" ] && die "Docker Compose не найден."
APP_ENV="$(get_env_var "$ENV_FILE" APP_ENV)"
if [ "$APP_ENV" = "production" ]; then
  COMPOSE_ARGS="-f infra/docker-compose.yml -f infra/docker-compose.prod.yml --env-file $ENV_FILE"
else
  COMPOSE_ARGS="-f infra/docker-compose.yml -f infra/docker-compose.dev.yml --env-file $ENV_FILE"
fi
COMPOSE_ARGS="$COMPOSE_ARGS$(profile_args "$ENV_FILE")"
# shellcheck disable=SC2086
run_compose() { $COMPOSE $COMPOSE_ARGS "$@"; }

PROJECT="$(get_env_var "$ENV_FILE" COMPOSE_PROJECT_NAME)"; PROJECT="${PROJECT:-infra}"
AK_DB_CONTAINER="${PROJECT}-authentik-db-1"
AK_USER="$(get_env_var "$ENV_FILE" AUTHENTIK_DB_USER)"; AK_USER="${AK_USER:-authentik}"
AK_DB="$(get_env_var "$ENV_FILE" AUTHENTIK_DB_NAME)"; AK_DB="${AK_DB:-authentik}"

# major.minor of a tag ("2025.2.4" → "2025.2")
minor_of() { printf '%s' "$1" | awk -F. '{print $1"."$2}'; }

# Everything in LADDER strictly after the current major.minor.
remaining_ladder() {
  local current="$1" seen=0 out="" m
  for m in $LADDER; do
    if [ "$seen" = 1 ]; then out="$out $m"; continue; fi
    [ "$m" = "$current" ] && seen=1
  done
  # current not on the ladder (older than the first rung) → whole ladder applies
  [ "$seen" = 0 ] && out="$LADDER"
  printf '%s' "$out"
}

CURRENT_TAG="$(get_env_var "$ENV_FILE" AUTHENTIK_TAG)"; CURRENT_TAG="${CURRENT_TAG:-2024.12.3}"
CURRENT_MINOR="$(minor_of "$CURRENT_TAG")"
REMAINING="$(remaining_ladder "$CURRENT_MINOR")"
LATEST="$(printf '%s' "$LADDER" | awk '{print $NF}')"

# Build the concrete hop list for this run.
HOPS=""
case "$MODE" in
  next)   HOPS="$(printf '%s' "$REMAINING" | awk '{print $1}')" ;;
  latest) HOPS="$REMAINING" ;;
  to)
    for m in $REMAINING; do
      HOPS="$HOPS $m"
      [ "$m" = "$TARGET" ] && break
    done
    case " $REMAINING " in *" $TARGET "*) : ;; *) die "Версия $TARGET не найдена в лестнице после $CURRENT_MINOR. Доступно: $REMAINING" ;; esac
    ;;
esac
HOPS="$(printf '%s' "$HOPS" | xargs 2>/dev/null || true)"

printf '\n%s\n' "${C_BOLD}${C_BLUE}Authentik — пошаговое обновление${C_RESET}"
info "Текущая версия:   $CURRENT_TAG ($CURRENT_MINOR)"
info "Последняя мажор:  $LATEST"
if [ -z "$REMAINING" ]; then
  ok "Уже на последней известной мажорной версии ($CURRENT_MINOR). Обновлять нечего."
  exit 0
fi
info "Осталось пройти:  $REMAINING"
if [ -z "$HOPS" ]; then ok "Нет шагов для выполнения."; exit 0; fi
info "Этот запуск:      $HOPS"

if [ "$CHECK_ONLY" = 1 ]; then
  exit 0
fi

warn "SSO будет кратко недоступно на каждом шаге. Убедитесь, что это окно обслуживания."
[ "$NONINTERACTIVE" = 1 ] || ask_yesno "Начать обновление ($HOPS)?" no || die "Отменено."

# ── Fast, targeted Authentik-DB dump for rollback ───────────────────────────
ROLLBACK_SQL=""
dump_authentik_db() {
  ROLLBACK_SQL="$(mktemp /tmp/ak-rollback-XXXX.sql)"
  if docker exec "$AK_DB_CONTAINER" pg_dump -U "$AK_USER" -d "$AK_DB" --clean --if-exists \
        > "$ROLLBACK_SQL" 2>/dev/null && [ -s "$ROLLBACK_SQL" ]; then
    ok "Дамп Authentik-БД для отката: $(wc -c < "$ROLLBACK_SQL") байт"
  else
    rm -f "$ROLLBACK_SQL"; ROLLBACK_SQL=""
    die "Не удалось снять дамп Authentik-БД ($AK_DB_CONTAINER) — прерываю до внесения изменений."
  fi
}

restore_authentik_db() {
  [ -n "$ROLLBACK_SQL" ] && [ -s "$ROLLBACK_SQL" ] || { warn "Нет дампа для восстановления Authentik-БД."; return 1; }
  info "Восстанавливаю Authentik-БД из дампа отката…"
  docker exec -i "$AK_DB_CONTAINER" psql -U "$AK_USER" -d "$AK_DB" -q < "$ROLLBACK_SQL" >/dev/null 2>&1
}

# Readiness: 204 from /-/health/ready/ means migrations ran and the app is up.
# Probed from the backend container, which shares the `app` network with Authentik.
wait_for_authentik() {
  local tries=0 max=180
  info "Жду готовности Authentik (миграции применяются автоматически)…"
  while [ $tries -lt $max ]; do
    if run_compose exec -T backend curl -fsS -o /dev/null \
         http://authentik-server:9000/-/health/ready/ 2>/dev/null; then
      ok "Authentik готов."
      return 0
    fi
    tries=$((tries+1)); sleep 2
    [ $((tries % 15)) -eq 0 ] && log "  …ещё жду ($((tries*2))с)"
  done
  err "Authentik не стал готов за $((max*2))с."
  return 1
}

do_hop() {
  local hop="$1" prev_tag="$2"
  step "Обновление до Authentik $hop  (было: $prev_tag)"

  if [ "$DO_BACKUP" = 1 ]; then
    info "Полный бэкап перед шагом…"
    if AIW_COMPOSE="$COMPOSE" AIW_COMPOSE_ARGS="$COMPOSE_ARGS" \
         bash "$SELF_DIR/backup.sh" --label "pre-ak-$hop"; then
      ok "Бэкап создан."
    else
      warn "Полный бэкап не удался."
      [ "$NONINTERACTIVE" = 1 ] || ask_yesno "Продолжить шаг без полного бэкапа (быстрый дамп БД всё равно снимается)?" no || die "Отменено."
    fi
  fi

  dump_authentik_db

  set_env_var "$ENV_FILE" AUTHENTIK_TAG "$hop"
  info "AUTHENTIK_TAG → $hop; тяну образ…"
  if ! run_compose pull authentik-server authentik-worker; then
    set_env_var "$ENV_FILE" AUTHENTIK_TAG "$prev_tag"
    die "Не удалось скачать образ ghcr.io/goauthentik/server:$hop. Тег возвращён на $prev_tag."
  fi

  info "Пересоздаю authentik-server / authentik-worker…"
  run_compose up -d authentik-server authentik-worker

  if wait_for_authentik; then
    ok "Шаг до $hop выполнен."
    rm -f "$ROLLBACK_SQL"; ROLLBACK_SQL=""
    return 0
  fi

  # ── Rollback ──
  err "Шаг до $hop не прошёл health-check. Откатываюсь на $prev_tag."
  set_env_var "$ENV_FILE" AUTHENTIK_TAG "$prev_tag"
  run_compose up -d authentik-server authentik-worker || true
  # Give the reverted containers a moment, then restore the DB into them.
  sleep 5
  if restore_authentik_db; then
    run_compose restart authentik-server authentik-worker || true
    wait_for_authentik || warn "Authentik не поднялся даже после отката — проверьте вручную."
    ok "Откат на $prev_tag выполнен, Authentik-БД восстановлена."
  else
    warn "Автовосстановление БД не удалось. Восстановите из бэкапа: infra/installer/restore.sh"
  fi
  die "Обновление остановлено на версии $prev_tag. Смотрите release notes $hop (breaking changes) и логи authentik-worker."
}

PREV="$CURRENT_TAG"
for hop in $HOPS; do
  do_hop "$hop" "$PREV"
  PREV="$hop"
done

FINAL="$(get_env_var "$ENV_FILE" AUTHENTIK_TAG)"
step "Готово"
ok "Authentik обновлён до $FINAL."
info "Проверьте вход и, при мажорных breaking changes, release notes на docs.goauthentik.io."
