"""Managed toolkit discovery and execution use scoped, pinned Jhin tools."""

import json
from typing import Any
from uuid import uuid4

import httpx
import pytest

from jhin_connectors.composio import COMPOSIO_ISSUER, ComposioError, managed_user_id
from jhin_connectors.registry import default_registry
from jhin_policy import RiskLevel


def listed(**updates: Any) -> dict[str, Any]:
    return {
        "slug": "SLACK_SEND_MESSAGE",
        "name": "Send message",
        "description": "Send a message.",
        "toolkit": {"slug": "slack"},
        "version": "20260908_00",
        "input_parameters": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
        **updates,
    }


def test_generic_managed_connector_is_registered() -> None:
    connector = default_registry().get("composio")
    assert connector is not None
    assert connector.manifest.auth_scheme("managed") is not None
    assert connector.tools() == ()
    with pytest.raises(ValueError):
        connector.validate_settings("managed", {"toolkit": "../slack", "server_slug": "slack"})


async def test_discovery_lists_paginated_toolkit_versions_and_pins_following_pages() -> None:
    from jhin_connectors.composio.tools_client import ComposioToolsClient

    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "items": [
                    listed(
                        slug=("SLACK_SEND_MESSAGE" if len(requests) == 1 else "SLACK_LIST_CHANNELS")
                    )
                ],
                "next_cursor": "page-two" if len(requests) == 1 else None,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await ComposioToolsClient(http, api_key="secret").list_tools("slack")
    assert len(result) == 2
    assert requests[0].url.path == "/api/v3.1/tools"
    assert requests[0].url.params["toolkit_slug"] == "slack"
    assert requests[0].url.params["toolkit_versions"] == "latest"
    assert requests[1].url.params["toolkit_versions[slack]"] == "20260908_00"
    assert requests[1].url.params["cursor"] == "page-two"


@pytest.mark.parametrize(
    "update",
    [
        {"version": "latest"},
        {"version": ""},
        {"toolkit": {"slug": "gmail"}},
        {"slug": "../execute/proxy"},
    ],
)
async def test_discovery_refuses_unpinned_or_cross_toolkit_tools(update) -> None:
    from jhin_connectors.composio.tools_client import ComposioToolsClient

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"items": [listed(**update)]})
        )
    ) as http:
        with pytest.raises(ComposioError):
            await ComposioToolsClient(http, api_key="secret").list_tools("slack")


def test_dynamic_definitions_are_scoped_and_default_to_destructive() -> None:
    from jhin_connectors.composio.tools import connection_tool_definitions, discovery_payload

    config = {"toolkit": "slack", "server_slug": "work_slack", **discovery_payload([listed()])}
    (definition,) = connection_tool_definitions(config)
    assert definition.name == "composio.work_slack.slack_send_message"
    assert definition.risk is RiskLevel.DESTRUCTIVE and definition.supports_approval
    assert set(definition.scope_keys) == {"connection_id", "server_slug", "toolkit", "tool"}
    schema = definition.input_json_schema()
    assert schema["properties"]["server_slug"]["const"] == "work_slack"
    assert schema["properties"]["toolkit"]["const"] == "slack"
    assert schema["properties"]["tool"]["const"] == "slack_send_message"
    for forbidden in (
        {"tool": "gmail_send_email"},
        {"connected_account_id": "other"},
        {"server_slug": "other"},
        {"toolkit": "gmail"},
        {"version": "latest"},
    ):
        with pytest.raises(ValueError):
            definition.input_model.model_validate({"connection_id": str(uuid4()), **forbidden})


async def _managed_connection(make_connection, workspace):
    from jhin_connectors.composio.tools import discovery_payload

    user = uuid4()
    binding = {
        "composio_account_id": "ca_slack",
        "composio_auth_config_id": "ac_slack",
        "composio_toolkit": "slack",
        "composio_user_id": managed_user_id(workspace.id, user),
    }
    connection = await make_connection(
        workspace,
        connector_type="composio",
        auth_type="managed",
        credentials=binding,
        config={"toolkit": "slack", "server_slug": "work_slack", **discovery_payload([listed()])},
    )
    connection.oauth_issuer = COMPOSIO_ISSUER
    connection.oauth_authorized_by_user_id = user
    return connection, binding


async def test_executor_sends_only_bound_account_tool_and_version(
    make_connection,
    workspace,
    context,
    monkeypatch,
) -> None:
    from jhin_connectors.composio import tools
    from jhin_connectors.composio.tools_client import ComposioToolsClient

    connection, binding = await _managed_connection(make_connection, workspace)
    await context.session.flush()
    seen = []

    def handler(request):
        seen.append(request)
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "id": "ca_slack",
                    "user_id": binding["composio_user_id"],
                    "toolkit": {"slug": "slack"},
                    "auth_config": {"id": "ac_slack"},
                    "status": "ACTIVE",
                },
            )
        return httpx.Response(200, json={"successful": True, "data": {"message_id": "message-1"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        monkeypatch.setattr(
            tools, "ComposioToolsClient", lambda: ComposioToolsClient(http, api_key="secret")
        )
        definition, executor = tools.connection_tools(connection.config_json)[0]
        payload = definition.input_model.model_validate(
            {"connection_id": str(connection.id), "arguments": {"text": "Hello"}}
        )
        result = await executor(context, payload)
    assert seen[-1].url.path == "/api/v3.1/tools/execute/SLACK_SEND_MESSAGE"
    assert json.loads(seen[-1].content) == {
        "connected_account_id": "ca_slack",
        "user_id": binding["composio_user_id"],
        "version": "20260908_00",
        "arguments": {"text": "Hello"},
    }
    assert result.model_dump()["data"] == {"message_id": "message-1"}


async def test_executor_refuses_same_secret_id_rotated_to_another_account(
    make_connection,
    workspace,
    context,
    monkeypatch,
) -> None:
    from jhin_connectors.composio import tools
    from jhin_secrets import SecretStore
    from jhin_tools.errors import ToolExecutionError

    connection, binding = await _managed_connection(make_connection, workspace)
    await context.session.flush()
    original_secret_id = connection.encrypted_secret_id
    dispatched = []

    class Client:
        async def get_account(self, account_id):
            # Reconnecting rotates ciphertext in place while the old remote
            # account remains active. The connection's secret ID is unchanged.
            await SecretStore(context.session, context.crypto).rotate(
                workspace.id,
                original_secret_id,
                json.dumps({**binding, "composio_account_id": "ca_reconnected"}),
            )
            await context.session.flush()
            return {
                "id": account_id,
                "user_id": binding["composio_user_id"],
                "toolkit": {"slug": "slack"},
                "auth_config": {"id": "ac_slack"},
                "status": "ACTIVE",
            }

        async def execute_tool(self, *args, **kwargs):
            dispatched.append(kwargs)
            return {"successful": True, "data": {}}

    monkeypatch.setattr(tools, "ComposioToolsClient", Client)
    definition, executor = tools.connection_tools(connection.config_json)[0]
    with pytest.raises(ToolExecutionError, match="binding changed") as error:
        await executor(
            context,
            definition.input_model.model_validate(
                {"connection_id": str(connection.id), "arguments": {"text": "Hello"}},
            ),
        )
    assert error.value.side_effect_possible is False
    assert connection.encrypted_secret_id == original_secret_id
    assert dispatched == []


@pytest.mark.parametrize("change", ["version", "tool", "risk", "schema", "toolkit"])
async def test_bound_execution_refuses_discovery_or_risk_changes(
    make_connection,
    workspace,
    context,
    monkeypatch,
    change,
) -> None:
    from jhin_connectors.composio import tools
    from jhin_tools.errors import ToolExecutionError

    connection, _ = await _managed_connection(make_connection, workspace)
    definition, executor = tools.connection_tools(connection.config_json)[0]
    config = json.loads(json.dumps(connection.config_json))
    if change == "version":
        config["composio_tools"][0]["version"] = "20260909_00"
    elif change == "tool":
        config["composio_tools"] = []
    elif change == "risk":
        config["tool_risk_overrides"] = {"slack_send_message": "read"}
    elif change == "schema":
        config["composio_tools"][0]["input_schema"] = {"type": "object"}
    else:
        config["toolkit"] = "gmail"
    connection.config_json = config
    await context.session.flush()
    monkeypatch.setattr(
        tools, "ComposioToolsClient", lambda: pytest.fail("changed tool must not execute")
    )
    with pytest.raises(ToolExecutionError):
        await executor(
            context,
            definition.input_model.model_validate(
                {"connection_id": str(connection.id), "arguments": {"text": "Hello"}}
            ),
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", "ca_other"),
        ("user_id", "another-user"),
        ("toolkit", {"slug": "gmail"}),
        ("auth_config", {"id": "ac_other"}),
        ("status", "EXPIRED"),
    ],
)
async def test_executor_checks_account_owner_toolkit_and_active_state_before_execution(
    make_connection,
    workspace,
    context,
    monkeypatch,
    field,
    value,
) -> None:
    from jhin_connectors.composio import tools
    from jhin_connectors.composio.tools_client import ComposioToolsClient
    from jhin_tools.errors import ToolExecutionError

    connection, binding = await _managed_connection(make_connection, workspace)
    await context.session.flush()
    account = {
        "id": "ca_slack",
        "user_id": binding["composio_user_id"],
        "toolkit": {"slug": "slack"},
        "auth_config": {"id": "ac_slack"},
        "status": "ACTIVE",
    }
    account[field] = value

    def handler(request):
        assert request.method == "GET", "a mismatched account must never execute a tool"
        return httpx.Response(200, json=account)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        monkeypatch.setattr(
            tools, "ComposioToolsClient", lambda: ComposioToolsClient(http, api_key="secret")
        )
        definition, executor = tools.connection_tools(connection.config_json)[0]
        with pytest.raises(ToolExecutionError):
            await executor(
                context,
                definition.input_model.model_validate(
                    {"connection_id": str(connection.id), "arguments": {"text": "Hello"}}
                ),
            )


async def test_dynamic_source_uses_only_active_managed_connections_in_its_workspace(
    make_connection,
    workspace,
    context,
) -> None:
    from jhin_connectors.composio.source import workspace_composio_tool_definitions
    from jhin_db.models import Workspace

    connection, _ = await _managed_connection(make_connection, workspace)
    other = Workspace(name="Other", slug="other-composio-tools")
    context.session.add(other)
    await context.session.flush()
    other_connection, _ = await _managed_connection(make_connection, other)
    other_connection.config_json = {**other_connection.config_json, "server_slug": "private"}
    await context.session.flush()
    definitions = await workspace_composio_tool_definitions(context.session, workspace.id)
    assert [item.name for item in definitions] == ["composio.work_slack.slack_send_message"]
    from jhin_connectors.registry import build_default_catalog

    catalog = await build_default_catalog().for_workspace(context.session, workspace.id)
    assert catalog.get("composio.work_slack.slack_send_message") is not None
    assert catalog.get("composio.private.slack_send_message") is None
    connection.status = "disabled"
    await context.session.flush()
    assert await workspace_composio_tool_definitions(context.session, workspace.id) == ()


def test_incomplete_settings_do_not_build_executable_tools() -> None:
    from jhin_connectors.composio.tools import connection_tools, discovery_payload

    assert connection_tools({"toolkit": "slack", **discovery_payload([listed()])}) == ()


async def test_bound_tool_schema_is_a_snapshot_not_a_mutable_config_reference(
    make_connection,
    workspace,
    context,
    monkeypatch,
) -> None:
    from jhin_connectors.composio import tools
    from jhin_tools.errors import ToolExecutionError

    connection, _ = await _managed_connection(make_connection, workspace)
    definition, executor = tools.connection_tools(connection.config_json)[0]
    connection.config_json["composio_tools"][0]["input_schema"]["properties"]["text"]["type"] = (
        "integer"
    )
    connection.config_json = json.loads(json.dumps(connection.config_json))
    from sqlalchemy.orm.attributes import flag_modified

    flag_modified(connection, "config_json")
    await context.session.flush()
    monkeypatch.setattr(
        tools, "ComposioToolsClient", lambda: pytest.fail("changed schema must not execute")
    )
    with pytest.raises(ToolExecutionError):
        await executor(
            context,
            definition.input_model.model_validate(
                {"connection_id": str(connection.id), "arguments": {"text": "Hello"}}
            ),
        )
