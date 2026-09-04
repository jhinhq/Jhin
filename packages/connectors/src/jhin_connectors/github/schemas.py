"""Strict input/output models for the GitHub tools (plan 21.4).

Every input carries ``connection_id`` and ``repository`` — the gateway
matches both against grant scopes (connection-scoped, repo-glob-scoped
access, plan 6.6). ``extra="forbid"`` everywhere: a hallucinated field is a
schema violation, not a silent pass-through.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# ``owner/name``, with neither half made of dots alone: every request path
# here is built as ``/repos/{repository}/…``, so a segment of ``..`` would be
# a traversal out of ``/repos`` and into another part of the API. The middle
# character class is the rule — each segment carries a character that is not a
# dot. (``cli/schemas.py`` states the same rule for the sandbox tools.)
_SEGMENT = r"[A-Za-z0-9_.\-]*[A-Za-z0-9_\-][A-Za-z0-9_.\-]*"
REPOSITORY_PATTERN = rf"^{_SEGMENT}/{_SEGMENT}$"
_REF_MAX = 250


class _GitHubInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connection_id: str = Field(description="Id of the GitHub connection to use.")
    repository: str = Field(
        pattern=REPOSITORY_PATTERN, description="Repository as owner/name, e.g. octo/widgets."
    )


# --- github.repository.read ---


class RepositoryReadInput(_GitHubInput):
    pass


class RepositoryReadOutput(BaseModel):
    full_name: str
    description: str
    default_branch: str
    private: bool
    html_url: str
    open_issues: int
    forks: int
    stars: int


# --- github.branch.list ---


class BranchListInput(_GitHubInput):
    per_page: int = Field(default=30, ge=1, le=100)


class BranchInfo(BaseModel):
    name: str
    sha: str
    protected: bool = False


class BranchListOutput(BaseModel):
    branches: list[BranchInfo]


# --- github.file.read ---


class FileReadInput(_GitHubInput):
    path: str = Field(min_length=1, max_length=500)
    ref: str | None = Field(default=None, max_length=_REF_MAX)


class FileReadOutput(BaseModel):
    path: str
    content: str
    size: int
    sha: str
    truncated: bool = False


# --- github.branch.create ---


class BranchCreateInput(_GitHubInput):
    branch: str = Field(min_length=1, max_length=_REF_MAX, description="New branch name.")
    from_branch: str | None = Field(
        default=None,
        max_length=_REF_MAX,
        description="Base branch; the repository default branch when omitted.",
    )


class BranchCreateOutput(BaseModel):
    branch: str
    sha: str
    ref: str


# --- github.issue.read ---


class IssueReadInput(_GitHubInput):
    number: int = Field(ge=1)


class IssueReadOutput(BaseModel):
    number: int
    title: str
    body: str
    state: str
    author: str
    labels: list[str]
    comments: int
    html_url: str


# --- github.issue.comment / github.pull_request.comment ---


class IssueCommentInput(_GitHubInput):
    number: int = Field(ge=1)
    body: str = Field(min_length=1, max_length=20_000)


class CommentOutput(BaseModel):
    comment_id: int
    html_url: str


# --- github.pull_request.create ---


class PullRequestCreateInput(_GitHubInput):
    title: str = Field(min_length=1, max_length=300)
    head: str = Field(min_length=1, max_length=_REF_MAX, description="Branch with the changes.")
    base: str = Field(min_length=1, max_length=_REF_MAX, description="Branch to merge into.")
    body: str = Field(default="", max_length=30_000)
    draft: bool = False


class PullRequestCreateOutput(BaseModel):
    number: int
    html_url: str
    state: str
    head: str
    base: str


# --- github.pull_request.read ---


class PullRequestReadInput(_GitHubInput):
    number: int = Field(ge=1)


class PullRequestReadOutput(BaseModel):
    number: int
    title: str
    body: str
    state: str
    head: str
    base: str
    merged: bool
    mergeable: bool | None
    author: str
    html_url: str


# --- github.pull_request.merge ---


class PullRequestMergeInput(_GitHubInput):
    number: int = Field(ge=1)
    merge_method: Literal["merge", "squash", "rebase"] = "merge"
    commit_title: str | None = Field(default=None, max_length=300)


class PullRequestMergeOutput(BaseModel):
    merged: bool
    sha: str
    message: str


# --- github.check.read ---


class CheckRunsInput(_GitHubInput):
    ref: str = Field(min_length=1, max_length=_REF_MAX, description="Commit SHA or branch.")


class CheckRunInfo(BaseModel):
    name: str
    status: str
    conclusion: str | None


class CheckRunsOutput(BaseModel):
    total_count: int
    check_runs: list[CheckRunInfo]


# --- github.workflow.dispatch ---


class WorkflowDispatchInput(_GitHubInput):
    workflow: str = Field(
        min_length=1, max_length=200, description="Workflow file name or numeric id."
    )
    ref: str = Field(min_length=1, max_length=_REF_MAX)
    inputs: dict[str, str] = Field(default_factory=dict)


class WorkflowDispatchOutput(BaseModel):
    dispatched: bool
    workflow: str
    ref: str


# --- github.workflow_run.read ---


class WorkflowRunStatusInput(_GitHubInput):
    run_id: int | None = Field(
        default=None, ge=1, description="Specific Actions run id; latest runs when omitted."
    )


class WorkflowRunInfo(BaseModel):
    id: int
    name: str
    status: str
    conclusion: str | None
    head_branch: str
    run_number: int
    html_url: str


class WorkflowRunStatusOutput(BaseModel):
    runs: list[WorkflowRunInfo]
