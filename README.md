# Jhin

[![CI](https://github.com/Teachmetech/Jhin/actions/workflows/ci.yml/badge.svg)](https://github.com/Teachmetech/Jhin/actions/workflows/ci.yml)
[![Security](https://github.com/Teachmetech/Jhin/actions/workflows/security.yml/badge.svg)](https://github.com/Teachmetech/Jhin/actions/workflows/security.yml)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Version](https://img.shields.io/badge/version-0.1.0--rc-orange.svg)](CHANGELOG.md)

Jhin is a self-hosted, open-source platform for creating hierarchical teams of
autonomous AI agents that can securely use external systems, react to triggers,
delegate work, execute long-running workflows, and expose all activity through
a polished web application.

**Architecture:** [Temporal](https://temporal.io) is the durable workflow
authority, [NATS JetStream](https://nats.io) is the asynchronous event
backbone, and PostgreSQL is the system of record. A FastAPI control-plane API
owns configuration and authorization, and a Next.js frontend provides the
operations UI.

> **Status: 0.1.0 release candidate.** Phases 1–9 of the implementation
> plan are complete and verified with compose-stack integration tests.
> Phase 10 (production operations) is in progress: deterministic connector,
> tool, trigger-sync, and sandbox effects now run on an isolated tool worker;
> model reasoning and its private durable record remain on the agent worker.
> See [Deterministic tool-worker boundary](docs/architecture/tool-worker-boundary.md)
> and [Sandboxing architecture](docs/architecture/sandboxing.md) for the
> ownership, compatibility, and Docker-authority contracts. Phase 11
> (open-source release) artifacts are in place: community files, CI/E2E/
> Security/Release workflows, the release Compose bundle under `deploy/`,
> the documentation set under `docs/`, and `scripts/release_preflight.py`.
> The first tagged release (`v0.1.0`) and public image publication remain
> owner-gated; do not treat an installation as production-ready until the
> section 49 criteria in `docs/implementation-plan.md` have evidence for
> your environment.

## Features

- **Chat-first operation.** Named, persistent conversations with every agent
  (`/chats`); each turn that needs work becomes a durable task behind the
  scenes. See [Conversations and Company Activity](docs/architecture/conversations.md).
- **A company, not a bot.** Teams, managers, and reporting lines
  (`/company`, `/agents`); grant-scoped delegation with implementer/QA
  routing, bounded fix-retest loops, and visible queues
  ([Delegation and Teams](docs/architecture/delegation-and-teams.md)).
- **Activity and Attention.** A plain-language feed of who asked whom for
  what and how it went (`/activity`), plus an inbox for approvals, failed
  work, and chats waiting on you (`/attention`).
- **People and permissions.** Invite colleagues with single-use, expiring
  links (no email server required — the link is shown once to share out of
  band), with four roles whose boundaries are written down rather than
  implied: owners run the workspace, admins run the agents, members use them,
  viewers read (`/people`, [roles and permissions](docs/architecture/rbac.md)).
- **Scoped API keys.** `jhin_`-prefixed bearer keys for scripts and CI, with a
  granular scope tree and one hard rule: a key can never do more than the
  person who created it. Every call lands in a usage log
  (`/api-keys`, [API keys](docs/architecture/api-keys.md)).
- **Secure tool use.** Deny-by-default capability grants, approval policies,
  sanitized and audited tool calls, executed only on an isolated tool worker;
  credentials live in an envelope-encrypted secret store and are never shown
  again.
- **Agent Skills.** A workspace library of reusable instruction packs in
  the open SKILL.md format (`/skills`) — ship the built-in starters, write
  your own, or import from GitHub (imports stay disabled until reviewed).
  Agents see only names and descriptions in their prompt and read a
  skill's full playbook on demand through the audited `skills.read` tool
  ([Agent Skills](docs/architecture/skills.md)).
- **Connectors.** GitHub, Linear, Vercel, Supabase, and a CLI sandbox that runs
  jobs in ephemeral non-root containers, with fake services for
  credential-free development (`/apps`,
  [connector SDK](docs/architecture/connectors.md)).
- **Apps library and any MCP server.** The Apps page is a searchable library
  of ~45 well-known apps (Notion, Slack, Stripe, Sentry, Figma, …) that
  connect either through a native connector or through the app's Model
  Context Protocol server — and a generic "Any MCP server" card takes any
  remote MCP URL. Discovered tools get risk levels from the server's
  annotations (read / write / destructive), admins can override them per
  tool, agents are granted `mcp.<server>.*` or single tools, and every call
  flows through the same gateway, approvals, and sanitization
  ([MCP security model](docs/architecture/mcp.md)).
- **Automations.** Signed webhooks in, WHEN/IF/THEN triggers with dry-run
  explanations, durable task creation out (`/automations`,
  [Events and triggers](docs/architecture/events.md)).
- **Durable by construction.** Temporal workflows survive worker restarts,
  NATS JetStream redelivery is idempotent, PostgreSQL is the single system
  of record, and every operational screen stays available under `/advanced`.
- **Memory and identity.** Curated long-term memory with secret redaction and
  agent avatars ([memory](docs/architecture/memory.md),
  [media](docs/architecture/media.md)).
- **Observable.** Structured JSON logs with redaction, OpenTelemetry traces
  and metrics, protected health endpoints.

## Quick start

> **macOS / Docker Desktop note.** The server-grade sandbox contract needs a
> Linux Docker socket in `rootful` (root-owned, positive docker GID) or
> `rootless` (UID 10001 daemon) mode. Docker Desktop exposes a `uid 0 / gid 0`
> socket behind a symlink, which satisfies neither, so on a Mac or Windows
> machine use the explicit, development-only third mode described under
> [Docker Desktop (macOS/Windows, local development only)](#docker-desktop-macoswindows-local-development-only):
> `compose.desktop.yaml` lets `sandbox-runner` reach the Desktop VM daemon
> through root-group membership, and the leased integration harness runs with
> `make test-integration PHASE10_MODE=desktop`. A base-only
> `docker compose -f compose.yaml -f compose.dev.yaml up` still starts every
> service except `sandbox-runner`. Never use `desktop` mode on a server.


Requirements: Docker with Compose v2 (Linux for servers; Docker Desktop on
macOS/Windows for local development only), Python 3.13, `uv`, and `make`.

```bash
git clone https://github.com/Teachmetech/Jhin.git
cd Jhin
cp .env.example .env
make master-key          # one-time: generate the secret-store master key
```

Choose exactly one Docker socket mode below (`rootless` or `rootful` on Linux;
`desktop` on Docker Desktop). The commands disable implicit `.env` loading,
scrub inherited Compose and Docker targeting controls, and pin the Compose
project to `jhin`: export any reviewed infrastructure values in the operator
environment, but do not put credentials or tokens on the command line. A
base-only start and a start containing more than one overlay are invalid. All
modes build the sandbox job image before starting the stack.

### Rootless Docker socket (Linux)

This mode requires an already-running rootless Docker daemon owned by host UID
10001. The preflight rejects a relative path, symlink, non-socket, wrong owner,
or a daemon whose security options do not include `name=rootless`. The runner
image is built explicitly before the image-only adapter starts; the adapter has
`pull_policy: never`, so both services must use that same local image.

```bash
set -euo pipefail
unset \
  APP_ENV \
  COMPOSE_FILE \
  COMPOSE_PROFILES \
  COMPOSE_PROJECT_NAME \
  COMPOSE_REMOVE_ORPHANS \
  COMPOSE_IGNORE_ORPHANS \
  COMPOSE_ENV_FILES \
  BUILDX_BUILDER \
  BUILDKIT_HOST \
  DOCKER_HOST \
  DOCKER_CONTEXT \
  DOCKER_TLS \
  DOCKER_TLS_VERIFY \
  DOCKER_CERT_PATH \
  DOCKER_API_VERSION \
  DOCKER_DEFAULT_PLATFORM
export COMPOSE_DISABLE_ENV_FILE=1
export COMPOSE_PROJECT_NAME=jhin
export PHASE10_SOCKET_MODE=rootless
export PHASE10_ROOTLESS_DOCKER_SOCKET=/run/user/10001/docker.sock
python - "$PHASE10_ROOTLESS_DOCKER_SOCKET" <<'PY'
import stat
import sys
from pathlib import Path

path = Path(sys.argv[1])
try:
    info = path.lstat()
except OSError as error:
    raise SystemExit(f"cannot inspect rootless Docker socket: {error}") from error
if not path.is_absolute() or stat.S_ISLNK(info.st_mode):
    raise SystemExit("rootless Docker socket must be an absolute non-symlink")
if not stat.S_ISSOCK(info.st_mode) or info.st_uid != 10001:
    raise SystemExit("rootless Docker socket must be a Unix socket owned by UID 10001")
PY
rootless_security="$(docker --host "unix://$PHASE10_ROOTLESS_DOCKER_SOCKET" info --format '{{json .SecurityOptions}}')"
case "$rootless_security" in
  *name=rootless*) ;;
  *) echo "configured daemon is not rootless" >&2; exit 1 ;;
esac
export DOCKER_HOST="unix://$PHASE10_ROOTLESS_DOCKER_SOCKET"
export BUILDX_BUILDER=default
uv run python scripts/assert_phase10_tool_worker_compose.py --mode rootless
docker compose \
  -f compose.yaml \
  -f compose.rootless.yaml \
  build sandbox-runner
docker compose \
  -f compose.yaml \
  -f compose.rootless.yaml \
  --profile build \
  build sandbox-image
docker compose \
  -f compose.yaml \
  -f compose.rootless.yaml \
  up -d --build --wait --wait-timeout 180
docker compose \
  -f compose.yaml \
  -f compose.rootless.yaml \
  exec -T rootless-docker-transport python -c "import urllib.request; r=urllib.request.urlopen('http://127.0.0.1:2375/_ping', timeout=2); raise SystemExit(0 if r.status == 200 and r.read() == b'OK' else 1)"
docker compose \
  -f compose.yaml \
  -f compose.rootless.yaml \
  exec -T sandbox-runner python -c "import urllib.request; raise SystemExit(0 if urllib.request.urlopen('http://127.0.0.1:8085/health', timeout=3).status == 200 else 1)"
docker compose \
  -f compose.yaml \
  -f compose.rootless.yaml \
  run --rm --no-deps api jhin-db-migrate
docker compose \
  -f compose.yaml \
  -f compose.rootless.yaml \
  ps --all
```

The real Docker `GET /_ping` check must pass before the daemon-backed runner
health check. The final `ps --all` must contain exactly `api`, `web`,
`workflow-worker`, `agent-worker`,
`tool-worker`, `sandbox-runner`,
`rootless-docker-transport`, `event-worker`, `postgres`, `nats`, `temporal`,
and `temporal-ui`; every row must be running and every health-bearing service
must be healthy.

### Rootful Docker socket (Linux)

This mode accepts only an absolute, non-symlink Unix socket owned by UID 0
with a positive numeric group. The preflight discovers that exact group without
changing the socket. A wrong type, owner, group, or runner access check is a
fatal startup error; repair the host's Docker installation instead of relaxing
permissions or running Jhin as root.

```bash
set -euo pipefail
unset \
  APP_ENV \
  COMPOSE_FILE \
  COMPOSE_PROFILES \
  COMPOSE_PROJECT_NAME \
  COMPOSE_REMOVE_ORPHANS \
  COMPOSE_IGNORE_ORPHANS \
  COMPOSE_ENV_FILES \
  BUILDX_BUILDER \
  BUILDKIT_HOST \
  DOCKER_HOST \
  DOCKER_CONTEXT \
  DOCKER_TLS \
  DOCKER_TLS_VERIFY \
  DOCKER_CERT_PATH \
  DOCKER_API_VERSION \
  DOCKER_DEFAULT_PLATFORM
export COMPOSE_DISABLE_ENV_FILE=1
export COMPOSE_PROJECT_NAME=jhin
export PHASE10_SOCKET_MODE=rootful
export SANDBOX_DOCKER_SOCKET_HOST=/var/run/docker.sock
SANDBOX_DOCKER_GID="$(python - "$SANDBOX_DOCKER_SOCKET_HOST" <<'PY'
import stat
import sys
from pathlib import Path

path = Path(sys.argv[1])
try:
    info = path.lstat()
except OSError as error:
    raise SystemExit(f"cannot inspect rootful Docker socket: {error}") from error
if not path.is_absolute() or stat.S_ISLNK(info.st_mode):
    raise SystemExit("rootful Docker socket must be an absolute non-symlink")
if not stat.S_ISSOCK(info.st_mode) or info.st_uid != 0 or info.st_gid <= 0:
    raise SystemExit("rootful Docker socket must be owned by UID 0 and a positive GID")
print(info.st_gid)
PY
)"
export SANDBOX_DOCKER_GID
export DOCKER_HOST="unix://$SANDBOX_DOCKER_SOCKET_HOST"
export BUILDX_BUILDER=default
uv run python scripts/assert_phase10_tool_worker_compose.py --mode rootful
docker compose \
  -f compose.yaml \
  -f compose.rootful.yaml \
  --profile build \
  build sandbox-image
docker compose \
  -f compose.yaml \
  -f compose.rootful.yaml \
  up -d --build --wait --wait-timeout 180
docker compose \
  -f compose.yaml \
  -f compose.rootful.yaml \
  exec -T sandbox-runner python -c "import urllib.request; raise SystemExit(0 if urllib.request.urlopen('http://127.0.0.1:8085/health', timeout=3).status == 200 else 1)"
docker compose \
  -f compose.yaml \
  -f compose.rootful.yaml \
  run --rm --no-deps api jhin-db-migrate
docker compose \
  -f compose.yaml \
  -f compose.rootful.yaml \
  ps --all
```

The final `ps --all` must contain exactly `api`, `web`, `workflow-worker`,
`agent-worker`,
`tool-worker`, `sandbox-runner`, `event-worker`, `postgres`,
`nats`, `temporal`, and `temporal-ui`; every row must be running and every
health-bearing service must be healthy. Rootful mode has no daemon sidecar and
therefore no daemon-service dependency.

### Docker Desktop (macOS/Windows, local development only)

Docker Desktop runs the daemon in a Linux VM. Its host socket is a user-owned
symlink target, and inside a container it is a `uid 0 / gid 0` Unix socket, so
the only way for the unprivileged `10001:10001` runner to use it is membership
in the root group (`group_add: ["0"]`). That is a weaker boundary than the two
Linux modes and is acceptable **only on a developer's own machine** — never on
a shared host, a server, or CI. The runner still drops every capability, keeps
`no-new-privileges`, and refuses to start unless the mounted socket is a
root-owned, root-group, non-symlink Unix socket and the daemon behind it
reports `OperatingSystem: Docker Desktop`.

Preflight resolves the compatibility symlink (the one mode where a symlink is
accepted on the host) and binds the real socket:

```bash
set -euo pipefail
unset \
  APP_ENV \
  COMPOSE_FILE \
  COMPOSE_PROFILES \
  COMPOSE_PROJECT_NAME \
  COMPOSE_REMOVE_ORPHANS \
  COMPOSE_IGNORE_ORPHANS \
  COMPOSE_ENV_FILES \
  BUILDX_BUILDER \
  BUILDKIT_HOST \
  DOCKER_HOST \
  DOCKER_CONTEXT \
  DOCKER_TLS \
  DOCKER_TLS_VERIFY \
  DOCKER_CERT_PATH \
  DOCKER_API_VERSION \
  DOCKER_DEFAULT_PLATFORM \
  SANDBOX_DOCKER_GID
export COMPOSE_DISABLE_ENV_FILE=1
export COMPOSE_PROJECT_NAME=jhin
export PHASE10_SOCKET_MODE=desktop
SANDBOX_DOCKER_SOCKET_HOST="$(python - /var/run/docker.sock <<'PY'
import stat
import sys
from pathlib import Path

path = Path(sys.argv[1])
if not path.is_absolute():
    raise SystemExit("desktop Docker socket path must be absolute")
try:
    resolved = path.resolve(strict=True)
except OSError as error:
    raise SystemExit(f"cannot resolve desktop Docker socket: {error}") from error
if not stat.S_ISSOCK(resolved.lstat().st_mode):
    raise SystemExit("desktop Docker socket must resolve to a Unix socket")
print(resolved)
PY
)"
export SANDBOX_DOCKER_SOCKET_HOST
desktop_os="$(docker --host "unix://$SANDBOX_DOCKER_SOCKET_HOST" info --format '{{.OperatingSystem}}')"
case "$desktop_os" in
  *"Docker Desktop"*) ;;
  *) echo "desktop mode requires a Docker Desktop daemon (got: $desktop_os)" >&2; exit 1 ;;
esac
export DOCKER_HOST="unix://$SANDBOX_DOCKER_SOCKET_HOST"
export BUILDX_BUILDER=default
uv run python scripts/assert_phase10_tool_worker_compose.py --mode desktop --dev
docker compose \
  -f compose.yaml \
  -f compose.dev.yaml \
  -f compose.desktop.yaml \
  --profile build \
  build sandbox-image
docker compose \
  -f compose.yaml \
  -f compose.dev.yaml \
  -f compose.desktop.yaml \
  up -d --build --wait --wait-timeout 300
docker compose \
  -f compose.yaml \
  -f compose.dev.yaml \
  -f compose.desktop.yaml \
  exec -T sandbox-runner python -c "import urllib.request; raise SystemExit(0 if urllib.request.urlopen('http://127.0.0.1:8085/health', timeout=3).status == 200 else 1)"
docker compose \
  -f compose.yaml \
  -f compose.dev.yaml \
  -f compose.desktop.yaml \
  run --rm --no-deps api jhin-db-migrate
docker compose \
  -f compose.yaml \
  -f compose.dev.yaml \
  -f compose.desktop.yaml \
  ps --all
```

The commands above include the dev overlay (fake connectors, loopback ports,
Temporal UI) because desktop mode is for development; drop
`-f compose.dev.yaml` for a production-shaped local stack. The final `ps --all`
has the same service set as rootful mode (no `rootless-docker-transport`).
The leased integration harness works the same way:

```bash
make test-integration PHASE10_MODE=desktop      # frozen live regression set
make test-sandbox-socket-desktop                # live desktop socket boundary
make compose-up PHASE10_MODE=desktop            # persistent isolated stack
make compose-down
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

## Screenshots and demo

The seeded, credential-free demo (fake model provider, fake Linear, fake
GitHub) walks through chats, the company map, an automation firing on a
Linear ticket, delegation to QA, and the approval surface. The step-by-step
flow, expected output, and the reviewed screenshot set are in
[docs/demo.md](docs/demo.md).

## Production deployment

Production installs use the pull-based release bundle
(`deploy/compose.release.yaml`, rendered per release with digest-pinned
images from `ghcr.io/teachmetech/jhin-<component>`), a TLS reverse proxy in
front of the web entry point, and exactly one Docker-socket overlay for the
sandbox runner. The full contract (configuration classes, secrets and the
master key, backups and restore, upgrades, sizing, health, telemetry,
troubleshooting) is in [docs/deployment.md](docs/deployment.md).

Connector credentials are stored encrypted under a master key that Jhin never
backs up for you: **back up the master key separately from the database**.
Losing it makes every stored credential unreadable.

Verify a released image before running it:

```bash
cosign verify \
  --certificate-identity-regexp '^https://github.com/Teachmetech/Jhin/.github/workflows/release.yml@refs/tags/v' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  ghcr.io/teachmetech/jhin-api@<digest-from-image-lock.json>
```

The self-hosted core (PostgreSQL, NATS, Temporal, all Jhin services) runs
entirely on your host. Model providers and external connectors (GitHub,
Linear, Vercel, Supabase) are optional managed services you connect with
your own credentials.

## Documentation

- [Documentation index](docs/README.md)
- [Architecture index](docs/architecture/README.md) — every service and
  trust boundary mapped to its authoritative document
- [Deployment guide](docs/deployment.md)
- [Demo and screenshots](docs/demo.md)
- [Starter templates](docs/templates.md)
- [Implementation plan](docs/implementation-plan.md)

## Community and security

- [Contributing](CONTRIBUTING.md) — setup, gates, conventions, boundaries,
  adding a connector
- [Support](SUPPORT.md) — where to ask and what to include
- [Security policy](SECURITY.md) — private vulnerability reporting, supported
  versions, hardening notes
- [Code of conduct](CODE_OF_CONDUCT.md)
- [Changelog](CHANGELOG.md)

## Repository layout

```text
apps/
  api/          FastAPI control plane (jhin_api)
  web/          Next.js web UI
services/
  workflow_worker/   Temporal worker (general workflows)
  agent_worker/      Temporal worker executing agent runs (model calls live here)
  tool_worker/       Temporal worker executing deterministic tools/connectors
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

# Start with one validated socket-mode command from Quick start. A dev stack
# also adds compose.dev.yaml before that one selected mode overlay.
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

### Home, Chats, Activity, and Attention

- **Home** (`/home`, the screen you land on after signing in) — what needs a
  person (approvals, reviews, failed work, chats waiting on you), what your
  agents are running right now with the latest handoffs between them, your
  recent chats, this month's model spend against the budget, and a team
  snapshot. A short setup checklist appears only while the workspace is
  missing a model provider, an agent, or a connected app.
- **Chats** — pick an agent and describe what you want.
  Each conversation is a persistent thread; every turn that needs work
  becomes a durable task behind the scenes, and follow-up turns carry the
  earlier conversation as context. Rename, pin, archive, and search chats;
  inline cards show handoffs, reviews, and approvals; "Details" reveals
  the underlying work episodes, cost, and a link into Advanced.
- **Activity** — who asked whom for what, and how it went: delegations,
  reviews, results, escalations, and task lifecycle projected as plain
  language cards with "Open chat" / "Open in Advanced" links.
- **Attention** — pending approvals, failed work, and chats waiting on you.
- **Agents / Company** — a directory with profiles (purpose, colleagues,
  what each agent can use, recent activity) and an org outline/map.
- **Automations** — a friendly view over triggers; the full builder remains
  under Advanced.
- **Apps** (`/apps`) — the one place connections live: a searchable library of
  well-known apps plus what is already connected, with per-connection
  verification, credential rotation, webhook setup, discovered tools and risk
  overrides, and the agent access summary in the connection drawer (the
  operational controls sit behind an "Advanced settings" disclosure). The
  older `/connectors` route permanently redirects here.

### Models and tasks

- **Models page** — add providers (API keys go into the encrypted secret
  store and are never displayed again), verify them with a live call, create
  priced model profiles, and pick the workspace default profile. Picking a
  model auto-fills its prices (live from OpenRouter, or from a public price
  catalog for OpenAI and Anthropic — editable), and each provider card shows
  a Balance block: live remaining credit (OpenRouter), month-to-date spend
  (OpenAI, with an optional admin key), or spend tracked by Jhin with an
  optional "loaded credits" figure. A Spend tile and the Settings page show
  the month's total against an optional monthly budget. Out-of-credit
  failures surface as a friendly "add funds" message in the chat. Details:
  [docs/architecture/models.md](docs/architecture/models.md). Agents use
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
Vercel, Supabase, and the CLI sandbox are live). Admins create a connection by
picking an auth method (including a Vercel access token, a Supabase Management
API token, or a separate Supabase PostgreSQL DSN), entering credentials
(stored in the encrypted secret store, never displayed again), and optional
public config. The create response shows webhook setup once: Jhin-generated
secrets are displayed once, while Vercel's provider-generated signing secret
is pasted into a write-only field. Connection details offer live verify,
credential rotation, enable/disable, delete, Agent Access, and recent tool
usage.

For a least-privilege production setup, create one Vercel connection for the
intended account/team and grant agents only the exact project, deployment,
environment, and repository dimensions they need. For Supabase, create one
Management API connection for project/log/Edge Function work and a different
PostgreSQL connection backed by a custom non-owner role for database work.
Keep `allow_writes=false` unless mutations are required, then combine the
smallest table privileges with exact `connection_id`/`project_ref`/`schema`
grants and approval policy. The complete setup and SQL safety model are in
[Vercel and Supabase Connectors](docs/architecture/vercel-and-supabase.md).

Agents get connector tools through grants (Tools & Access tab or wizard
step 5), scoped to the connector's required dimensions; GitHub repository and
branch values may use patterns like `octo/*` or `agent/*`. See
[docs/architecture/connectors.md](docs/architecture/connectors.md) for the
SDK and how to contribute a connector. The agent worker receives only
advertised tool schemas; the isolated tool worker owns executable connector
catalogs, credentials, and effects. In the dev overlay the `fake-github`
and `fake-linear` services let the GitHub and Linear flows run without real
credentials (point a connection's `base_url` at `http://fake-github:8080`
or `http://fake-linear:8080`).

### Delegation and teams

Jhin has two separate ways to create child work. Model-initiated
`organization.delegate_task` is deny-by-default: grant the delegating agent a
live `organization.delegate` capability with the intended target scope.
Reporting relationships only constrain that scope; they do not grant
delegation authority. Removing the allow grant or adding an applicable deny
stops future gateway delegation requests.

The engineering ticket template is different: an administrator configures a
trigger, its target and optional implementer/QA routing, and the
`engineering_ticket` template. Its child-task routing is authorized by that
trigger configuration and does not call `organization.delegate_task`, so a
delegation grant is neither needed nor a way to disable it. Disable or delete
the trigger, or update its routing/template configuration, to stop new
template routes. Direct mode assigns the root ticket to the SWE; coordinator
mode keeps the CTO-owned root model-free and routes implementation to the
configured SWE. Optional manager and QA reviews use real child tasks, with
bounded fix/retest cycles after a failed verdict. Follow the delegation chain
and structured results on the task page; tasks waiting for an agent or
workspace run slot remain visibly queued until capacity is available.

### Triggers — the showcase demo

The **Triggers page** automates WHEN/IF/THEN: WHEN a connector event arrives
(connection + canonical event type), IF the filter conditions match (with a
"State changes to Todo" preset and a team picker fed by connector metadata),
THEN a task is created and assigned to an agent. Every trigger can be
dry-run against a sample event with per-condition pass/fail explanations,
and recent invocations link to the tasks they started.

To walk the flagship slice after starting a dev stack with
`compose.dev.yaml` plus the same one explicit socket-mode overlay, apply
migrations and seed data, then:

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
