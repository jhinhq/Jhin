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
    ModelProfile,
    ModelProvider,
    Team,
    User,
    Workspace,
    WorkspaceMembership,
)
from jhin_domain import ActorType, ModelProviderType, WorkspaceRole, new_uuid7

DEV_OWNER_EMAIL = "owner@jhin.dev"
DEV_OWNER_PASSWORD = "jhin-dev-password"  # dev-only; never use in production
DEV_WORKSPACE_NAME = "Jhin HQ"

# The compose dev stack runs a fake OpenAI-compatible provider (plan 32.2);
# override when seeding from the host against a different endpoint.
DEFAULT_FAKE_PROVIDER_URL = "http://fake-provider:8080/v1"


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

    await session.commit()
    return (
        f"seeded: workspace '{DEV_WORKSPACE_NAME}' with Engineering and Marketing, "
        f"fake model provider (default profile: Fake Mini); "
        f"dev owner {DEV_OWNER_EMAIL} / {DEV_OWNER_PASSWORD}"
    )


async def run() -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise SystemExit("DATABASE_URL environment variable is required")
    engine = create_engine(database_url)
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
