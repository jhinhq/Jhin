"""Network denies must apply to the network the command will actually use."""

from types import SimpleNamespace
from typing import Any

import pytest

from jhin_connectors.cli import connector, tools, validators
from jhin_connectors.cli.connector import CliConnector
from jhin_connectors.cli.schemas import CommandExecuteInput
from jhin_policy import DecisionType, Grant, evaluate


@pytest.mark.parametrize(
    "network,default,granted,allowed",
    [
        ("none", "none", "internet", True),
        ("", "none", "internet", True),
        ("", "internet", "internet", True),
        ("internet", "none", "internet", True),
        ("none", "internet", "none", True),
        ("", "none", "none", True),
        ("internet", "none", "none", False),
        ("", "internet", "none", False),
    ],
)
async def test_network_permission_is_a_ceiling_after_authorized_defaults_resolve(
    monkeypatch, network, default, granted, allowed
):
    async def connection(*_args):
        return SimpleNamespace(config_json={"default_network": default}, name="Sandbox")

    async def workspace(*_args):
        return None

    monkeypatch.setattr(tools, "_load_cli_connection", connection)
    monkeypatch.setattr(validators, "workspace_repository_validator", workspace)
    cli = CliConnector()
    definition = next(item for item in cli.tool_definitions() if item.name == "cli.command.execute")
    payload = CommandExecuteInput(connection_id="sandbox", command="ls", network=network)
    grant = Grant(
        capability="cli.command.execute",
        scope={"connection_id": "sandbox", "command": "*", "network": granted},
    )
    before = grant.model_dump()
    policy = evaluate(definition, grants=[grant], rules=[], requested_scope=payload.model_dump())
    veto = await cli.tool_validators()[definition.name](SimpleNamespace(), payload, [grant])
    assert (policy.allowed and veto is None) is allowed
    assert grant.model_dump() == before


@pytest.mark.parametrize(
    "scope,payload_changes,default_image,allowed",
    [
        ({"connection_id": "other"}, {}, "", False),
        ({"command": "curl *"}, {}, "", False),
        ({"image": "approved:1"}, {"image": "other:1"}, "approved:1", False),
        ({"image": "approved:1"}, {}, "other:1", False),
        ({"image": "approved:1"}, {}, "approved:1", True),
        ({"image": "approved:1"}, {"image": "approved:1"}, "other:1", True),
        ({"repository": "unprovided"}, {}, "", False),
    ],
)
async def test_internet_permission_keeps_every_other_scope_dimension(
    monkeypatch, scope, payload_changes, default_image, allowed
):
    async def connection(*_args):
        return SimpleNamespace(
            config_json={"default_network": "none", "default_image": default_image}, name="Sandbox"
        )

    async def workspace(*_args):
        return None

    monkeypatch.setattr(tools, "_load_cli_connection", connection)
    monkeypatch.setattr(validators, "workspace_repository_validator", workspace)
    payload = CommandExecuteInput(connection_id="sandbox", command="ls", **payload_changes)
    grants = [Grant(capability="cli.command.execute", scope={"network": "internet", **scope})]
    result = await validators.command_network_validator(SimpleNamespace(), payload, grants)
    assert (result is None) is allowed
    if not allowed:
        assert result.code == "scope_mismatch"


@pytest.mark.parametrize(
    "scope",
    [
        {"network": "none"},
        {"network": ""},
        {"command": "ls"},
        {"image": "approved:1"},
        {"image": ""},
        {"connection_id": "sandbox"},
    ],
)
async def test_cli_denies_keep_raw_and_resolved_scope_restrictions(monkeypatch, scope):
    async def connection(*_args):
        return SimpleNamespace(
            config_json={"default_network": "none", "default_image": "approved:1"}, name="Sandbox"
        )

    async def workspace(*_args):
        return None

    monkeypatch.setattr(tools, "_load_cli_connection", connection)
    monkeypatch.setattr(validators, "workspace_repository_validator", workspace)
    result = await validators.command_network_validator(
        SimpleNamespace(),
        CommandExecuteInput(connection_id="sandbox", command="ls"),
        [
            Grant(capability="cli.*"),
            Grant(capability="cli.command.execute", effect="deny", scope=scope),
        ],
    )
    assert result is not None and result.decision is DecisionType.DENY


async def test_gateway_dispatches_default_offline_and_rechecks_revocation(
    session, workspace, context, monkeypatch
):
    import json

    from jhin_connectors.registry import ConnectorRegistry, build_default_catalog
    from jhin_db.models import Agent, AgentCapabilityGrant, AgentRun, Connection, Task
    from jhin_domain import new_uuid7
    from jhin_tools.gateway import ToolGateway

    session.add(Agent(id=context.agent_id, workspace_id=workspace.id, name="Scout", slug="scout"))
    session.add(
        Task(
            id=context.task_id,
            workspace_id=workspace.id,
            assigned_agent_id=context.agent_id,
            title="Commands",
            correlation_id=new_uuid7(),
            state="running",
        )
    )
    session.add(
        AgentRun(
            id=context.run_id,
            workspace_id=workspace.id,
            agent_id=context.agent_id,
            task_id=context.task_id,
            status="running",
        )
    )
    connection = Connection(
        workspace_id=workspace.id,
        name="Sandbox",
        connector_type="cli",
        auth_type="none",
        status="active",
        config_json={"default_network": "none"},
    )
    session.add(connection)
    await session.flush()
    scope = {"connection_id": str(connection.id), "network": "internet", "command": "*"}
    grant = AgentCapabilityGrant(
        workspace_id=workspace.id,
        agent_id=context.agent_id,
        capability="cli.command.execute",
        effect="allow",
        scope_json=scope,
    )
    session.add(grant)
    await session.commit()
    registry = ConnectorRegistry()
    registry.register(CliConnector())
    gateway = ToolGateway(context, build_default_catalog(registry))
    dispatched = []

    async def run(_ctx, **kwargs):
        dispatched.append(kwargs["network"])
        return SimpleNamespace(
            id=new_uuid7(),
            status="completed",
            network_policy=kwargs["network"],
            exit_code=0,
            duration_ms=1,
            stdout_tail="files",
            stderr_tail="",
        ), {}

    monkeypatch.setattr(tools, "_run_job", run)

    async def command(network=""):
        return await gateway.request(
            "cli.command.execute",
            json.dumps(
                {
                    "connection_id": str(connection.id),
                    "command": "ls",
                    "network": network,
                }
            ),
        )

    assert (await command()).status == "executed"
    assert dispatched == ["none"]
    grant.scope_json = {**scope, "network": "none"}
    connection.config_json = {"default_network": "internet"}
    await session.commit()
    assert (await command()).decision_code == "scope_mismatch"
    assert dispatched == ["none"]
    grant.scope_json = scope
    session.add(
        AgentCapabilityGrant(
            workspace_id=workspace.id,
            agent_id=context.agent_id,
            capability="cli.command.execute",
            effect="deny",
            scope_json={"network": "internet"},
        )
    )
    await session.commit()
    assert (await command()).decision_code == "terminal_internet_denied"
    assert (await command("none")).status == "executed"
    assert dispatched == ["none", "none"]


@pytest.mark.parametrize(
    "network,default,denied",
    [
        ("internet", "none", True),
        ("", "internet", True),
        ("none", "internet", False),
        ("", "none", False),
    ],
)
async def test_off_overrides_wildcard_allow_without_blocking_isolated_commands(
    monkeypatch: pytest.MonkeyPatch,
    network: str,
    default: str,
    denied: bool,
) -> None:
    async def connection(*_args: Any) -> Any:
        return SimpleNamespace(config_json={"default_network": default}, name="Sandbox")

    async def workspace(*_args: Any) -> None:
        return None

    monkeypatch.setattr(tools, "_load_cli_connection", connection)
    monkeypatch.setattr(validators, "workspace_repository_validator", workspace)
    monkeypatch.setattr(connector, "workspace_repository_validator", workspace)
    validator = CliConnector().tool_validators()["cli.command.execute"]
    result = await validator(
        SimpleNamespace(),  # type: ignore[arg-type]
        CommandExecuteInput(
            connection_id="sandbox", command="curl https://example.com", network=network
        ),  # type: ignore[arg-type]
        [
            Grant(capability="*"),
            Grant(capability="cli.command.execute", effect="deny", scope={"network": "internet"}),
        ],
    )
    assert (result is not None and result.decision is DecisionType.DENY) is denied
    if denied:
        assert result is not None and result.code == "terminal_internet_denied"


@pytest.mark.parametrize(
    "scope",
    [
        {"network": "internet", "connection_id": "other"},
        {"network": "internet", "command": "curl *"},
    ],
)
async def test_narrow_deny_does_not_block_other_connections_or_commands(
    monkeypatch: pytest.MonkeyPatch,
    scope: dict[str, str],
) -> None:
    async def connection(*_args: Any) -> Any:
        return SimpleNamespace(config_json={"default_network": "internet"}, name="Sandbox")

    async def workspace(*_args: Any) -> None:
        return None

    monkeypatch.setattr(tools, "_load_cli_connection", connection)
    monkeypatch.setattr(validators, "workspace_repository_validator", workspace)
    monkeypatch.setattr(connector, "workspace_repository_validator", workspace)
    result = await CliConnector().tool_validators()["cli.command.execute"](
        SimpleNamespace(),  # type: ignore[arg-type]
        CommandExecuteInput(connection_id="sandbox", command="python -m pip install requests"),
        [
            Grant(capability="cli.*"),
            Grant(capability="cli.command.execute", effect="deny", scope=scope),
        ],
    )
    assert result is None


async def test_resolved_default_and_denies_are_rechecked_on_each_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = {"default_network": "none"}

    async def connection(*_args: Any) -> Any:
        return SimpleNamespace(config_json=config, name="Sandbox")

    async def workspace(*_args: Any) -> None:
        return None

    monkeypatch.setattr(tools, "_load_cli_connection", connection)
    monkeypatch.setattr(validators, "workspace_repository_validator", workspace)
    monkeypatch.setattr(connector, "workspace_repository_validator", workspace)
    validator = CliConnector().tool_validators()["cli.command.execute"]
    payload = CommandExecuteInput(connection_id="sandbox", command="curl https://example.com")
    grants = [
        Grant(capability="cli.*"),
        Grant(capability="cli.*", effect="deny", scope={"network": "internet"}),
    ]
    assert await validator(SimpleNamespace(), payload, grants) is None  # type: ignore[arg-type]
    config["default_network"] = "internet"
    result = await validator(SimpleNamespace(), payload, grants)  # type: ignore[arg-type]
    assert result is not None and result.decision is DecisionType.DENY


async def test_command_network_validation_keeps_the_repository_veto(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jhin_policy import PolicyDecision

    veto = PolicyDecision(
        decision=DecisionType.DENY, code="repository_not_allowed", reason="Blocked repository"
    )

    async def workspace(*_args: Any) -> Any:
        return veto

    monkeypatch.setattr(validators, "workspace_repository_validator", workspace)
    monkeypatch.setattr(connector, "workspace_repository_validator", workspace)
    result = await CliConnector().tool_validators()["cli.command.execute"](
        SimpleNamespace(),  # type: ignore[arg-type]
        CommandExecuteInput(connection_id="sandbox", command="echo hello", network="none"),
        [Grant(capability="cli.*")],
    )
    assert result == veto
