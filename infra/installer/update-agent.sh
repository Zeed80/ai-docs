#!/usr/bin/env bash
#
# Host-side operations agent.
#
# The backend (running in a container) cannot touch compose/.env or run
# `docker compose`, so admin-confirmed operations are handed off through request
# files in the shared backups volume. This agent — run on the HOST by a systemd
# timer (see update-agent.README) — picks up a "requested" job and executes it,
# streaming progress back into the same file for the GUI.
#
# Handled jobs:
#   authentik-update.json  → infra/installer/upgrade-authentik.sh (staged ladder)
#   mailcow-install.json   → infra/installer/install-mailcow.sh   (deploy mail server)
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
mkdir -p "$CONTROL_DIR"

# Heartbeat: lets the GUI tell "agent not installed" from "agent idle", instead of
# silently queueing a request that nobody will ever execute.
date -u +%Y-%m-%dT%H:%M:%SZ > "$CONTROL_DIR/agent.heartbeat" 2>/dev/null || true

# Single-flight across timer fires (and across job kinds).
exec 9>"$CONTROL_DIR/.agent.lock"
flock -n 9 || exit 0

# --- tiny JSON helpers (python3), bound to the job currently being processed ---
CONTROL_FILE=""
LOG_FILE=""

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

# run_job <command...> — runs it, keeps current_step/log_tail fresh while it works,
# and records the outcome in the control file.
run_job() {
  ( "$@" >"$LOG_FILE" 2>&1 ) &
  local pid=$!
  local step
  while kill -0 "$pid" 2>/dev/null; do
    step="$(grep -aE '^══ ' "$LOG_FILE" 2>/dev/null | tail -1 | sed 's/^══ //; s/ ══$//')"
    jset current_step="${step:-выполняется}"
    sleep 4
  done
  if wait "$pid"; then
    jset status=done current_step=done "finished_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    return 0
  fi
  local err
  err="$(tail -3 "$LOG_FILE" 2>/dev/null | tr '\n' ' ')"
  jset status=error current_step=failed "finished_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
       "error=${err:-job failed}"
  return 1
}

# ── Job: Authentik staged upgrade ───────────────────────────────────────────
process_authentik() {
  CONTROL_FILE="$CONTROL_DIR/authentik-update.json"
  LOG_FILE="$CONTROL_DIR/authentik-update.log"
  [ -f "$CONTROL_FILE" ] || return 0
  [ "$(jget status)" = "requested" ] || return 0

  local mode target
  mode="$(jget mode)"; target="$(jget target)"
  : > "$LOG_FILE"
  jset status=running "started_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)" current_step=starting

  local args=(--yes)
  case "$mode" in
    next)   args+=(--next) ;;
    to)     args+=(--to "$target") ;;
    latest|"") : ;;   # walk to the end
    *)      jset status=error "error=Неизвестный режим: $mode"; return 0 ;;
  esac

  run_job bash "$SELF_DIR/upgrade-authentik.sh" "${args[@]}" || true
}

# ── Job: Mailcow deployment ─────────────────────────────────────────────────
# The installer is non-interactive with --yes: it pins the tag, writes
# MAIL_DOMAIN/MAILCOW_TAG/ports into infra/.env, wires the Traefik route and
# copies the TLS certificate. DNS, firewall ports, DKIM and the API key stay with
# the human — the GUI guide (/admin/integrations/mailcow-guide) lists them.
process_mailcow_install() {
  CONTROL_FILE="$CONTROL_DIR/mailcow-install.json"
  LOG_FILE="$CONTROL_DIR/mailcow-install.log"
  [ -f "$CONTROL_FILE" ] || return 0
  [ "$(jget status)" = "requested" ] || return 0

  local domain tag tz
  domain="$(jget mail_domain)"; tag="$(jget tag)"; tz="$(jget timezone)"
  if [ -z "$domain" ]; then
    jset status=error "error=В заявке не указан домен почты"
    return 0
  fi

  : > "$LOG_FILE"
  jset status=running "started_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)" current_step=starting

  local args=(--domain "$domain" --yes)
  [ -n "$tag" ] && args+=(--tag "$tag")

  # MAILCOW_TZ is consumed by generate_config.sh inside the installer.
  if MAILCOW_TZ="${tz:-Europe/Moscow}" run_job bash "$SELF_DIR/install-mailcow.sh" "${args[@]}"; then
    # The backend reads MAILCOW_TAG from its environment, so recreate it to pick
    # up the value the installer just wrote into infra/.env.
    local compose
    compose="$(compose_cmd)"
    ( cd "$ROOT_DIR" && $compose -f infra/docker-compose.yml -f infra/docker-compose.prod.yml \
        --env-file "$ENV_FILE" up -d backend >>"$LOG_FILE" 2>&1 ) || true
  fi
}

process_authentik
process_mailcow_install
