# Deployment guide

This is the production deployment contract for Jhin. It covers the release
bundle, reverse proxy and TLS, secrets and the master key, Docker-socket
modes, sizing, backups and restore, upgrades and migrations, health
endpoints, telemetry, and troubleshooting. Development conveniences
(`compose.dev.yaml`, fake services, seeded credentials) are never part of a
production deployment.

> Production-readiness statement: Phase 10 hardening is implemented and
> verified by the live harness, but `v0.1.0` is a release candidate. Do not
> treat an installation as production-ready until every criterion in
> section 49 of `docs/implementation-plan.md` has current evidence for your
> environment (backup/restore drill, key recovery, upgrade, sizing).

## Supported platforms

| Host | Status |
|---|---|
| Linux x86-64 or arm64 with Docker Engine 25+ and Compose v2 | supported (rootful or rootless socket mode) |
| Docker Desktop on macOS/Windows | development only; `sandbox-runner` cannot satisfy the rootful/rootless socket contract (see README) |
| Kubernetes, Podman, Nomad | not supported in `0.1.x` |

Images are multi-arch (`linux/amd64`, `linux/arm64`), run as non-root, and
are published to `ghcr.io/jhinhq/jhin-<component>` for the eight
components: `web`, `api`, `workflow-worker`, `agent-worker`, `tool-worker`,
`event-worker`, `sandbox-runner`, `sandbox`.

## Installation paths

Private installations can use direct app sign-in without a public domain.
See [local app sign-in](operations/local-app-sign-in.md) for localhost access,
private NAS forwarding, and provider-specific setup requirements.

### Release bundle (recommended)

Each GitHub Release attaches `jhin-<version>-compose.tar.gz`, `image-lock.json`,
per-image and source SBOMs (`*.spdx.json`), `SHA256SUMS`, and Sigstore
bundles. The tarball contains a digest-pinned `compose.yaml`, the rootful and
rootless overlays, `config/nats.conf`, `.env.release.example`, `MANIFEST.SHA256`,
`VERIFY.md`, and this documentation.

```bash
VERSION=0.1.0
curl -fsSLO "https://github.com/jhinhq/Jhin/releases/download/v$VERSION/jhin-$VERSION-compose.tar.gz"
curl -fsSLO "https://github.com/jhinhq/Jhin/releases/download/v$VERSION/SHA256SUMS"
curl -fsSLO "https://github.com/jhinhq/Jhin/releases/download/v$VERSION/SHA256SUMS.sigstore.json"

# Verify the checksum file was produced by the release workflow, then the tarball.
cosign verify-blob \
  --bundle SHA256SUMS.sigstore.json \
  --certificate-identity-regexp '^https://github.com/jhinhq/Jhin/.github/workflows/release.yml@refs/tags/v' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  SHA256SUMS
sha256sum --check --ignore-missing SHA256SUMS

tar -xzf "jhin-$VERSION-compose.tar.gz"
cd "jhin-$VERSION-compose"
sha256sum --check MANIFEST.SHA256
cp .env.release.example .env
```

Verify any image before you trust it (every digest is listed in
`image-lock.json`):

```bash
cosign verify \
  --certificate-identity-regexp '^https://github.com/jhinhq/Jhin/.github/workflows/release.yml@refs/tags/v' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  ghcr.io/jhinhq/jhin-api@sha256:<digest>
```

The repository copy of the manifest is `deploy/compose.release.yaml`; it
resolves images by `${JHIN_VERSION}` tag, while the bundle rendered by the
release workflow pins them by digest.

### Source checkout

`compose.yaml` at the repository root builds the same images locally. Use it
for development and for contributors; the README quick start documents the
exact commands for each socket mode.

## Configuration

Every variable is read by Compose from the operator environment or `.env`.
Classification: **required** must be set, **generated** should be produced
by the operator once, **optional** has a safe default, **secret** must never
be committed or logged, **dev-only** must be absent in production.

| Variable | Class | Consumed by | Notes |
|---|---|---|---|
| `JHIN_VERSION` | required (bundle) | release Compose | image tag, e.g. `0.1.0`; the bundle pins digests so this only labels the stack |
| `JHIN_IMAGE_NAMESPACE` | optional | release Compose | default `ghcr.io/jhinhq`; set for a mirror |
| `APP_NAME` | optional | web, api | display name, default `Jhin` |
| `APP_ENV` | required | all services | `production` for production; `dev`/`test`/`staging` accepted; the dev overlay forces `dev` |
| `APP_URL` | required | api | public origin of the web UI (`https://jhin.example.com`); used for CORS |
| `COOKIE_SECURE` | required behind TLS | api | `true` in production so session/CSRF cookies are `Secure` |
| `LOG_LEVEL` | optional | all services | `INFO` default; JSON logs on stdout |
| `WEB_PORT` | optional | web | host port for the UI, default `3000`; bind it to loopback behind a proxy (`127.0.0.1:3000`) |
| `API_PORT` | optional | api | host port for the API, default `8000`; bind to loopback or remove the mapping in production |
| `POSTGRES_USER`, `POSTGRES_DB` | optional | postgres, api, workers, temporal | default `jhin` |
| `POSTGRES_PASSWORD` | generated, secret | postgres, api, workers, temporal | random 32+ characters; no production default |
| `TEMPORAL_NAMESPACE` | optional | api, workers | default `default` |
| `MASTER_KEY_FILE_HOST` | generated, secret | api, agent-worker, tool-worker (mounted as `/run/secrets/jhin_master_key`) | 32-byte base64 key file, mode `0600`; `uv run python scripts/generate_master_key.py` in a checkout or `openssl rand -base64 32 > jhin_master_key` |
| `SANDBOX_RUNNER_TOKEN` | generated, secret | tool-worker, sandbox-runner | bearer token on the internal runner API; `openssl rand -hex 32` |
| `SANDBOX_DEFAULT_IMAGE` | optional | tool-worker, sandbox-runner | default `jhin-sandbox:latest`; the bundle sets the GHCR digest |
| `SANDBOX_RUNNER_IMAGE` | optional | sandbox-runner, rootless transport | the rootless transport sidecar runs the same image as the runner |
| `SANDBOX_NETWORK` | optional | sandbox-runner | bridge for `network_policy=internet` jobs, default `jhin_sandbox` |
| `SANDBOX_DOCKER_SOCKET_HOST`, `SANDBOX_DOCKER_GID` | required (rootful mode) | sandbox-runner | absolute non-symlink socket owned by UID 0 and its positive numeric group |
| `PHASE10_ROOTLESS_DOCKER_SOCKET` | required (rootless mode) | rootless-docker-transport | socket of an already-running rootless daemon owned by host UID 10001 |
| `JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS` | optional, operator-only | api, tool-worker | exact `scheme://host[:port]` origins for self-hosted providers; empty by default |
| `JHIN_CONNECTOR_ALLOWED_DB_HOSTS` | optional, operator-only | api, tool-worker | exact `host[:port]` PostgreSQL targets; empty by default |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | optional | all Python services | enables OTLP trace/metric export; unset disables export |
| `OTEL_EXPORTER_OTLP_INSECURE`, `OTEL_EXPORTER_OTLP_CERTIFICATE`, `OTEL_EXPORTER_OTLP_CLIENT_CERTIFICATE`, `OTEL_EXPORTER_OTLP_CLIENT_KEY` | optional | all Python services | transport security for the collector |
| `OTEL_TRACES_SAMPLER` (`parentbased_traceidratio` default), `OTEL_TRACES_SAMPLER_ARG` (`0.10`), `OTEL_BSP_MAX_QUEUE_SIZE`, `OTEL_BSP_MAX_EXPORT_BATCH_SIZE`, `OTEL_EXPORTER_OTLP_TIMEOUT_MILLIS`, `OTEL_METRIC_EXPORT_INTERVAL_MILLIS` | optional | all Python services | sampling and batching; values are validated and bounded |
| `FAKE_*`, `*_DEV_PORT`, `JHIN_TEST_CRASH_BARRIER_*`, `JHIN_PHASE9_DB_*_DSN` | dev-only | `compose.dev.yaml` | must not exist in production |

`OTEL_*` and `COOKIE_SECURE` are read from the container environment; the
base manifest does not forward them, so add them through a small override
file (`compose.override.yaml` with an `environment:` block per service) or
your orchestration's env injection. User-added credentials (model API keys,
GitHub/Linear/Vercel/Supabase tokens) are never environment variables: they
are entered in the UI and stored in the encrypted secret store.

### External Temporal

The bundled `temporal` service uses `temporalio/auto-setup` against the same
PostgreSQL instance. To use an external Temporal cluster, remove the
`temporal` and `temporal-ui` services from your override, set
`TEMPORAL_ADDRESS` and `TEMPORAL_NAMESPACE` on `api`, `workflow-worker`,
`agent-worker`, `tool-worker`, and `event-worker`, and ensure the namespace
exists before the first start.

## Secrets and the master key

- The master key encrypts every stored credential with envelope encryption
  (per-secret data keys wrapped by the master key). It is mounted only into
  `api`, `agent-worker`, and `tool-worker`, never into `web`, workers that
  do not decrypt, or job containers.
- Generate it once, store it with mode `0600`, and **back it up separately
  from database backups**. A database backup without the key cannot recover
  any credential; a leaked key exposes every credential.
- Never commit it, put it in `.env`, or pass it on a command line.
- Rotation: the Phase 10 master-key rotation procedure re-wraps data keys
  under a new master key; follow
  `docs/superpowers/plans/2026-08-18-phase-10-master-key-rotation.md` and
  take a backup immediately before and after.

## Network exposure and reverse proxy

The base manifest publishes only `web` (`WEB_PORT`) and `api` (`API_PORT`).
PostgreSQL, NATS, Temporal, Temporal UI, and `sandbox-runner` have no host
bindings. In production:

1. Put a TLS-terminating reverse proxy in front of `web` only. The browser
   reaches the API through the Next.js `/api/*` rewrite, so the API does not
   need a public hostname.
2. Bind `WEB_PORT` and `API_PORT` to loopback (`127.0.0.1:3000:3000`) or
   remove the API mapping entirely in an override.
3. Set `APP_URL=https://<host>` and `COOKIE_SECURE=true`.
4. Forward `Host`, `X-Forwarded-For`, and `X-Forwarded-Proto`; allow request
   bodies up to 1 MiB for webhook endpoints (`/api/v1/webhooks/*`), which
   matches the NATS `max_payload` headroom in `config/nats.conf`.

Caddy (automatic TLS):

```caddyfile
jhin.example.com {
  encode zstd gzip
  request_body {
    max_size 1MB
  }
  reverse_proxy 127.0.0.1:3000 {
    header_up X-Forwarded-Proto {scheme}
    header_up X-Forwarded-For {remote_host}
  }
}
```

Nginx:

```nginx
server {
  listen 443 ssl http2;
  server_name jhin.example.com;
  ssl_certificate     /etc/letsencrypt/live/jhin.example.com/fullchain.pem;
  ssl_certificate_key /etc/letsencrypt/live/jhin.example.com/privkey.pem;
  client_max_body_size 1m;
  location / {
    proxy_pass http://127.0.0.1:3000;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_read_timeout 300s;
  }
}
```

Traefik and other proxies need the same headers and body limit. TLS is
mandatory outside loopback use.

## Docker socket modes

`sandbox-runner` is the only service that reaches Docker, always as UID/GID
10001 and never privileged. Exactly one overlay must be added to the base
manifest:

| Mode | Overlay | Host requirement | How access works |
|---|---|---|---|
| rootful | `compose.rootful.yaml` | `/var/run/docker.sock` (or another absolute, non-symlink path) owned by UID 0 with a positive numeric group | the socket's GID is passed as `SANDBOX_DOCKER_GID` and added as a supplementary group |
| rootless | `compose.rootless.yaml` | a running rootless Docker daemon owned by host UID 10001 at `PHASE10_ROOTLESS_DOCKER_SOCKET` | a minimal transport sidecar on an internal network forwards the socket; the runner matches the daemon's UID |

Startup rejects UID 0, a relative or symlinked socket path, an inaccessible
socket, a wrong owner or group, a world-writable socket, and privileged
mode. There is no fallback; repair the host Docker installation instead.
Socket preflight commands for both modes are in the README quick start.

Job containers run from `SANDBOX_DEFAULT_IMAGE` (or a grant-allowed image)
as UID 1000 with a read-only root filesystem, dropped capabilities, CPU,
memory, PID, output, and timeout limits, and either no network or the
dedicated `SANDBOX_NETWORK` bridge. The runner also reaps expired
workspaces (`SANDBOX_WORKSPACE_MAX_AGE_HOURS`, default 24).

## Sizing

Phase 10 defines five profiles. Values are host baselines for the bundled
stack (PostgreSQL, NATS, Temporal, seven Jhin services) and must be
re-measured for your model latency and sandbox workload before you rely on
them.

| Profile | Host baseline | Intended load |
|---|---|---|
| Development | 4 vCPU, 8 GiB RAM, 40 GiB SSD | one developer, one active sandbox, monitoring disabled |
| Small production | 8 vCPU, 16 GiB RAM, 100 GiB SSD | up to 10 active agents, two concurrent runs, one concurrent sandbox |
| Small + bundled monitoring | 12 vCPU, 24 GiB RAM, 150 GiB SSD | small production plus 15-day metrics and 72-hour traces |
| Medium production | 16 vCPU, 32 GiB RAM, 250 GiB SSD | up to 50 active agents, ten concurrent runs, four concurrent sandboxes |
| Medium + bundled monitoring | 24 vCPU, 48 GiB RAM, 400 GiB SSD | medium production plus local monitoring retention |

Per-job sandbox ceilings default to 2 CPUs, 4096 MiB, 256 PIDs, and 30
minutes (`SANDBOX_MAX_CPUS`, `SANDBOX_MAX_MEMORY_MB`, `SANDBOX_MAX_PIDS`,
`SANDBOX_MAX_TIMEOUT_SECONDS` on `sandbox-runner`). Disk grows mainly with
Temporal history and PostgreSQL audit/run data; plan for retention.

## Backups and restore

A complete backup covers, in this order, with the recovery point made
explicit:

1. **Quiesce** writers: `docker compose stop web api event-worker workflow-worker agent-worker tool-worker sandbox-runner`.
   Temporal, NATS, and PostgreSQL stay up.
2. **PostgreSQL** (Jhin data and Temporal persistence share the instance):
   `docker compose exec -T postgres pg_dumpall -U "$POSTGRES_USER" | gzip > jhin-$(date -u +%Y%m%dT%H%M%SZ).sql.gz`
3. **NATS JetStream state** (`nats_data` volume): archive the volume while
   the server is idle, e.g.
   `docker run --rm --volumes-from "$(docker compose ps -q nats)" -v "$PWD:/backup" alpine tar -C /data -czf /backup/nats-data.tgz .`
4. **Master key**: copy the key file to a separate, access-controlled
   location. Never bundle it with the database archive.
5. Resume: `docker compose start ...` (or `up -d --wait`).

Restore onto empty volumes: start `postgres` alone, restore the dump, restore
the `nats_data` archive, install the master key at `MASTER_KEY_FILE_HOST`,
then bring up the full stack and run `jhin-db-migrate`. Verify with the
health endpoints, by decrypting a stored credential through a connection
"verify" action in the UI, and by confirming that a task that was waiting
on an approval or timer before the backup resumes rather than restarting.
Rehearse this drill before relying on it; the Phase 10 plan
(`docs/superpowers/plans/2026-08-18-phase-10-runbooks-hardening.md`)
describes the evidence the release approval expects.

## Upgrades and migrations

1. Read the release notes and `CHANGELOG.md` for compatibility notes.
2. Take the coordinated backup above.
3. Update `JHIN_VERSION`/the bundle, then `docker compose pull`.
4. Preview migrations (current revision versus the release head):

   ```bash
   docker compose run --rm --no-deps api python -c \
     "import os; from alembic import command; from jhin_db.migrate import alembic_config; \
      c = alembic_config(os.environ['DATABASE_URL']); command.current(c); command.heads(c)"
   ```

5. Apply: `docker compose run --rm --no-deps api jhin-db-migrate`. Migrations
   are forward-only; a release that declares a forward-only migration
   promises no database downgrade.
6. Roll out in order: `sandbox-runner`, then `api`, then `workflow-worker`,
   `tool-worker`, `agent-worker`, `event-worker`, then `web`. Compose cannot
   enforce the first step for you — `tool-worker` reaches `sandbox-runner`
   over HTTP and has no `depends_on` edge to it — so take that one on its
   own and let the rest follow:

   ```bash
   docker compose up -d --wait --wait-timeout 300 sandbox-runner
   docker compose up -d --wait --wait-timeout 300
   ```

   **`sandbox-runner` goes first, and that one is a hard interlock.** A
   `tool-worker` at this version sends every sandbox job the invocation it
   belongs to, which is what lets a re-dispatched tool call attach to the
   container its first dispatch started instead of running a second one. The
   runner's job request rejects fields it does not know, so a new
   `tool-worker` against an older `sandbox-runner` does not degrade — every
   sandbox job is refused `422` and every CLI tool call fails until the
   runner catches up. The reverse pairing is safe: an older `tool-worker`
   sends no invocation, and a newer runner simply skips the ledger for it.

   **`web` goes last, and the order is not a preference.** The web app renders
   what the API tells it; a newer `web` against an older `api` is simply
   missing fields, and it is built to degrade rather than guess — a failure
   card falls back to "The run stopped before it finished." instead of the
   run's internal note, and the chat's "how long has this been thinking"
   timer shows nothing at all rather than count from the wrong clock. So the
   cost of getting this wrong is temporarily less detail, never a wrong answer
   or an internal sentence in front of a customer. Finish the rollout and the
   detail comes back; nothing needs clearing or restarting.

   *Nothing at all* is the exact claim, and it is worth stating because the
   near miss is a wrong answer rather than less detail. `active_run_working_since`
   is absent from an older `api` and `null` from a current one that measured a
   run parked on an approval nobody closed, and the chat says different things
   about those: silence for the first, "Working time unavailable" (or the
   seconds the turn banked before it stalled) plus a tooltip for the second —
   one that says only that nothing here has an instant to count from, and
   offers a pause nobody closed as one possibility among several rather than
   naming it as the cause, because the same `null` arrives for a finished run
   and for a turn with no start recorded. A `web` that treated a missing field
   as a measured one would tell every reader mid-rollout that their agents'
   work could not be timed — a sentence about *their* workspace, not about
   the deploy. `apps/web/lib/chat.ts` (`statusLabelFor`) is where the two are
   kept apart, and `apps/web/tests/chat-status-timer.test.tsx` is the gate.
7. Verify health (below) and watch in-flight workflows complete. Workflow
   code is versioned so Phase 9 histories replay on Phase 10 workers; CI
   proves this with `make test-tool-worker-live-upgrade`.

## Rolling back

Before step 5 you can revert images freely. After a migration, roll back
application images only if the release notes state the schema is backward
compatible; otherwise restore from the backup taken in step 2. Release tags
are immutable; a bad release is superseded by the next patch version.

**Do the release notes' rollback steps first, before you change a single
image.** A release that adds a state the previous version has never heard of
ships a one-shot command that clears it, and that command exists *only in the
image you are leaving* — the version you are going back to has no such module
and no such entry point. Run it while `tool-worker` still resolves to the new
image and the step is one line; revert first and the same line is
`error: unrecognized command`.

This release has exactly one such step (`tool_call.status` gained `claimed`;
see **Upgrade notes** in `CHANGELOG.md`):

```bash
# 1. still on the release you are leaving:
docker compose run --rm --no-deps tool-worker jhin-tool-calls-rollback

# 2. then revert the images and bring the stack back up
```

It is idempotent, guarded, and safe to run with the workers up, so there is no
harm in running it twice or in running it when there is nothing to close —
`--dry-run` lists what it would do.

**If you have already reverted**, the command has not become impossible, only
harder to name: run it out of the release you left, for that one call.

| How you deploy | Run |
|---|---|
| tags (`deploy/compose.release.yaml`, `JHIN_VERSION` in `.env`) | `JHIN_VERSION=<release you left> docker compose run --rm --no-deps tool-worker jhin-tool-calls-rollback` |
| a digest-pinned release bundle | the same `docker compose run` from *that release's* bundle directory, whose `compose.release.yaml` still names its own digests |
| built from source (`compose.yaml`) | the same `docker compose run` from a checkout of the release you left |

Each of these starts one short-lived container from the newer image against
the same database; nothing else in the stack is touched, and the older
workers keep running while it does. The rows it closes are read by the old
release as ordinary failed tool calls.

## Health and operations

| Endpoint or check | Meaning |
|---|---|
| `GET /api/v1/health` | liveness: app name and version |
| `GET /api/v1/health/ready` | readiness: database and Temporal reachability; `503` with a report when degraded |
| `web` healthcheck | `GET /` on port 3000 |
| `workflow-worker`, `event-worker` | `jhin-health-check` container healthcheck |
| `agent-worker`, `tool-worker` | `jhin-temporal-poller-check <queue>` proves the worker is polling `jhin-agent-queue` / `jhin-tool-queue` |
| `sandbox-runner` | `GET /health` on the internal port 8085; returns `503` while Docker is unreachable |
| `docker compose ps --all` | every service running and healthy |

Logs are structured JSON on stdout with secret redaction
(`docker compose logs -f <service>`). With `OTEL_EXPORTER_OTLP_ENDPOINT`
set, traces and metrics export to your collector with the bounded sampler
and batch settings listed in Configuration.

## Troubleshooting

| Symptom | Check |
|---|---|
| `api` unhealthy, `/health/ready` returns 503 | the readiness report names the failing dependency; confirm `postgres` and `temporal` are healthy and `DATABASE_URL`/`POSTGRES_*` agree |
| "master key" errors on `api`/`agent-worker`/`tool-worker` | the file at `MASTER_KEY_FILE_HOST` exists, is 32 bytes base64, and is readable by the container user; a key mismatch after a restore means the wrong key was installed |
| `sandbox-runner` exits at startup | the socket mode preflight failed (UID 0, symlink, wrong GID/owner, inaccessible socket, privileged mode); fix the host, do not relax permissions |
| `sandbox-runner` healthy but `cli.*` tools fail | `SANDBOX_DEFAULT_IMAGE` is not present on the daemon the runner uses; pull or build it there |
| tasks stay queued | `jhin-temporal-poller-check` fails on a worker; check worker logs and Temporal connectivity; agent concurrency limits also queue tasks visibly |
| events redelivered or duplicated | JetStream redelivery is expected after a consumer restart; dedupe layers prevent duplicate tasks; persistent redelivery means the event worker cannot reach PostgreSQL or Temporal |
| migration fails | restore from the pre-upgrade backup; run the migration with `LOG_LEVEL=DEBUG` to capture the failing revision |
| disk pressure | prune old sandbox workspaces, check Temporal history growth and PostgreSQL audit volume; never delete `postgres_data`/`nats_data` without a backup |
| webhook rejected | signature, delivery dedupe, or body size (1 MiB cap); the API logs the reason without the secret |

When reporting a problem, follow [SUPPORT.md](../SUPPORT.md) and never
include credentials or the master key.
