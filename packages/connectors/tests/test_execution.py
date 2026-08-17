"""Execution-time connection resolution: workspace isolation, disabled
connections, credential decryption, and the example connector end-to-end."""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_connectors.example.connector import ExampleConnector
from jhin_connectors.example.schemas import PingInput, PingOutput
from jhin_connectors.execution import ConnectionResolutionError, resolve_connection
from jhin_db.models import Workspace
from jhin_domain import new_uuid7
from jhin_tools.builtin import ToolExecutionContext


async def test_resolves_connection_and_decrypts_credentials(
    workspace: Workspace, context: ToolExecutionContext, make_connection
) -> None:
    connection = await make_connection(workspace, credentials={"token": "ghp_secret_value_1234"})
    resolved = await resolve_connection(context, connection.id, connector_type="github")
    assert resolved.credentials == {"token": "ghp_secret_value_1234"}
    assert resolved.connection.id == connection.id


async def test_wrong_workspace_behaves_like_missing(
    session: AsyncSession, context: ToolExecutionContext, make_connection
) -> None:
    other = Workspace(name="Other", slug=f"other-{new_uuid7().hex[:8]}")
    session.add(other)
    await session.flush()
    foreign = await make_connection(other, name="Foreign")  # other workspace
    with pytest.raises(ConnectionResolutionError, match="no github connection"):
        await resolve_connection(context, foreign.id, connector_type="github")


async def test_wrong_connector_type_is_rejected(
    workspace: Workspace, context: ToolExecutionContext, make_connection
) -> None:
    connection = await make_connection(workspace)
    with pytest.raises(ConnectionResolutionError, match="no example connection"):
        await resolve_connection(context, connection.id, connector_type="example")


async def test_disabled_connection_is_rejected(
    workspace: Workspace, context: ToolExecutionContext, make_connection
) -> None:
    connection = await make_connection(workspace, status="disabled")
    with pytest.raises(ConnectionResolutionError, match="disabled"):
        await resolve_connection(context, connection.id, connector_type="github")


async def test_missing_crypto_is_rejected(
    session: AsyncSession, workspace: Workspace, context: ToolExecutionContext, make_connection
) -> None:
    connection = await make_connection(workspace)
    bare = ToolExecutionContext(
        session=session,
        workspace_id=context.workspace_id,
        task_id=context.task_id,
        run_id=context.run_id,
        agent_id=context.agent_id,
        agent_name=context.agent_name,
        crypto=None,
    )
    with pytest.raises(ConnectionResolutionError, match="master key"):
        await resolve_connection(bare, connection.id, connector_type="github")


async def test_invalid_uuid_is_rejected(context: ToolExecutionContext) -> None:
    with pytest.raises(ConnectionResolutionError, match="not a valid UUID"):
        await resolve_connection(context, "not-a-uuid", connector_type="github")


async def test_example_ping_tool_executes_through_resolution(
    workspace: Workspace, context: ToolExecutionContext, make_connection
) -> None:
    connection = await make_connection(
        workspace,
        connector_type="example",
        name="Example conn",
        auth_type="api_key",
        credentials={"api_key": "ex-123456"},
    )
    _definition, executor = ExampleConnector().tools()[0]
    output = await executor(context, PingInput(connection_id=str(connection.id), message="hello"))
    assert isinstance(output, PingOutput)
    assert output.reply == "pong: hello"
    assert output.connection_name == "Example conn"
