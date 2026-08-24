"""Agent Skills: the workspace skills library and per-agent enablement
(docs/architecture/skills.md).

A ``skill`` row stores one skill in the open SKILL.md convention: the
markdown instruction body (frontmatter already stripped), plus optional
extra reference files as ``files_json`` ``[{"path", "content"}, ...]``.
Skills are admin-curated workspace content — imports land disabled until a
person reviews and enables them. ``agent_skill`` is the join table naming
which agents have a skill in their prompt's skills list.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from jhin_db.base import Base
from jhin_db.columns import CreatedAtMixin, JsonList, StdUuid, TimestampMixin, UuidPkMixin


class Skill(Base, UuidPkMixin, TimestampMixin):
    __tablename__ = "skill"
    __table_args__ = (UniqueConstraint("workspace_id", "name", name="uq_skill_workspace_id_name"),)

    workspace_id: Mapped[UUID] = mapped_column(
        StdUuid, ForeignKey("workspace.id", ondelete="CASCADE"), index=True
    )
    # Slug per the open format: lowercase letters/digits/hyphens, <= 64.
    name: Mapped[str] = mapped_column(String(64))
    # 2000, not 500: real-world skills (anthropics/skills' docx/pptx/xlsx)
    # legitimately exceed 500. The prompt truncates to 300 regardless.
    description: Mapped[str] = mapped_column(String(2000))
    # The SKILL.md markdown body (<= 64 KB, enforced by jhin_skills).
    content: Mapped[str] = mapped_column(Text, default="")
    # Extra reference files: [{"path": str, "content": str}, ...]
    # (each <= 64 KB, total <= 256 KB, enforced by jhin_skills).
    files_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JsonList, default=list, server_default=text("'[]'")
    )
    # built_in | imported | custom | agent_authored
    source: Mapped[str] = mapped_column(String(16), default="custom")
    source_url: Mapped[str] = mapped_column(String(500), default="", server_default=text("''"))
    # Nullable display grouping (docs/architecture/skills.md): "General" when
    # unset. Derived from repo folder structure on a browse install, hand
    # assigned for built-ins, "General" (editable) everywhere else.
    category: Mapped[str | None] = mapped_column(String(64), nullable=True, default=None)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    version: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))
    # Set only for source="agent_authored": the agent whose skills.create call
    # made this skill (docs/architecture/skills.md). SET NULL on delete so a
    # removed agent does not take its authored skills down with it.
    created_by_agent_id: Mapped[UUID | None] = mapped_column(
        StdUuid, ForeignKey("agent.id", ondelete="SET NULL"), nullable=True, index=True
    )


class AgentSkill(Base, UuidPkMixin, CreatedAtMixin):
    """One agent's enablement of one skill (deny-by-default: no row, no
    skill in the agent's prompt)."""

    __tablename__ = "agent_skill"
    __table_args__ = (
        UniqueConstraint("agent_id", "skill_id", name="uq_agent_skill_agent_id_skill_id"),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        StdUuid, ForeignKey("workspace.id", ondelete="CASCADE"), index=True
    )
    agent_id: Mapped[UUID] = mapped_column(
        StdUuid, ForeignKey("agent.id", ondelete="CASCADE"), index=True
    )
    skill_id: Mapped[UUID] = mapped_column(
        StdUuid, ForeignKey("skill.id", ondelete="CASCADE"), index=True
    )
