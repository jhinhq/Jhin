"""Restate existing grants in the scope dimensions their tools now require.

Three tools started requiring scope keys a grant written before this release
had no reason to carry: ``cli.repository.checkout`` and ``cli.repository.push``
now require ``connection_id`` (and push, ``branch``), and
``github.pull_request.create`` now requires ``base``. The evaluator denies with
``required_scope_missing`` when *no* allow grant carries all of them, so
without this migration a hand-made grant that worked yesterday stops working —
and stops working on the agent's next run rather than in front of the operator
who could fix it.

The rule this migration follows, and the reason it is safe to run unattended:

    **it never changes what an agent may do; it only writes down what the
    grant already meant.**

So an absent ``repository``/``branch``/``base`` — which authorised *any* value,
because ``scope_matches`` only checks the keys a grant constrains — is restated
as the explicit ``"*"`` it already was. That is deliberately not a security
improvement: narrowing someone's grants is a decision for a person, and the
release note (docs/operations/grant-scope-migration.md) asks for exactly that
review. What the new required-key contract does buy is that every grant written
from here on — the agent wizard's included — must state these dimensions, so
the breadth can never be invisible again.

``connection_id`` is the one key that cannot be restated as ``"*"``: it names a
row, and a wildcard there would mean "any connection in the workspace", which
is wider than the tool's own validator allows. It is filled in only where the
answer is unambiguous — the workspace has exactly one connection of the right
connector type — which is a narrowing, from "whichever connection the call
named" to "this one". A workspace with none, or with several, is left alone:
those grants now fail with a denial that names ``connection_id``, which is the
loud failure F6 asked for in place of a silent one.

A grant does not have to *name* one of these capabilities to authorise it.
``capability`` is a pattern — ``cli.*`` and ``*`` match ``cli.repository.push``
the same way an exact name does — so a wildcard grant lost exactly as much
authority as an exact one and would have been left out of a backfill that
selected exact strings. It cannot be restated in place, though: adding
``repository`` to a ``cli.*`` grant would constrain every *other* cli tool the
same grant covers, and ``cli.command.execute`` calls carry no ``repository``
at all — ``scope_matches`` would then deny them. So the wildcard grant is left
untouched and the authority it is about to lose is written down beside it, as
one exact-capability grant per affected capability, carrying that grant's own
scope plus the restated dimensions. The new row says out loud what the wildcard
row silently allowed, which is the same trade the rest of this migration makes.

Downgrade removes the values this migration would have written, and the rows it
would have inserted, and only where they still look untouched.

Revision ID: 0039
Revises: 0038
Create Date: 2026-09-03
"""

import json
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

from jhin_domain import new_uuid7

revision: str = "0039"
down_revision: str | None = "0038"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSON_DICT = sa.JSON().with_variant(JSONB(), "postgresql")

_GRANT = sa.table(
    "agent_capability_grant",
    sa.column("id", sa.Uuid(as_uuid=True)),
    sa.column("workspace_id", sa.Uuid(as_uuid=True)),
    sa.column("agent_id", sa.Uuid(as_uuid=True)),
    sa.column("capability", sa.String),
    sa.column("scope_json", _JSON_DICT),
    sa.column("effect", sa.String),
    sa.column("created_at", sa.DateTime(timezone=True)),
    sa.column("updated_at", sa.DateTime(timezone=True)),
)

_CONNECTION = sa.table(
    "connection",
    sa.column("id", sa.Uuid(as_uuid=True)),
    sa.column("workspace_id", sa.Uuid(as_uuid=True)),
    sa.column("connector_type", sa.String),
    sa.column("status", sa.String),
)

ANY_VALUE = "*"

# capability -> (connector type that owns its connection_id, keys restated as "*")
AFFECTED: dict[str, tuple[str, tuple[str, ...]]] = {
    "cli.repository.checkout": ("cli", ("repository",)),
    "cli.repository.push": ("cli", ("repository", "branch")),
    "github.pull_request.create": ("github", ("repository", "base")),
}


def restated(capability: str, scope: dict[str, Any], sole_connection: str | None) -> dict[str, Any]:
    """The same authority, written down. Pure so the decision can be read and
    tested without a database."""
    _connector, wildcard_keys = AFFECTED[capability]
    updated = dict(scope)
    for key in wildcard_keys:
        updated.setdefault(key, ANY_VALUE)
    if "connection_id" not in updated and sole_connection is not None:
        updated["connection_id"] = sole_connection
    return updated


def covers(pattern: str, capability: str) -> bool:
    """Does a grant's ``capability`` pattern authorise this capability?

    The evaluator's rule (``jhin_policy.capability_matches``), spelled out
    here rather than imported: a migration has to keep meaning what it meant
    on the day it ran, even after the matcher changes.
    """
    if pattern == "*":
        return True
    if pattern.endswith(".*"):
        return capability.startswith(pattern[:-1])
    return pattern == capability


def derived(
    pattern: str,
    scope: dict[str, Any],
    sole_connections: Mapping[str, str | None],
    connector_of: Mapping[str, str],
) -> list[tuple[str, dict[str, Any]]]:
    """The exact-capability grants a *wildcard* grant needs so that it keeps
    authorising what it authorised.

    One per affected capability the pattern covers, carrying the wildcard
    grant's own scope plus the restated dimensions. Empty for a grant that
    names an affected capability outright (that one is restated in place), and
    empty for a capability whose ``connection_id`` cannot be answered for —
    there the loud ``required_scope_missing`` denial is the right outcome, and
    a row that still lacks a required key would only add noise to it.

    A wildcard grant that names a connection reaches only that connection's own
    tools, and ``connector_of`` is how this knows which those are. ``*`` scoped
    to a CLI connection authorised no ``github.pull_request.create`` call
    yesterday — ``scope_matches`` compares the ``connection_id`` the call
    carries against the one the grant names, and a GitHub call carries a GitHub
    connection — so no row is written for it. Writing one that carried the CLI
    id would say nothing (it can never match); writing one that carried the
    workspace's GitHub connection would say something new, which is the one
    thing this migration must not do.
    """
    if pattern in AFFECTED:
        return []
    named = str(scope.get("connection_id") or "")
    rows: list[tuple[str, dict[str, Any]]] = []
    for capability, (connector, _keys) in AFFECTED.items():
        if not covers(pattern, capability):
            continue
        if named and connector_of.get(named) != connector:
            continue
        updated = restated(capability, scope, sole_connections.get(connector))
        if "connection_id" not in updated:
            continue
        rows.append((capability, updated))
    return rows


def undone(capability: str, scope: dict[str, Any], sole_connection: str | None) -> dict[str, Any]:
    """The inverse, applied only to values still equal to what upgrade writes."""
    _connector, wildcard_keys = AFFECTED[capability]
    updated = dict(scope)
    for key in wildcard_keys:
        if updated.get(key) == ANY_VALUE:
            updated.pop(key)
    if sole_connection is not None and updated.get("connection_id") == sole_connection:
        updated.pop("connection_id")
    return updated


def _sole_connection(bind: sa.engine.Connection, workspace_id: UUID, connector: str) -> str | None:
    rows = bind.execute(
        sa.select(_CONNECTION.c.id).where(
            _CONNECTION.c.workspace_id == workspace_id,
            _CONNECTION.c.connector_type == connector,
        )
    ).all()
    return str(rows[0][0]) if len(rows) == 1 else None


def _connector_of(bind: sa.engine.Connection, workspace_id: UUID) -> dict[str, str]:
    """connection id -> connector type, for one workspace. A grant naming a
    connection that no longer exists is absent from this map, which reads the
    same way as naming the wrong connector: no derived row."""
    rows = bind.execute(
        sa.select(_CONNECTION.c.id, _CONNECTION.c.connector_type).where(
            _CONNECTION.c.workspace_id == workspace_id
        )
    ).all()
    return {str(row[0]): str(row[1]) for row in rows}


def _affected_grants() -> list[tuple[Any, Any, str, dict[str, Any]]]:
    bind = op.get_bind()
    rows = bind.execute(
        sa.select(
            _GRANT.c.id,
            _GRANT.c.workspace_id,
            _GRANT.c.capability,
            _GRANT.c.scope_json,
        ).where(
            _GRANT.c.capability.in_(sorted(AFFECTED)),
            _GRANT.c.effect == "allow",
        )
    ).all()
    return [(row[0], row[1], row[2], dict(row[3] or {})) for row in rows]


def _apply(rewrite: Callable[[str, dict[str, Any], str | None], dict[str, Any]]) -> None:
    bind = op.get_bind()
    for grant_id, workspace_id, capability, scope in _affected_grants():
        connector, _keys = AFFECTED[capability]
        updated = rewrite(capability, scope, _sole_connection(bind, workspace_id, connector))
        if updated == scope:
            continue
        bind.execute(_GRANT.update().where(_GRANT.c.id == grant_id).values(scope_json=updated))


def _wildcard_grants() -> list[tuple[Any, Any, Any, str, dict[str, Any]]]:
    """Allow grants whose capability is a pattern rather than a name. The
    ``LIKE`` is the cheap half; :func:`covers` decides."""
    bind = op.get_bind()
    rows = bind.execute(
        sa.select(
            _GRANT.c.id,
            _GRANT.c.workspace_id,
            _GRANT.c.agent_id,
            _GRANT.c.capability,
            _GRANT.c.scope_json,
        ).where(
            _GRANT.c.capability.like("%*"),
            _GRANT.c.capability.notin_(sorted(AFFECTED)),
            _GRANT.c.effect == "allow",
        )
    ).all()
    return [(row[0], row[1], row[2], row[3], dict(row[4] or {})) for row in rows]


def _derived_rows() -> list[tuple[Any, Any, str, dict[str, Any]]]:
    """(workspace_id, agent_id, capability, scope) for every grant a wildcard
    grant needs beside it. Deduplicated, and never for a capability the agent
    already has a grant of its own for — an existing row is somebody's
    decision, in either effect, and this migration does not second-guess it.
    """
    bind = op.get_bind()
    connectors = {connector for connector, _keys in AFFECTED.values()}
    rows: list[tuple[Any, Any, str, dict[str, Any]]] = []
    seen: set[tuple[str, str, str]] = set()
    for _id, workspace_id, agent_id, pattern, scope in _wildcard_grants():
        sole = {
            connector: _sole_connection(bind, workspace_id, connector) for connector in connectors
        }
        for capability, updated in derived(pattern, scope, sole, _connector_of(bind, workspace_id)):
            key = (str(agent_id), capability, json.dumps(updated, sort_keys=True))
            if key in seen:
                continue
            seen.add(key)
            already = bind.execute(
                sa.select(_GRANT.c.id).where(
                    _GRANT.c.agent_id == agent_id, _GRANT.c.capability == capability
                )
            ).first()
            if already is not None:
                continue
            rows.append((workspace_id, agent_id, capability, updated))
    return rows


def upgrade() -> None:
    _apply(restated)
    bind = op.get_bind()
    # A Python timestamp, not sa.func.now(): these rows go in as one
    # executemany, and a SQL function cannot ride in a bound parameter.
    now = datetime.now(UTC)
    inserts = [
        {
            "id": new_uuid7(),
            "workspace_id": workspace_id,
            "agent_id": agent_id,
            "capability": capability,
            "scope_json": scope,
            "effect": "allow",
            "created_at": now,
            "updated_at": now,
        }
        for workspace_id, agent_id, capability, scope in _derived_rows()
    ]
    if inserts:
        bind.execute(_GRANT.insert(), inserts)


def downgrade() -> None:
    bind = op.get_bind()
    # The inserted rows go first: they are exact-capability grants carrying the
    # ``"*"`` values ``undone`` strips, so restating them first would leave
    # behind an *unscoped* grant — wider than anything that was here before.
    #
    # They are recognised by recomputing what upgrade would write and matching
    # it in Python: JSON equality in SQL is a different thing on each dialect,
    # and a row somebody has since edited must be left alone. It cannot tell a
    # row this migration inserted from an identical hand-made one (0032 makes
    # the same trade); the hand-made one would have *stopped* the insert, so
    # the cost of the ambiguity is a grant removed on downgrade, which fails
    # closed — the wildcard grant beside it is still there and still denied by
    # name until somebody scopes it.
    wanted = {
        (str(agent_id), capability, json.dumps(scope, sort_keys=True))
        for agent_id, capability, scope in _derived_rows_that_exist()
    }
    for grant_id, agent_id, capability, scope in _exact_allow_grants():
        if (str(agent_id), capability, json.dumps(scope, sort_keys=True)) in wanted:
            bind.execute(_GRANT.delete().where(_GRANT.c.id == grant_id))
    _apply(undone)


def _derived_rows_that_exist() -> list[tuple[Any, str, dict[str, Any]]]:
    """What upgrade would write for the wildcard grants still in the table."""
    bind = op.get_bind()
    connectors = {connector for connector, _keys in AFFECTED.values()}
    rows: list[tuple[Any, str, dict[str, Any]]] = []
    for _id, workspace_id, agent_id, pattern, scope in _wildcard_grants():
        sole = {
            connector: _sole_connection(bind, workspace_id, connector) for connector in connectors
        }
        rows.extend(
            (agent_id, capability, updated)
            for capability, updated in derived(
                pattern, scope, sole, _connector_of(bind, workspace_id)
            )
        )
    return rows


def _exact_allow_grants() -> list[tuple[Any, Any, str, dict[str, Any]]]:
    bind = op.get_bind()
    rows = bind.execute(
        sa.select(_GRANT.c.id, _GRANT.c.agent_id, _GRANT.c.capability, _GRANT.c.scope_json).where(
            _GRANT.c.capability.in_(sorted(AFFECTED)), _GRANT.c.effect == "allow"
        )
    ).all()
    return [(row[0], row[1], row[2], dict(row[3] or {})) for row in rows]
