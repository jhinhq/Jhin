.DEFAULT_GOAL := help

PYTEST := uv run pytest
PHASE10_HARNESS := uv run python -m tests.integration.phase10_upgrade_harness
# rootful | rootless (Linux servers) | desktop (Docker Desktop on macOS/Windows,
# local development only).
PHASE10_MODE ?= rootful
SANDBOX_DOCKER_SOCKET_HOST ?= /var/run/docker.sock

.PHONY: help dev test test-unit test-integration test-e2e lint typecheck migrate seed \
	compose-up compose-down sample-workflow master-key sandbox-image \
	test-tool-worker-boundary test-tool-worker-boundary-integration \
	test-phase10-regressions test-phase10-extended test-tool-worker-live-upgrade \
	test-sandbox-socket-rootful test-sandbox-socket-rootless \
	test-sandbox-socket-desktop test-sandbox-socket-wrong-gid

help: ## List available targets
	@grep -E '^[a-zA-Z0-9_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-38s\033[0m %s\n", $$1, $$2}'

dev: compose-up ## Boot one isolated dev stack through the Phase 10 authority

master-key: ## Explain master-key ownership for the isolated stack
	@echo "The Phase 10 lifecycle creates, installs, and removes its own nonprinting master key."

sandbox-image: ## Rebuild the sandbox job image through the leased Docker authority
	$(PHASE10_HARNESS) compose -- --profile build build sandbox-image

test: test-unit ## Alias for test-unit

test-unit: ## Run Python unit tests and frontend Vitest
	$(PYTEST)
	pnpm --filter jhin-web test

test-integration: test-phase10-regressions test-phase10-extended ## Run the frozen live regression set and the extended live files in isolation

test-e2e: ## Run the Playwright chat browser specs against a running dev stack (see apps/web/e2e/README.md)
	@node apps/web/e2e/tools/check-browsers.mjs
	pnpm --filter jhin-web test:e2e

test-tool-worker-boundary: ## Run focused Phase 10 unit, replay, dependency, and render gates
	$(PYTEST) \
		packages/workflows/tests/test_agent_task_tool_routing.py \
		packages/workflows/tests/test_tool_compat_workflows.py \
		packages/workflows/tests/test_phase10_history_replay.py \
		services/agent_worker/tests/test_reasoning_manifest.py \
		services/agent_worker/tests/test_legacy_manifest_sidecar.py \
		services/agent_worker/tests/test_step_projection.py \
		services/agent_worker/tests/test_compatibility_coordinators.py \
		services/tool_worker/tests \
		services/sandbox_runner/tests \
		tests/test_worker_dependency_boundaries.py \
		tests/test_executable_catalog_boundary.py \
		tests/test_phase10_tool_worker_compose.py \
		tests/test_phase9_production_compose.py \
		tests/integration/test_phase10_tool_worker_boundary.py \
		tests/integration/test_phase10_sandbox_socket_modes.py \
		-q

test-tool-worker-boundary-integration: ## Run the exact live boundary and crash matrix
	$(PHASE10_HARNESS) run --mode $(PHASE10_MODE) --scenario boundary

test-phase10-regressions: ## Run the exact Phase 3/6/7/9 live regression files
	$(PHASE10_HARNESS) run --mode $(PHASE10_MODE) --scenario regressions

test-phase10-extended: ## Run the remaining live exit, durability, seed, and health files
	$(PHASE10_HARNESS) run --mode $(PHASE10_MODE) --scenario extended

test-tool-worker-live-upgrade: ## Run the frozen Phase 9 to current in-flight upgrade
	$(PHASE10_HARNESS) run --mode $(PHASE10_MODE) --scenario upgrade

test-sandbox-socket-rootful: ## Verify the selected rootful socket and live sandbox path
	test -n "$(SANDBOX_DOCKER_GID)"
	$(PHASE10_HARNESS) run --mode rootful --scenario socket-rootful

test-sandbox-socket-rootless: ## Verify an existing host-UID-10001 rootless daemon
	test -n "$(PHASE10_ROOTLESS_DOCKER_SOCKET)"
	test -S "$(PHASE10_ROOTLESS_DOCKER_SOCKET)"
	test "$$(stat -c %u "$(PHASE10_ROOTLESS_DOCKER_SOCKET)")" = "10001"
	env -u SANDBOX_DOCKER_GID \
		PHASE10_ROOTLESS_DOCKER_SOCKET="$(PHASE10_ROOTLESS_DOCKER_SOCKET)" \
		$(PHASE10_HARNESS) run --mode rootless --scenario socket-rootless

test-sandbox-socket-desktop: ## Verify the Docker Desktop (macOS/Windows, dev-only) socket and live sandbox path
	env -u SANDBOX_DOCKER_GID $(PHASE10_HARNESS) run --mode desktop --scenario socket-desktop

test-sandbox-socket-wrong-gid: ## Prove rootful runner startup fails closed on a false GID
	test -n "$(SANDBOX_DOCKER_GID)"
	$(PHASE10_HARNESS) run --mode rootful --scenario wrong-gid

lint: ## Ruff + eslint
	uv run ruff check .
	uv run ruff format --check .
	pnpm --filter jhin-web lint

typecheck: ## mypy + tsc
	uv run mypy
	pnpm --filter jhin-web typecheck

migrate: ## Run Alembic through the leased isolated Compose authority
	$(PHASE10_HARNESS) compose -- run --rm --no-deps api jhin-db-migrate

seed: ## Seed dev data through the leased isolated Compose authority
	$(PHASE10_HARNESS) compose -- run --rm --no-deps api jhin-seed-dev

compose-up: ## Start one persistent isolated production-shaped stack
	$(PHASE10_HARNESS) up --mode $(PHASE10_MODE)

compose-down: ## Stop and exhaustively clean the leased isolated stack
	$(PHASE10_HARNESS) down

sample-workflow: ## Start the sample durable Temporal workflow (requires compose-up)
	uv run python scripts/start_sample_workflow.py
