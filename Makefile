.DEFAULT_GOAL := help
COMPOSE := docker compose
COMPOSE_DEV := docker compose -f compose.yaml -f compose.dev.yaml

.PHONY: help dev test test-unit test-integration lint typecheck migrate seed \
	compose-up compose-down sample-workflow master-key

MASTER_KEY_PATH := secrets/dev/jhin_master_key

help: ## List available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

master-key: ## Generate the local dev master key file (required once before `make dev`)
	@test -f $(MASTER_KEY_PATH) && echo "master key already exists at $(MASTER_KEY_PATH)" \
		|| uv run python scripts/generate_master_key.py $(MASTER_KEY_PATH)

dev: master-key ## Boot the full stack with dev overrides (hot reload, localhost infra ports)
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

seed: ## Seed dev data: owner account + Engineering/Marketing sample org
	$(COMPOSE) run --rm --no-deps api jhin-seed-dev

compose-up: master-key ## Start the production-shaped stack
	$(COMPOSE) up -d --build

compose-down: ## Stop the stack (volumes preserved)
	$(COMPOSE_DEV) down --remove-orphans

sample-workflow: ## Start the sample durable Temporal workflow (requires dev stack)
	uv run python scripts/start_sample_workflow.py
