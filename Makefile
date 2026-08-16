.DEFAULT_GOAL := help
COMPOSE := docker compose
COMPOSE_DEV := docker compose -f compose.yaml -f compose.dev.yaml

.PHONY: help dev test test-unit test-integration lint typecheck migrate seed \
	compose-up compose-down sample-workflow

help: ## List available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

dev: ## Boot the full stack with dev overrides (hot reload, localhost infra ports)
	$(COMPOSE_DEV) up -d --build

test: test-unit ## Alias for test-unit

test-unit: ## Run Python unit tests and frontend Vitest
	uv run pytest
	pnpm --filter jhin-web test

test-integration: ## Run integration tests against the running compose stack
	uv run pytest -m integration tests/integration -v

lint: ## Ruff + eslint
	uv run ruff check .
	uv run ruff format --check .
	pnpm --filter jhin-web lint

typecheck: ## mypy + tsc
	uv run mypy
	pnpm --filter jhin-web typecheck

migrate: ## Run Alembic migrations inside the compose network
	$(COMPOSE) run --rm --no-deps api jhin-db-migrate

seed: ## Seed development data (stub — arrives with the domain phases)
	@echo "seed: no seedable domain entities exist yet (Phase 1); this becomes real in Phase 2."

compose-up: ## Start the production-shaped stack
	$(COMPOSE) up -d --build

compose-down: ## Stop the stack (volumes preserved)
	$(COMPOSE_DEV) down --remove-orphans

sample-workflow: ## Start the sample durable Temporal workflow (requires dev stack)
	uv run python scripts/start_sample_workflow.py
