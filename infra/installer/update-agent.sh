#!/usr/bin/env bash
#
# Host-side update agent.
#
# The backend (running in a container) cannot touch compose/.env, so admin-
# confirmed upgrades are handed off through a request file in the shared backups
# volume. This agent — run on the HOST by a systemd timer (see
# update-agent.README) — picks up a "requested" job and executes the staged
# Authentik upgrader, streaming progress back into the same file for the GUI.
#
# It is intentionally tiny and idempotent: if there's no pending request it exits
# immediately, and a flock prevents overlapping timer fires from double-running.
#
set -euo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
ROOT_DIR="$(cd "$SELF_DIR/../.." && pwd)"
. "$SELF_DIR/lib.sh"
cd "$ROOT_DIR"

ENV_FILE="infra/.env"
PROJECT="$(get_env_var "$ENV_FILE" COMPOSE_PROJECT_NAME)"; PROJECT="${PROJECT:-infra}"

command -v python3 >/dev/null 2>&1 || { echo "python3 требуется для update-agent" >&2; exit 1; }

# Locate the backups volume on the host; that's where the backend drops requests.
MOUNT="$(docker volume inspect "${PROJECT}_backups_data" -f '{{ .Mountpoint }}' 2>/dev/null || true)"
[ -n "$MOUNT" ] || { echo "Том ${PROJECT}_backups_data не найден" >&2; exit 0; }
CONTROL_DIR="$MOUNT/_control"
CONTROL_FILE="$CONTROL_DIR/authentik-update.json"
LOG_FILE="$CONTROL_DIR/authentik-update.log"

[ -f "$CONTROL_FILE" ] || exit 0   # nothing queued

# Single-flight across timer fires.
exec 9>"$CONTROL_DIR/.agent.lock"
flock -n 9 || exit 0

# --- tiny JSON helpers (python3) ---
jget() { python3 -c "import json,sys; print(json.load(open('$CONTROL_FILE')).get('$1','') or '')" 2>/dev/null || true; }
jset() {
  # jset key=value [key=value ...]; values are strings. log_tail read from $LOG_FILE.
  python3 - "$@" <<PY
import json, sys
p = "$CONTROL_FILE"
d = json.load(open(p))
for kv in sys.argv[1:]:
    k, _, v = kv.partition("=")
    d[k] = v if v != "" else None
try:
    d["log_tail"] = "".join(open("$LOG_FILE").readlines()[-40:])
except Exception:
    pass
tmp = p + ".tmp"
json.dump(d, open(tmp, "w"), ensure_ascii=False, indent=2)
import os; os.replace(tmp, p)
PY
}

STATUS="$(jget status)"
[ "$STATUS" = "requested" ] || exit 0   # only act on fresh requests

MODE="$(jget mode)"; TARGET="$(jget target)"
: > "$LOG_FILE"
jset status=running "started_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)" current_step=starting

# Build upgrader args from the request.
ARGS=(--yes)
case "$MODE" in
  next)   ARGS+=(--next) ;;
  to)     ARGS+=(--to "$TARGET") ;;
  latest|"") : ;;   # walk to the end
  *)      jset status=error "error=Неизвестный режим: $MODE"; exit 0 ;;
esac

# Run the staged upgrader, tee to the log, and refresh the control file's
# log_tail/current_step while it runs so the GUI shows live progress.
( bash "$SELF_DIR/upgrade-authentik.sh" "${ARGS[@]}" >"$LOG_FILE" 2>&1 ) &
UPGRADE_PID=$!

while kill -0 "$UPGRADE_PID" 2>/dev/null; do
  STEP="$(grep -aE '^══ ' "$LOG_FILE" 2>/dev/null | tail -1 | sed 's/^══ //; s/ ══$//')"
  jset current_step="${STEP:-выполняется}"
  sleep 4
done

if wait "$UPGRADE_PID"; then
  jset status=done current_step=done "finished_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
else
  ERR="$(tail -3 "$LOG_FILE" 2>/dev/null | tr '\n' ' ')"
  jset status=error current_step=failed "finished_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)" "error=${ERR:-upgrade failed}"
fi
