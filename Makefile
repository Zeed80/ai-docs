.PHONY: help \
        dev dev-build dev-bg dev-llamacpp down restart \
        prod prod-bg prod-build prod-down \
        clean rebuild nuke \
        setup health logs ps shell-backend shell-celery shell-frontend \
        migrate migrate-new seed \
        test test-cov e2e regression emg-schema emg-schema-check emg-validate emg-regression emg-live-regression agent-regression agent-test agent-ws-smoke \
        studio-queue-smoke cad-kernel-smoke cad-regression cad-candidate-gate cad-drawing-graph-eval \
        cad-corpus-acquire cad-corpus-generate cad-pmi-truth \
        turboquant-benchmark turboquant-quality \
        lint lint-fix \
        skills aiagent-contract \
        monitoring monitoring-down

# ──────────────────────────────────────────────────────────────────────────────
# Docker Compose file sets
# ──────────────────────────────────────────────────────────────────────────────
COMPOSE_DEV      := -f infra/docker-compose.yml -f infra/docker-compose.dev.yml
COMPOSE_PROD     := -f infra/docker-compose.yml -f infra/docker-compose.prod.yml --env-file infra/.env
COMPOSE_LLAMACPP := -f infra/docker-compose.yml --profile embedded-llamacpp

# ──────────────────────────────────────────────────────────────────────────────
# help — list all targets with descriptions
# ──────────────────────────────────────────────────────────────────────────────
help:
	@echo ""
	@echo "  AI Manufacturing Workspace — make targets"
	@echo ""
	@echo "  DEVELOPMENT"
	@echo "    make dev              — dev stack (fg, hot-reload via volume mounts)"
	@echo "    make dev-bg           — dev stack (bg, detached)"
	@echo "    make dev-build        — dev stack + rebuild images"
	@echo "    make dev-llamacpp     — add llama.cpp server to running dev stack"
	@echo "    make down             — stop dev stack (keep volumes)"
	@echo "    make restart          — restart all dev containers"
	@echo ""
	@echo "  PRODUCTION"
	@echo "    make prod             — production stack (fg)"
	@echo "    make prod-bg          — production stack (bg, detached)"
	@echo "    make prod-build       — production stack + build (detached)"
	@echo "    make prod-down        — stop production stack"
	@echo ""
	@echo "  CLEAN / REBUILD"
	@echo "    make clean            — stop + remove local images + prune build cache"
	@echo "    make rebuild          — clean + build from scratch (no cache) + start dev"
	@echo "    make nuke             — ⚠️  clean + remove ALL volumes (data loss!)"
	@echo ""
	@echo "  SETUP / FIRST RUN"
	@echo "    make setup            — copy .env.example → infra/.env (if missing)"
	@echo "    make health           — show container health status"
	@echo "    make ps               — show running containers"
	@echo "    make logs             — tail all logs"
	@echo "    make shell-backend    — exec bash inside backend container"
	@echo "    make shell-celery     — exec bash inside celery-worker container"
	@echo "    make shell-frontend   — exec bash inside frontend container"
	@echo ""
	@echo "  DATABASE"
	@echo "    make migrate          — run alembic upgrade head"
	@echo "    make migrate-new msg=X — create new migration"
	@echo "    make seed             — load seed data"
	@echo ""
	@echo "  TESTS"
	@echo "    make test             — backend unit + API tests"
	@echo "    make test-cov         — backend tests with HTML coverage report"
	@echo "    make e2e              — Playwright E2E tests"
	@echo "    make regression       — manifest regression checks"
	@echo "    make emg-schema       — regenerate public EMG v1 JSON Schemas"
	@echo "    make emg-schema-check — fail when checked-in EMG Schemas are stale"
	@echo "    make emg-validate     — validate checked-in .emg.json examples"
	@echo "    make emg-regression   — four-domain EngineeringModelGraph golden gate"
	@echo "    make emg-live-regression — live CAD/STEP/IFC/system matrix in production stack"
	@echo "    make cad-regression   — scan-to-DXF golden regression"
	@echo "    make cad-candidate-gate — fail-closed entity-level model promotion gate"
	@echo "    make cad-drawing-graph-eval — exact EngineeringDrawingGraph → DXF contract benchmark"
	@echo "    make cad-corpus-acquire — лицензированный внешний CAD-корпус"
	@echo "    make cad-corpus-generate — 300 mechanical + 300 construction эталонов"
	@echo "    make cad-pmi-truth      — official NIST PMI semantic truth + fail-closed self-check"
	@echo "    make agent-test       — AiAgent scenario tests"
	@echo "    make studio-queue-smoke — read-only concurrent studio queue API smoke"
	@echo "    make cad-kernel-smoke — live OpenCascade build/projection/incremental-cache checks"
	@echo "    make lint             — ruff + eslint"
	@echo "    make lint-fix         — ruff autofix"
	@echo ""

# ──────────────────────────────────────────────────────────────────────────────
# Development
# ──────────────────────────────────────────────────────────────────────────────
dev:
	docker compose $(COMPOSE_DEV) up

dev-bg:
	docker compose $(COMPOSE_DEV) up -d

dev-build:
	docker compose $(COMPOSE_DEV) up --build

dev-llamacpp:
	docker compose $(COMPOSE_LLAMACPP) up -d llama-server

down:
	docker compose $(COMPOSE_DEV) down

restart:
	docker compose $(COMPOSE_DEV) restart

# ──────────────────────────────────────────────────────────────────────────────
# Production
# ──────────────────────────────────────────────────────────────────────────────
# Plain production start/rebuild must not auto-enable embedded model-server
# profiles persisted in infra/.env. vLLM/llama.cpp are started on demand by
# provider/model activation, or explicitly with --profile.
prod:
	COMPOSE_PROFILES= docker compose $(COMPOSE_PROD) up

prod-bg:
	COMPOSE_PROFILES= docker compose $(COMPOSE_PROD) up -d

prod-build:
	COMPOSE_PROFILES= docker compose $(COMPOSE_PROD) up -d --build

prod-down:
	docker compose $(COMPOSE_PROD) down

# ──────────────────────────────────────────────────────────────────────────────
# Clean / Rebuild
# ──────────────────────────────────────────────────────────────────────────────

# Stop stack, remove locally-built images, wipe build cache
clean:
	docker compose $(COMPOSE_DEV) down --rmi local
	docker builder prune -af

# Full rebuild from scratch: clean → build --no-cache → start dev in background
rebuild:
	docker compose $(COMPOSE_DEV) down --rmi local
	docker builder prune -af
	docker compose $(COMPOSE_DEV) build --no-cache
	docker compose $(COMPOSE_DEV) up -d
	@echo ""
	@echo "  Stack is starting. Check status with: make health"
	@echo "  Tail logs with:                       make logs"

# ⚠️  DESTRUCTIVE — also removes ALL named volumes (database, MinIO, Qdrant data)
nuke:
	@echo "WARNING: This will delete ALL volumes (postgres, minio, qdrant, redis, ...)."
	@echo "Press Ctrl-C to abort, or wait 5 seconds to continue..."
	@sleep 5
	docker compose $(COMPOSE_DEV) down --rmi local -v
	docker builder prune -af

# ──────────────────────────────────────────────────────────────────────────────
# Setup / Ops
# ──────────────────────────────────────────────────────────────────────────────

# First-time setup: create infra/.env from template
setup:
	@if [ ! -f infra/.env ]; then \
		cp infra/.env.example infra/.env; \
		echo "  Created infra/.env from .env.example. Review and adjust before starting."; \
	else \
		echo "  infra/.env already exists — skipping."; \
	fi

# Pretty health table
health:
	@docker compose $(COMPOSE_DEV) ps --format "table {{.Name}}\t{{.Status}}\t{{.Ports}}"

logs:
	docker compose $(COMPOSE_DEV) logs -f

logs-backend:
	docker compose $(COMPOSE_DEV) logs -f backend

logs-celery:
	docker compose $(COMPOSE_DEV) logs -f celery-worker

ps:
	docker compose $(COMPOSE_DEV) ps

shell-backend:
	docker compose $(COMPOSE_DEV) exec backend bash

shell-celery:
	docker compose $(COMPOSE_DEV) exec celery-worker bash

shell-frontend:
	docker compose $(COMPOSE_DEV) exec frontend sh

# ──────────────────────────────────────────────────────────────────────────────
# Database
# ──────────────────────────────────────────────────────────────────────────────
migrate:
	cd backend && alembic upgrade head

migrate-new:
	cd backend && alembic revision --autogenerate -m "$(msg)"

seed:
	cd backend && python3 -m app.scripts.seed_data

# ──────────────────────────────────────────────────────────────────────────────
# Tests
# ──────────────────────────────────────────────────────────────────────────────
test:
	python3 -m pytest backend/tests -m "not live and not llamacpp and not vllm" --tb=short

test-live:  ## Live tests (need Ollama/llama.cpp/vLLM + the running stack)
	python3 -m pytest backend/tests -m "live or llamacpp or vllm" -s --tb=short

test-cov:
	python3 -m pytest backend/tests -m "not live and not llamacpp and not vllm" --cov=backend/app --cov-report=html

e2e:
	cd frontend && npx playwright test

regression:
	python3 scripts/regression_manifest_check.py example-invoices/manifest.json docs/drawing-samples-manifest.json docs/technology-regression-manifest.json
	python3 scripts/agent_role_regression_check.py

emg-schema:
	cd backend && PYTHONPATH=. python3 scripts/export_emg_schemas.py

emg-schema-check:
	cd backend && PYTHONPATH=. python3 scripts/export_emg_schemas.py --check

emg-validate:
	cd backend && PYTHONPATH=. python3 scripts/validate_emg_file.py \
		../examples/emg/minimal-mechanical.emg.json \
		../examples/emg/full-mechanical.emg.json \
		../examples/emg/human-correction.emg-patch.json

emg-regression:
	cd backend && PYTHONPATH=. python3 scripts/eval_emg_domains.py \
		--manifest tests/fixtures/emg_domain_golden.json \
		--out ../test-results/emg_domain_regression.json

emg-live-regression:
	mkdir -p test-results
	docker exec infra-backend-1 python scripts/live_emg_stack_regression.py \
		--out /tmp/emg_live_stack_regression.json
	docker cp infra-backend-1:/tmp/emg_live_stack_regression.json \
		test-results/emg_live_stack_regression.json

cad-kernel-smoke:
	docker exec infra-backend-1 python scripts/cad_kernel_smoke.py

agent-regression:
	python3 scripts/agent_role_regression_check.py

# H1: golden vectorize regression — full production path (arbitrate) over the
# DWG+photo corpus, then gate against the committed baseline (exit 1 on
# recall/quality/DXF-reopen/ЕСКД regression). Runs inside the backend
# container: it has dwg2dxf and reaches the technical-vectorizer service.
cad-regression:
	@run_dir=$$(docker exec infra-backend-1 mktemp -d /tmp/cad-regression.XXXXXX); \
		docker exec infra-backend-1 mkdir -p $$run_dir/input $$run_dir/results; \
		docker cp cleanup_test_files/. infra-backend-1:$$run_dir/input; \
		docker cp test-results/eval_vectorize_baseline.json infra-backend-1:$$run_dir/baseline.json; \
		docker exec infra-backend-1 python scripts/eval_vectorize.py \
			--dir $$run_dir/input --recognizer arbitrate \
			--out $$run_dir/results/eval_vectorize_run.json \
			--check-baseline $$run_dir/baseline.json; \
		status=$$?; \
		docker cp infra-backend-1:$$run_dir/results/eval_vectorize_run.json \
			test-results/eval_vectorize_run.json 2>/dev/null || true; \
		exit $$status

# Promotion is deliberately stricter than ordinary regression: legacy pixel
# coverage cannot pass it, and false "exact" claims are forbidden.
cad-candidate-gate:
	python3 backend/scripts/gate_vectorizer_candidate.py \
		--baseline tools/cad-dataset/baselines/entity_baseline_20260719.json \
		--candidate test-results/eval_vectorize_candidate.json

cad-description-eval:
	PYTHONPATH=backend python3 backend/scripts/eval_cad_descriptions.py \
		--cases tools/cad-dataset/description_cases.json \
		--out test-results/eval_cad_descriptions.json

cad-drawing-graph-eval:
	PYTHONPATH=backend python3 backend/scripts/eval_cad_drawing_graphs.py \
		--cases tools/cad-dataset/drawing_graph_cases.json \
		--out test-results/eval_cad_drawing_graphs.json

cad-corpus-acquire:
	python3 tools/cad-dataset/acquire_open_sources.py \
		--registry tools/cad-dataset/source_registry.json \
		--out cad-dataset-out/open-sources

cad-pmi-truth:
	python3 tools/cad-dataset/build_nist_pmi_truth.py \
		--source-root cad-dataset-out/open-sources/holdout/nist_mbe_pmi/extracted \
		--ir-root cad-dataset-out/nist-pmi-holdout/ir \
		--output cad-dataset-out/nist-pmi-holdout/pmi_truth.jsonl \
		--summary cad-dataset-out/nist-pmi-holdout/pmi_truth_summary.json
	python3 backend/scripts/eval_pmi_manifest.py \
		--reference cad-dataset-out/nist-pmi-holdout/pmi_truth.jsonl \
		--candidate cad-dataset-out/nist-pmi-holdout/pmi_truth.jsonl \
		--output cad-dataset-out/nist-pmi-holdout/pmi_truth_self_eval.json

# --- B5 active-learning flywheel: production accepted edits ---
# Export accepted (image, human-corrected IR) pairs from the prod DB (needs DB
# access -> runs inside infra-backend-1). Empty until the system has usage.
# The image->DSL generative consumer of these pairs was removed (two LoRA runs
# scored entity F1 0.000 on real sheets); the pairs now feed spec-reader
# evaluation instead.
cad-flywheel-export:
	docker exec infra-backend-1 sh -c 'cd /app && python scripts/export_self_learning_pairs.py \
		--out /app/data/self-learning'
	docker cp infra-backend-1:/app/data/self-learning cad-dataset-out/self-learning

cad-web-dxf-corpus:
	python3 tools/cad-dataset/build_dxf_raster_corpus.py \
		--assets cad-dataset-out/open-sources/assets.jsonl \
		--out cad-dataset-out/web-dxf-corpus \
		--train-variants 4 --eval-variants 2 --long-side 2048 --min-long-side 1024

cad-web-dxf-eval:
	python3 backend/scripts/eval_cad_manifest.py \
		--manifest cad-dataset-out/web-dxf-corpus/manifest.jsonl \
		--split holdout --recognizer cv \
		--out test-results/eval_web_dxf_cv.json

cad-corpus-generate:
	python3 tools/cad-dataset/generate_profile_corpus.py \
		--out cad-dataset-out/profile-corpus \
		--count 300 --profiles mechanical construction --variants 1

agent-test:
	cd infra/scripts && python3 run-agent-tests.py

agent-ws-smoke:
	node scripts/check_agent_ws_adapter.js

studio-queue-smoke:
	python3 scripts/studio_queue_load_smoke.py

turboquant-benchmark:
	python3 scripts/turboquant_benchmark.py --baseline-model "$${BASELINE_MODEL}" --turboquant-model "$${TURBOQUANT_MODEL}" --baseline-url "$${BASELINE_URL:-http://localhost:8000}" --turboquant-url "$${TURBOQUANT_URL:-http://localhost:8001}"

turboquant-quality:
	python3 scripts/turboquant_benchmark.py --baseline-model "$${BASELINE_MODEL}" --turboquant-model "$${TURBOQUANT_MODEL}" --baseline-url "$${BASELINE_URL:-http://localhost:8000}" --turboquant-url "$${TURBOQUANT_URL:-http://localhost:8001}" --quality-manifest docs/technology-regression-manifest.json

# ──────────────────────────────────────────────────────────────────────────────
# Lint
# ──────────────────────────────────────────────────────────────────────────────
lint:
	cd backend && ruff check app/ tests/
	cd frontend && npm run lint

lint-fix:
	cd backend && ruff check --fix app/ tests/

# ──────────────────────────────────────────────────────────────────────────────
# Skills / AiAgent
# ──────────────────────────────────────────────────────────────────────────────
skills:
	cd backend && python3 -m app.scripts.generate_skill_registry

aiagent-contract:
	python3 scripts/check_aiagent_contract.py --strict

# ──────────────────────────────────────────────────────────────────────────────
# Monitoring
# ──────────────────────────────────────────────────────────────────────────────
monitoring:
	cd infra && docker compose -f docker-compose.yml -f docker-compose.monitoring.yml up -d prometheus grafana

monitoring-down:
	cd infra && docker compose -f docker-compose.yml -f docker-compose.monitoring.yml down prometheus grafana
