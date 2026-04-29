# Repository Guidelines

## Project Structure & Module Organization
This repository is currently a planning workspace. The source of truth is in [`CLAUDE.md`](./CLAUDE.md), [`PLAN.md`](./PLAN.md), and [`DEVPLAN.md`](./DEVPLAN.md). Contributors should keep product, architecture, and delivery changes aligned across those files.

The target implementation structure is:
- `backend/app/` for FastAPI code (`api/`, `domain/`, `tasks/`, `ai/`, `db/`)
- `frontend/app/` for Next.js routes and pages
- `frontend/components/` for shared React UI
- `openclaw/` for prompts, skills, and scenarios
- `infra/` for `docker-compose`, Traefik, and deployment scripts

## Build, Test, and Development Commands
Planned local workflow:
- `make dev` starts the full stack
- `make test` runs unit, API, and integration tests
- `make e2e` runs Playwright end-to-end coverage
- `make regression` checks extraction quality
- `make agent-test` validates OpenClaw scenarios against mock skills

If you add real code, keep command examples in this file and the planning docs synchronized.

## Coding Style & Naming Conventions
Write documentation and contributor discussion in Russian. Keep code, identifiers, and code comments in English. Use clear module boundaries: OpenClaw for planning/orchestration, FastAPI for data and async work, Next.js for UI.

Prefer:
- `snake_case` for Python modules and functions
- `PascalCase` for React components and Pydantic models
- feature-focused filenames such as `style_matching.py` or `supplier_profile.tsx`

Add formatters and linters with the implementation. `DEVPLAN.md` already expects pre-commit hooks.

## Testing Guidelines
The planned stack is `pytest` for backend logic and API tests, plus Playwright for keyboard-first UI flows. Place backend tests under `backend/tests/` and frontend E2E specs under `frontend/tests/e2e/`.

Name tests after the behavior under test, for example `test_invoice_duplicate_detection.py` or `inbox-review-streak.spec.ts`.

## Commit & Pull Request Guidelines
No local `.git` history is present in this workspace, so commit conventions cannot be inferred from prior commits. Until history exists, use short imperative subjects such as `Add invoice anomaly schema` or `Document OpenClaw skill registry flow`.

Pull requests should include:
- a concise problem/solution summary
- links to the relevant plan section or issue
- screenshots or terminal output for UI, API, or workflow changes
- notes on new commands, env vars, or approval gates

## Security & Configuration Tips
Follow the documented rule: confidential OCR and extraction stay local. Do not commit secrets, production credentials, or real customer documents. Treat approval gates and auditability as non-optional design constraints.
