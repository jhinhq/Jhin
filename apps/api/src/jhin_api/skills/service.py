"""Skills management service (docs/architecture/skills.md).

RBAC: any workspace member (viewer+) may read the library; every mutation
requires admin (skills become agent instructions, so curating them is an
admin act). Every mutation is audited content-free. Imported skills are
created ``enabled=False`` — the admin reviews and enables them explicitly.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_api.audit import service as audit
from jhin_api.deps import WorkspaceContext
from jhin_api.skills.schemas import SkillCreate, SkillFile, SkillSourceCreateIn, SkillUpdate
from jhin_db.models import Agent, AgentSkill, Skill, Workspace
from jhin_domain import WorkspaceRole, role_satisfies
from jhin_skills import (
    DEFAULT_CATEGORY,
    MAX_CONTENT_BYTES,
    MAX_FILE_BYTES,
    MAX_TOTAL_BYTES,
    BundleError,
    BundleResult,
    SkillImportError,
    SkillParseError,
    derive_category,
    fetch_github_repo_zip,
    find_secret,
    load_builtin_skills,
    load_zip,
    parse_github_ref,
    source_url_for,
    validate_file_path,
)

MAX_PAGE_SIZE = 100
TARGET_TYPE = "skill"

_SOURCES = ("built_in", "imported", "custom", "agent_authored")

# --- the skill sources catalog (browse gallery) -----------------------------
#
# A small, hardcoded list of public GitHub repositories worth browsing for
# skills — this is only "where to look", never the skills themselves (no
# bundling, no vendoring). Every entry here is treated as maintainer-reviewed:
# installing a single skill from one of these repos through the browse flow
# enables it immediately instead of landing in the "review and enable" queue
# that a raw `owner/repo` GitHub import uses (docs/architecture/skills.md
# explains the reasoning). Extend this tuple to add a source to the gallery.
SKILL_SOURCES: tuple[dict[str, str], ...] = (
    {
        "source": "anthropics/skills",
        "label": "Anthropic's official skills library",
        "description": (
            "The public Agent Skills library published by Anthropic — the "
            "same open format Jhin uses."
        ),
        "url": "https://github.com/anthropics/skills",
    },
    {
        "source": "obra/superpowers",
        "label": "Superpowers",
        "description": (
            "An agentic skills framework and software-development methodology: "
            "TDD, systematic debugging, code review, git worktrees, and more."
        ),
        "url": "https://github.com/obra/superpowers",
    },
    {
        "source": "addyosmani/agent-skills",
        "label": "Addy Osmani's agent skills",
        "description": "Production-grade engineering skills for AI coding agents.",
        "url": "https://github.com/addyosmani/agent-skills",
    },
    {
        "source": "jamestorrevillas/dev-skills",
        "label": "Dev skills",
        "description": (
            "A modular skill library for software engineers — technical, soft, and career skills."
        ),
        "url": "https://github.com/jamestorrevillas/dev-skills",
    },
    {
        "source": "avizmarlon/agent-skills",
        "label": "Portable agent skills",
        "description": (
            "Portable agent skills for AI coding tools — one SKILL.md library "
            "shared across Claude Code, Codex, Cursor, and Gemini."
        ),
        "url": "https://github.com/avizmarlon/agent-skills",
    },
)

_KNOWN_SOURCES = {entry["source"] for entry in SKILL_SOURCES}

# Every workspace's own custom browse sources live at
# workspace.settings_json["skill_sources"]: a small JSON list rather than a
# dedicated table, since it is admin-curated, low-cardinality, per-workspace
# configuration with no need for its own relational identity (the same
# reasoning that already puts budgets and coordination limits there). Each
# entry: {"source", "label", "description", "url", "added_by", "added_at"}.
_CUSTOM_SOURCES_KEY = "skill_sources"

# Hand-picked categories for the shipped starters (docs/architecture/skills.md
# section 1) — every other creation path derives or defaults its category.
_BUILTIN_CATEGORIES: dict[str, str] = {
    "writing-clear-updates": "Communication",
    "code-review-checklist": "Engineering",
    "bug-report-triage": "Support",
    "meeting-notes-summary": "Communication",
    "release-notes": "Engineering",
}

# In-process cache of a source's parsed skill listing, keyed by "owner/repo".
# A short TTL keeps repeated keystrokes in the search box from re-fetching
# and re-parsing the whole repo zip on every request.
_BROWSE_CACHE_TTL_SECONDS = 600.0


@dataclass
class _CachedBundle:
    bundle: BundleResult
    fetched_at: float


_browse_cache: dict[str, _CachedBundle] = {}


def reset_browse_cache() -> None:
    """Test hook: drop every cached browse listing."""
    _browse_cache.clear()


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
    category: str | None = None,
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
    if category is not None:
        if category == DEFAULT_CATEGORY:
            query = query.where((Skill.category == DEFAULT_CATEGORY) | (Skill.category.is_(None)))
        else:
            query = query.where(Skill.category == category)
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
        category=payload.category or DEFAULT_CATEGORY,
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
    if payload.category is not None and payload.category != record.category:
        record.category = payload.category
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


async def _install_builtins_core(
    db: AsyncSession,
    workspace_id: UUID,
    *,
    actor_id: UUID | None,
    request_id: UUID | None,
    ip_hash: str | None,
    source: str,
) -> tuple[list[str], list[str]]:
    """Install the shipped starter skills; existing names are left alone.

    Stages the rows and the audit event in the caller's transaction —
    does not commit — so a caller building a workspace can install its
    starters atomically with the rest of workspace creation. Also repairs
    the category of a starter that is already present but mis-categorized
    (see below). Returns ``(installed_names, skipped_names)``.
    """
    installed: list[str] = []
    skipped: list[str] = []
    repaired: list[str] = []
    for loaded in load_builtin_skills():
        wanted_category = _BUILTIN_CATEGORIES.get(loaded.skill.name, DEFAULT_CATEGORY)
        existing = await db.scalar(
            select(Skill).where(Skill.workspace_id == workspace_id, Skill.name == loaded.skill.name)
        )
        if existing is not None:
            # "Install missing defaults" doubles as "repair the defaults":
            # a starter installed before the category taxonomy existed has a
            # NULL category and would otherwise read as "General" forever.
            # Only a still-built_in skill is touched, and only its category —
            # never content an admin may have edited.
            if existing.source == "built_in" and existing.category != wanted_category:
                existing.category = wanted_category
                repaired.append(loaded.skill.name)
            skipped.append(loaded.skill.name)
            continue
        record = Skill(
            workspace_id=workspace_id,
            name=loaded.skill.name,
            description=loaded.skill.description,
            content=loaded.skill.content,
            files_json=[{"path": file.path, "content": file.content} for file in loaded.files],
            source="built_in",
            category=wanted_category,
            enabled=True,
        )
        db.add(record)
        installed.append(loaded.skill.name)
    await db.flush()
    audit.record(
        db,
        action="skill.builtins_installed",
        target_type=TARGET_TYPE,
        workspace_id=workspace_id,
        actor_id=actor_id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={
            "installed": len(installed),
            "skipped": len(skipped),
            "repaired": len(repaired),
            "source": source,
        },
    )
    return installed, skipped


async def install_builtins(
    db: AsyncSession,
    ctx: WorkspaceContext,
    *,
    request_id: UUID | None = None,
    ip_hash: str | None = None,
) -> tuple[list[str], list[str]]:
    """Admin action ("Install starter skills" / "Install missing defaults"):
    idempotently install any of the five starters this workspace is still
    missing. Safe to call on a workspace that already has some or all of
    them — those names are skipped, never duplicated or overwritten."""
    _require_admin(ctx)
    installed, skipped = await _install_builtins_core(
        db,
        ctx.workspace_id,
        actor_id=ctx.user.id,
        request_id=request_id,
        ip_hash=ip_hash,
        source="manual",
    )
    await db.commit()
    return installed, skipped


async def install_builtins_for_new_workspace(
    db: AsyncSession,
    workspace_id: UUID,
    *,
    actor_id: UUID,
    request_id: UUID | None = None,
    ip_hash: str | None = None,
) -> tuple[list[str], list[str]]:
    """Called by workspace creation (docs/architecture/skills.md): every new
    workspace starts with the five starter skills already installed and
    enabled, not proposed. No admin check — the caller is still building the
    workspace and its owner membership in the same transaction; this never
    runs against an *existing* workspace (that stays the explicit,
    idempotent ``install_builtins`` admin action above)."""
    return await _install_builtins_core(
        db,
        workspace_id,
        actor_id=actor_id,
        request_id=request_id,
        ip_hash=ip_hash,
        source="default",
    )


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
            # A raw admin-typed GitHub/zip import defaults to General — only
            # a browse-gallery install derives category from folder structure
            # (docs/architecture/skills.md). Editable afterward either way.
            category=DEFAULT_CATEGORY,
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


def _workspace_custom_sources(workspace: Workspace) -> list[dict[str, Any]]:
    raw = workspace.settings_json.get(_CUSTOM_SOURCES_KEY, [])
    return [dict(entry) for entry in raw] if isinstance(raw, list) else []


async def _get_workspace(db: AsyncSession, workspace_id: UUID) -> Workspace:
    workspace = await db.get(Workspace, workspace_id)
    if workspace is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Workspace not found")
    return workspace


async def list_skill_sources(db: AsyncSession, workspace_id: UUID) -> list[dict[str, Any]]:
    """The hardcoded default catalog plus this workspace's own custom
    additions (viewer+)."""
    workspace = await _get_workspace(db, workspace_id)
    defaults = [{**entry, "custom": False} for entry in SKILL_SOURCES]
    custom = [{**entry, "custom": True} for entry in _workspace_custom_sources(workspace)]
    return [*defaults, *custom]


async def _known_sources_for_workspace(db: AsyncSession, workspace_id: UUID) -> set[str]:
    workspace = await _get_workspace(db, workspace_id)
    custom = {entry["source"] for entry in _workspace_custom_sources(workspace)}
    return _KNOWN_SOURCES | custom


def _require_known_source(source: str, known: set[str]) -> None:
    if source not in known:
        raise HTTPException(
            422,
            f"{source!r} is not a known skill source; browse it via GET /skill-sources or add "
            "it as a custom source first",
        )


async def _fetch_cached_bundle(source: str) -> BundleResult:
    """The parsed skill listing for a whole repo, cached briefly in-process
    so rapid search keystrokes don't re-fetch and re-parse the zip."""
    cached = _browse_cache.get(source)
    now = time.monotonic()
    if cached is not None and now - cached.fetched_at < _BROWSE_CACHE_TTL_SECONDS:
        return cached.bundle
    try:
        data, path_prefix, _source_url = await fetch_github_repo_zip(source)
        bundle = load_zip(data, path_prefix=path_prefix)
    except (SkillImportError, BundleError) as error:
        raise HTTPException(422, str(error)) from None
    _browse_cache[source] = _CachedBundle(bundle=bundle, fetched_at=now)
    return bundle


async def browse_source(
    db: AsyncSession,
    workspace_id: UUID,
    *,
    source: str,
    q: str | None = None,
) -> list[dict[str, Any]]:
    """List one known source's skills (name/description parsed from each
    SKILL.md) without importing anything. Marks skills already installed in
    this workspace (matched by name + source_url) as ``installed: True``, and
    computes each skill's category the same way an install would derive it
    (docs/architecture/skills.md) — purely for display/filtering, nothing is
    written until the skill is actually installed."""
    known = await _known_sources_for_workspace(db, workspace_id)
    _require_known_source(source, known)
    owner, repo, _ = parse_github_ref(source)
    bundle = await _fetch_cached_bundle(source)

    needle = q.strip().lower() if q else ""
    matches = [
        loaded
        for loaded in bundle.skills
        if not needle
        or needle in loaded.skill.name.lower()
        or needle in loaded.skill.description.lower()
    ]
    if not matches:
        return []

    urls = {loaded.skill.name: source_url_for(owner, repo, loaded.folder) for loaded in matches}
    installed_urls = set(
        await db.scalars(
            select(Skill.source_url).where(
                Skill.workspace_id == workspace_id,
                Skill.source_url.in_(urls.values()),
            )
        )
    )
    return [
        {
            "source": source,
            "name": loaded.skill.name,
            "description": loaded.skill.description,
            "path": loaded.folder,
            "installed": urls[loaded.skill.name] in installed_urls,
            "category": derive_category(
                loaded.folder,
                name=loaded.skill.name,
                description=loaded.skill.description,
                declared=loaded.skill.category,
            ),
        }
        for loaded in matches
    ]


async def install_from_browse(
    db: AsyncSession,
    ctx: WorkspaceContext,
    *,
    source: str,
    skill_path: str,
    request_id: UUID | None = None,
    ip_hash: str | None = None,
) -> tuple[Skill, bool]:
    """Install exactly one skill folder from a known source (admin).

    Reuses the single-skill GitHub fetch/parse path — not the whole-repo
    import flow — and, because every source here is a curated, hardcoded
    public library (not an arbitrary admin-typed repo), enables the skill
    immediately instead of landing it in the "review and enable" queue a raw
    GitHub import uses. Idempotent: retrying an already-installed
    source + skill_path returns the existing record instead of erroring or
    duplicating. Returns ``(skill, created)``.
    """
    _require_admin(ctx)
    known = await _known_sources_for_workspace(db, ctx.workspace_id)
    _require_known_source(source, known)
    ref = f"{source}/{skill_path}".strip("/")
    try:
        data, path_prefix, source_url = await fetch_github_repo_zip(ref)
        bundle = load_zip(data, path_prefix=path_prefix)
    except (SkillImportError, BundleError) as error:
        raise HTTPException(422, str(error)) from None
    wanted_folder = skill_path.strip("/")
    exact = [loaded for loaded in bundle.skills if loaded.folder == wanted_folder]
    if not exact and len(bundle.skills) == 1:
        exact = list(bundle.skills)
    if not exact:
        raise HTTPException(422, f"no skill found at {source}/{skill_path}")
    loaded = exact[0]

    existing = await db.scalar(
        select(Skill).where(Skill.workspace_id == ctx.workspace_id, Skill.name == loaded.skill.name)
    )
    if existing is not None:
        if existing.source_url != source_url:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"a skill named {loaded.skill.name!r} already exists from a different source",
            )
        return existing, False

    record = Skill(
        workspace_id=ctx.workspace_id,
        name=loaded.skill.name,
        description=loaded.skill.description,
        content=loaded.skill.content,
        files_json=[{"path": file.path, "content": file.content} for file in loaded.files],
        source="imported",
        source_url=source_url[:500],
        category=derive_category(
            loaded.folder,
            name=loaded.skill.name,
            description=loaded.skill.description,
            declared=loaded.skill.category,
        ),
        enabled=True,
    )
    db.add(record)
    await db.flush()
    _audit(
        db,
        ctx,
        "skill.browse_installed",
        record.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={"name": record.name, "source": source, "path": skill_path},
    )
    await db.commit()
    return record, True


async def add_custom_source(
    db: AsyncSession,
    ctx: WorkspaceContext,
    payload: SkillSourceCreateIn,
    *,
    request_id: UUID | None = None,
    ip_hash: str | None = None,
) -> dict[str, Any]:
    """Add a workspace-custom browse source (admin).

    Validated live, over the exact same codeload mechanism a browse or
    install already uses: the source must fetch and contain at least one
    skill this parser accepts, or the add is rejected with a clear reason —
    nothing is persisted on a failed validation.
    """
    _require_admin(ctx)
    owner, repo, path = parse_github_ref(payload.source)
    source = f"{owner}/{repo}/{path}".rstrip("/") if path else f"{owner}/{repo}"
    if source in _KNOWN_SOURCES:
        raise HTTPException(status.HTTP_409_CONFLICT, f"{source!r} is already a default source")
    workspace = await _get_workspace(db, ctx.workspace_id)
    existing = _workspace_custom_sources(workspace)
    if any(entry["source"] == source for entry in existing):
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"{source!r} has already been added to this workspace"
        )
    bundle = await _fetch_cached_bundle(source)  # raises 422 on fetch/zip failure
    if not bundle.skills:
        reason = "; ".join(bundle.warnings[:3]) or "no SKILL.md files were found"
        raise HTTPException(
            422, f"{source!r} was reachable but had no valid skill this app could parse ({reason})"
        )
    entry = {
        "source": source,
        "label": payload.label.strip() or source,
        "description": payload.description.strip(),
        "url": source_url_for(owner, repo, path),
        "added_by": str(ctx.user.id),
        "added_at": datetime.now(UTC).isoformat(),
    }
    workspace.settings_json = {
        **workspace.settings_json,
        _CUSTOM_SOURCES_KEY: [*existing, entry],
    }
    _audit(
        db,
        ctx,
        "skill_source.added",
        None,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={"source": source, "skills_found": len(bundle.skills)},
    )
    await db.commit()
    return {**entry, "custom": True}


async def remove_custom_source(
    db: AsyncSession,
    ctx: WorkspaceContext,
    source: str,
    *,
    request_id: UUID | None = None,
    ip_hash: str | None = None,
) -> None:
    """Remove one of this workspace's own custom sources (admin). A default
    source is never a valid target — it is not stored per-workspace."""
    _require_admin(ctx)
    workspace = await _get_workspace(db, ctx.workspace_id)
    existing = _workspace_custom_sources(workspace)
    remaining = [entry for entry in existing if entry["source"] != source]
    if len(remaining) == len(existing):
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"{source!r} is not a custom source in this workspace"
        )
    workspace.settings_json = {**workspace.settings_json, _CUSTOM_SOURCES_KEY: remaining}
    _audit(
        db,
        ctx,
        "skill_source.removed",
        None,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={"source": source},
    )
    await db.commit()


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
