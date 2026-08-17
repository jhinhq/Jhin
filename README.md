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

> Status: Phase 4 — agents call tools, safely. On top of durable agent runs
> (Phase 3), Jhin now has a capability registry with built-in demo tools at
> every risk level, deny-by-default capability grants (explicit deny beats
> allow, scoped), per-agent approval policies (Autonomous/Balanced/Restricted
> presets persisted as explicit rules), and a single tool gateway that
> validates, authorizes, executes, sanitizes, and audits every call — model
> output is never authorization. Approval-gated calls park the run durably in
> Temporal (surviving worker restarts) until a human approves or rejects from
> the new Approvals inbox. Model adapters speak OpenAI-style function calling,
> and the web app gains the Tools & Access agent tab, real wizard steps for
> tools and autonomy, an approvals inbox with a live pending badge, and
> tool/approval events on the task timeline. Real connectors arrive in
> Phase 5.

## Quick start

Requirements: Docker with Compose v2.

```bash
git clone <this repo>
cd jhin
cp .env.example .env
make master-key          # one-time: generate the secret-store master key
docker compose up -d --build
make migrate
```

The master key file (`secrets/dev/jhin_master_key` by default) encrypts every
stored credential. Back it up; losing it makes stored secrets unreadable.

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
  workflow_worker/   Temporal worker (general workflows)
  agent_worker/      Temporal worker executing agent runs (model calls live here)
  event_worker/      NATS JetStream durable consumer
packages/
  db/           SQLAlchemy 2 + Alembic (jhin_db)
  domain/       Shared enums + UUIDv7 helper (jhin_domain)
  events/       Event envelope, subjects, JetStream helpers (jhin_events)
  workflows/    Temporal workflow definitions (jhin_workflows)
  secrets/      Envelope encryption, secret store, log redaction (jhin_secrets)
  models/       Model provider adapters + fake test provider (jhin_models)
  agents/       Execution snapshots, prompt layers, step runtime (jhin_agents)
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

The seed also creates a "Fake Provider (dev)" model provider pointing at the
in-stack fake OpenAI-compatible server (`fake-provider`, dev overlay only)
with two priced profiles — `Fake Mini` (the workspace default) and
`Fake Pro` — so agents can run tasks immediately without real API keys.

### Models and tasks

- **Models page** — add providers (API keys go into the encrypted secret
  store and are never displayed again), verify them with a live call, create
  priced model profiles, and pick the workspace default profile. Agents use
  the workspace default unless a custom profile is set (wizard step 4 or the
  agent drawer's Model tab).
- **Tasks page** — create a task and optionally assign it to an agent;
  assignment starts a durable `AgentTaskWorkflow` (workflow id `task-<uuid>`)
  on the agent worker. The task detail view shows the execution timeline,
  the conversation (you can send instructions mid-run), and token/cost
  totals. Pause, resume, and cancel map to Temporal signals.
- **Message an agent** — the Message action on any active agent starts a
  conversational task; the agent's reply lands in the task conversation.
- **Runs page** — every run with status, tokens, estimated cost, and a link
  back to its task. Costs come from the profile's per-million-token pricing
  (stored as integer micro-dollars).

### Frontend data fetching

The web app uses TanStack Query on the client with cookie-based sessions and a
CSRF double-submit header. Browser calls go to `/api/*`, which Next.js
rewrites to the API container (`API_INTERNAL_URL`) so cookies stay
same-origin.

## License

Apache-2.0 — see [LICENSE](LICENSE). Copyright 2026 Jhin contributors.
