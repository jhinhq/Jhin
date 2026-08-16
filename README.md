# Jhin

Jhin is a self-hosted, open-source platform for creating hierarchical teams of
autonomous AI agents that can securely use external systems, react to triggers,
delegate work, execute long-running workflows, and expose all activity through
a polished web application.

**Architecture:** [Temporal](https://temporal.io) is the durable workflow
authority, [NATS JetStream](https://nats.io) is the asynchronous event
backbone, and PostgreSQL is the system of record. A FastAPI control-plane API
owns configuration and authorization, and a Next.js frontend provides the
operations UI.

> Status: Phase 1 — foundation and full local stack. The platform boots via
> Docker Compose with the API, web shell, Postgres, NATS, Temporal, and both
> workers healthy. Product features arrive in subsequent phases.

## Quick start

Requirements: Docker with Compose v2.

```bash
git clone <this repo>
cd jhin
cp .env.example .env
docker compose up -d --build
make migrate
```

Then open:

- Web UI (stack status): http://localhost:3000
- API health: http://localhost:8000/api/v1/health/ready
- Temporal UI (admin, localhost-only): http://127.0.0.1:8080

Internal infrastructure ports (Postgres, NATS, Temporal) are **not** published
publicly. The dev overlay (`compose.dev.yaml`) binds them to `127.0.0.1` only.

## Repository layout

```text
apps/
  api/          FastAPI control plane (jhin_api)
  web/          Next.js web UI
services/
  workflow_worker/   Temporal worker (sample durable workflow)
  event_worker/      NATS JetStream durable consumer
packages/
  db/           SQLAlchemy 2 + Alembic (jhin_db)
  events/       Event envelope, subjects, JetStream helpers (jhin_events)
  workflows/    Temporal workflow definitions (jhin_workflows)
  observability/  Structured JSON logging (jhin_observability)
tests/integration/  Compose-stack integration tests
```

## Development

Python tooling uses [uv](https://docs.astral.sh/uv/) with a workspace;
frontend tooling uses pnpm.

```bash
uv sync --all-packages   # install Python workspace
pnpm install             # install frontend workspace

make dev                 # full stack with dev overrides (hot reload, local ports)
make lint                # ruff + eslint
make typecheck           # mypy + tsc
make test-unit           # pytest (unit) + vitest
make test-integration    # pytest against the running compose stack
make sample-workflow     # start the sample durable workflow
make compose-down        # stop the stack
```

The display name is configurable via `APP_NAME` (defaults to `Jhin`).

## License

Apache-2.0 — see [LICENSE](LICENSE). Copyright 2026 Jhin contributors.
