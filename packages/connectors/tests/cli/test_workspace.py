"""The durable sandbox workspace: isolation, concurrency, bounds, release.

These are the load-bearing claims of ``jhin_connectors.cli.workspace``, and each
one is proven against a real database rather than argued in a docstring:

* two agents can never land on one disk, and one agent always lands on its own;
* two overlapping runs of one agent never share a tree, and neither waits;
* a run pinned to a disk stays on it for its whole life;
* a lease is never taken from a run that is still alive;
* a run that *did* lose its lease is told so rather than drifting onto a fresh
  disk with its checkout on the old one;
* a workspace is only ever destroyed when nothing holds it, and never to
  reclaim space from underneath a running agent;
* finalize releases an agent's disk and destroys a run's.

The store is a file-backed SQLite database rather than the shared in-memory one
in ``conftest``: binding commits in its *own* session, and proving that a lease
survives a rolled-back tool call needs two real connections rather than two
handles onto one transaction.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from jhin_connectors.cli import workspace as ws
from jhin_db.base import Base
from jhin_db.models import Agent, AgentRun, AuditEvent, SandboxWorkspace, Task, Workspace
from jhin_domain import new_uuid7
from jhin_sandbox_runner.schemas import WORKSPACE_KEY_RE
from jhin_tools.errors import ToolExecutionError


class DeletedVolumes:
    """A runner that only remembers. Eviction is a policy decision, and a
    policy is provable without a Docker daemon."""

    def __init__(self, *, succeeds: bool = True) -> None:
        self.keys: list[str] = []
        self._succeeds = succeeds

    async def __call__(self, key: str) -> bool:
        self.keys.append(key)
        return self._succeeds


@pytest.fixture
async def engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    made = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'workspaces.db'}")
    async with made.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield made
    await made.dispose()


@pytest.fixture
def factory(engine: AsyncEngine) -> async_sessionmaker[Any]:
    return async_sessionmaker(engine, expire_on_commit=False)


class Fixtures:
    """A workspace, its agents, and runs of them — the identities a binding is
    derived from, all of which the tool worker reads from the database rather
    than from anything a model said."""

    def __init__(self, factory: async_sessionmaker[Any]) -> None:
        self.factory = factory
        self.workspace_id = new_uuid7()

    async def setup(self) -> None:
        async with self.factory() as session:
            session.add(
                Workspace(
                    id=self.workspace_id,
                    name="Test",
                    slug=f"test-ws-{self.workspace_id.hex[-12:]}",
                )
            )
            await session.commit()

    async def agent(self, name: str) -> UUID:
        agent_id = new_uuid7()
        async with self.factory() as session:
            session.add(
                Agent(
                    id=agent_id,
                    workspace_id=self.workspace_id,
                    name=name,
                    slug=name.lower().replace(" ", "-"),
                )
            )
            await session.commit()
        return agent_id

    async def run(self, agent_id: UUID, *, status: str = "running") -> UUID:
        run_id = new_uuid7()
        task_id = new_uuid7()
        async with self.factory() as session:
            session.add(
                Task(
                    id=task_id,
                    workspace_id=self.workspace_id,
                    title="t",
                    correlation_id=new_uuid7(),
                )
            )
            session.add(
                AgentRun(
                    id=run_id,
                    workspace_id=self.workspace_id,
                    agent_id=agent_id,
                    task_id=task_id,
                    status=status,
                )
            )
            await session.commit()
        return run_id

    async def set_run_status(self, run_id: UUID, status: str) -> None:
        async with self.factory() as session:
            row = await session.get(AgentRun, run_id)
            assert row is not None
            row.status = status
            await session.commit()

    async def bind(self, agent_id: UUID, run_id: UUID, **kwargs: Any) -> ws.WorkspaceBinding:
        return await ws.bind_workspace(
            self.factory,
            workspace_id=self.workspace_id,
            agent_id=agent_id,
            run_id=run_id,
            **kwargs,
        )

    async def row(self, agent_id: UUID, kind: str = ws.KIND_AGENT) -> SandboxWorkspace:
        async with self.factory() as session:
            row = await session.scalar(
                select(SandboxWorkspace).where(
                    SandboxWorkspace.agent_id == agent_id, SandboxWorkspace.kind == kind
                )
            )
            assert row is not None
            return row

    async def touch(self, agent_id: UUID, **values: Any) -> None:
        """Age a row, fill it, or mark it — whatever the policy under test needs
        to have been true before the next bind looks."""
        async with self.factory() as session:
            row = await session.scalar(
                select(SandboxWorkspace).where(
                    SandboxWorkspace.agent_id == agent_id,
                    SandboxWorkspace.kind == ws.KIND_AGENT,
                )
            )
            assert row is not None
            for name, value in values.items():
                setattr(row, name, value)
            await session.commit()

    async def touch_row(self, row_id: UUID, **values: Any) -> None:
        """The same, for a row named directly — a run-scoped workspace has no
        agent of its own to look it up by."""
        async with self.factory() as session:
            row = await session.get(SandboxWorkspace, row_id)
            assert row is not None
            for name, value in values.items():
                setattr(row, name, value)
            await session.commit()

    async def audit_actions(self) -> list[str]:
        async with self.factory() as session:
            return [
                event.action
                for event in await session.scalars(
                    select(AuditEvent).order_by(AuditEvent.created_at, AuditEvent.id)
                )
                if event.action.startswith("sandbox.workspace.")
            ]

    async def audit_for(self, action: str) -> list[dict[str, Any]]:
        async with self.factory() as session:
            return [
                dict(event.metadata_json or {})
                for event in await session.scalars(
                    select(AuditEvent)
                    .where(AuditEvent.action == action)
                    .order_by(AuditEvent.created_at, AuditEvent.id)
                )
            ]


@pytest.fixture
async def world(factory: async_sessionmaker[Any]) -> Fixtures:
    fixtures = Fixtures(factory)
    await fixtures.setup()
    return fixtures


class TestKeyIsIdentityOnly:
    """Isolation, proven where it is decided: in the derivation."""

    def test_key_ignores_everything_except_agent_identity(self) -> None:
        workspace_id = new_uuid7()
        agent_id = new_uuid7()
        # The same agent in the same tenant, described by two different runs of
        # two different tasks: one key, because a run is not an identity.
        assert ws.agent_workspace_key(workspace_id, agent_id) == ws.agent_workspace_key(
            workspace_id, agent_id
        )
        # A different agent, or the same agent id imagined into another tenant,
        # is a different disk.
        assert ws.agent_workspace_key(workspace_id, new_uuid7()) != ws.agent_workspace_key(
            workspace_id, agent_id
        )
        assert ws.agent_workspace_key(new_uuid7(), agent_id) != ws.agent_workspace_key(
            workspace_id, agent_id
        )

    def test_every_key_satisfies_the_runners_own_shape(self) -> None:
        """A key the runner would reject is a key that fails at the container,
        not at the derivation, so the shape is asserted against the runner's
        own regex rather than a copy of it."""
        for _ in range(200):
            key = ws.agent_workspace_key(new_uuid7(), new_uuid7())
            assert WORKSPACE_KEY_RE.match(key), key
            assert len(key) <= 81
        for _ in range(50):
            assert WORKSPACE_KEY_RE.match(ws.run_workspace_key(new_uuid7()))

    def test_run_key_spelling_is_unchanged(self) -> None:
        """Byte-identical to what shipped before durable workspaces, so the
        volumes, docs and cleanup paths that already exist stay true."""
        run_id = new_uuid7()
        assert ws.run_workspace_key(run_id) == f"run-{run_id}"

    async def test_two_agents_never_share_a_row_or_a_key(self, world: Fixtures) -> None:
        first = await world.agent("Engineer")
        second = await world.agent("Researcher")
        one = await world.bind(first, await world.run(first))
        two = await world.bind(second, await world.run(second))

        assert one.kind == ws.KIND_AGENT and two.kind == ws.KIND_AGENT
        assert one.key != two.key
        assert one.row_id != two.row_id
        assert one.key == ws.agent_workspace_key(world.workspace_id, first)
        assert two.key == ws.agent_workspace_key(world.workspace_id, second)


class TestOneAgentOneDisk:
    async def test_later_runs_of_one_agent_return_to_the_same_disk(self, world: Fixtures) -> None:
        agent = await world.agent("Engineer")
        first_run = await world.run(agent)
        first = await world.bind(agent, first_run)
        await world.set_run_status(first_run, "completed")
        await ws.release_run_bindings(
            world.factory,
            workspace_id=world.workspace_id,
            run_id=first_run,
            delete_workspace=DeletedVolumes(),
        )

        second = await world.bind(agent, await world.run(agent))
        assert second.key == first.key
        assert second.row_id == first.row_id

    async def test_a_bound_run_never_re_evaluates_which_disk_it_is_on(
        self, world: Fixtures
    ) -> None:
        """The renew is the first statement of every bind, so a run that holds
        any binding gets it back — even once the world around it has changed."""
        agent = await world.agent("Engineer")
        run_id = await world.run(agent)
        first = await world.bind(agent, run_id)

        # Somebody marks the workspace for reset and ages it past every limit.
        await world.touch(
            agent,
            reset_requested_at=datetime.now(UTC),
            last_used_at=datetime.now(UTC) - timedelta(days=400),
        )
        deleted = DeletedVolumes()
        again = await world.bind(agent, run_id, delete_workspace=deleted)

        assert again.key == first.key
        assert again.row_id == first.row_id
        # And nothing was destroyed underneath the run that is using it.
        assert deleted.keys == []


class TestConcurrentRunsOfOneAgent:
    async def test_the_second_run_gets_a_private_disk_rather_than_the_agents(
        self, world: Fixtures
    ) -> None:
        agent = await world.agent("Engineer")
        chat = await world.run(agent)
        task = await world.run(agent)

        first = await world.bind(agent, chat)
        second = await world.bind(agent, task)

        assert first.kind == ws.KIND_AGENT
        assert first.contended is False
        # Separate, never shared: the loser works on its own tree with exactly
        # the guarantees every run had before durable workspaces existed.
        assert second.kind == ws.KIND_RUN
        assert second.contended is True
        assert second.key == ws.run_workspace_key(task)
        assert second.key != first.key
        assert second.row_id != first.row_id

        # And the cost is recorded rather than mysterious.
        bound = await world.audit_for(ws.AUDIT_BOUND)
        assert [entry["contended"] for entry in bound] == [False, True]

    async def test_the_contended_run_stays_on_its_private_disk(self, world: Fixtures) -> None:
        agent = await world.agent("Engineer")
        holder = await world.run(agent)
        loser_run = await world.run(agent)
        await world.bind(agent, holder)
        loser = await world.bind(agent, loser_run)
        assert loser.kind == ws.KIND_RUN

        # The holder finishes and releases; the loser must NOT migrate onto the
        # freed disk, because its checkout is on the one it already has.
        await world.set_run_status(holder, "completed")
        await ws.release_run_bindings(
            world.factory,
            workspace_id=world.workspace_id,
            run_id=holder,
            delete_workspace=DeletedVolumes(),
        )
        again = await world.bind(agent, loser_run)
        assert again.key == loser.key

    async def test_a_self_delegating_sub_run_is_just_another_concurrent_run(
        self, world: Fixtures
    ) -> None:
        """A sub-run carries the same agent id, which is exactly the case that
        would share a tree if the key were the only mechanism."""
        agent = await world.agent("Engineer")
        parent = await world.bind(agent, await world.run(agent))
        child = await world.bind(agent, await world.run(agent))
        assert parent.key != child.key


class TestTheLease:
    async def test_a_lease_is_taken_from_a_finished_holder(self, world: Fixtures) -> None:
        agent = await world.agent("Engineer")
        first_run = await world.run(agent)
        first = await world.bind(agent, first_run)
        # No cleanup ran at all — the holder simply reached a terminal status.
        await world.set_run_status(first_run, "failed")

        second = await world.bind(agent, await world.run(agent))
        assert second.kind == ws.KIND_AGENT
        assert second.key == first.key

    async def test_a_lease_is_never_taken_from_a_live_holder_however_old(
        self, world: Fixtures
    ) -> None:
        """Liveness is the holder's own run status, not a clock. A run parked on
        an approval for six hours keeps its workspace."""
        agent = await world.agent("Engineer")
        holder = await world.run(agent, status="waiting_approval")
        held = await world.bind(agent, holder)
        await world.touch(agent, lease_expires_at=datetime.now(UTC) - timedelta(hours=6))

        other = await world.bind(agent, await world.run(agent))
        assert other.kind == ws.KIND_RUN
        assert other.key != held.key

    async def test_no_clock_takes_a_lease_from_a_run_that_never_finalized(
        self, world: Fixtures
    ) -> None:
        """A stale lease is not evidence of a dead run.

        Nothing in ``agent_run`` moves while a run is alive, so a run parked on
        a push approval for two days and a run that crashed without finalizing
        look identical from here. Taking the disk on age alone meant deleting
        the tree of the first one; the contender takes a private disk instead,
        which is what every sandbox job had before durable workspaces existed.
        """
        agent = await world.agent("Engineer")
        crashed = await world.run(agent, status="running")
        held = await world.bind(agent, crashed)
        await world.touch(
            agent, lease_expires_at=datetime.now(UTC) - ws.lease_ceiling() - timedelta(days=30)
        )

        contender = await world.bind(agent, await world.run(agent))
        assert contender.kind == ws.KIND_RUN
        assert contender.key != held.key
        # And the original holder still has its own disk, untouched.
        assert (await world.bind(agent, crashed)).key == held.key

    async def test_the_run_that_lost_a_lease_is_told_so_rather_than_moved(
        self, world: Fixtures
    ) -> None:
        """The only alternative is silently continuing on a fresh disk while the
        checkout sits on the other one, which is a wrong answer rather than a
        degraded one.

        A lease is now only ever lost to an operator's forced reset, so that is
        what this reproduces: the holder cleared, the previous holder recorded,
        and a reset still pending.
        """
        agent = await world.agent("Engineer")
        holder = await world.run(agent, status="running")
        await world.bind(agent, holder)
        await world.touch(
            agent,
            holder_run_id=None,
            last_holder_run_id=holder,
            lease_expires_at=None,
            reset_requested_at=datetime.now(UTC),
        )

        with pytest.raises(ToolExecutionError) as raised:
            await world.bind(agent, holder)
        assert raised.value.code == "workspace_lease_lost"
        assert raised.value.side_effect_possible is False
        assert "check the repository out again" in (raised.value.hint or "").lower()

    async def test_a_run_whose_lease_another_run_took_is_told_so_too(self, world: Fixtures) -> None:
        agent = await world.agent("Engineer")
        holder = await world.run(agent, status="running")
        await world.bind(agent, holder)
        other = await world.run(agent)
        await world.touch(agent, holder_run_id=other, last_holder_run_id=holder)

        with pytest.raises(ToolExecutionError) as raised:
            await world.bind(agent, holder)
        assert raised.value.code == "workspace_lease_lost"

    async def test_an_ordinary_release_is_not_read_as_a_lost_lease(self, world: Fixtures) -> None:
        """``release_run_bindings`` also leaves ``last_holder_run_id`` naming
        the run that finished. That must not look like a stolen lease, or a
        replayed cleanup would turn into a refusal."""
        agent = await world.agent("Engineer")
        run_id = await world.run(agent)
        first = await world.bind(agent, run_id)
        await ws.release_run_bindings(world.factory, workspace_id=world.workspace_id, run_id=run_id)

        again = await world.bind(agent, run_id)
        assert again.key == first.key

    async def test_release_clears_the_holder_without_calling_the_runner(
        self, world: Fixtures
    ) -> None:
        agent = await world.agent("Engineer")
        run_id = await world.run(agent)
        await world.bind(agent, run_id)
        deleted = DeletedVolumes()

        result = await ws.release_run_bindings(
            world.factory,
            workspace_id=world.workspace_id,
            run_id=run_id,
            delete_workspace=deleted,
        )

        assert result is False
        # The whole point: the disk survives the run that made it.
        assert deleted.keys == []
        row = await world.row(agent)
        assert row.holder_run_id is None
        assert row.last_holder_run_id == run_id
        assert ws.AUDIT_RELEASED in await world.audit_actions()

    async def test_release_destroys_a_private_run_disk(self, world: Fixtures) -> None:
        agent = await world.agent("Engineer")
        await world.bind(agent, await world.run(agent))
        loser_run = await world.run(agent)
        loser = await world.bind(agent, loser_run)
        deleted = DeletedVolumes()

        result = await ws.release_run_bindings(
            world.factory,
            workspace_id=world.workspace_id,
            run_id=loser_run,
            delete_workspace=deleted,
        )

        assert result is True
        assert deleted.keys == [loser.key]
        async with world.factory() as session:
            remaining = list(await session.scalars(select(SandboxWorkspace)))
        assert [row.kind for row in remaining] == [ws.KIND_AGENT]


class TestBounds:
    async def test_a_full_workspace_still_lets_the_branch_out(
        self, world: Fixtures, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``workspace_full``'s hint says to push what you have. If the push
        were refused too the hint would be a lie and the branch would be
        stranded until an operator threw the disk away — so the one call that
        makes the situation better is the one call exempt from the cap."""
        monkeypatch.setenv("SANDBOX_WORKSPACE_MAX_MB", "1")
        agent = await world.agent("Engineer")
        run_id = await world.run(agent)
        first = await world.bind(agent, run_id)
        await world.touch(agent, size_bytes=50 * 1024 * 1024)

        with pytest.raises(ToolExecutionError):
            await world.bind(agent, run_id)

        pushing = await ws.bind_workspace(
            world.factory,
            workspace_id=world.workspace_id,
            agent_id=agent,
            run_id=run_id,
            enforce_size=False,
        )
        # And it is the same disk the work is on, not a fresh one.
        assert pushing.key == first.key
        assert pushing.row_id == first.row_id

    async def test_a_workspace_over_its_cap_mid_run_refuses_rather_than_vanishing(
        self, world: Fixtures, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SANDBOX_WORKSPACE_MAX_MB", "1")
        agent = await world.agent("Engineer")
        run_id = await world.run(agent)
        await world.bind(agent, run_id)
        await world.touch(agent, size_bytes=50 * 1024 * 1024)

        deleted = DeletedVolumes()
        with pytest.raises(ToolExecutionError) as raised:
            await world.bind(agent, run_id, delete_workspace=deleted)
        assert raised.value.code == "workspace_full"
        assert raised.value.side_effect_possible is False
        # Uncommitted work is never traded for disk while a run is using it.
        assert deleted.keys == []

    async def test_a_workspace_over_its_cap_is_recycled_when_the_next_run_binds(
        self, world: Fixtures, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SANDBOX_WORKSPACE_MAX_MB", "1")
        agent = await world.agent("Engineer")
        first_run = await world.run(agent)
        held = await world.bind(agent, first_run)
        await world.set_run_status(first_run, "completed")
        await world.touch(agent, size_bytes=50 * 1024 * 1024, holder_run_id=None)

        deleted = DeletedVolumes()
        again = await world.bind(agent, await world.run(agent), delete_workspace=deleted)

        assert again.key == held.key
        assert deleted.keys == [held.key]
        row = await world.row(agent)
        assert row.size_bytes == 0
        assert row.state == ws.STATE_ACTIVE
        assert [entry["reason"] for entry in await world.audit_for(ws.AUDIT_EVICTED)] == ["size"]

    async def test_an_idle_workspace_is_emptied_before_its_next_run_starts(
        self, world: Fixtures
    ) -> None:
        agent = await world.agent("Engineer")
        first_run = await world.run(agent)
        await world.bind(agent, first_run)
        await world.set_run_status(first_run, "completed")
        await world.touch(
            agent, holder_run_id=None, last_used_at=datetime.now(UTC) - timedelta(days=90)
        )

        deleted = DeletedVolumes()
        bound = await world.bind(agent, await world.run(agent), delete_workspace=deleted)

        assert deleted.keys == [bound.key]
        assert [entry["reason"] for entry in await world.audit_for(ws.AUDIT_EVICTED)] == ["idle"]

    async def test_an_operators_reset_is_applied_by_the_next_bind(self, world: Fixtures) -> None:
        agent = await world.agent("Engineer")
        first_run = await world.run(agent)
        await world.bind(agent, first_run)
        await world.set_run_status(first_run, "completed")
        await world.touch(agent, holder_run_id=None, reset_requested_at=datetime.now(UTC))

        deleted = DeletedVolumes()
        bound = await world.bind(agent, await world.run(agent), delete_workspace=deleted)

        assert deleted.keys == [bound.key]
        row = await world.row(agent)
        assert row.reset_requested_at is None
        assert [entry["reason"] for entry in await world.audit_for(ws.AUDIT_RECYCLED)] == ["reset"]

    async def test_a_failed_eviction_does_not_strand_the_agent(self, world: Fixtures) -> None:
        """Fail open. A runner that is momentarily unhappy must not stop an
        agent that has work to do; the next bind tries again."""
        agent = await world.agent("Engineer")
        first_run = await world.run(agent)
        await world.bind(agent, first_run)
        await world.set_run_status(first_run, "completed")
        await world.touch(agent, holder_run_id=None, reset_requested_at=datetime.now(UTC))

        bound = await world.bind(
            agent, await world.run(agent), delete_workspace=DeletedVolumes(succeeds=False)
        )
        assert bound.kind == ws.KIND_AGENT
        assert [entry["deleted"] for entry in await world.audit_for(ws.AUDIT_RECYCLED)] == [False]
        # And the operator's request is still outstanding. Clearing it on a
        # delete the runner refused forgot a reset the tree never had, and the
        # only trace was one ``deleted: false`` in an audit row.
        row = await world.row(agent)
        assert row.reset_requested_at is not None

    async def test_a_reset_the_runner_refused_is_retried_by_the_next_bind(
        self, world: Fixtures
    ) -> None:
        agent = await world.agent("Engineer")
        first_run = await world.run(agent)
        await world.bind(agent, first_run)
        await world.set_run_status(first_run, "completed")
        await world.touch(agent, holder_run_id=None, reset_requested_at=datetime.now(UTC))

        refused = await world.run(agent)
        await world.bind(agent, refused, delete_workspace=DeletedVolumes(succeeds=False))
        await world.set_run_status(refused, "completed")
        await world.touch(agent, holder_run_id=None)

        succeeded = DeletedVolumes()
        bound = await world.bind(agent, await world.run(agent), delete_workspace=succeeded)
        assert succeeded.keys == [bound.key]
        assert (await world.row(agent)).reset_requested_at is None

    async def test_eviction_never_crosses_a_tenant_boundary(
        self, world: Fixtures, factory, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One tenant's bind may not free disk by deleting another's work.

        The sweep runs on somebody's bind, and a bind is an action by one
        tenant's agent. Reading the whole table let a busy tenant delete a
        quiet tenant's durable workspace — the quiet tenant's agent found its
        checkout gone, and its own audit trail said nothing at all.
        """
        monkeypatch.setenv("SANDBOX_WORKSPACE_TOTAL_MAX_MB", "10")
        neighbour = Fixtures(factory)
        await neighbour.setup()
        stranger = await neighbour.agent("Stranger")
        stranger_run = await neighbour.run(stranger)
        stranger_binding = await neighbour.bind(stranger, stranger_run)
        await neighbour.set_run_status(stranger_run, "completed")
        await neighbour.touch(
            stranger,
            holder_run_id=None,
            size_bytes=500 * 1024 * 1024,
            last_used_at=datetime.now(UTC) - timedelta(days=400),
        )

        newcomer = await world.agent("Newcomer")
        deleted = DeletedVolumes()
        await world.bind(newcomer, await world.run(newcomer), delete_workspace=deleted)

        assert deleted.keys == []
        assert (await neighbour.row(stranger)).state == ws.STATE_ACTIVE
        assert stranger_binding.key not in deleted.keys

    async def test_the_budget_charges_the_agent_that_caused_the_overrun(
        self, world: Fixtures, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The overrunning agent is not immune to the sweep it triggers.

        Its own lease made it one: candidates required ``holder_run_id IS
        NULL``, and the binder holds its own row for the whole of its run. So
        the one workspace actually spending the tenant's budget was the one
        workspace guaranteed to survive, and the bytes were taken from
        whichever neighbour was least recently used — the smaller and more
        recently useful disk destroyed on behalf of the larger one.

        Both workspaces here are under the *per-agent* cap (which is itself
        never more than the tenant total), so the only rule in play is the
        tenant budget and the only question is which disk it charges.
        """
        monkeypatch.setenv("SANDBOX_WORKSPACE_TOTAL_MAX_MB", "10")
        neighbour = await world.agent("Neighbour")
        spender = await world.agent("Spender")

        neighbour_run = await world.run(neighbour)
        neighbour_binding = await world.bind(neighbour, neighbour_run)
        await world.set_run_status(neighbour_run, "completed")
        await world.touch(neighbour, holder_run_id=None, size_bytes=5 * 1024 * 1024)

        spender_run = await world.run(spender)
        spender_binding = await world.bind(spender, spender_run)
        await world.set_run_status(spender_run, "completed")
        await world.touch(spender, holder_run_id=None, size_bytes=6 * 1024 * 1024)

        deleted = DeletedVolumes()
        await world.bind(spender, await world.run(spender), delete_workspace=deleted)

        assert deleted.keys == [spender_binding.key]
        assert neighbour_binding.key not in deleted.keys
        assert (await world.row(neighbour)).size_bytes == 5 * 1024 * 1024
        # Recycled rather than evicted: the run is about to use the disk, so
        # the volume goes and the row stays active and held.
        spent = await world.row(spender)
        assert spent.state == ws.STATE_ACTIVE
        assert spent.size_bytes == 0
        assert [entry["reason"] for entry in await world.audit_for(ws.AUDIT_EVICTED)] == [
            "total_size"
        ]

    async def test_the_push_exemption_reaches_the_budget_sweep(
        self, world: Fixtures, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``enforce_size=False`` is the call that gets the work out.

        Charging the overrun to the binder is right on every other call and
        wrong on this one: destroying this agent's disk here would delete the
        branch the push was about to send.
        """
        monkeypatch.setenv("SANDBOX_WORKSPACE_TOTAL_MAX_MB", "10")
        spender = await world.agent("Spender")
        spender_run = await world.run(spender)
        spender_binding = await world.bind(spender, spender_run)
        await world.set_run_status(spender_run, "completed")
        await world.touch(spender, holder_run_id=None, size_bytes=100 * 1024 * 1024)

        deleted = DeletedVolumes()
        await world.bind(
            spender, await world.run(spender), delete_workspace=deleted, enforce_size=False
        )

        assert deleted.keys == []
        assert (await world.row(spender)).size_bytes == 100 * 1024 * 1024
        assert spender_binding.key not in deleted.keys

    async def test_an_eviction_the_runner_refused_leaves_the_row_as_it_was(
        self, world: Fixtures, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The sweep's half of the defect ``_recycle`` was fixed for.

        A delete the runner refuses used to be written as ``evicted, 0``. The
        next bind then took the "the volume is already gone" branch, attempted
        no delete, and returned an untouched full disk to service recorded as
        empty — permanently invisible to the cap and to every future sweep.
        """
        monkeypatch.setenv("SANDBOX_WORKSPACE_TOTAL_MAX_MB", "10")
        stale = await world.agent("Stale")
        newcomer = await world.agent("Newcomer")

        stale_run = await world.run(stale)
        stale_binding = await world.bind(stale, stale_run)
        await world.set_run_status(stale_run, "completed")
        measured = datetime.now(UTC) - timedelta(hours=1)
        await world.touch(
            stale,
            holder_run_id=None,
            size_bytes=11 * 1024 * 1024,
            size_measured_at=measured,
        )

        refused = DeletedVolumes(succeeds=False)
        with pytest.raises(ToolExecutionError) as raised:
            await world.bind(newcomer, await world.run(newcomer), delete_workspace=refused)

        # And the bind is refused, because the sweep did not achieve what it
        # set out to: the plan was one eviction, the runner refused it, and
        # the tenant is exactly as far over budget as before. Serving the bind
        # here -- on the grounds that a refused delete is a machine's bad
        # moment rather than a state of the tenant -- is how a half-executed
        # plan gets to destroy neighbours *and* hand out the disk they paid
        # for.
        assert raised.value.code == "workspace_tenant_full"
        assert refused.keys == [stale_binding.key]
        row = await world.row(stale)
        assert row.state == ws.STATE_ACTIVE
        assert row.size_bytes == 11 * 1024 * 1024
        assert row.size_state == ws.SIZE_MEASURED
        assert row.size_measured_at is not None
        assert [entry["deleted"] for entry in await world.audit_for(ws.AUDIT_EVICTED)] == [False]

    async def test_the_global_budget_evicts_the_largest_and_never_a_held_one(
        self, world: Fixtures, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SANDBOX_WORKSPACE_TOTAL_MAX_MB", "10")
        stale = await world.agent("Stale")
        busy = await world.agent("Busy")
        newcomer = await world.agent("Newcomer")

        stale_run = await world.run(stale)
        stale_binding = await world.bind(stale, stale_run)
        await world.set_run_status(stale_run, "completed")
        await world.touch(
            stale,
            holder_run_id=None,
            size_bytes=8 * 1024 * 1024,
            last_used_at=datetime.now(UTC) - timedelta(hours=5),
        )
        busy_binding = await world.bind(busy, await world.run(busy))
        await world.touch(
            busy,
            size_bytes=8 * 1024 * 1024,
            last_used_at=datetime.now(UTC) - timedelta(hours=9),
        )

        deleted = DeletedVolumes()
        await world.bind(newcomer, await world.run(newcomer), delete_workspace=deleted)

        # ``busy`` is older but a live run holds it, and it is not the binder's
        # own row, so it is never a victim.
        assert deleted.keys == [stale_binding.key]
        assert busy_binding.key not in deleted.keys
        assert (await world.row(stale)).state == ws.STATE_EVICTED
        assert (await world.row(busy)).state == ws.STATE_ACTIVE

    def test_the_per_agent_cap_never_exceeds_the_tenant_budget(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The structural half of the eviction fix.

        A per-agent cap above the tenant total lets one agent put its tenant
        over budget by itself. If that agent is holding its workspace — an
        overlapping run, or a run parked on an approval for a day — no sweep
        can bring the tenant back under, and every other agent in the tenant
        is refused for a situation none of them caused and none of them can
        fix. Bounded here, the cost lands on whoever spent it: the
        overspender's own next bind recycles its own disk.
        """
        monkeypatch.setenv("SANDBOX_WORKSPACE_MAX_MB", "5120")
        monkeypatch.setenv("SANDBOX_WORKSPACE_TOTAL_MAX_MB", "50")
        assert ws.max_workspace_bytes() == 50 * 1024 * 1024
        assert ws.max_workspace_bytes() <= ws.total_workspace_bytes()
        # And below the total it is still the configured number.
        monkeypatch.setenv("SANDBOX_WORKSPACE_MAX_MB", "20")
        assert ws.max_workspace_bytes() == 20 * 1024 * 1024

    async def test_a_sweep_that_cannot_reach_the_overrun_destroys_nothing(
        self, world: Fixtures, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The reproduction, exactly: five volumes destroyed to free 20 KiB.

        Budget 50 MB. One workspace at 100 MB held by a run that is still
        going — the ordinary case, not a rare one: runs of an agent overlap,
        and a run parked on a push approval holds its lease for as long as the
        approval takes. Four unheld neighbours at 4 KiB each, and a binder at
        4 KiB. Largest-first with no feasibility test destroyed all five,
        freed 20,480 bytes against a 52 MB overrun, left the tenant over
        budget, refused nothing, and never went near the disk that was
        actually spending the budget.

        Destroying another agent's durable work is only justified when it
        achieves the thing it is destroying work for. So the sweep takes
        nothing and the call is refused, naming the real situation.
        """
        monkeypatch.setenv("SANDBOX_WORKSPACE_TOTAL_MAX_MB", "50")
        spender = await world.agent("Spender")
        spender_run = await world.run(spender)
        spender_binding = await world.bind(spender, spender_run)

        neighbours = []
        for index in range(4):
            agent = await world.agent(f"Neighbour {index}")
            run_id = await world.run(agent)
            neighbours.append(await world.bind(agent, run_id))
            await world.set_run_status(run_id, "completed")
            await world.touch(agent, holder_run_id=None, size_bytes=4096)

        binder = await world.agent("Binder")
        binder_run = await world.run(binder)
        await world.bind(binder, binder_run)
        await world.set_run_status(binder_run, "completed")
        await world.touch(binder, holder_run_id=None, size_bytes=4096)

        # Written last, so every other row was bound while the tenant was
        # still under budget: the overrun appears between two of the
        # spender's own jobs, which is exactly how it appears in life — one
        # job's writes, measured after the fact — and the run that made it is
        # still going.
        await world.touch(spender, size_bytes=100 * 1024 * 1024)

        deleted = DeletedVolumes()
        binding_run = await world.run(binder)
        with pytest.raises(ToolExecutionError) as raised:
            await world.bind(binder, binding_run, delete_workspace=deleted)

        assert raised.value.code == "workspace_tenant_full"
        assert raised.value.side_effect_possible is False
        # The same run's next call is refused too rather than served by the
        # renewal path on a lease the first refusal left behind.
        with pytest.raises(ToolExecutionError) as again:
            await world.bind(binder, binding_run, delete_workspace=deleted)
        assert again.value.code == "workspace_tenant_full"
        # Nothing was destroyed. Not the neighbours, not the binder's own.
        assert deleted.keys == []
        assert spender_binding.key not in deleted.keys
        for binding in neighbours:
            assert binding.key not in deleted.keys
        assert await world.audit_for(ws.AUDIT_EVICTED) == []
        assert (await world.row(spender)).size_bytes == 100 * 1024 * 1024
        # And the decision is visible rather than an absence: a decision to
        # destroy nothing is recorded as explicitly as a decision to destroy
        # something, with how far over the tenant was when it was taken.
        refusals = await world.audit_for(ws.AUDIT_BUDGET_REFUSED)
        # One per refused call: both attempts above, each recorded.
        assert len(refusals) == 2
        assert refusals[0]["over_budget_bytes"] == (100 * 1024 * 1024 + 5 * 4096 - 50 * 1024 * 1024)
        assert refusals[0]["budget_bytes"] == 50 * 1024 * 1024
        # A refused bind leaves the run holding nothing, so the refusal is a
        # bound rather than one confusing hiccup before the renewal path
        # serves the very next call.
        assert (await world.row(binder)).holder_run_id is None

    async def test_the_refusal_ends_when_the_holder_finishes(
        self, world: Fixtures, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Taking nothing is not giving up: the situation resolves itself.

        The overspender's lease is released when its run finalizes, and the
        very next bind can reach it — so the refusal lasts exactly as long as
        the run that made it unreachable, and it is *that* disk that pays.

        The holder finalizes through :func:`release_run_bindings`, the path a
        real run takes, because "the lease comes back" is the whole mechanism
        being asserted.
        """
        monkeypatch.setenv("SANDBOX_WORKSPACE_TOTAL_MAX_MB", "50")
        spender = await world.agent("Spender")
        spender_run = await world.run(spender)
        spender_binding = await world.bind(spender, spender_run)

        binder = await world.agent("Binder")
        binder_run = await world.run(binder)
        await world.bind(binder, binder_run)
        await world.set_run_status(binder_run, "completed")
        await world.touch(binder, holder_run_id=None, size_bytes=4096)

        await world.touch(spender, size_bytes=100 * 1024 * 1024)

        refused_run = await world.run(binder)
        with pytest.raises(ToolExecutionError) as raised:
            await world.bind(binder, refused_run)
        assert raised.value.code == "workspace_tenant_full"
        await world.set_run_status(refused_run, "completed")

        # The holder finishes and gives the lease back.
        await world.set_run_status(spender_run, "completed")
        await ws.release_run_bindings(
            world.factory, workspace_id=world.workspace_id, run_id=spender_run
        )

        deleted = DeletedVolumes()
        await world.bind(binder, await world.run(binder), delete_workspace=deleted)
        assert deleted.keys == [spender_binding.key]
        assert (await world.row(spender)).state == ws.STATE_EVICTED

    async def test_the_push_exemption_survives_a_tenant_over_budget(
        self, world: Fixtures, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The one call that makes the situation better is never refused.

        ``cli.repository.push`` gets the work out of a workspace, so it is
        exempt from the tenant refusal for the same reason it is exempt from
        ``workspace_full`` — and its own disk is still not swept out from
        under it.
        """
        monkeypatch.setenv("SANDBOX_WORKSPACE_TOTAL_MAX_MB", "50")
        spender = await world.agent("Spender")
        spender_run = await world.run(spender)
        await world.bind(spender, spender_run)

        pusher = await world.agent("Pusher")
        pusher_run = await world.run(pusher)
        first = await world.bind(pusher, pusher_run)
        await world.set_run_status(pusher_run, "completed")
        await world.touch(pusher, holder_run_id=None, size_bytes=4096)

        await world.touch(spender, size_bytes=100 * 1024 * 1024)

        deleted = DeletedVolumes()
        pushing = await world.bind(
            pusher,
            await world.run(pusher),
            delete_workspace=deleted,
            enforce_size=False,
        )
        assert pushing.key == first.key
        assert deleted.keys == []

    async def test_a_sweep_that_can_reach_the_overrun_still_takes_it(
        self, world: Fixtures, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The gate refuses the impossible sweep, not every sweep.

        Same shape as the reproduction with one thing changed — the
        overspender's run has finished — and the answer flips: one workspace
        is destroyed, it is the one that spent the budget, and the four
        neighbours and the binder keep their disks because taking them is not
        needed to get under.
        """
        monkeypatch.setenv("SANDBOX_WORKSPACE_TOTAL_MAX_MB", "50")
        spender = await world.agent("Spender")
        spender_run = await world.run(spender)
        spender_binding = await world.bind(spender, spender_run)
        await world.set_run_status(spender_run, "completed")

        neighbours = []
        for index in range(4):
            agent = await world.agent(f"Neighbour {index}")
            run_id = await world.run(agent)
            neighbours.append(await world.bind(agent, run_id))
            await world.set_run_status(run_id, "completed")
            await world.touch(agent, holder_run_id=None, size_bytes=4096)

        # Written after every other bind, so none of them swept a tenant that
        # was already over budget: the overrun appears between two jobs, which
        # is how it appears in life.
        await world.touch(spender, holder_run_id=None, size_bytes=100 * 1024 * 1024)

        binder = await world.agent("Binder")
        deleted = DeletedVolumes()
        await world.bind(binder, await world.run(binder), delete_workspace=deleted)

        assert deleted.keys == [spender_binding.key]
        for binding in neighbours:
            assert binding.key not in deleted.keys

    async def test_an_evicted_workspace_comes_back_into_service_on_the_next_bind(
        self, world: Fixtures
    ) -> None:
        agent = await world.agent("Engineer")
        first_run = await world.run(agent)
        bound = await world.bind(agent, first_run)
        await world.set_run_status(first_run, "completed")
        await world.touch(agent, holder_run_id=None, state=ws.STATE_EVICTED, size_bytes=99)

        deleted = DeletedVolumes()
        await world.bind(agent, await world.run(agent), delete_workspace=deleted)

        row = await world.row(agent)
        assert row.state == ws.STATE_ACTIVE
        assert row.size_bytes == 0
        # The delete is repeated rather than assumed. An evicted row is
        # supposed to mean the volume is gone, and rows written before that
        # was true are still in the table — one idempotent call is what makes
        # the row's claim about the disk a fact rather than a hope.
        assert deleted.keys == [bound.key]

    async def test_an_evicted_row_whose_volume_survived_is_not_returned_as_empty(
        self, world: Fixtures
    ) -> None:
        """The row says evicted, the runner says the volume is still there.

        That row was written by a version that recorded a refused delete as an
        eviction, so believing it hands an agent a full disk recorded as empty.
        The bind refuses to take the row's word for it.
        """
        agent = await world.agent("Engineer")
        first_run = await world.run(agent)
        await world.bind(agent, first_run)
        await world.set_run_status(first_run, "completed")
        await world.touch(agent, holder_run_id=None, state=ws.STATE_EVICTED, size_bytes=99)

        refused = DeletedVolumes(succeeds=False)
        await world.bind(agent, await world.run(agent), delete_workspace=refused)

        assert len(refused.keys) == 1
        assert [entry["deleted"] for entry in await world.audit_for(ws.AUDIT_RECYCLED)] == [False]


class TestTheSweepSeesEveryDisk:
    """The two ways a real disk used to be invisible to the thing bounding it.

    Both are the same mistake in different clothes: the sweep asked a question
    the rest of the module answers differently, and every byte behind the
    disagreement was spent against nobody's account.
    """

    async def test_a_holder_that_finished_without_finalizing_is_not_a_holder(
        self, world: Fixtures, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``_acquire`` and ``_sweep`` used to disagree about "held".

        The acquire reads the holder's ``agent_run.status``; the sweep read
        whether ``holder_run_id`` was set. A run that finished without
        finalizing leaves its id on the row, and the two readings then
        disagree: the acquire would hand the disk out, while the sweep treated
        it as untouchable and excluded it from the idle pass, from the budget
        pass, and from any hope of freeing the bytes it was still counting. One
        such row was enough to leave a tenant permanently over budget with
        nothing any bind could free.
        """
        monkeypatch.setenv("SANDBOX_WORKSPACE_TOTAL_MAX_MB", "50")
        stranded = await world.agent("Stranded")
        stranded_run = await world.run(stranded)
        stranded_binding = await world.bind(stranded, stranded_run)
        # Terminal run, lease never given back: the state ``_acquire``'s own
        # docstring acknowledges.
        await world.set_run_status(stranded_run, "completed")
        await world.touch(stranded, size_bytes=100 * 1024 * 1024)
        assert (await world.row(stranded)).holder_run_id == stranded_run

        binder = await world.agent("Binder")
        deleted = DeletedVolumes()
        bound = await world.bind(binder, await world.run(binder), delete_workspace=deleted)

        assert deleted.keys == [stranded_binding.key]
        assert bound.durable is True
        assert (await world.row(stranded)).state == ws.STATE_EVICTED

    async def test_a_run_scoped_disk_counts_against_the_tenant(
        self, world: Fixtures, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A contended run's private disk is real bytes on the same host.

        Selecting only ``kind='agent'`` left them measured, stored, occupying
        disk and contributing nothing: a 10 GiB run-scoped workspace sat
        inside a 52 MB budget with the total reading zero, and a third agent's
        bind was served. The exposure was the agent budget plus one
        per-workspace cap for every concurrent contended run, with nothing
        bounding the second term.
        """
        monkeypatch.setenv("SANDBOX_WORKSPACE_TOTAL_MAX_MB", "50")
        busy = await world.agent("Busy")
        first_run = await world.run(busy)
        await world.bind(busy, first_run)
        # A second, overlapping run of the same agent: it loses the contention
        # and takes a private run-scoped disk.
        contender = await world.run(busy)
        private = await world.bind(busy, contender)
        assert private.kind == ws.KIND_RUN
        assert private.row_id is not None
        await world.touch_row(private.row_id, size_bytes=10 * 1024 * 1024 * 1024)

        binder = await world.agent("Binder")
        deleted = DeletedVolumes()
        with pytest.raises(ToolExecutionError) as raised:
            await world.bind(binder, await world.run(binder), delete_workspace=deleted)

        assert raised.value.code == "workspace_tenant_full"
        # The live run keeps its tree: the tenant is refused instead, exactly
        # as it is for a held agent workspace.
        assert deleted.keys == []

    async def test_a_run_scoped_disk_of_a_finished_run_is_reclaimable(
        self, world: Fixtures, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """And when that run finishes without its cleanup running, the disk it
        left is a candidate like any other -- which is what stops the refusal
        above from being permanent."""
        monkeypatch.setenv("SANDBOX_WORKSPACE_TOTAL_MAX_MB", "50")
        busy = await world.agent("Busy")
        first_run = await world.run(busy)
        await world.bind(busy, first_run)
        contender = await world.run(busy)
        private = await world.bind(busy, contender)
        assert private.row_id is not None
        await world.touch_row(private.row_id, size_bytes=10 * 1024 * 1024 * 1024)
        await world.set_run_status(contender, "failed")

        binder = await world.agent("Binder")
        deleted = DeletedVolumes()
        bound = await world.bind(binder, await world.run(binder), delete_workspace=deleted)

        assert deleted.keys == [private.key]
        assert bound.durable is True

    async def test_a_plan_a_refused_delete_broke_stops_and_refuses(
        self, world: Fixtures, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """All-or-nothing has to hold inside the loop, not only before it.

        Two spenders, both reachable, and a plan that needs both. The runner
        refuses the second delete, so the plan cannot get the tenant under
        budget any more. The loop stops rather than destroying the rest -- and
        the bind is refused, because the first spender's disk is already gone
        and serving the call would make that destruction free for the caller
        and pointless for the tenant.
        """
        monkeypatch.setenv("SANDBOX_WORKSPACE_TOTAL_MAX_MB", "10")

        class RefusesTheSecond:
            def __init__(self) -> None:
                self.keys: list[str] = []

            async def __call__(self, key: str) -> bool:
                self.keys.append(key)
                return len(self.keys) == 1

        spender_agents = []
        spenders = []
        for index in range(2):
            agent = await world.agent(f"Spender {index}")
            run_id = await world.run(agent)
            spenders.append(await world.bind(agent, run_id))
            await world.set_run_status(run_id, "completed")
            spender_agents.append(agent)

        small = await world.agent("Small")
        small_run = await world.run(small)
        small_binding = await world.bind(small, small_run)
        await world.set_run_status(small_run, "completed")

        # Sizes last, so every bind above ran against a tenant under budget.
        for agent in spender_agents:
            await world.touch(agent, holder_run_id=None, size_bytes=40 * 1024 * 1024)
        await world.touch(small, holder_run_id=None, size_bytes=4096)
        keys = [binding.key for binding in spenders]

        binder = await world.agent("Binder")
        deleter = RefusesTheSecond()
        with pytest.raises(ToolExecutionError) as raised:
            await world.bind(binder, await world.run(binder), delete_workspace=deleter)

        assert raised.value.code == "workspace_tenant_full"
        # Two asked for, one gone, and the small neighbour never touched: the
        # loop stopped the moment the plan stopped adding up.
        assert deleter.keys == keys
        assert small_binding.key not in deleter.keys
        assert (await world.row(small)).state == ws.STATE_ACTIVE


class TestSizeMeasurement:
    async def test_a_complete_measurement_is_stored(self, world: Fixtures) -> None:
        agent = await world.agent("Engineer")
        bound = await world.bind(agent, await world.run(agent))
        await ws.record_size(world.factory, row_id=bound.row_id, size_bytes=4096, partial=False)
        row = await world.row(agent)
        assert row.size_bytes == 4096
        assert row.size_state == ws.SIZE_MEASURED

    async def test_a_partial_measurement_is_a_floor_and_says_so(self, world: Fixtures) -> None:
        """A walk that stopped early reports a floor, and the row says floor.

        It is stored rather than discarded -- a floor above the cap proves the
        disk is over it, and an operator would rather read "at least this
        much, incomplete" than nothing -- but nothing downstream may read it
        as a size.
        """
        agent = await world.agent("Engineer")
        bound = await world.bind(agent, await world.run(agent))
        await ws.record_size(world.factory, row_id=bound.row_id, size_bytes=10, partial=True)
        row = await world.row(agent)
        assert row.size_bytes == 10
        assert row.size_state == ws.SIZE_UNKNOWN

    async def test_a_partial_measurement_replaces_a_complete_one(self, world: Fixtures) -> None:
        """The ratchet, and why keeping the larger number was not conservative.

        A floor used to be written only when it exceeded what was stored, so
        that a partial walk could never undo a complete one. What that built
        was a paper size with no expiry: one complete measurement of 4.9 GB,
        then six walks that all ran out of budget, and the row still said
        4.9 GB days after the agent deleted the data. The budget sweep takes
        the largest first, so that agent was first in line to have its live
        work destroyed to free space that had already been freed.

        The state flag is what makes replacing safe: the floor cannot be
        mistaken for a size, so it does not have to be inflated to be safe.
        """
        agent = await world.agent("Engineer")
        bound = await world.bind(agent, await world.run(agent))
        gigabytes = 4_900 * 1024 * 1024
        await ws.record_size(
            world.factory, row_id=bound.row_id, size_bytes=gigabytes, partial=False
        )
        for _ in range(6):
            await ws.record_size(
                world.factory, row_id=bound.row_id, size_bytes=20 * 1024 * 1024, partial=True
            )
        row = await world.row(agent)
        assert row.size_bytes == 20 * 1024 * 1024
        assert row.size_state == ws.SIZE_UNKNOWN

    async def test_no_measurement_leaves_the_stored_number_alone(self, world: Fixtures) -> None:
        agent = await world.agent("Engineer")
        bound = await world.bind(agent, await world.run(agent))
        await ws.record_size(world.factory, row_id=bound.row_id, size_bytes=555, partial=False)
        await ws.record_size(world.factory, row_id=bound.row_id, size_bytes=None, partial=False)
        row = await world.row(agent)
        assert row.size_bytes == 555
        assert row.size_state == ws.SIZE_MEASURED


class TestADiskNobodyCanMeasure:
    """What the platform does with a workspace whose size is not known.

    The reproduction: 3,000 directories of 1,000 empty files with a 6 GiB
    payload in the directory ``scandir`` returns last. Through the deployed
    runner the job's own ``du -sB1`` read 6,530,826,240 while the walk, out of
    budget, reported 12 to 21 MB and marked itself incomplete -- 0.3% of the
    truth, under every cap in the product. Stored as a size, that is the cap
    not existing. Stored as a floor, it is a question the platform answers by
    refusing rather than by guessing in either direction.
    """

    async def test_an_unmeasurable_disk_refuses_the_next_call(self, world: Fixtures) -> None:
        agent = await world.agent("Engineer")
        first = await world.run(agent)
        await world.bind(agent, first)
        await world.set_run_status(first, "completed")
        # A floor far under the cap, exactly as the reproduction produced.
        await world.touch(
            agent,
            holder_run_id=None,
            size_bytes=21 * 1024 * 1024,
            size_state=ws.SIZE_UNKNOWN,
        )

        deleted = DeletedVolumes()
        with pytest.raises(ToolExecutionError) as raised:
            await world.bind(agent, await world.run(agent), delete_workspace=deleted)

        assert raised.value.code == "workspace_unmeasured"
        assert raised.value.side_effect_possible is False
        # Refused, not emptied: "we do not know" is not evidence, and the tree
        # may be a day's work.
        assert deleted.keys == []
        row = await world.row(agent)
        assert row.state == ws.STATE_ACTIVE
        assert row.size_bytes == 21 * 1024 * 1024
        # And the refusal leaves no lease behind, so it repeats instead of
        # being served by the next call's renewal.
        assert row.holder_run_id is None
        refusals = await world.audit_for(ws.AUDIT_SIZE_REFUSED)
        assert len(refusals) == 1
        assert refusals[0]["size_floor_bytes"] == 21 * 1024 * 1024

    async def test_a_run_already_on_an_unmeasurable_disk_is_refused_too(
        self, world: Fixtures
    ) -> None:
        """The renewal path, which is where an agent filling its disk lives.

        The measurement is always one job stale -- it is taken by the init
        container of the job that then runs -- so the call that makes a disk
        unmeasurable is never the call that is refused. The next one is.
        """
        agent = await world.agent("Engineer")
        run_id = await world.run(agent)
        await world.bind(agent, run_id)
        await world.touch(agent, size_bytes=1024, size_state=ws.SIZE_UNKNOWN)

        with pytest.raises(ToolExecutionError) as raised:
            await world.bind(agent, run_id)
        assert raised.value.code == "workspace_unmeasured"
        # The lease is not given back here: this run still holds the disk and
        # is still allowed to push off it.
        assert (await world.row(agent)).holder_run_id == run_id

    async def test_the_push_still_gets_the_branch_out(self, world: Fixtures) -> None:
        """The refusal's own hint says to push, so the push cannot be refused
        by it -- the same exemption ``workspace_full`` has, for the same
        reason."""
        agent = await world.agent("Engineer")
        run_id = await world.run(agent)
        await world.bind(agent, run_id)
        await world.touch(agent, size_bytes=1024, size_state=ws.SIZE_UNKNOWN)

        pushing = await world.bind(agent, run_id, enforce_size=False)
        assert pushing.kind == ws.KIND_AGENT

    async def test_a_floor_above_the_cap_is_still_a_proof_and_recycles(
        self, world: Fixtures, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The one thing an incomplete walk *can* prove.

        A floor of 6 GiB under a 5 GiB cap says the disk is over its cap
        whatever the rest of the walk would have counted, so this is not a
        destructive action on a number nobody has. It is the agent's own disk,
        at the one moment it is provably idle, and the cost lands on whoever
        spent it.
        """
        monkeypatch.setenv("SANDBOX_WORKSPACE_MAX_MB", "100")
        agent = await world.agent("Engineer")
        first = await world.run(agent)
        bound = await world.bind(agent, first)
        await world.set_run_status(first, "completed")
        await world.touch(
            agent,
            holder_run_id=None,
            size_bytes=200 * 1024 * 1024,
            size_state=ws.SIZE_UNKNOWN,
        )

        deleted = DeletedVolumes()
        rebound = await world.bind(agent, await world.run(agent), delete_workspace=deleted)

        assert deleted.keys == [bound.key]
        assert rebound.key == bound.key
        row = await world.row(agent)
        # An emptied volume is a size again, which is what lets the disk come
        # back into service instead of refusing forever.
        assert row.size_bytes == 0
        assert row.size_state == ws.SIZE_MEASURED

    async def test_an_operators_reset_ends_the_refusal(self, world: Fixtures) -> None:
        """The remedy the hint names, proven to work.

        A refusal an operator cannot clear is a workspace bricked for good, so
        the reset branch runs before the size question is asked and the disk
        that comes back is empty and known.
        """
        agent = await world.agent("Engineer")
        first = await world.run(agent)
        bound = await world.bind(agent, first)
        await world.set_run_status(first, "completed")
        await world.touch(
            agent,
            holder_run_id=None,
            size_bytes=21 * 1024 * 1024,
            size_state=ws.SIZE_UNKNOWN,
            reset_requested_at=datetime.now(UTC),
        )

        deleted = DeletedVolumes()
        rebound = await world.bind(agent, await world.run(agent), delete_workspace=deleted)

        assert deleted.keys == [bound.key]
        row = await world.row(agent)
        assert row.size_bytes == 0
        assert row.size_state == ws.SIZE_MEASURED
        assert row.reset_requested_at is None
        assert rebound.durable is True

    async def test_an_unmeasurable_neighbour_is_charged_but_never_taken(
        self, world: Fixtures, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Both halves of "a size Jhin does not have is not a small size".

        Charged: an unmeasurable disk counts against the tenant at the cap it
        is allowed to fill, so a disk nobody counted cannot be spent for free.
        Never paid for with somebody else's work: the overrun here is made
        entirely of that charge, and the neighbour whose disk *is* measured
        and *is* reachable keeps it. Destruction is planned against the bytes
        this table has actually seen; the refusal is decided against the bytes
        that cannot be ruled out. Only one of those two decisions may rest on
        a number nobody has.
        """
        monkeypatch.setenv("SANDBOX_WORKSPACE_TOTAL_MAX_MB", "50")
        monkeypatch.setenv("SANDBOX_WORKSPACE_MAX_MB", "30")
        opaque = await world.agent("Opaque")
        opaque_run = await world.run(opaque)
        opaque_binding = await world.bind(opaque, opaque_run)
        await world.set_run_status(opaque_run, "completed")

        neighbour = await world.agent("Neighbour")
        neighbour_run = await world.run(neighbour)
        neighbour_binding = await world.bind(neighbour, neighbour_run)
        await world.set_run_status(neighbour_run, "completed")

        binder = await world.agent("Binder")
        # 25 MB of measured, reachable disk and a 4 KiB floor charged at the
        # 30 MB cap: 55 MB against a 50 MB budget, all of the overrun coming
        # from the disk nobody could count.
        await world.touch(neighbour, holder_run_id=None, size_bytes=25 * 1024 * 1024)
        await world.touch(opaque, holder_run_id=None, size_bytes=4096, size_state=ws.SIZE_UNKNOWN)

        deleted = DeletedVolumes()
        with pytest.raises(ToolExecutionError) as raised:
            await world.bind(binder, await world.run(binder), delete_workspace=deleted)

        assert raised.value.code == "workspace_tenant_full"
        assert deleted.keys == []
        assert opaque_binding.key not in deleted.keys
        assert neighbour_binding.key not in deleted.keys
        assert (await world.row(opaque)).size_bytes == 4096
        assert (await world.row(neighbour)).state == ws.STATE_ACTIVE


class TestWithoutADurableStore:
    """Every construction in the tool worker carries a session factory. Where
    one is absent there is no way to prove who owns a shared disk, so none is
    taken — the fail-safe direction, and byte-for-byte the old behaviour."""

    async def test_no_factory_falls_back_to_the_run_scoped_key(self) -> None:
        run_id = new_uuid7()
        bound = await ws.bind_workspace(
            None, workspace_id=new_uuid7(), agent_id=new_uuid7(), run_id=run_id
        )
        assert bound.key == f"run-{run_id}"
        assert bound.kind == ws.KIND_RUN
        assert bound.durable is False
        assert bound.row_id is None
        # And the checkout record keeps the shape it had before this table.
        assert bound.record_target_type == ws.CHECKOUT_TARGET_RUN
        assert bound.record_target_id == run_id


class TestTheCheckoutRecordFollowsTheDisk:
    """The re-keying that makes persistence usable rather than decorative."""

    async def test_a_later_run_of_one_agent_finds_the_record_the_earlier_one_wrote(
        self, world: Fixtures
    ) -> None:
        from jhin_connectors.cli import tools as cli_tools
        from jhin_tools.builtin import ToolExecutionContext

        agent = await world.agent("Engineer")
        first_run = await world.run(agent)
        async with world.factory() as session:
            context = ToolExecutionContext(
                session=session,
                workspace_id=world.workspace_id,
                task_id=new_uuid7(),
                run_id=first_run,
                agent_id=agent,
                agent_name="Engineer",
                session_factory=world.factory,
            )
            binding = await cli_tools._binding(context)
            cli_tools._record_checkout(
                context,
                binding,
                {
                    "repository": "octo/alpha",
                    "branch": "agent/fix",
                    "base_ref": "main",
                    "head_sha": "0" * 40,
                    "config_sha": "c" * 64,
                },
            )
            await session.commit()

        await world.set_run_status(first_run, "completed")
        await ws.release_run_bindings(
            world.factory,
            workspace_id=world.workspace_id,
            run_id=first_run,
            delete_workspace=DeletedVolumes(),
        )

        # Tomorrow's chat turn: a different run, the same agent, the same disk.
        async with world.factory() as session:
            later = replace(
                context, session=session, run_id=await world.run(agent), task_id=new_uuid7()
            )
            binding = await cli_tools._binding(later)
            record = await cli_tools._checkout_record(later, binding, "octo/alpha")
        assert record["base_ref"] == "main"
        assert record["config_sha"] == "c" * 64

    async def test_a_contended_run_only_ever_sees_its_own_record(self, world: Fixtures) -> None:
        from jhin_connectors.cli import tools as cli_tools
        from jhin_tools.builtin import ToolExecutionContext

        agent = await world.agent("Engineer")
        holder = await world.run(agent)
        async with world.factory() as session:
            context = ToolExecutionContext(
                session=session,
                workspace_id=world.workspace_id,
                task_id=new_uuid7(),
                run_id=holder,
                agent_id=agent,
                agent_name="Engineer",
                session_factory=world.factory,
            )
            cli_tools._record_checkout(
                context,
                await cli_tools._binding(context),
                {"repository": "octo/alpha", "base_ref": "main", "config_sha": "c" * 64},
            )
            await session.commit()

        async with world.factory() as session:
            loser = replace(
                context, session=session, run_id=await world.run(agent), task_id=new_uuid7()
            )
            binding = await cli_tools._binding(loser)
            assert binding.kind == ws.KIND_RUN
            with pytest.raises(ToolExecutionError) as raised:
                await cli_tools._checkout_record(loser, binding, "octo/alpha")
        assert raised.value.code == "no_checkout_record"


class TestTheAllowListReachesTheDisk:
    """A repository the connection no longer allows must not stay readable.

    Every tool that names a repository was already checked. A durable
    workspace made the tools that name *none* — file reads, searches, tests,
    commands — into the way round the list: yesterday's allowed checkout is
    still in ``/workspace/repo`` today.
    """

    async def _connection(self, world: Fixtures, allowed: list[str]) -> UUID:
        from jhin_db.models import Connection

        connection_id = new_uuid7()
        async with world.factory() as session:
            session.add(
                Connection(
                    id=connection_id,
                    workspace_id=world.workspace_id,
                    connector_type="cli",
                    name=f"cli-{connection_id.hex[-8:]}",
                    auth_type="none",
                    config_json={
                        "default_image": "jhin-sandbox:latest",
                        "allowed_repositories": allowed,
                    },
                )
            )
            await session.commit()
        return connection_id

    async def _decision(self, world: Fixtures, agent: UUID, run_id: UUID, connection_id: UUID):
        from jhin_connectors.cli.schemas import FileReadInput
        from jhin_connectors.cli.validators import workspace_repository_validator
        from jhin_tools.builtin import ToolExecutionContext

        async with world.factory() as session:
            context = ToolExecutionContext(
                session=session,
                workspace_id=world.workspace_id,
                task_id=new_uuid7(),
                run_id=run_id,
                agent_id=agent,
                agent_name="Engineer",
                session_factory=world.factory,
            )
            return await workspace_repository_validator(
                context,
                FileReadInput(connection_id=str(connection_id), path="src/app.py"),
                [],
            )

    async def _record(
        self,
        world: Fixtures,
        agent: UUID,
        run_id: UUID,
        repository: str,
        *,
        purged: bool = False,
    ) -> None:
        from jhin_connectors.cli import tools as cli_tools
        from jhin_tools.builtin import ToolExecutionContext

        async with world.factory() as session:
            context = ToolExecutionContext(
                session=session,
                workspace_id=world.workspace_id,
                task_id=new_uuid7(),
                run_id=run_id,
                agent_id=agent,
                agent_name="Engineer",
                session_factory=world.factory,
            )
            cli_tools._record_checkout(
                context,
                await cli_tools._binding(context),
                {
                    "repository": repository,
                    "base_ref": "main",
                    "config_sha": "c" * 64,
                    "purged": purged,
                },
            )
            await session.commit()

    async def test_a_file_read_is_denied_once_the_list_drops_what_is_on_disk(
        self, world: Fixtures
    ) -> None:
        agent = await world.agent("Engineer")
        run_id = await world.run(agent)
        await self._record(world, agent, run_id, "octo/alpha")

        still_allowed = await self._connection(world, ["octo/*"])
        assert await self._decision(world, agent, run_id, still_allowed) is None

        narrowed = await self._connection(world, ["other/*"])
        denied = await self._decision(world, agent, run_id, narrowed)
        assert denied is not None
        assert denied.code == "repository_not_allowed"
        assert "octo/alpha" in denied.reason
        # The way out is named, and it is one the agent can take.
        assert "check out a repository this connection allows" in denied.reason.lower()

    async def test_a_later_allowed_checkout_does_not_clear_what_is_still_on_the_disk(
        self, world: Fixtures
    ) -> None:
        """The validator follows the disk, not the newest record.

        Checking out an allowed repository replaces ``/workspace/repo`` and
        nothing else: a copy taken anywhere else survives, and so does
        ``/workspace/.cache``, where pip and npm wrote the forbidden
        repository's packages because ``HOME`` is ``/workspace``. Reading only
        the newest record turned the answer back to "allowed" with all of that
        still there.
        """
        agent = await world.agent("Engineer")
        run_id = await world.run(agent)
        await self._record(world, agent, run_id, "octo/alpha")
        await self._record(world, agent, run_id, "octo/beta")

        narrowed = await self._connection(world, ["octo/beta"])
        denied = await self._decision(world, agent, run_id, narrowed)
        assert denied is not None
        assert "octo/alpha" in denied.reason

    async def test_a_checkout_that_emptied_the_workspace_starts_the_history_again(
        self, world: Fixtures
    ) -> None:
        """And the remedy the denial prints is that checkout.

        ``purged: true`` is written only when the job reported that it emptied
        the whole workspace, under ``set -e``, so it is a fact about the disk
        rather than an intention.
        """
        agent = await world.agent("Engineer")
        run_id = await world.run(agent)
        await self._record(world, agent, run_id, "octo/alpha")
        await self._record(world, agent, run_id, "octo/beta", purged=True)

        narrowed = await self._connection(world, ["octo/beta"])
        assert await self._decision(world, agent, run_id, narrowed) is None

    async def test_a_workspace_another_run_holds_is_still_answered_for(
        self, world: Fixtures
    ) -> None:
        """The window a read-only question cannot see the far side of.

        While another run held the workspace the answer used to be None — the
        next bind would lose the contention and take a fresh private disk, so
        there was nothing to ask about. But the holder can finalize between
        that answer and the executor's bind, and then the call acquires the
        very disk it was allowed against on the grounds that it would not.
        """
        agent = await world.agent("Engineer")
        holder = await world.run(agent)
        await self._record(world, agent, holder, "octo/alpha")
        assert (await world.row(agent)).holder_run_id == holder

        narrowed = await self._connection(world, ["other/*"])
        denied = await self._decision(world, agent, await world.run(agent), narrowed)
        assert denied is not None
        assert "octo/alpha" in denied.reason

    async def test_an_empty_workspace_is_not_a_denial(self, world: Fixtures) -> None:
        """Deny-by-default belongs to the checkout, which names a repository.
        A disk with nothing on it has nothing to refuse access to."""
        agent = await world.agent("Engineer")
        run_id = await world.run(agent)
        await world.bind(agent, run_id)
        connection_id = await self._connection(world, [])
        assert await self._decision(world, agent, run_id, connection_id) is None

    async def test_a_disabled_connection_is_denied_rather_than_raised(
        self, world: Fixtures
    ) -> None:
        agent = await world.agent("Engineer")
        run_id = await world.run(agent)
        decision = await self._decision(world, agent, run_id, new_uuid7())
        assert decision is not None
        assert decision.code == "sandbox_connection_unavailable"
