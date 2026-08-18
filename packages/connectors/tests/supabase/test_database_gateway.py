"""Real ToolGateway boundaries for Supabase database approval inputs."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_connectors.supabase.connector import SupabaseConnector
from jhin_connectors.supabase.schemas import (
    DatabaseMutationInput,
    DatabaseMutationOutput,
)
from jhin_db.models import (
    Agent,
    AgentCapabilityGrant,
    Approval,
    AuditEvent,
    Connection,
    ToolCall,
    Workspace,
)
from jhin_domain import ApprovalStatus, ToolCallStatus
from jhin_secrets import get_redactor
from jhin_tools.builtin import ToolCatalog, ToolExecutionContext
from jhin_tools.gateway import ToolGateway

PROJECT_REF = "abcdefghijklmnopqrst"


async def _setup_gateway(
    *,
    session: AsyncSession,
    context: ToolExecutionContext,
    workspace: Workspace,
    make_connection: Any,
    effects: list[dict[str, object]],
) -> tuple[ToolGateway, Connection]:
    session.add(
        Agent(
            id=context.agent_id,
            workspace_id=context.workspace_id,
            name=context.agent_name,
            slug="database-approval-agent",
        )
    )
    connection = cast(
        Connection,
        await make_connection(
            workspace,
            connector_type="supabase",
            name="Approval-bound database",
            auth_type="postgres",
            credentials={
                "database_url": (
                    "postgresql://jhin_writer:gateway-password-marker@127.0.0.1:65433/fixture"
                )
            },
            config={
                "project_ref": PROJECT_REF,
                "allowed_schemas": ["public"],
                "allow_writes": True,
            },
        ),
    )
    session.add(
        AgentCapabilityGrant(
            workspace_id=context.workspace_id,
            agent_id=context.agent_id,
            capability="supabase.database.destructive",
            scope_json={
                "connection_id": str(connection.id),
                "project_ref": PROJECT_REF,
                "schema": "public",
            },
            effect="allow",
        )
    )
    await session.flush()

    async def recording_executor(
        _ctx: ToolExecutionContext,
        payload: BaseModel,
    ) -> DatabaseMutationOutput:
        data = cast(DatabaseMutationInput, payload)
        effects.append(data.model_dump(mode="json"))
        return DatabaseMutationOutput(affected_rows=1)

    definition = next(
        definition
        for definition, _executor in SupabaseConnector().tools()
        if definition.name == "supabase.database.destructive"
    )
    catalog = ToolCatalog()
    catalog.register(definition, recording_executor)
    return ToolGateway(context, catalog), connection


async def test_database_approval_preserves_complete_input_digest_and_replay(
    session: AsyncSession,
    context: ToolExecutionContext,
    workspace: Workspace,
    make_connection: Any,
) -> None:
    effects: list[dict[str, object]] = []
    gateway, connection = await _setup_gateway(
        session=session,
        context=context,
        workspace=workspace,
        make_connection=make_connection,
        effects=effects,
    )
    sql = "UPDATE public.widgets\nSET name = $1\nWHERE id = $2 /* preserve SQL bytes exactly */"
    exact_parameter_boundary = ["é" * 4_096, "z" * 7_801]
    assert (
        len(
            json.dumps(
                exact_parameter_boundary,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        == 16_000
    )
    expected: dict[str, object] = {
        "connection_id": str(connection.id),
        "project_ref": PROJECT_REF,
        "schema": "public",
        "sql": sql,
        "params": exact_parameter_boundary,
    }

    parked = await gateway.request(
        "supabase.database.destructive",
        json.dumps(expected, ensure_ascii=False),
    )

    assert parked.status == "needs_approval"
    assert parked.sanitized_input == expected
    assert effects == []
    approval = await session.get(Approval, parked.approval_id)
    assert approval is not None
    action = approval.action_payload_sanitized
    assert action["input"] == expected
    digest = action.get("connection_authorization_digest")
    assert isinstance(digest, str) and len(digest) == 64
    assert "gateway-password-marker" not in json.dumps(action)
    tool_call = await session.get(ToolCall, parked.tool_call_id)
    assert tool_call is not None
    assert tool_call.sanitized_input_json == expected

    approval.status = ApprovalStatus.APPROVED.value
    approval.decided_at = datetime.now(UTC)
    await session.flush()
    executed = await gateway.resolve_approved(approval.id)
    replayed = await gateway.resolve_approved(approval.id)

    assert executed.status == "executed"
    assert replayed.status == "executed"
    assert replayed.replayed is True
    assert effects == [expected]


async def test_database_redaction_hit_is_denied_before_approval_claim_or_effect(
    session: AsyncSession,
    context: ToolExecutionContext,
    workspace: Workspace,
    make_connection: Any,
) -> None:
    effects: list[dict[str, object]] = []
    gateway, connection = await _setup_gateway(
        session=session,
        context=context,
        workspace=workspace,
        make_connection=make_connection,
        effects=effects,
    )
    marker = "registered-database-input-secret"
    expected = {
        "connection_id": str(connection.id),
        "project_ref": PROJECT_REF,
        "schema": "public",
        "sql": "DELETE FROM public.widgets WHERE name = $1",
        "params": [marker],
    }
    get_redactor().register(marker)
    try:
        outcome = await gateway.request(
            "supabase.database.destructive",
            json.dumps(expected),
        )
    finally:
        get_redactor().clear()

    assert outcome.status == "denied"
    assert outcome.decision_code == "approval_input_not_lossless"
    assert effects == []
    assert (await session.scalars(select(Approval))).all() == []
    tool_call = await session.get(ToolCall, outcome.tool_call_id)
    assert tool_call is not None
    assert tool_call.status == ToolCallStatus.DENIED.value
    actions = list(await session.scalars(select(AuditEvent.action)))
    assert "tool.call.claimed" not in actions


@pytest.mark.parametrize(
    ("params", "boundary"),
    [
        (["x" * 8_193], "leaf"),
        (["x" * 8_000, "y" * 8_000], "document"),
    ],
)
async def test_oversized_database_params_are_denied_before_approval_or_claim(
    session: AsyncSession,
    context: ToolExecutionContext,
    workspace: Workspace,
    make_connection: Any,
    params: list[str],
    boundary: str,
) -> None:
    effects: list[dict[str, object]] = []
    gateway, connection = await _setup_gateway(
        session=session,
        context=context,
        workspace=workspace,
        make_connection=make_connection,
        effects=effects,
    )
    compact_bytes = len(
        json.dumps(params, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )
    if boundary == "leaf":
        assert len(params[0].encode("utf-8")) == 8_193
        assert compact_bytes < 16_000
    else:
        assert all(len(value.encode("utf-8")) <= 8_192 for value in params)
        assert compact_bytes > 16_000

    outcome = await gateway.request(
        "supabase.database.destructive",
        json.dumps(
            {
                "connection_id": str(connection.id),
                "project_ref": PROJECT_REF,
                "schema": "public",
                "sql": "DELETE FROM public.widgets WHERE name = $1 OR name = $2",
                "params": params,
            }
        ),
    )

    assert outcome.status == "denied"
    assert outcome.decision_code == "invalid_input"
    assert effects == []
    assert (await session.scalars(select(Approval))).all() == []
    tool_call = await session.get(ToolCall, outcome.tool_call_id)
    assert tool_call is not None
    assert tool_call.status == ToolCallStatus.DENIED.value
    actions = list(await session.scalars(select(AuditEvent.action)))
    assert "tool.call.claimed" not in actions
