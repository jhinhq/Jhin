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
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_connectors.cli.workspace import readable_binding, workspace_repositories
from jhin_connectors.execution import ConnectionResolutionError
from jhin_policy import (
    DecisionType,
    Grant,
    GrantEffect,
    PolicyDecision,
    capability_matches,
    scope_matches,
)

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


async def workspace_repository_validator(
    ctx: ToolExecutionContext,
    payload: BaseModel,
    grants: Sequence[Grant],
) -> PolicyDecision | None:
    """Deny any sandbox call whose workspace holds a repository the connection
    no longer allows.

    The allow-list used to be bound to the two tools that *name* a repository,
    which was enough while every workspace died with its run: a checkout that
    the list refused simply never happened, and there was nothing on disk
    afterwards. A durable workspace breaks that. The clone an allowed checkout
    made yesterday is still in ``/workspace/repo`` today, and
    ``cli.file.read``, ``cli.file.search``, ``cli.test.run`` and
    ``cli.command.execute`` name no repository at all -- so narrowing the
    connection's list stopped new checkouts of a repository while leaving the
    old one readable, indefinitely, to every tool that does not have to ask
    for it by name.

    So the question these tools are asked is the one they can actually answer:
    not "may this call touch that repository" but "may this connection still
    reach whatever is on this disk".

    **The disk, not the last record.** Asking only what the *most recent*
    checkout named made the answer follow a record instead of the tree it
    described, and the tree outlived the record three ways: a copy taken
    anywhere but ``/workspace/repo`` survived every later checkout, because
    the reuse prologue removes that one path; ``HOME`` is ``/workspace``, so
    pip and npm leave a private repository's packages in ``/workspace/.cache``
    with nobody meaning anything by it; and checking out an allowed repository
    -- the remedy this denial prints -- moved the record forward and turned the
    answer back to "allowed" while the forbidden tree sat there readable. So
    the question is put to :func:`workspace_repositories`, which is every
    repository recorded on this disk since the disk was last actually emptied,
    and the remedy had to become one that empties it: a checkout onto a
    workspace whose history is not fully allowed now wipes the whole workspace
    before it clones, rather than just the repository path.

    **What this is not.** It is not an egress control. ``cli.command.execute``
    with ``network: "internet"`` can clone anything it likes, and no record
    will name it; an allow-list over Jhin's own repository operations cannot
    bound what an arbitrary command does with a network, any more than it can
    stop that command reading a file and printing it. The control for that is
    the connection's ``default_network``, the grant scope over ``network``,
    and the sandbox bridge itself. This validator bounds Jhin's credential and
    Jhin's disks, which is the boundary it can actually hold.

    Denied rather than deleted, and denied rather than raised: a policy denial
    is a decision the agent reads and the run survives.
    """
    from jhin_connectors.cli.tools import _load_cli_connection, allowed_repositories

    connection_id = str(getattr(payload, "connection_id", "") or "")
    if not connection_id:
        return None
    try:
        connection = await _load_cli_connection(ctx, connection_id)
    except ConnectionResolutionError as exc:
        return PolicyDecision(
            decision=DecisionType.DENY,
            code="sandbox_connection_unavailable",
            reason=str(exc),
        )

    row_id = await readable_binding(
        ctx.session,
        workspace_id=ctx.workspace_id,
        agent_id=ctx.agent_id,
        run_id=ctx.run_id,
    )
    if row_id is None:
        # This agent has no durable workspace at all, so the next bind makes
        # one, and a fresh disk holds nothing.
        return None
    allowed = allowed_repositories(connection)
    forbidden = await forbidden_repositories_on_disk(
        ctx.session, workspace_id=ctx.workspace_id, row_id=row_id, allowed=allowed
    )
    if not forbidden:
        return None
    return PolicyDecision(
        decision=DecisionType.DENY,
        code="repository_not_allowed",
        reason=(
            f"this sandbox workspace holds {', '.join(forbidden)}, which connection "
            f"'{connection.name}' no longer allows"
            + (f" (allowed: {', '.join(sorted(allowed))})" if allowed else "")
            + ". Check out a repository this connection allows: that empties the "
            "whole workspace first, so the files that are no longer allowed go "
            "with it. An operator can do the same with "
            "`jhin-admin agent workspace reset`."
        ),
    )


async def command_network_validator(
    ctx: ToolExecutionContext,
    payload: BaseModel,
    grants: Sequence[Grant],
) -> PolicyDecision | None:
    """Own command scope matching after authorized connection defaults resolve.

    Internet is an allow ceiling that includes an isolated command. Denies
    remain exact: Internet Off must not prevent an explicitly offline command.
    All other dimensions still need to match the same grant. This validator
    is mandatory for this deferred-scope tool, including approval/review resume.
    """
    from jhin_connectors.cli.tools import _connection_defaults, _load_cli_connection

    veto = await workspace_repository_validator(ctx, payload, grants)
    if veto is not None:
        return veto
    relevant = [
        grant for grant in grants if capability_matches(grant.capability, "cli.command.execute")
    ]
    raw = payload.model_dump(mode="json")
    requested = dict(raw)
    try:
        connection = await _load_cli_connection(ctx, str(requested.get("connection_id", "")))
    except ConnectionResolutionError as exc:
        return PolicyDecision(
            decision=DecisionType.DENY,
            code="sandbox_connection_unavailable",
            reason=str(exc),
        )
    default_image, default_network, _ = _connection_defaults(connection)
    requested["network"] = requested.get("network") or default_network
    requested["image"] = requested.get("image") or default_image
    denied = next(
        (
            grant
            for grant in relevant
            if grant.effect is GrantEffect.DENY
            and (scope_matches(grant.scope, requested) or scope_matches(grant.scope, raw))
        ),
        None,
    )
    if denied is not None:
        return PolicyDecision(
            decision=DecisionType.DENY,
            code="terminal_internet_denied" if "network" in denied.scope else "explicit_deny",
            reason=(
                "This terminal command is blocked by an explicit permission restriction "
                f"(resolved network: {requested['network']})."
            ),
        )
    for grant in relevant:
        if grant.effect is not GrantEffect.ALLOW:
            continue
        if scope_matches(grant.scope, requested):
            return None
        network = grant.scope.get("network")
        permits_internet = network == "internet" or (
            isinstance(network, list) and "internet" in network
        )
        if (
            requested["network"] == "none"
            and permits_internet
            and scope_matches(grant.scope, {**requested, "network": "internet"})
        ):
            return None
    return PolicyDecision(
        decision=DecisionType.DENY,
        code="scope_mismatch",
        reason="No terminal grant covers this connection, command, image and resolved network.",
    )


async def forbidden_repositories_on_disk(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    row_id: UUID,
    allowed: Sequence[str],
) -> tuple[str, ...]:
    """Which repositories on this disk the connection no longer allows.

    Shared by the validator, which denies on a non-empty answer, and by
    ``cli.repository.checkout``, which purges the workspace on one -- the two
    have to agree about what "this disk is no longer clean" means, or the
    remedy the denial prints does not clear the denial.
    """
    on_disk = await workspace_repositories(session, workspace_id=workspace_id, row_id=row_id)
    return tuple(
        repository
        for repository in on_disk
        if not any(repository_matches(pattern, repository) for pattern in allowed)
    )


__all__ = [
    "command_network_validator",
    "forbidden_repositories_on_disk",
    "is_plain_repository",
    "repository_allow_list_validator",
    "repository_matches",
    "workspace_repository_validator",
]
