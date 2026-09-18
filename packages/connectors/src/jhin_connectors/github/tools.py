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
from typing import Any, cast

from pydantic import BaseModel

from jhin_connectors.execution import resolve_connection
from jhin_connectors.github.auth import AUTH_GITHUB_APP, resolve_access_token
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
    RepositoryListEntry,
    RepositoryListInput,
    RepositoryListOutput,
    RepositoryReadInput,
    RepositoryReadOutput,
    WorkflowDispatchInput,
    WorkflowDispatchOutput,
    WorkflowRunInfo,
    WorkflowRunStatusInput,
    WorkflowRunStatusOutput,
)
from jhin_policy import RiskLevel, ToolDefinition, result_scope_admits
from jhin_tools.builtin import ToolExecutionContext, ToolExecutor
from jhin_tools.sanitize import MAX_DOCUMENT_BYTES

# File contents re-enter the prompt; cap well below the sanitizer's document
# limit so one file read cannot evict everything else.
_MAX_FILE_CHARS = 6_000

# Repository listing bounds. 100 is GitHub's maximum page size; five pages
# is the whole inventory of every connection we have seen and still a fixed
# ceiling on what one call may cost. A description is a sentence in a list,
# not a document.
_LIST_PAGE_SIZE = 100
_MAX_LIST_PAGES = 5
_MAX_DESCRIPTION_CHARS = 200
# Room left under the gateway's document cap for the rest of the payload
# (the truncated and limited_by_grant flags, the JSON scaffolding) so a
# listing is trimmed by this tool, which can say so, rather than replaced
# wholesale by the sanitizer's preview marker, which cannot.
_RESULT_BYTES_HEADROOM = 2_048


async def _bearer_with_auth(ctx: ToolExecutionContext, connection_id: str) -> tuple[str, str, str]:
    """(base_url, token, auth_type) for one call — the credential path."""
    resolved = await resolve_connection(ctx, connection_id, connector_type="github")
    base_url = str(resolved.config.get("base_url") or DEFAULT_BASE_URL)
    token = await resolve_access_token(
        resolved.connection.auth_type, resolved.credentials, base_url
    )
    return base_url, token, resolved.connection.auth_type


async def _bearer(ctx: ToolExecutionContext, connection_id: str) -> tuple[str, str]:
    """(base_url, token) for one call — the credential resolution path."""
    base_url, token, _auth_type = await _bearer_with_auth(ctx, connection_id)
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


def _within_grant(ctx: ToolExecutionContext, repo: dict[str, Any]) -> bool:
    """Whether one provider row is inside the grants that authorized this
    call. Separate from the caller's own query so the executor can tell a
    row the agent may not see from one it simply did not ask for.
    """
    full_name = str(repo.get("full_name", ""))
    return bool(full_name) and result_scope_admits(ctx.authorizing_grants, "repository", full_name)


def _listable(ctx: ToolExecutionContext, data: RepositoryListInput, repo: dict[str, Any]) -> bool:
    """Whether one provider row belongs in this agent's listing.

    The grant filter is the least-privilege half of a call that names no
    repository. The gateway has already allowed the call; the repository
    patterns of the allow grants that authorized *it* then bound the rows,
    so an agent granted ``octo/*`` reads back octo names and nothing else.
    This is narrowing, never deciding: it cannot allow a call the evaluator
    denied, and it is not a place to enforce policy. With no authorizing
    grants in the context it admits nothing — a listing that lost its
    provenance returns an empty page rather than the whole inventory.
    """
    full_name = str(repo.get("full_name", ""))
    if not full_name:
        return False
    if not _within_grant(ctx, repo):
        return False
    owner = full_name.partition("/")[0]
    if data.owner is not None and owner.casefold() != data.owner.strip().casefold():
        return False
    return data.query is None or data.query.strip().casefold() in full_name.casefold()


def _list_entry(repo: dict[str, Any]) -> RepositoryListEntry:
    return RepositoryListEntry(
        full_name=str(repo.get("full_name", "")),
        private=bool(repo.get("private", False)),
        default_branch=str(repo.get("default_branch", "")),
        can_push=bool((repo.get("permissions") or {}).get("push", False)),
        description=str(repo.get("description") or "")[:_MAX_DESCRIPTION_CHARS],
    )


async def _repository_list(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    """The repositories this connection's token can reach, in name order.

    Which endpoint answers is decided by the connection's auth type, not by
    asking GitHub what it can do: a GitHub App's installation token may only
    call ``/installation/repositories``, and every user token (PAT, browser
    or device sign-in) may only call ``/user/repos``. Nothing here consults
    ``/user/installations``, so a token whose installations list is empty —
    the ordinary state of a browser sign-in — still lists its repositories.
    """
    data = cast(RepositoryListInput, payload)
    base_url, token, auth_type = await _bearer_with_auth(ctx, data.connection_id)
    installation = auth_type == AUTH_GITHUB_APP
    path = "/installation/repositories" if installation else "/user/repos"
    # ``/user/repos`` orders the whole result set, so that walk may stop as
    # soon as it holds one row more than was asked for. The installation
    # endpoint takes no sort, so its rows are ordered here instead and its
    # walk runs to the page cap before anything is cut.
    params: dict[str, Any] = {"per_page": _LIST_PAGE_SIZE}
    if not installation:
        params["sort"] = "full_name"

    entries: list[RepositoryListEntry] = []
    truncated = False
    limited_by_grant = False
    for page in range(1, _MAX_LIST_PAGES + 1):
        body = await github_request("GET", base_url, path, token, params={**params, "page": page})
        batch = body.get("repositories") if isinstance(body, dict) else body
        if not isinstance(batch, list) or not batch:
            break
        rows = [repo for repo in batch if isinstance(repo, dict)]
        limited_by_grant = limited_by_grant or any(not _within_grant(ctx, repo) for repo in rows)
        entries.extend(_list_entry(repo) for repo in rows if _listable(ctx, data, repo))
        if len(batch) < _LIST_PAGE_SIZE:
            break
        if not installation and len(entries) > data.limit:
            break
        if page == _MAX_LIST_PAGES:
            # The cap stopped the walk, not the provider. Say so, rather
            # than let a partial answer read as the whole inventory.
            truncated = True
    if installation:
        entries.sort(key=lambda entry: entry.full_name.casefold())
    if len(entries) > data.limit:
        truncated = True
        entries = entries[: data.limit]
    kept = _within_result_bytes(entries)
    if len(kept) < len(entries):
        truncated = True
        entries = kept
    return RepositoryListOutput(
        returned_count=len(entries),
        repositories=entries,
        truncated=truncated,
        limited_by_grant=limited_by_grant,
    )


def _within_result_bytes(entries: list[RepositoryListEntry]) -> list[RepositoryListEntry]:
    """As many rows as a tool result may carry, and no more.

    The gateway size-caps every result, and a document over the cap is not
    trimmed but *replaced* by a preview marker — which would throw the whole
    listing away and hand back a ``truncated`` of its own that means
    something else. A hundred rows of long names and descriptions can reach
    that size, so the answer is cut here, where the cut can be reported
    honestly as this tool's own truncation.
    """
    budget = MAX_DOCUMENT_BYTES - _RESULT_BYTES_HEADROOM
    kept: list[RepositoryListEntry] = []
    used = 0
    for entry in entries:
        size = len(entry.model_dump_json().encode()) + 1
        if used + size > budget:
            break
        kept.append(entry)
        used += size
    return kept


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

# ``redispatch_is_safe`` across this connector, in one place because the line
# falls in one place: GitHub is somebody else's system, so the split is
# between the calls that only look at it and the calls that change it.
#
# Every GET is True. A repository read, a listing, a branch listing, a file
# read, an issue or pull request read, a check read, a workflow-run read —
# running one of these a second time after a worker died mid-call returns the
# same answer and leaves GitHub exactly as it was. There is nothing to
# reconcile, and stopping the run to ask a person about one was always the
# wrong ending.
#
# Every mutation is False, including the ones that would arguably survive a
# repeat. ``github.branch.create`` would answer "reference already exists" the
# second time and ``github.pull_request.create`` might too — but "probably
# refuses" is not the same promise as "cannot happen twice", and a second
# ``issue.comment``, ``workflow.dispatch`` or ``pull_request.merge`` is a
# visible, sometimes irreversible act in somebody's repository. These are the
# calls a human approves, and the thing they are approving is that it happens
# once.

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
            redispatch_is_safe=True,
        ),
        _repository_read,
    ),
    (
        ToolDefinition(
            name="github.repository.list",
            description=(
                "Find repositories: list the ones this GitHub connection can reach, in name "
                "order, with an optional query matching part of owner/name. Takes no "
                "repository — call it when you need a repository's owner/name and do not "
                "already know it. Use returned_count for the number of visible rows in "
                "this response. If truncated is true, more matching rows may exist; "
                "limited_by_grant means this agent's permissions narrowed the listing."
            ),
            risk=RiskLevel.READ,
            input_model=RepositoryListInput,
            output_model=RepositoryListOutput,
            required_capability="github.repository.list",
            # ``repository`` is a scope key the *call* never names: listing
            # is how an agent finds one. A grant's repository patterns
            # therefore bound the rows the executor returns, and the
            # evaluator matches such a grant on the connection alone.
            scope_keys=_REPO_SCOPE,
            result_scope_keys=("repository",),
            redispatch_is_safe=True,
        ),
        _repository_list,
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
            redispatch_is_safe=True,
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
            redispatch_is_safe=True,
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
            redispatch_is_safe=False,
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
            redispatch_is_safe=True,
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
            redispatch_is_safe=False,
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
            redispatch_is_safe=False,
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
            redispatch_is_safe=True,
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
            redispatch_is_safe=False,
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
            redispatch_is_safe=False,
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
            redispatch_is_safe=True,
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
            redispatch_is_safe=False,
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
            redispatch_is_safe=True,
        ),
        _workflow_run_status,
    ),
)
