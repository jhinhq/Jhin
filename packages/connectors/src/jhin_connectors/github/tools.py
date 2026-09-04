"""GitHub tool definitions + executors (plan 11.2, 12.1).

Every executor follows the plan-13.5 sequence: the gateway has already
authorized the call (capability + connection/repo/branch scope); here the
connection credential is decrypted, exchanged for a bearer token (PAT direct
or GitHub App installation token), used in process memory, and discarded.
Outputs are compact typed models — the gateway sanitizes and size-caps them
before anything is persisted or fed back to the model.
"""

from __future__ import annotations

import base64
import binascii
from typing import cast

from pydantic import BaseModel

from jhin_connectors.execution import resolve_connection
from jhin_connectors.github.auth import resolve_access_token
from jhin_connectors.github.client import DEFAULT_BASE_URL, github_request
from jhin_connectors.github.schemas import (
    BranchCreateInput,
    BranchCreateOutput,
    BranchInfo,
    BranchListInput,
    BranchListOutput,
    CheckRunInfo,
    CheckRunsInput,
    CheckRunsOutput,
    CommentOutput,
    FileReadInput,
    FileReadOutput,
    IssueCommentInput,
    IssueReadInput,
    IssueReadOutput,
    PullRequestCreateInput,
    PullRequestCreateOutput,
    PullRequestMergeInput,
    PullRequestMergeOutput,
    PullRequestReadInput,
    PullRequestReadOutput,
    RepositoryReadInput,
    RepositoryReadOutput,
    WorkflowDispatchInput,
    WorkflowDispatchOutput,
    WorkflowRunInfo,
    WorkflowRunStatusInput,
    WorkflowRunStatusOutput,
)
from jhin_policy import RiskLevel, ToolDefinition
from jhin_tools.builtin import ToolExecutionContext, ToolExecutor

# File contents re-enter the prompt; cap well below the sanitizer's document
# limit so one file read cannot evict everything else.
_MAX_FILE_CHARS = 6_000


async def _bearer(ctx: ToolExecutionContext, connection_id: str) -> tuple[str, str]:
    """(base_url, token) for one call — the credential resolution path."""
    resolved = await resolve_connection(ctx, connection_id, connector_type="github")
    base_url = str(resolved.config.get("base_url") or DEFAULT_BASE_URL)
    token = await resolve_access_token(
        resolved.connection.auth_type, resolved.credentials, base_url
    )
    return base_url, token


async def _repository_read(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    data = cast(RepositoryReadInput, payload)
    base_url, token = await _bearer(ctx, data.connection_id)
    repo = await github_request("GET", base_url, f"/repos/{data.repository}", token)
    return RepositoryReadOutput(
        full_name=str(repo.get("full_name", data.repository)),
        description=str(repo.get("description") or ""),
        default_branch=str(repo.get("default_branch", "")),
        private=bool(repo.get("private", False)),
        html_url=str(repo.get("html_url", "")),
        open_issues=int(repo.get("open_issues_count", 0)),
        forks=int(repo.get("forks_count", 0)),
        stars=int(repo.get("stargazers_count", 0)),
    )


async def _branch_list(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    data = cast(BranchListInput, payload)
    base_url, token = await _bearer(ctx, data.connection_id)
    branches = await github_request(
        "GET",
        base_url,
        f"/repos/{data.repository}/branches",
        token,
        params={"per_page": data.per_page},
    )
    return BranchListOutput(
        branches=[
            BranchInfo(
                name=str(branch.get("name", "")),
                sha=str((branch.get("commit") or {}).get("sha", "")),
                protected=bool(branch.get("protected", False)),
            )
            for branch in branches
        ]
    )


async def _file_read(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    data = cast(FileReadInput, payload)
    base_url, token = await _bearer(ctx, data.connection_id)
    params = {"ref": data.ref} if data.ref else None
    document = await github_request(
        "GET", base_url, f"/repos/{data.repository}/contents/{data.path}", token, params=params
    )
    if isinstance(document, list):
        raise ValueError(f"'{data.path}' is a directory, not a file")
    raw = str(document.get("content", ""))
    try:
        content = base64.b64decode(raw, validate=False).decode("utf-8", errors="replace")
    except (binascii.Error, ValueError):
        content = ""
    truncated = len(content) > _MAX_FILE_CHARS
    return FileReadOutput(
        path=str(document.get("path", data.path)),
        content=content[:_MAX_FILE_CHARS],
        size=int(document.get("size", 0)),
        sha=str(document.get("sha", "")),
        truncated=truncated,
    )


async def _branch_create(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    data = cast(BranchCreateInput, payload)
    base_url, token = await _bearer(ctx, data.connection_id)
    base_branch = data.from_branch
    if not base_branch:
        repo = await github_request("GET", base_url, f"/repos/{data.repository}", token)
        base_branch = str(repo.get("default_branch", "main"))
    ref = await github_request(
        "GET", base_url, f"/repos/{data.repository}/git/ref/heads/{base_branch}", token
    )
    sha = str((ref.get("object") or {}).get("sha", ""))
    if not sha:
        raise ValueError(f"cannot resolve base branch '{base_branch}'")
    created = await github_request(
        "POST",
        base_url,
        f"/repos/{data.repository}/git/refs",
        token,
        json_body={"ref": f"refs/heads/{data.branch}", "sha": sha},
    )
    return BranchCreateOutput(
        branch=data.branch,
        sha=str((created.get("object") or {}).get("sha", sha)),
        ref=str(created.get("ref", f"refs/heads/{data.branch}")),
    )


async def _issue_read(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    data = cast(IssueReadInput, payload)
    base_url, token = await _bearer(ctx, data.connection_id)
    issue = await github_request(
        "GET", base_url, f"/repos/{data.repository}/issues/{data.number}", token
    )
    return IssueReadOutput(
        number=int(issue.get("number", data.number)),
        title=str(issue.get("title", "")),
        body=str(issue.get("body") or "")[:_MAX_FILE_CHARS],
        state=str(issue.get("state", "")),
        author=str((issue.get("user") or {}).get("login", "")),
        labels=[
            str(label.get("name", "")) if isinstance(label, dict) else str(label)
            for label in issue.get("labels", [])
        ],
        comments=int(issue.get("comments", 0)),
        html_url=str(issue.get("html_url", "")),
    )


async def _issue_comment(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    data = cast(IssueCommentInput, payload)
    base_url, token = await _bearer(ctx, data.connection_id)
    comment = await github_request(
        "POST",
        base_url,
        f"/repos/{data.repository}/issues/{data.number}/comments",
        token,
        json_body={"body": data.body},
    )
    return CommentOutput(
        comment_id=int(comment.get("id", 0)), html_url=str(comment.get("html_url", ""))
    )


async def _pull_request_create(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    data = cast(PullRequestCreateInput, payload)
    base_url, token = await _bearer(ctx, data.connection_id)
    pull = await github_request(
        "POST",
        base_url,
        f"/repos/{data.repository}/pulls",
        token,
        json_body={
            "title": data.title,
            "head": data.head,
            "base": data.base,
            "body": data.body,
            "draft": data.draft,
        },
    )
    return PullRequestCreateOutput(
        number=int(pull.get("number", 0)),
        html_url=str(pull.get("html_url", "")),
        state=str(pull.get("state", "")),
        head=str((pull.get("head") or {}).get("ref", data.head)),
        base=str((pull.get("base") or {}).get("ref", data.base)),
    )


async def _pull_request_read(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    data = cast(PullRequestReadInput, payload)
    base_url, token = await _bearer(ctx, data.connection_id)
    pull = await github_request(
        "GET", base_url, f"/repos/{data.repository}/pulls/{data.number}", token
    )
    return PullRequestReadOutput(
        number=int(pull.get("number", data.number)),
        title=str(pull.get("title", "")),
        body=str(pull.get("body") or "")[:_MAX_FILE_CHARS],
        state=str(pull.get("state", "")),
        head=str((pull.get("head") or {}).get("ref", "")),
        base=str((pull.get("base") or {}).get("ref", "")),
        merged=bool(pull.get("merged", False)),
        mergeable=cast("bool | None", pull.get("mergeable")),
        author=str((pull.get("user") or {}).get("login", "")),
        html_url=str(pull.get("html_url", "")),
    )


async def _pull_request_comment(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    # PR conversation comments use the Issues comments endpoint.
    return await _issue_comment(ctx, payload)


async def _pull_request_merge(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    data = cast(PullRequestMergeInput, payload)
    base_url, token = await _bearer(ctx, data.connection_id)
    body: dict[str, str] = {"merge_method": data.merge_method}
    if data.commit_title:
        body["commit_title"] = data.commit_title
    result = await github_request(
        "PUT",
        base_url,
        f"/repos/{data.repository}/pulls/{data.number}/merge",
        token,
        json_body=body,
    )
    return PullRequestMergeOutput(
        merged=bool(result.get("merged", False)),
        sha=str(result.get("sha", "")),
        message=str(result.get("message", "")),
    )


async def _check_runs(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    data = cast(CheckRunsInput, payload)
    base_url, token = await _bearer(ctx, data.connection_id)
    result = await github_request(
        "GET", base_url, f"/repos/{data.repository}/commits/{data.ref}/check-runs", token
    )
    return CheckRunsOutput(
        total_count=int(result.get("total_count", 0)),
        check_runs=[
            CheckRunInfo(
                name=str(run.get("name", "")),
                status=str(run.get("status", "")),
                conclusion=cast("str | None", run.get("conclusion")),
            )
            for run in result.get("check_runs", [])
        ],
    )


async def _workflow_dispatch(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    data = cast(WorkflowDispatchInput, payload)
    base_url, token = await _bearer(ctx, data.connection_id)
    body: dict[str, object] = {"ref": data.ref}
    if data.inputs:
        body["inputs"] = data.inputs
    await github_request(
        "POST",
        base_url,
        f"/repos/{data.repository}/actions/workflows/{data.workflow}/dispatches",
        token,
        json_body=body,
    )
    return WorkflowDispatchOutput(dispatched=True, workflow=data.workflow, ref=data.ref)


async def _workflow_run_status(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    data = cast(WorkflowRunStatusInput, payload)
    base_url, token = await _bearer(ctx, data.connection_id)
    if data.run_id is not None:
        run = await github_request(
            "GET", base_url, f"/repos/{data.repository}/actions/runs/{data.run_id}", token
        )
        runs = [run]
    else:
        result = await github_request(
            "GET",
            base_url,
            f"/repos/{data.repository}/actions/runs",
            token,
            params={"per_page": 5},
        )
        runs = list(result.get("workflow_runs", []))
    return WorkflowRunStatusOutput(
        runs=[
            WorkflowRunInfo(
                id=int(run.get("id", 0)),
                name=str(run.get("name", "")),
                status=str(run.get("status", "")),
                conclusion=cast("str | None", run.get("conclusion")),
                head_branch=str(run.get("head_branch", "")),
                run_number=int(run.get("run_number", 0)),
                html_url=str(run.get("html_url", "")),
            )
            for run in runs
        ]
    )


_REPO_SCOPE = ("connection_id", "repository")

GITHUB_TOOLS: tuple[tuple[ToolDefinition, ToolExecutor], ...] = (
    (
        ToolDefinition(
            name="github.repository.read",
            description="Read repository metadata (default branch, visibility, counters).",
            risk=RiskLevel.READ,
            input_model=RepositoryReadInput,
            output_model=RepositoryReadOutput,
            required_capability="github.repository.read",
            scope_keys=_REPO_SCOPE,
        ),
        _repository_read,
    ),
    (
        ToolDefinition(
            name="github.branch.list",
            description="List branches of a repository with head commit SHAs.",
            risk=RiskLevel.READ,
            input_model=BranchListInput,
            output_model=BranchListOutput,
            required_capability="github.repository.read",
            scope_keys=_REPO_SCOPE,
        ),
        _branch_list,
    ),
    (
        ToolDefinition(
            name="github.file.read",
            description="Read one file's contents from a repository (optionally at a ref).",
            risk=RiskLevel.READ,
            input_model=FileReadInput,
            output_model=FileReadOutput,
            required_capability="github.repository.read",
            scope_keys=_REPO_SCOPE,
        ),
        _file_read,
    ),
    (
        ToolDefinition(
            name="github.branch.create",
            description="Create a new branch from an existing branch (default branch if omitted).",
            risk=RiskLevel.WRITE,
            input_model=BranchCreateInput,
            output_model=BranchCreateOutput,
            required_capability="github.branch.create",
            supports_approval=True,
            scope_keys=("connection_id", "repository", "branch"),
        ),
        _branch_create,
    ),
    (
        ToolDefinition(
            name="github.issue.read",
            description="Read one issue: title, body, state, labels.",
            risk=RiskLevel.READ,
            input_model=IssueReadInput,
            output_model=IssueReadOutput,
            required_capability="github.issue.read",
            scope_keys=_REPO_SCOPE,
        ),
        _issue_read,
    ),
    (
        ToolDefinition(
            name="github.issue.comment",
            description="Post a comment on an issue.",
            risk=RiskLevel.WRITE,
            input_model=IssueCommentInput,
            output_model=CommentOutput,
            required_capability="github.issue.comment",
            supports_approval=True,
            scope_keys=_REPO_SCOPE,
        ),
        _issue_comment,
    ),
    (
        ToolDefinition(
            name="github.pull_request.create",
            description="Open a pull request from a head branch into a base branch.",
            risk=RiskLevel.WRITE,
            input_model=PullRequestCreateInput,
            output_model=PullRequestCreateOutput,
            required_capability="github.pull_request.create",
            supports_approval=True,
            # head and base are policy dimensions in their own right: without
            # them a grant that names a repository still lets a pull request
            # target any base branch in it. ``base`` is required rather than
            # merely available, because ``scope_matches`` only checks the keys
            # a grant constrains — an unstated base is an unlimited one.
            scope_keys=(*_REPO_SCOPE, "head", "base"),
            required_grant_scope_keys=(*_REPO_SCOPE, "base"),
        ),
        _pull_request_create,
    ),
    (
        ToolDefinition(
            name="github.pull_request.read",
            description="Read one pull request: title, body, branches, merge state.",
            risk=RiskLevel.READ,
            input_model=PullRequestReadInput,
            output_model=PullRequestReadOutput,
            required_capability="github.pull_request.read",
            scope_keys=_REPO_SCOPE,
        ),
        _pull_request_read,
    ),
    (
        ToolDefinition(
            name="github.pull_request.comment",
            description="Post a comment on a pull request conversation.",
            risk=RiskLevel.WRITE,
            input_model=IssueCommentInput,
            output_model=CommentOutput,
            required_capability="github.pull_request.comment",
            supports_approval=True,
            scope_keys=_REPO_SCOPE,
        ),
        _pull_request_comment,
    ),
    (
        ToolDefinition(
            name="github.pull_request.merge",
            description="Merge a pull request (merge, squash, or rebase).",
            risk=RiskLevel.ELEVATED,
            input_model=PullRequestMergeInput,
            output_model=PullRequestMergeOutput,
            required_capability="github.pull_request.merge",
            supports_approval=True,
            scope_keys=_REPO_SCOPE,
        ),
        _pull_request_merge,
    ),
    (
        ToolDefinition(
            name="github.check.read",
            description="Read check-run statuses for a commit SHA or branch.",
            risk=RiskLevel.READ,
            input_model=CheckRunsInput,
            output_model=CheckRunsOutput,
            required_capability="github.check.read",
            scope_keys=_REPO_SCOPE,
        ),
        _check_runs,
    ),
    (
        ToolDefinition(
            name="github.workflow.dispatch",
            description="Trigger a workflow_dispatch event for an Actions workflow.",
            risk=RiskLevel.WRITE,
            input_model=WorkflowDispatchInput,
            output_model=WorkflowDispatchOutput,
            required_capability="github.workflow.dispatch",
            supports_approval=True,
            scope_keys=_REPO_SCOPE,
        ),
        _workflow_dispatch,
    ),
    (
        ToolDefinition(
            name="github.workflow_run.read",
            description="Read Actions workflow run status (one run id or the latest runs).",
            risk=RiskLevel.READ,
            input_model=WorkflowRunStatusInput,
            output_model=WorkflowRunStatusOutput,
            required_capability="github.workflow_run.read",
            scope_keys=_REPO_SCOPE,
        ),
        _workflow_run_status,
    ),
)
