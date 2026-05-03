.PHONY: dev dev-build down test e2e regression agent-regression agent-test agent-ws-smoke aiagent-contract aiagent-strict aiagent-official-up aiagent-official-down aiagent-official-logs aiagent-official-dashboard turboquant-benchmark turboquant-quality lint migrate seed skills logs ps

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

aiagent-contract:
	python3 scripts/check_aiagent_contract.py

aiagent-strict:
	python3 scripts/generate_aiagent_strict_gateway.py
	python3 scripts/generate_aiagent_official_sample.py
	python3 scripts/check_aiagent_contract.py --gateway aiagent/config/gateway.strict.yml --strict

aiagent-official-up: aiagent-strict
	docker compose -f infra/docker-compose.yml -f infra/docker-compose.dev.yml -f infra/docker-compose.aiagent.yml up -d aiagent-gateway

aiagent-official-down:
	docker compose -f infra/docker-compose.yml -f infra/docker-compose.dev.yml -f infra/docker-compose.aiagent.yml stop aiagent-gateway aiagent-cli

aiagent-official-logs:
	docker compose -f infra/docker-compose.yml -f infra/docker-compose.dev.yml -f infra/docker-compose.aiagent.yml logs -f aiagent-gateway

aiagent-official-dashboard:
	docker compose -f infra/docker-compose.yml -f infra/docker-compose.dev.yml -f infra/docker-compose.aiagent.yml run --rm aiagent-cli dashboard --no-open

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
