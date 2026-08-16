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

> Status: Phase 2 — identity and organization. On top of the Phase 1 stack,
> Jhin now has email/password auth (Argon2id, server-side sessions, CSRF),
> workspace RBAC (owner/admin/member/viewer), CRUD APIs for workspaces, teams,
> agents, and members with server-side cycle prevention, an append-only audit
> log, and a dark-themed web app: first-run owner setup, login, an
> organization chart with team/agent management, an agent creation wizard,
> and audit/settings pages. Agents do not run yet — execution arrives in
> Phase 3.

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

- Web UI: http://localhost:3000 — on a fresh install you are redirected to
  `/setup` to create the initial owner account and workspace (this first-run
  flow disables itself once any user exists)
- API health: http://localhost:8000/api/v1/health/ready
- Temporal UI (admin, dev overlay only): http://127.0.0.1:8233

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

### Dev seed data

With the dev stack running and migrations applied, `make seed` creates a dev
owner account plus a sample organization (Engineering: CTO → Senior SWE + QA
Engineer; Marketing: Marketing Director → Blogger):

```text
email:    owner@jhin.dev
password: jhin-dev-password
```

Seeding is idempotent and refuses to run if users already exist. These
credentials are for local development only.

### Frontend data fetching

The web app uses TanStack Query on the client with cookie-based sessions and a
CSRF double-submit header. Browser calls go to `/api/*`, which Next.js
rewrites to the API container (`API_INTERNAL_URL`) so cookies stay
same-origin.

## License

Apache-2.0 — see [LICENSE](LICENSE). Copyright 2026 Jhin contributors.
