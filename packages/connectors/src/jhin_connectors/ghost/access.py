"""Connection permissions for explicitly bound variables and manual app grants."""

from collections.abc import Sequence
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import select

from jhin_db.models import Connection
from jhin_db.models.variables import VariableConnectionBinding
from jhin_policy import (
    DecisionType,
    Grant,
    GrantEffect,
    PolicyDecision,
    capability_matches,
    scope_matches,
)
from jhin_secrets.variables import (
    VariableActor,
    VariableError,
    VariableStore,
    canonical_admin_url,
    origin,
)
from jhin_tools.builtin import ToolExecutionContext, ToolValidator


async def variable_audience(ctx: ToolExecutionContext, connection: Connection) -> bool:
    try:
        variable_id = UUID(str(connection.config_json.get("admin_key_variable_id", "")))
        row = await VariableStore(ctx.session).get(
            VariableActor(ctx.workspace_id, "agent", ctx.agent_id), variable_id
        )
        approved_origin = origin(str(connection.config_json.get("admin_url", "")))
        approved_url = canonical_admin_url(str(connection.config_json.get("admin_url", "")))
    except (ValueError, VariableError):
        return False
    return bool(
        row.sensitive
        and row.secret_id
        and await ctx.session.scalar(
            select(VariableConnectionBinding.id).where(
                VariableConnectionBinding.workspace_id == ctx.workspace_id,
                VariableConnectionBinding.variable_id == variable_id,
                VariableConnectionBinding.connection_id == connection.id,
                VariableConnectionBinding.credential_field == "admin_key",
                VariableConnectionBinding.approved_origin == approved_origin,
                VariableConnectionBinding.approved_admin_url == approved_url,
            )
        )
    )


async def ghost_access_allowed(
    ctx: ToolExecutionContext,
    connection: Connection,
    tool_name: str,
    grants: Sequence[Grant],
    *,
    audience: bool | None = None,
) -> bool:
    """Scoped denies win. Default audience grants cannot use unbound app keys."""
    accessible = await variable_audience(ctx, connection) if audience is None else audience
    if connection.config_json.get("admin_key_variable_id") and not accessible:
        return False
    accepted = False
    for grant in grants:
        if not capability_matches(grant.capability, tool_name):
            continue
        scope = dict(grant.scope)
        audience = scope.pop("variable_audience", None)
        if audience is not None and (audience is not True or not accessible):
            continue
        if set(scope) - {"connection_id"} or not scope_matches(
            scope, {"connection_id": str(connection.id)}
        ):
            continue
        if grant.effect == GrantEffect.DENY:
            return False
        if grant.effect == GrantEffect.ALLOW:
            accepted = True
    if tool_name in {"ghost.review.decide", "ghost.post.publish"}:
        accepted = accepted and str(connection.config_json.get("publisher_agent_id", "")) == str(
            ctx.agent_id
        )
    return accepted


def validator_for(tool_name: str) -> ToolValidator:
    async def validate(
        ctx: ToolExecutionContext, payload: BaseModel, grants: Sequence[Grant]
    ) -> PolicyDecision | None:
        try:
            connection_id = UUID(str(getattr(payload, "connection_id", "")))
        except ValueError:
            connection_id = None
        connection = await ctx.session.scalar(
            select(Connection).where(
                Connection.id == connection_id,
                Connection.workspace_id == ctx.workspace_id,
                Connection.connector_type == "ghost",
                Connection.status == "active",
            )
        )
        if connection is None or not await ghost_access_allowed(ctx, connection, tool_name, grants):
            return PolicyDecision(
                decision=DecisionType.DENY,
                code="ghost_access_denied",
                reason=(
                    "This Ghost operation requires current variable audience access or "
                    "an app grant; publishing also requires the designated director."
                ),
            )
        return None

    return validate
