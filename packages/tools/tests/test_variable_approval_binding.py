"""Parked Ghost approvals must recheck secret revisions and current audience."""

import json
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from pydantic import BaseModel
from sqlalchemy import select

from jhin_connectors.ghost.access import validator_for
from jhin_connectors.ghost.tools import GHOST_TOOLS, _api
from jhin_db.models import (
    Agent,
    AgentCapabilityGrant,
    AgentRun,
    AgentTeamMembership,
    Approval,
    Connection,
    Task,
    Team,
    ToolCall,
    User,
)
from jhin_domain import new_uuid7
from jhin_policy import RiskLevel
from jhin_secrets import MasterKey, SecretCrypto
from jhin_secrets.variables import VariableActor, VariableStore
from jhin_tools.builtin import ToolCatalog
from jhin_tools.gateway import ToolGateway

KEY = "45" * 12 + ":" + "ef" * 32


class ProbeOut(BaseModel):
    executed: bool


@pytest.mark.parametrize("drift", ["unchanged", "rotation", "deletion", "membership"])
async def test_parked_approval_revalidates_bound_variable_and_default_audience(context, drift):
    db = context.session
    crypto = SecretCrypto(MasterKey(key=b"v" * 32))
    ctx = replace(context, crypto=crypto)
    user = User(email="approval-owner@example.test", display_name="Owner", password_hash="x")
    team = Team(workspace_id=ctx.workspace_id, name="Marketing")
    db.add_all([user, team])
    await db.flush()
    (await db.get(Agent, ctx.agent_id)).team_id = team.id
    membership = AgentTeamMembership(
        workspace_id=ctx.workspace_id, agent_id=ctx.agent_id, team_id=team.id, is_primary=True
    )
    db.add_all(
        [
            membership,
            Task(
                id=ctx.task_id,
                workspace_id=ctx.workspace_id,
                title="Check Ghost",
                assigned_agent_id=ctx.agent_id,
                correlation_id=new_uuid7(),
            ),
            AgentRun(
                id=ctx.run_id,
                workspace_id=ctx.workspace_id,
                task_id=ctx.task_id,
                agent_id=ctx.agent_id,
            ),
            AgentCapabilityGrant(
                workspace_id=ctx.workspace_id,
                agent_id=ctx.agent_id,
                capability="ghost.post.read",
                effect="allow",
                scope_json={"variable_audience": True},
            ),
        ]
    )
    await db.flush()
    store = VariableStore(db, crypto)
    owner = VariableActor(ctx.workspace_id, "user", user.id, is_admin=True)
    variable = await store.set(
        owner, name="ghost.key", scope="team", scope_id=team.id, sensitive=True, value=KEY
    )
    connection = Connection(
        workspace_id=ctx.workspace_id,
        name="Blog",
        connector_type="ghost",
        auth_type="api_key",
        config_json={
            "admin_url": "https://blog.example.test",
            "admin_key_variable_id": str(variable.id),
        },
    )
    db.add(connection)
    await db.flush()
    await store.bind(
        VariableActor(ctx.workspace_id, "agent", ctx.agent_id),
        variable.id,
        connection.id,
        credential_field="admin_key",
        approved_origin="https://blog.example.test",
    )
    seen = []

    async def execute(executor_ctx, payload):
        _connection, _base, key = await _api(executor_ctx, payload.connection_id)
        assert key == KEY
        seen.append("executed")
        return ProbeOut(executed=True)

    actual = next(
        definition for definition, _ in GHOST_TOOLS if definition.name == "ghost.post.read"
    )
    # The admin's policy may require review even for a read. Preserve the real
    # provider scope contract and validator while making this review explicit.
    definition = actual.model_copy(
        update={"risk": RiskLevel.ELEVATED, "supports_approval": True, "output_model": ProbeOut}
    )
    catalog = ToolCatalog()
    catalog.register(definition, execute, validator=validator_for(definition.name))
    gateway = ToolGateway(ctx, catalog)
    parked = await gateway.request(
        definition.name, json.dumps({"connection_id": str(connection.id), "post_id": "ab" * 12})
    )
    assert parked.status == "needs_approval", parked.decision_reason
    assert parked.approval_id is not None and seen == []
    approval = await db.get(Approval, parked.approval_id)
    assert len(approval.action_payload_sanitized["connection_authorization_digest"]) == 64
    assert KEY not in json.dumps(approval.action_payload_sanitized)
    if drift == "rotation":
        await store.set(
            owner, variable_id=variable.id, expected_version=1, value="67" * 12 + ":" + "ab" * 32
        )
    elif drift == "deletion":
        await store.delete(owner, variable.id, expected_version=1)
    elif drift == "membership":
        membership.left_at = datetime.now(UTC)
    approval.status = "approved"
    approval.decided_at = datetime.now(UTC)
    await db.commit()
    outcome = await gateway.resolve_approved(parked.approval_id)
    if drift == "unchanged":
        assert outcome.status == "executed" and seen == ["executed"]
    else:
        assert outcome.status == "denied" and seen == []
        assert outcome.decision_code == (
            "ghost_access_denied" if drift == "membership" else "approval_connection_changed"
        )
    repeated = await gateway.resolve_approved(parked.approval_id)
    assert repeated.status == outcome.status
    assert len(seen) == (1 if drift == "unchanged" else 0)
    for row in await db.scalars(select(ToolCall)):
        assert KEY not in json.dumps(row.sanitized_output_json, default=str)
