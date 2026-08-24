"""Routes for the skills library and per-agent skill enablement.

/api/v1/workspaces/{workspace_id}/skills                 library CRUD,
                                                        install-builtins,
                                                        import (GitHub / zip)
/api/v1/workspaces/{workspace_id}/skill-sources           browse catalog:
                                                        defaults + custom
/api/v1/workspaces/{workspace_id}/agents/{id}/skills     per-agent enablement
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status

from jhin_api.deps import AdminCtx, DbSession, ViewerCtx
from jhin_api.deps import client_ip_hash as ip_hash
from jhin_api.deps import get_request_id as req_id
from jhin_api.security.csrf import csrf_protect
from jhin_api.skills import service
from jhin_api.skills.schemas import (
    AgentSkillOut,
    AgentSkillsUpdate,
    BrowseInstallIn,
    BrowseInstallOut,
    BrowseListOut,
    BrowseSkillOut,
    ImportedSkillOut,
    InstallBuiltinsOut,
    SkillCreate,
    SkillDetailOut,
    SkillFile,
    SkillImportIn,
    SkillImportOut,
    SkillListOut,
    SkillOut,
    SkillSourceCreateIn,
    SkillSourceOut,
    SkillUpdate,
)
from jhin_db.models import Skill
from jhin_skills import (
    DEFAULT_CATEGORY,
    MAX_ZIP_BYTES,
    BundleError,
    SkillImportError,
    fetch_github_repo_zip,
    load_zip,
)

skill_sources_router = APIRouter(
    prefix="/api/v1/workspaces/{workspace_id}/skill-sources",
    tags=["skills"],
    dependencies=[Depends(csrf_protect)],
)

skills_router = APIRouter(
    prefix="/api/v1/workspaces/{workspace_id}/skills",
    tags=["skills"],
    dependencies=[Depends(csrf_protect)],
)

agent_skills_router = APIRouter(
    prefix="/api/v1/workspaces/{workspace_id}/agents/{agent_id}/skills",
    tags=["skills"],
    dependencies=[Depends(csrf_protect)],
)


@skill_sources_router.get("")
async def list_skill_sources(ctx: ViewerCtx, db: DbSession) -> list[SkillSourceOut]:
    """The hardcoded default catalog plus this workspace's own custom
    additions (docs/architecture/skills.md) — where to look, not the skills
    themselves."""
    entries = await service.list_skill_sources(db, ctx.workspace_id)
    return [SkillSourceOut.model_validate(entry) for entry in entries]


@skill_sources_router.post("", status_code=status.HTTP_201_CREATED)
async def add_skill_source(
    payload: SkillSourceCreateIn, request: Request, ctx: AdminCtx, db: DbSession
) -> SkillSourceOut:
    """Add a workspace-custom browse source (admin); validated live before
    it is persisted."""
    entry = await service.add_custom_source(
        db, ctx, payload, request_id=req_id(request), ip_hash=ip_hash(request)
    )
    return SkillSourceOut.model_validate(entry)


@skill_sources_router.delete("/{source:path}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_skill_source(source: str, request: Request, ctx: AdminCtx, db: DbSession) -> None:
    """Remove one of this workspace's own custom sources (admin); a default
    source can never be removed this way."""
    await service.remove_custom_source(
        db, ctx, source, request_id=req_id(request), ip_hash=ip_hash(request)
    )


def _out(record: Skill) -> SkillOut:
    out = SkillOut.model_validate(record)
    return out.model_copy(
        update={
            "file_count": len(record.files_json),
            "category": record.category or DEFAULT_CATEGORY,
        }
    )


def _detail_out(record: Skill) -> SkillDetailOut:
    out = SkillDetailOut.model_validate(record)
    return out.model_copy(
        update={
            "file_count": len(record.files_json),
            "category": record.category or DEFAULT_CATEGORY,
            "content": record.content,
            "files": [SkillFile.model_validate(entry) for entry in record.files_json],
        }
    )


@skills_router.get("")
async def list_skills(
    ctx: ViewerCtx,
    db: DbSession,
    q: str | None = None,
    source: str | None = None,
    category: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> SkillListOut:
    items, total = await service.list_skills(
        db, ctx.workspace_id, q=q, source=source, category=category, limit=limit, offset=offset
    )
    return SkillListOut(items=[_out(record) for record in items], total=total)


@skills_router.post("", status_code=status.HTTP_201_CREATED)
async def create_skill(
    payload: SkillCreate, request: Request, ctx: AdminCtx, db: DbSession
) -> SkillDetailOut:
    record = await service.create_skill(
        db, ctx, payload, request_id=req_id(request), ip_hash=ip_hash(request)
    )
    return _detail_out(record)


@skills_router.post("/install-builtins")
async def install_builtins(request: Request, ctx: AdminCtx, db: DbSession) -> InstallBuiltinsOut:
    """Install the shipped starter skills (idempotent; existing names kept)."""
    installed, skipped = await service.install_builtins(
        db, ctx, request_id=req_id(request), ip_hash=ip_hash(request)
    )
    return InstallBuiltinsOut(installed=len(installed), skipped=len(skipped), names=installed)


@skills_router.post("/import")
async def import_from_github(
    payload: SkillImportIn, request: Request, ctx: AdminCtx, db: DbSession
) -> SkillImportOut:
    """Import skill folders from a public GitHub repository.

    Skills arrive ``enabled=false`` ("proposed") so an admin reviews the
    content before any agent can see it.
    """
    try:
        data, path_prefix, source_url = await fetch_github_repo_zip(payload.github)
        bundle = load_zip(data, path_prefix=path_prefix)
    except (SkillImportError, BundleError) as error:
        raise HTTPException(422, str(error)) from None
    results, created, skipped = await service.import_bundle(
        db,
        ctx,
        bundle,
        source_url=source_url,
        request_id=req_id(request),
        ip_hash=ip_hash(request),
    )
    return SkillImportOut(
        created=created,
        skipped=skipped,
        skills=[ImportedSkillOut.model_validate(entry) for entry in results],
        warnings=list(bundle.warnings),
    )


@skills_router.post("/import-zip")
async def import_from_zip(
    request: Request,
    ctx: AdminCtx,
    db: DbSession,
    file: Annotated[UploadFile, File(description="A zip of skill folders; at most 5 MB")],
) -> SkillImportOut:
    data = await file.read(MAX_ZIP_BYTES + 1)
    if len(data) > MAX_ZIP_BYTES:
        raise HTTPException(413, "the zip archive exceeds 5 MB")
    try:
        bundle = load_zip(data)
    except BundleError as error:
        raise HTTPException(422, str(error)) from None
    results, created, skipped = await service.import_bundle(
        db,
        ctx,
        bundle,
        source_url="",
        request_id=req_id(request),
        ip_hash=ip_hash(request),
    )
    return SkillImportOut(
        created=created,
        skipped=skipped,
        skills=[ImportedSkillOut.model_validate(entry) for entry in results],
        warnings=list(bundle.warnings),
    )


@skills_router.get("/browse")
async def browse_skills(
    ctx: ViewerCtx,
    db: DbSession,
    source: str,
    q: str | None = None,
) -> BrowseListOut:
    """List a known source's skills (name/description parsed live from each
    SKILL.md) without importing anything; marks ones already installed in
    this workspace."""
    entries = await service.browse_source(db, ctx.workspace_id, source=source, q=q)
    return BrowseListOut(
        source=source, skills=[BrowseSkillOut.model_validate(entry) for entry in entries]
    )


@skills_router.post("/browse/install")
async def install_from_browse(
    payload: BrowseInstallIn, request: Request, ctx: AdminCtx, db: DbSession
) -> BrowseInstallOut:
    """Install exactly one skill folder found while browsing a known source
    (admin). Enabled immediately — see docs/architecture/skills.md for why
    that differs from a raw GitHub import."""
    record, created = await service.install_from_browse(
        db,
        ctx,
        source=payload.source,
        skill_path=payload.skill_path,
        request_id=req_id(request),
        ip_hash=ip_hash(request),
    )
    return BrowseInstallOut(
        skill=_out(record), status="installed" if created else "already_installed"
    )


@skills_router.get("/{skill_id}")
async def get_skill(skill_id: UUID, ctx: ViewerCtx, db: DbSession) -> SkillDetailOut:
    return _detail_out(await service.get_skill(db, ctx.workspace_id, skill_id))


@skills_router.patch("/{skill_id}")
async def update_skill(
    skill_id: UUID, payload: SkillUpdate, request: Request, ctx: AdminCtx, db: DbSession
) -> SkillDetailOut:
    record = await service.update_skill(
        db, ctx, skill_id, payload, request_id=req_id(request), ip_hash=ip_hash(request)
    )
    return _detail_out(record)


@skills_router.delete("/{skill_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_skill(skill_id: UUID, request: Request, ctx: AdminCtx, db: DbSession) -> None:
    await service.delete_skill(
        db, ctx, skill_id, request_id=req_id(request), ip_hash=ip_hash(request)
    )


def _agent_out(pairs: list[tuple[Skill, bool]]) -> list[AgentSkillOut]:
    return [
        AgentSkillOut(
            skill_id=skill.id,
            name=skill.name,
            description=skill.description,
            source=skill.source,
            category=skill.category or DEFAULT_CATEGORY,
            enabled=skill.enabled,
            enabled_for_agent=enabled_for_agent,
        )
        for skill, enabled_for_agent in pairs
    ]


@agent_skills_router.get("")
async def list_agent_skills(agent_id: UUID, ctx: ViewerCtx, db: DbSession) -> list[AgentSkillOut]:
    return _agent_out(await service.list_agent_skills(db, ctx.workspace_id, agent_id))


@agent_skills_router.put("")
async def set_agent_skills(
    agent_id: UUID,
    payload: AgentSkillsUpdate,
    request: Request,
    ctx: AdminCtx,
    db: DbSession,
) -> list[AgentSkillOut]:
    pairs = await service.set_agent_skills(
        db,
        ctx,
        agent_id,
        payload.skill_ids,
        request_id=req_id(request),
        ip_hash=ip_hash(request),
    )
    return _agent_out(pairs)
