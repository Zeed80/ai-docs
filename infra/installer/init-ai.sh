#!/usr/bin/env bash
#
# Initialize the local AI layer: pull Ollama models, apply agent defaults,
# pin the planner model. Idempotent — safe to re-run.
#
# Reads compose invocation from env (set by install.sh) or derives it:
#   AIW_COMPOSE="docker compose"  AIW_COMPOSE_ARGS="-f ... --env-file ..."
#
# Model choices (override via env):
#   AIW_AGENT_MODEL       main reasoning/worker model (default APEX 35B MoE)
#   AIW_EMBED_MODEL       embeddings
#   AIW_RERANK_MODEL      reranker
#   AIW_EXTRACT_MODEL     OCR/extraction model
#   AIW_SKIP_PULL=1       apply config only, don't pull models
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
. "$SCRIPT_DIR/lib.sh"
cd "$REPO_DIR"

ENV_FILE="infra/.env"
COMPOSE="${AIW_COMPOSE:-$(compose_cmd)}"
COMPOSE_ARGS="${AIW_COMPOSE_ARGS:--f infra/docker-compose.yml --env-file $ENV_FILE}"
# shellcheck disable=SC2086
run_compose() { $COMPOSE $COMPOSE_ARGS "$@"; }

AGENT_MODEL="${AIW_AGENT_MODEL:-fredrezones55/Qwen3.6-35B-A3B-APEX:Compact}"
EMBED_MODEL="${AIW_EMBED_MODEL:-qwen3-embedding:8b}"
RERANK_MODEL="${AIW_RERANK_MODEL:-qllama/bge-reranker-v2-m3:f16}"
EXTRACT_MODEL="${AIW_EXTRACT_MODEL:-qwen3.5:9b}"

AGENT_KEY="$(get_env_var "$ENV_FILE" AGENT_SERVICE_KEY)"
[ -z "$AGENT_KEY" ] && die "AGENT_SERVICE_KEY не найден в $ENV_FILE."

# Ollama is reached from inside the backend container via host-gateway.
ollama_api() { run_compose exec -T backend curl -fsS "http://host-gateway:11434$1" "${@:2}"; }
backend_api() {
  local method="$1" path="$2" body="${3:-}"
  if [ -n "$body" ]; then
    run_compose exec -T backend curl -fsS -X "$method" \
      -H "X-API-Key: $AGENT_KEY" -H "Content-Type: application/json" \
      "http://localhost:8000$path" -d "$body"
  else
    run_compose exec -T backend curl -fsS -X "$method" \
      -H "X-API-Key: $AGENT_KEY" "http://localhost:8000$path"
  fi
}

step "Проверка Ollama"
if ! ollama_api /api/tags >/dev/null 2>&1; then
  warn "Ollama недоступен по host-gateway:11434."
  warn "Установите Ollama на хосте (https://ollama.com) и запустите 'ollama serve', затем повторите."
  warn "Конфиг агента всё равно применю — модели подтянутся при первом обращении."
fi

# ── Pull models ──────────────────────────────────────────────────────────────
pull_model() {
  local model="$1"
  [ -z "$model" ] && return 0
  info "Загрузка модели: $model (может занять время)…"
  if ollama_api /api/pull -X POST -d "{\"model\":\"$model\",\"stream\":false}" >/dev/null 2>&1; then
    ok "  $model готова"
  else
    warn "  Не удалось загрузить $model (Ollama недоступен?). Пропускаю."
  fi
}

if [ "${AIW_SKIP_PULL:-0}" != 1 ]; then
  step "Загрузка моделей Ollama"
  pull_model "$AGENT_MODEL"
  pull_model "$EMBED_MODEL"
  pull_model "$RERANK_MODEL"
  pull_model "$EXTRACT_MODEL"
else
  warn "AIW_SKIP_PULL=1 — пропускаю загрузку моделей."
fi

# ── Apply agent defaults ──────────────────────────────────────────────────────
# One resident model on one GPU (see ops notes): all roles = APEX, fast/auditor
# inherit it; planner routing pinned to the same model so it stays warm.
step "Применение дефолтов агента"
agent_cfg=$(cat <<JSON
{
  "model": "$AGENT_MODEL",
  "orchestrator_model": "$AGENT_MODEL",
  "worker_model": "$AGENT_MODEL",
  "auditor_model": "$AGENT_MODEL",
  "fast_model": null,
  "max_history_messages": 12,
  "max_worker_steps": 5,
  "max_audit_retries": 0,
  "orchestrator_plan_timeout_seconds": 12.0
}
JSON
)
if backend_api PATCH /api/ai/agent-config "$agent_cfg" >/dev/null 2>&1; then
  ok "Конфиг агента применён (все роли = $AGENT_MODEL)."
else
  warn "Не удалось применить конфиг агента через API."
fi

# Pin the planner routing to the agent model so it is the resident (keep_alive=-1)
# model — otherwise an ephemeral default reloads 18GB on every idle gap. The
# routing store keys by catalog name; default to the APEX key, override via env.
step "Закрепление модели планировщика"
plan_key="${AIW_PLAN_KEY:-qwen3_6_35b_apex_ollama}"
routing_body="{\"models\":[\"$plan_key\"],\"profile\":\"structured_reasoning\",\"local_only\":true,\"allow_cloud\":false}"
if backend_api PUT /api/local-models/routing/orchestrator_planning "$routing_body" >/dev/null 2>&1; then
  ok "Планировщик закреплён на $plan_key (резидентная модель)."
else
  warn "Не удалось закрепить планировщик (ключ $plan_key не в каталоге?). Пропускаю."
fi

# Warm the model into VRAM with keep_alive=-1.
if ollama_api /api/tags >/dev/null 2>&1; then
  info "Прогрев $AGENT_MODEL (keep_alive=-1)…"
  ollama_api /api/generate -X POST \
    -d "{\"model\":\"$AGENT_MODEL\",\"prompt\":\"ok\",\"stream\":false,\"keep_alive\":-1,\"options\":{\"num_predict\":1}}" \
    >/dev/null 2>&1 && ok "Модель прогрета и закреплена в VRAM." || warn "Прогрев пропущен."
fi

ok "Инициализация AI завершена."
