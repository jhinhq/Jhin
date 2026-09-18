"""Which disk an agent's sandbox jobs run on, and who is holding it.

Until this module existed every sandbox workspace was named ``run-<run_id>``
and destroyed when the run finalized. That is correct and useless: an agent
re-cloned the repository, re-installed its dependencies and threw away every
build artefact between one message and the next, so "make a change, run the
tests, fix it, push it" paid for a cold start at every step.

The durable version is one row per volume and one rule per hard question.

**Isolation.** The key is derived from identity alone --
``agent-<workspace_id.hex>-<agent_id.hex>`` -- and nothing a model says
contributes a byte. ``ctx.agent_id`` is read by the tool worker from the
``agent_run`` row (never from tool input) and re-checked against the bound
manifest authority, so an agent cannot name another agent's disk because it
never names a disk at all. Two agents therefore produce two keys, two keys
produce two Docker volumes, and a job is given exactly one volume mount and no
Docker socket. The tenant id is redundant with the (globally unique) agent id
and is in the key anyway: it makes ``docker volume ls`` self-describing and
makes a cross-tenant collision impossible by construction rather than by an
argument about uuid uniqueness.

**Concurrency: separate, never share, never switch mid-run.** Two runs of one
agent can overlap -- a chat turn while a task runs, or a self-delegating
sub-run. Exactly one holds the agent's durable workspace; the other gets a
private ``run-<run_id>`` volume, which is precisely today's behaviour with
today's guarantees. Nothing is shared and nothing waits. Sharing one tree
between two runs would be a half-written checkout, a git index race and a
``git clean`` that deletes the other run's files; serialising would park a chat
turn behind a long task's entire run, including the hours it may sit on a push
approval.

A run can never straddle two disks: the first statement of every bind renews
``WHERE workspace_id = :ws AND holder_run_id = :run``, so a run that already
holds *any* binding gets back the key it got last time, and no code path
re-decides. A lease is never taken from a live run, because the acquire
predicate reads liveness from the holder's own ``agent_run.status`` rather than
from a clock -- the TTL expiring means nothing on its own. The lease ceiling
exists only for a run that crashed so hard it never reached a terminal status.
If a lease *is* taken that way and the loser turns out to be alive, its next
bind refuses with ``workspace_lease_lost`` rather than silently continuing on a
fresh disk with its checkout on the other one.

**Bounds.** A workspace is only ever destroyed at bind time, before any
container of the new run starts -- the one moment it is provably idle. A
workspace that crosses its cap *while a run is using it* is never destroyed;
the next call is refused with ``workspace_full`` instead, because throwing away
a running agent's uncommitted work to reclaim disk is the one trade that is
never worth making. An eviction is recorded only once the runner says the
volume is gone: a row that claims a disk is empty when it is not takes that
disk out of the cap's sight and out of every future sweep's, which is a worse
outcome than an eviction that has to be retried.

**A size Jhin does not have is not a small size.** The cap is an accounting
cap -- this daemon has no filesystem quota to fall back on -- so it is a bound
only while the number under it is the disk's. A walk that did not finish
reports a *floor*, and a floor read as an answer is how a 6.5 GB disk came
back as 28 MB, under every cap in the product, recycling nothing and refusing
nothing. So a floor is carried as :data:`SIZE_UNKNOWN` and the platform
**refuses** on it (``workspace_unmeasured``) rather than guessing: a false
refusal costs a run, a false eviction costs an agent's unpushed work, and the
two are not comparable. The floor is still worth keeping -- a floor above the
cap *proves* the disk is over it, which is the one thing an incomplete walk
can prove, and the tenant budget charges an unmeasurable disk the larger of
its floor and the per-agent cap, because a disk nobody can count is not a disk
holding nothing. That charge can make a bind wait; it never makes a
neighbour's tree disappear. :func:`_sweep` plans what to destroy against the
bytes this table has actually seen and decides what to refuse against the
bytes it cannot rule out, which is the same principle again: only one of
those two decisions is allowed to rest on a number nobody has.

**One definition of "held".** Every path that destroys asks whether a run
still has the workspace, and they all ask :func:`_holder_finished`: the
holder's own ``agent_run.status``, never the mere presence of a
``holder_run_id``. When those two readings disagreed -- a run that finished
without finalizing leaves its id on the row -- ``_acquire`` handed the disk
out while the sweep treated it as untouchable, so the row was excluded from
every eviction path at once and one stale id was enough to put a whole tenant
permanently over budget with nothing it could free.

The same trade decides the tenant-wide budget, and it is the whole of
:func:`_sweep`'s policy: **destroying another agent's durable work is only
justified when it achieves the thing it is destroying work for.** A sweep that
cannot bring the tenant under budget with the workspaces it is allowed to
touch takes *nothing* and says so, and the bind is refused with
``workspace_tenant_full``. Anything else is the worst of both: the ordinary
case is an overspender that is *held* -- overlapping runs, or a run parked on
an approval for a day -- and a sweep that cannot reach it used to destroy
every small unheld neighbour instead, free a rounding error, leave the tenant
over budget and refuse nothing. The structural half is
:func:`max_workspace_bytes`, which never exceeds the tenant budget: one agent
alone can then no longer put the tenant somewhere no sweep can rescue it
from.

**History.** A durable disk outlives the record of what was put on it, so the
disk's own history is a question this module answers
(:func:`workspace_repositories`): every repository checked out here since the
volume was last actually emptied. The allow-list validator asks it, because
"what did the last checkout name" describes one path and a disk is not a path.
The history ends when the disk does -- the volume destroyed, or a checkout that
wiped the whole workspace -- and both of those are facts written by Jhin at the
moment they happened, into a table no sandbox job can reach.

**Degrading safely.** Binding needs a durable store, which needs
``ctx.session_factory``. The tool worker always sets it. Where it is absent
this module returns the run-scoped key with no row at all -- no durable disk,
no lease, no sharing -- which is the fail-safe direction: without a binding you
cannot prove who owns a shared disk, so you do not take one.
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from jhin_connectors.cli.runner_client import delete_workspace as delete_runner_workspace
from jhin_db.models import AgentRun, AuditEvent, Conversation, SandboxWorkspace, Task
from jhin_domain import ActorType
from jhin_tools.errors import ToolExecutionError

#: How a volume is destroyed. Injected so the eviction policy can be proven
#: without a runner, and so the tool worker's own client is the only default.
DeleteWorkspace = Callable[[str], Awaitable[bool]]

#: The two kinds of workspace. ``agent`` survives a run; ``run`` dies with it.
KIND_AGENT = "agent"
KIND_RUN = "run"

STATE_ACTIVE = "active"
STATE_EVICTED = "evicted"

#: ``size_bytes`` is this disk's usage, to the byte: the walk finished.
SIZE_MEASURED = "measured"
#: ``size_bytes`` is only a floor: the walk stopped early, so the disk holds
#: *at least* that much and nothing more is known. Never read as a size --
#: see :func:`_charge` for what it costs a budget and ``bind_workspace`` for
#: why the answer is a refusal rather than a guess.
SIZE_UNKNOWN = "unknown"

#: Audit actions. These are ``audit_event.action`` values -- a free-form
#: column -- deliberately, not structlog event names: the observability event
#: registry is a closed vocabulary and a durable disk's history belongs in the
#: append-only table an operator already reads, next to
#: ``sandbox.checkout.recorded``.
AUDIT_BOUND = "sandbox.workspace.bound"
AUDIT_RELEASED = "sandbox.workspace.released"
AUDIT_EVICTED = "sandbox.workspace.evicted"
AUDIT_RECYCLED = "sandbox.workspace.recycled"
AUDIT_RESET_REQUESTED = "sandbox.workspace.reset_requested"
#: A bind refused because the tenant is over its total budget and nothing the
#: sweep may destroy adds up to the overrun. Its own action rather than a
#: ``bound`` event with a flag, because nothing was bound -- and a decision to
#: destroy nothing has to be as visible in this table as a decision to destroy
#: something, or "the sweep took nothing" is indistinguishable from "no sweep
#: ran".
AUDIT_BUDGET_REFUSED = "sandbox.workspace.budget_refused"
#: A bind refused because nothing knows how big this disk is. Recorded for the
#: same reason as the budget refusal: the workspace was left exactly as it was
#: found, and "the platform declined to guess" has to be legible to the
#: operator who will have to reset it.
AUDIT_SIZE_REFUSED = "sandbox.workspace.size_refused"

#: Jhin's own account of a checkout that landed on a disk. Defined here rather
#: than beside the tool that writes it because it is half of what this module
#: knows about a workspace: the other half of "how big is this disk" is "what
#: has been on it".
AUDIT_CHECKOUT_RECORDED = "sandbox.checkout.recorded"

#: The audit target type a checkout record is keyed on now that the disk it
#: describes outlives the run that made it.
CHECKOUT_TARGET_WORKSPACE = "sandbox_workspace"
#: What a checkout record was keyed on before durable workspaces, and still is
#: for a call with no durable binding.
CHECKOUT_TARGET_RUN = "agent_run"

#: Statuses that mean the holder will make no further tool calls, so its lease
#: can be taken without any chance of interleaving with a live job.
_TERMINAL_RUN_STATUSES = ("completed", "failed", "cancelled")

#: Sort position for a row with no ``last_used_at``. Only ever a tie-break, so
#: it decides nothing on its own; a row with no use age is not idle-evictable
#: at all.
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

WORKSPACE_REFUSAL_HINTS: dict[str, str] = {
    "workspace_lease_lost": (
        "Another run took over this agent's workspace while this one was idle, "
        "so the files this run was working on are no longer the ones on disk. "
        "Check the repository out again."
    ),
    "workspace_full": (
        "The sandbox workspace is full. Push the branch you have with "
        "cli.repository.push, or ask an operator to run "
        "`jhin-admin agent workspace reset` for this agent."
    ),
    "workspace_tenant_full": (
        "The sandbox disks in this workspace are together over their budget, "
        "and the disks holding that space belong to runs that have not "
        "finished, so nothing can be freed yet. Push the branch you have with "
        "cli.repository.push; the space is released when those runs finish, "
        "or when an operator runs `jhin-admin agent workspace reset` for the "
        "agent holding it."
    ),
    "workspace_unmeasured": (
        "The sandbox workspace could not be measured, so there is no way to "
        "tell whether it is within its size limit, and the platform will not "
        "guess. This is what a tree of very many files does to the "
        "measurement. Push the branch you have with cli.repository.push, then "
        "ask an operator to run `jhin-admin agent workspace reset` for this "
        "agent: the next call after that starts from an empty disk."
    ),
}


def _int_env(name: str, default: int) -> int:
    """A positive integer setting from the environment, or the default.

    Read at call time rather than at import, matching ``runner_client``: these
    have to be right with nothing configured, because the values that ship are
    the values a live stack runs until somebody edits compose.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def max_workspace_bytes() -> int:
    """Per-agent cap. 5 GiB holds a checkout, a virtualenv and a build tree.

    Never more than the tenant's whole budget, whatever the two settings say
    on their own. This is the structural half of the eviction policy: a
    per-agent cap above the tenant total lets one agent put its tenant over
    budget by itself, and if that agent is *holding* its workspace -- an
    overlapping run, or a run parked on an approval -- no sweep can bring the
    tenant back under, so every other agent in the tenant is refused for a
    situation none of them caused and none of them can fix. Bounded here, an
    agent's own bind recycles its own disk before it can reach that point,
    which is the one place the cost lands on whoever spent it.
    """
    configured = _int_env("SANDBOX_WORKSPACE_MAX_MB", 5_120) * 1024 * 1024
    return min(configured, total_workspace_bytes())


def total_workspace_bytes() -> int:
    """Cap across one tenant's durable workspaces.

    Per tenant, not per install: the sweep that enforces it runs on an agent's
    bind, and one tenant's agent may not free disk by deleting another
    tenant's work.
    """
    return _int_env("SANDBOX_WORKSPACE_TOTAL_MAX_MB", 40_960) * 1024 * 1024


def idle_eviction() -> timedelta:
    return timedelta(days=_int_env("SANDBOX_WORKSPACE_IDLE_DAYS", 7))


def lease_ttl() -> timedelta:
    return timedelta(minutes=_int_env("SANDBOX_WORKSPACE_LEASE_MINUTES", 240))


def lease_ceiling() -> timedelta:
    """How far past its TTL a lease must be before a *non-terminal* holder can
    lose it. This is a liveness backstop for a run that crashed without ever
    finalizing, never the primary mechanism -- which is the holder's own
    status."""
    return timedelta(hours=_int_env("SANDBOX_WORKSPACE_LEASE_CEILING_HOURS", 24))


def agent_workspace_key(workspace_id: UUID, agent_id: UUID) -> str:
    """The durable key for one agent. 71 characters, inside the runner's
    81-character ``WORKSPACE_KEY_RE``."""
    return f"{KIND_AGENT}-{workspace_id.hex}-{agent_id.hex}"


def run_workspace_key(run_id: UUID) -> str:
    """The run-scoped key, spelled exactly as it was before durable workspaces
    existed so that volumes, tests and documentation from before this change
    stay true."""
    return f"{KIND_RUN}-{run_id}"


@dataclass(frozen=True)
class WorkspaceBinding:
    """The disk one run is pinned to, decided once and then only re-read."""

    key: str
    kind: str
    #: What a ``sandbox.checkout.recorded`` row is keyed on. A durable binding
    #: keys the record to the *disk* it describes, so tomorrow's chat turn can
    #: push what yesterday's task checked out; an undurable one keys it to the
    #: run, exactly as before.
    record_target_type: str
    record_target_id: UUID
    #: True when a second concurrent run of this agent found the durable
    #: workspace held and took a private one instead.
    contended: bool = False
    #: True when this binding is backed by a ``sandbox_workspace`` row.
    durable: bool = False
    #: The row id, for the size write-back after the job.
    row_id: UUID | None = None


def _refusal(code: str) -> ToolExecutionError:
    """A named refusal raised before any container starts, so "no side effect"
    is a fact about when it happened rather than a claim about what ran."""
    return ToolExecutionError(
        code,
        code=code,
        side_effect_possible=False,
        hint=WORKSPACE_REFUSAL_HINTS.get(code, ""),
    )


def _as_utc(value: datetime | None) -> datetime | None:
    """Stored timestamps come back naive on SQLite and aware on Postgres; the
    comparisons below must not depend on which."""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _holder_finished() -> sa.ColumnElement[bool]:
    """Correlated EXISTS: this row's holder run has reached a terminal status.

    The single definition of "the holder is done with it", used by the acquire,
    by the eviction claim and by the sweep's candidate scan, because those
    three disagreeing is a state nothing can get out of. ``_sweep`` used to
    decide it by ``holder_run_id IS NULL`` while ``_acquire`` decided it by the
    holder's status; a run that finished without finalizing leaves its id on
    the row, so the acquire handed that disk out while the sweep treated it as
    untouchable -- excluded from the idle pass, excluded from the budget pass,
    and counted in the total it could not be used to reduce. One such row put
    a whole tenant permanently over budget with nothing any bind could free.
    """
    return (
        sa.select(sa.literal(1))
        .select_from(AgentRun)
        .where(
            AgentRun.id == SandboxWorkspace.holder_run_id,
            AgentRun.status.in_(_TERMINAL_RUN_STATUSES),
        )
        .exists()
    )


def _unheld() -> sa.ColumnElement[bool]:
    """No live run has this workspace, so destroying it interleaves with
    nothing. The lease being *free* and the holder being *finished* are the
    same fact to everything downstream."""
    return sa.or_(SandboxWorkspace.holder_run_id.is_(None), _holder_finished())


def _held_by_a_live_run(holder_run_id: UUID | None, holder_status: str | None) -> bool:
    """:func:`_unheld` decided in Python, from an outer join onto the holder.

    Deliberately the same reading, including the unlikely corner: a
    ``holder_run_id`` with no matching row counts as *held*, exactly as the
    EXISTS above makes it, so the two never disagree about a row neither of
    them can explain.
    """
    if holder_run_id is None:
        return False
    return holder_status not in _TERMINAL_RUN_STATUSES


def _charge(size_bytes: int | None, size_state: str) -> int:
    """What one workspace costs the tenant's budget.

    A measured disk costs what it holds. An unmeasurable one costs the larger
    of the floor its walk reached and the per-agent cap: the cap is the most a
    workspace is *allowed* to hold, so charging it is the conservative reading
    of a disk nobody could count, and it cannot exceed the tenant budget by
    itself (:func:`max_workspace_bytes` is bounded by it). Charging zero is how
    a disk that is invisible to the measure gets to be invisible to the budget
    as well, which is the same defect as a run-scoped workspace the sweep
    never selected: real bytes on a real disk, spent against nobody's account.
    """
    size = max(int(size_bytes or 0), 0)
    if size_state == SIZE_UNKNOWN:
        return max(size, max_workspace_bytes())
    return size


def _audit(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    agent_id: UUID | None,
    action: str,
    row_id: UUID,
    metadata: dict[str, object],
) -> None:
    session.add(
        AuditEvent(
            workspace_id=workspace_id,
            actor_type=ActorType.AGENT.value,
            actor_id=agent_id,
            action=action,
            target_type=CHECKOUT_TARGET_WORKSPACE,
            target_id=row_id,
            metadata_json=metadata,
        )
    )


async def _renew(
    session: AsyncSession, *, workspace_id: UUID, run_id: UUID, now: datetime
) -> sa.Row[tuple[UUID, str, str, int, str]] | None:
    """The fast path, and the reason a run can never change disks.

    Any binding this run already holds -- durable or private -- is renewed and
    returned. Nothing here re-evaluates *which* disk; the key was decided on
    the first ``cli.*`` call of the run and this only extends the lease.
    """
    result = await session.execute(
        sa.update(SandboxWorkspace)
        .where(
            SandboxWorkspace.workspace_id == workspace_id,
            SandboxWorkspace.holder_run_id == run_id,
        )
        .values(lease_expires_at=now + lease_ttl(), last_used_at=now)
        .returning(
            SandboxWorkspace.id,
            SandboxWorkspace.workspace_key,
            SandboxWorkspace.kind,
            SandboxWorkspace.size_bytes,
            SandboxWorkspace.size_state,
        )
        .execution_options(synchronize_session=False)
    )
    return result.first()


async def _lost_the_lease(
    session: AsyncSession, *, workspace_id: UUID, agent_id: UUID, run_id: UUID
) -> bool:
    """Did this run hold the agent workspace and lose it?

    Reached when an operator ran ``jhin-admin agent workspace reset --force``
    on a workspace this run was holding. Two shapes, and both have to fail
    here, because the alternative in either is drifting onto a second tree
    with the checkout on the first one -- not a degradation, a wrong answer:

    * somebody else has already taken the lease, or
    * the lease is free and a reset is still pending on it, which is the
      operator's release before anyone else has bound. Without this second
      clause the displaced run would simply re-acquire its own workspace, and
      the very next statement would recycle it -- deleting the run's own tree
      underneath it and continuing as if nothing had happened.

    An ordinary release at the end of a run also leaves ``last_holder_run_id``
    naming that run, which is why a pending reset (never written by a release)
    is what separates the two.
    """
    found = await session.scalar(
        sa.select(SandboxWorkspace.id).where(
            SandboxWorkspace.workspace_id == workspace_id,
            SandboxWorkspace.agent_id == agent_id,
            SandboxWorkspace.kind == KIND_AGENT,
            SandboxWorkspace.last_holder_run_id == run_id,
            sa.or_(
                sa.and_(
                    SandboxWorkspace.holder_run_id.is_not(None),
                    SandboxWorkspace.holder_run_id != run_id,
                ),
                sa.and_(
                    SandboxWorkspace.holder_run_id.is_(None),
                    SandboxWorkspace.reset_requested_at.is_not(None),
                ),
            ),
        )
    )
    return found is not None


async def _ensure_agent_row(session: AsyncSession, *, workspace_id: UUID, agent_id: UUID) -> None:
    """Create the agent's row if it has none yet.

    The partial unique index is the arbiter, so two binds racing the first call
    of an agent's life end with one row and one loser that simply re-reads.
    """
    existing = await session.scalar(
        sa.select(SandboxWorkspace.id).where(
            SandboxWorkspace.workspace_id == workspace_id,
            SandboxWorkspace.agent_id == agent_id,
            SandboxWorkspace.kind == KIND_AGENT,
        )
    )
    if existing is not None:
        return
    session.add(
        SandboxWorkspace(
            workspace_id=workspace_id,
            agent_id=agent_id,
            kind=KIND_AGENT,
            workspace_key=agent_workspace_key(workspace_id, agent_id),
            state=STATE_ACTIVE,
        )
    )
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()


async def _acquire(
    session: AsyncSession, *, workspace_id: UUID, agent_id: UUID, run_id: UUID, now: datetime
) -> sa.Row[tuple[UUID, str, int, str, str, datetime | None, datetime | None]] | None:
    """Take the agent's durable workspace, or return None because another run
    holds it.

    One statement, so two concurrent binds cannot both win: Postgres serialises
    the row update and the loser sees zero rows.

    **The predicate does not consult a clock at all.** A holder is displaceable
    only when its own ``agent_run`` row says it is finished -- :func:`_unheld`,
    the same predicate the eviction claim and the sweep's candidate scan ask,
    because a workspace this can take and the sweep cannot reach is a
    workspace nothing can free -- or when the row is gone, which the
    ``SET NULL`` foreign key turns into a free lease. There
    used to be a fourth way in: a lease more than the ceiling past its TTL
    could be taken from a holder in *any* state. That contradicted this
    module's own rule and it was not a safe reading of the world, because
    nothing in ``agent_run`` moves while a run is alive. A run parked on a push
    approval for two days is indistinguishable from a run that crashed without
    finalizing, and taking the disk from the first one means deleting a tree
    somebody is about to approve a push from.

    Losing this race costs a cold workspace and nothing else: the contender
    gets a private run-scoped disk, which is exactly what every sandbox job had
    before this module existed. A workspace genuinely stranded by a run that
    never finalized is an operator's ``jhin-admin agent workspace reset``,
    which is a deliberate act by somebody who can see the run.

    ``last_used_at`` is deliberately *not* stamped here: RETURNING hands back
    post-update values, and the idle policy has to see when the disk was last
    touched rather than the instant it was just claimed. The caller stamps it
    once the policy has run, by which point the row is held and nobody else can
    be looking at it.

    ``last_holder_run_id`` is written from the row's *own* pre-update holder in
    the same statement, which is what lets a displaced run be told it was
    displaced. Without it a run whose lease was taken past the ceiling would
    find no binding, take a fresh one, and carry on with its checkout on the
    other disk -- the exact silent drift ``workspace_lease_lost`` exists to
    prevent. The coalesce keeps the last real holder when the lease was free,
    so an operator reading the row still sees who used it last.
    """
    result = await session.execute(
        sa.update(SandboxWorkspace)
        .where(
            SandboxWorkspace.workspace_id == workspace_id,
            SandboxWorkspace.agent_id == agent_id,
            SandboxWorkspace.kind == KIND_AGENT,
            sa.or_(SandboxWorkspace.holder_run_id == run_id, _unheld()),
        )
        .values(
            last_holder_run_id=sa.func.coalesce(
                SandboxWorkspace.holder_run_id, SandboxWorkspace.last_holder_run_id
            ),
            holder_run_id=run_id,
            lease_expires_at=now + lease_ttl(),
        )
        .returning(
            SandboxWorkspace.id,
            SandboxWorkspace.workspace_key,
            SandboxWorkspace.size_bytes,
            SandboxWorkspace.size_state,
            SandboxWorkspace.state,
            SandboxWorkspace.reset_requested_at,
            SandboxWorkspace.last_used_at,
        )
        .execution_options(synchronize_session=False)
    )
    return result.first()


async def _private_binding(
    session: AsyncSession, *, workspace_id: UUID, agent_id: UUID, run_id: UUID, now: datetime
) -> WorkspaceBinding:
    """A run-scoped workspace for a run that lost the contention.

    It behaves exactly as every sandbox workspace behaved before this module:
    cloned fresh, deleted at finalize. What is lost is the warm cache, not a
    capability -- and the ``contended`` flag on the audit event says so, so the
    cost is visible rather than mysterious.
    """
    key = run_workspace_key(run_id)
    row = SandboxWorkspace(
        workspace_id=workspace_id,
        agent_id=agent_id,
        kind=KIND_RUN,
        workspace_key=key,
        run_id=run_id,
        holder_run_id=run_id,
        lease_expires_at=now + lease_ttl(),
        last_used_at=now,
        state=STATE_ACTIVE,
    )
    session.add(row)
    _audit(
        session,
        workspace_id=workspace_id,
        agent_id=agent_id,
        action=AUDIT_BOUND,
        row_id=row.id,
        metadata={
            "run_id": str(run_id),
            "workspace_key": key,
            "kind": KIND_RUN,
            "contended": True,
        },
    )
    await session.commit()
    return WorkspaceBinding(
        key=key,
        kind=KIND_RUN,
        record_target_type=CHECKOUT_TARGET_WORKSPACE,
        record_target_id=row.id,
        contended=True,
        durable=True,
        row_id=row.id,
    )


async def _recycle(
    session: AsyncSession,
    *,
    row_id: UUID,
    workspace_id: UUID,
    agent_id: UUID,
    key: str,
    reason: str,
    action: str,
    now: datetime,
    delete_workspace: DeleteWorkspace | None = None,
) -> bool:
    """Destroy the volume behind one binding and record why. True when it went.

    Called only from a bind, before any container of the new run starts, which
    is the one moment nothing is using the disk. Failure is *open*: the bind
    proceeds with ``deleted: false`` in the audit trail and the next bind tries
    again, because a runner that is momentarily unhappy must not strand an
    agent that has work to do.

    "The next bind tries again" is the promise, and keeping it is the reason
    the row is only cleared when the volume actually went. Clearing
    ``reset_requested_at`` on a delete the runner refused forgot the
    operator's request entirely: the tree the operator asked to be emptied was
    still there, nothing said so, and the only sign was one ``deleted: false``
    in an audit row nobody was reading. Size is left alone for the same
    reason -- a workspace that is still full should not be recorded as empty,
    or the cap stops biting on the very disk that failed to be cleared.

    A volume that *did* go leaves a disk whose size is known rather than
    merely small: the volume no longer exists, so ``measured, 0`` is a fact
    and not an assumption, and it is what lets a workspace nobody could
    measure come back into service instead of refusing its agent forever.
    """
    remove = delete_workspace or delete_runner_workspace
    try:
        deleted = bool(await remove(key))
    except Exception:
        deleted = False
    cleared: dict[str, object] = (
        {
            "size_bytes": 0,
            "size_state": SIZE_MEASURED,
            "size_measured_at": now,
            "reset_requested_at": None,
            "reset_requested_by": None,
        }
        if deleted
        else {}
    )
    await session.execute(
        sa.update(SandboxWorkspace)
        .where(SandboxWorkspace.id == row_id)
        .values(state=STATE_ACTIVE, **cleared)
        .execution_options(synchronize_session=False)
    )
    _audit(
        session,
        workspace_id=workspace_id,
        agent_id=agent_id,
        action=action,
        row_id=row_id,
        metadata={
            "workspace_key": key,
            "reason": reason,
            "deleted": deleted,
            "at": now.isoformat(),
        },
    )
    return deleted


async def _evict(
    session: AsyncSession,
    *,
    row_id: UUID,
    workspace_id: UUID,
    agent_id: UUID,
    key: str,
    size: int,
    size_state: str,
    measured_at: datetime | None,
    now: datetime,
    reason: str,
    remove: DeleteWorkspace,
) -> bool:
    """Evict one workspace no live run is holding. True when the volume went.

    Claimed first with a conditional ``UPDATE ... RETURNING``, so two evictors
    racing cannot both take the same row, and *un*-claimed again when the
    runner refuses the delete -- which is the whole of this function's reason
    to exist.

    The claim asks :func:`_unheld`, not ``holder_run_id IS NULL``, for the
    reason that function exists: a run that finished without finalizing leaves
    its id behind, and a claim that reads that id as a live holder silently
    declines to evict the one row the sweep planned its whole arithmetic
    around. Writing ``state=evicted, size_bytes=0`` for a volume that is
    still there was the same defect :func:`_recycle` was fixed for, and it
    survived here: the row said empty, the next bind took the "the volume is
    already gone" branch and attempted no delete, and an untouched full disk
    came back
    into service recorded as empty -- permanently invisible to the cap and to
    every future sweep, because a sweep only ever sees the sizes this table
    holds.

    Nothing outside this transaction ever observes the claim, so the revert
    is not a second state to reason about: the bind commits once, and it
    commits either an eviction whose volume is gone or no eviction at all.
    """
    claimed = (
        await session.execute(
            sa.update(SandboxWorkspace)
            .where(
                SandboxWorkspace.id == row_id,
                _unheld(),
                SandboxWorkspace.state == STATE_ACTIVE,
            )
            .values(
                state=STATE_EVICTED,
                size_bytes=0,
                size_state=SIZE_MEASURED,
                size_measured_at=now,
            )
            .returning(SandboxWorkspace.id)
            .execution_options(synchronize_session=False)
        )
    ).first()
    if claimed is None:
        return False
    try:
        deleted = bool(await remove(key))
    except Exception:
        deleted = False
    if not deleted:
        await session.execute(
            sa.update(SandboxWorkspace)
            .where(SandboxWorkspace.id == row_id)
            .values(
                state=STATE_ACTIVE,
                size_bytes=size,
                size_state=size_state,
                size_measured_at=measured_at,
            )
            .execution_options(synchronize_session=False)
        )
    _audit(
        session,
        workspace_id=workspace_id,
        agent_id=agent_id,
        action=AUDIT_EVICTED,
        row_id=row_id,
        metadata={"workspace_key": key, "reason": reason, "deleted": deleted},
    )
    return deleted


@dataclass(frozen=True)
class _SweepOutcome:
    """What one bind's sweep did, and what it could not do."""

    #: Why the caller's *own* workspace was recycled, or ``""``.
    own_reason: str = ""
    #: How many bytes the tenant is **still** over its total budget by when
    #: this sweep finished. Zero is the sweep's only success condition, and the
    #: caller refuses the bind on anything else.
    #:
    #: There are two ways to end up here and they are deliberately not
    #: distinguished, because the caller's question is about the tenant rather
    #: than about the sweep: the plan never added up (the space is held by runs
    #: that have not finished, so nothing was destroyed), or the plan added up
    #: and a delete the runner refused took it apart half-way through. The
    #: second used to report zero -- "a momentary failure of a machine" -- and
    #: that reading is what made a partial plan the worst of both outcomes:
    #: the neighbours destroyed on the earlier iterations are gone, the tenant
    #: is still over budget, and the bind that caused it was served anyway.
    #: The all-or-nothing gate exists to prevent exactly that shape, and it has
    #: to hold from inside the loop as well as before it.
    over_by: int = 0


async def _sweep(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    now: datetime,
    own_row_id: UUID | None = None,
    delete_workspace: DeleteWorkspace | None = None,
) -> _SweepOutcome:
    """Evict idle and over-budget durable workspaces.

    **Only this tenant's.** The sweep runs on somebody's bind, and a bind is
    an action by one tenant's agent; letting it read the whole table made a
    busy tenant delete a quiet tenant's disk, with the quiet tenant's agent
    finding its checkout gone and nothing in its own audit trail to say why.
    A shared install's total is not a budget one tenant may spend on another's
    behalf, so the candidate scan and the total are both scoped to
    ``workspace_id`` and the cap is per tenant. An install-wide ceiling, if one
    is ever wanted, belongs to an operator's own sweep with an operator's own
    authority -- not to whichever agent happened to run a tool call.

    Runs at bind time because that is when the tool worker is already in the
    database and already talking to the runner, and because a workspace is only
    safely destroyable when no run holds it -- which is a fact this table has
    and no scheduled job would have any better.

    **Two passes, because the two reasons are not the same question.**

    *Idle* is about age, so it takes the least recently used first and takes
    every workspace that is past the idle horizon whether or not the tenant is
    over budget.

    *Budget* is about bytes, and **it is all-or-nothing.** Its candidates are
    the workspaces no live run holds, plus the binder's own -- which this instant
    proves idle, before any container of the new run has started, and which is
    recycled rather than evicted because the run is about to use it. It takes
    the largest first, so the fewest agents lose a disk. And before it takes
    anything at all it asks whether the candidates it may touch add up to the
    overrun. If they do not, it takes **nothing** and reports the shortfall,
    and the caller refuses the bind with ``workspace_tenant_full``.

    That gate is the whole fix, because the overspender is usually not the
    binder and usually not free: two runs of an agent overlap, and a run
    parked on an approval holds its lease for as long as the approval takes.
    Without the gate, a tenant 52 MB over budget because of one 100 MB
    workspace held by a live run destroyed four unheld neighbours of 4 KiB
    each *and* the binder's own 4 KiB row, freed 20 KiB, left the tenant over
    budget, refused nothing, and never went near the workspace that was
    actually spending the budget. Largest-first ordering made that strictly
    worse than the LRU it replaced rather than better, because it added the
    binder's own row to the pile. The old promise that "a candidate that would
    free nothing is never taken" was true only of a row recorded as *zero*;
    every one of those five was non-zero and every one of them was pointless.

    Stopping is not a worse outcome than sweeping. Refusing the bind leaves
    five agents with their work and one call unserved, and the situation ends
    on its own when the holder finishes; sweeping left five agents without
    their work, the tenant still over budget, and the sixth call served.

    **All-or-nothing holds inside the loop too, and that is the same rule
    rather than a second one.** A plan that added up when it started can stop
    adding up half-way through, because a delete the runner refuses frees
    nothing; the loop notices at the next candidate and stops destroying, but
    the disks it already took are gone. If the tenant is still over budget
    when the loop ends, this reports it and the bind is refused. Reporting
    success there -- on the grounds that a refused delete is a machine's bad
    moment rather than a state of the tenant -- produced the exact shape the
    gate exists to prevent: neighbours destroyed, tenant over budget, and the
    bind that caused it served anyway.

    The caller passes ``own_row_id=None`` on the one call where recycling the
    binder's own disk is the wrong trade -- ``cli.repository.push``, which
    exists to get the work *out* of the workspace -- and that call is also
    never refused for the tenant being over budget, for the same reason.

    A workspace a live run holds is never a victim unless it is the binder's
    own, and a row that cannot pay for itself is never a candidate: one
    recorded as zero cannot move the total, and one recorded as *unknown*
    would be destroyed for a number nobody has. Both can only cost some agent
    its warm disk.

    **Two totals, because destroying and refusing are different decisions.**
    What may be destroyed is planned against the bytes this table has actually
    seen -- a measured disk's size, an unmeasurable one's floor -- so every
    eviction frees bytes that were provably there. What is refused is decided
    against the charged total, where an unmeasurable disk costs the cap it is
    allowed to fill. The gap between them is a disk nobody could count, and
    keeping the two apart is what lets such a disk make a bind wait without
    making a neighbour's tree disappear.

    **Every durable disk of the tenant is on the books**, run-scoped ones
    included. They are real bytes on the same host: selecting only
    ``kind='agent'`` left them measured, stored, occupying disk and
    contributing nothing, so a 10 GiB run workspace sat inside a 52 MB budget
    with the total reading zero. Their eviction rule is the same as everything
    else's -- a live run's disk is never taken -- so a contended run keeps its
    private tree while it is running and the tenant is refused instead, which
    is the bound the second term never had.
    """
    remove = delete_workspace or delete_runner_workspace
    holder = aliased(AgentRun)
    rows = list(
        (
            await session.execute(
                sa.select(
                    SandboxWorkspace.id,
                    SandboxWorkspace.workspace_id,
                    SandboxWorkspace.agent_id,
                    SandboxWorkspace.workspace_key,
                    SandboxWorkspace.size_bytes,
                    SandboxWorkspace.size_state,
                    SandboxWorkspace.size_measured_at,
                    SandboxWorkspace.last_used_at,
                    SandboxWorkspace.holder_run_id,
                    holder.status.label("holder_status"),
                )
                .select_from(SandboxWorkspace)
                # The holder's own row, because "held" is a question about a
                # run's status and never about an id being present. Outer, so
                # a free lease is still a row here rather than a row that
                # vanishes from the total it belongs in.
                .outerjoin(holder, holder.id == SandboxWorkspace.holder_run_id)
                .where(
                    SandboxWorkspace.workspace_id == workspace_id,
                    SandboxWorkspace.state == STATE_ACTIVE,
                )
            )
        ).all()
    )
    # What each row costs the budget, computed once: an unmeasurable disk is
    # charged the cap rather than the floor its walk reached, and every later
    # subtraction has to use the same number it was added with.
    charges = {row.id: _charge(row.size_bytes, row.size_state) for row in rows}
    # Two totals, because destroying and refusing are different decisions and
    # only one of them may be taken on a number nobody has.
    #
    # ``certain`` is bytes this table has actually seen: a measured disk's
    # size, and an unmeasurable one's floor, which is still a fact about the
    # disk. It is what the budget pass plans against, so every eviction frees
    # bytes that were provably there.
    #
    # ``total`` is what the tenant is charged, with an unmeasurable disk
    # charged the cap it is allowed to fill. It is what the *refusal* is
    # decided on, because it is the number that cannot be ruled out. The
    # difference between them is a disk nobody could measure, and the whole
    # point of separating them is that such a disk can make a bind wait
    # without making a neighbour's tree disappear.
    certain = {row.id: max(int(row.size_bytes or 0), 0) for row in rows}
    certain_total = sum(certain.values())
    total = sum(charges.values())
    budget = total_workspace_bytes()
    idle_before = now - idle_eviction()

    unheld = [row for row in rows if not _held_by_a_live_run(row.holder_run_id, row.holder_status)]
    # A workspace that has never been used has no age to be idle by, which is
    # what ``last_used_at IS NULL`` means and why it is not a candidate here.
    stale = [
        row
        for row in unheld
        if (used := _as_utc(row.last_used_at)) is not None and used < idle_before
    ]
    # Every row the idle pass *asked* about, not only the ones it freed: a
    # delete the runner just refused is not worth a second request in the same
    # bind, and a row it took is gone from the budget's arithmetic already.
    asked: set[UUID] = set()
    for row in sorted(stale, key=lambda row: _as_utc(row.last_used_at) or _EPOCH):
        asked.add(row.id)
        if await _evict(
            session,
            row_id=row.id,
            workspace_id=row.workspace_id,
            agent_id=row.agent_id,
            key=row.workspace_key,
            size=int(row.size_bytes or 0),
            size_state=row.size_state,
            measured_at=row.size_measured_at,
            now=now,
            reason="idle",
            remove=remove,
        ):
            total -= charges[row.id]
            certain_total -= certain[row.id]

    if certain_total <= budget:
        # Nothing whose existence is established puts this tenant over, so
        # there is nothing to destroy. Whether the *bind* is served is a
        # separate question, answered at the bottom from the charged total: a
        # disk nobody can measure can leave a tenant unable to prove it is
        # under budget, and the answer to that is to wait rather than to take
        # a neighbour's work for a number that was never counted.
        return _SweepOutcome(over_by=max(total - budget, 0))
    # Largest first, so the overrun is covered by the fewest agents losing a
    # disk. Two kinds of row are not candidates at all. One recorded as zero
    # cannot move the total, so destroying it is pure cost. One whose size is
    # *unknown* would be destroyed on a number nobody has: it is charged the
    # cap above -- so it can put the tenant over budget and get every bind
    # refused -- but it is never the thing that pays, because a refusal costs
    # a run and an eviction costs an agent's unpushed work.
    spenders = sorted(
        (
            row
            for row in rows
            if row.id not in asked
            and row.size_state == SIZE_MEASURED
            and int(row.size_bytes or 0) > 0
            and (
                not _held_by_a_live_run(row.holder_run_id, row.holder_status)
                or row.id == own_row_id
            )
        ),
        key=lambda row: (-int(row.size_bytes or 0), _as_utc(row.last_used_at) or _EPOCH),
    )
    # ``outstanding[index]`` is everything still on the table from ``index``
    # onwards. Compared against the overrun before each destruction, it is the
    # rule this pass exists for: a workspace is destroyed only while the plan
    # it belongs to can still bring the tenant under budget. The first
    # comparison is the one that matters -- it is the difference between
    # taking five disks to free 20 KiB of a 52 MB overrun and taking none.
    sizes = [certain[row.id] for row in spenders]
    outstanding = [0] * (len(sizes) + 1)
    for index in range(len(sizes) - 1, -1, -1):
        outstanding[index] = outstanding[index + 1] + sizes[index]
    if certain_total - outstanding[0] > budget:
        # Everything this sweep may touch, taken together, does not add up to
        # the overrun. Take nothing: the space is somewhere it cannot reach --
        # a workspace held by a run that has not finished -- and destroying
        # the disks it *can* reach would cost several agents their work and
        # still leave the tenant over budget.
        return _SweepOutcome(over_by=total - budget)
    own_reason = ""
    for index, row in enumerate(spenders):
        if certain_total <= budget:
            break
        if certain_total - outstanding[index] > budget:
            # A delete the runner refused has taken the rest of the plan below
            # what it needs. Stop: every further eviction is somebody's work
            # destroyed for an outcome this sweep can no longer reach. The
            # bind is refused for it too -- see ``_SweepOutcome.over_by``.
            break
        if row.id == own_row_id:
            # ``_recycle`` clears the size only when the volume actually went,
            # and the reason the sweep is running is that these bytes are on
            # disk, so the running total follows what the runner said rather
            # than assuming.
            if await _recycle(
                session,
                row_id=row.id,
                workspace_id=row.workspace_id,
                agent_id=row.agent_id,
                key=row.workspace_key,
                reason="total_size",
                action=AUDIT_EVICTED,
                now=now,
                delete_workspace=delete_workspace,
            ):
                own_reason = "total_size"
                total -= charges[row.id]
                certain_total -= certain[row.id]
            continue
        if await _evict(
            session,
            row_id=row.id,
            workspace_id=row.workspace_id,
            agent_id=row.agent_id,
            key=row.workspace_key,
            size=int(row.size_bytes or 0),
            size_state=row.size_state,
            measured_at=row.size_measured_at,
            now=now,
            reason="total_size",
            remove=remove,
        ):
            total -= charges[row.id]
            certain_total -= certain[row.id]
    # Under budget or not: the sweep reports what it left behind, and a bind
    # is only served on a tenant it brought home. Reporting zero here because
    # the shortfall was a runner's refusal rather than a held disk is what let
    # a half-executed plan destroy neighbours *and* serve the bind that paid
    # for them.
    return _SweepOutcome(own_reason=own_reason, over_by=max(total - budget, 0))


async def _release_refused_lease(session: AsyncSession, *, row_id: UUID, run_id: UUID) -> None:
    """Give back the lease this bind took, because the bind is being refused.

    A bind that is refused must not leave the run holding the workspace it was
    refused: the run would look alive to every other bind's contention check,
    keep the row out of every future sweep's reach, and be served on its own
    next call through the renewal path -- which would make the refusal a
    single confusing hiccup rather than a bound. Given back, the row is
    exactly as this bind found it and the refusal repeats for as long as the
    situation lasts.
    """
    await session.execute(
        sa.update(SandboxWorkspace)
        .where(SandboxWorkspace.id == row_id, SandboxWorkspace.holder_run_id == run_id)
        .values(holder_run_id=None, lease_expires_at=None)
        .execution_options(synchronize_session=False)
    )


async def bind_workspace(
    session_factory: async_sessionmaker[AsyncSession] | None,
    *,
    workspace_id: UUID,
    agent_id: UUID,
    run_id: UUID,
    delete_workspace: DeleteWorkspace | None = None,
    enforce_size: bool = True,
) -> WorkspaceBinding:
    """Decide, once per run, which disk this run's sandbox jobs run on.

    Committed in its own transaction so a lease taken is durable before a
    container starts, and so a tool call that later rolls back cannot release
    one it never gave back.

    Three refusals come out of the size rules, and they name three different
    situations because they have three different remedies. ``workspace_full``
    is this agent's own disk over its own cap, and the agent can act on it:
    push. ``workspace_tenant_full`` is the tenant's disks over their shared
    budget with the space held by runs that have not finished -- nothing this
    agent did and nothing it can undo, so the hint says who has to finish or
    what an operator can reset. Collapsing the second into the first would
    tell an agent to push a branch that would free nothing.

    ``workspace_unmeasured`` is the third, and it is a refusal about knowledge
    rather than about space: the last walk of this disk did not finish, so
    there is no number to enforce the cap with. It refuses instead of
    guessing, in both directions. Guessing *small* -- which is what storing an
    incomplete walk's floor as a size amounted to -- is the cap not existing:
    28 MB recorded, 6.5 GB on the disk, nothing recycled and nothing refused.
    Guessing *large* and emptying the disk would destroy a day's uncommitted
    work on the strength of a measurement that failed. A refusal costs a run,
    which is the only one of the three prices worth paying, and the disk is
    left for a push and an operator's reset -- or for the idle pass, which
    answers to age and needs no size at all.

    They also apply at different moments, and deliberately. ``workspace_full``
    is checked on *every* call including the renewals, because it is the
    overspender's own disk and stopping it mid-run is the point -- with the
    per-agent cap bounded by the tenant total, that check is what actually
    holds the tenant's budget. ``workspace_tenant_full`` is checked only where
    a run *takes* a disk, because that is the decision it is about: a tenant
    over budget is a reason not to hand out another workspace, not a reason to
    cut off a run already doing work, which could no more get out of the
    situation than it got into it. A run refused there is left holding
    nothing, so the refusal repeats for as long as the situation lasts rather
    than being served by the next call's renewal, and it ends when the run
    holding the space finishes and its disk becomes reachable.

    ``enforce_size=False`` is for ``cli.repository.push`` alone, and it turns
    off both. The whole point of refusing a full workspace is to make the
    agent get its work *out* of it, and the refusal's own hint says to push --
    so refusing the push too would make that hint a lie and leave the branch
    stranded until an operator threw the disk away. The cap is Jhin's
    accounting limit rather than a full filesystem, so the commit and push the
    exemption allows still have room to run, and they are the only two things
    that make the situation better.
    """
    if session_factory is None:
        # No durable store: no lease can be proven, so no shared disk is taken.
        return WorkspaceBinding(
            key=run_workspace_key(run_id),
            kind=KIND_RUN,
            record_target_type=CHECKOUT_TARGET_RUN,
            record_target_id=run_id,
        )

    from jhin_connectors.cli.conversation_workspace import bind_conversation_workspace

    conversation_binding = await bind_conversation_workspace(
        session_factory,
        workspace_id,
        agent_id,
        run_id,
        enforce_size=enforce_size,
    )
    if conversation_binding is not None:
        return conversation_binding
    now = datetime.now(UTC)
    async with session_factory() as session:
        renewed = await _renew(session, workspace_id=workspace_id, run_id=run_id, now=now)
        if renewed is not None:
            await session.commit()
            # Mid-run the disk is never destroyed to reclaim space; the call is
            # refused instead, so the agent can still push what it has. A disk
            # nobody could measure is refused on the same terms: "we do not
            # know" is not "it is small", and the alternative -- carrying on
            # against a floor of 28 MB under a disk holding 6.5 GB -- is the
            # cap not existing.
            if enforce_size:
                if renewed.size_state == SIZE_UNKNOWN:
                    raise _refusal("workspace_unmeasured")
                if int(renewed.size_bytes or 0) > max_workspace_bytes():
                    raise _refusal("workspace_full")
            return WorkspaceBinding(
                key=renewed.workspace_key,
                kind=renewed.kind,
                record_target_type=CHECKOUT_TARGET_WORKSPACE,
                record_target_id=renewed.id,
                contended=renewed.kind == KIND_RUN,
                durable=True,
                row_id=renewed.id,
            )

        if await _lost_the_lease(
            session, workspace_id=workspace_id, agent_id=agent_id, run_id=run_id
        ):
            raise _refusal("workspace_lease_lost")

        await _ensure_agent_row(session, workspace_id=workspace_id, agent_id=agent_id)
        acquired = await _acquire(
            session, workspace_id=workspace_id, agent_id=agent_id, run_id=run_id, now=now
        )
        if acquired is None:
            return await _private_binding(
                session,
                workspace_id=workspace_id,
                agent_id=agent_id,
                run_id=run_id,
                now=now,
            )

        # The only moment a durable workspace may be destroyed: it is held by
        # this run, and no container of this run has started yet.
        used = _as_utc(acquired.last_used_at)
        recycled = ""
        #: True once the runner has confirmed the volume is gone, which is the
        #: only thing that turns an unmeasurable disk back into a known one.
        emptied = False
        if acquired.reset_requested_at is not None:
            recycled = "reset"
            emptied = await _recycle(
                session,
                row_id=acquired.id,
                workspace_id=workspace_id,
                agent_id=agent_id,
                key=acquired.workspace_key,
                reason="reset",
                action=AUDIT_RECYCLED,
                now=now,
                delete_workspace=delete_workspace,
            )
        elif acquired.state == STATE_EVICTED:
            # An evicted row's volume is gone, because eviction is only ever
            # committed once the runner said so. It was not always: a delete
            # the runner refused used to be written as ``evicted, 0`` anyway,
            # and this branch then returned a full disk to service recorded as
            # empty. Rows written by that version are still in the table, so
            # the delete is *repeated* here rather than assumed -- it is one
            # idempotent call on a volume that is almost always already gone,
            # and it is the difference between the row being true and the row
            # being hoped for. The size is only cleared if it comes back gone.
            recycled = "evicted"
            emptied = await _recycle(
                session,
                row_id=acquired.id,
                workspace_id=workspace_id,
                agent_id=agent_id,
                key=acquired.workspace_key,
                reason="evicted",
                action=AUDIT_RECYCLED,
                now=now,
                delete_workspace=delete_workspace,
            )
        elif enforce_size and int(acquired.size_bytes or 0) > max_workspace_bytes():
            # ``enforce_size`` gates the *destruction* here, not only the
            # refusal further down. This is the same trade the sweep makes for
            # the binder's own row and the same answer: on
            # ``cli.repository.push`` the disk holds the branch the push is
            # about to send, and emptying it to reclaim space would destroy
            # the one thing that makes the situation better. The next call
            # that is not a push recycles it.
            recycled = "size"
            emptied = await _recycle(
                session,
                row_id=acquired.id,
                workspace_id=workspace_id,
                agent_id=agent_id,
                key=acquired.workspace_key,
                reason="size",
                action=AUDIT_EVICTED,
                now=now,
                delete_workspace=delete_workspace,
            )
        elif used is not None and used < now - idle_eviction():
            recycled = "idle"
            emptied = await _recycle(
                session,
                row_id=acquired.id,
                workspace_id=workspace_id,
                agent_id=agent_id,
                key=acquired.workspace_key,
                reason="idle",
                action=AUDIT_EVICTED,
                now=now,
                delete_workspace=delete_workspace,
            )

        if enforce_size and acquired.size_state == SIZE_UNKNOWN and not emptied:
            # Nothing knows how big this disk is, and nothing above emptied
            # it, so there is no bound to hand it out under. It is refused and
            # left exactly as it was found -- not destroyed, because "unknown"
            # is not evidence of anything and the tree may be a day's work,
            # and not served, because serving it is the cap not existing.
            #
            # The lease goes back for the same reason it does on the budget
            # refusal: a run holding a workspace it was refused looks alive to
            # every contention check, keeps the row out of reach of every
            # sweep, and gets served by its own next call's renewal, which
            # turns a bound into one confusing hiccup. Given back, the refusal
            # repeats until the disk is emptied -- by the operator's reset, or
            # by the idle pass -- and the agent's own remedy (push, which is
            # exempt) still works.
            await _release_refused_lease(session, row_id=acquired.id, run_id=run_id)
            _audit(
                session,
                workspace_id=workspace_id,
                agent_id=agent_id,
                action=AUDIT_SIZE_REFUSED,
                row_id=acquired.id,
                metadata={
                    "run_id": str(run_id),
                    "workspace_key": acquired.workspace_key,
                    "size_floor_bytes": int(acquired.size_bytes or 0),
                    "max_bytes": max_workspace_bytes(),
                },
            )
            await session.commit()
            raise _refusal("workspace_unmeasured")

        await session.execute(
            sa.update(SandboxWorkspace)
            .where(SandboxWorkspace.id == acquired.id)
            .values(last_used_at=now)
            .execution_options(synchronize_session=False)
        )
        swept = await _sweep(
            session,
            workspace_id=workspace_id,
            now=now,
            # The push exemption reaches all the way here. ``enforce_size=False``
            # is ``cli.repository.push`` saying "this is the call that gets the
            # work *out* of the workspace", and a sweep that destroyed this
            # agent's disk to make room would delete the branch the push was
            # about to send. Another agent's idle disk is still fair game; this
            # run's own is not, on this one call.
            own_row_id=acquired.id if enforce_size else None,
            delete_workspace=delete_workspace,
        )
        recycled = recycled or swept.own_reason
        if enforce_size and swept.over_by > 0:
            # The tenant is still over budget after the sweep -- either
            # nothing it may destroy added up to the overrun, or a delete the
            # runner refused took the plan apart -- so no disk is handed out,
            # and that has to include the one this statement had already
            # taken. It stops by itself when the run holding the space
            # finishes and that disk becomes reachable.
            await _release_refused_lease(session, row_id=acquired.id, run_id=run_id)
            # Recorded under its own action rather than as a ``bound`` event,
            # because nothing was bound. This is the row an operator reads to
            # see that an agent was refused for a neighbour's disk, and how
            # far over the tenant was when it happened.
            _audit(
                session,
                workspace_id=workspace_id,
                agent_id=agent_id,
                action=AUDIT_BUDGET_REFUSED,
                row_id=acquired.id,
                metadata={
                    "run_id": str(run_id),
                    "workspace_key": acquired.workspace_key,
                    "over_budget_bytes": swept.over_by,
                    "budget_bytes": total_workspace_bytes(),
                    "recycled": recycled,
                },
            )
            await session.commit()
            raise _refusal("workspace_tenant_full")
        _audit(
            session,
            workspace_id=workspace_id,
            agent_id=agent_id,
            action=AUDIT_BOUND,
            row_id=acquired.id,
            metadata={
                "run_id": str(run_id),
                "workspace_key": acquired.workspace_key,
                "kind": KIND_AGENT,
                "contended": False,
                "recycled": recycled,
            },
        )
        await session.commit()
        return WorkspaceBinding(
            key=acquired.workspace_key,
            kind=KIND_AGENT,
            record_target_type=CHECKOUT_TARGET_WORKSPACE,
            record_target_id=acquired.id,
            durable=True,
            row_id=acquired.id,
        )


async def readable_binding(
    session: AsyncSession, *, workspace_id: UUID, agent_id: UUID, run_id: UUID
) -> UUID | None:
    """Which workspace row this run's next job *could* read, without binding.

    A read-only answer to "what is on the disk this call is about to use", for
    callers that must ask *before* the call is allowed to happen -- the policy
    layer, which runs long before a container and must not take a lease, sweep
    anything or delete a volume as a side effect of deciding.

    A row this run already holds is the one it will keep. Otherwise the answer
    is the agent's durable row **whoever holds it and whatever state it is
    in**, which is a deliberately wider answer than "the row the next bind
    would acquire", and the width is the point:

    * *Held by another run.* The next bind would lose the contention and take
      a fresh private disk, so the narrow answer was None -- the policy layer
      was told the disk was empty and allowed the call. But nothing holds that
      contention still: the holder can finalize between this question and the
      executor's bind, and then the call acquires the very disk it was allowed
      against on the grounds that it would not. Answering with the agent's
      durable row closes that window from the only side a read-only question
      can close it: by not depending on a race it cannot observe.
    * *Evicted.* Same shape. An evicted row's volume is gone, so its history
      reads as an empty disk anyway -- the answer costs a query and prevents
      a state that used to skip the question entirely.

    The cost is a denial for a run that would in fact have got a fresh private
    disk, which happens only when this agent's own durable workspace holds
    something its connection may no longer reach. That is a state somebody has
    to fix rather than route around, and the denial names how.
    """
    held = await session.scalar(
        sa.select(SandboxWorkspace.id).where(
            SandboxWorkspace.workspace_id == workspace_id,
            SandboxWorkspace.holder_run_id == run_id,
        )
    )
    if held is not None:
        return held
    task = await session.scalar(
        sa.select(Task)
        .join(AgentRun, AgentRun.task_id == Task.id)
        .where(
            AgentRun.id == run_id,
            AgentRun.workspace_id == workspace_id,
            AgentRun.agent_id == agent_id,
        )
    )
    if task is not None and task.conversation_id is not None:
        chat = await session.get(Conversation, task.conversation_id)
        if chat is not None and chat.workspace_id == workspace_id and chat.workspace_version >= 1:
            conversation_workspace: UUID | None = await session.scalar(
                sa.select(SandboxWorkspace.id).where(
                    SandboxWorkspace.workspace_id == workspace_id,
                    SandboxWorkspace.conversation_id == chat.id,
                    SandboxWorkspace.kind == "conversation",
                )
            )
            return conversation_workspace
    durable: UUID | None = await session.scalar(
        sa.select(SandboxWorkspace.id).where(
            SandboxWorkspace.workspace_id == workspace_id,
            SandboxWorkspace.agent_id == agent_id,
            SandboxWorkspace.kind == KIND_AGENT,
        )
    )
    return durable


#: How far back the scan for "when was this disk last emptied" looks. Reached
#: only by a workspace with this many checkouts and no wipe in between; the
#: answer when it *is* reached is "never emptied", which counts every
#: repository the disk has ever held -- the conservative direction.
_EMPTIED_SCAN_LIMIT = 200


async def _last_emptied_at(
    session: AsyncSession, *, workspace_id: UUID, row_id: UUID
) -> datetime | None:
    """When this disk was last provably empty, or None.

    Two things empty a durable disk, and both leave a row in the same
    append-only table the checkout records live in: destroying the volume
    (``deleted: true`` -- and only ``true``, which is why the runner had to
    stop reporting a refused delete as a success), and a checkout that purged
    the whole workspace before cloning (``purged: true``).
    """
    rows = (
        await session.execute(
            sa.select(AuditEvent.created_at, AuditEvent.action, AuditEvent.metadata_json)
            .where(
                AuditEvent.workspace_id == workspace_id,
                AuditEvent.target_type == CHECKOUT_TARGET_WORKSPACE,
                AuditEvent.target_id == row_id,
                AuditEvent.action.in_((AUDIT_EVICTED, AUDIT_RECYCLED, AUDIT_CHECKOUT_RECORDED)),
            )
            .order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
            .limit(_EMPTIED_SCAN_LIMIT)
        )
    ).all()
    for created_at, action, metadata in rows:
        data = metadata if isinstance(metadata, dict) else {}
        key = "purged" if action == AUDIT_CHECKOUT_RECORDED else "deleted"
        if data.get(key) is True:
            # Handed back exactly as the database gave it, naive or aware, so
            # the comparison it feeds is made in the column's own terms.
            emptied: datetime = created_at
            return emptied
    return None


async def workspace_repositories(
    session: AsyncSession, *, workspace_id: UUID, row_id: UUID
) -> tuple[str, ...]:
    """Every repository this disk has held since it was last emptied.

    The question a durable workspace forces and a single checkout record
    cannot answer. "The last repository checked out here" describes one path,
    ``/workspace/repo``, and a disk is not a path: ``HOME`` is ``/workspace``,
    so pip and npm write a private repository's packages to ``/workspace/.cache``
    with nobody intending anything by it, a copy taken anywhere else survives
    every checkout because the reuse prologue only removes ``/workspace/repo``,
    and checking out an allowed repository -- the remedy the denial itself
    prints -- used to flip the answer back to "allowed" with the forbidden tree
    still sitting there readable.

    So the disk's history is what is asked, and it is only forgotten when the
    disk itself is: the volume destroyed, or a checkout that wiped the whole
    workspace. Both are facts written by Jhin into a table no sandbox job can
    reach, at the moment the thing actually happened.

    This is provenance, not surveillance of content. It cannot see a clone
    that ``cli.command.execute`` made with its own egress, because no record
    names one -- see :func:`workspace_repository_validator` for why that is the
    network policy's boundary rather than this one's.
    """
    emptied = await _last_emptied_at(session, workspace_id=workspace_id, row_id=row_id)
    query = sa.select(sa.distinct(AuditEvent.metadata_json["repository"].as_string())).where(
        AuditEvent.workspace_id == workspace_id,
        AuditEvent.action == AUDIT_CHECKOUT_RECORDED,
        AuditEvent.target_type == CHECKOUT_TARGET_WORKSPACE,
        AuditEvent.target_id == row_id,
    )
    if emptied is not None:
        query = query.where(AuditEvent.created_at >= emptied)
    found = (await session.scalars(query)).all()
    return tuple(sorted({str(name) for name in found if name}))


async def record_size(
    session_factory: async_sessionmaker[AsyncSession] | None,
    *,
    row_id: UUID | None,
    size_bytes: int | None,
    partial: bool,
) -> None:
    """Store what the runner measured, and whether it is a size or a floor.

    ``partial`` means the walk stopped early, so the number is a *floor* and
    the disk's size is :data:`SIZE_UNKNOWN`. It is stored anyway, because a
    floor is worth having -- one above the cap proves the disk is over it, and
    an operator would rather read "at least 28 MB, incomplete" than nothing --
    but it is stored as what it is.

    **The measurement always replaces what was there.** It used to be a
    ratchet: a floor was written only when it exceeded the stored number, on
    the reasoning that a partial walk must never undo a complete one. What
    that actually built was a paper size with no expiry. One complete
    measurement of 4.9 GB, then six walks that all timed out, and the row
    still said 4.9 GB days after the agent deleted the data -- and since the
    budget sweep takes the largest first, that agent was first in line to have
    its live work destroyed to free space that had already been freed. A stale
    number is not a conservative number. The state flag is what makes
    replacing safe: the floor cannot be mistaken for a size, so it does not
    need to be inflated to be safe, and "we no longer know" is now something
    this table can say.

    Best effort, in its own transaction: failing to store a measurement must
    never fail the tool call that just ran.
    """
    if session_factory is None or row_id is None or size_bytes is None or size_bytes < 0:
        return
    try:
        async with session_factory() as session:
            await session.execute(
                sa.update(SandboxWorkspace)
                .where(SandboxWorkspace.id == row_id)
                .values(
                    size_bytes=int(size_bytes),
                    size_state=SIZE_UNKNOWN if partial else SIZE_MEASURED,
                    size_measured_at=datetime.now(UTC),
                )
                .execution_options(synchronize_session=False)
            )
            await session.commit()
    except Exception:
        # The measurement is an optimisation for the operator and the eviction
        # policy. Failing to store it must never fail the tool call that ran.
        return


async def release_run_bindings(
    session_factory: async_sessionmaker[AsyncSession] | None,
    *,
    workspace_id: UUID,
    run_id: UUID,
    delete_workspace: DeleteWorkspace | None = None,
) -> bool:
    """Give back what this run held when the run finalizes.

    A durable workspace is *released*, not destroyed: the holder is cleared,
    the last holder is remembered, and the disk survives for the agent's next
    turn. A private run workspace is destroyed exactly as before.

    Returns True when a volume was actually deleted, which is what the cleanup
    activity reports.
    """
    if session_factory is None:
        return False
    from jhin_connectors.cli.chat_snapshots import capture_run_outputs
    from jhin_connectors.cli.conversation_workspace import has_unconfirmed_jobs

    async with session_factory() as db:
        if await has_unconfirmed_jobs(db, workspace_id, run_id):
            return False
    remove = delete_workspace or delete_runner_workspace
    await capture_run_outputs(session_factory, workspace_id, run_id)
    deleted = False
    async with session_factory() as session:
        rows = list(
            (
                await session.execute(
                    sa.select(
                        SandboxWorkspace.id,
                        SandboxWorkspace.agent_id,
                        SandboxWorkspace.kind,
                        SandboxWorkspace.workspace_key,
                    ).where(
                        SandboxWorkspace.workspace_id == workspace_id,
                        sa.or_(
                            SandboxWorkspace.holder_run_id == run_id,
                            sa.and_(
                                SandboxWorkspace.kind == KIND_RUN,
                                SandboxWorkspace.run_id == run_id,
                            ),
                        ),
                    )
                )
            ).all()
        )
        if not rows:
            return False
        now = datetime.now(UTC)
        for row_id, agent_id, kind, key in rows:
            if kind in {KIND_AGENT, "conversation", "delegated"}:
                released = await session.scalar(
                    sa.update(SandboxWorkspace)
                    .where(
                        SandboxWorkspace.id == row_id,
                        SandboxWorkspace.holder_run_id == run_id,
                    )
                    .values(
                        holder_run_id=None,
                        last_holder_run_id=run_id,
                        lease_expires_at=None,
                        last_used_at=now,
                    )
                    .returning(SandboxWorkspace.id)
                    .execution_options(synchronize_session=False)
                )
                if released is None:
                    continue
                _audit(
                    session,
                    workspace_id=workspace_id,
                    agent_id=agent_id,
                    action=AUDIT_RELEASED,
                    row_id=row_id,
                    metadata={"run_id": str(run_id), "workspace_key": key, "kind": kind},
                )
                continue
            try:
                gone = bool(await remove(key))
            except Exception:
                gone = False
            deleted = deleted or gone
            await session.execute(sa.delete(SandboxWorkspace).where(SandboxWorkspace.id == row_id))
        await session.commit()
    return deleted


__all__ = [
    "AUDIT_BOUND",
    "AUDIT_BUDGET_REFUSED",
    "AUDIT_CHECKOUT_RECORDED",
    "AUDIT_EVICTED",
    "AUDIT_RECYCLED",
    "AUDIT_RELEASED",
    "AUDIT_RESET_REQUESTED",
    "AUDIT_SIZE_REFUSED",
    "CHECKOUT_TARGET_RUN",
    "CHECKOUT_TARGET_WORKSPACE",
    "KIND_AGENT",
    "KIND_RUN",
    "SIZE_MEASURED",
    "SIZE_UNKNOWN",
    "STATE_ACTIVE",
    "STATE_EVICTED",
    "WORKSPACE_REFUSAL_HINTS",
    "WorkspaceBinding",
    "agent_workspace_key",
    "bind_workspace",
    "idle_eviction",
    "lease_ceiling",
    "lease_ttl",
    "max_workspace_bytes",
    "readable_binding",
    "record_size",
    "release_run_bindings",
    "run_workspace_key",
    "total_workspace_bytes",
    "workspace_repositories",
]
