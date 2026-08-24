"""Built-in skills tool: ``skills.read`` (docs/architecture/skills.md).

Progressive disclosure: the agent's prompt lists only the *names and
descriptions* of its enabled skills; this read-risk tool returns the full
markdown instructions (and, via ``file``, a skill's reference files). It is
scoped to the calling agent by construction — only skills that are enabled
in the workspace library AND enabled for this agent are visible — and the
``name`` input is a grant scope key, so an admin can restrict a grant to a
subset of skills with an fnmatch pattern.

Skill content is operator-curated (admins create, import-review, and enable
it), so it re-enters the prompt as tool output without any extra trust
labeling beyond the gateway's normal sanitization.
"""

from __future__ import annotations

from typing import cast

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from jhin_db.models import AgentSkill, Skill
from jhin_policy import SKILLS_READ_CAPABILITY, RiskLevel, ToolDefinition
from jhin_tools.builtin import ToolExecutionContext, ToolExecutor, ToolValidator
from jhin_tools.errors import ToolExecutionError

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
)
