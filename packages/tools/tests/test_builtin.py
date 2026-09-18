"""Registry lookups, advertisement filtering, and the plan 21.7 assertion
that no self-modifying / capability-granting tool exists."""

from typing import Literal

import pytest
from pydantic import BaseModel

from jhin_db.models import Task
from jhin_domain import new_uuid7
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
    task_expects_a_reported_result,
    task_has_a_person_watching,
    task_scoped_tool_definitions,
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
    grants.append(
        Grant(capability="github.*", scope={"connection_id": conn}, effect=GrantEffect.DENY)
    )
    assert connection_hints(definition, grants, labels) == ""


def test_unpinned_hints_need_compatible_connection_metadata() -> None:
    definition = _connector_definition().model_copy(update={"required_grant_scope_keys": ()})
    grants = [Grant(capability="github.*", scope={"repository": "octo/alpha"})]
    labels = {"github-id": "GitHub", "cli-id": "Sandbox"}
    # Labels alone do not say which connector can execute the tool.
    assert connection_hints(definition, grants, labels) == ""
    hints = connection_hints(
        definition,
        grants,
        labels,
        connection_tool_names={"github-id": {definition.name}, "cli-id": {"cli.test.run"}},
    )
    assert "connection_id=github-id (repository=octo/alpha)" in hints
    assert "cli-id" not in hints


@pytest.mark.parametrize(
    "scope",
    [{}, {"repository": "octo/alpha"}, {"connection_id": "github-id", "unknown_dimension": "x"}],
)
def test_connection_hints_do_not_repair_unusable_grant_scopes(scope: dict[str, object]) -> None:
    definition = _connector_definition()
    assert (
        connection_hints(
            definition,
            [Grant(capability=definition.name, scope=scope)],
            {"github-id": "GitHub"},
            connection_tool_names={"github-id": {definition.name}},
        )
        == ""
    )


def test_connection_hints_withhold_connection_wide_denies() -> None:
    definition = _connector_definition()
    assert (
        connection_hints(
            definition,
            [
                Grant(capability=definition.name, scope={"connection_id": "github-id"}),
                Grant(capability="github.*", scope={}, effect=GrantEffect.DENY),
            ],
            {"github-id": "GitHub"},
            connection_tool_names={"github-id": {definition.name}},
        )
        == ""
    )


@pytest.mark.parametrize("allowed_path", [None, "public/readme.txt", "public/*", "private/?*"])
def test_narrow_path_deny_keeps_connections_with_remaining_allowed_calls(
    allowed_path: str | None,
) -> None:
    definition = _connector_definition().model_copy(
        update={
            "name": "cli.file.read",
            "required_capability": "cli.file.read",
            "scope_keys": ("connection_id", "path"),
        }
    )
    scope = {"connection_id": "cli-id"}
    if allowed_path is not None:
        scope["path"] = allowed_path
    hints = connection_hints(
        definition,
        [
            Grant(capability=definition.name, scope=scope),
            Grant(capability=definition.name, scope={"path": "private/*"}, effect=GrantEffect.DENY),
        ],
        {"cli-id": "Sandbox"},
        connection_tool_names={"cli-id": {definition.name}},
    )
    assert "connection_id=cli-id" in hints
    assert "denied scopes: path=private/*" in hints


@pytest.mark.parametrize(
    "allowed_path", ["private/readme.txt", "private/*", ["private/a", "private/b"]]
)
def test_narrow_path_deny_withholds_a_fully_covered_allow_scope(allowed_path: object) -> None:
    definition = _connector_definition().model_copy(
        update={
            "name": "cli.file.read",
            "required_capability": "cli.file.read",
            "scope_keys": ("connection_id", "path"),
        }
    )
    assert (
        connection_hints(
            definition,
            [
                Grant(
                    capability=definition.name,
                    scope={"connection_id": "cli-id", "path": allowed_path},
                ),
                Grant(
                    capability=definition.name, scope={"path": "private/*"}, effect=GrantEffect.DENY
                ),
            ],
            {"cli-id": "Sandbox"},
            connection_tool_names={"cli-id": {definition.name}},
        )
        == ""
    )


@pytest.mark.parametrize("dimension", ["tool", "server_slug", "toolkit"])
@pytest.mark.parametrize(
    ("effect", "value", "visible"),
    [
        ("allow", "fixed", True),
        ("allow", "other", False),
        ("deny", "fixed", False),
        ("deny", "other", True),
    ],
)
def test_connection_hints_respect_fixed_input_scope_values(
    dimension: str, effect: str, value: str, visible: bool
) -> None:
    class DynamicInput(BaseModel):
        connection_id: str
        tool: Literal["echo"] = "echo"
        server_slug: Literal["demo"] = "demo"
        toolkit: Literal["example"] = "example"

    definition = _connector_definition().model_copy(
        update={
            "name": "mcp.demo.echo",
            "required_capability": "mcp.demo.echo",
            "input_model": DynamicInput,
            "scope_keys": ("connection_id", "tool", "server_slug", "toolkit"),
            "required_grant_scope_keys": (),
        }
    )
    fixed = {"tool": "echo", "server_slug": "demo", "toolkit": "example"}
    grant = Grant(
        capability="mcp.demo.*",
        scope={dimension: fixed[dimension] if value == "fixed" else value},
        effect=GrantEffect(effect),
    )
    grants = [grant] if effect == "allow" else [Grant(capability="mcp.demo.*"), grant]
    hints = connection_hints(
        definition,
        grants,
        {"mcp-id": "Demo"},
        connection_tool_names={"mcp-id": {definition.name}},
    )
    assert ("connection_id=mcp-id" in hints) is visible


def test_connection_hints_are_empty_without_connection_scope() -> None:
    definition = _connector_definition()
    assert connection_hints(definition, [], {}) == ""
    entry = build_builtin_catalog().get("system.echo")
    assert entry is not None
    echo = entry[0]
    grants = [Grant(capability="system.echo", scope={}, effect=GrantEffect.ALLOW)]
    assert advertised_description(echo, grants, {}) == echo.description


def _task(**values: object) -> Task:
    return Task(
        id=new_uuid7(),
        workspace_id=new_uuid7(),
        title="Task",
        correlation_id=new_uuid7(),
        metadata_json=values.pop("metadata_json", {}),
        **values,
    )


@pytest.mark.parametrize(
    ("task", "expected"),
    [
        # A delegated or review child reports back to its delegator.
        (_task(parent_task_id=new_uuid7(), metadata_json={"origin": "delegation"}), True),
        (
            _task(
                parent_task_id=new_uuid7(),
                metadata_json={"delegation": {"kind": "review_request"}},
            ),
            True,
        ),
        # An accepted work request is assigned work even though it is linked
        # to the requester's chat, so conversation_id must not decide alone.
        (
            _task(
                conversation_id=new_uuid7(),
                metadata_json={"origin": "work_request", "work_request": {"id": "w"}},
            ),
            True,
        ),
        # A standalone task from the Tasks UI: its result card is its outcome.
        (_task(), True),
        # A trigger-created task is nobody's chat turn either.
        (_task(metadata_json={"origin": "trigger"}), True),
        # Chat turns: the person is waiting for a reply, not for a report.
        (
            _task(
                conversation_id=new_uuid7(),
                metadata_json={"origin": "conversation", "conversation_id": "c"},
            ),
            False,
        ),
        (_task(metadata_json={"origin": "message"}), False),
        (_task(conversation_id=new_uuid7()), False),
        # Unknown task: advertisement stays as-is (the gateway still decides).
        (None, True),
    ],
)
def test_reported_results_are_expected_only_from_assigned_work(
    task: Task | None, expected: bool
) -> None:
    assert task_expects_a_reported_result(task) is expected


def test_task_scoping_drops_only_the_reporting_tool_on_a_chat_turn() -> None:
    catalog = build_builtin_catalog()
    grants = [
        Grant(capability="organization.*", scope={}, effect=GrantEffect.ALLOW),
        Grant(capability="system.echo", scope={}, effect=GrantEffect.ALLOW),
    ]
    granted = allowed_tool_definitions(catalog, grants)
    assert "organization.report_result" in {d.name for d in granted}

    chat = task_scoped_tool_definitions(granted, _task(conversation_id=new_uuid7()))
    delegated = task_scoped_tool_definitions(granted, _task(parent_task_id=new_uuid7()))

    assert {d.name for d in granted} - {d.name for d in chat} == {"organization.report_result"}
    # A delegated child keeps everything it can act on; the one thing it
    # loses is the ask, because it has nobody to ask.
    assert {d.name for d in granted} - {d.name for d in delegated} == {"organization.ask_person"}


@pytest.mark.parametrize(
    ("task", "expected"),
    [
        # A chat thread a person opened: the only place there is somebody to
        # interrupt.
        (
            _task(
                conversation_id=new_uuid7(),
                metadata_json={"origin": "conversation", "conversation_id": "c"},
            ),
            True,
        ),
        (_task(metadata_json={"origin": "message"}), True),
        (_task(conversation_id=new_uuid7()), True),
        # A delegated child reports into its parent; the person is watching
        # that thread, not this task's page.
        (
            _task(
                parent_task_id=new_uuid7(),
                conversation_id=new_uuid7(),
                metadata_json={"delegation": {"kind": "delegation"}},
            ),
            False,
        ),
        # An accepted work request is linked to the requester's chat, so
        # conversation_id must not decide alone.
        (
            _task(
                conversation_id=new_uuid7(),
                metadata_json={"origin": "work_request", "work_request": {"id": "w"}},
            ),
            False,
        ),
        # Nobody is sitting in front of a trigger-fired run or a standalone
        # task from the Tasks UI.
        (_task(metadata_json={"origin": "trigger"}), False),
        (_task(), False),
        # Unknown task: the opposite default to task_expects_a_reported_result.
        # Withholding a report is prompt economy; withholding an interruption
        # is a promise to the person.
        (None, False),
    ],
)
def test_only_a_chat_turn_has_a_person_to_ask(task: Task | None, expected: bool) -> None:
    assert task_has_a_person_watching(task) is expected


def test_asking_a_person_is_not_advertised_where_nobody_is_watching() -> None:
    catalog = build_builtin_catalog()
    grants = [Grant(capability="organization.*", scope={}, effect=GrantEffect.ALLOW)]
    granted = allowed_tool_definitions(catalog, grants)
    assert "organization.ask_person" in {d.name for d in granted}

    chat = task_scoped_tool_definitions(granted, _task(conversation_id=new_uuid7()))
    delegated = task_scoped_tool_definitions(granted, _task(parent_task_id=new_uuid7()))
    triggered = task_scoped_tool_definitions(granted, _task(metadata_json={"origin": "trigger"}))

    assert "organization.ask_person" in {d.name for d in chat}
    assert "organization.ask_person" not in {d.name for d in delegated}
    assert "organization.ask_person" not in {d.name for d in triggered}
    # The two filters are independent: a chat turn loses the reporting tool
    # and keeps the ask; a delegated child does the reverse.
    assert "organization.report_result" not in {d.name for d in chat}
    assert "organization.report_result" in {d.name for d in delegated}


def test_the_reporting_tool_never_reads_as_how_to_end_a_chat() -> None:
    """Its description is the only guidance the model gets about when to use
    it; it must say who it reports to and what to do in a conversation."""
    entry = build_builtin_catalog().get("organization.report_result")
    assert entry is not None
    description = entry[0].description
    assert "delegated" in description
    assert "answer them in your reply" in description


def test_allowed_definitions_withhold_grants_pinned_outside_the_live_set() -> None:
    """A grant pinned to a deleted or disabled connection advertises nothing:
    a tool the agent could only ever be denied is not worth offering."""
    catalog = build_builtin_catalog()
    live = "01a02d06-7971-7280-9511-0e579bd4d0a0"
    dead = "01a02d06-7971-7280-9511-0e579bd4d0a1"
    grants = [
        Grant(capability="system.echo", scope={"connection_id": dead}, effect=GrantEffect.ALLOW),
        Grant(capability="system.time", scope={"connection_id": live}, effect=GrantEffect.ALLOW),
        Grant(capability="system.demo.*", scope={}, effect=GrantEffect.ALLOW),
    ]

    names = {d.name for d in allowed_tool_definitions(catalog, grants, live_connection_ids={live})}

    assert "system.echo" not in names
    assert "system.time" in names
    assert {"system.demo.elevated", "system.demo.destructive"} <= names


def test_allowed_definitions_keep_a_tool_with_one_live_pin_among_dead_ones() -> None:
    catalog = build_builtin_catalog()
    live = "01a02d06-7971-7280-9511-0e579bd4d0a0"
    grants = [
        Grant(capability="system.echo", scope={"connection_id": "gone"}, effect=GrantEffect.ALLOW),
        Grant(capability="system.echo", scope={"connection_id": live}, effect=GrantEffect.ALLOW),
    ]
    names = {d.name for d in allowed_tool_definitions(catalog, grants, live_connection_ids={live})}
    assert names == {"system.echo"}


def test_allowed_definitions_are_unchanged_without_a_live_set() -> None:
    catalog = build_builtin_catalog()
    grants = [
        Grant(capability="system.echo", scope={"connection_id": "gone"}, effect=GrantEffect.ALLOW),
    ]
    assert {d.name for d in allowed_tool_definitions(catalog, grants)} == {"system.echo"}
    assert {
        d.name for d in allowed_tool_definitions(catalog, grants, live_connection_ids=None)
    } == {"system.echo"}
