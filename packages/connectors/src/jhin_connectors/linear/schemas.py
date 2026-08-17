"""Strict input/output models for the Linear tools (plan 21.4).

Every input carries ``connection_id``; issue-addressed tools carry the human
issue identifier (``ENG-142``) and are scoped on it, so team-level access is
granted with identifier globs (``ENG-*``). Team-addressed tools carry the
team ``key`` and are scoped on it directly. ``extra="forbid"`` everywhere: a
hallucinated field is a schema violation, not a silent pass-through.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

# "ENG-142" or a Linear issue UUID.
ISSUE_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9-]*$"
TEAM_KEY_PATTERN = r"^[A-Za-z0-9_]+$"

_MAX_TEXT = 20_000


class _LinearInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connection_id: str = Field(description="Id of the Linear connection to use.")


class IssueRef(BaseModel):
    """Compact issue facts shared by several outputs."""

    identifier: str
    issue_id: str
    title: str
    description: str = ""
    state_name: str = ""
    state_type: str = ""
    team_key: str = ""
    priority: int = 0
    assignee: str = ""
    labels: list[str] = Field(default_factory=list)
    url: str = ""


# --- linear.issue.read ---


class IssueReadInput(_LinearInput):
    issue: str = Field(
        pattern=ISSUE_PATTERN,
        max_length=100,
        description="Issue identifier (e.g. ENG-142) or Linear issue id.",
    )


class IssueReadOutput(IssueRef):
    pass


# --- linear.issue.search ---


class IssueSearchInput(_LinearInput):
    query: str = Field(default="", max_length=500, description="Text to match in issue titles.")
    team: str = Field(
        default="", description="Restrict to one team key (e.g. ENG); empty for all teams."
    )
    state_name: str = Field(default="", max_length=100, description="Restrict to one state name.")
    limit: int = Field(default=10, ge=1, le=50)


class IssueSearchOutput(BaseModel):
    issues: list[IssueRef]


# --- linear.issue.create ---


class IssueCreateInput(_LinearInput):
    team: str = Field(pattern=TEAM_KEY_PATTERN, max_length=50, description="Team key, e.g. ENG.")
    title: str = Field(min_length=1, max_length=500)
    description: str = Field(default="", max_length=_MAX_TEXT)
    state_name: str = Field(
        default="",
        max_length=100,
        description="Initial workflow state name; team default if empty.",
    )


class IssueCreateOutput(BaseModel):
    identifier: str
    issue_id: str
    state_name: str = ""
    url: str = ""


# --- linear.issue.update ---


class IssueUpdateInput(_LinearInput):
    issue: str = Field(pattern=ISSUE_PATTERN, max_length=100)
    title: str | None = Field(default=None, max_length=500)
    description: str | None = Field(default=None, max_length=_MAX_TEXT)
    state_name: str | None = Field(
        default=None, max_length=100, description="Move the issue to this workflow state."
    )


class IssueUpdateOutput(BaseModel):
    identifier: str
    issue_id: str
    title: str = ""
    state_name: str = ""
    url: str = ""


# --- linear.comment.create ---


class CommentCreateInput(_LinearInput):
    issue: str = Field(pattern=ISSUE_PATTERN, max_length=100)
    body: str = Field(min_length=1, max_length=_MAX_TEXT)


class CommentCreateOutput(BaseModel):
    comment_id: str
    url: str = ""


# --- linear.metadata.read ---


class MetadataReadInput(_LinearInput):
    team: str = Field(default="", description="Restrict to one team key; empty for all teams.")


class WorkflowStateInfo(BaseModel):
    id: str
    name: str
    type: str


class TeamInfo(BaseModel):
    id: str
    key: str
    name: str
    states: list[WorkflowStateInfo] = Field(default_factory=list)


class MetadataReadOutput(BaseModel):
    teams: list[TeamInfo]
