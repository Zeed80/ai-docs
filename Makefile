.PHONY: help \
        dev dev-build dev-bg dev-llamacpp down restart \
        prod prod-bg prod-build prod-down \
        clean rebuild nuke \
        setup health logs ps shell-backend shell-celery shell-frontend \
        migrate migrate-new seed \
        test test-cov e2e regression agent-regression agent-test agent-ws-smoke \
        studio-queue-smoke \
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
prod:
	docker compose $(COMPOSE_PROD) up

prod-bg:
	docker compose $(COMPOSE_PROD) up -d

prod-build:
	docker compose $(COMPOSE_PROD) up -d --build

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
