"""The terminal Internet control uses ordinary audited grants atomically."""

import pytest
from apps.api.tests.test_policy_rbac import (
    CSRF_HEADERS,
    PolicyRbacHarness,
)
from apps.api.tests.test_policy_rbac import (
    policy_rbac as policy_rbac,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_api.access.keys import ApiKeyPrincipal
from jhin_connectors.cli.connector import CliConnector
from jhin_db.models import AgentCapabilityGrant, AuditEvent, Connection
from jhin_domain import WorkspaceRole
from jhin_domain.ids import new_uuid7
from jhin_policy import Grant, evaluate


async def test_api_keys_require_agent_admin_and_hide_connections_without_app_read(
    policy_rbac: PolicyRbacHarness, session: AsyncSession
) -> None:
    world = policy_rbac
    sandbox = await _sandbox(session, world)
    assert (await _set(world, True, sandbox)).status_code == 200

    def key(*scopes: str) -> ApiKeyPrincipal:
        return ApiKeyPrincipal(
            id=new_uuid7(),
            workspace_id=world.workspace_id,
            name="automation",
            prefix="jhin_test",
            role_ceiling=WorkspaceRole.ADMIN,
            scopes=frozenset(scopes),
        )

    world.actor["api_key"] = key("agents:write", "apps:read")
    assert (await world.client.get(_url(world))).status_code == 403
    assert (await _set(world, False)).status_code == 403
    world.actor["api_key"] = key("agents:admin")
    status = await world.client.get(_url(world))
    assert status.status_code == 200, status.text
    assert status.json()["enabled"] is True
    assert status.json()["connections"] == []
    assert status.json()["connection_id"] is None
    updated = await _set(world, True, sandbox)
    assert updated.status_code == 200, updated.text
    assert updated.json()["connections"] == []
    assert updated.json()["connection_id"] is None
    world.actor["api_key"] = key("agents:admin", "apps:read")
    status = await world.client.get(_url(world))
    assert status.status_code == 200, status.text
    assert status.json()["connections"] == [{"id": str(sandbox.id), "name": sandbox.name}]
    assert status.json()["connection_id"] == str(sandbox.id)


def _url(world: PolicyRbacHarness) -> str:
    return f"/api/v1/workspaces/{world.workspace_id}/agents/{world.agent.id}/terminal-internet"


async def _sandbox(
    session: AsyncSession, world: PolicyRbacHarness, name: str = "Sandbox"
) -> Connection:
    connection = Connection(
        workspace_id=world.workspace_id,
        name=name,
        connector_type="cli",
        auth_type="none",
        status="active",
        config_json={"default_network": "none"},
    )
    session.add(connection)
    await session.commit()
    return connection


async def _rows(session: AsyncSession, world: PolicyRbacHarness) -> list[AgentCapabilityGrant]:
    return list(
        await session.scalars(
            select(AgentCapabilityGrant).where(AgentCapabilityGrant.agent_id == world.agent.id)
        )
    )


async def _set(world: PolicyRbacHarness, enabled: bool, connection: Connection | None = None):
    return await world.client.put(
        _url(world),
        json={"enabled": enabled, "connection_id": str(connection.id) if connection else None},
        headers=CSRF_HEADERS,
    )


async def test_enable_is_pinned_audited_and_idempotent(
    policy_rbac: PolicyRbacHarness, session: AsyncSession
) -> None:
    world = policy_rbac
    sandbox = await _sandbox(session, world)
    original_policy = list(world.agent.approval_policy_json)
    first = await _set(world, True, sandbox)
    assert first.status_code == 200, first.text
    assert first.json()["enabled"] is True
    assert first.json()["connection_id"] == str(sandbox.id)
    assert (await _set(world, True, sandbox)).status_code == 200
    grants = [row for row in await _rows(session, world) if row.capability == "cli.command.execute"]
    assert len(grants) == 1
    assert grants[0].effect == "allow"
    assert grants[0].scope_json == {
        "connection_id": str(sandbox.id),
        "network": "internet",
        "command": "*",
    }
    events = list(
        await session.scalars(
            select(AuditEvent).where(AuditEvent.action == "agent.permission.granted")
        )
    )
    assert len(events) == 1
    assert events[0].metadata_json["source"] == "terminal_internet_control"
    assert world.agent.approval_policy_json == original_policy
    assert sandbox.config_json == {"default_network": "none"}


async def test_off_beats_wildcard_and_preserves_handmade_grants(
    policy_rbac: PolicyRbacHarness, session: AsyncSession
) -> None:
    world = policy_rbac
    sandbox = await _sandbox(session, world)
    handmade = AgentCapabilityGrant(
        workspace_id=world.workspace_id,
        agent_id=world.agent.id,
        capability="*",
        scope_json={},
        effect="allow",
    )
    session.add(handmade)
    await session.commit()
    assert (await _set(world, True, sandbox)).status_code == 200
    off = await _set(world, False)
    assert off.status_code == 200, off.text
    assert off.json()["status"] == "blocked"
    assert off.json()["enabled"] is False
    rows = await _rows(session, world)
    assert any(row.id == handmade.id for row in rows)
    assert not any(
        row.capability == "cli.command.execute" and row.effect == "allow" for row in rows
    )
    grants = [
        Grant(capability=row.capability, scope=row.scope_json, effect=row.effect) for row in rows
    ]
    definition = next(
        item for item in CliConnector().tool_definitions() if item.name == "cli.command.execute"
    )
    # Command scope is resolved by the mandatory CLI validator, after the
    # generic capability/risk decision, exactly as the gateway runs it.
    assert evaluate(
        definition,
        grants=grants,
        rules=[],
        requested_scope={
            "connection_id": str(sandbox.id),
            "network": "internet",
            "command": "curl https://example.com",
            "image": "",
        },
    ).allowed
    from jhin_connectors.cli.schemas import CommandExecuteInput
    from jhin_tools.builtin import ToolExecutionContext

    ctx = ToolExecutionContext(
        session=session,
        workspace_id=world.workspace_id,
        agent_id=world.agent.id,
        agent_name=world.agent.name,
        task_id=new_uuid7(),
        run_id=new_uuid7(),
    )
    validator = CliConnector().tool_validators()[definition.name]
    denied = await validator(
        ctx,
        CommandExecuteInput(
            connection_id=str(sandbox.id),
            command="curl https://example.com",
            network="internet",
        ),
        grants,
    )
    assert denied is not None and denied.code == "terminal_internet_denied"
    assert (
        await validator(
            ctx,
            CommandExecuteInput(
                connection_id=str(sandbox.id),
                command="ls",
                network="none",
            ),
            grants,
        )
        is None
    )
    assert (await _set(world, False)).status_code == 200
    assert len([row for row in await _rows(session, world) if row.effect == "deny"]) == 1


async def test_switching_connection_removes_only_previously_managed_allow(
    policy_rbac: PolicyRbacHarness, session: AsyncSession
) -> None:
    world = policy_rbac
    one = await _sandbox(session, world, "One")
    two = await _sandbox(session, world, "Two")
    assert (await _set(world, True, one)).status_code == 200
    assert (await _set(world, True, two)).status_code == 200
    grants = [row for row in await _rows(session, world) if row.capability == "cli.command.execute"]
    assert len(grants) == 1
    assert grants[0].scope_json["connection_id"] == str(two.id)


async def test_existing_exact_allow_reads_on_without_adoption_or_duplication(
    policy_rbac: PolicyRbacHarness, session: AsyncSession
) -> None:
    world = policy_rbac
    sandbox = await _sandbox(session, world)
    handmade = AgentCapabilityGrant(
        workspace_id=world.workspace_id,
        agent_id=world.agent.id,
        capability="cli.command.execute",
        scope_json={"connection_id": str(sandbox.id), "network": "internet", "command": "*"},
        effect="allow",
    )
    session.add(handmade)
    await session.commit()
    response = await world.client.get(_url(world))
    assert response.status_code == 200, response.text
    assert response.json()["enabled"] is True
    assert (await _set(world, True, sandbox)).status_code == 200
    assert (
        len([row for row in await _rows(session, world) if row.capability == "cli.command.execute"])
        == 1
    )
    assert (await _set(world, False)).status_code == 200
    assert await session.get(AgentCapabilityGrant, handmade.id) is not None
    assert (await _set(world, True, sandbox)).json()["enabled"] is True


async def test_handmade_deny_is_preserved_and_reported_as_custom(
    policy_rbac: PolicyRbacHarness, session: AsyncSession
) -> None:
    world = policy_rbac
    sandbox = await _sandbox(session, world)
    deny = AgentCapabilityGrant(
        workspace_id=world.workspace_id,
        agent_id=world.agent.id,
        capability="cli.*",
        scope_json={},
        effect="deny",
    )
    session.add(deny)
    await session.commit()
    response = await _set(world, True, sandbox)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "custom"
    assert response.json()["enabled"] is False
    assert await session.get(AgentCapabilityGrant, deny.id) is not None


async def test_list_scoped_legacy_grant_is_custom_not_a_status_error(
    policy_rbac: PolicyRbacHarness, session: AsyncSession
) -> None:
    world = policy_rbac
    sandbox = await _sandbox(session, world)
    session.add(
        AgentCapabilityGrant(
            workspace_id=world.workspace_id,
            agent_id=world.agent.id,
            capability="cli.command.execute",
            scope_json={"connection_id": [str(sandbox.id)], "network": "internet", "command": "*"},
            effect="allow",
        )
    )
    await session.commit()
    response = await world.client.get(_url(world))
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "custom"


async def test_narrow_handmade_deny_keeps_enabled_status_with_custom_warning(
    policy_rbac: PolicyRbacHarness, session: AsyncSession
) -> None:
    world = policy_rbac
    sandbox = await _sandbox(session, world)
    session.add(
        AgentCapabilityGrant(
            workspace_id=world.workspace_id,
            agent_id=world.agent.id,
            capability="cli.command.execute",
            effect="deny",
            scope_json={
                "connection_id": str(sandbox.id),
                "network": "internet",
                "command": "curl *",
            },
        )
    )
    await session.commit()
    response = await _set(world, True, sandbox)
    assert response.status_code == 200, response.text
    assert response.json()["enabled"] is True
    assert response.json()["has_custom_grants"] is True
    rows = [row for row in await _rows(session, world) if row.capability == "cli.command.execute"]
    assert len(rows) == 2 and any(row.effect == "deny" for row in rows)


@pytest.mark.parametrize("role", [WorkspaceRole.MEMBER, WorkspaceRole.VIEWER])
async def test_terminal_internet_is_admin_only(
    policy_rbac: PolicyRbacHarness, role: WorkspaceRole
) -> None:
    world = policy_rbac
    world.actor["user"] = world.users[role.value]
    assert (await world.client.get(_url(world))).status_code == 403
    assert (await _set(world, False)).status_code == 403


async def test_invalid_connection_cannot_remove_existing_access(
    policy_rbac: PolicyRbacHarness, session: AsyncSession
) -> None:
    world = policy_rbac
    sandbox = await _sandbox(session, world)
    assert (await _set(world, True, sandbox)).status_code == 200
    for connection in (world.connection, world.foreign_connection):
        response = await _set(world, True, connection)
        assert response.status_code == 422, response.text
    assert (await world.client.get(_url(world))).json()["connection_id"] == str(sandbox.id)
    response = await world.client.put(_url(world), json={"enabled": False})
    assert response.status_code == 403
    foreign = _url(world).replace(str(world.agent.id), str(world.foreign_agent.id))
    assert (await world.client.get(foreign)).status_code == 404
    assert (
        await world.client.put(foreign, json={"enabled": False}, headers=CSRF_HEADERS)
    ).status_code == 404


async def test_disabled_connection_is_not_an_enable_choice(
    policy_rbac: PolicyRbacHarness, session: AsyncSession
) -> None:
    world = policy_rbac
    sandbox = await _sandbox(session, world)
    sandbox.status = "disabled"
    await session.commit()
    assert (await _set(world, True, sandbox)).status_code == 422
    response = await world.client.get(_url(world))
    assert response.status_code == 200
    assert response.json()["connections"] == []
