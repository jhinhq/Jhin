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

> Status: Phase 7 — the flagship vertical slice is live: a Linear issue
> moving into **Todo** automatically starts exactly one SWE task whose agent
> checks the repository out in an ephemeral sandbox, fixes the failing test,
> pushes an agent branch, opens a GitHub pull request, and comments the
> outcome back on the issue. A new Linear connector (GraphQL, API-key auth,
> HMAC-verified webhooks) normalizes issue/comment events — preserving
> Linear's `updatedFrom` as a `changed_from` mirror so state *transitions*
> are detectable. A connector-agnostic trigger engine (`jhin_triggers`)
> evaluates a safe JSON filter DSL (all/any groups; eq/neq/in/not_in/
> contains/exists/gt/gte/lt/lte plus first-class `transitioned_to`) against
> canonical events in the event worker, and duplicates never duplicate work:
> webhook delivery dedupe, a deterministic trigger idempotency key
> (unique-indexed `trigger_invocation` rows), and Temporal's duplicate-start
> policy on deterministic workflow ids. The Triggers page offers a
> WHEN/IF/THEN builder with per-condition dry-run testing, and task details
> show trigger origin. See docs/architecture/events.md for the full flow.

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
  triggers/     Trigger filter DSL, transition matching, idempotency keys (jhin_triggers)
  connectors/   Connector SDK + GitHub/CLI/Linear connectors + fake GitHub/Linear (jhin_connectors)
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

When the master key is available the seed additionally wires the Phase 7
showcase: a fake Linear connection, `linear.*` read/search/metadata/comment
grants for the Senior Software Engineer, and the enabled trigger **"Pick up
new engineering tickets"** (team ENG + state transitions to Todo → assign to
the SWE, comment the outcome back on the issue).

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

The **Connectors page** connects Jhin to external systems (GitHub, Linear,
and the CLI sandbox are live). Admins create a connection by picking an auth
method (GitHub: personal access token or GitHub App; Linear: API key, with
OAuth noted for later), entering credentials
(stored in the encrypted secret store, never displayed again), and optional
config such as `base_url` for GitHub Enterprise. The create response shows
the webhook payload URL and signing secret **once** — paste them into the
provider's webhook settings. Connection details offer live verify, credential
rotation, enable/disable, delete, and recent tool usage.

Agents get connector tools through grants (Tools & Access tab or wizard
step 5), optionally scoped to one connection and repository/branch glob
patterns like `octo/*` or `agent/*`. See
[docs/architecture/connectors.md](docs/architecture/connectors.md) for the
SDK and how to contribute a connector. In the dev overlay the `fake-github`
and `fake-linear` services let the GitHub and Linear flows run without real
credentials (point a connection's `base_url` at `http://fake-github:8080`
or `http://fake-linear:8080`).

### Triggers — the showcase demo

The **Triggers page** automates WHEN/IF/THEN: WHEN a connector event arrives
(connection + canonical event type), IF the filter conditions match (with a
"State changes to Todo" preset and a team picker fed by connector metadata),
THEN a task is created and assigned to an agent. Every trigger can be
dry-run against a sample event with per-condition pass/fail explanations,
and recent invocations link to the tasks they started.

To walk the flagship slice on a fresh dev environment (`make dev`,
`make migrate`, `make seed` — the seed installs everything above):

```bash
# 1. Point fake Linear's webhook at the seeded connection: copy the webhook
#    URL/secret shown once when a connection is created (for the seeded
#    connection, see the fixed dev values in apps/api/src/jhin_api/seed.py):
curl -X POST http://localhost:8092/_admin/webhook \
  -H 'content-type: application/json' \
  -d '{"url":"http://api:8000/api/v1/webhooks/linear/<public_id>","secret":"<secret>"}'

# 2. The moment: move the seeded issue ENG-142 from Backlog to Todo.
curl -X POST http://localhost:8092/_admin/issues/ENG-142/transition \
  -H 'content-type: application/json' -d '{"state":"Todo"}'
```

Fake Linear signs and fires the webhook; the API verifies the signature,
dedupes the delivery, and publishes the raw event; the event worker
normalizes it and matches the trigger; a `TriggeredTaskWorkflow` creates the
task ("[ENG-142] …", linked to `linear`/`ENG-142`) and runs the SWE agent.
Watch it on the Tasks page — the detail view shows "Started by trigger …
from linear ENG-142" — and afterwards `curl http://localhost:8092/_state`
shows the outcome comment on the issue. Redelivering the same webhook (or
refiring the same transition) never creates a second task.

### Frontend data fetching

The web app uses TanStack Query on the client with cookie-based sessions and a
CSRF double-submit header. Browser calls go to `/api/*`, which Next.js
rewrites to the API container (`API_INTERNAL_URL`) so cookies stay
same-origin.

## License

Apache-2.0 — see [LICENSE](LICENSE). Copyright 2026 Jhin contributors.
