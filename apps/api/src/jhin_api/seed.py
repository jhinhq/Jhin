"""Development seed data (`make seed`).

Creates a dev owner account plus the sample organization from the plan's
vision section: Engineering (CTO -> Senior SWE + QA) and Marketing
(Marketing Director -> Blogger). Idempotent: refuses to run twice.

Dev credentials (documented in README):
    email:    owner@jhin.dev
    password: jhin-dev-password
"""

from __future__ import annotations

import asyncio
import json
import os
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_api.audit import service as audit
from jhin_api.security.passwords import hash_password
from jhin_api.slugs import slugify
from jhin_db import create_engine, create_session_factory
from jhin_db.models import (
    Agent,
    AgentCapabilityGrant,
    Connection,
    ModelProfile,
    ModelProvider,
    Team,
    Trigger,
    User,
    Workspace,
    WorkspaceMembership,
)
from jhin_db.models.connection import new_public_id
from jhin_domain import (
    ActorType,
    ModelProviderType,
    SecretType,
    WorkspaceRole,
    new_uuid7,
)
from jhin_observability import noop_tracer
from jhin_policy import default_agent_grant_specs
from jhin_secrets import SecretCrypto, SecretStore, load_master_key
from jhin_secrets.crypto import MasterKeyError

DEV_OWNER_EMAIL = "owner@jhin.dev"
DEV_OWNER_PASSWORD = "jhin-dev-password"  # dev-only; never use in production
DEV_WORKSPACE_NAME = "Jhin HQ"

# The compose dev stack runs a fake OpenAI-compatible provider (plan 32.2);
# override when seeding from the host against a different endpoint.
DEFAULT_FAKE_PROVIDER_URL = "http://fake-provider:8080/v1"

# Fake Linear (dev profile). The webhook secret is deterministic so demo
# scripts and integration tests can configure the fake service to sign with
# it (dev-only; real connections generate a random secret at creation).
DEFAULT_FAKE_LINEAR_URL = "http://fake-linear:8080"
DEV_LINEAR_WEBHOOK_SECRET = "fake-linear-webhook-secret"
DEV_LINEAR_API_KEY = "fake-linear-api-key"

# The showcase trigger (plan 26): ENG issues transitioning into Todo.
SHOWCASE_TRIGGER_NAME = "Pick up new engineering tickets"
SHOWCASE_FILTER: dict[str, object] = {
    "all": [
        {"path": "data.team.key", "op": "eq", "value": "ENG"},
        {"path": "data.state.name", "op": "transitioned_to", "value": "Todo"},
    ]
}


def _agent(
    workspace_id: UUID,
    *,
    name: str,
    role_title: str,
    system_prompt: str,
    team_id: UUID,
    manager_agent_id: UUID | None = None,
) -> Agent:
    return Agent(
        workspace_id=workspace_id,
        team_id=team_id,
        manager_agent_id=manager_agent_id,
        name=name,
        slug=slugify(name),
        role_title=role_title,
        system_prompt=system_prompt,
        description=f"{role_title} (seeded dev data)",
    )


def _seed_delegation_grants(
    session: AsyncSession, workspace_id: UUID, *, cto: Agent, swe: Agent, qa: Agent
) -> None:
    """Phase 8 delegation defaults (plan 27, 45): deny-by-default, so the
    hierarchy only works with explicit grants.

    - CTO may delegate to direct/indirect subordinates (SWE, QA).
    - SWE may delegate to same-team members, pinned to the QA agent
      (the review_request hop of the engineering lifecycle).
    - QA gets read/test sandbox tools + GitHub read so it can check out a
      PR branch and run the test suite — never write access.
    - Everyone in the chain may report structured results upward.
    """
    grants: list[tuple[Agent, str, dict[str, object]]] = [
        (cto, "organization.delegate", {"targets": "subordinates"}),
        (swe, "organization.delegate", {"targets": "team", "target_agent_id": [str(qa.id)]}),
        (cto, "organization.report_result", {}),
        (swe, "organization.report_result", {}),
        (qa, "organization.report_result", {}),
        (qa, "cli.repository.checkout", {}),
        (qa, "cli.test.run", {}),
        (qa, "cli.file.read", {}),
        (qa, "github.repository.read", {}),
        (qa, "github.pull_request.read", {}),
        (qa, "github.check.read", {}),
    ]
    for agent, capability, scope in grants:
        session.add(
            AgentCapabilityGrant(
                workspace_id=workspace_id,
                agent_id=agent.id,
                capability=capability,
                scope_json=dict(scope),
                effect="allow",
            )
        )


def _seed_collaboration_grants(
    session: AsyncSession, workspace_id: UUID, agents: list[Agent]
) -> None:
    """Safe-by-default collaboration baseline for every seeded teammate so a
    fresh workspace demonstrates agents that work together out of the box:
    find colleagues, ask peers for help, and answer requests
    (:func:`jhin_policy.default_agent_grant_specs`). Delegation stays
    deny-by-default and is granted separately in ``_seed_delegation_grants``.
    """
    for agent in agents:
        for capability, scope in default_agent_grant_specs():
            session.add(
                AgentCapabilityGrant(
                    workspace_id=workspace_id,
                    agent_id=agent.id,
                    capability=capability,
                    scope_json=dict(scope),
                    effect="allow",
                )
            )


def _load_crypto() -> SecretCrypto | None:
    """Master-key crypto when available; the seed degrades gracefully
    without it (connection + trigger seeding is skipped)."""
    try:
        return SecretCrypto(load_master_key())
    except MasterKeyError:
        return None


async def _seed_linear_showcase(
    session: AsyncSession,
    crypto: SecretCrypto,
    *,
    workspace: Workspace,
    owner: User,
    swe: Agent,
    qa: Agent,
) -> Connection:
    """Fake Linear connection + SWE linear grants + the showcase trigger
    (plan 45 Phase 7 item 6) so a fresh dev stack demos the full slice."""
    store = SecretStore(session, crypto)
    public_id = new_public_id()
    credential_secret = await store.create(
        workspace_id=workspace.id,
        name=f"connection/{public_id}/credentials",
        plaintext=json.dumps(
            {"api_key": os.environ.get("FAKE_LINEAR_API_KEY", DEV_LINEAR_API_KEY)}
        ),
        secret_type=SecretType.CONNECTION_CREDENTIALS,
        created_by_user_id=owner.id,
    )
    webhook_secret = await store.create(
        workspace_id=workspace.id,
        name=f"connection/{public_id}/webhook",
        plaintext=DEV_LINEAR_WEBHOOK_SECRET,
        secret_type=SecretType.WEBHOOK_SECRET,
        created_by_user_id=owner.id,
    )
    connection = Connection(
        workspace_id=workspace.id,
        connector_type="linear",
        name="Linear (fake, dev)",
        auth_type="api_key",
        public_id=public_id,
        encrypted_secret_id=credential_secret.id,
        webhook_secret_id=webhook_secret.id,
        config_json={"base_url": os.environ.get("FAKE_LINEAR_BASE_URL", DEFAULT_FAKE_LINEAR_URL)},
        created_by_user_id=owner.id,
    )
    session.add(connection)
    await session.flush()

    # Deny-by-default (plan 12): the SWE needs explicit linear grants —
    # read/search/metadata plus comment (progress notes). No issue.update:
    # state moves stay with humans or the trigger's own sync-back.
    scope = {"connection_id": str(connection.id)}
    for capability in (
        "linear.issue.read",
        "linear.issue.search",
        "linear.metadata.read",
        "linear.comment.create",
    ):
        session.add(
            AgentCapabilityGrant(
                workspace_id=workspace.id,
                agent_id=swe.id,
                capability=capability,
                scope_json=scope,
                effect="allow",
            )
        )

    # Phase 8: the showcase selects the engineering template (plan 8.4) so a
    # fresh stack demos delegated QA review with the fail→fix→retest loop.
    # Switch the trigger back to "Standard" in the builder for the plain flow.
    trigger = Trigger(
        workspace_id=workspace.id,
        name=SHOWCASE_TRIGGER_NAME,
        enabled=True,
        connection_id=connection.id,
        event_type="connector.linear.issue.updated",
        filter_json=dict(SHOWCASE_FILTER),
        target_agent_id=swe.id,
        action_config_json={"comment_back": True},
        workflow_definition={
            "template": "engineering_ticket",
            "qa_agent_id": str(qa.id),
            "max_retest_cycles": 3,
        },
        created_by_user_id=owner.id,
    )
    session.add(trigger)
    await session.flush()

    request_id = new_uuid7()
    for target_id, action, target_type, name in (
        (connection.id, "connection.created", "connection", connection.name),
        (trigger.id, "trigger.created", "trigger", trigger.name),
    ):
        audit.record(
            session,
            action=action,
            target_type=target_type,
            target_id=target_id,
            workspace_id=workspace.id,
            actor_type=ActorType.SYSTEM,
            request_id=request_id,
            metadata={"seed": True, "name": name},
        )
    return connection


async def seed(session: AsyncSession) -> str:
    existing = await session.scalar(select(User).where(User.email == DEV_OWNER_EMAIL))
    if existing is not None:
        return "already seeded: dev owner exists"

    owner = User(
        email=DEV_OWNER_EMAIL,
        display_name="Dev Owner",
        password_hash=hash_password(DEV_OWNER_PASSWORD),
    )
    session.add(owner)
    await session.flush()

    workspace = Workspace(name=DEV_WORKSPACE_NAME, slug=slugify(DEV_WORKSPACE_NAME))
    session.add(workspace)
    await session.flush()
    session.add(
        WorkspaceMembership(
            workspace_id=workspace.id, user_id=owner.id, role=WorkspaceRole.OWNER.value
        )
    )

    engineering = Team(
        workspace_id=workspace.id,
        name="Engineering",
        description="Builds and ships the product.",
        color_token="indigo",
        icon="wrench",
    )
    marketing = Team(
        workspace_id=workspace.id,
        name="Marketing",
        description="Tells the world about it.",
        color_token="amber",
        icon="megaphone",
    )
    session.add_all([engineering, marketing])
    await session.flush()

    cto = _agent(
        workspace.id,
        name="CTO",
        role_title="Chief Technology Officer",
        system_prompt="You lead the engineering organization. Break work down and delegate.",
        team_id=engineering.id,
    )
    session.add(cto)
    await session.flush()
    swe = _agent(
        workspace.id,
        name="Senior Software Engineer",
        role_title="Senior Software Engineer",
        system_prompt="You implement well-tested, production-quality software.",
        team_id=engineering.id,
        manager_agent_id=cto.id,
    )
    qa = _agent(
        workspace.id,
        name="QA Engineer",
        role_title="QA Engineer",
        system_prompt="You verify changes and hunt for regressions before release.",
        team_id=engineering.id,
        manager_agent_id=cto.id,
    )
    director = _agent(
        workspace.id,
        name="Marketing Director",
        role_title="Marketing Director",
        system_prompt="You own the marketing strategy and delegate content work.",
        team_id=marketing.id,
    )
    session.add_all([swe, qa, director])
    await session.flush()
    blogger = _agent(
        workspace.id,
        name="Blogger",
        role_title="Content Writer",
        system_prompt="You write clear, useful blog posts.",
        team_id=marketing.id,
        manager_agent_id=director.id,
    )
    session.add(blogger)
    await session.flush()

    engineering.manager_agent_id = cto.id
    marketing.manager_agent_id = director.id

    # Fake model provider + profiles: agents can run tasks with zero real
    # API keys. Users add real providers via the Models page later.
    fake_provider = ModelProvider(
        workspace_id=workspace.id,
        type=ModelProviderType.OPENAI_COMPATIBLE.value,
        display_name="Fake Provider (dev)",
        base_url=os.environ.get("FAKE_PROVIDER_BASE_URL", DEFAULT_FAKE_PROVIDER_URL),
    )
    session.add(fake_provider)
    await session.flush()
    fake_mini = ModelProfile(
        workspace_id=workspace.id,
        provider_id=fake_provider.id,
        model_name="fake-mini",
        display_name="Fake Mini",
        input_cost_micros_per_million=150_000,  # $0.15 / 1M input tokens
        output_cost_micros_per_million=600_000,
    )
    fake_pro = ModelProfile(
        workspace_id=workspace.id,
        provider_id=fake_provider.id,
        model_name="fake-pro",
        display_name="Fake Pro",
        input_cost_micros_per_million=2_500_000,
        output_cost_micros_per_million=10_000_000,
    )
    session.add_all([fake_mini, fake_pro])
    await session.flush()
    workspace.default_model_profile_id = fake_mini.id
    cto.model_profile_id = fake_pro.id  # one agent on a custom profile

    request_id = new_uuid7()
    seeded: list[tuple[UUID, str, str, str]] = [
        (workspace.id, "workspace.created", "workspace", workspace.name),
        (engineering.id, "team.created", "team", engineering.name),
        (marketing.id, "team.created", "team", marketing.name),
        (cto.id, "agent.created", "agent", cto.name),
        (swe.id, "agent.created", "agent", swe.name),
        (qa.id, "agent.created", "agent", qa.name),
        (director.id, "agent.created", "agent", director.name),
        (blogger.id, "agent.created", "agent", blogger.name),
        (fake_provider.id, "provider.created", "model_provider", fake_provider.display_name),
        (fake_mini.id, "model_profile.created", "model_profile", fake_mini.display_name),
        (fake_pro.id, "model_profile.created", "model_profile", fake_pro.display_name),
    ]
    for target_id, action, target_type, name in seeded:
        audit.record(
            session,
            action=action,
            target_type=target_type,
            target_id=target_id,
            workspace_id=workspace.id,
            actor_type=ActorType.SYSTEM,
            request_id=request_id,
            metadata={"seed": True, "name": name},
        )

    _seed_delegation_grants(session, workspace.id, cto=cto, swe=swe, qa=qa)
    _seed_collaboration_grants(session, workspace.id, [cto, swe, qa, director, blogger])

    crypto = _load_crypto()
    if crypto is not None:
        connection = await _seed_linear_showcase(
            session, crypto, workspace=workspace, owner=owner, swe=swe, qa=qa
        )
        linear_note = f"Linear connection '{connection.name}' + trigger '{SHOWCASE_TRIGGER_NAME}'"
    else:
        linear_note = "no master key: skipped Linear connection + showcase trigger"

    await session.commit()
    return (
        f"seeded: workspace '{DEV_WORKSPACE_NAME}' with Engineering and Marketing, "
        f"fake model provider (default profile: Fake Mini); {linear_note}; "
        f"dev owner {DEV_OWNER_EMAIL} / {DEV_OWNER_PASSWORD}"
    )


async def run() -> None:
    # The seed writes a documented password and a fake model provider; that
    # combination must never land in a real deployment, whatever the operator
    # typed. (It already refuses whenever any user exists.)
    if os.environ.get("APP_ENV", "").lower() in {"staging", "production"}:
        raise SystemExit(
            "jhin-seed-dev is development-only: it creates a publicly documented "
            "owner password and a fake model provider. Refusing under "
            f"APP_ENV={os.environ['APP_ENV']}."
        )
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise SystemExit("DATABASE_URL environment variable is required")
    engine = create_engine(database_url, tracer=noop_tracer())
    session_factory = create_session_factory(engine)
    try:
        async with session_factory() as session:
            print(await seed(session))
    finally:
        await engine.dispose()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
