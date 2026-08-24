"""Skills management service (docs/architecture/skills.md).

RBAC: any workspace member (viewer+) may read the library; every mutation
requires admin (skills become agent instructions, so curating them is an
admin act). Every mutation is audited content-free. Imported skills are
created ``enabled=False`` — the admin reviews and enables them explicitly.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_api.audit import service as audit
from jhin_api.deps import WorkspaceContext
from jhin_api.skills.schemas import SkillCreate, SkillFile, SkillUpdate
from jhin_db.models import Agent, AgentSkill, Skill
from jhin_domain import WorkspaceRole, role_satisfies
from jhin_skills import (
    MAX_CONTENT_BYTES,
    MAX_FILE_BYTES,
    MAX_TOTAL_BYTES,
    BundleResult,
    SkillParseError,
    find_secret,
    load_builtin_skills,
    validate_file_path,
)

MAX_PAGE_SIZE = 100
TARGET_TYPE = "skill"

_SOURCES = ("built_in", "imported", "custom")


def _require_admin(ctx: WorkspaceContext) -> None:
    if not role_satisfies(ctx.role, WorkspaceRole.ADMIN):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Managing skills requires an admin")


def _audit(
    session: AsyncSession,
    ctx: WorkspaceContext,
    action: str,
    target_id: UUID | None,
    *,
    request_id: UUID | None,
    ip_hash: str | None,
    metadata: dict[str, Any] | None = None,
) -> None:
    audit.record(
        session,
        action=action,
        target_type=TARGET_TYPE,
        workspace_id=ctx.workspace_id,
        actor_id=ctx.user.id,
        target_id=target_id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata=metadata,
    )


def _validated_files(files: list[SkillFile], *, content: str) -> list[dict[str, str]]:
    """Enforce the format's size caps and reject credential-like content."""
    if len(content.encode("utf-8")) > MAX_CONTENT_BYTES:
        raise HTTPException(422, "instructions exceed 64 KB")
    secret = find_secret(content)
    if secret is not None:
        raise HTTPException(
            422,
            f"the instructions contain credential-like content ({secret})",
        )
    total = len(content.encode("utf-8"))
    seen: set[str] = set()
    result: list[dict[str, str]] = []
    for file in files:
        try:
            validate_file_path(file.path)
        except SkillParseError as error:
            raise HTTPException(422, str(error)) from None
        if file.path in seen:
            raise HTTPException(422, f"duplicate file path {file.path!r}")
        seen.add(file.path)
        size = len(file.content.encode("utf-8"))
        if size > MAX_FILE_BYTES:
            raise HTTPException(422, f"file {file.path!r} exceeds 64 KB")
        total += size
        if total > MAX_TOTAL_BYTES:
            raise HTTPException(422, "the skill's total size exceeds 256 KB")
        secret = find_secret(file.content)
        if secret is not None:
            raise HTTPException(
                422,
                f"file {file.path!r} contains credential-like content ({secret})",
            )
        result.append({"path": file.path, "content": file.content})
    return result


async def list_skills(
    db: AsyncSession,
    workspace_id: UUID,
    *,
    q: str | None = None,
    source: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[Skill], int]:
    if source is not None and source not in _SOURCES:
        raise HTTPException(422, f"invalid source {source!r}")
    query = select(Skill).where(Skill.workspace_id == workspace_id)
    if q:
        needle = f"%{q.lower()}%"
        query = query.where(
            func.lower(Skill.name).like(needle) | func.lower(Skill.description).like(needle)
        )
    if source is not None:
        query = query.where(Skill.source == source)
    total = await db.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = await db.scalars(
        query.order_by(Skill.name).limit(min(limit, MAX_PAGE_SIZE)).offset(offset)
    )
    return list(rows), int(total)


async def get_skill(db: AsyncSession, workspace_id: UUID, skill_id: UUID) -> Skill:
    record = await db.scalar(
        select(Skill).where(Skill.id == skill_id, Skill.workspace_id == workspace_id)
    )
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Skill not found")
    return record


async def _name_taken(db: AsyncSession, workspace_id: UUID, name: str) -> bool:
    return (
        await db.scalar(
            select(Skill.id).where(Skill.workspace_id == workspace_id, Skill.name == name)
        )
        is not None
    )


async def create_skill(
    db: AsyncSession,
    ctx: WorkspaceContext,
    payload: SkillCreate,
    *,
    request_id: UUID | None = None,
    ip_hash: str | None = None,
) -> Skill:
    _require_admin(ctx)
    if await _name_taken(db, ctx.workspace_id, payload.name):
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"a skill named {payload.name!r} already exists"
        )
    record = Skill(
        workspace_id=ctx.workspace_id,
        name=payload.name,
        description=payload.description,
        content=payload.content,
        files_json=_validated_files(payload.files, content=payload.content),
        source="custom",
        enabled=True,
    )
    db.add(record)
    await db.flush()
    _audit(
        db,
        ctx,
        "skill.created",
        record.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={"name": record.name, "source": record.source},
    )
    await db.commit()
    return record


async def update_skill(
    db: AsyncSession,
    ctx: WorkspaceContext,
    skill_id: UUID,
    payload: SkillUpdate,
    *,
    request_id: UUID | None = None,
    ip_hash: str | None = None,
) -> Skill:
    _require_admin(ctx)
    record = await get_skill(db, ctx.workspace_id, skill_id)
    changed_body = False
    if payload.description is not None and payload.description != record.description:
        record.description = payload.description
    new_content = payload.content if payload.content is not None else record.content
    new_files = (
        _validated_files(payload.files, content=new_content)
        if payload.files is not None
        else record.files_json
    )
    if payload.content is not None and payload.content != record.content:
        _validated_files(
            [SkillFile.model_validate(entry) for entry in new_files], content=new_content
        )
        record.content = new_content
        changed_body = True
    if payload.files is not None and new_files != record.files_json:
        record.files_json = new_files
        changed_body = True
    toggled: bool | None = None
    if payload.enabled is not None and payload.enabled != record.enabled:
        record.enabled = payload.enabled
        toggled = payload.enabled
    if changed_body:
        record.version += 1
    await db.flush()
    _audit(
        db,
        ctx,
        "skill.updated",
        record.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={"name": record.name, "version": record.version},
    )
    if toggled is not None:
        _audit(
            db,
            ctx,
            "skill.enabled" if toggled else "skill.disabled",
            record.id,
            request_id=request_id,
            ip_hash=ip_hash,
            metadata={"name": record.name},
        )
    await db.commit()
    return record


async def delete_skill(
    db: AsyncSession,
    ctx: WorkspaceContext,
    skill_id: UUID,
    *,
    request_id: UUID | None = None,
    ip_hash: str | None = None,
) -> None:
    _require_admin(ctx)
    record = await get_skill(db, ctx.workspace_id, skill_id)
    name = record.name
    # SQLite in unit tests does not enforce FK cascades; delete the join
    # rows explicitly (a no-op on PostgreSQL where the FK cascades).
    await db.execute(delete(AgentSkill).where(AgentSkill.skill_id == record.id))
    await db.delete(record)
    _audit(
        db,
        ctx,
        "skill.deleted",
        skill_id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={"name": name},
    )
    await db.commit()


async def install_builtins(
    db: AsyncSession,
    ctx: WorkspaceContext,
    *,
    request_id: UUID | None = None,
    ip_hash: str | None = None,
) -> tuple[list[str], list[str]]:
    """Install the shipped starter skills; existing names are left alone.

    Returns ``(installed_names, skipped_names)``.
    """
    _require_admin(ctx)
    installed: list[str] = []
    skipped: list[str] = []
    for loaded in load_builtin_skills():
        if await _name_taken(db, ctx.workspace_id, loaded.skill.name):
            skipped.append(loaded.skill.name)
            continue
        record = Skill(
            workspace_id=ctx.workspace_id,
            name=loaded.skill.name,
            description=loaded.skill.description,
            content=loaded.skill.content,
            files_json=[{"path": file.path, "content": file.content} for file in loaded.files],
            source="built_in",
            enabled=True,
        )
        db.add(record)
        installed.append(loaded.skill.name)
    await db.flush()
    _audit(
        db,
        ctx,
        "skill.builtins_installed",
        None,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={"installed": len(installed), "skipped": len(skipped)},
    )
    await db.commit()
    return installed, skipped


async def import_bundle(
    db: AsyncSession,
    ctx: WorkspaceContext,
    bundle: BundleResult,
    *,
    source_url: str,
    request_id: UUID | None = None,
    ip_hash: str | None = None,
) -> tuple[list[dict[str, str]], int, int]:
    """Create imported skills as ``enabled=False`` proposals for review.

    Returns ``(per_skill_results, created, skipped)`` where each result is
    ``{"name", "description", "status", "reason"}``.
    """
    _require_admin(ctx)
    results: list[dict[str, str]] = []
    created = 0
    skipped = 0
    for loaded in bundle.skills:
        if await _name_taken(db, ctx.workspace_id, loaded.skill.name):
            results.append(
                {
                    "name": loaded.skill.name,
                    "description": loaded.skill.description,
                    "status": "skipped",
                    "reason": "a skill with this name already exists",
                }
            )
            skipped += 1
            continue
        record = Skill(
            workspace_id=ctx.workspace_id,
            name=loaded.skill.name,
            description=loaded.skill.description,
            content=loaded.skill.content,
            files_json=[{"path": file.path, "content": file.content} for file in loaded.files],
            source="imported",
            source_url=source_url[:500],
            enabled=False,
        )
        db.add(record)
        results.append(
            {
                "name": loaded.skill.name,
                "description": loaded.skill.description,
                "status": "proposed",
                "reason": "",
            }
        )
        created += 1
    await db.flush()
    _audit(
        db,
        ctx,
        "skill.imported",
        None,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={"created": created, "skipped": skipped, "source_url": source_url[:500]},
    )
    await db.commit()
    return results, created, skipped


async def _require_agent(db: AsyncSession, workspace_id: UUID, agent_id: UUID) -> Agent:
    agent = await db.scalar(
        select(Agent).where(Agent.id == agent_id, Agent.workspace_id == workspace_id)
    )
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Agent not found")
    return agent


async def list_agent_skills(
    db: AsyncSession, workspace_id: UUID, agent_id: UUID
) -> list[tuple[Skill, bool]]:
    """Every workspace skill plus whether it is enabled for this agent."""
    await _require_agent(db, workspace_id, agent_id)
    enabled_ids = set(
        await db.scalars(select(AgentSkill.skill_id).where(AgentSkill.agent_id == agent_id))
    )
    rows = await db.scalars(
        select(Skill).where(Skill.workspace_id == workspace_id).order_by(Skill.name)
    )
    return [(skill, skill.id in enabled_ids) for skill in rows]


async def set_agent_skills(
    db: AsyncSession,
    ctx: WorkspaceContext,
    agent_id: UUID,
    skill_ids: list[UUID],
    *,
    request_id: UUID | None = None,
    ip_hash: str | None = None,
) -> list[tuple[Skill, bool]]:
    """Replace the agent's enabled-skill set with exactly ``skill_ids``."""
    _require_admin(ctx)
    await _require_agent(db, ctx.workspace_id, agent_id)
    wanted = set(skill_ids)
    if wanted:
        known = set(
            await db.scalars(
                select(Skill.id).where(Skill.workspace_id == ctx.workspace_id, Skill.id.in_(wanted))
            )
        )
        missing = wanted - known
        if missing:
            raise HTTPException(
                422,
                f"{len(missing)} skill id(s) do not exist in this workspace",
            )
    await db.execute(delete(AgentSkill).where(AgentSkill.agent_id == agent_id))
    for skill_id in sorted(wanted):
        db.add(AgentSkill(workspace_id=ctx.workspace_id, agent_id=agent_id, skill_id=skill_id))
    await db.flush()
    _audit(
        db,
        ctx,
        "agent.skills_updated",
        agent_id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={"count": len(wanted)},
    )
    await db.commit()
    return await list_agent_skills(db, ctx.workspace_id, agent_id)
