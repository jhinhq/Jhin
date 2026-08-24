"""Built-in skills tools: ``skills.read``, ``skills.create``, ``skills.update``
(docs/architecture/skills.md).

Progressive disclosure: the agent's prompt lists only the *names and
descriptions* of its enabled skills; ``skills.read`` returns the full
markdown instructions (and, via ``file``, a skill's reference files). It is
scoped to the calling agent by construction — only skills that are enabled
in the workspace library AND enabled for this agent are visible — and the
``name`` input is a grant scope key, so an admin can restrict a grant to a
subset of skills with an fnmatch pattern.

Skill content is operator-curated (admins create, import-review, and enable
it), so it re-enters the prompt as tool output without any extra trust
labeling beyond the gateway's normal sanitization.

``skills.create`` and ``skills.update`` let an agent author skills through
chat, gated by ``skills.manage`` — elevated risk, approval-gated by default,
same posture as ``organization.create_agent`` (this creates persistent
workspace configuration other agents may come to read). Both reuse
``jhin_skills``' parser/validation primitives directly — the same size caps,
name-slug rule, and secret screen an import or a human-authored skill goes
through — so an agent-authored skill obeys the exact same rules. A skill
created this way is enabled immediately (the human already approved the
tool call), but ``skills.update`` may only revise a skill the *same* agent
authored with ``skills.create``; every other skill (human-authored,
imported, built-in, or authored by a different agent) stays out of reach —
enabling, disabling, and deleting remain human/admin-only through the
existing API.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select

from jhin_db.models import AgentSkill, AuditEvent, Skill
from jhin_domain import ActorType
from jhin_policy import (
    SKILLS_MANAGE_CAPABILITY,
    SKILLS_READ_CAPABILITY,
    DecisionType,
    Grant,
    PolicyDecision,
    RiskLevel,
    ToolDefinition,
)
from jhin_skills import (
    DEFAULT_CATEGORY,
    MAX_CONTENT_BYTES,
    MAX_DESCRIPTION_CHARS,
    MAX_FILE_BYTES,
    MAX_FILES,
    MAX_NAME_CHARS,
    MAX_TOTAL_BYTES,
    SkillParseError,
    find_secret,
    is_valid_skill_name,
    validate_file_path,
)
from jhin_tools.builtin import ToolExecutionContext, ToolExecutor, ToolValidator
from jhin_tools.errors import ToolExecutionError

# Content-preview length for the tool's own summary text; the approval card
# itself is trimmed further by the web app (docs/architecture/skills.md,
# "Approval card readability").
_CONTENT_PREVIEW_CHARS = 160

# The gateway sanitizer caps every output string at 8,192 chars; this tool
# bounds its own payload below that so long skills are cut at a clean
# boundary with an explicit ``truncated`` flag (page onward via ``offset``)
# instead of by the sanitizer's marker.
MAX_READ_CHARS = 8_000


class SkillsReadInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        min_length=1,
        max_length=64,
        description="The skill's name, exactly as it appears in your skills list.",
    )
    file: str = Field(
        default="",
        max_length=255,
        description=(
            "Optional: the path of one of the skill's reference files to read "
            "instead of the main instructions."
        ),
    )
    offset: int = Field(
        default=0,
        ge=0,
        le=262_144,
        description=(
            "Character offset to continue reading a long document from; use "
            "the previous call's offset + content length when truncated is true."
        ),
    )


class SkillsReadOutput(BaseModel):
    name: str
    description: str
    # The SKILL.md instruction body, or the requested reference file.
    content: str
    # Paths of the skill's reference files (fetch one via ``file``).
    files: list[str]
    truncated: bool
    version: int


def _bounded(content: str, offset: int) -> tuple[str, bool]:
    page = content[offset : offset + MAX_READ_CHARS]
    return page, offset + len(page) < len(content)


async def _skills_read(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    data = cast(SkillsReadInput, payload)
    row = await ctx.session.scalar(
        select(Skill)
        .join(AgentSkill, AgentSkill.skill_id == Skill.id)
        .where(
            Skill.workspace_id == ctx.workspace_id,
            Skill.name == data.name,
            Skill.enabled.is_(True),
            AgentSkill.agent_id == ctx.agent_id,
        )
    )
    if row is None:
        raise ToolExecutionError(
            f"no skill named {data.name!r} is enabled for you",
            code="skill_not_found",
            side_effect_possible=False,
            hint="Pass a name exactly as it appears in your 'Skills available to you' list.",
        )
    file_entries = [entry for entry in row.files_json if isinstance(entry, dict)]
    file_paths = sorted(str(entry.get("path", "")) for entry in file_entries)
    if data.file:
        raw = next(
            (
                str(entry.get("content", ""))
                for entry in file_entries
                if entry.get("path") == data.file
            ),
            None,
        )
        if raw is None:
            raise ToolExecutionError(
                f"skill {data.name!r} has no file {data.file!r}",
                code="skill_file_not_found",
                side_effect_possible=False,
                hint="Pass one of the paths from the skill's 'files' list, or omit 'file'.",
            )
    else:
        raw = row.content
    content, truncated = _bounded(raw, data.offset)
    return SkillsReadOutput(
        name=row.name,
        description=row.description,
        content=content,
        files=file_paths,
        truncated=truncated,
        version=row.version,
    )


def _preview(text: str) -> str:
    if len(text) <= _CONTENT_PREVIEW_CHARS:
        return text
    return text[:_CONTENT_PREVIEW_CHARS] + "…"


class SkillFileInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=255)
    content: str


def _validate_skill_body(content: str, files: Sequence[SkillFileInput]) -> list[dict[str, str]]:
    """The exact caps and secret screen ``jhin_skills`` enforces on an
    import or a human-authored skill (docs/architecture/skills.md) — an
    agent-authored skill obeys the same rules, not a looser set."""
    if len(content.encode("utf-8")) > MAX_CONTENT_BYTES:
        raise ToolExecutionError(
            "the skill's instructions are larger than 64 KB",
            code="skill_too_large",
            side_effect_possible=False,
        )
    secret = find_secret(content)
    if secret is not None:
        raise ToolExecutionError(
            f"the instructions contain credential-like content ({secret})",
            code="skill_contains_secret",
            side_effect_possible=False,
        )
    total = len(content.encode("utf-8"))
    seen: set[str] = set()
    result: list[dict[str, str]] = []
    for file in files:
        try:
            validate_file_path(file.path)
        except SkillParseError as error:
            raise ToolExecutionError(
                str(error), code="skill_invalid_file_path", side_effect_possible=False
            ) from None
        if file.path in seen:
            raise ToolExecutionError(
                f"duplicate file path {file.path!r}",
                code="skill_duplicate_file",
                side_effect_possible=False,
            )
        seen.add(file.path)
        size = len(file.content.encode("utf-8"))
        if size > MAX_FILE_BYTES:
            raise ToolExecutionError(
                f"file {file.path!r} is larger than 64 KB",
                code="skill_file_too_large",
                side_effect_possible=False,
            )
        total += size
        if total > MAX_TOTAL_BYTES:
            raise ToolExecutionError(
                "the skill's total size is larger than 256 KB",
                code="skill_too_large",
                side_effect_possible=False,
            )
        secret = find_secret(file.content)
        if secret is not None:
            raise ToolExecutionError(
                f"file {file.path!r} contains credential-like content ({secret})",
                code="skill_contains_secret",
                side_effect_possible=False,
            )
        result.append({"path": file.path, "content": file.content})
    return result


async def _resolve_skill(
    ctx: ToolExecutionContext, *, skill_id: str | None, name: str | None
) -> Skill | None:
    """Quiet lookup shared by the update validator (no exceptions) and
    executor, mirroring ``organization_admin._find_agent``."""
    if skill_id:
        try:
            parsed = UUID(skill_id)
        except ValueError:
            return None
        by_id: Skill | None = await ctx.session.scalar(
            select(Skill).where(Skill.id == parsed, Skill.workspace_id == ctx.workspace_id)
        )
        return by_id
    if name:
        by_name: Skill | None = await ctx.session.scalar(
            select(Skill).where(Skill.workspace_id == ctx.workspace_id, Skill.name == name)
        )
        return by_name
    return None


def _is_own_authored_skill(ctx: ToolExecutionContext, skill: Skill) -> bool:
    return skill.source == "agent_authored" and skill.created_by_agent_id == ctx.agent_id


# --- skills.create (elevated) -----------------------------------------------


class SkillsCreateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        min_length=1,
        max_length=MAX_NAME_CHARS,
        description="Lowercase letters, digits, and hyphens; how agents refer to the skill.",
    )
    description: str = Field(
        min_length=1,
        max_length=MAX_DESCRIPTION_CHARS,
        description="One or two sentences shown in agents' prompts: what it does, when to use it.",
    )
    content: str = Field(min_length=1, description="The full SKILL.md markdown body.")
    files: list[SkillFileInput] = Field(default_factory=list, max_length=MAX_FILES)
    # Advisory only (docs/architecture/skills.md): what an agent may call is
    # decided exclusively by the tool gateway, never by this list. Accepted
    # for format compatibility; not persisted, matching every other skill
    # creation path in Jhin (the plain create/import APIs don't store it
    # either — allowed-tools is parsed straight out of frontmatter text,
    # which no creation path here builds).
    allowed_tools: list[str] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def _validate_name(self) -> SkillsCreateInput:
        if not is_valid_skill_name(self.name):
            raise ValueError(
                "skill names use lowercase letters, digits, and hyphens "
                f"(at most {MAX_NAME_CHARS} characters)"
            )
        return self


class SkillsCreateOutput(BaseModel):
    skill_id: str
    name: str
    version: int
    content_preview: str
    summary: str


async def _skills_create(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    data = cast(SkillsCreateInput, payload)
    session = ctx.session
    existing = await session.scalar(
        select(Skill).where(Skill.workspace_id == ctx.workspace_id, Skill.name == data.name)
    )
    if existing is not None:
        raise ToolExecutionError(
            f"a skill named {data.name!r} already exists",
            code="skill_name_taken",
            side_effect_possible=False,
            hint="use skills.update to revise a skill you authored, or pick a different name",
        )
    files = _validate_skill_body(data.content, data.files)

    record = Skill(
        workspace_id=ctx.workspace_id,
        name=data.name,
        description=data.description,
        content=data.content,
        files_json=files,
        source="agent_authored",
        category=DEFAULT_CATEGORY,
        enabled=True,
        created_by_agent_id=ctx.agent_id,
    )
    session.add(record)
    await session.flush()
    session.add(
        AuditEvent(
            workspace_id=ctx.workspace_id,
            actor_type=ActorType.AGENT.value,
            actor_id=ctx.agent_id,
            action="skill.created",
            target_type="skill",
            target_id=record.id,
            metadata_json={
                "name": record.name,
                "source": record.source,
                "run_id": str(ctx.run_id),
                "created_via": "skills.create",
            },
        )
    )
    await session.flush()
    return SkillsCreateOutput(
        skill_id=str(record.id),
        name=record.name,
        version=record.version,
        content_preview=_preview(data.content),
        summary=(
            f"Created skill '{record.name}', enabled in the workspace library. "
            "A workspace admin (or you, via skills.update) can revise it; an "
            "admin still needs to add it to an agent's skills list — and that "
            "agent needs the skills.read tool — before it can be read."
        ),
    )


# --- skills.update (elevated, own authored content only) -------------------


class SkillsUpdateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skill_id: str | None = Field(default=None, max_length=64)
    name: str | None = Field(default=None, max_length=MAX_NAME_CHARS)
    description: str | None = Field(default=None, min_length=1, max_length=MAX_DESCRIPTION_CHARS)
    content: str | None = Field(default=None, min_length=1)
    files: list[SkillFileInput] | None = Field(default=None, max_length=MAX_FILES)

    @model_validator(mode="after")
    def _validate_shape(self) -> SkillsUpdateInput:
        if not self.skill_id and not self.name:
            raise ValueError("pass skill_id or name to pick the skill to update")
        if self.description is None and self.content is None and self.files is None:
            raise ValueError("pass at least one of description, content, or files to update")
        return self


class SkillsUpdateOutput(BaseModel):
    skill_id: str
    name: str
    version: int
    updated_fields: list[str]
    content_preview: str
    summary: str


async def validate_skills_update(
    ctx: ToolExecutionContext, payload: BaseModel, grants: Sequence[Grant]
) -> PolicyDecision | None:
    """Policy: ``skills.update`` may only revise a skill the calling agent
    itself authored with ``skills.create`` — never a human-authored,
    imported, built-in, or another agent's skill. Runs in the gateway before
    approval/execution; resolution failures fall through to the executor,
    which reports the clearer typed error."""
    data = cast(SkillsUpdateInput, payload)
    target = await _resolve_skill(ctx, skill_id=data.skill_id, name=data.name)
    if target is None:
        return None
    if not _is_own_authored_skill(ctx, target):
        return PolicyDecision(
            decision=DecisionType.DENY,
            code="not_skill_author",
            reason=(
                "skills.update can only revise a skill you authored with "
                "skills.create; ask a workspace admin to edit any other skill"
            ),
        )
    return None


async def _skills_update(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    data = cast(SkillsUpdateInput, payload)
    session = ctx.session
    target = await _resolve_skill(ctx, skill_id=data.skill_id, name=data.name)
    if target is None:
        raise ToolExecutionError(
            f"no skill '{data.skill_id or data.name}' in this workspace",
            code="skill_not_found",
            side_effect_possible=False,
        )
    # Defense in depth: the registered validator already vetoed callers who
    # did not author this skill; never trust that it ran.
    if not _is_own_authored_skill(ctx, target):
        raise ToolExecutionError(
            "skills.update can only revise a skill you authored with skills.create",
            code="not_skill_author",
            side_effect_possible=False,
        )

    new_content = data.content if data.content is not None else target.content
    if data.files is not None:
        new_files_input = data.files
    else:
        new_files_input = [
            SkillFileInput(path=str(entry.get("path", "")), content=str(entry.get("content", "")))
            for entry in target.files_json
            if isinstance(entry, dict)
        ]
    files = _validate_skill_body(new_content, new_files_input)

    updated: list[str] = []
    if data.description is not None and data.description != target.description:
        target.description = data.description
        updated.append("description")
    changed_body = False
    if data.content is not None and data.content != target.content:
        target.content = data.content
        changed_body = True
        updated.append("content")
    if data.files is not None and files != target.files_json:
        target.files_json = files
        changed_body = True
        updated.append("files")
    if changed_body:
        target.version += 1

    session.add(
        AuditEvent(
            workspace_id=ctx.workspace_id,
            actor_type=ActorType.AGENT.value,
            actor_id=ctx.agent_id,
            action="skill.updated",
            target_type="skill",
            target_id=target.id,
            metadata_json={
                "name": target.name,
                "version": target.version,
                "changed_fields": sorted(updated),
                "run_id": str(ctx.run_id),
                "updated_via": "skills.update",
            },
        )
    )
    await session.flush()
    return SkillsUpdateOutput(
        skill_id=str(target.id),
        name=target.name,
        version=target.version,
        updated_fields=updated,
        content_preview=_preview(target.content),
        summary=(
            f"Updated skill '{target.name}'"
            + (f" ({', '.join(updated)})" if updated else " (no changes)")
            + "."
        ),
    )


SKILL_TOOLS: tuple[tuple[ToolDefinition, ToolExecutor, ToolValidator | None], ...] = (
    (
        ToolDefinition(
            name="skills.read",
            description=(
                "Read the full instructions of one of your enabled skills before "
                "using it. Returns the skill's markdown instructions plus the "
                "names of its reference files; pass 'file' to read one of those "
                "files instead. Long documents are paged: when 'truncated' is "
                "true, call again with 'offset' advanced by the returned length."
            ),
            risk=RiskLevel.READ,
            input_model=SkillsReadInput,
            output_model=SkillsReadOutput,
            required_capability=SKILLS_READ_CAPABILITY,
            scope_keys=("name",),
        ),
        _skills_read,
        None,
    ),
    (
        ToolDefinition(
            name="skills.create",
            description=(
                "Author a new skill in the workspace skills library — a reusable "
                "instruction pack other agents can read. Calling it automatically "
                "sends the request to a human for approval, so do not route the "
                "request to an admin yourself. Give it a name, a one-or-two "
                "sentence description, and the full markdown content. The new "
                "skill is enabled immediately in the library, but an admin still "
                "needs to add it to an agent's skills list before that agent sees "
                "it, and that agent needs the skills.read tool to read it."
            ),
            risk=RiskLevel.ELEVATED,
            input_model=SkillsCreateInput,
            output_model=SkillsCreateOutput,
            required_capability=SKILLS_MANAGE_CAPABILITY,
            supports_approval=True,
        ),
        _skills_create,
        None,
    ),
    (
        ToolDefinition(
            name="skills.update",
            description=(
                "Revise a skill you previously authored with skills.create — "
                "pick it by name or skill_id and pass whichever of description, "
                "content, or files you want to change. You can only update a "
                "skill you yourself authored; every other skill (human-authored, "
                "imported, built-in, or authored by a different agent) is out of "
                "reach here. Calling it automatically sends the request to a "
                "human for approval."
            ),
            risk=RiskLevel.ELEVATED,
            input_model=SkillsUpdateInput,
            output_model=SkillsUpdateOutput,
            required_capability=SKILLS_MANAGE_CAPABILITY,
            supports_approval=True,
        ),
        _skills_update,
        validate_skills_update,
    ),
)
