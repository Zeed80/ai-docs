#!/usr/bin/env bash
#
# AI Workspace — one-line installer (Linux / macOS).
#
#   curl -fsSL https://raw.githubusercontent.com/Zeed80/ai-docs/main/install.sh | bash
#
# or, from a cloned repo:
#
#   ./install.sh                 # interactive (TUI if whiptail present)
#   ./install.sh --mode prod --domain example.com --email me@example.com --yes
#
# Flags:
#   --mode dev|prod          deployment mode (default: ask, fallback dev)
#   --domain <d>             public domain (prod)
#   --email <e>              ACME / admin email (prod)
#   --branch <b>             git branch to clone/checkout (default: main)
#   --dir <path>            install directory for curl|bash mode (default: ./ai-workspace)
#   --no-ai                  skip pulling Ollama models / agent defaults
#   --reconfigure            regenerate infra/.env even if it exists
#   --yes, --non-interactive run unattended with defaults/flags (no prompts)
#
set -euo pipefail

REPO_URL="${AIW_REPO_URL:-https://github.com/Zeed80/ai-docs.git}"
BRANCH="main"
MODE=""
DOMAIN=""
EMAIL=""
INSTALL_DIR=""
NO_AI=0
RECONFIGURE=0
NONINTERACTIVE=0

while [ $# -gt 0 ]; do
  case "$1" in
    --mode) MODE="$2"; shift 2 ;;
    --domain) DOMAIN="$2"; shift 2 ;;
    --email) EMAIL="$2"; shift 2 ;;
    --branch) BRANCH="$2"; shift 2 ;;
    --dir) INSTALL_DIR="$2"; shift 2 ;;
    --no-ai) NO_AI=1; shift ;;
    --reconfigure) RECONFIGURE=1; shift ;;
    --yes|--non-interactive) NONINTERACTIVE=1; export AIW_NO_TUI=1; shift ;;
    -h|--help) grep -E '^#( |$)' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Неизвестный флаг: $1" >&2; exit 1 ;;
  esac
done

# ── Locate the repo (or clone it for curl|bash) ─────────────────────────────
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"

if [ -n "$SELF_DIR" ] && [ -f "$SELF_DIR/infra/installer/lib.sh" ]; then
  REPO_DIR="$SELF_DIR"
else
  # curl|bash mode — clone the repo, then re-exec the bundled installer.
  command -v git >/dev/null 2>&1 || { echo "git требуется для установки." >&2; exit 1; }
  TARGET="${INSTALL_DIR:-./ai-workspace}"
  if [ -d "$TARGET/.git" ]; then
    echo "Репозиторий уже есть в $TARGET — обновляю."
    git -C "$TARGET" fetch --depth 1 origin "$BRANCH" && git -C "$TARGET" checkout "$BRANCH" && git -C "$TARGET" pull --ff-only
  else
    echo "Клонирую $REPO_URL ($BRANCH) → $TARGET"
    git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$TARGET"
  fi
  REPO_DIR="$(cd "$TARGET" && pwd)"
  exec bash "$REPO_DIR/install.sh" \
    ${MODE:+--mode "$MODE"} ${DOMAIN:+--domain "$DOMAIN"} ${EMAIL:+--email "$EMAIL"} \
    --branch "$BRANCH" $([ "$NO_AI" = 1 ] && echo --no-ai) \
    $([ "$RECONFIGURE" = 1 ] && echo --reconfigure) \
    $([ "$NONINTERACTIVE" = 1 ] && echo --yes)
fi

# shellcheck source=infra/installer/lib.sh
. "$REPO_DIR/infra/installer/lib.sh"
cd "$REPO_DIR"

ENV_FILE="infra/.env"

printf '\n%s\n' "${C_BOLD}${C_BLUE}AI Workspace — установка${C_RESET}"

# ── 1. Environment & dependencies ───────────────────────────────────────────
step "1/6  Проверка окружения"
OS="$(detect_os)"; ARCH="$(detect_arch)"
[ "$OS" = "unknown" ] && die "Неподдерживаемая ОС: $(uname -s). Поддерживаются Linux и macOS."
ok "ОС: $OS ($ARCH)"
check_dependencies "$OS" || die "Установите недостающие зависимости и повторите."
COMPOSE="$(compose_cmd)"
ok "Docker Compose: $COMPOSE"

# ── 2. Mode selection ───────────────────────────────────────────────────────
step "2/6  Режим развёртывания"
if [ -z "$MODE" ]; then
  if [ "$NONINTERACTIVE" = 1 ]; then MODE="dev"; else
    MODE="$(ask_menu "Выберите режим развёртывания:" "Режим" \
      dev  "Локальная разработка (localhost, без TLS/SSO)" \
      prod "Продакшен (домен, HTTPS, SSO)")"
  fi
fi
[ "$MODE" = "dev" ] || [ "$MODE" = "prod" ] || die "Неверный режим: $MODE"
ok "Режим: $MODE"

# ── 3. Configure infra/.env ─────────────────────────────────────────────────
step "3/6  Конфигурация ($ENV_FILE)"
configure_env() {
  cp infra/.env.example "$ENV_FILE"
  # Always generate strong secrets (safe for both dev and prod).
  for key in APP_SECRET_KEY CSRF_SECRET AGENT_SERVICE_KEY POSTGRES_PASSWORD \
             MINIO_SECRET_KEY OAUTH_CLIENT_SECRET AUTHENTIK_SECRET_KEY \
             AUTHENTIK_DB_PASSWORD AUTHENTIK_BOOTSTRAP_PASSWORD \
             REDIS_PASSWORD QDRANT_API_KEY; do
    set_env_var "$ENV_FILE" "$key" "$(gen_secret)"
  done

  if [ "$MODE" = "prod" ]; then
    [ -z "$DOMAIN" ] && [ "$NONINTERACTIVE" != 1 ] && \
      DOMAIN="$(ask_input "Публичный домен (например, ptsai.ru):" "" "Домен")"
    [ -z "$EMAIL" ] && [ "$NONINTERACTIVE" != 1 ] && \
      EMAIL="$(ask_input "Email для Let's Encrypt / админа:" "admin@${DOMAIN:-company.com}" "Email")"
    DOMAIN="${DOMAIN:-localhost}"; EMAIL="${EMAIL:-admin@company.com}"
    set_env_var "$ENV_FILE" APP_ENV production
    set_env_var "$ENV_FILE" AUTH_ENABLED true
    set_env_var "$ENV_FILE" CSP_ENABLED true
    set_env_var "$ENV_FILE" TRUSTED_PROXY true
    set_env_var "$ENV_FILE" TRAEFIK_DOMAIN "$DOMAIN"
    set_env_var "$ENV_FILE" TRAEFIK_ACME_EMAIL "$EMAIL"
    set_env_var "$ENV_FILE" CORS_ORIGINS "https://$DOMAIN"
    set_env_var "$ENV_FILE" AUTHENTIK_EXTERNAL_URL "https://$DOMAIN"
    set_env_var "$ENV_FILE" NEXT_PUBLIC_API_URL same-origin
    set_env_var "$ENV_FILE" NEXT_PUBLIC_WS_URL same-origin
    set_env_var "$ENV_FILE" AUTHENTIK_BOOTSTRAP_EMAIL "$EMAIL"
    ok "Прод-конфиг: домен=$DOMAIN, email=$EMAIL, секреты сгенерированы"
  else
    set_env_var "$ENV_FILE" APP_ENV development
    ok "Dev-конфиг: localhost, секреты сгенерированы"
  fi

  # Optional Anthropic key (cloud reasoning); local Ollama works without it.
  if [ "$NONINTERACTIVE" != 1 ]; then
    if ask_yesno "Добавить ключ Anthropic API (облачный reasoning)? Локальный Ollama работает и без него." no "AI"; then
      local akey; akey="$(ask_password "ANTHROPIC_API_KEY (sk-ant-…):" "Anthropic")"
      [ -n "$akey" ] && set_env_var "$ENV_FILE" ANTHROPIC_API_KEY "$akey"
    fi
  fi
}

if [ -f "$ENV_FILE" ] && [ "$RECONFIGURE" != 1 ]; then
  warn "$ENV_FILE уже существует — пропускаю генерацию (используйте --reconfigure для пересоздания)."
else
  [ -f "$ENV_FILE" ] && cp "$ENV_FILE" "$ENV_FILE.bak.$(date +%s)" && log "  Бэкап старого .env создан."
  configure_env
fi

# ── 4. Build & start the stack ──────────────────────────────────────────────
step "4/6  Сборка и запуск стека"
if [ "$MODE" = "prod" ]; then
  COMPOSE_ARGS="-f infra/docker-compose.yml -f infra/docker-compose.prod.yml --env-file $ENV_FILE"
else
  COMPOSE_ARGS="-f infra/docker-compose.yml -f infra/docker-compose.dev.yml"
fi
# shellcheck disable=SC2086
run_compose() { $COMPOSE $COMPOSE_ARGS "$@"; }

info "docker compose up -d --build (это займёт время при первом запуске)…"
run_compose up -d --build
ok "Контейнеры запущены."

# ── 5. Wait for health (migrations run automatically in entrypoint) ─────────
step "5/6  Инициализация БД и проверка здоровья"
wait_for_backend run_compose || die "Стек не поднялся. Логи: $COMPOSE $COMPOSE_ARGS logs backend"

# ── 6. Optional AI initialization ───────────────────────────────────────────
step "6/6  Инициализация AI (модели и агент)"
if [ "$NO_AI" = 1 ]; then
  warn "Пропущено (--no-ai). Запустите позже: bash infra/installer/init-ai.sh"
else
  if [ "$NONINTERACTIVE" = 1 ] || ask_yesno "Загрузить локальные модели Ollama и применить дефолты агента? (большие загрузки)" no "AI"; then
    AIW_COMPOSE="$COMPOSE" AIW_COMPOSE_ARGS="$COMPOSE_ARGS" \
      bash "$REPO_DIR/infra/installer/init-ai.sh" || warn "Инициализация AI завершилась с предупреждениями."
  else
    warn "Пропущено. Запустите позже: bash infra/installer/init-ai.sh"
  fi
fi

# ── Done ────────────────────────────────────────────────────────────────────
step "Готово"
if [ "$MODE" = "prod" ]; then
  ok "Откройте: https://${DOMAIN:-<домен>}"
else
  ok "Откройте:  http://localhost:3000  (API: http://localhost:8000)"
fi
log "  Статус:   $COMPOSE $COMPOSE_ARGS ps"
log "  Логи:     $COMPOSE $COMPOSE_ARGS logs -f backend"
log "  Обновить: ./update.sh"
log "  Бэкап:    bash infra/installer/backup.sh"
