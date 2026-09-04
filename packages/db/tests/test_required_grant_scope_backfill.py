"""0039: what an upgrade does to grants that predate the required scope keys.

The migration's whole promise is that it restates authority rather than
changing it, so the interesting assertions are about equivalence: a grant that
authorised any branch still authorises any branch, and one that already names a
branch is left exactly as its author wrote it. The decision is a pure function
so it can be read here without a database standing in the way; the SQL around
it is four lines of ``UPDATE``, and ``test_migration_graph`` pins where it sits
in the chain.
"""

from __future__ import annotations

import importlib
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
import sqlalchemy as sa
from pydantic import BaseModel

from jhin_domain import new_uuid7
from jhin_policy import DecisionType, Grant, RiskLevel, ToolDefinition, evaluate

MODULE = importlib.import_module(
    "jhin_db.alembic.versions.20260903_0039_required_grant_scope_backfill"
)

CONNECTION = "8e4a0b2e-0000-4000-8000-000000000001"
WORKSPACE = UUID("8e4a0b2e-0000-4000-8000-000000000002")
AGENT = UUID("8e4a0b2e-0000-4000-8000-000000000003")


class _Payload(BaseModel):
    """Stand-in schema: these tests exercise scope, never a tool body."""


def _tool(name: str, risk: RiskLevel, keys: tuple[str, ...]) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description="",
        risk=risk,
        input_model=_Payload,
        output_model=_Payload,
        required_capability=name,
        supports_approval=True,
        scope_keys=keys,
        required_grant_scope_keys=keys,
    )


def test_an_unstated_branch_becomes_the_star_it_already_was() -> None:
    """``scope_matches`` walks the granted keys, so a push grant with no
    ``branch`` matched every branch. ``*`` says the same thing out loud."""
    scope = {"connection_id": CONNECTION, "repository": "octo/alpha"}
    assert MODULE.restated("cli.repository.push", scope, CONNECTION) == {
        "connection_id": CONNECTION,
        "repository": "octo/alpha",
        "branch": "*",
    }


def test_a_branch_somebody_chose_is_never_touched() -> None:
    scope = {"connection_id": CONNECTION, "repository": "octo/alpha", "branch": "agent/*"}
    assert MODULE.restated("cli.repository.push", scope, CONNECTION) == scope


def test_the_sole_connection_fills_in_a_connection_id() -> None:
    """A narrowing, and the only one this migration makes: from "whichever
    connection the call named" to the one that exists."""
    assert MODULE.restated("cli.repository.checkout", {"repository": "octo/*"}, CONNECTION) == {
        "repository": "octo/*",
        "connection_id": CONNECTION,
    }


def test_an_ambiguous_workspace_is_left_to_fail_loudly() -> None:
    """No CLI connection, or several, and the migration cannot answer for the
    operator. The grant stays as it is and the denial names ``connection_id``
    the first time it is used — the loud failure F6 asked for."""
    restated = MODULE.restated("cli.repository.checkout", {"repository": "octo/*"}, None)
    assert "connection_id" not in restated

    decision = evaluate(
        _tool("cli.repository.checkout", RiskLevel.WRITE, ("connection_id", "repository")),
        grants=[Grant(capability="cli.repository.checkout", scope=restated)],
        rules=[],
        requested_scope={"connection_id": CONNECTION, "repository": "octo/alpha"},
    )
    assert decision.decision is DecisionType.DENY
    assert decision.code == "required_scope_missing"
    assert "connection_id" in decision.reason


def test_a_restated_grant_authorises_exactly_what_it_did_before() -> None:
    """The equivalence the migration promises, measured through the evaluator
    rather than asserted in prose."""
    tool = _tool(
        "cli.repository.push", RiskLevel.ELEVATED, ("connection_id", "repository", "branch")
    )
    legacy = {"connection_id": CONNECTION, "repository": "octo/alpha"}
    grant = Grant(
        capability="cli.repository.push",
        scope=MODULE.restated("cli.repository.push", legacy, CONNECTION),
    )
    for branch in ("agent/fix", "feature/anything", "some-branch"):
        decision = evaluate(
            tool,
            grants=[grant],
            rules=[],
            requested_scope={
                "connection_id": CONNECTION,
                "repository": "octo/alpha",
                "branch": branch,
            },
        )
        assert decision.decision is not DecisionType.DENY, branch


@pytest.mark.parametrize(
    "capability",
    ["cli.repository.checkout", "cli.repository.push", "github.pull_request.create"],
)
def test_downgrade_undoes_only_what_upgrade_wrote(capability: str) -> None:
    original = {"repository": "octo/alpha"}
    restated = MODULE.restated(capability, original, CONNECTION)
    assert MODULE.undone(capability, restated, CONNECTION) == original


def test_downgrade_leaves_a_value_somebody_has_since_edited() -> None:
    edited = {"connection_id": CONNECTION, "repository": "octo/alpha", "branch": "agent/*"}
    assert MODULE.undone("cli.repository.push", edited, "another-connection") == edited


def test_the_migration_restates_exactly_the_keys_the_tools_require() -> None:
    """The migration and the tool definitions must not drift. A key the tool
    requires but the migration does not restate is a grant that breaks on
    upgrade; a key the migration writes but the tool does not require is a
    grant silently given a dimension nobody asked for."""
    from jhin_connectors.cli.tools import CLI_TOOLS
    from jhin_connectors.github.tools import GITHUB_TOOLS

    by_name = {definition.name: definition for definition, _ in (*CLI_TOOLS, *GITHUB_TOOLS)}
    for capability, (_connector, wildcard_keys) in MODULE.AFFECTED.items():
        required = set(by_name[capability].required_grant_scope_keys)
        # connection_id is the one key restated from the workspace rather than
        # as "*", so it is handled separately and is not in wildcard_keys.
        assert set(wildcard_keys) | {"connection_id"} == required, capability


# --- grants whose capability is a pattern, not a name -------------------------


def test_covers_is_the_evaluators_own_rule() -> None:
    """The migration spells the matcher out rather than importing it, so the
    two are pinned together here instead of by an import."""
    from jhin_policy import capability_matches

    patterns = ("*", "cli.*", "github.*", "cli.repository.push", "cli.repository.pus", "clip.*")
    capabilities = (*MODULE.AFFECTED, "cli.command.execute", "system.echo")
    for pattern in patterns:
        for capability in capabilities:
            assert MODULE.covers(pattern, capability) is capability_matches(pattern, capability), (
                pattern,
                capability,
            )


CLI_CONNECTORS = {CONNECTION: "cli"}


def test_a_wildcard_grant_is_given_the_grants_it_is_about_to_need() -> None:
    """``cli.*`` authorised the push yesterday and would be denied today, so
    the authority is written down beside it — one exact row per capability,
    carrying the wildcard grant's own scope."""
    rows = MODULE.derived(
        "cli.*", {"connection_id": CONNECTION}, {"cli": CONNECTION}, CLI_CONNECTORS
    )
    assert dict(rows) == {
        "cli.repository.checkout": {"connection_id": CONNECTION, "repository": "*"},
        "cli.repository.push": {"connection_id": CONNECTION, "repository": "*", "branch": "*"},
    }


def test_a_star_grant_reaches_the_github_capability_as_well() -> None:
    rows = MODULE.derived("*", {}, {"cli": CONNECTION, "github": "gh-1"}, CLI_CONNECTORS)
    assert sorted(capability for capability, _scope in rows) == sorted(MODULE.AFFECTED)
    assert dict(rows)["github.pull_request.create"] == {
        "repository": "*",
        "base": "*",
        "connection_id": "gh-1",
    }


def test_a_star_grant_scoped_to_one_connection_derives_only_that_connectors_rows() -> None:
    """A ``*`` grant that names a CLI connection never authorised a GitHub
    call: ``scope_matches`` compares the ``connection_id`` the call carries
    against the one the grant names, and a ``github.pull_request.create`` call
    carries a GitHub connection. So no row is written for it — one carrying the
    CLI id could never match, and one carrying the workspace's GitHub
    connection would grant something the wildcard row never did."""
    rows = MODULE.derived(
        "*", {"connection_id": CONNECTION}, {"cli": CONNECTION, "github": "gh-1"}, CLI_CONNECTORS
    )
    assert sorted(capability for capability, _scope in rows) == [
        "cli.repository.checkout",
        "cli.repository.push",
    ]

    # And the equivalence measured rather than asserted: neither the wildcard
    # grant nor anything derived from it lets a GitHub call through.
    tool = _tool(
        "github.pull_request.create", RiskLevel.WRITE, ("connection_id", "repository", "base")
    )
    grants = [
        Grant(capability="*", scope={"connection_id": CONNECTION}),
        *(Grant(capability=name, scope=scope) for name, scope in rows),
    ]
    decision = evaluate(
        tool,
        grants=grants,
        rules=[],
        requested_scope={"connection_id": "gh-1", "repository": "octo/alpha", "base": "main"},
    )
    assert decision.decision is DecisionType.DENY


def test_a_grant_naming_a_connection_that_is_gone_derives_nothing() -> None:
    """Same rule, and the reason it is written as "which connector is this id"
    rather than "is this id the wrong one": an id nobody can resolve is not
    evidence that the grant meant this connector."""
    assert MODULE.derived("*", {"connection_id": "vanished"}, {"cli": CONNECTION}, {}) == []


def test_a_grant_that_names_the_capability_derives_nothing() -> None:
    """It is restated in place; a second row would say the same thing twice."""
    assert MODULE.derived("cli.repository.push", {}, {"cli": CONNECTION}, CLI_CONNECTORS) == []


def test_an_ambiguous_connection_derives_nothing_and_fails_loudly_instead() -> None:
    assert MODULE.derived("cli.*", {}, {"cli": None}, CLI_CONNECTORS) == []


def test_the_derived_grant_authorises_the_push_without_touching_the_rest() -> None:
    """The equivalence that makes this safe: the wildcard grant keeps covering
    every other tool it covered — including ``cli.command.execute``, whose
    calls carry no ``repository`` at all and which would have been denied if
    the wildcard grant itself had been given one."""
    wildcard = Grant(capability="cli.*", scope={"connection_id": CONNECTION})
    rows = MODULE.derived(
        "cli.*", {"connection_id": CONNECTION}, {"cli": CONNECTION}, CLI_CONNECTORS
    )
    grants = [wildcard, *(Grant(capability=name, scope=scope) for name, scope in rows)]

    push = evaluate(
        _tool("cli.repository.push", RiskLevel.ELEVATED, ("connection_id", "repository", "branch")),
        grants=grants,
        rules=[],
        requested_scope={
            "connection_id": CONNECTION,
            "repository": "octo/alpha",
            "branch": "agent/fix",
        },
    )
    assert push.decision is not DecisionType.DENY

    command = ToolDefinition(
        name="cli.command.execute",
        description="",
        risk=RiskLevel.WRITE,
        input_model=_Payload,
        output_model=_Payload,
        required_capability="cli.command.execute",
        supports_approval=True,
        scope_keys=("connection_id", "command"),
    )
    decision = evaluate(
        command,
        grants=grants,
        rules=[],
        requested_scope={"connection_id": CONNECTION, "command": "ls"},
    )
    assert decision.decision is DecisionType.ALLOW


# --- the SQL, against a database ---------------------------------------------


def _database() -> sa.Engine:
    """The two tables this migration reads and writes, so ``upgrade()`` and
    ``downgrade()`` can be run for real rather than described."""
    metadata = sa.MetaData()
    sa.Table(
        "agent_capability_grant",
        metadata,
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("workspace_id", sa.Uuid(as_uuid=True)),
        sa.Column("agent_id", sa.Uuid(as_uuid=True)),
        sa.Column("capability", sa.String(200)),
        sa.Column("scope_json", sa.JSON),
        sa.Column("effect", sa.String(16)),
        sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
    )
    sa.Table(
        "connection",
        metadata,
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("workspace_id", sa.Uuid(as_uuid=True)),
        sa.Column("connector_type", sa.String(50)),
        sa.Column("status", sa.String(16)),
    )
    engine = sa.create_engine("sqlite://")
    metadata.create_all(engine)
    return engine


@pytest.fixture
def migration_db(monkeypatch: pytest.MonkeyPatch) -> Iterator[sa.Connection]:
    engine = _database()
    with engine.begin() as connection:
        monkeypatch.setattr(MODULE, "op", SimpleNamespace(get_bind=lambda: connection))
        yield connection
    engine.dispose()


def _seed(connection: sa.Connection, *grants: tuple[str, dict[str, Any], str]) -> None:
    """Rows as they stood before the release, written through the migration's
    own table definitions so the column types are the ones it will use."""
    connection.execute(
        MODULE._CONNECTION.insert(),
        [
            {
                "id": UUID(CONNECTION),
                "workspace_id": WORKSPACE,
                "connector_type": "cli",
                "status": "active",
            }
        ],
    )
    connection.execute(
        MODULE._GRANT.insert(),
        [
            {
                "id": new_uuid7(),
                "workspace_id": WORKSPACE,
                "agent_id": AGENT,
                "capability": capability,
                "scope_json": scope,
                "effect": effect,
            }
            for capability, scope, effect in grants
        ],
    )


def _rows(connection: sa.Connection) -> list[tuple[str, dict[str, Any], str]]:
    rows = connection.execute(
        sa.select(
            MODULE._GRANT.c.capability, MODULE._GRANT.c.scope_json, MODULE._GRANT.c.effect
        ).order_by(MODULE._GRANT.c.capability, MODULE._GRANT.c.effect)
    ).all()
    return [(row[0], dict(row[1] or {}), row[2]) for row in rows]


def test_upgrade_writes_the_wildcard_grants_authority_down_and_downgrade_takes_it_back(
    migration_db: sa.Connection,
) -> None:
    _seed(migration_db, ("cli.*", {"connection_id": CONNECTION}, "allow"))

    MODULE.upgrade()

    assert _rows(migration_db) == [
        ("cli.*", {"connection_id": CONNECTION}, "allow"),
        ("cli.repository.checkout", {"connection_id": CONNECTION, "repository": "*"}, "allow"),
        (
            "cli.repository.push",
            {"connection_id": CONNECTION, "repository": "*", "branch": "*"},
            "allow",
        ),
    ]

    MODULE.downgrade()

    # Back to the row that was there, with nothing left unscoped behind it.
    assert _rows(migration_db) == [("cli.*", {"connection_id": CONNECTION}, "allow")]


def test_upgrade_does_not_second_guess_a_grant_somebody_already_wrote(
    migration_db: sa.Connection,
) -> None:
    """An existing row for the capability — in either effect — is a decision.
    A deny especially: writing an allow beside it would be noise at best."""
    _seed(
        migration_db,
        ("cli.*", {"connection_id": CONNECTION}, "allow"),
        ("cli.repository.push", {}, "deny"),
        ("cli.repository.checkout", {"connection_id": CONNECTION, "repository": "octo/*"}, "allow"),
    )

    MODULE.upgrade()

    assert _rows(migration_db) == [
        ("cli.*", {"connection_id": CONNECTION}, "allow"),
        # Restated in place, as an exact grant always was.
        ("cli.repository.checkout", {"connection_id": CONNECTION, "repository": "octo/*"}, "allow"),
        ("cli.repository.push", {}, "deny"),
    ]


def test_a_wildcard_grant_that_reaches_no_affected_capability_is_left_alone(
    migration_db: sa.Connection,
) -> None:
    _seed(migration_db, ("memory.*", {}, "allow"), ("cli.file.*", {}, "allow"))

    MODULE.upgrade()

    assert _rows(migration_db) == [("cli.file.*", {}, "allow"), ("memory.*", {}, "allow")]


def test_downgrade_removes_only_the_rows_it_would_have_written(
    migration_db: sa.Connection,
) -> None:
    """A grant somebody wrote by hand is not one of these rows, however close
    it sits: the scope has to be exactly what upgrade would have produced."""
    mine = {"connection_id": CONNECTION, "repository": "octo/*", "branch": "agent/*"}
    _seed(
        migration_db,
        ("cli.*", {"connection_id": CONNECTION}, "allow"),
        ("cli.repository.push", dict(mine), "allow"),
    )

    MODULE.upgrade()
    # Only the checkout row is added: the push capability already has a grant.
    assert [capability for capability, _scope, _effect in _rows(migration_db)] == [
        "cli.*",
        "cli.repository.checkout",
        "cli.repository.push",
    ]

    MODULE.downgrade()

    remaining = _rows(migration_db)
    assert [capability for capability, _scope, _effect in remaining] == [
        "cli.*",
        "cli.repository.push",
    ]
    # The hand-written scope keeps everything except the connection_id, which
    # downgrade cannot tell from one this migration filled in (as documented).
    assert remaining[1][1] == {"repository": "octo/*", "branch": "agent/*"}
