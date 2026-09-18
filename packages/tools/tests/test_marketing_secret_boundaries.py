"""Representative native credential boundaries, with real stores/gateway and no egress.

This is not exhaustive ingress, browser, trace-export, or remote-runner coverage.
The internal VariableStore.bind primitive is trusted; the negative binding
assertions exercise the model-facing native schemas, not arbitrary Python callers.
"""

import json
from dataclasses import replace
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import select

from jhin_agents.context import ConversationTurn, TaskContext, build_messages
from jhin_agents.snapshot import AgentExecutionSnapshot, ModelProfileSnapshot, RunLimits
from jhin_connectors.execution import ConnectionResolutionError, resolve_connection
from jhin_connectors.mcp.discovery import DiscoveredTool
from jhin_connectors.mcp.tools import build_definition, make_executor
from jhin_connectors.registry import build_default_catalog
from jhin_db.models import (
    AgentCapabilityGrant,
    AgentRun,
    AuditEvent,
    Connection,
    Conversation,
    MemoryRecord,
    Message,
    Secret,
    Task,
    Team,
    ToolCall,
    User,
)
from jhin_db.models.variables import ScopedVariable
from jhin_domain import new_uuid7
from jhin_models import ModelRequest
from jhin_models.providers.openai_compatible import OpenAICompatibleClient
from jhin_policy import RiskLevel
from jhin_secrets import MasterKey, SecretCrypto, SecretStore, get_redactor
from jhin_secrets.intake import capture_input
from jhin_secrets.redaction import redact_event_dict
from jhin_secrets.variables import VariableActor, VariableError, VariableStore
from jhin_tools.gateway import ToolGateway


@pytest.fixture
async def captured(context, monkeypatch):
    db = context.session
    crypto = SecretCrypto(MasterKey(key=b"s" * 32))
    ctx = replace(context, crypto=crypto)
    owner = User(email=f"{new_uuid7()}@example.test", display_name="Owner", password_hash="fixture")
    team = Team(workspace_id=ctx.workspace_id, name="Marketing")
    db.add_all([owner, team])
    await db.flush()
    conversation = Conversation(
        workspace_id=ctx.workspace_id,
        primary_agent_id=ctx.agent_id,
        created_by_user_id=owner.id,
        title="Ghost setup",
        last_activity_at=datetime.now(UTC),
    )
    db.add(conversation)
    await db.flush()
    db.add(
        Task(
            id=ctx.task_id,
            workspace_id=ctx.workspace_id,
            assigned_agent_id=ctx.agent_id,
            assigned_team_id=team.id,
            conversation_id=conversation.id,
            title="Ghost setup",
            correlation_id=new_uuid7(),
        )
    )
    await db.flush()
    db.add(
        AgentRun(
            id=ctx.run_id, workspace_id=ctx.workspace_id, task_id=ctx.task_id, agent_id=ctx.agent_id
        )
    )
    sentinel = "1234567890abcdef12345678:" + "a1" * 32
    intake = await capture_input(
        db,
        crypto,
        workspace_id=ctx.workspace_id,
        conversation_id=conversation.id,
        agent_id=ctx.agent_id,
        user_id=owner.id,
        text=f"Connect Ghost using this API key: {sentinel}",
    )
    actor = VariableActor(ctx.workspace_id, "agent", ctx.agent_id, conversation_id=conversation.id)
    store = VariableStore(db, crypto)
    variable = await store.set(
        actor,
        name="ghost.key",
        scope="agent",
        scope_id=ctx.agent_id,
        sensitive=True,
        secret_ref=intake.references[0]["secret_ref"],
    )
    ghost = Connection(
        workspace_id=ctx.workspace_id,
        name="Ghost",
        connector_type="ghost",
        auth_type="api_key",
        config_json={
            "admin_url": "https://blog.example.test",
            "admin_key_variable_id": str(variable.id),
        },
    )
    message = Message(
        workspace_id=ctx.workspace_id,
        conversation_id=conversation.id,
        task_id=ctx.task_id,
        sender_type="user",
        sender_id=owner.id,
        recipient_type="agent",
        recipient_id=ctx.agent_id,
        visibility="visible",
        content_json={"text": intake.text, "secure_inputs": intake.references},
    )
    db.add_all([ghost, message])
    await db.flush()
    await store.bind(
        actor,
        variable.id,
        ghost.id,
        credential_field="admin_key",
        approved_origin="https://blog.example.test",
    )
    # Positive control: the legitimate native binding resolves the encrypted key.
    assert (
        await store.resolve_bound(
            actor,
            variable.id,
            ghost.id,
            credential_field="admin_key",
            approved_origin="https://blog.example.test",
        )
        == sentinel
    )
    await db.commit()
    reveals = []
    original_reveal = SecretStore.reveal

    async def observe_reveal(store, workspace_id, secret_id, **kwargs):
        reveals.append(secret_id)
        return await original_reveal(store, workspace_id, secret_id, **kwargs)

    async def no_egress(*args, **kwargs):
        pytest.fail("A negative credential-boundary case attempted network I/O")

    monkeypatch.setattr(SecretStore, "reveal", observe_reveal)
    monkeypatch.setattr(httpx.AsyncClient, "send", no_egress)
    try:
        yield ctx, store, actor, variable, ghost, sentinel, intake, message, team, reveals
    finally:
        get_redactor().clear()


def catalog():
    result = build_default_catalog()
    tool = DiscoveredTool(
        name="inspect",
        slug="inspect",
        derived_risk=RiskLevel.READ,
        input_schema={"type": "object", "properties": {}},
    )
    result.register(build_definition("fixture", tool, {}), make_executor("fixture", tool))
    return result


async def allow(ctx, capability, **scope):
    ctx.session.add(
        AgentCapabilityGrant(
            workspace_id=ctx.workspace_id,
            agent_id=ctx.agent_id,
            capability=capability,
            effect="allow",
            scope_json=scope,
        )
    )
    await ctx.session.flush()


@pytest.mark.parametrize(
    "tool_name,arguments",
    [
        ("http.get", {"path": "/"}),
        ("cli.command.execute", {"command": "true", "network": "none"}),
        ("mcp.fixture.inspect", {"arguments": {}}),
    ],
)
async def test_generic_native_tools_cannot_consume_a_ghost_connection(
    captured, tool_name, arguments
):
    ctx, _, _, _, ghost, _, _, _, _, reveals = captured
    await allow(ctx, tool_name, connection_id=str(ghost.id))
    outcome = await ToolGateway(ctx, catalog()).request(
        tool_name, json.dumps({"connection_id": str(ghost.id), **arguments})
    )
    assert outcome.status in {"denied", "failed"}, outcome
    assert not reveals


@pytest.mark.parametrize(
    "tool_name,arguments",
    [
        ("http.get", {"path": "/"}),
        ("cli.command.execute", {"command": "true", "network": "none"}),
        ("mcp.fixture.inspect", {"arguments": {}}),
    ],
)
async def test_generic_native_schemas_reject_credential_variable_binding(
    captured, tool_name, arguments
):
    ctx, _, _, variable, ghost, _, _, _, _, reveals = captured
    await allow(ctx, tool_name, connection_id=str(ghost.id))
    outcome = await ToolGateway(ctx, catalog()).request(
        tool_name,
        json.dumps({"connection_id": str(ghost.id), **arguments, "variable_id": str(variable.id)}),
    )
    assert outcome.status == "denied" and outcome.decision_code == "invalid_input"
    assert "variable_id: extra_forbidden" in outcome.decision_reason
    assert not reveals


async def test_native_cli_has_no_model_controlled_secret_environment_export(captured):
    ctx, _, _, variable, ghost, _, _, _, _, reveals = captured
    await allow(ctx, "cli.command.execute", connection_id=str(ghost.id))
    outcome = await ToolGateway(ctx, catalog()).request(
        "cli.command.execute",
        json.dumps(
            {
                "connection_id": str(ghost.id),
                "command": "true",
                "network": "none",
                "env": {"GHOST_ADMIN_KEY": {"variable_id": str(variable.id)}},
            }
        ),
    )
    assert outcome.status == "denied" and outcome.decision_code == "invalid_input"
    assert "env: extra_forbidden" in outcome.decision_reason and not reveals


@pytest.mark.parametrize("connector", ["http", "cli", "mcp"])
async def test_opaque_variable_config_does_not_grant_generic_credential_access(captured, connector):
    ctx, store, actor, variable, _, _, _, _, _, reveals = captured
    row = Connection(
        workspace_id=ctx.workspace_id,
        connector_type=connector,
        name="Generic fixture",
        auth_type="api_key",
        config_json={
            "admin_url": "https://blog.example.test",
            "admin_key_variable_id": str(variable.id),
        },
    )
    ctx.session.add(row)
    await ctx.session.flush()
    with pytest.raises(ConnectionResolutionError, match="no stored credential"):
        await resolve_connection(ctx, str(row.id), connector_type=connector)
    with pytest.raises(VariableError, match="not bound"):
        await store.resolve_bound(
            actor,
            variable.id,
            row.id,
            credential_field="admin_key",
            approved_origin="https://blog.example.test",
        )
    assert not reveals


@pytest.mark.parametrize("field", ["token", "access_key", "GHOST_ADMIN_KEY"])
async def test_bound_key_cannot_be_resolved_as_another_credential_field(captured, field):
    _, store, actor, variable, ghost, _, _, _, _, reveals = captured
    with pytest.raises(VariableError, match="not bound"):
        await store.resolve_bound(
            actor,
            variable.id,
            ghost.id,
            credential_field=field,
            approved_origin="https://blog.example.test",
        )
    assert not reveals


@pytest.mark.parametrize("scope", ["agent", "team", "workspace"])
@pytest.mark.parametrize("field", ["content", "subject", "tags"])
async def test_captured_credential_never_becomes_memory_in_any_scope_or_text_field(
    captured, scope, field
):
    ctx, _, _, _, _, sentinel, _, _, team, _ = captured
    await allow(ctx, "memory.propose")
    values = {
        "content": "Prefer concise editorial writing",
        "requested_scope": scope,
        "scope_id": str(
            team.id
            if scope == "team"
            else ctx.workspace_id
            if scope == "workspace"
            else ctx.agent_id
        ),
    }
    values[field] = [sentinel] if field == "tags" else sentinel
    outcome = await ToolGateway(ctx, catalog()).request("memory.propose", json.dumps(values))
    assert (
        outcome.status in {"denied", "failed"}
        or (outcome.sanitized_output or {}).get("outcome") == "reject"
    )
    assert await ctx.session.scalar(select(MemoryRecord)) is None
    for call in await ctx.session.scalars(select(ToolCall)):
        public_call = json.dumps(
            {
                "input": call.sanitized_input_json,
                "output": call.sanitized_output_json,
                "error": call.error_code,
            }
        )
        assert sentinel not in public_call
        assert sentinel[:12] not in public_call and sentinel[-12:] not in public_call
    assert sentinel not in str(outcome)
    assert sentinel[:12] not in str(outcome) and sentinel[-12:] not in str(outcome)


async def test_capture_metadata_gateway_output_model_context_and_log_processor_hide_value(captured):
    ctx, store, _, variable, _, sentinel, intake, message, _, reveals = captured
    await allow(ctx, "variables.read")
    outcome = await ToolGateway(ctx, catalog()).request(
        "variables.get", json.dumps({"variable_id": str(variable.id)})
    )
    assert outcome.status == "executed", outcome
    public = outcome.sanitized_output
    assert public["item"]["configured"] and public["item"]["sensitive"]
    assert not ({"value", "secret_id", "masked_hint", "ciphertext"} & public["item"].keys())
    assert sentinel not in intake.text and sentinel not in json.dumps(
        store.public(variable), default=str
    )
    secret = await ctx.session.get(Secret, variable.secret_id)
    assert secret.masked_hint == "" and sentinel.encode() not in secret.ciphertext
    assert (await ctx.session.get(ScopedVariable, variable.id)).plaintext is None
    assert sentinel not in json.dumps(message.content_json)
    assert not reveals

    snapshot = AgentExecutionSnapshot(
        agent_id=ctx.agent_id,
        workspace_id=ctx.workspace_id,
        name="Writer",
        role_title="Writer",
        system_prompt="Help with editorial work.",
        autonomy_level="supervised",
        team_id=None,
        team_name=None,
        manager_agent_id=None,
        manager_name=None,
        temperature=None,
        max_output_tokens=32,
        run_limits=RunLimits(max_steps=3, max_run_minutes=5),
        model_profile=ModelProfileSnapshot(
            profile_id=new_uuid7(),
            provider_id=new_uuid7(),
            provider_type="openai_compatible",
            base_url="https://fixture.invalid/v1",
            secret_id=None,
            model_name="fixture",
            display_name="Fixture",
            input_cost_micros_per_million=None,
            output_cost_micros_per_million=None,
        ),
    )
    messages = build_messages(
        snapshot,
        TaskContext(
            title="Ghost setup",
            description="",
            history=(
                ConversationTurn(role="user", text=message.content_json["text"]),
                ConversationTurn(
                    role="agent",
                    text=json.dumps(public),
                    kind="tool_result",
                    tool_call_id=str(outcome.tool_call_id),
                    tool_name="variables.get",
                ),
            ),
        ),
    )
    client = OpenAICompatibleClient(base_url="https://fixture.invalid/v1")
    try:
        wire = client._payload(ModelRequest(model="fixture", messages=messages), stream=True)
    finally:
        await client.close()
    assert sentinel not in json.dumps(wire) and str(variable.id) in json.dumps(wire)
    event = redact_event_dict(
        None,
        "info",
        {
            "event": "credential fixture",
            "detail": sentinel,
            "nested": {"value": f"prefix {sentinel} suffix"},
        },
    )
    assert sentinel not in json.dumps(event) and "[REDACTED]" in json.dumps(event)
    for audit in await ctx.session.scalars(select(AuditEvent)):
        assert sentinel not in json.dumps(audit.metadata_json)
