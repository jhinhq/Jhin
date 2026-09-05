"""The bundle planner never emits a row the evaluator would refuse."""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from jhin_connectors import build_default_definition_catalog
from jhin_policy import (
    BUNDLES,
    ConnectionRef,
    DecisionType,
    Grant,
    GrantEffect,
    Need,
    PolicyRule,
    ToolDefinition,
    bundle_by_id,
    bundle_state,
    evaluate,
    grant_problem_details,
    grant_problems,
    is_forbidden_capability,
    plan_bundle,
)
from jhin_policy.bundles import CODE_EDITING_BUNDLE_ID, GITHUB_READ_BUNDLE_ID, WEB_ACCESS_BUNDLE_ID
from jhin_policy.risk import RuleAction

GH = "01a07171-751a-7010-8aef-fd77bc32c2ac"
GH2 = "01a07171-751a-7010-8aef-fd77bc32c2ad"
CLI = "01a07171-751a-7010-8aef-fd77bc32c2ae"
CLI2 = "01a07171-751a-7010-8aef-fd77bc32c2af"
WEB = "01a07171-751a-7010-8aef-fd77bc32c2b0"


def catalog() -> tuple[ToolDefinition, ...]:
    return build_default_definition_catalog().definitions()


def github(
    connection_id: str = GH, *, name: str = "GitHub", status: str = "active"
) -> ConnectionRef:
    return ConnectionRef(id=connection_id, connector_type="github", name=name, status=status)


def sandbox(
    connection_id: str = CLI,
    *,
    git: str = GH,
    allowed: tuple[str, ...] | None = ("*",),
    name: str = "Sandbox",
    status: str = "active",
) -> ConnectionRef:
    return ConnectionRef(
        id=connection_id,
        connector_type="cli",
        name=name,
        status=status,
        git_connection_id=git,
        allowed_repositories=allowed,
    )


def web(connection_id: str = WEB) -> ConnectionRef:
    return ConnectionRef(id=connection_id, connector_type="web", name="Web", status="active")


CODE_EDITING = bundle_by_id(CODE_EDITING_BUNDLE_ID)
GITHUB_READ = bundle_by_id(GITHUB_READ_BUNDLE_ID)
WEB_ACCESS = bundle_by_id(WEB_ACCESS_BUNDLE_ID)
assert CODE_EDITING is not None and GITHUB_READ is not None and WEB_ACCESS is not None


def _rows(plan_grants: Sequence[object]) -> set[tuple[str, tuple[tuple[str, str], ...]]]:
    return {
        (spec.capability, tuple(sorted(spec.scope.items())))  # type: ignore[attr-defined]
        for spec in plan_grants
    }


# --- The bundles themselves -----------------------------------------------


def test_every_bundle_tool_exists_in_the_default_catalog() -> None:
    names = {definition.name for definition in catalog()}
    for bundle in BUNDLES:
        missing = set(bundle.tools) - names
        assert not missing, f"{bundle.id}: {sorted(missing)}"


def test_no_bundle_names_a_forbidden_capability_or_a_connection() -> None:
    by_name = {definition.name: definition for definition in catalog()}
    for bundle in BUNDLES:
        for name, scope in bundle.tools.items():
            assert not is_forbidden_capability(by_name[name].required_capability)
            assert "connection_id" not in scope


def test_connector_types_are_derived_from_the_tools() -> None:
    assert CODE_EDITING.connector_types == ("cli", "github")
    assert GITHUB_READ.connector_types == ("github",)
    assert WEB_ACCESS.connector_types == ("web",)
    collaboration = bundle_by_id("collaboration")
    assert collaboration is not None
    assert collaboration.connector_types == ()
    assert not collaboration.is_connector_bundle


# --- Planning -------------------------------------------------------------


def test_code_editing_plans_exactly_the_eleven_rows_with_base_star() -> None:
    plan = plan_bundle(CODE_EDITING, catalog=catalog(), connections=[github(), sandbox()])

    assert plan.needs == ()
    assert plan.refusals == ()
    assert _rows(plan.grants) == {
        ("cli.repository.checkout", (("connection_id", CLI), ("repository", "*"))),
        ("cli.file.list", (("connection_id", CLI), ("path", "*"))),
        ("cli.file.search", (("connection_id", CLI), ("path", "*"))),
        ("cli.file.read", (("connection_id", CLI), ("path", "*"))),
        ("cli.file.edit", (("connection_id", CLI), ("path", "*"))),
        ("cli.file.write", (("connection_id", CLI), ("path", "*"))),
        ("cli.test.run", (("command", "*"), ("connection_id", CLI))),
        (
            "cli.repository.push",
            (("branch", "agent/*"), ("connection_id", CLI), ("repository", "*")),
        ),
        ("github.repository.read", (("connection_id", GH), ("repository", "*"))),
        ("github.pull_request.read", (("connection_id", GH), ("repository", "*"))),
        (
            "github.pull_request.create",
            (("base", "*"), ("connection_id", GH), ("repository", "*")),
        ),
    }
    assert len(plan.grants) == 11
    assert plan.rules == (
        PolicyRule(capability="cli.repository.push", risk=None, action=RuleAction.APPROVAL),
    )


def test_github_read_plans_five_rows_and_web_access_two() -> None:
    read = plan_bundle(GITHUB_READ, catalog=catalog(), connections=[github()])
    # Seven tools, five capabilities: branch.list and file.read ride on
    # repository.read, so their rows collapse into one.
    assert {spec.capability for spec in read.grants} == {
        "github.repository.read",
        "github.pull_request.read",
        "github.issue.read",
        "github.check.read",
        "github.workflow_run.read",
    }
    assert len(read.grants) == 5
    assert all(spec.scope == {"connection_id": GH, "repository": "*"} for spec in read.grants)

    browse = plan_bundle(WEB_ACCESS, catalog=catalog(), connections=[web()])
    assert _rows(browse.grants) == {
        ("web.search", (("connection_id", WEB),)),
        ("web.fetch", (("connection_id", WEB), ("domain", "*"))),
    }


def test_two_github_connections_need_a_choice() -> None:
    plan = plan_bundle(
        GITHUB_READ, catalog=catalog(), connections=[github(), github(GH2, name="Other")]
    )
    assert plan.grants == ()
    assert plan.needs == (
        Need(
            kind="choose",
            connector_type="github",
            choices=(github(), github(GH2, name="Other")),
        ),
    )


def test_no_github_connection_needs_a_connect_and_no_sandbox_needs_one_created() -> None:
    assert plan_bundle(GITHUB_READ, catalog=catalog(), connections=[]).needs == (
        Need(kind="connect", connector_type="github"),
    )
    plan = plan_bundle(CODE_EDITING, catalog=catalog(), connections=[github()])
    assert plan.needs == (Need(kind="create_sandbox", connector_type="cli", choices=(github(),)),)
    assert plan.refusals == ()
    assert plan.grants == ()


def test_a_sandbox_pointing_at_another_github_connection_is_not_auto_picked() -> None:
    other = sandbox(CLI, git=GH2, name="Other sandbox")
    plan = plan_bundle(
        CODE_EDITING, catalog=catalog(), connections=[github(), github(GH2, name="GH2"), other]
    )
    # Two GitHub connections: the choice comes first.
    assert plan.needs[0].kind == "choose"

    chosen = plan_bundle(
        CODE_EDITING,
        catalog=catalog(),
        connections=[github(), github(GH2, name="GH2"), other],
        chosen={"github": GH},
    )
    assert chosen.needs == (Need(kind="create_sandbox", connector_type="cli", choices=(github(),)),)

    forced = plan_bundle(
        CODE_EDITING,
        catalog=catalog(),
        connections=[github(), github(GH2, name="GH2"), other],
        chosen={"github": GH, "cli": CLI},
    )
    assert forced.refusals == (
        "'Other sandbox' uses 'GH2' for repository jobs, not 'GitHub'. Pick a sandbox that "
        "uses this connection, or change its GitHub connection under Apps first.",
    )
    assert forced.grants == ()


def test_a_sandbox_without_a_github_connection_is_refused_when_chosen() -> None:
    plan = plan_bundle(
        CODE_EDITING,
        catalog=catalog(),
        connections=[github(), sandbox(git="")],
        chosen={"cli": CLI},
    )
    assert plan.refusals == (
        "'Sandbox' names no GitHub connection for repository jobs. Set one under Apps first.",
    )


@pytest.mark.parametrize(
    ("status", "sentence"),
    [
        ("disabled", "'GitHub' is disabled; enable it or pick another connection."),
        ("needs_reauth", "'GitHub' needs to be reconnected before agents can use it."),
        ("error", "'GitHub' needs to be reconnected before agents can use it."),
    ],
)
def test_a_chosen_connection_that_is_not_active_is_refused(status: str, sentence: str) -> None:
    plan = plan_bundle(
        GITHUB_READ, catalog=catalog(), connections=[github(status=status)], chosen={"github": GH}
    )
    assert plan.refusals == (sentence,)
    # And it is never auto-picked either.
    assert plan_bundle(
        GITHUB_READ, catalog=catalog(), connections=[github(status=status)]
    ).needs == (Need(kind="connect", connector_type="github"),)


def test_choosing_a_connection_of_the_wrong_type_is_refused() -> None:
    plan = plan_bundle(
        GITHUB_READ, catalog=catalog(), connections=[github(), web()], chosen={"github": WEB}
    )
    assert plan.refusals == ("'Web' is a web connection, not github.",)


def test_repositories_default_to_star_and_a_list_gives_one_row_each() -> None:
    plan = plan_bundle(
        GITHUB_READ,
        catalog=catalog(),
        connections=[github()],
        repositories=["octo/alpha", "octo/beta", "octo/alpha"],
    )
    reads = [spec for spec in plan.grants if spec.capability == "github.repository.read"]
    assert [spec.scope["repository"] for spec in reads] == ["octo/alpha", "octo/beta"]
    assert len(plan.grants) == 10


def test_a_bad_repository_entry_is_refused_by_sentence() -> None:
    plan = plan_bundle(
        GITHUB_READ,
        catalog=catalog(),
        connections=[github()],
        repositories=["https://github.com/octo/alpha"],
    )
    assert plan.refusals == (
        "'https://github.com/octo/alpha' is not a repository: use owner/name, owner/*, or * "
        "for every repository.",
    )


def test_a_repository_outside_the_sandbox_allow_list_is_refused() -> None:
    plan = plan_bundle(
        CODE_EDITING,
        catalog=catalog(),
        connections=[github(), sandbox(allowed=("octo/alpha", "octo/beta"))],
        repositories=["octo/gamma"],
    )
    assert plan.refusals == (
        "'Sandbox' allows only: octo/alpha, octo/beta — 'octo/gamma' is outside it. Add it to "
        "the sandbox's allowed repositories under Apps, or grant only what the sandbox "
        "allows.",
    )
    inside = plan_bundle(
        CODE_EDITING,
        catalog=catalog(),
        connections=[github(), sandbox(allowed=("octo/*",))],
        repositories=["octo/gamma"],
    )
    assert inside.refusals == ()


def test_an_empty_sandbox_allow_list_is_refused() -> None:
    plan = plan_bundle(CODE_EDITING, catalog=catalog(), connections=[github(), sandbox(allowed=())])
    assert plan.refusals == (
        "'Sandbox' allows no repositories; list them on the connection under Apps first.",
    )


def test_base_defaults_to_star_and_can_be_overridden_but_not_broken() -> None:
    def base_of(plan: object) -> str:
        return next(
            spec.scope["base"]  # type: ignore[attr-defined]
            for spec in plan.grants  # type: ignore[attr-defined]
            if spec.capability == "github.pull_request.create"  # type: ignore[attr-defined]
        )

    default = plan_bundle(CODE_EDITING, catalog=catalog(), connections=[github(), sandbox()])
    assert base_of(default) == "*"
    main = plan_bundle(
        CODE_EDITING, catalog=catalog(), connections=[github(), sandbox()], base="main"
    )
    assert base_of(main) == "main"
    broken = plan_bundle(
        CODE_EDITING, catalog=catalog(), connections=[github(), sandbox()], base="main branch"
    )
    assert broken.refusals == ("base must be a branch name or pattern such as main or release/*.",)


def test_rules_are_only_added_when_nothing_speaks_for_the_capability() -> None:
    spoken = plan_bundle(
        CODE_EDITING,
        catalog=catalog(),
        connections=[github(), sandbox()],
        existing_rules=[
            PolicyRule(capability="cli.repository.push", risk=None, action=RuleAction.AUTO)
        ],
    )
    assert spoken.rules == ()


def test_a_tool_the_catalog_lacks_is_a_catalog_need() -> None:
    trimmed = [definition for definition in catalog() if definition.name != "github.check.read"]
    plan = plan_bundle(GITHUB_READ, catalog=trimmed, connections=[github()])
    assert Need(kind="catalog", connector_type="github", detail="github.check.read") in plan.needs


def test_every_emitted_row_passes_the_evaluator_inside_its_scope() -> None:
    by_name = {definition.name: definition for definition in catalog()}
    plan = plan_bundle(
        CODE_EDITING, catalog=catalog(), connections=[github(), sandbox()], base="main"
    )
    grants = [
        Grant(capability=spec.capability, scope=spec.scope, effect=GrantEffect.ALLOW)
        for spec in plan.grants
    ]
    rules = list(plan.rules)

    push = evaluate(
        by_name["cli.repository.push"],
        grants=grants,
        rules=rules,
        requested_scope={"connection_id": CLI, "repository": "octo/alpha", "branch": "agent/fix"},
    )
    assert push.decision is DecisionType.REQUIRE_APPROVAL
    to_main = evaluate(
        by_name["cli.repository.push"],
        grants=grants,
        rules=rules,
        requested_scope={"connection_id": CLI, "repository": "octo/alpha", "branch": "main"},
    )
    assert to_main.code == "scope_mismatch"

    pr = evaluate(
        by_name["github.pull_request.create"],
        grants=grants,
        rules=rules,
        requested_scope={
            "connection_id": GH,
            "repository": "octo/alpha",
            "head": "agent/fix",
            "base": "main",
        },
    )
    assert pr.decision is not DecisionType.DENY
    release = evaluate(
        by_name["github.pull_request.create"],
        grants=grants,
        rules=rules,
        requested_scope={
            "connection_id": GH,
            "repository": "octo/alpha",
            "head": "agent/fix",
            "base": "release",
        },
    )
    assert release.code == "scope_mismatch"

    for spec in plan.grants:
        assert (
            grant_problems(
                capability=spec.capability,
                scope=spec.scope,
                effect=spec.effect,
                catalog=catalog(),
                connections=[github(), sandbox()],
            )
            == ()
        )


# --- grant_problems -------------------------------------------------------


def test_problem_kind_one_no_match_with_a_hint() -> None:
    problems = grant_problems(
        capability="github.repository.raed",
        scope={},
        effect="allow",
        catalog=catalog(),
        connections=[],
    )
    assert problems == (
        "Matches no tool in this workspace's catalog. Did you mean github.repository.read?",
    )
    assert grant_problems(
        capability="mcp.demo.something",
        scope={},
        effect="allow",
        catalog=catalog(),
        connections=[],
    ) == ("Matches no tool in this workspace's catalog.",)


def test_problem_kind_two_wildcards_cannot_carry_required_scope() -> None:
    problems = grant_problems(
        capability="cli.*",
        scope={"connection_id": CLI},
        effect="allow",
        catalog=catalog(),
        connections=[sandbox()],
    )
    assert problems[0] == (
        "A wildcard grant cannot carry the scope ['cli.repository.checkout', "
        "'cli.repository.push'] require. Grant those capabilities by name, or turn on the "
        "Code editing capability."
    )


def test_problem_kind_three_required_keys_missing() -> None:
    problems = grant_problems(
        capability="cli.repository.push",
        scope={"connection_id": CLI, "repository": "*"},
        effect="allow",
        catalog=catalog(),
        connections=[sandbox()],
    )
    assert problems == (
        "cli.repository.push needs branch in its grant scope; a grant without it is refused "
        "on every call.",
    )
    # A read tool has no required keys, so a bare connection pin is fine.
    assert (
        grant_problems(
            capability="github.repository.read",
            scope={"connection_id": GH},
            effect="allow",
            catalog=catalog(),
            connections=[github()],
        )
        == ()
    )


def test_problem_kind_four_unknown_scope_key() -> None:
    problems = grant_problems(
        capability="github.repository.read",
        scope={"connection_id": GH, "branch": "main"},
        effect="allow",
        catalog=catalog(),
        connections=[github()],
    )
    assert problems == (
        "'branch' is not a scope key of github.repository.read (known keys: "
        "['connection_id', 'repository']).",
    )


def test_problem_kind_five_connection_problems() -> None:
    def problem(connections: list[ConnectionRef], connection_id: str = GH) -> tuple[str, ...]:
        return grant_problems(
            capability="github.repository.read",
            scope={"connection_id": connection_id},
            effect="allow",
            catalog=catalog(),
            connections=connections,
        )

    assert problem([]) == ("Connection no longer exists.",)
    assert problem([github()], "not-a-uuid") == ("Connection no longer exists.",)
    assert problem([web(GH)]) == ("Connection 'Web' is a web connection, not github.",)
    assert problem([github(status="disabled")]) == ("Connection 'GitHub' is disabled.",)
    assert problem([github(status="needs_reauth")]) == (
        "Connection 'GitHub' needs to be reconnected.",
    )
    kinds = [
        detail.kind
        for detail in grant_problem_details(
            capability="github.repository.read",
            scope={"connection_id": GH},
            effect="allow",
            catalog=catalog(),
            connections=[github(status="error")],
        )
    ]
    assert kinds == ["connection_needs_reconnect"]


def test_problem_kind_six_repository_shape() -> None:
    problems = grant_problems(
        capability="github.repository.read",
        scope={"connection_id": GH, "repository": "../x"},
        effect="allow",
        catalog=catalog(),
        connections=[github()],
    )
    assert problems == ("repository must be owner/name, owner/*, or *.",)


def test_deny_grants_carry_no_problems() -> None:
    assert (
        grant_problems(
            capability="nothing.at.all",
            scope={"bogus": "x"},
            effect="deny",
            catalog=catalog(),
            connections=[],
        )
        == ()
    )


# --- bundle_state ---------------------------------------------------------


def test_bundle_state_is_scope_aware_through_problems() -> None:
    plan = plan_bundle(CODE_EDITING, catalog=catalog(), connections=[github(), sandbox()])
    complete = [(spec.capability, spec.scope, "allow", ()) for spec in plan.grants]
    assert bundle_state(CODE_EDITING, grants=complete, catalog=catalog()) == "on"

    # A bare checkout grant carries a problem, so it does not count.
    bare = [
        (
            "cli.repository.checkout",
            {},
            "allow",
            (
                "cli.repository.checkout needs connection_id, repository in its grant scope; a "
                "grant without it is refused on every call.",
            ),
        ),
        ("github.repository.read", {"connection_id": GH}, "allow", ()),
    ]
    assert bundle_state(CODE_EDITING, grants=bare, catalog=catalog()) == "partial"
    assert bundle_state(CODE_EDITING, grants=[], catalog=catalog()) == "off"
    assert bundle_state(GITHUB_READ, grants=bare, catalog=catalog()) == "partial"
    assert bundle_state(WEB_ACCESS, grants=bare, catalog=catalog()) == "off"


def test_problem_kind_seven_a_repository_the_sandbox_does_not_allow() -> None:
    """The sandbox's allow-list is the outer limit under every grant on it,
    so a hand-made row wider than it carries the planner's own refusal."""

    def problem(scope: dict[str, str], *connections: ConnectionRef) -> tuple[str, ...]:
        return grant_problems(
            capability="cli.repository.checkout",
            scope=scope,
            effect="allow",
            catalog=catalog(),
            connections=list(connections),
        )

    narrow = sandbox(allowed=("octo/a",))
    outside = (
        "'Sandbox' allows only: octo/a — '*' is outside it. Add it to the sandbox's allowed "
        "repositories under Apps, or grant only what the sandbox allows."
    )
    assert problem({"connection_id": CLI, "repository": "*"}, github(), narrow) == (outside,)
    assert problem({"connection_id": CLI, "repository": "octo/a"}, github(), narrow) == ()
    assert problem({"connection_id": CLI, "repository": "octo/b"}, github(), narrow) == (
        outside.replace("'*'", "'octo/b'"),
    )
    assert problem({"connection_id": CLI, "repository": "*"}, github(), sandbox(allowed=())) == (
        "'Sandbox' allows no repositories; list them on the connection under Apps first.",
    )
    # The same width the planner refuses, in the same words.
    plan = plan_bundle(CODE_EDITING, catalog=catalog(), connections=[github(), narrow])
    assert plan.refusals == (outside,)
    assert plan.grants == ()
    # A GitHub connection has no allow-list; a malformed entry is that
    # problem and no other; a dead pin never reaches the allow-list.
    assert (
        grant_problems(
            capability="github.repository.read",
            scope={"connection_id": GH, "repository": "*"},
            effect="allow",
            catalog=catalog(),
            connections=[github()],
        )
        == ()
    )
    kinds = [
        detail.kind
        for detail in grant_problem_details(
            capability="cli.repository.checkout",
            scope={"connection_id": CLI, "repository": "../x"},
            effect="allow",
            catalog=catalog(),
            connections=[github(), narrow],
        )
    ]
    assert kinds == ["repository_invalid"]
    assert problem({"connection_id": CLI2, "repository": "*"}, github(), narrow) == (
        "Connection no longer exists.",
    )


def test_problem_kind_eight_a_branch_the_push_tool_refuses() -> None:
    """``main``, ``master`` and ``HEAD`` are refused inside the sandbox on
    every push, so a push grant naming one exactly can never pass; a
    pattern that also covers them is the admin's own choice and stands."""

    def problem(branch: str) -> tuple[str, ...]:
        return grant_problems(
            capability="cli.repository.push",
            scope={"connection_id": CLI, "repository": "*", "branch": branch},
            effect="allow",
            catalog=catalog(),
            connections=[github(), sandbox()],
        )

    assert problem("main") == (
        "branch 'main' is refused on every push: the sandbox never pushes to main, master or "
        "HEAD. Use a pattern such as agent/*.",
    )
    assert problem("master")[0].startswith("branch 'master' is refused on every push")
    assert problem("HEAD")[0].startswith("branch 'HEAD' is refused on every push")
    assert problem("agent/*") == ()
    assert problem("*") == ()
    assert problem("release/*") == ()
    # ``branch`` on a tool that has no such key is the unknown-key problem.
    kinds = [
        detail.kind
        for detail in grant_problem_details(
            capability="github.repository.read",
            scope={"connection_id": GH, "branch": "main"},
            effect="allow",
            catalog=catalog(),
            connections=[github()],
        )
    ]
    assert kinds == ["unknown_scope_key"]


def test_problem_kind_nine_a_blank_scope_value() -> None:
    """A blank value matches only itself, so the evaluator denies every real
    call while the row looks granted. The bundle planner already refused it
    for the keys it validates; the shared problem list refuses it for every
    key, on every writer, before it is saved."""
    from jhin_policy.bundles import REFUSED_PROBLEM_KINDS, grant_problem_details

    assert "scope_value_blank" in REFUSED_PROBLEM_KINDS

    def kinds(capability: str, scope: dict[str, object], *connections: object) -> list[str]:
        return [
            detail.kind
            for detail in grant_problem_details(
                capability=capability,
                scope=scope,
                effect="allow",
                catalog=catalog(),
                connections=list(connections),  # type: ignore[arg-type]
            )
        ]

    assert kinds(
        "github.pull_request.create",
        {"connection_id": GH, "repository": "*", "base": " "},
        github(),
    ) == ["scope_value_blank"]
    assert kinds("cli.file.read", {"connection_id": CLI, "path": ""}, github(), sandbox()) == [
        "scope_value_blank"
    ]
    assert kinds(
        "cli.repository.push",
        {"connection_id": CLI, "repository": "*", "branch": "  "},
        github(),
        sandbox(),
    ) == ["scope_value_blank"]
    (problem,) = grant_problems(
        capability="github.pull_request.create",
        scope={"connection_id": GH, "repository": "*", "base": ""},
        effect="allow",
        catalog=catalog(),
        connections=[github()],
    )
    assert problem == "'base' is blank; a scope value must be a name or a pattern such as *."
    assert (
        kinds(
            "github.pull_request.create",
            {"connection_id": GH, "repository": "*", "base": "main"},
            github(),
        )
        == []
    )


def test_neutral_problem_text_leaves_the_connection_inventory_out() -> None:
    """A sentence that names a connection, its status or an allow-list is
    the inventory GET /connections keeps behind the admin role; its neutral
    form says the row is dead and why in kind, and nothing else."""
    from jhin_policy.bundles import GrantProblem, neutral_problem_text

    named = GrantProblem(kind="connection_disabled", text="Connection 'GitHub (off)' is disabled.")
    assert neutral_problem_text(named) == "The pinned connection is disabled."
    outside = GrantProblem(
        kind="repository_outside_allow_list",
        text="'Sandbox' allows only: octo/a -- '*' is outside it.",
    )
    assert neutral_problem_text(outside) == "Outside the sandbox's allowed repositories."
    plain = GrantProblem(kind="connection_missing", text="Connection no longer exists.")
    assert neutral_problem_text(plain) == plain.text
