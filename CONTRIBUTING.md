# Contributing to Jhin

Thanks for helping build Jhin. This guide covers local setup, the gates every
change must pass, conventions, and the architecture boundaries reviewers
protect. Inbound contributions are licensed under
[Apache-2.0](LICENSE) and signed off under the Developer Certificate of
Origin (DCO); there is no separate contributor license agreement.

## Prerequisites

- Linux with Docker Engine and Compose v2 for the live stack (macOS with
  Docker Desktop runs everything except `sandbox-runner`; see the README).
- Python 3.13 and [uv](https://docs.astral.sh/uv/).
- Node 22 and pnpm 10 (`corepack enable pnpm`).
- `make`.

## Set up a development checkout

```bash
git clone https://github.com/Teachmetech/Jhin.git
cd Jhin
uv sync --all-packages      # Python workspace (all apps/services/packages)
pnpm install                # web workspace (apps/web)
cp .env.example .env        # development defaults; never commit .env
```

Start a stack with exactly one socket-mode overlay from the README quick
start, then apply migrations and seed demo data:

```bash
make migrate
make seed                    # owner@jhin.dev / jhin-dev-password (dev only)
```

`make dev` and `make compose-up` boot an isolated, production-shaped stack
through the Phase 10 harness; `make compose-down` cleans it up.

## Verification gates

Every pull request must pass these locally before review:

| Command | What it runs |
|---|---|
| `make lint` | `ruff check`, `ruff format --check`, `eslint` |
| `make typecheck` | `mypy --strict` over every package, `tsc --noEmit` |
| `make test-unit` | `pytest` (unit, integration deselected) and `vitest` |
| `make test-tool-worker-boundary` | focused Phase 10 unit/replay/dependency/render gates |
| `make test-integration` | frozen live regression set against the isolated stack (Linux) |
| `uv run python scripts/release_preflight.py` | release artifacts, versions, links, env coverage, secret scan |

CI runs the same commands (`.github/workflows/ci.yml`, `e2e.yml`,
`security.yml`). A change that crosses a service boundary (API, worker,
event, sandbox) needs integration coverage under `tests/integration`.

## Where plans and specs live

- `docs/implementation-plan.md` is the authoritative plan; phase checklists
  are in its final sections.
- `docs/superpowers/specs/` holds approved designs; `docs/superpowers/plans/`
  holds the step-by-step implementation plans derived from them.
- `docs/architecture/` documents the shipped system. Update it in the same PR
  as the behaviour it describes.

Open an issue or discussion before large changes so the design can be
reviewed against the plan first.

## Architecture boundaries to respect

These are enforced by tests (`tests/test_worker_dependency_boundaries.py`,
`tests/test_executable_catalog_boundary.py`, `tests/test_phase9_production_compose.py`)
and by review:

1. **PostgreSQL is product truth, Temporal is workflow authority, NATS is
   transport.** Never make a NATS message or a Temporal history the only
   record of user-visible state.
2. **The API is the control-plane authority.** Workers never accept
   configuration from the UI directly.
3. **The agent worker reasons; the tool worker executes.** Model calls live
   only in `services/agent_worker`; connector credentials, gateway
   authorization, approval revalidation, sanitization, and audit live only in
   `services/tool_worker` (see
   [tool-worker boundary](docs/architecture/tool-worker-boundary.md)).
4. **Only `sandbox-runner` touches the Docker socket**, always as a non-root
   UID, through one explicit rootful or rootless overlay. Jobs never receive
   the socket ([sandboxing](docs/architecture/sandboxing.md)).
5. **Secrets are envelope-encrypted** in `packages/secrets` and never logged,
   returned by the API after creation, or written to `.env`.
6. **Production Compose contains no fake service, dev credential, or dev
   allowlist.** Dev conveniences go in `compose.dev.yaml` only.
7. **Schema changes are Alembic migrations** in `packages/db`, forward-only
   unless a reviewed downgrade exists. Application startup never creates
   tables.
8. **Workflow changes must replay.** Add a frozen-history replay test when you
   change a workflow or activity signature
   (`packages/workflows/tests/test_phase10_history_replay.py`).

## Adding a connector

Connectors are self-contained packages under
`packages/connectors/src/jhin_connectors/<name>/` and follow the shape of the
`example` connector:

```text
packages/connectors/src/jhin_connectors/<name>/
  __init__.py
  manifest.py     # ConnectorManifest: id, auth methods, required scope dimensions
  connector.py    # verify(), metadata, webhook setup descriptors
  tools.py        # tool definitions + handlers (pure, sanitized, scoped)
  schemas.py      # pydantic input/output models
  webhook.py      # signature verification + canonical event normalization
packages/connectors/tests/<name>/
```

Then:

1. Register the manifest in `packages/connectors/src/jhin_connectors/registry.py`.
2. Add outbound targets to the endpoint policy (`endpoints.py`); only
   official SaaS origins are built in, everything else must come from the
   operator allowlist `JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS`.
3. Provide a fake service under `jhin_connectors/testing/` so the flow runs
   in `compose.dev.yaml` with zero real credentials, and wire it into the
   dev overlay.
4. Write unit tests for scope matching and sanitization, and an integration
   exit test that exercises the tool through the tool worker.
5. Document the connector in `docs/architecture/connectors.md` and, if it is
   user-facing, the README connectors section.

The full SDK contract is in [docs/architecture/connectors.md](docs/architecture/connectors.md).

## Commits and pull requests

- Use [Conventional Commits](https://www.conventionalcommits.org/):
  `feat(connectors): ...`, `fix(api): ...`, `docs: ...`, `test: ...`,
  `chore(release): ...`.
- Sign off every commit (`git commit -s`) to certify the
  [DCO](https://developercertificate.org/).
- Keep PRs focused; fill in the pull-request template (gates, compatibility,
  security impact).
- Do not commit generated local state: `.env`, `secrets/`, `.venv`,
  `node_modules`, `.next`, `.test-artifacts`, captured Temporal histories
  outside the reviewed fixtures.
- Update `CHANGELOG.md` under **Unreleased** for user-visible changes.

## Security-sensitive review paths

Changes under `packages/secrets`, `packages/policy`, `packages/tools`,
`services/tool_worker`, `services/sandbox_runner`, `apps/api/src/jhin_api/auth`,
`packages/connectors`, `compose*.yaml`, `deploy/`, and `.github/` require a
maintainer review (see `.github/CODEOWNERS`). Report vulnerabilities privately
per [SECURITY.md](SECURITY.md), never in a public issue or PR.

## Release process (maintainers)

Versions live in `VERSION`, every `pyproject.toml`, and `apps/web/package.json`
and must agree; `scripts/release_preflight.py` enforces this and the
`CHANGELOG.md` section. Tagging `vX.Y.Z` runs `.github/workflows/release.yml`.
The owner-gated publication steps are listed in
`docs/superpowers/specs/2026-08-18-phase-11-open-source-release-design.md` §7.2.
