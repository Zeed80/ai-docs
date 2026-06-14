#!/usr/bin/env bash
# Shared helpers for the AI Workspace installer / updater / backup scripts.
# Sourced by install.sh, update.sh, backup.sh, restore.sh.
# Pure bash 3.2+ (works on macOS default bash) — no bashisms requiring bash 4.

# ── Colors / logging ────────────────────────────────────────────────────────
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  C_RESET=$'\033[0m'; C_DIM=$'\033[2m'; C_RED=$'\033[31m'; C_GREEN=$'\033[32m'
  C_YELLOW=$'\033[33m'; C_BLUE=$'\033[34m'; C_BOLD=$'\033[1m'
else
  C_RESET=""; C_DIM=""; C_RED=""; C_GREEN=""; C_YELLOW=""; C_BLUE=""; C_BOLD=""
fi

log()   { printf '%s\n' "${C_DIM}$*${C_RESET}"; }
info()  { printf '%s\n' "${C_BLUE}${C_BOLD}›${C_RESET} $*"; }
ok()    { printf '%s\n' "${C_GREEN}✓${C_RESET} $*"; }
warn()  { printf '%s\n' "${C_YELLOW}!${C_RESET} $*" >&2; }
err()   { printf '%s\n' "${C_RED}✗${C_RESET} $*" >&2; }
die()   { err "$*"; exit 1; }

step()  { printf '\n%s\n' "${C_BOLD}══ $* ══${C_RESET}"; }

# ── OS / arch detection ─────────────────────────────────────────────────────
detect_os() {
  case "$(uname -s)" in
    Linux*)  echo "linux" ;;
    Darwin*) echo "macos" ;;
    *)       echo "unknown" ;;
  esac
}

detect_arch() {
  case "$(uname -m)" in
    x86_64|amd64) echo "amd64" ;;
    arm64|aarch64) echo "arm64" ;;
    *) echo "$(uname -m)" ;;
  esac
}

# ── Dependency checks ───────────────────────────────────────────────────────
has_cmd() { command -v "$1" >/dev/null 2>&1; }

# docker compose v2 (plugin) preferred; fall back to docker-compose v1.
compose_cmd() {
  if docker compose version >/dev/null 2>&1; then
    echo "docker compose"
  elif has_cmd docker-compose; then
    echo "docker-compose"
  else
    echo ""
  fi
}

# Verify required tooling; print actionable install hints per-OS. Returns non-zero
# if anything mandatory is missing.
check_dependencies() {
  local os="$1" missing=0
  if ! has_cmd docker; then
    err "Docker не найден."
    case "$os" in
      macos) log "  Установите Docker Desktop: https://www.docker.com/products/docker-desktop/" ;;
      linux) log "  Установите: curl -fsSL https://get.docker.com | sh" ;;
    esac
    missing=1
  elif ! docker info >/dev/null 2>&1; then
    err "Docker установлен, но демон не запущен (или нет прав)."
    case "$os" in
      macos) log "  Запустите Docker Desktop." ;;
      linux) log "  sudo systemctl start docker  (или добавьте пользователя в группу docker)" ;;
    esac
    missing=1
  fi
  if [ -z "$(compose_cmd)" ]; then
    err "Docker Compose не найден (нужен docker compose v2 или docker-compose)."
    missing=1
  fi
  has_cmd git     || { err "git не найден."; missing=1; }
  has_cmd openssl || { warn "openssl не найден — секреты будут сгенерированы через /dev/urandom."; }
  return $missing
}

# ── Secret generation ───────────────────────────────────────────────────────
gen_secret() {
  if has_cmd openssl; then
    openssl rand -base64 36 | tr -d '/+=' | cut -c1-48
  else
    LC_ALL=C tr -dc 'a-zA-Z0-9' < /dev/urandom | head -c 48
  fi
}

# ── Interactive prompts: whiptail TUI with read() fallback ──────────────────
# TUI is used only when stdin is a TTY, whiptail exists, and AIW_NO_TUI is unset.
_use_tui() {
  [ -t 0 ] && [ -z "${AIW_NO_TUI:-}" ] && has_cmd whiptail
}

# ask_input <var_message> <default> [title]
ask_input() {
  local prompt="$1" default="${2:-}" title="${3:-Настройка}" result
  if _use_tui; then
    result=$(whiptail --title "$title" --inputbox "$prompt" 10 70 "$default" 3>&1 1>&2 2>&3) \
      || result="$default"
  elif [ -t 0 ]; then
    printf '%s [%s]: ' "$prompt" "$default" >&2
    read -r result
    [ -z "$result" ] && result="$default"
  else
    result="$default"
  fi
  printf '%s' "$result"
}

# ask_password <prompt> [title] — empty allowed (caller decides)
ask_password() {
  local prompt="$1" title="${2:-Секрет}" result
  if _use_tui; then
    result=$(whiptail --title "$title" --passwordbox "$prompt" 10 70 3>&1 1>&2 2>&3) || result=""
  elif [ -t 0 ]; then
    printf '%s: ' "$prompt" >&2
    read -rs result; printf '\n' >&2
  else
    result=""
  fi
  printf '%s' "$result"
}

# ask_yesno <prompt> <default:yes|no> [title] → returns 0 for yes, 1 for no
ask_yesno() {
  local prompt="$1" default="${2:-yes}" title="${3:-Подтверждение}" ans
  if _use_tui; then
    if [ "$default" = "yes" ]; then
      whiptail --title "$title" --yesno "$prompt" 10 70 3>&1 1>&2 2>&3
    else
      whiptail --title "$title" --defaultno --yesno "$prompt" 10 70 3>&1 1>&2 2>&3
    fi
    return $?
  elif [ -t 0 ]; then
    local hint="[Y/n]"; [ "$default" = "no" ] && hint="[y/N]"
    printf '%s %s: ' "$prompt" "$hint" >&2
    read -r ans
    [ -z "$ans" ] && ans="$default"
    case "$ans" in [Yy]*|yes) return 0 ;; *) return 1 ;; esac
  else
    [ "$default" = "yes" ]
  fi
}

# ask_menu <prompt> <title> <tag1> <label1> [tag2 label2 ...] → prints chosen tag
ask_menu() {
  local prompt="$1" title="$2"; shift 2
  if _use_tui; then
    local args=() ; while [ $# -gt 0 ]; do args+=("$1" "$2"); shift 2; done
    whiptail --title "$title" --menu "$prompt" 16 70 6 "${args[@]}" 3>&1 1>&2 2>&3
  elif [ -t 0 ]; then
    printf '%s\n' "$prompt" >&2
    local i=1; local tags=()
    while [ $# -gt 0 ]; do
      tags+=("$1"); printf '  %d) %s — %s\n' "$i" "$1" "$2" >&2
      i=$((i+1)); shift 2
    done
    printf 'Выбор [1]: ' >&2; local n; read -r n; [ -z "$n" ] && n=1
    echo "${tags[$((n-1))]:-${tags[0]}}"
  else
    # non-interactive: first option
    echo "$1"
  fi
}

# ── .env helpers ────────────────────────────────────────────────────────────
# set_env_var <file> <KEY> <value> — replace existing KEY= line or append.
set_env_var() {
  local file="$1" key="$2" value="$3" tmp
  tmp="$(mktemp)"
  if grep -qE "^${key}=" "$file" 2>/dev/null; then
    # Use awk to avoid sed delimiter issues with slashes/special chars in value.
    awk -v k="$key" -v v="$value" '
      BEGIN{FS=OFS="="}
      $1==k {print k "=" v; next}
      {print}
    ' "$file" > "$tmp"
    mv "$tmp" "$file"
  else
    printf '%s=%s\n' "$key" "$value" >> "$file"
  fi
}

get_env_var() {
  # awk one-shot — avoids `grep | head` which raises SIGPIPE under
  # `set -o pipefail` (grep writes to a closed pipe → silent script death).
  local file="$1" key="$2"
  [ -f "$file" ] || return 0
  awk -F= -v k="$key" '$1==k{sub(/^[^=]*=/,""); print; exit}' "$file"
}

# ── Wait for service health ─────────────────────────────────────────────────
# wait_for_backend <compose-invocation...> — polls backend /health up to ~5 min.
wait_for_backend() {
  local tries=0 max=150
  info "Жду готовности backend (миграции применяются автоматически)…"
  while [ $tries -lt $max ]; do
    if "$@" exec -T backend curl -fsS http://localhost:8000/health >/dev/null 2>&1; then
      ok "Backend здоров."
      return 0
    fi
    tries=$((tries+1)); sleep 2
    [ $((tries % 15)) -eq 0 ] && log "  …ещё жду ($((tries*2))с)"
  done
  err "Backend не стал здоровым за $((max*2))с. Смотрите логи."
  return 1
}
