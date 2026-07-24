#!/usr/bin/env bash
#
# Staged Mailcow updater — same shape as upgrade-authentik.sh (backup → pull →
# recreate → health-check → rollback on failure), adapted to Mailcow's rolling
# tag releases (no major-version ladder to walk, unlike Authentik).
#
# Mailcow ships frequent security releases (docs.mailcow.email/maintenance/update/,
# mailcow.email/posts/). Pin to an explicit tag here rather than update.sh's own
# `git pull` on a branch, so an update is a deliberate, reviewable, rollback-able
# step — consistent with how the rest of this repo treats infra upgrades.
#
# Usage:
#   update-mailcow.sh --check                 # show current vs. target tag, do nothing
#   update-mailcow.sh --to 2026-06a [--yes]    # update to a specific tag
#   update-mailcow.sh [--yes]                  # update to DEFAULT_TAG below
#     --no-backup   skip the full backup.sh call (targeted mysql dump is always taken)
#
set -euo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
ROOT_DIR="$(cd "$SELF_DIR/../.." && pwd)"
. "$SELF_DIR/lib.sh"
cd "$ROOT_DIR"

ENV_FILE="infra/.env"
MAILCOW_DIR="infra/mailcow"
# Bump deliberately after checking https://github.com/mailcow/mailcow-dockerized/releases
# for security-relevant changes since the currently pinned tag.
DEFAULT_TAG="2026-05c"

DO_BACKUP=1
NONINTERACTIVE=0
MODE="default"   # default | to
TARGET=""
CHECK_ONLY=0

while [ $# -gt 0 ]; do
  case "$1" in
    --check) CHECK_ONLY=1; shift ;;
    --to) MODE="to"; TARGET="$2"; shift 2 ;;
    --no-backup) DO_BACKUP=0; shift ;;
    --yes|--non-interactive) NONINTERACTIVE=1; export AIW_NO_TUI=1; shift ;;
    -h|--help) grep -E '^#( |$)' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "Неизвестный флаг: $1" ;;
  esac
done

[ -d "$MAILCOW_DIR/.git" ] || die "$MAILCOW_DIR не найден — сначала запустите install-mailcow.sh."
COMPOSE="$(compose_cmd)"; [ -z "$COMPOSE" ] && die "Docker Compose не найден."

CURRENT_TAG="$(git -C "$MAILCOW_DIR" describe --tags --exact-match 2>/dev/null || git -C "$MAILCOW_DIR" rev-parse --short HEAD)"
TARGET_TAG="$DEFAULT_TAG"
[ "$MODE" = "to" ] && TARGET_TAG="$TARGET"

printf '\n%s\n' "${C_BOLD}${C_BLUE}Mailcow — обновление${C_RESET}"
info "Текущий тег:  $CURRENT_TAG"
info "Целевой тег:  $TARGET_TAG"

if [ "$CURRENT_TAG" = "$TARGET_TAG" ]; then
  ok "Уже на целевом теге ($TARGET_TAG). Обновлять нечего."
  exit 0
fi
[ "$CHECK_ONLY" = 1 ] && exit 0

warn "Веб/почта Mailcow будет кратко недоступна во время обновления."
[ "$NONINTERACTIVE" = 1 ] || ask_yesno "Обновить Mailcow $CURRENT_TAG → $TARGET_TAG?" no || die "Отменено."

run_mailcow() { ( cd "$MAILCOW_DIR" && $COMPOSE "$@" ); }

# ── Targeted mysql dump for fast rollback (Mailcow's own helper script) ────
DUMP_DIR=""
dump_mailcow_db() {
  DUMP_DIR="$(mktemp -d /tmp/mailcow-rollback-XXXX)"
  info "Снимаю дамп БД Mailcow для отката ($DUMP_DIR)…"
  if ( cd "$MAILCOW_DIR" && MAILCOW_BACKUP_LOCATION="$DUMP_DIR" yes "" | ./helper-scripts/backup_and_restore.sh backup mysql >/dev/null 2>&1 ); then
    ok "Дамп БД снят."
  else
    rm -rf "$DUMP_DIR"; DUMP_DIR=""
    die "Не удалось снять дамп БД Mailcow — прерываю до внесения изменений."
  fi
}

restore_mailcow_db() {
  [ -n "$DUMP_DIR" ] && [ -d "$DUMP_DIR" ] || { warn "Нет дампа для восстановления БД Mailcow."; return 1; }
  info "Восстанавливаю БД Mailcow из дампа отката…"
  ( cd "$MAILCOW_DIR" && MAILCOW_BACKUP_LOCATION="$DUMP_DIR" yes "" | ./helper-scripts/backup_and_restore.sh restore mysql >/dev/null 2>&1 )
}

# Readiness: nginx-mailcow answers on the loopback HTTP bind set by
# install-mailcow.sh (HTTP_BIND=127.0.0.1, HTTP_PORT=8080).
wait_for_mailcow() {
  local tries=0 max=150
  info "Жду готовности Mailcow…"
  while [ $tries -lt $max ]; do
    if curl -fsS -o /dev/null "http://127.0.0.1:8080/" 2>/dev/null; then
      ok "Mailcow отвечает."
      return 0
    fi
    tries=$((tries+1)); sleep 2
    [ $((tries % 15)) -eq 0 ] && log "  …ещё жду ($((tries*2))с)"
  done
  err "Mailcow не ответил за $((max*2))с."
  return 1
}

if [ "$DO_BACKUP" = 1 ]; then
  info "Полный бэкап перед обновлением…"
  bash "$SELF_DIR/backup.sh" --label "pre-mailcow-$TARGET_TAG" \
    || { warn "Полный бэкап не удался."; [ "$NONINTERACTIVE" = 1 ] || ask_yesno "Продолжить без него (целевой дамп БД всё равно снимается)?" no || die "Отменено."; }
fi

dump_mailcow_db

info "Переключаю infra/mailcow на тег $TARGET_TAG…"
if ! git -C "$MAILCOW_DIR" fetch --tags --quiet || ! git -C "$MAILCOW_DIR" checkout --quiet "$TARGET_TAG"; then
  die "Не удалось переключиться на тег $TARGET_TAG. Проверьте https://github.com/mailcow/mailcow-dockerized/releases"
fi

info "Тяну образы и пересоздаю контейнеры…"
run_mailcow pull
run_mailcow up -d

if wait_for_mailcow; then
  ok "Mailcow обновлён до $TARGET_TAG."
  rm -rf "$DUMP_DIR"
  exit 0
fi

# ── Rollback ──
err "Обновление до $TARGET_TAG не прошло health-check. Откатываюсь на $CURRENT_TAG."
git -C "$MAILCOW_DIR" checkout --quiet "$CURRENT_TAG" || warn "Не удалось вернуть git на $CURRENT_TAG вручную — проверьте."
run_mailcow up -d || true
sleep 5
if restore_mailcow_db; then
  run_mailcow restart || true
  wait_for_mailcow || warn "Mailcow не поднялся даже после отката — проверьте вручную."
  ok "Откат на $CURRENT_TAG выполнен, БД восстановлена."
else
  warn "Автовосстановление БД не удалось. Восстановите из бэкапа: infra/installer/restore.sh"
fi
die "Обновление остановлено на версии $CURRENT_TAG. Смотрите https://mailcow.email/ (release notes) и логи контейнеров."
