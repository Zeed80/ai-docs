.PHONY: dev dev-build down prod prod-build prod-down test e2e regression agent-regression agent-test agent-ws-smoke turboquant-benchmark turboquant-quality lint migrate seed skills logs ps

# Docker Compose file sets
COMPOSE_DEV  := -f infra/docker-compose.yml -f infra/docker-compose.dev.yml
COMPOSE_PROD := -f infra/docker-compose.yml --profile prod

# === Development (default — code is hot-reloaded via volume mounts) ===
dev:
	docker compose $(COMPOSE_DEV) up

dev-build:
	docker compose $(COMPOSE_DEV) up --build

down:
	docker compose $(COMPOSE_DEV) down

# === Production (explicit — builds images, no volume mounts, starts Traefik+Authentik) ===
prod:
	docker compose $(COMPOSE_PROD) up

prod-build:
	docker compose $(COMPOSE_PROD) up --build

prod-down:
	docker compose $(COMPOSE_PROD) down

# === Backend ===
migrate:
	cd backend && alembic upgrade head

migrate-new:
	cd backend && alembic revision --autogenerate -m "$(msg)"

seed:
	cd backend && python3 -m app.scripts.seed_data

# === Tests ===
test:
	python3 -m pytest backend/tests -v --tb=short

test-cov:
	python3 -m pytest backend/tests -v --cov=backend/app --cov-report=html

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

turboquant-benchmark:
	python3 scripts/turboquant_benchmark.py --baseline-model "$${BASELINE_MODEL}" --turboquant-model "$${TURBOQUANT_MODEL}" --baseline-url "$${BASELINE_URL:-http://localhost:8000}" --turboquant-url "$${TURBOQUANT_URL:-http://localhost:8001}"

turboquant-quality:
	python3 scripts/turboquant_benchmark.py --baseline-model "$${BASELINE_MODEL}" --turboquant-model "$${TURBOQUANT_MODEL}" --baseline-url "$${BASELINE_URL:-http://localhost:8000}" --turboquant-url "$${TURBOQUANT_URL:-http://localhost:8001}" --quality-manifest docs/technology-regression-manifest.json

# === Lint ===
lint:
	cd backend && ruff check app/ tests/
	cd frontend && npm run lint

lint-fix:
	cd backend && ruff check --fix app/ tests/

# === Skills ===
skills:
	cd backend && python3 -m app.scripts.generate_skill_registry

aiagent-contract:
	python3 scripts/check_aiagent_contract.py --strict

aiagent-strict:
	python3 scripts/generate_aiagent_strict_gateway.py
	python3 scripts/generate_aiagent_official_sample.py

# === Docker Ops ===
logs:
	docker compose $(COMPOSE_DEV) logs -f

ps:
	docker compose $(COMPOSE_DEV) ps
