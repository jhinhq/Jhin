"""Registry lookups, advertisement filtering, and the plan 21.7 assertion
that no self-modifying / capability-granting tool exists."""

import pytest

from jhin_policy import (
    FORBIDDEN_CAPABILITY_PREFIXES,
    Grant,
    GrantEffect,
    RegistryError,
    RiskLevel,
    ToolDefinition,
)
from jhin_tools.builtin import (
    BUILTIN_TOOLS,
    EchoInput,
    EchoOutput,
    advertised_description,
    allowed_tool_definitions,
    build_builtin_catalog,
    connection_hints,
)


def test_catalog_contains_one_tool_per_risk_level() -> None:
    catalog = build_builtin_catalog()
    risks = {definition.risk for definition in catalog.definitions()}
    assert risks == set(RiskLevel)


def test_catalog_get_returns_definition_and_executor() -> None:
    catalog = build_builtin_catalog()
    entry = catalog.get("system.echo")
    assert entry is not None
    definition, executor = entry
    assert definition.required_capability == "system.echo"
    assert callable(executor)
    assert catalog.get("system.nope") is None


def test_no_builtin_tool_grants_capabilities_or_self_modifies() -> None:
    """Plan 21.7: agents must not reach any capability-granting or
    self-modifying surface through the tool registry."""
    for definition, _ in BUILTIN_TOOLS:
        for prefix in FORBIDDEN_CAPABILITY_PREFIXES:
            assert not definition.name.startswith(prefix)
            assert not definition.required_capability.startswith(prefix)


def test_registry_rejects_forbidden_capability_registration() -> None:
    catalog = build_builtin_catalog()
    bad = ToolDefinition(
        name="agent.permission.grant",
        description="must never register",
        risk=RiskLevel.READ,
        input_model=EchoInput,
        output_model=EchoOutput,
        required_capability="agent.permission.grant",
    )

    async def _noop(ctx: object, payload: object) -> EchoOutput:
        return EchoOutput(text="")

    with pytest.raises(RegistryError):
        catalog.register(bad, _noop)  # type: ignore[arg-type]


def test_allowed_definitions_follow_allow_grants_only() -> None:
    catalog = build_builtin_catalog()
    grants = [
        Grant(capability="system.echo", scope={}, effect=GrantEffect.ALLOW),
        Grant(capability="system.time", scope={}, effect=GrantEffect.DENY),
    ]
    names = {d.name for d in allowed_tool_definitions(catalog, grants)}
    assert names == {"system.echo"}


def test_allowed_definitions_expand_wildcards() -> None:
    catalog = build_builtin_catalog()
    grants = [Grant(capability="system.demo.*", scope={}, effect=GrantEffect.ALLOW)]
    names = {d.name for d in allowed_tool_definitions(catalog, grants)}
    assert names == {"system.demo.elevated", "system.demo.destructive"}


def test_no_grants_means_nothing_advertised() -> None:
    assert allowed_tool_definitions(build_builtin_catalog(), []) == ()


def _connector_definition() -> ToolDefinition:
    return ToolDefinition(
        name="github.repository.read",
        description="Read repository metadata.",
        risk=RiskLevel.READ,
        input_model=EchoInput,
        output_model=EchoOutput,
        required_capability="github.repository.read",
        scope_keys=("connection_id", "repository"),
        required_grant_scope_keys=("connection_id",),
    )


def test_connection_hints_spell_out_pinned_connections() -> None:
    definition = _connector_definition()
    conn = "01a02d06-7971-7280-9511-0e579bd4d0a0"
    grants = [
        Grant(
            capability="github.repository.read",
            scope={"connection_id": conn, "repository": "octo/alpha"},
            effect=GrantEffect.ALLOW,
        ),
        # Unknown connection ids (disabled / foreign) and denies add nothing.
        Grant(
            capability="github.repository.read",
            scope={"connection_id": "11111111-1111-7111-8111-111111111111"},
            effect=GrantEffect.ALLOW,
        ),
        Grant(capability="github.*", scope={"connection_id": conn}, effect=GrantEffect.DENY),
    ]
    labels = {conn: "GitHub (dev fake) (github)"}
    hints = connection_hints(definition, grants, labels)
    assert hints == (
        "Connections you may use (pass the connection_id exactly as given): "
        f"GitHub (dev fake) (github) — connection_id={conn} (repository=octo/alpha)"
    )
    assert advertised_description(definition, grants, labels).startswith(
        "Read repository metadata. Connections you may use"
    )


def test_connection_hints_are_empty_without_connection_scope() -> None:
    definition = _connector_definition()
    assert connection_hints(definition, [], {}) == ""
    entry = build_builtin_catalog().get("system.echo")
    assert entry is not None
    echo = entry[0]
    grants = [Grant(capability="system.echo", scope={}, effect=GrantEffect.ALLOW)]
    assert advertised_description(echo, grants, {}) == echo.description
