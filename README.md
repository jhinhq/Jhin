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

> Status: Phase 6 — coding agents work in ephemeral repository sandboxes. A
> new internal-only sandbox runner service executes every `cli.*` tool call
> in a fresh locked-down Docker container (non-root uid 1000, read-only
> rootfs, cap_drop ALL, no-new-privileges, cpu/memory/pids/timeout caps,
> network `none` or a dedicated bridge — never the compose control/data
> networks, and never the Docker socket). The CLI connector adds five tools
> (command execute, repository checkout, test run, file read/write) with
> fnmatch scope enforcement over command/image/network/repository/path; a
> per-run workspace volume carries a checkout across calls and dies with the
> run. Short-lived git credentials are injected as job-scoped secret env
> (askpass helper — never in the remote URL) and redacted from all persisted
> output. `sandbox_job` rows, `sandbox.job.*` audit events, and collapsible
> job output in the task timeline make every job attributable. The fake
> GitHub service now serves git smart-HTTP so clone/push/PR flows run and
> test with zero real credentials. See docs/architecture/sandboxing.md
> (including the Docker-socket trust boundary). Triggers that react to
> connector events arrive in Phase 7.

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
  sandbox_runner/    Internal-only API executing cli.* jobs in ephemeral containers
packages/
  db/           SQLAlchemy 2 + Alembic (jhin_db)
  domain/       Shared enums + UUIDv7 helper (jhin_domain)
  events/       Event envelope, subjects, JetStream helpers (jhin_events)
  workflows/    Temporal workflow definitions (jhin_workflows)
  secrets/      Envelope encryption, secret store, log redaction (jhin_secrets)
  models/       Model provider adapters + fake test provider (jhin_models)
  agents/       Execution snapshots, prompt layers, step runtime (jhin_agents)
  policy/       Capability registry, tool definitions, policy evaluator (jhin_policy)
  tools/        Tool gateway: authorize, execute, sanitize, audit (jhin_tools)
  connectors/   Connector SDK + GitHub/CLI connectors + fake GitHub/git (jhin_connectors)
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

### Connectors

The **Connectors page** connects Jhin to external systems (GitHub is live;
more arrive in later phases). Admins create a connection by picking an auth
method (GitHub: personal access token or GitHub App), entering credentials
(stored in the encrypted secret store, never displayed again), and optional
config such as `base_url` for GitHub Enterprise. The create response shows
the webhook payload URL and signing secret **once** — paste them into the
provider's webhook settings. Connection details offer live verify, credential
rotation, enable/disable, delete, and recent tool usage.

Agents get connector tools through grants (Tools & Access tab or wizard
step 5), optionally scoped to one connection and repository/branch glob
patterns like `octo/*` or `agent/*`. See
[docs/architecture/connectors.md](docs/architecture/connectors.md) for the
SDK and how to contribute a connector. In the dev overlay a `fake-github`
service lets the whole GitHub flow run without real credentials
(point a connection's `base_url` at `http://fake-github:8080`).

### Frontend data fetching

The web app uses TanStack Query on the client with cookie-based sessions and a
CSRF double-submit header. Browser calls go to `/api/*`, which Next.js
rewrites to the API container (`API_INTERNAL_URL`) so cookies stay
same-origin.

## License

Apache-2.0 — see [LICENSE](LICENSE). Copyright 2026 Jhin contributors.
