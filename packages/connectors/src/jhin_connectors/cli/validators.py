"""The repository allow-list, as a :class:`ToolValidator` (plan 7.5).

A grant's ``repository`` scope is per agent. This is the other half: a
per-connection statement of which repositories *this instance* may touch at
all, so an operator has one place to answer "what can these agents reach"
without auditing every agent's grants.

It is deny-by-default. A CLI connection with no ``allowed_repositories`` does
no repository work, and the denial names the allowed set so the model
self-corrects in one step instead of retrying blind.

The gateway runs validators at three points — policy decision, approval
resume, and execution bind — so narrowing the list invalidates an approval
that is already parked, exactly as rotating the connection's credential does.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import cast

from pydantic import BaseModel

from jhin_connectors.execution import ConnectionResolutionError
from jhin_policy import DecisionType, Grant, PolicyDecision

# ``is_plain_repository`` and ``repository_matches`` live in
# ``jhin_policy.repositories`` now, so the grant writers and the bundle
# planner apply the same definition of a name as this validator; they are
# re-exported here (see ``__all__``) for the callers that import them from
# the connector.
from jhin_policy.repositories import is_plain_repository, repository_matches
from jhin_tools.builtin import ToolExecutionContext


class _RepositoryCall(BaseModel):
    connection_id: str
    repository: str


async def repository_allow_list_validator(
    ctx: ToolExecutionContext,
    payload: BaseModel,
    grants: Sequence[Grant],
) -> PolicyDecision | None:
    """Deny a checkout or push whose repository the connection does not allow."""
    from jhin_connectors.cli.tools import _load_cli_connection, allowed_repositories

    data = cast(_RepositoryCall, payload)
    repository = getattr(data, "repository", "")
    connection_id = getattr(data, "connection_id", "")
    if not repository:
        return None

    try:
        connection = await _load_cli_connection(ctx, connection_id)
    except ConnectionResolutionError as exc:
        return PolicyDecision(
            decision=DecisionType.DENY,
            code="sandbox_connection_unavailable",
            reason=str(exc),
        )

    allowed = allowed_repositories(connection)
    if not allowed:
        return PolicyDecision(
            decision=DecisionType.DENY,
            code="repository_not_allowed",
            reason=(
                f"connection '{connection.name}' allows no repositories; an "
                "admin must list them in the connection's settings"
            ),
        )
    if any(repository_matches(pattern, repository) for pattern in allowed):
        return None
    return PolicyDecision(
        decision=DecisionType.DENY,
        code="repository_not_allowed",
        reason=(f"connection '{connection.name}' allows only: " + ", ".join(sorted(allowed))),
    )


__all__ = ["is_plain_repository", "repository_allow_list_validator", "repository_matches"]
