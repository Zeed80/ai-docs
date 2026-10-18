#!/usr/bin/env bash
# E49: one live employee evaluation in a fresh isolated world.
#
#   infra/eval-world/run.sh [--case ID]... [--repeat N]
#   EVAL_CORPUS=employee_security_v1 infra/eval-world/run.sh   # E51 attacks
#
# Builds nothing: it measures the images the stack runs now. The world is
# created empty, takes the stack's model setup (agent config, task routing,
# GPU owner), runs the corpus and is destroyed; results land in
# infra/eval-world/results/<timestamp>.jsonl.
set -euo pipefail
cd "$(dirname "$0")/../.."

COMPOSE=(docker compose -f infra/eval-world/docker-compose.yml)
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
OUT_DIR=infra/eval-world/results
mkdir -p "$OUT_DIR"
REV=$(git rev-parse --short HEAD)$(git diff --quiet HEAD -- backend aiagent || echo "-dirty")

# EVAL_KEEP_WORLD=1 leaves the world up for inspection (down it yourself:
# docker compose -f infra/eval-world/docker-compose.yml down -v).
# Results are saved before the world goes: a run stopped midway (E52 was
# killed at 72/363 on host memory) lost everything but its log.
cleanup() {
  docker cp aiw-eval-eval-backend-1:/tmp/results.jsonl "$OUT_DIR/$STAMP.jsonl" >/dev/null 2>&1 || true
  # The API and the worker log what the runner only sees as "HTTP 500"
  # (E52: catalog cases failed 0/3 twice with nothing to read afterwards).
  for svc in backend worker; do
    docker logs "aiw-eval-eval-$svc-1" 2>&1 | gzip > "$OUT_DIR/$STAMP.$svc.log.gz" || true
  done
  [ "${EVAL_KEEP_WORLD:-0}" = "1" ] && return
  "${COMPOSE[@]}" down -v >/dev/null 2>&1 || true
}
trap cleanup EXIT
trap 'exit 130' INT TERM

export STRATA_API_KEY
STRATA_API_KEY=$(docker exec infra-backend-1 printenv STRATA_API_KEY || true)
"${COMPOSE[@]}" down -v >/dev/null 2>&1 || true
"${COMPOSE[@]}" up -d
until docker exec aiw-eval-eval-backend-1 curl -sf localhost:8000/health >/dev/null 2>&1; do
  sleep 3
done

# Corpus development: EVAL_USE_WORKTREE=1 runs the working tree's eval code
# and corpus without rebuilding the image (the result says "-dirty").
if [ "${EVAL_USE_WORKTREE:-0}" = "1" ]; then
  docker cp backend/app/ai/evals/. aiw-eval-eval-backend-1:/app/app/ai/evals/
  REV="$REV-worktree"
fi

SETUP=$(mktemp)
docker exec -w /app infra-backend-1 python -m app.ai.evals.live_world export > "$SETUP"
# EVAL_AGENT_CONFIG='{"department_enabled": false}' changes the agent config of
# this world only (an A/B against the stack's setup); it enters the setup
# digest, so such results never pass for the stack's own.
if [ -n "${EVAL_AGENT_CONFIG:-}" ]; then
  python3 - "$SETUP" "$EVAL_AGENT_CONFIG" <<'PY'
import json, sys
path, override = sys.argv[1], json.loads(sys.argv[2])
setup = json.load(open(path))
setup["agent_config"].update(override)
setup.pop("digest", None)  # the import digests the changed content
json.dump(setup, open(path, "w"))
PY
fi
DIGEST=$(docker exec -i -w /app aiw-eval-eval-backend-1 python -m app.ai.evals.live_world import < "$SETUP" 2>/dev/null | tail -1)
rm -f "$SETUP"

LOG="$OUT_DIR/$STAMP.log"
docker exec -w /app aiw-eval-eval-backend-1 python -m app.ai.evals.live_runner \
  --corpus "app/ai/evals/data/${EVAL_CORPUS:-employee_live_v1}.yaml" \
  --out /tmp/results.jsonl --code-revision "$REV" --setup-digest "$DIGEST" "$@" \
  > "$LOG" 2>&1 || true
if ! grep -E '^[a-z0-9-]+: (passed|failed|error)' "$LOG"; then
  # No verdict at all: the runner itself failed — show why instead of silence.
  echo "runner produced no results; last lines:" >&2
  tail -20 "$LOG" >&2
fi
docker cp aiw-eval-eval-backend-1:/tmp/results.jsonl "$OUT_DIR/$STAMP.jsonl"
echo "results: $OUT_DIR/$STAMP.jsonl"
