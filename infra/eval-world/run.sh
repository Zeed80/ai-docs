#!/usr/bin/env bash
# E49: one live employee evaluation in a fresh isolated world.
#
#   infra/eval-world/run.sh [--case ID]... [--repeat N]
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
cleanup() {
  [ "${EVAL_KEEP_WORLD:-0}" = "1" ] && return
  "${COMPOSE[@]}" down -v >/dev/null 2>&1 || true
}
trap cleanup EXIT

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
DIGEST=$(docker exec -i -w /app aiw-eval-eval-backend-1 python -m app.ai.evals.live_world import < "$SETUP" 2>/dev/null | tail -1)
rm -f "$SETUP"

LOG=$(mktemp)
docker exec -w /app aiw-eval-eval-backend-1 python -m app.ai.evals.live_runner \
  --corpus app/ai/evals/data/employee_live_v1.yaml \
  --out /tmp/results.jsonl --code-revision "$REV" --setup-digest "$DIGEST" "$@" \
  > "$LOG" 2>&1 || true
if ! grep -E '^[a-z0-9-]+: (passed|failed|error)' "$LOG"; then
  # No verdict at all: the runner itself failed — show why instead of silence.
  echo "runner produced no results; last lines:" >&2
  tail -20 "$LOG" >&2
fi
rm -f "$LOG"
docker cp aiw-eval-eval-backend-1:/tmp/results.jsonl "$OUT_DIR/$STAMP.jsonl"
echo "results: $OUT_DIR/$STAMP.jsonl"
