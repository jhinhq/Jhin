# Sandboxing architecture

Jhin executes every `cli.*` call in a fresh, ephemeral, locked-down Docker
container. The control path is `tool-worker → sandbox-runner`; model reasoning
never receives Docker authority, runner credentials, or an executable connector
catalog.

## Components and execution path

```text
services/tool_worker/             policy, connector execution, runner client
services/sandbox_runner/          authenticated job API and Docker lifecycle
  rootless_transport.py           fixed rootless TCP-to-Unix adapter entrypoint
packages/connectors/.../cli/      five cli.* tool definitions and executors
docker/sandbox.Dockerfile         default job image (jhin-sandbox:latest)
packages/db/.../models/sandbox.py durable sandbox_job projection
```

```text
model response                                                [agent-worker]
  → atomically bind the ordered canonical tool manifest       [agent-worker]
  → schema, live grant, scope, policy, and approval            [tool-worker]
  → stable ToolCall claim and connector execution             [tool-worker]
  → authenticated internal job request                        [sandbox-runner]
  → fresh job container, then forced removal                  [Docker]
  → sanitized durable transcript/timeline projection          [agent-worker]
```

The agent process handles the model and private reasoning record. It does not
import connector executors, hold `SANDBOX_RUNNER_URL` or
`SANDBOX_RUNNER_TOKEN`, or join the Compose `runner` network. The tool worker
holds the master key needed for short-lived connector resolution and reaches
the runner over that private network. Only the runner or the fixed rootless
adapter receives Docker authority.

## Three mutually exclusive Docker modes

The base Compose file intentionally supplies no Docker endpoint. Every start,
render, recreate, or upgrade must use `compose.yaml` plus exactly one of
`compose.rootless.yaml`, `compose.rootful.yaml`, and `compose.desktop.yaml`.
Base-only and multi-overlay stacks fail the boundary contract. Operator
commands disable implicit `.env` loading so an old local mode or production
value cannot silently select authority.

| Mode | Host | Runner reaches Docker through | Extra runner group | Daemon identity check | Intended use |
| --- | --- | --- | --- | --- | --- |
| `rootless` | Linux | fixed `rootless-docker-transport` adapter on the internal `engine` network | none | `SecurityOptions` contains `name=rootless`, cgroup v2, systemd driver | servers and CI |
| `rootful` | Linux | root-owned non-symlink socket mounted at `/run/jhin/docker.sock` | exact `SANDBOX_DOCKER_GID` of that socket | not rootless | servers and CI |
| `desktop` | macOS / Windows Docker Desktop | VM socket mounted at `/run/jhin/docker.sock` (`uid 0 / gid 0`) | root group `0` | `OperatingSystem` contains `Docker Desktop`, not rootless | local development only |

In every mode, `sandbox-runner` has the exact runtime identity `10001:10001`,
drops every capability, is non-privileged, and has
`no-new-privileges:true`. Its Docker authority is validated before it creates
the sandbox network or reaps any artifact. A version probe and daemon identity
probe must pass first; failed startup closes the Docker client and performs no
mutation. `SANDBOX_DOCKER_MODE` has no default: an omitted or unknown mode is
a configuration error.

### Rootless mode

Rootless mode runs the entire stack against an already-running rootless daemon
whose host Unix socket is owned by UID 10001. Host preflight uses `lstat` and
rejects a relative path, symlink, non-socket, wrong owner, or daemon whose
security options omit `name=rootless`.

The `rootless-docker-transport` adapter alone mounts that socket. Its container
identity is `0:0`, but only inside the rootless daemon's user namespace:
container UID 0 maps to the unprivileged host daemon user. The adapter is
non-privileged, drops all capabilities, is read-only apart from `/tmp`, accepts
no arguments or Docker-related environment override, has no supplemental group, and exposes only the fixed private endpoint
`http://rootless-docker-transport:2375` on the internal `engine` network.

The adapter and runner use the exact same explicit local image tag. Operators
build `sandbox-runner` before the adapter can start, and the adapter declares
`pull_policy: never`; it cannot fetch an unreviewed image independently. The
adapter's health check performs a real Docker `GET /_ping` and requires status
200 with body `OK`. The runner starts only after that service is healthy, then
its own `/health` performs a daemon-backed ping.

Compose health alone does not restart an unhealthy container. An upstream or
copy failure therefore makes the adapter exit; `restart: unless-stopped`
relaunches it. The runner reports HTTP 503 while Docker connectivity is absent.
An explicit adapter restart also restarts the dependent runner through the
Compose dependency contract. The adapter must become healthy before runner
readiness is accepted.

### Rootful mode

Rootful mode mounts one operator-verified Docker Unix socket directly into the
runner at `/run/jhin/docker.sock`. The host path must be absolute, must not be a
symlink, must be a socket owned by UID 0, and must have an exact positive
numeric group. `SANDBOX_DOCKER_GID` must equal that `lstat` group and becomes
the runner's sole additional group authority. The runner verifies effective
read/write access. Wrong path, type, owner, GID, access, identity, or any extra
group is a fatal startup error; the deployment must repair its Docker setup,
not weaken permissions or elevate the service.

The rootful mode has no daemon-service dependency: there is no adapter or
`engine` network member, and runner health talks to the mounted socket. It does
not inherit a rootless transport URL.

### Desktop mode (macOS / Windows, local development only)

Docker Desktop runs the daemon inside a Linux VM. On the host,
`/var/run/docker.sock` is a compatibility symlink to a user-owned socket under
`~/.docker/run/`; inside any container that mounts it, the same endpoint is a
Unix socket owned by `uid 0 / gid 0`. Neither the rootful contract (real
root-owned non-symlink socket with a positive docker GID) nor the rootless
contract (host UID 10001 daemon) can be satisfied, so `desktop` is an explicit,
opt-in third mode rather than a relaxation of the other two.

`compose.desktop.yaml` mirrors the rootful overlay: it bind-mounts
`${SANDBOX_DOCKER_SOCKET_HOST:-/var/run/docker.sock}` at
`/run/jhin/docker.sock`, sets `SANDBOX_DOCKER_MODE=desktop`, supplies no
`SANDBOX_DOCKER_GID` and no transport URL, and adds exactly `group_add: ["0"]`.
The runner still runs as `10001:10001` with every capability dropped and
`no-new-privileges:true`; at startup it requires the mounted path to be an
absolute, non-symlink Unix socket owned by UID 0 and GID 0, requires the root
group to be its only supplemental group, verifies effective read/write access,
and then requires the daemon reached through that socket to report an
`OperatingSystem` containing `Docker Desktop`. A Linux host daemon, a rootless
daemon, a symlink, a foreign GID, an extra group, or a configured
`SANDBOX_DOCKER_GID` is a fatal startup error. Job constraints (network policy,
identity, limits, cleanup, secret redaction) are identical to the other modes.

Host preflight in the harness and in `assert_phase10_tool_worker_compose.py
--mode desktop` is the one place a symlink is accepted: the configured path is
resolved to its real socket target, that target becomes the immutable
snapshotted authority and the bind-mount source, and `docker info` must report
Docker Desktop and no `name=rootless` option.

**Desktop harness caveats.** Two behaviours of the Docker Desktop daemon
differ from a Linux daemon and are handled explicitly rather than skipped:
the runner container reports `GroupAdd: ["0"]` (the root group, because no
socket GID exists), which the live boundary assertions expect only in
`desktop` mode; and BuildKit may assign distinct image IDs to identical
per-service builds that run in parallel, so the upgrade overlay gives every
current worker of one kind one explicit `image` tag
(`jhin-phase10-{agent,tool}-worker:<token>`) that the harness builds exactly
once before recreating the four services with `--no-build`.

**Threat-model caveat.** Root-group membership inside the runner container is
strictly weaker than the rootful exact-GID or the rootless user-namespace
boundary: any file in the runner image that is group-`0` writable becomes
reachable, and the socket grants full control of the Desktop VM daemon, which
already belongs to the developer. That is acceptable for a single developer's
laptop and nothing else. Never select `desktop` on a shared host, a server, or
CI; the mode exists so that `make test-integration PHASE10_MODE=desktop` and
the full stack (including CLI sandbox jobs) run on Docker Desktop with the
same fail-closed startup contract.

### Readiness

Startup uses bounded `up -d --build --wait --wait-timeout` and then `ps --all`
with the exact service set for the selected mode. Rootless requires
`rootless-docker-transport` in addition to the base production services;
rootful and desktop forbid it. Every returned row must be running, and every service with
a health check must be healthy. Absence, duplicate rows, exited services,
blank health, or extra mode-specific services fails closed. Phase 10 Task 10
owns the current-image live start, recreate, crash, and upgrade acceptance
commands; static render success alone is not live acceptance.

## Network and endpoint isolation

Compose owns explicit nonexternal bridge networks. The global `runner` network
contains exactly the tool worker and runner. In rootless mode, the internal
`engine` network contains exactly the adapter and runner. The API, agent,
workflow, and event workers join neither network.

Job containers receive no socket, adapter endpoint or DNS, engine or runner network, Docker-related environment authority, or supplemental group. They
cannot resolve the tool worker, adapter, runner, agent worker, API, database,
NATS, or Temporal by control-plane DNS. Host inspection in the live security
gate checks the actual container's user, network mode, `GroupAdd`, environment,
mounts, and exact job label instead of relying on an in-container guess.

Two job network policies are available:

- **`none`** uses Docker `NetworkMode: none`; the job has no external or
  control-plane network access.
- **`internet`** joins the configured dedicated sandbox bridge. The name must
  satisfy the bounded safe grammar, must not be a Docker reserved/container
  mode, and must not alias any authority network. No production control service
  is attached to it.

The dev overlay attaches only `fake-github` to the sandbox network for
integration tests. This test exception must never be copied to a production
service.

## Job isolation

Every job container is created with this fixed security shape:

| Control | Value |
| --- | --- |
| User | exact `1000:1000` |
| Root filesystem | read-only |
| Writable storage | one named per-run workspace volume, plus bounded tmpfs |
| Capabilities | `CapDrop: ALL`, nothing added |
| Privilege | non-privileged and `no-new-privileges:true` |
| Groups | empty `GroupAdd` |
| CPU / memory / pids | capped at 2 CPUs / 4 GiB / 256 pids |
| Timeout | capped at 30 minutes, then force-killed |
| Host authority | no host bind, Docker socket, adapter URL, or control network |
| Cleanup | force-removed in `finally`, with startup orphan reaping by exact label |

One job gets one fresh container. A repository checkout persists only in the
named volume `jhin-sandbox-ws-run-<run_id>`, mounted at `/workspace`. Tool-worker
requests `DELETE /v1/workspaces/run-<id>` before the agent-side final projection;
the deletion is idempotent and startup reaping removes old volumes as a
backstop.

Stdout and stderr have independent byte caps. The runner registers every
job-scoped secret value, redacts it before returning output, and forgets it with
the job. Tool-worker applies its process redactor again before persisting a
`sandbox_job` row or tool result.

## Secret split

The caller resolves and the runner relays:

1. Tool-worker reloads the authorized connection and decrypts or mints the
   short-lived credential.
2. It sends the value in `secret_env` over the private runner network.
3. The runner injects it only while creating the job, redacts captured output,
   and retains no master key or database credential.

This keeps secret-store authority and Docker authority in different processes.
Git checkout uses a workspace askpass helper that reads the job-scoped
`GIT_TOKEN`; the token is not embedded in a remote URL or repository config.

## CLI connector policy

| Tool | Risk | Scope keys (all fnmatch) |
| --- | --- | --- |
| `cli.command.execute` | write, approvable | connection, command, image, network |
| `cli.repository.checkout` | write, approvable | connection, repository, image |
| `cli.test.run` | read | connection, command, image |
| `cli.file.read` | read | connection, path |
| `cli.file.write` | write, approvable | connection, path |

A CLI connection stores defaults and an optional GitHub connection reference,
not a plaintext credential. Deny-by-default remains in force. A grant that
pins `image` or `network` matches only a call that explicitly carries that
field; relying on a connection default does not broaden the grant.

## Giving an agent file editing

An agent edits code only inside a sandbox job, and the change reaches the
repository only through `git push` from that sandbox. The minimum setup is:

1. **Connections.** A `github` connection (PAT or app) for the repository, and
   a `cli` connection (auth type `none`) whose `git_connection_id` points at it.
   Leave the CLI connection's `default_network` at `none`; pushes opt into the
   bridge per call.
2. **Grants** (Tools & Access on the agent, or the wizard's **Code editing**
   preset which issues exactly these with `*` scopes):

   | Capability | Scope | Why |
   | --- | --- | --- |
   | `cli.repository.checkout` | `connection_id`, `repository` | clone + create the `agent/<task>-<repo>` branch |
   | `cli.file.read` | `connection_id`, `path` | read files in the checkout |
   | `cli.file.write` | `connection_id`, `path` | edit files in the checkout |
   | `cli.test.run` | `connection_id`, `command` | run the test command |
   | `cli.command.execute` | `connection_id`, `command: "git *"` | `git add/commit/push` with `network: "internet"` |
   | `github.repository.read` | `connection_id`, `repository` | inspect the repository |
   | `github.pull_request.create` | `connection_id`, `repository` | open the PR from the pushed branch |

   Do not pin `network` in the `cli.command.execute` grant unless every call
   carries it; the agent passes `network: "internet"` only for the push.
3. **Step budget.** A checkout → read → write → test → commit+push → PR →
   report flow takes seven or more steps; give the agent at least 12.

What the agent sees: the checkout returns the working branch; `cli.file.write`
reminds it that the change is sandbox-only until pushed; the fake GitHub (like
real GitHub) refuses a pull request whose head has no commits beyond the base,
so a branch created through the refs API without a push cannot produce an
empty PR. The credential for the push is the GitHub connection's short-lived
token, injected as job-scoped `GIT_TOKEN` and consumed by the askpass helper —
it is never written to the workspace or the run record.

## Configuration ownership

| Variable | Owner | Meaning |
| --- | --- | --- |
| `SANDBOX_RUNNER_TOKEN` | tool-worker + runner | bearer token; an empty value denies requests |
| `SANDBOX_RUNNER_URL` | tool-worker | fixed internal runner base URL |
| `SANDBOX_DEFAULT_IMAGE` | tool-worker + runner | reviewed default job image |
| `SANDBOX_NETWORK` | runner | unique dedicated bridge for `internet` jobs |
| `SANDBOX_RUNNER_IMAGE` | Compose | identical local runner/adapter image tag |
| `PHASE10_ROOTLESS_DOCKER_SOCKET` | rootless overlay | verified host-UID-10001 daemon socket; no GID applies |
| `SANDBOX_DOCKER_SOCKET_HOST` | rootful + desktop overlays | rootful: verified absolute root-owned non-symlink socket; desktop: resolved Docker Desktop socket (default `/var/run/docker.sock`) |
| `SANDBOX_DOCKER_GID` | rootful overlay | exact positive numeric group of that socket; forbidden in desktop mode |
| `PHASE10_DESKTOP_DOCKER_SOCKET` | desktop harness preflight | host path (symlink allowed) resolved to the real Docker Desktop socket |
| `SANDBOX_MAX_*` | runner | hard CPU, memory, pids, timeout, and output caps |

The runner and adapter receive no master key, database DSN, NATS credentials,
or connector allowlist. Job requests are filtered so `DOCKER_*`,
`SANDBOX_DOCKER_*`, known socket paths, and adapter endpoint values cannot be
forwarded even if a caller attempts to provide them.

## Host support

The two server-grade authority modes are Linux rootless Docker with host UID
10001 and Linux rootful Docker with a real root-owned non-symlink socket. A
Docker Desktop compatibility symlink does not satisfy the rootful contract; on
macOS or Windows use the explicit, development-only `desktop` mode described
above. Nested Docker-in-LXC is safe only when the outer host provides secure
nested-container isolation; a permissive nesting configuration can void every
guarantee above.
