# Sandboxing architecture

How Jhin lets a coding agent run shell commands, tests, and repository work
without ever touching a host shell (plan sections 11.6, 13.6, 14, 21). Every
CLI tool call becomes one **job** executed in a **fresh, ephemeral, locked-
down Docker container** managed by the sandbox runner service.

## The pieces

```text
services/sandbox_runner/          jhin-sandbox-runner — internal-only FastAPI
  settings.py                     env config: token, caps, default image
  schemas.py                      SandboxJobRequest / status wire models
  jobs.py                         JobManager — container lifecycle via aiodocker
  main.py                         HTTP API: submit / status / logs / cancel /
                                  delete-workspace (+ open /health)
packages/connectors/.../cli/      CLI connector — five cli.* tools
docker/sandbox.Dockerfile         default job image (jhin-sandbox:latest)
packages/db/.../models/sandbox.py sandbox_job table — one row per job
```

Execution path:

```text
model tool call (cli.*)
 → tool gateway: schema → capability grant → scope (command/image/network/
   repository/path fnmatch) → policy/approval          [agent worker]
 → CLI executor: resolve defaults + credentials, submit job, poll,
   persist sandbox_job row + sandbox.job.* audit events [agent worker]
 → sandbox runner API (bearer token, `runner` network)  [sandbox-runner]
 → fresh container, forced removal afterwards           [Docker]
```

## Trust boundaries

**The Docker socket lives in exactly one place.** `sandbox-runner` mounts
`/var/run/docker.sock` and is therefore root-equivalent on the Docker host —
that is the trust boundary of this design. It is mitigated, not eliminated:

- the runner is on the compose `runner` network only; nothing but the agent
  worker can reach it, and it is never published to the host (compose.dev
  optionally binds `127.0.0.1:${SANDBOX_RUNNER_DEV_PORT}` for debugging);
- every job endpoint requires the shared bearer token from
  `SANDBOX_RUNNER_TOKEN` (empty token = all requests denied, fail closed);
- the runner holds **no master key, no database credentials, and no
  long-lived user secrets** — the service with Docker power has nothing
  worth stealing at rest;
- the runner's own container runs a single-purpose Python process; it never
  execs user-controlled input on its own host (commands run inside job
  containers only).

**Job containers never get the socket.** `build_container_config` produces a
fixed shape with no `Binds`/`Mounts` at all — no Docker socket, no host
paths, no control-plane secrets. This invariant is unit-tested and
integration-tested (a job that stats `/var/run/docker.sock` finds nothing).

## Job isolation (plan 14.3)

Every job container is created with:

| Control | Value |
| --- | --- |
| User | `sandbox` (uid/gid 1000) — never root |
| Root filesystem | read-only (`ReadonlyRootfs: true`) |
| Writable areas | `/workspace` (named volume) + small tmpfs `/tmp` |
| Capabilities | `CapDrop: ALL`, nothing added back |
| Privilege escalation | `no-new-privileges:true`, `Privileged: false` |
| CPU / memory / pids | request values capped at 2 CPUs / 4 GiB / 256 pids |
| Timeout | capped at 30 min; the runner force-kills on expiry |
| Network | see below |
| Cleanup | force-removed in `finally`; labeled `jhin.sandbox.job` so orphans are reaped on runner startup |

Stdout/stderr are captured with a per-stream byte cap, scrubbed of every
`secret_env` value, and only then returned/persisted.

## Network policy (plan 14.4)

Two modes:

- **`none`** — `NetworkMode: none`. No interfaces, no DNS: external hosts
  *and* compose control-plane names (postgres, temporal, nats, api) are
  unreachable.
- **`internet`** — the container joins the dedicated `jhin_sandbox` bridge
  network. No control-plane service may ever attach to that network; in
  production compose nothing references it (the runner creates it via the
  Docker API at startup). Jobs still cannot resolve `postgres`, `temporal`
  etc. because those live on different networks.

Dev/test exception: `compose.dev.yaml` attaches **fake-github** to the
sandbox network so integration tests can clone/push over git smart-HTTP and
call the fake REST API from inside jobs. That is a deliberate, dev-only
breach of "no services on the sandbox network" — never replicate it for real
services.

## Secrets in jobs (plan 13.6)

Design decision — **the caller resolves, the runner relays**:

1. The CLI executor (agent worker, which already holds the master key for
   connector credential decryption) resolves the short-lived credential —
   e.g. a GitHub PAT or a minted GitHub App installation token.
2. It sends the plaintext in `secret_env` over the internal runner network.
3. The runner injects it as container env at create time, registers the
   value for log redaction, and forgets it with the job.

The alternative (runner resolves secret refs itself) would put the master
key inside the one service that also has the Docker socket; we chose to keep
key material and Docker power in different processes.

Git credentials specifically: `cli.repository.checkout` writes a small
askpass helper into the workspace and points `GIT_ASKPASS` at it; the helper
echoes `$GIT_TOKEN` (job-scoped secret env). The token is therefore never
embedded in the git remote URL and never written to the workspace volume, so
a later `cat .git/config` cannot leak it. `cli.command.execute` re-injects
`GIT_TOKEN` on `internet` jobs so `git push` works the same way.

Redaction is layered (plan 48.9): the runner scrubs `secret_env` values from
captured output; the CLI executor additionally runs the worker's process
redactor (which knows every credential revealed this process lifetime) over
the tails before persisting `sandbox_job` rows or tool outputs.

## Workspace persistence (plan 14.5)

One job = one fresh container, but a repository checkout must survive across
the several tool calls of one agent run. Design decision: a **named Docker
volume per run** (`jhin-sbx-ws-run-<run_id>`) mounted at `/workspace` in every
job of that run. Checkouts land in `/workspace/repo`; command-style jobs
start there when it exists. The volume is deleted when the run finalizes
(agent worker → `DELETE /v1/workspaces/run-<id>`), and the runner reaps
volumes older than 24 h on startup as a backstop.

## The CLI connector (plan 11.6)

| Tool | Risk | Scope keys (all fnmatch) |
| --- | --- | --- |
| `cli.command.execute` | write, approvable | connection, command, image, network |
| `cli.repository.checkout` | write, approvable | connection, repository, image |
| `cli.test.run` | read | connection, command, image |
| `cli.file.read` | read | connection, path |
| `cli.file.write` | write, approvable | connection, path |

A CLI *connection* stores no credential — it is a named bundle of defaults
(`default_image`, `default_network`) plus `git_connection_id`, a reference
to the GitHub connection used for repository jobs. Deny-by-default applies
as everywhere: without an explicit `cli.*` allow grant an agent cannot
submit any job, and scope patterns constrain command shapes, images,
networks, repositories, and paths per capability.

Note on constrained grants: a grant that pins `image` (or `network`) only
matches calls that explicitly carry that field — calls relying on connection
defaults are denied. Fail-closed by design.

## Git in the fake environment

The Phase 6 exit test needs real `git clone`/`git push`. The `fake-github`
service serves git smart-HTTP at `/git/<owner>/<repo>.git` through a thin
CGI bridge to stock `git http-backend` (chosen over a sidecar `git daemon`
or nginx+fcgiwrap as the least complex reliable option — zero extra
services). Bare repos are seeded at startup with a failing test target
(`app.py` `VALUE = 1`, `run_tests.sh` asserting `VALUE == 2`); auth is HTTP
Basic where the password must be the same PAT the REST API accepts; pushed
branches are synced back into REST state so a PR can immediately target
them. PR creation itself still goes through the fake REST API via the
GitHub connector.

## Host requirements

- **macOS / Docker Desktop:** works out of the box. Note that "the Docker
  host" is Docker Desktop's Linux VM: resource caps apply inside the VM, and
  the VM's total resources bound everything.
- **Linux:** any standard Docker Engine.
- **Docker-in-LXC:** the plan (14.3) warns that an LXC container running
  Docker must support nested containerization securely; misconfigured
  nesting can void the isolation described here. Configure the LXC host per
  its documentation and treat it as a security-sensitive decision.

## Configuration (plan 39)

| Variable | Service | Meaning |
| --- | --- | --- |
| `SANDBOX_RUNNER_TOKEN` | runner + agent worker | shared bearer token; empty = runner refuses everything |
| `SANDBOX_RUNNER_URL` | agent worker | runner base URL (compose: `http://sandbox-runner:8085`) |
| `SANDBOX_DEFAULT_IMAGE` | runner | image when a job names none (`jhin-sandbox:latest`, built by `make sandbox-image`) |
| `SANDBOX_NETWORK` | runner | dedicated bridge for `internet` jobs (`jhin_sandbox`) |
| `SANDBOX_MAX_*` | runner | hard caps: cpus 2, memory 4096 MB, pids 256, timeout 1800 s |
| `SANDBOX_RUNNER_DEV_PORT` | compose.dev | optional 127.0.0.1 debug binding |
