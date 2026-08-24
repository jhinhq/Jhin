"""Skills prompt context (docs/architecture/skills.md).

Loads the names and descriptions of the skills enabled for an agent —
workspace-enabled AND agent-enabled, deny-by-default — and renders the
bounded "Skills available to you" block via :func:`jhin_agents.context.
skills_block`. Reasoning calls this best-effort in its own session: a
failure degrades to no skills block, never to a failed step.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_agents.context import MAX_SKILLS_LISTED, skills_block
from jhin_db.models import AgentSkill, Skill


async def skills_prompt_context(session: AsyncSession, workspace_id: UUID, agent_id: UUID) -> str:
    """The skills block for one agent ("" when it has no enabled skills)."""
    rows = (
        await session.execute(
            select(Skill.name, Skill.description)
            .join(AgentSkill, AgentSkill.skill_id == Skill.id)
            .where(
                Skill.workspace_id == workspace_id,
                Skill.enabled.is_(True),
                AgentSkill.agent_id == agent_id,
                AgentSkill.workspace_id == workspace_id,
            )
            .order_by(Skill.name)
            .limit(MAX_SKILLS_LISTED)
        )
    ).all()
    return skills_block([(name, description) for name, description in rows])
