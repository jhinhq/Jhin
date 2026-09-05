"""Capability bundles on an agent: one action, one transaction, no dead rows."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_api.connections import service as connections_service
from jhin_api.deps import WorkspaceContext
from jhin_api.policy import bundles
from jhin_api.policy import service as grants_service
from jhin_api.policy.schemas import BundleApply, BundleApplyOut, SandboxCreate
from jhin_db.models import Agent, AgentCapabilityGrant, AuditEvent, Connection
from jhin_domain import ActorType, new_uuid7
from jhin_secrets import SecretCrypto

REQ: dict[str, Any] = {"request_id": new_uuid7(), "ip_hash": "test"}
GATE = {"capability": "cli.repository.push", "risk": None, "action": "approval"}


async def _agent(
    session: AsyncSession, ctx: WorkspaceContext, *, rules: list[dict[str, Any]] | None = None
) -> Agent:
    agent = Agent(
        workspace_id=ctx.workspace_id,
        name="Engineer",
        slug=f"engineer-{new_uuid7().hex[:8]}",
        approval_policy_json=rules or [],
    )
    session.add(agent)
    await session.commit()
    return agent


async def _github(
    session: AsyncSession, crypto: SecretCrypto, ctx: WorkspaceContext, *, name: str = "GitHub"
) -> Connection:
    connection, _ = await connections_service.create_connection(
        session,
        crypto,
        ctx,
        connector_type="github",
        name=name,
        auth_type="pat",
        credentials={"token": "github-pat-for-tests"},
        config={},
        **REQ,
    )
    return connection


async def _sandbox(
    session: AsyncSession,
    crypto: SecretCrypto,
    ctx: WorkspaceContext,
    *,
    git: Connection,
    allowed: list[str],
    name: str = "Existing sandbox",
) -> Connection:
    connection, _ = await connections_service.create_connection(
        session,
        crypto,
        ctx,
        connector_type="cli",
        name=name,
        auth_type="none",
        credentials={},
        config={
            "default_network": "none",
            "git_connection_id": str(git.id),
            "allowed_repositories": allowed,
        },
        **REQ,
    )
    return connection


async def _apply(
    session: AsyncSession,
    crypto: SecretCrypto,
    ctx: WorkspaceContext,
    agent: Agent,
    bundle_id: str = "code-editing",
    **request: Any,
) -> BundleApplyOut:
    return await bundles.apply_bundle(
        session, crypto, ctx, agent.id, bundle_id, BundleApply(**request), **REQ
    )


async def _grant_rows(session: AsyncSession, agent: Agent) -> list[AgentCapabilityGrant]:
    return list(
        await session.scalars(
            select(AgentCapabilityGrant).where(AgentCapabilityGrant.agent_id == agent.id)
        )
    )


async def _count(session: AsyncSession, model: type[Any]) -> int:
    return int(await session.scalar(select(func.count()).select_from(model)) or 0)


async def _audit(session: AsyncSession, action: str) -> list[AuditEvent]:
    return list(
        await session.scalars(
            select(AuditEvent).where(AuditEvent.action == action).order_by(AuditEvent.created_at)
        )
    )


def _sandbox_request(git: Connection, **overrides: Any) -> dict[str, Any]:
    return {
        "sandbox": SandboxCreate(git_connection_id=git.id, allowed_repositories=["*"]),
        **overrides,
    }


# --- Reads ------------------------------------------------------------------


async def test_workspace_bundles_report_readiness_on_an_empty_workspace(
    session: AsyncSession, admin_ctx: WorkspaceContext
) -> None:
    listed = {
        bundle.id: bundle
        for bundle in await bundles.workspace_bundles(session, admin_ctx.workspace_id)
    }

    assert listed["collaboration"].readiness.state == "ready"
    assert listed["github-read"].readiness.state == "needs"
    assert [need.kind for need in listed["github-read"].readiness.needs] == ["connect"]
    assert listed["code-editing"].readiness.state == "needs"
    assert listed["code-editing"].readiness.needs[0].connector_type == "github"
    assert listed["web-access"].readiness.state == "needs"
    assert [rule.capability for rule in listed["code-editing"].rules] == ["cli.repository.push"]
    assert "pushing to main" in listed["code-editing"].not_included


async def test_agent_bundles_are_on_partial_or_off(
    session: AsyncSession, crypto: SecretCrypto, admin_ctx: WorkspaceContext
) -> None:
    agent = await _agent(session, admin_ctx)
    github = await _github(session, crypto, admin_ctx)
    await grants_service.create_grant(
        session,
        admin_ctx,
        agent.id,
        capability="github.repository.read",
        scope={"connection_id": str(github.id)},
        effect="allow",
        **REQ,
    )

    before = {
        b.id: b for b in await bundles.agent_bundles(session, admin_ctx.workspace_id, agent.id)
    }
    assert before["github-read"].state == "partial"
    assert before["github-read"].granted_capabilities == ["github.repository.read"]
    assert before["code-editing"].state == "partial"
    assert before["collaboration"].state == "off"

    await _apply(session, crypto, admin_ctx, agent, **_sandbox_request(github))

    after = {
        b.id: b for b in await bundles.agent_bundles(session, admin_ctx.workspace_id, agent.id)
    }
    assert after["code-editing"].state == "on"
    assert after["code-editing"].missing_capabilities == []
    assert after["github-read"].state == "partial"


# --- Applying ---------------------------------------------------------------


async def test_code_editing_without_a_sandbox_answers_with_a_need_and_writes_nothing(
    session: AsyncSession, crypto: SecretCrypto, admin_ctx: WorkspaceContext
) -> None:
    agent = await _agent(session, admin_ctx)
    github = await _github(session, crypto, admin_ctx)

    result = await _apply(session, crypto, admin_ctx, agent)

    assert [need.kind for need in result.needs] == ["create_sandbox"]
    assert result.needs[0].choices[0].id == github.id
    assert result.grants_created == []
    assert await _grant_rows(session, agent) == []
    assert await _count(session, Connection) == 1


async def test_code_editing_creates_the_sandbox_and_writes_eleven_grants_and_one_rule(
    session: AsyncSession, crypto: SecretCrypto, admin_ctx: WorkspaceContext
) -> None:
    agent = await _agent(session, admin_ctx)
    github = await _github(session, crypto, admin_ctx)

    result = await _apply(session, crypto, admin_ctx, agent, **_sandbox_request(github))

    assert result.created_connection is not None
    sandbox = await session.get(Connection, result.created_connection.id)
    assert sandbox is not None
    assert sandbox.connector_type == "cli"
    assert sandbox.auth_type == "none"
    assert sandbox.status == "active"
    assert sandbox.name == "Sandbox for GitHub"
    assert sandbox.config_json == {
        "default_network": "none",
        "git_connection_id": str(github.id),
        "allowed_repositories": ["*"],
    }
    assert len(result.grants_created) == 11
    assert result.grants_existing == []
    rows = await _grant_rows(session, agent)
    assert len(rows) == 11
    cli_id, gh_id = str(sandbox.id), str(github.id)
    scopes = {(row.capability, tuple(sorted(row.scope_json.items()))) for row in rows}
    assert (
        "cli.repository.push",
        (("branch", "agent/*"), ("connection_id", cli_id), ("repository", "*")),
    ) in scopes
    assert (
        "github.pull_request.create",
        (("base", "*"), ("connection_id", gh_id), ("repository", "*")),
    ) in scopes
    assert all(row.problems == [] for row in result.grants_created)
    assert {row.connection_name for row in result.grants_created} == {
        "Sandbox for GitHub",
        "GitHub",
    }
    assert [rule.capability for rule in result.rules_added] == ["cli.repository.push"]
    refreshed = await session.get(Agent, agent.id)
    assert refreshed is not None
    assert refreshed.approval_policy_json == [GATE]

    granted = await _audit(session, "agent.permission.granted")
    assert len(granted) == 11
    assert all(event.metadata_json["bundle"] == "code-editing" for event in granted)
    policy = await _audit(session, "agent.policy.updated")
    assert len(policy) == 1
    assert policy[0].metadata_json["bundle"] == "code-editing"
    created = await _audit(session, "connection.created")
    assert [event.metadata_json["connector_type"] for event in created] == ["github", "cli"]
    assert created[1].metadata_json["bundle"] == "code-editing"
    assert created[1].metadata_json["agent_id"] == str(agent.id)
    assert set(result.callable_tools) >= {"cli.repository.checkout", "github.pull_request.create"}


async def test_dry_run_computes_everything_and_writes_nothing(
    session: AsyncSession, crypto: SecretCrypto, admin_ctx: WorkspaceContext
) -> None:
    agent = await _agent(session, admin_ctx)
    github = await _github(session, crypto, admin_ctx)

    result = await _apply(
        session, crypto, admin_ctx, agent, **_sandbox_request(github), dry_run=True
    )

    assert result.dry_run is True
    assert result.created_connection is None
    assert len(result.grants_created) == 11
    assert [rule.capability for rule in result.rules_added] == ["cli.repository.push"]
    assert await _grant_rows(session, agent) == []
    assert await _count(session, Connection) == 1
    assert await _audit(session, "agent.permission.granted") == []


async def test_a_refusal_writes_nothing_not_even_the_sandbox(
    session: AsyncSession, crypto: SecretCrypto, admin_ctx: WorkspaceContext
) -> None:
    agent = await _agent(session, admin_ctx)
    github = await _github(session, crypto, admin_ctx)
    audits_before = await _count(session, AuditEvent)

    with pytest.raises(HTTPException) as caught:
        await _apply(
            session,
            crypto,
            admin_ctx,
            agent,
            **_sandbox_request(github),
            repositories=["https://github.com/octo/alpha"],
        )

    assert caught.value.status_code == 422
    assert "is not a repository" in str(caught.value.detail)
    assert await _grant_rows(session, agent) == []
    assert await _count(session, Connection) == 1
    assert await _count(session, AuditEvent) == audits_before


async def test_reapplying_is_idempotent_and_keeps_a_hand_made_read_row(
    session: AsyncSession, crypto: SecretCrypto, admin_ctx: WorkspaceContext
) -> None:
    agent = await _agent(session, admin_ctx)
    github = await _github(session, crypto, admin_ctx)
    hand_made = await grants_service.create_grant(
        session,
        admin_ctx,
        agent.id,
        capability="github.repository.read",
        scope={"connection_id": str(github.id)},
        effect="allow",
        **REQ,
    )
    first = await _apply(session, crypto, admin_ctx, agent, **_sandbox_request(github))
    assert len(first.grants_created) == 11
    assert first.grants_existing == []
    assert first.created_connection is not None

    again = await _apply(
        session, crypto, admin_ctx, agent, connections={"cli": first.created_connection.id}
    )

    assert again.grants_created == []
    assert len(again.grants_existing) == 11
    assert again.rules_added == []
    assert [rule.capability for rule in again.rules_kept] == ["cli.repository.push"]
    rows = await _grant_rows(session, agent)
    assert len(rows) == 12
    kept = next(row for row in rows if row.id == hand_made.id)
    assert kept.scope_json == {"connection_id": str(github.id)}
    (_row, problems, _name), *_ = await grants_service.annotate_grants(
        session, admin_ctx.workspace_id, [kept]
    )
    assert problems == []


async def test_two_github_connections_need_a_choice(
    session: AsyncSession, crypto: SecretCrypto, admin_ctx: WorkspaceContext
) -> None:
    agent = await _agent(session, admin_ctx)
    await _github(session, crypto, admin_ctx, name="GitHub one")
    await _github(session, crypto, admin_ctx, name="GitHub two")

    result = await _apply(session, crypto, admin_ctx, agent, "github-read")

    assert [need.kind for need in result.needs] == ["choose"]
    assert {choice.name for choice in result.needs[0].choices} == {"GitHub one", "GitHub two"}
    assert await _grant_rows(session, agent) == []


async def test_a_sandbox_that_uses_another_github_connection_is_refused_by_sentence(
    session: AsyncSession, crypto: SecretCrypto, admin_ctx: WorkspaceContext
) -> None:
    agent = await _agent(session, admin_ctx)
    github = await _github(session, crypto, admin_ctx, name="GitHub")
    other = await _github(session, crypto, admin_ctx, name="Other GitHub")
    sandbox = await _sandbox(session, crypto, admin_ctx, git=other, allowed=["*"])

    with pytest.raises(HTTPException) as caught:
        await _apply(
            session,
            crypto,
            admin_ctx,
            agent,
            connections={"github": github.id, "cli": sandbox.id},
        )

    assert caught.value.detail == (
        "'Existing sandbox' uses 'Other GitHub' for repository jobs, not 'GitHub'. Pick a "
        "sandbox that uses this connection, or change its GitHub connection under Apps first."
    )
    assert await _grant_rows(session, agent) == []


async def test_creating_a_second_sandbox_for_the_same_github_connection_is_refused(
    session: AsyncSession, crypto: SecretCrypto, admin_ctx: WorkspaceContext
) -> None:
    agent = await _agent(session, admin_ctx)
    github = await _github(session, crypto, admin_ctx, name="GitHub")
    other = await _github(session, crypto, admin_ctx, name="Other GitHub")
    await _sandbox(session, crypto, admin_ctx, git=other, allowed=["*"], name="Other sandbox")

    # A sandbox on a *different* GitHub connection is no reason to refuse.
    created = await _apply(
        session,
        crypto,
        admin_ctx,
        agent,
        **_sandbox_request(github),
        connections={"github": github.id},
        dry_run=True,
    )
    assert len(created.grants_created) == 11

    await _sandbox(session, crypto, admin_ctx, git=github, allowed=["*"], name="Mine")
    with pytest.raises(HTTPException) as caught:
        await _apply(
            session,
            crypto,
            admin_ctx,
            agent,
            **_sandbox_request(github),
            connections={"github": github.id},
        )

    assert caught.value.detail == (
        "A CLI Sandbox connection 'Mine' already uses 'GitHub' for repository jobs; pick it "
        "under connections.cli instead of creating another."
    )


async def test_warnings_name_a_covering_deny_and_a_wildcard_allow(
    session: AsyncSession, crypto: SecretCrypto, admin_ctx: WorkspaceContext
) -> None:
    agent = await _agent(session, admin_ctx)
    github = await _github(session, crypto, admin_ctx)
    await grants_service.create_grant(
        session,
        admin_ctx,
        agent.id,
        capability="github.pull_request.create",
        scope={},
        effect="deny",
        **REQ,
    )
    session.add(
        AgentCapabilityGrant(
            workspace_id=admin_ctx.workspace_id,
            agent_id=agent.id,
            capability="cli.*",
            scope_json={},
            effect="allow",
        )
    )
    await session.commit()

    result = await _apply(
        session, crypto, admin_ctx, agent, **_sandbox_request(github), dry_run=True
    )

    assert result.warnings == [
        "An explicit deny on github.pull_request.create for this agent still wins; remove it "
        "under Capability grants if the agent should use it.",
        "A wildcard grant (cli.*) also covers these tools; the rows written here are what make "
        "checkout, push and pull-request calls pass.",
    ]


async def test_an_existing_auto_decision_for_push_is_not_overruled(
    session: AsyncSession, crypto: SecretCrypto, admin_ctx: WorkspaceContext
) -> None:
    chosen = {"capability": "cli.repository.push", "risk": None, "action": "auto"}
    agent = await _agent(session, admin_ctx, rules=[chosen])
    github = await _github(session, crypto, admin_ctx)

    result = await _apply(session, crypto, admin_ctx, agent, **_sandbox_request(github))

    assert result.rules_added == []
    assert [rule.action for rule in result.rules_kept] == ["auto"]
    refreshed = await session.get(Agent, agent.id)
    assert refreshed is not None
    assert refreshed.approval_policy_json == [chosen]
    assert await _audit(session, "agent.policy.updated") == []


async def test_an_unknown_bundle_is_a_404_that_lists_the_choices(
    session: AsyncSession, crypto: SecretCrypto, admin_ctx: WorkspaceContext
) -> None:
    agent = await _agent(session, admin_ctx)
    with pytest.raises(HTTPException) as caught:
        await _apply(session, crypto, admin_ctx, agent, "nope")
    assert caught.value.status_code == 404
    assert "code-editing, team-building, web-access" in str(caught.value.detail)


async def test_a_sandbox_is_only_for_code_editing_and_not_beside_an_existing_one(
    session: AsyncSession, crypto: SecretCrypto, admin_ctx: WorkspaceContext
) -> None:
    agent = await _agent(session, admin_ctx)
    github = await _github(session, crypto, admin_ctx)

    with pytest.raises(HTTPException) as only:
        await _apply(session, crypto, admin_ctx, agent, "github-read", **_sandbox_request(github))
    assert only.value.detail == "Only Code editing creates a sandbox."

    with pytest.raises(HTTPException) as both:
        await _apply(
            session,
            crypto,
            admin_ctx,
            agent,
            **_sandbox_request(github),
            connections={"cli": github.id},
        )
    assert both.value.detail == "Pass either a sandbox to create or an existing one, not both."

    with pytest.raises(HTTPException) as empty:
        await _apply(
            session,
            crypto,
            admin_ctx,
            agent,
            sandbox=SandboxCreate(git_connection_id=github.id, allowed_repositories=[]),
        )
    assert "list at least one" in str(empty.value.detail)


# --- Removing ---------------------------------------------------------------


async def test_removing_revokes_only_unshared_capabilities_and_names_hand_made_rows(
    session: AsyncSession, crypto: SecretCrypto, admin_ctx: WorkspaceContext
) -> None:
    agent = await _agent(session, admin_ctx)
    github = await _github(session, crypto, admin_ctx)
    await _apply(session, crypto, admin_ctx, agent, "github-read")
    applied = await _apply(session, crypto, admin_ctx, agent, **_sandbox_request(github))
    assert applied.created_connection is not None
    by_hand = await grants_service.create_grant(
        session,
        admin_ctx,
        agent.id,
        capability="cli.file.read",
        scope={"connection_id": str(applied.created_connection.id), "path": "docs/*"},
        effect="allow",
        **REQ,
    )

    preview = await bundles.remove_bundle(
        session, admin_ctx, agent.id, "code-editing", dry_run=True, **REQ
    )

    # github.repository.read and github.pull_request.read are also GitHub
    # (read)'s, which is on, so they stay; the nine cli rows, the hand-made
    # cli.file.read and pull_request.create go.
    revoked = {row.capability for row in preview.revoked}
    assert "github.repository.read" not in revoked
    assert "github.pull_request.read" not in revoked
    assert "github.pull_request.create" in revoked
    assert len(preview.revoked) == 10
    assert [row.id for row in preview.hand_made] == [by_hand.id]
    assert len(await _grant_rows(session, agent)) == 15

    result = await bundles.remove_bundle(
        session, admin_ctx, agent.id, "code-editing", dry_run=False, **REQ
    )

    assert len(result.revoked) == 10
    remaining = await _grant_rows(session, agent)
    assert {row.capability for row in remaining} == {
        "github.repository.read",
        "github.pull_request.read",
        "github.issue.read",
        "github.check.read",
        "github.workflow_run.read",
    }
    revocations = await _audit(session, "agent.permission.revoked")
    assert len(revocations) == 10
    assert all(event.metadata_json["bundle"] == "code-editing" for event in revocations)
    refreshed = await session.get(Agent, agent.id)
    assert refreshed is not None
    assert refreshed.approval_policy_json == [GATE]  # rules stay


async def test_a_system_actor_and_provenance_reach_every_audit_row(
    session: AsyncSession, crypto: SecretCrypto, admin_ctx: WorkspaceContext
) -> None:
    agent = await _agent(session, admin_ctx)
    github = await _github(session, crypto, admin_ctx)

    await bundles.apply_bundle(
        session,
        crypto,
        admin_ctx,
        agent.id,
        "code-editing",
        BundleApply(**_sandbox_request(github)),
        request_id=new_uuid7(),
        ip_hash=None,
        actor_type=ActorType.SYSTEM,
        extra_metadata={"cli": "jhin-admin agent grant"},
    )

    for action in ("agent.permission.granted", "agent.policy.updated"):
        events = await _audit(session, action)
        assert events
        assert all(event.actor_type == "system" for event in events)
        assert all(event.metadata_json["cli"] == "jhin-admin agent grant" for event in events)
    sandbox_created = (await _audit(session, "connection.created"))[-1]
    assert sandbox_created.actor_type == "system"
    assert sandbox_created.metadata_json["cli"] == "jhin-admin agent grant"
    assert isinstance(UUID(str(sandbox_created.target_id)), UUID)
