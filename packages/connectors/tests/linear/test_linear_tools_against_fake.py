"""Linear tool executors end-to-end against the in-process fake Linear
server: real httpx, real credential decryption, GraphQL round trips."""

from collections.abc import Iterator

import pytest

from jhin_connectors.base import VerifyContext
from jhin_connectors.linear.connector import LinearConnector
from jhin_connectors.linear.schemas import (
    CommentCreateInput,
    CommentCreateOutput,
    IssueCreateInput,
    IssueCreateOutput,
    IssueReadInput,
    IssueReadOutput,
    IssueSearchInput,
    IssueSearchOutput,
    IssueUpdateInput,
    IssueUpdateOutput,
    MetadataReadInput,
    MetadataReadOutput,
)
from jhin_connectors.testing.fake_linear import FakeLinearServer
from jhin_db.models import Workspace
from jhin_tools.builtin import ToolExecutionContext

API_KEY = "fake-linear-api-key"

connector = LinearConnector()
EXECUTORS = {definition.name: executor for definition, executor in connector.tools()}


@pytest.fixture
def fake_linear() -> Iterator[FakeLinearServer]:
    with FakeLinearServer() as server:
        yield server


@pytest.fixture
async def linear_connection(  # type: ignore[no-untyped-def]
    workspace: Workspace, make_connection, fake_linear: FakeLinearServer
):
    return await make_connection(
        workspace,
        connector_type="linear",
        auth_type="api_key",
        credentials={"api_key": API_KEY},
        config={"base_url": fake_linear.base_url},
    )


async def test_verify_connection_viewer(fake_linear: FakeLinearServer) -> None:
    health = await connector.verify_connection(
        VerifyContext(
            auth_type="api_key",
            credentials={"api_key": API_KEY},
            config={"base_url": fake_linear.base_url},
        )
    )
    assert health.ok, health.message
    assert "Fake Linear User" in health.message


async def test_verify_connection_bad_key(fake_linear: FakeLinearServer) -> None:
    health = await connector.verify_connection(
        VerifyContext(
            auth_type="api_key",
            credentials={"api_key": "wrong"},
            config={"base_url": fake_linear.base_url},
        )
    )
    assert not health.ok


async def test_verify_connection_oauth_not_implemented() -> None:
    health = await connector.verify_connection(
        VerifyContext(auth_type="oauth", credentials={"access_token": "x"})
    )
    assert not health.ok
    assert "not implemented" in health.message


async def test_issue_read_by_identifier(context: ToolExecutionContext, linear_connection) -> None:
    output = await EXECUTORS["linear.issue.read"](
        context, IssueReadInput(connection_id=str(linear_connection.id), issue="ENG-142")
    )
    assert isinstance(output, IssueReadOutput)
    assert output.identifier == "ENG-142"
    assert output.team_key == "ENG"
    assert output.state_name == "Backlog"
    assert "octo/alpha" in output.description


async def test_issue_search_filters(context: ToolExecutionContext, linear_connection) -> None:
    output = await EXECUTORS["linear.issue.search"](
        context,
        IssueSearchInput(connection_id=str(linear_connection.id), team="ENG", state_name="Backlog"),
    )
    assert isinstance(output, IssueSearchOutput)
    assert [issue.identifier for issue in output.issues] == ["ENG-142"]

    miss = await EXECUTORS["linear.issue.search"](
        context, IssueSearchInput(connection_id=str(linear_connection.id), query="nonexistent")
    )
    assert isinstance(miss, IssueSearchOutput)
    assert miss.issues == []


async def test_issue_create_update_comment_flow(
    context: ToolExecutionContext, linear_connection, fake_linear: FakeLinearServer
) -> None:
    created = await EXECUTORS["linear.issue.create"](
        context,
        IssueCreateInput(
            connection_id=str(linear_connection.id),
            team="ENG",
            title="New ticket",
            description="Details",
            state_name="Todo",
        ),
    )
    assert isinstance(created, IssueCreateOutput)
    assert created.identifier.startswith("ENG-")
    assert created.state_name == "Todo"

    updated = await EXECUTORS["linear.issue.update"](
        context,
        IssueUpdateInput(
            connection_id=str(linear_connection.id),
            issue=created.identifier,
            state_name="In Progress",
        ),
    )
    assert isinstance(updated, IssueUpdateOutput)
    assert updated.state_name == "In Progress"

    comment = await EXECUTORS["linear.comment.create"](
        context,
        CommentCreateInput(
            connection_id=str(linear_connection.id), issue=created.identifier, body="On it."
        ),
    )
    assert isinstance(comment, CommentCreateOutput)
    assert comment.comment_id

    state = fake_linear.state.snapshot()
    assert state["comments"][created.identifier][0]["body"] == "On it."


async def test_metadata_read(context: ToolExecutionContext, linear_connection) -> None:
    output = await EXECUTORS["linear.metadata.read"](
        context, MetadataReadInput(connection_id=str(linear_connection.id))
    )
    assert isinstance(output, MetadataReadOutput)
    assert [team.key for team in output.teams] == ["ENG"]
    assert [state.name for state in output.teams[0].states] == [
        "Backlog",
        "Todo",
        "In Progress",
        "Done",
    ]


async def test_fetch_metadata_for_ui_pickers(fake_linear: FakeLinearServer) -> None:
    metadata = await connector.fetch_metadata(
        VerifyContext(
            auth_type="api_key",
            credentials={"api_key": API_KEY},
            config={"base_url": fake_linear.base_url},
        )
    )
    assert metadata["teams"][0]["key"] == "ENG"
    assert {state["name"] for state in metadata["teams"][0]["states"]} >= {"Backlog", "Todo"}


async def test_wrong_connector_type_is_rejected(
    context: ToolExecutionContext,
    workspace: Workspace,
    make_connection,  # type: ignore[no-untyped-def]
) -> None:
    github_connection = await make_connection(workspace, connector_type="github")
    with pytest.raises(Exception, match="no linear connection"):
        await EXECUTORS["linear.issue.read"](
            context, IssueReadInput(connection_id=str(github_connection.id), issue="ENG-142")
        )
