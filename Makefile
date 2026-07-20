.PHONY: help \
        dev dev-build dev-bg dev-llamacpp down restart \
        prod prod-bg prod-build prod-down \
        clean rebuild nuke \
        setup health logs ps shell-backend shell-celery shell-frontend \
        migrate migrate-new seed \
        test test-cov e2e regression agent-regression agent-test agent-ws-smoke \
        studio-queue-smoke cad-regression cad-candidate-gate \
        cad-corpus-acquire cad-corpus-generate cad-corpus-tile cad-corpus-pack \
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
	@echo "    make cad-regression   — scan-to-DXF golden regression"
	@echo "    make cad-candidate-gate — fail-closed entity-level model promotion gate"
	@echo "    make cad-corpus-acquire — лицензированный внешний CAD-корпус"
	@echo "    make cad-corpus-generate — 300 mechanical + 300 construction эталонов"
	@echo "    make cad-corpus-tile — exact local tiles без source-group leakage"
	@echo "    make cad-corpus-pack — train/val + real-only holdout manifests"
	@echo "    make agent-test       — AiAgent scenario tests"
	@echo "    make studio-queue-smoke — read-only concurrent studio queue API smoke"
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

agent-regression:
	python3 scripts/agent_role_regression_check.py

# H1: golden vectorize regression — full production path (arbitrate) over the
# DWG+photo corpus, then gate against the committed baseline (exit 1 on
# recall/quality/DXF-reopen/ЕСКД regression). Runs inside the backend
# container: it has dwg2dxf and reaches the technical-vectorizer service.
cad-regression:
	docker exec infra-backend-1 sh -c 'cd /app && python scripts/eval_vectorize.py \
		--dir cleanup_test_files --recognizer arbitrate \
		--out test-results/eval_vectorize_run.json \
		--check-baseline test-results/eval_vectorize_baseline.json'

# Promotion is deliberately stricter than ordinary regression: legacy pixel
# coverage cannot pass it, and false "exact" claims are forbidden.
cad-candidate-gate:
	python3 backend/scripts/gate_vectorizer_candidate.py \
		--baseline tools/cad-dataset/baselines/entity_baseline_20260719.json \
		--candidate test-results/eval_vectorize_candidate.json

cad-primitive-train:
	cad-dataset-out/venv/bin/python infra/cad-vectorizer/train_primitives.py \
		--data cad-dataset-out/profile-tiles-packed \
		--out cad-dataset-out/primitive-set-checkpoints \
		--epochs 10 --batch-size 32 --num-workers 0

cad-web-primitive-train:
	cad-dataset-out/venv/bin/python infra/cad-vectorizer/train_primitives.py \
		--data cad-dataset-out/web-step-packed \
		--out cad-dataset-out/web-step-primitive-checkpoints \
		--resume cad-dataset-out/primitive-set-checkpoints/best.pt \
		--dataset-kind freecad_library_step_projections_cc_by_3 \
		--epochs 10 --batch-size 8 --lr 5e-5 --eval-every 200 --num-workers 0

cad-web-sheet-layout-build:
	python3 tools/cad-dataset/build_sheet_layout_corpus.py \
		--source cad-dataset-out/web-step-corpus \
		--out cad-dataset-out/web-sheet-layout \
		--train-variants 9 --eval-variants 3

cad-web-sheet-layout-train:
	cad-dataset-out/venv/bin/python infra/cad-vectorizer/train_sheet_layout.py \
		--data cad-dataset-out/web-sheet-layout \
		--out cad-dataset-out/web-sheet-layout-checkpoints-v2 \
		--epochs 15 --batch-size 24 --lr 2e-4 --eval-every 100 --num-workers 0

cad-web-evidence-train:
	cad-dataset-out/venv/bin/python infra/cad-vectorizer/train_evidence.py \
		--data cad-dataset-out/web-step-packed \
		--out cad-dataset-out/web-evidence-checkpoints \
		--epochs 15 --batch-size 12 --lr 2e-4 --eval-every 200 --num-workers 0

cad-web-directional-train:
	cad-dataset-out/venv/bin/python infra/cad-vectorizer/train_directional.py \
		--data cad-dataset-out/web-step-packed \
		--out cad-dataset-out/web-directional-checkpoints \
		--warm-start cad-dataset-out/web-evidence-checkpoints/best.pt \
		--epochs 18 --batch-size 10 --lr 2e-4 --eval-every 200 --num-workers 0

cad-web-graph-train:
	cad-dataset-out/venv/bin/python infra/cad-vectorizer/train_graph.py \
		--data cad-dataset-out/web-step-packed \
		--out cad-dataset-out/web-graph-checkpoints-v1-1 \
		--warm-start cad-dataset-out/web-step-primitive-checkpoints/best.pt \
		--epochs 20 --batch-size 8 --lr 1e-4 --eval-every 250 --num-workers 0

cad-web-edge-verifier-train:
	cad-dataset-out/venv/bin/python infra/cad-vectorizer/train_edge_verifier.py \
		--data cad-dataset-out/web-step-packed \
		--directional-checkpoint cad-dataset-out/web-directional-checkpoints/best.pt \
		--out cad-dataset-out/web-edge-verifier-checkpoints \
		--epochs 30 --batch-size 512 --lr 3e-4

cad-corpus-acquire:
	python3 tools/cad-dataset/acquire_open_sources.py \
		--registry tools/cad-dataset/source_registry.json \
		--out cad-dataset-out/open-sources

# Generative-vectorization stage 1: reshape (image, CadIR) corpus pairs into
# Qwen3-VL SFT records (image -> isotropic 0..1000 primitive DSL).
cad-vlm-sft:
	python3 tools/cad-dataset/build_vlm_sft.py \
		--manifest cad-dataset-out/web-dxf-corpus-floor/manifest.jsonl \
		--manifest cad-dataset-out/profile-corpus/manifest.jsonl \
		--manifest cad-dataset-out/web-step-corpus/manifest.jsonl \
		--out cad-dataset-out/vlm-sft --backend backend

# Stage 2: LoRA fine-tune Qwen3-VL on the SFT set. Needs the GPU free — stop
# the production qwen3-vl:32b in ollama first (see infra/vlm-finetune/README).
cad-vlm-train-image:
	docker build -t vlm-finetune infra/vlm-finetune

cad-vlm-train:
	cp infra/vlm-finetune/dataset_info.json infra/vlm-finetune/qwen3vl_lora_sft.yaml cad-dataset-out/vlm-sft/
	docker run --rm --gpus all \
		-v $(CURDIR)/cad-dataset-out:$(CURDIR)/cad-dataset-out \
		-v $(CURDIR)/cad-dataset-out/vlm-sft:/data \
		-v $(CURDIR)/cad-dataset-out/vlm-sft/out:/out \
		-v $(HOME)/.cache/huggingface:/root/.cache/huggingface \
		vlm-finetune

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

cad-web-step-project:
	python3 tools/cad-dataset/project_step_corpus.py \
		--source cad-dataset-out/open-sources/step/freecad_parts_library \
		--out cad-dataset-out/step-projections \
		--image infra-cad-kernel

cad-web-step-build:
	python3 tools/cad-dataset/build_web_step_corpus.py \
		--assets cad-dataset-out/open-sources/assets.jsonl \
		--projections cad-dataset-out/step-projections \
		--out cad-dataset-out/web-step-corpus

cad-web-step-tile:
	python3 tools/cad-dataset/tile_ir_dataset.py \
		--source cad-dataset-out/web-step-corpus \
		--out cad-dataset-out/web-step-tiles-q90 \
		--tile-size 640 --overlap 160 --max-commands 90

cad-web-step-pack:
	python3 tools/cad-dataset/build_dataset.py \
		--synth cad-dataset-out/web-step-tiles-q90 \
		--holdout cad-dataset-out/holdout \
		--out cad-dataset-out/web-step-packed \
		--split-manifest cad-dataset-out/web-step-tiles-q90/manifest.jsonl \
		--image-source clean

cad-corpus-generate:
	python3 tools/cad-dataset/generate_profile_corpus.py \
		--out cad-dataset-out/profile-corpus \
		--count 300 --profiles mechanical construction --variants 1

cad-corpus-tile:
	python3 tools/cad-dataset/tile_ir_dataset.py \
		--source cad-dataset-out/profile-corpus \
		--out cad-dataset-out/profile-tiles \
		--tile-size 640 --overlap 160 --max-commands 180

cad-corpus-pack:
	python3 tools/cad-dataset/build_dataset.py \
		--synth cad-dataset-out/profile-tiles \
		--holdout cad-dataset-out/holdout \
		--out cad-dataset-out/profile-tiles-packed \
		--split-manifest cad-dataset-out/profile-tiles/manifest.jsonl

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
