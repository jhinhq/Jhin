"""Capability bundles: the named sets of grants an admin turns on with one
action, and the planner that turns a bundle into complete grant rows.

A bundle says *which tools* and *which fixed scope values* ("branch
``agent/*``", "path ``*``"); it never names a connection. The planner fills
the connection in from the workspace's live connections, fills the
repositories from what the operator asked for, and refuses — by sentence —
anything the evaluator would later deny. The postcondition every caller
relies on: **no row the planner emits carries a problem**
(:func:`grant_problems` is empty for each of them), so a bundle can never
write the dead grant a hand-made ``POST /grants`` used to be able to.

This module performs no I/O. The API loads the catalog and the connections
and calls :func:`plan_bundle`; the same plan is what the CLI writes.
"""

from __future__ import annotations

import difflib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import UUID

from jhin_policy.capabilities import ToolDefinition, capability_matches
from jhin_policy.evaluator import PolicyRule
from jhin_policy.repositories import is_repository_pattern, repository_covered_by_allow_list
from jhin_policy.risk import RuleAction

# Connector types that are not connectors at all: their tools are built in
# and never pin a connection, so a bundle made of them needs nothing set up.
_BUILTIN_TOOL_PREFIXES = frozenset({"organization", "skills", "memory"})

# The branch pattern the Code editing bundle fixes on pushes. Never
# overridable: ``agent/*`` is what keeps a push off ``main``.
AGENT_BRANCH_PATTERN = "agent/*"

MAX_REPOSITORIES = 50
MAX_REPOSITORIES_SENTENCE = f"At most {MAX_REPOSITORIES} repositories can be granted at once."
NO_REPOSITORIES_SENTENCE = "List at least one repository, or * for every repository."
_MAX_BASE_CHARS = 200

# Branch names the push tool refuses inside the sandbox on every call
# (``jhin_connectors.cli.tools``: ``case $branch in main|master|HEAD``), so a
# push grant naming one of them exactly is a grant that can never pass.
PROTECTED_BRANCHES: frozenset[str] = frozenset({"main", "master", "HEAD"})

ACTIVE = "active"
DISABLED = "disabled"
_NEEDS_RECONNECT = frozenset({"needs_reauth", "error"})


@dataclass(frozen=True)
class Bundle:
    id: str
    label: str
    summary: str
    description: str
    # tool name -> fixed scope values; never ``connection_id``.
    tools: Mapping[str, Mapping[str, str]]
    policy_rules: tuple[PolicyRule, ...] = ()
    not_included: tuple[str, ...] = ()
    # Derived at import: the first segment of each tool name, minus the
    # built-in prefixes. What the bundle needs a connection *of*.
    connector_types: tuple[str, ...] = field(init=False)

    def __post_init__(self) -> None:
        types: list[str] = []
        for name in self.tools:
            prefix = name.split(".", 1)[0]
            if prefix in _BUILTIN_TOOL_PREFIXES or prefix in types:
                continue
            types.append(prefix)
        object.__setattr__(self, "connector_types", tuple(types))

    @property
    def is_connector_bundle(self) -> bool:
        return bool(self.connector_types)


COLLABORATION_BUNDLE_ID = "collaboration"
GITHUB_READ_BUNDLE_ID = "github-read"
CODE_EDITING_BUNDLE_ID = "code-editing"
WEB_ACCESS_BUNDLE_ID = "web-access"

_ANY_REPOSITORY_SCOPE: Mapping[str, str] = {"repository": "*"}

BUNDLES: tuple[Bundle, ...] = (
    Bundle(
        id=COLLABORATION_BUNDLE_ID,
        label="Collaboration",
        summary=(
            "Work with teammates: find colleagues, see what they are working on, ask them "
            "for help, and answer their requests"
        ),
        description=(
            "Let this agent look colleagues up in the directory, see what a teammate is "
            "working on right now, ask any teammate for help with a piece of work (they "
            "decide whether to accept), and respond to requests addressed to it. This is "
            "safe by default: it can only read public work status — never a colleague's "
            "instructions, permissions, notes, or conversations — and a request only asks, "
            "so it can never make a colleague do anything they are not already allowed to "
            "do. It is on by default for new agents. It does NOT include delegation, which "
            "transfers authority and stays off unless an admin grants it."
        ),
        tools={
            "organization.directory.search": {},
            "organization.colleague_status": {},
            "organization.request_work": {"targets": "any"},
            "organization.respond_work_request": {},
        },
    ),
    Bundle(
        id=GITHUB_READ_BUNDLE_ID,
        label="GitHub (read)",
        summary=(
            "Read code on GitHub: repositories, branches, files, issues, pull requests, "
            "checks and workflow runs"
        ),
        description=(
            "Look at repositories, branches and files, and read issues, pull requests, "
            "check results and workflow runs through a GitHub connection. Nothing is "
            "written."
        ),
        tools={
            "github.repository.read": _ANY_REPOSITORY_SCOPE,
            "github.branch.list": _ANY_REPOSITORY_SCOPE,
            "github.file.read": _ANY_REPOSITORY_SCOPE,
            "github.pull_request.read": _ANY_REPOSITORY_SCOPE,
            "github.issue.read": _ANY_REPOSITORY_SCOPE,
            "github.check.read": _ANY_REPOSITORY_SCOPE,
            "github.workflow_run.read": _ANY_REPOSITORY_SCOPE,
        },
        not_included=("commenting on issues or pull requests", "creating branches", "merging"),
    ),
    Bundle(
        id=CODE_EDITING_BUNDLE_ID,
        label="Code editing",
        summary="Write code: check out a repo, edit files, run tests, and open pull requests",
        description=(
            "Clone a repository into the sandbox, find your way around it, read and change "
            "files, run tests, and — once a human approves it — push a branch and open a "
            "pull request. Needs a GitHub connection; the CLI Sandbox it runs in is created "
            "for you when the capability is turned on. Running tests means running a "
            "command the agent chose, inside the checkout, so it can change files there — "
            "but it never holds the git credential, and the push tool re-checks the "
            "repository against what Jhin recorded at checkout rather than trusting "
            "anything the sandbox left behind."
        ),
        tools={
            "cli.repository.checkout": {"repository": "*"},
            "cli.file.list": {"path": "*"},
            "cli.file.search": {"path": "*"},
            "cli.file.read": {"path": "*"},
            "cli.file.edit": {"path": "*"},
            "cli.file.write": {"path": "*"},
            "cli.test.run": {"command": "*"},
            "cli.repository.push": {"repository": "*", "branch": AGENT_BRANCH_PATTERN},
            "github.repository.read": {"repository": "*"},
            "github.pull_request.read": {"repository": "*"},
            "github.pull_request.create": {"repository": "*", "base": "*"},
        },
        # Pushing to a real repository is the first thing that leaves the
        # sandbox, so it asks — even for an Autonomous agent, where the
        # ELEVATED risk level alone would run it unattended.
        policy_rules=(
            PolicyRule(capability="cli.repository.push", risk=None, action=RuleAction.APPROVAL),
        ),
        not_included=(
            "running arbitrary commands (cli.command.execute)",
            "pushing to main",
            "merging pull requests",
        ),
    ),
    Bundle(
        id="team-building",
        label="Team building",
        summary="Hire teammates: create agents and teams (each new hire needs your approval)",
        description=(
            "Create new AI teammates and teams, update teammate profiles, and look up "
            "colleagues in the directory. A human must approve each new teammate, and new "
            "teammates start with no tool access."
        ),
        tools={
            "organization.create_agent": {},
            "organization.update_agent_profile": {},
            "organization.create_team": {},
            "organization.directory.search": {},
        },
    ),
    Bundle(
        id=WEB_ACCESS_BUNDLE_ID,
        label="Web search & browsing",
        summary="Research online: search the web and read public pages",
        description=(
            "Search the web and read public pages through a Web connection. Everything that "
            "comes back is untrusted external content; fetch can be limited to specific "
            "domains."
        ),
        tools={"web.search": {}, "web.fetch": {"domain": "*"}},
    ),
    Bundle(
        id="skills",
        label="Skills",
        summary="Follow playbooks: read the skills your workspace publishes",
        description=(
            "Let this agent read the workspace skills library — the playbooks your admins "
            "curate. Grants the skills.read tool for every skill."
        ),
        tools={"skills.read": {"name": "*"}},
    ),
    Bundle(
        id="skill-authoring",
        label="Skill authoring",
        summary="Write playbooks: add and revise skills it wrote itself (with approval)",
        description=(
            "Let this agent write new playbooks to the skills library and revise ones it "
            "wrote, through skills.create and skills.update. Every call needs your approval "
            "first, and it can only ever touch skills it authored itself — never anyone "
            "else's."
        ),
        tools={"skills.create": {}, "skills.update": {}},
    ),
)

BUNDLE_IDS: tuple[str, ...] = tuple(bundle.id for bundle in BUNDLES)


def bundle_by_id(bundle_id: str) -> Bundle | None:
    return next((bundle for bundle in BUNDLES if bundle.id == bundle_id), None)


# --- The planner's inputs and outputs -----------------------------------


@dataclass(frozen=True)
class ConnectionRef:
    """The slice of a workspace connection the planner needs."""

    id: str
    connector_type: str
    name: str
    status: str
    # cli only
    git_connection_id: str = ""
    allowed_repositories: tuple[str, ...] | None = None


NeedKind = Literal["connect", "choose", "create_sandbox", "catalog"]


@dataclass(frozen=True)
class Need:
    """Something the operator has to answer before anything can be written."""

    kind: NeedKind
    connector_type: str
    choices: tuple[ConnectionRef, ...] = ()
    detail: str = ""


@dataclass(frozen=True)
class GrantSpec:
    capability: str
    scope: dict[str, str]
    effect: str = "allow"


@dataclass(frozen=True)
class BundlePlan:
    bundle_id: str
    # Complete rows, de-duplicated on (capability, sorted scope).
    grants: tuple[GrantSpec, ...]
    # Bundle rules not already spoken for.
    rules: tuple[PolicyRule, ...]
    # Non-empty => nothing may be written.
    needs: tuple[Need, ...]
    # Non-empty => 422.
    refusals: tuple[str, ...]


def _connector_type_of(tool_name: str) -> str:
    return tool_name.split(".", 1)[0]


def _describe_status_refusal(connection: ConnectionRef, expected_type: str) -> str | None:
    if connection.connector_type != expected_type:
        return (
            f"'{connection.name}' is a {connection.connector_type} connection, not {expected_type}."
        )
    if connection.status == DISABLED:
        return f"'{connection.name}' is disabled; enable it or pick another connection."
    if connection.status != ACTIVE:
        return f"'{connection.name}' needs to be reconnected before agents can use it."
    return None


def _sandbox_refusal(
    sandbox: ConnectionRef, github: ConnectionRef, names: Mapping[str, str]
) -> str | None:
    if not sandbox.git_connection_id:
        return (
            f"'{sandbox.name}' names no GitHub connection for repository jobs. Set one under "
            "Apps first."
        )
    if sandbox.git_connection_id != github.id:
        other = names.get(sandbox.git_connection_id, sandbox.git_connection_id)
        return (
            f"'{sandbox.name}' uses '{other}' for repository jobs, not "
            f"'{github.name}'. Pick a sandbox that uses this connection, or change its "
            "GitHub connection under Apps first."
        )
    return None


def _validate_repositories(repositories: Sequence[str]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """De-duplicated, bounded repository entries, plus the refusals."""
    refusals: list[str] = []
    entries: list[str] = []
    for raw in repositories:
        entry = raw.strip()
        if entry in entries:
            continue
        if not is_repository_pattern(entry):
            refusals.append(
                f"'{entry}' is not a repository: use owner/name, owner/*, or * for every "
                "repository."
            )
            continue
        entries.append(entry)
    if len(entries) > MAX_REPOSITORIES:
        refusals.append(MAX_REPOSITORIES_SENTENCE)
        entries = entries[:MAX_REPOSITORIES]
    if not entries and not refusals:
        refusals.append(NO_REPOSITORIES_SENTENCE)
    return tuple(entries), tuple(refusals)


def sandbox_allow_list_problems(sandbox: ConnectionRef, entries: Sequence[str]) -> tuple[str, ...]:
    """The repository entries a CLI Sandbox's allow-list does not cover, as
    the sentences that refuse them — the same ones whether the row comes
    from the planner or from an admin's own hand.

    A sandbox with no allow-list can check out nothing, so every entry is
    outside it; otherwise each entry must be covered
    (:func:`repository_covered_by_allow_list`).
    """
    allowed = sandbox.allowed_repositories or ()
    if not allowed:
        return (
            f"'{sandbox.name}' allows no repositories; list them on the connection under "
            "Apps first.",
        )
    return tuple(
        f"'{sandbox.name}' allows only: {', '.join(sorted(allowed))} — '{entry}' is outside "
        "it. Add it to the sandbox's allowed repositories under Apps, or grant only what the "
        "sandbox allows."
        for entry in entries
        if not repository_covered_by_allow_list(entry, allowed)
    )


def _validate_base(base: str | None) -> tuple[str, str | None]:
    if base is None:
        return "*", None
    value = base.strip()
    if not value or any(char.isspace() for char in value) or len(value) > _MAX_BASE_CHARS:
        return value, "base must be a branch name or pattern such as main or release/*."
    return value, None


def plan_bundle(
    bundle: Bundle,
    *,
    catalog: Sequence[ToolDefinition],
    connections: Sequence[ConnectionRef],
    existing_rules: Sequence[PolicyRule] = (),
    chosen: Mapping[str, str] | None = None,
    repositories: Sequence[str] = ("*",),
    base: str | None = None,
) -> BundlePlan:
    """Turn a bundle into complete grant rows against live connections.

    Every ``connection_id`` is resolved from ``chosen`` or auto-picked when
    exactly one active connection qualifies; ``repository`` yields one row
    per requested entry; ``branch`` is fixed; ``base`` is validated. A
    question the planner cannot answer becomes a :class:`Need`; a value it
    must not accept becomes a refusal sentence. Either one means nothing is
    written.
    """
    chosen = dict(chosen or {})
    by_id = {connection.id: connection for connection in connections}
    names = {connection.id: connection.name for connection in connections}
    catalog_by_name = {definition.name: definition for definition in catalog}

    needs: list[Need] = []
    refusals: list[str] = []
    resolved: dict[str, ConnectionRef] = {}

    present_tools = [
        (name, catalog_by_name[name]) for name in bundle.tools if name in catalog_by_name
    ]
    for name in bundle.tools:
        if name not in catalog_by_name:
            needs.append(Need(kind="catalog", connector_type=_connector_type_of(name), detail=name))

    needed_types = []
    for name, definition in present_tools:
        if "connection_id" in definition.scope_keys:
            connector_type = _connector_type_of(name)
            if connector_type not in needed_types:
                needed_types.append(connector_type)
    # A sandbox is only ever chosen relative to the GitHub connection it
    # borrows its credential from, so GitHub resolves first.
    needed_types.sort(key=lambda kind: (kind != "github", kind == "cli"))

    for connector_type in needed_types:
        github = resolved.get("github")
        if connector_type == "cli" and "github" in needed_types and github is None:
            # Nothing to choose a sandbox against until GitHub is answered.
            continue
        chosen_id = chosen.get(connector_type)
        if chosen_id is not None:
            connection = by_id.get(chosen_id)
            if connection is None:
                refusals.append(f"'{chosen_id}' is not a connection in this workspace.")
                continue
            refusal = _describe_status_refusal(connection, connector_type)
            if refusal is None and connector_type == "cli" and github is not None:
                refusal = _sandbox_refusal(connection, github, names)
            if refusal is not None:
                refusals.append(refusal)
                continue
            resolved[connector_type] = connection
            continue
        candidates = [
            connection
            for connection in connections
            if connection.connector_type == connector_type and connection.status == ACTIVE
        ]
        if connector_type == "cli" and github is not None:
            candidates = [
                connection for connection in candidates if connection.git_connection_id == github.id
            ]
        if len(candidates) == 1:
            resolved[connector_type] = candidates[0]
        elif not candidates:
            if connector_type == "cli" and github is not None:
                needs.append(Need(kind="create_sandbox", connector_type="cli", choices=(github,)))
            elif connector_type == "cli":
                active_github = tuple(
                    connection
                    for connection in connections
                    if connection.connector_type == "github" and connection.status == ACTIVE
                )
                needs.append(
                    Need(kind="create_sandbox", connector_type="cli", choices=active_github)
                )
            else:
                needs.append(Need(kind="connect", connector_type=connector_type))
        else:
            needs.append(
                Need(kind="choose", connector_type=connector_type, choices=tuple(candidates))
            )

    entries, repository_refusals = _validate_repositories(repositories)
    uses_repository = any("repository" in definition.scope_keys for _, definition in present_tools)
    if uses_repository:
        refusals.extend(repository_refusals)
    base_value, base_refusal = _validate_base(base)
    if base_refusal is not None and any(
        "base" in definition.scope_keys for _, definition in present_tools
    ):
        refusals.append(base_refusal)

    # An entry outside the sandbox's allow-list is not checked here by hand:
    # it is a problem of the row itself (:func:`sandbox_allow_list_problems`,
    # through :func:`grant_problems`), and the postcondition below turns it
    # into a refusal exactly as it would for a hand-made row.
    grants: list[GrantSpec] = []
    seen: set[tuple[str, tuple[tuple[str, str], ...]]] = set()
    for name, definition in present_tools:
        fixed = bundle.tools[name]
        connector_type = _connector_type_of(name)
        if "connection_id" in definition.scope_keys and connector_type not in resolved:
            continue
        base_scope: dict[str, str] = {}
        for key in definition.scope_keys:
            if key == "connection_id":
                base_scope[key] = resolved[connector_type].id
            elif key == "branch":
                base_scope[key] = AGENT_BRANCH_PATTERN
            elif key == "base":
                base_scope[key] = base_value or "*"
            elif key == "repository":
                continue
            elif key in fixed:
                base_scope[key] = fixed[key]
        variants = (
            [{**base_scope, "repository": entry} for entry in entries]
            if "repository" in definition.scope_keys
            else [base_scope]
        )
        for scope in variants:
            signature = (definition.required_capability, tuple(sorted(scope.items())))
            if signature in seen:
                continue
            seen.add(signature)
            grants.append(GrantSpec(capability=definition.required_capability, scope=scope))

    # The postcondition, checked at runtime as well as in tests: a row the
    # planner would itself flag is never handed back as writable.
    for spec in grants:
        for problem in grant_problems(
            capability=spec.capability,
            scope=spec.scope,
            effect=spec.effect,
            catalog=catalog,
            connections=connections,
        ):
            if problem not in refusals:
                refusals.append(problem)

    spoken_for = {rule.capability for rule in existing_rules}
    rules = tuple(rule for rule in bundle.policy_rules if rule.capability not in spoken_for)

    # A plan with an open question or a refusal in it is not a plan anybody
    # may write from, so it carries no rows at all rather than a partial set
    # a caller might be tempted to apply.
    if needs or refusals:
        grants = []

    return BundlePlan(
        bundle_id=bundle.id,
        grants=tuple(grants),
        rules=rules,
        needs=tuple(needs),
        refusals=tuple(refusals),
    )


# --- Problems: what is wrong with a grant row, in order ------------------

ProblemKind = Literal[
    "no_match",
    "wildcard_required_scope",
    "required_scope_missing",
    "unknown_scope_key",
    "connection_missing",
    "connection_wrong_type",
    "connection_disabled",
    "connection_needs_reconnect",
    "repository_invalid",
    "repository_outside_allow_list",
    "branch_refused",
    "scope_value_blank",
]

# Problems a grant writer must refuse outright. The two that are not here —
# no catalog match (MCP servers register tools after the connection exists)
# and a connection that is merely off or lapsed — describe a row that is
# dead *today* and may come back, so they are written and reported instead.
REFUSED_PROBLEM_KINDS: frozenset[str] = frozenset(
    {
        "wildcard_required_scope",
        "required_scope_missing",
        "unknown_scope_key",
        "connection_missing",
        "connection_wrong_type",
        "repository_invalid",
        "repository_outside_allow_list",
        "branch_refused",
        "scope_value_blank",
    }
)


@dataclass(frozen=True)
class GrantProblem:
    kind: ProblemKind
    text: str


def _is_wildcard(capability: str) -> bool:
    return capability == "*" or capability.endswith(".*")


def grant_problem_details(
    *,
    capability: str,
    scope: Mapping[str, Any],
    effect: str,
    catalog: Sequence[ToolDefinition],
    connections: Sequence[ConnectionRef],
) -> tuple[GrantProblem, ...]:
    """Everything wrong with one allow grant, each as a sentence an operator
    can act on. Deny grants carry no problems: a deny that matches nothing
    denies nothing, which is exactly what it says."""
    if effect != "allow":
        return ()
    matched = [
        definition
        for definition in catalog
        if capability_matches(capability, definition.required_capability)
    ]
    if not matched:
        text = "Matches no tool in this workspace's catalog."
        known = sorted({definition.required_capability for definition in catalog})
        closest = difflib.get_close_matches(capability, known, n=1, cutoff=0.8)
        if closest:
            text += f" Did you mean {closest[0]}?"
        return (GrantProblem(kind="no_match", text=text),)

    problems: list[GrantProblem] = []
    scope_keys = set(scope)
    if _is_wildcard(capability):
        requiring = sorted(
            definition.name for definition in matched if definition.required_grant_scope_keys
        )
        if requiring:
            problems.append(
                GrantProblem(
                    kind="wildcard_required_scope",
                    text=(
                        f"A wildcard grant cannot carry the scope {requiring} require. Grant "
                        "those capabilities by name, or turn on the Code editing capability."
                    ),
                )
            )
    for definition in matched:
        missing = [key for key in definition.required_grant_scope_keys if key not in scope_keys]
        if missing:
            problems.append(
                GrantProblem(
                    kind="required_scope_missing",
                    text=(
                        f"{definition.name} needs {', '.join(missing)} in its grant scope; a "
                        "grant without it is refused on every call."
                    ),
                )
            )
    if not all(definition.defers_scope for definition in matched):
        known_keys = sorted({key for definition in matched for key in definition.scope_keys})
        for key in sorted(scope_keys - set(known_keys)):
            problems.append(
                GrantProblem(
                    kind="unknown_scope_key",
                    text=(
                        f"'{key}' is not a scope key of {matched[0].name} "
                        f"(known keys: {known_keys})."
                    ),
                )
            )
    # A blank value matches nothing but itself -- fnmatch('x', '') is False --
    # so the evaluator would deny every real call while the row sat there
    # looking granted. The bundle planner refuses the same value for the keys
    # it validates; this refuses it for every key, on every writer.
    for key in sorted(scope_keys):
        value = scope[key]
        if isinstance(value, str):
            blank = not value.strip()
        elif isinstance(value, list):
            blank = not value or any(isinstance(item, str) and not item.strip() for item in value)
        else:
            blank = False
        if blank:
            problems.append(
                GrantProblem(
                    kind="scope_value_blank",
                    text=(
                        f"'{key}' is blank; a scope value must be a name or a pattern such as *."
                    ),
                )
            )
    # The pinned connection, once it is known to exist and to be of the type
    # the matched tools call through; only then can its own limits apply.
    pinned: ConnectionRef | None = None
    if "connection_id" in scope:
        raw = scope["connection_id"]
        connection: ConnectionRef | None = None
        if isinstance(raw, str):
            try:
                UUID(raw)
            except ValueError:
                connection = None
            else:
                connection = next((item for item in connections if item.id == raw), None)
        expected = _connector_type_of(matched[0].name)
        if connection is None:
            problems.append(
                GrantProblem(kind="connection_missing", text="Connection no longer exists.")
            )
        elif connection.connector_type != expected:
            problems.append(
                GrantProblem(
                    kind="connection_wrong_type",
                    text=(
                        f"Connection '{connection.name}' is a {connection.connector_type} "
                        f"connection, not {expected}."
                    ),
                )
            )
        else:
            pinned = connection
            if connection.status == DISABLED:
                problems.append(
                    GrantProblem(
                        kind="connection_disabled",
                        text=f"Connection '{connection.name}' is disabled.",
                    )
                )
            elif connection.status in _NEEDS_RECONNECT:
                problems.append(
                    GrantProblem(
                        kind="connection_needs_reconnect",
                        text=f"Connection '{connection.name}' needs to be reconnected.",
                    )
                )
    if "repository" in scope:
        value = scope["repository"]
        values = value if isinstance(value, list) else [value]
        if not values or not all(
            isinstance(item, str) and is_repository_pattern(item) for item in values
        ):
            problems.append(
                GrantProblem(
                    kind="repository_invalid",
                    text="repository must be owner/name, owner/*, or *.",
                )
            )
        elif pinned is not None and pinned.connector_type == "cli":
            # The sandbox's allow-list is the outer limit under every grant
            # on it; a row wider than that limit is refused with the same
            # sentence the bundle planner uses for the same width.
            problems.extend(
                GrantProblem(kind="repository_outside_allow_list", text=text)
                for text in sandbox_allow_list_problems(pinned, values)
            )
    if "branch" in scope and any("branch" in definition.scope_keys for definition in matched):
        branch = scope["branch"]
        if isinstance(branch, str) and branch in PROTECTED_BRANCHES:
            problems.append(
                GrantProblem(
                    kind="branch_refused",
                    text=(
                        f"branch '{branch}' is refused on every push: the sandbox never pushes "
                        f"to main, master or HEAD. Use a pattern such as {AGENT_BRANCH_PATTERN}."
                    ),
                )
            )
    return tuple(problems)


_NEUTRAL_PROBLEM_TEXT: Mapping[str, str] = {
    "connection_wrong_type": "The pinned connection is not the kind this tool calls through.",
    "connection_disabled": "The pinned connection is disabled.",
    "connection_needs_reconnect": "The pinned connection needs to be reconnected.",
    "repository_outside_allow_list": "Outside the sandbox's allowed repositories.",
}


def neutral_problem_text(problem: GrantProblem) -> str:
    """The problem's sentence with the connection inventory left out.

    Some sentences name a connection, its status, or a sandbox's allow-list,
    which is exactly what ``GET /connections`` keeps behind the admin role
    and its own scope. A caller who may not read that inventory still gets
    told the row is dead, and why in kind, without the names.
    """
    return _NEUTRAL_PROBLEM_TEXT.get(problem.kind, problem.text)


def grant_problems(
    *,
    capability: str,
    scope: Mapping[str, Any],
    effect: str,
    catalog: Sequence[ToolDefinition],
    connections: Sequence[ConnectionRef],
) -> tuple[str, ...]:
    return tuple(
        problem.text
        for problem in grant_problem_details(
            capability=capability,
            scope=scope,
            effect=effect,
            catalog=catalog,
            connections=connections,
        )
    )


# --- Is a bundle on? ------------------------------------------------------

BundleState = Literal["on", "partial", "off"]

# (capability, scope, effect, problems)
AnnotatedGrant = tuple[str, Mapping[str, Any], str, tuple[str, ...]]


def bundle_capabilities(bundle: Bundle, catalog: Sequence[ToolDefinition]) -> tuple[str, ...]:
    """The capabilities the bundle's tools require, in bundle order, given
    this workspace's catalog (a tool the catalog lacks contributes none)."""
    by_name = {definition.name: definition for definition in catalog}
    capabilities: list[str] = []
    for name in bundle.tools:
        definition = by_name.get(name)
        if definition is None or definition.required_capability in capabilities:
            continue
        capabilities.append(definition.required_capability)
    return tuple(capabilities)


def covered_capabilities(
    capabilities: Sequence[str], grants: Sequence[AnnotatedGrant]
) -> tuple[str, ...]:
    """The capabilities some problem-free allow grant matches."""
    return tuple(
        capability
        for capability in capabilities
        if any(
            effect == "allow" and not problems and capability_matches(pattern, capability)
            for pattern, _scope, effect, problems in grants
        )
    )


def bundle_state(
    bundle: Bundle,
    *,
    grants: Sequence[AnnotatedGrant],
    catalog: Sequence[ToolDefinition],
) -> BundleState:
    """``on`` when every capability is covered by a problem-free allow grant,
    ``partial`` when some are, ``off`` when none are (or the catalog offers
    none of the bundle's tools)."""
    capabilities = bundle_capabilities(bundle, catalog)
    if not capabilities:
        return "off"
    covered = covered_capabilities(capabilities, grants)
    if len(covered) == len(capabilities):
        return "on"
    return "partial" if covered else "off"


__all__ = [
    "AGENT_BRANCH_PATTERN",
    "BUNDLES",
    "BUNDLE_IDS",
    "CODE_EDITING_BUNDLE_ID",
    "COLLABORATION_BUNDLE_ID",
    "GITHUB_READ_BUNDLE_ID",
    "MAX_REPOSITORIES",
    "MAX_REPOSITORIES_SENTENCE",
    "NO_REPOSITORIES_SENTENCE",
    "PROTECTED_BRANCHES",
    "REFUSED_PROBLEM_KINDS",
    "WEB_ACCESS_BUNDLE_ID",
    "AnnotatedGrant",
    "Bundle",
    "BundlePlan",
    "BundleState",
    "ConnectionRef",
    "GrantProblem",
    "GrantSpec",
    "Need",
    "NeedKind",
    "ProblemKind",
    "bundle_by_id",
    "bundle_capabilities",
    "bundle_state",
    "covered_capabilities",
    "grant_problem_details",
    "grant_problems",
    "neutral_problem_text",
    "plan_bundle",
    "sandbox_allow_list_problems",
]
