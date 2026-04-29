.PHONY: dev dev-build down test e2e regression agent-regression agent-test agent-ws-smoke openclaw-contract openclaw-strict openclaw-official-up openclaw-official-down openclaw-official-logs openclaw-official-dashboard turboquant-benchmark turboquant-quality lint migrate seed skills logs ps

# === Development ===
dev:
	docker compose -f infra/docker-compose.yml -f infra/docker-compose.dev.yml up

dev-build:
	docker compose -f infra/docker-compose.yml -f infra/docker-compose.dev.yml up --build

down:
	docker compose -f infra/docker-compose.yml -f infra/docker-compose.dev.yml down

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

openclaw-contract:
	python3 scripts/check_openclaw_contract.py

openclaw-strict:
	python3 scripts/generate_openclaw_strict_gateway.py
	python3 scripts/generate_openclaw_official_sample.py
	python3 scripts/check_openclaw_contract.py --gateway openclaw/config/gateway.strict.yml --strict

openclaw-official-up: openclaw-strict
	docker compose -f infra/docker-compose.yml -f infra/docker-compose.dev.yml -f infra/docker-compose.openclaw.yml up -d openclaw-gateway

openclaw-official-down:
	docker compose -f infra/docker-compose.yml -f infra/docker-compose.dev.yml -f infra/docker-compose.openclaw.yml stop openclaw-gateway openclaw-cli

openclaw-official-logs:
	docker compose -f infra/docker-compose.yml -f infra/docker-compose.dev.yml -f infra/docker-compose.openclaw.yml logs -f openclaw-gateway

openclaw-official-dashboard:
	docker compose -f infra/docker-compose.yml -f infra/docker-compose.dev.yml -f infra/docker-compose.openclaw.yml run --rm openclaw-cli dashboard --no-open

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

# === Docker Ops ===
logs:
	docker compose -f infra/docker-compose.yml -f infra/docker-compose.dev.yml logs -f

ps:
	docker compose -f infra/docker-compose.yml -f infra/docker-compose.dev.yml ps
