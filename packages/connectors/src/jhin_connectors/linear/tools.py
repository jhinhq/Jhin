"""Linear tool definitions + executors (plan 11.3, 12.1).

Every executor follows the plan-13.5 sequence: the gateway has already
authorized the call (capability + connection/issue/team scope); here the
connection credential is decrypted, used as the GraphQL Authorization value
in process memory, and discarded. Outputs are compact typed models — the
gateway sanitizes and size-caps them before anything is persisted or fed
back to the model.
"""

from __future__ import annotations

from typing import Any, cast

from pydantic import BaseModel

from jhin_connectors.execution import resolve_connection
from jhin_connectors.linear.client import DEFAULT_BASE_URL, linear_graphql
from jhin_connectors.linear.schemas import (
    CommentCreateInput,
    CommentCreateOutput,
    IssueCreateInput,
    IssueCreateOutput,
    IssueReadInput,
    IssueReadOutput,
    IssueRef,
    IssueSearchInput,
    IssueSearchOutput,
    IssueUpdateInput,
    IssueUpdateOutput,
    MetadataReadInput,
    MetadataReadOutput,
    TeamInfo,
    WorkflowStateInfo,
)
from jhin_policy import RiskLevel, ToolDefinition
from jhin_tools.builtin import ToolExecutionContext, ToolExecutor

# Issue descriptions re-enter the prompt; cap well below the sanitizer's
# document limit so one issue cannot evict everything else.
_MAX_DESCRIPTION_CHARS = 6_000

_ISSUE_FIELDS = """
id identifier title description priority url
state { id name type }
team { id key name }
assignee { id name }
labels { nodes { name } }
"""

_ISSUE_QUERY = f"query($id: String!) {{ issue(id: $id) {{ {_ISSUE_FIELDS} }} }}"

_ISSUES_QUERY = f"""
query($filter: IssueFilter, $first: Int) {{
  issues(filter: $filter, first: $first) {{ nodes {{ {_ISSUE_FIELDS} }} }}
}}
"""

TEAMS_QUERY = """
query {
  teams {
    nodes { id key name states { nodes { id name type } } }
  }
}
"""

_ISSUE_CREATE = """
mutation($input: IssueCreateInput!) {
  issueCreate(input: $input) {
    success
    issue { id identifier url state { name } }
  }
}
"""

_ISSUE_UPDATE = """
mutation($id: String!, $input: IssueUpdateInput!) {
  issueUpdate(id: $id, input: $input) {
    success
    issue { id identifier title url state { id name type } }
  }
}
"""

_COMMENT_CREATE = """
mutation($input: CommentCreateInput!) {
  commentCreate(input: $input) { success comment { id url } }
}
"""


async def _api(ctx: ToolExecutionContext, connection_id: str) -> tuple[str, str]:
    """(base_url, api_key) for one call — the credential resolution path."""
    resolved = await resolve_connection(ctx, connection_id, connector_type="linear")
    base_url = str(resolved.config.get("base_url") or DEFAULT_BASE_URL)
    api_key = resolved.credentials.get("api_key", "")
    if not api_key:
        raise ValueError("this Linear connection stores no API key credential")
    return base_url, api_key


def _obj(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _issue_ref(issue: dict[str, Any]) -> IssueRef:
    state = _obj(issue.get("state"))
    team = _obj(issue.get("team"))
    labels = _obj(issue.get("labels")).get("nodes", [])
    return IssueRef(
        identifier=str(issue.get("identifier", "")),
        issue_id=str(issue.get("id", "")),
        title=str(issue.get("title", "")),
        description=str(issue.get("description") or "")[:_MAX_DESCRIPTION_CHARS],
        state_name=str(state.get("name", "")),
        state_type=str(state.get("type", "")),
        team_key=str(team.get("key", "")),
        priority=int(issue.get("priority") or 0),
        assignee=str(_obj(issue.get("assignee")).get("name", "")),
        labels=[str(label.get("name", "")) for label in labels if isinstance(label, dict)],
        url=str(issue.get("url", "")),
    )


async def _fetch_issue(base_url: str, api_key: str, issue: str) -> dict[str, Any]:
    data = await linear_graphql(base_url, api_key, _ISSUE_QUERY, {"id": issue})
    found = _obj(data.get("issue"))
    if not found:
        raise ValueError(f"Linear issue '{issue}' not found")
    return found


async def _issue_read(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    data = cast(IssueReadInput, payload)
    base_url, api_key = await _api(ctx, data.connection_id)
    issue = await _fetch_issue(base_url, api_key, data.issue)
    return IssueReadOutput(**_issue_ref(issue).model_dump())


async def _issue_search(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    data = cast(IssueSearchInput, payload)
    base_url, api_key = await _api(ctx, data.connection_id)
    conditions: dict[str, Any] = {}
    if data.query:
        conditions["title"] = {"containsIgnoreCase": data.query}
    if data.team:
        conditions["team"] = {"key": {"eq": data.team}}
    if data.state_name:
        conditions["state"] = {"name": {"eq": data.state_name}}
    result = await linear_graphql(
        base_url,
        api_key,
        _ISSUES_QUERY,
        {"filter": conditions or None, "first": data.limit},
    )
    nodes = _obj(result.get("issues")).get("nodes", [])
    return IssueSearchOutput(issues=[_issue_ref(node) for node in nodes if isinstance(node, dict)])


async def _team_and_states(
    base_url: str, api_key: str, team_key: str
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    data = await linear_graphql(base_url, api_key, TEAMS_QUERY)
    for team in _obj(data.get("teams")).get("nodes", []):
        if isinstance(team, dict) and str(team.get("key", "")) == team_key:
            states = _obj(team.get("states")).get("nodes", [])
            return team, [state for state in states if isinstance(state, dict)]
    raise ValueError(f"Linear team '{team_key}' not found")


def _state_id_by_name(states: list[dict[str, Any]], name: str, team_key: str) -> str:
    for state in states:
        if str(state.get("name", "")).lower() == name.lower():
            return str(state.get("id", ""))
    raise ValueError(f"workflow state '{name}' not found in team '{team_key}'")


async def _issue_create(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    data = cast(IssueCreateInput, payload)
    base_url, api_key = await _api(ctx, data.connection_id)
    team, states = await _team_and_states(base_url, api_key, data.team)
    input_object: dict[str, Any] = {
        "teamId": str(team.get("id", "")),
        "title": data.title,
        "description": data.description,
    }
    if data.state_name:
        input_object["stateId"] = _state_id_by_name(states, data.state_name, data.team)
    result = await linear_graphql(base_url, api_key, _ISSUE_CREATE, {"input": input_object})
    created = _obj(_obj(result.get("issueCreate")).get("issue"))
    return IssueCreateOutput(
        identifier=str(created.get("identifier", "")),
        issue_id=str(created.get("id", "")),
        state_name=str(_obj(created.get("state")).get("name", "")),
        url=str(created.get("url", "")),
    )


async def _issue_update(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    data = cast(IssueUpdateInput, payload)
    base_url, api_key = await _api(ctx, data.connection_id)
    # Resolve the identifier once: gives the issue UUID plus the team for
    # state-name resolution.
    issue = await _fetch_issue(base_url, api_key, data.issue)
    input_object: dict[str, Any] = {}
    if data.title is not None:
        input_object["title"] = data.title
    if data.description is not None:
        input_object["description"] = data.description
    if data.state_name is not None:
        team_key = str(_obj(issue.get("team")).get("key", ""))
        _, states = await _team_and_states(base_url, api_key, team_key)
        input_object["stateId"] = _state_id_by_name(states, data.state_name, team_key)
    if not input_object:
        raise ValueError("nothing to update: provide title, description, or state_name")
    result = await linear_graphql(
        base_url,
        api_key,
        _ISSUE_UPDATE,
        {"id": str(issue.get("id", "")), "input": input_object},
    )
    updated = _obj(_obj(result.get("issueUpdate")).get("issue"))
    return IssueUpdateOutput(
        identifier=str(updated.get("identifier", data.issue)),
        issue_id=str(updated.get("id", "")),
        title=str(updated.get("title", "")),
        state_name=str(_obj(updated.get("state")).get("name", "")),
        url=str(updated.get("url", "")),
    )


async def _comment_create(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    data = cast(CommentCreateInput, payload)
    base_url, api_key = await _api(ctx, data.connection_id)
    issue = await _fetch_issue(base_url, api_key, data.issue)
    result = await linear_graphql(
        base_url,
        api_key,
        _COMMENT_CREATE,
        {"input": {"issueId": str(issue.get("id", "")), "body": data.body}},
    )
    comment = _obj(_obj(result.get("commentCreate")).get("comment"))
    return CommentCreateOutput(
        comment_id=str(comment.get("id", "")), url=str(comment.get("url", ""))
    )


async def _metadata_read(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    data = cast(MetadataReadInput, payload)
    base_url, api_key = await _api(ctx, data.connection_id)
    result = await linear_graphql(base_url, api_key, TEAMS_QUERY)
    teams: list[TeamInfo] = []
    for team in _obj(result.get("teams")).get("nodes", []):
        if not isinstance(team, dict):
            continue
        key = str(team.get("key", ""))
        if data.team and key != data.team:
            continue
        states = _obj(team.get("states")).get("nodes", [])
        teams.append(
            TeamInfo(
                id=str(team.get("id", "")),
                key=key,
                name=str(team.get("name", "")),
                states=[
                    WorkflowStateInfo(
                        id=str(state.get("id", "")),
                        name=str(state.get("name", "")),
                        type=str(state.get("type", "")),
                    )
                    for state in states
                    if isinstance(state, dict)
                ],
            )
        )
    return MetadataReadOutput(teams=teams)


# Issue-addressed tools scope on the identifier: team-wide grants use globs
# like {"issue": "ENG-*"}. Team-addressed tools scope on the team key.
_ISSUE_SCOPE = ("connection_id", "issue")
_TEAM_SCOPE = ("connection_id", "team")

LINEAR_TOOLS: tuple[tuple[ToolDefinition, ToolExecutor], ...] = (
    (
        ToolDefinition(
            name="linear.issue.read",
            description="Read one Linear issue: title, description, state, team, labels.",
            risk=RiskLevel.READ,
            input_model=IssueReadInput,
            output_model=IssueReadOutput,
            required_capability="linear.issue.read",
            scope_keys=_ISSUE_SCOPE,
        ),
        _issue_read,
    ),
    (
        ToolDefinition(
            name="linear.issue.search",
            description="Search Linear issues by title text, team key, and/or state name.",
            risk=RiskLevel.READ,
            input_model=IssueSearchInput,
            output_model=IssueSearchOutput,
            required_capability="linear.issue.search",
            scope_keys=_TEAM_SCOPE,
        ),
        _issue_search,
    ),
    (
        ToolDefinition(
            name="linear.issue.create",
            description="Create a Linear issue in a team (optionally in a named state).",
            risk=RiskLevel.WRITE,
            input_model=IssueCreateInput,
            output_model=IssueCreateOutput,
            required_capability="linear.issue.create",
            supports_approval=True,
            scope_keys=_TEAM_SCOPE,
        ),
        _issue_create,
    ),
    (
        ToolDefinition(
            name="linear.issue.update",
            description="Update a Linear issue's title/description or move it to another state.",
            risk=RiskLevel.WRITE,
            input_model=IssueUpdateInput,
            output_model=IssueUpdateOutput,
            required_capability="linear.issue.update",
            supports_approval=True,
            scope_keys=_ISSUE_SCOPE,
        ),
        _issue_update,
    ),
    (
        ToolDefinition(
            name="linear.comment.create",
            description="Post a comment on a Linear issue.",
            risk=RiskLevel.WRITE,
            input_model=CommentCreateInput,
            output_model=CommentCreateOutput,
            required_capability="linear.comment.create",
            supports_approval=True,
            scope_keys=_ISSUE_SCOPE,
        ),
        _comment_create,
    ),
    (
        ToolDefinition(
            name="linear.metadata.read",
            description="Discover Linear teams and their workflow states.",
            risk=RiskLevel.READ,
            input_model=MetadataReadInput,
            output_model=MetadataReadOutput,
            required_capability="linear.metadata.read",
            scope_keys=("connection_id",),
        ),
        _metadata_read,
    ),
)
