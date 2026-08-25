"""Canonical API-key scope taxonomy (docs/architecture/api-keys.md).

One module owns the whole vocabulary: the API validates against it, the API
serves it to the web client, and the web client renders the scope tree from
what it is served. Nothing else may define a scope string.

A scope is ``<category>:<action>``. ``<category>:*`` is a wildcard that grants
every action in that category (there is deliberately no global ``*``).

Every scope declares the *minimum workspace role* that may hold it. That is
the mechanism behind the ceiling rule: an API key's effective permission is
``intersection(key scopes, scopes allowed for the key's role ceiling)``, so a
member's key can never carry an admin scope no matter what was requested.
"""

from __future__ import annotations

from dataclasses import dataclass

from jhin_domain.enums import WorkspaceRole, role_satisfies

WILDCARD_ACTION = "*"


@dataclass(frozen=True, slots=True)
class ScopeDefinition:
    """One granular scope: its identity, human copy, and role floor."""

    key: str
    category: str
    action: str
    label: str
    description: str
    min_role: WorkspaceRole


@dataclass(frozen=True, slots=True)
class ScopeCategory:
    """A group of related scopes, rendered as one expandable branch."""

    key: str
    label: str
    description: str


_CATEGORY_ROWS: tuple[tuple[str, str, str], ...] = (
    ("workspace", "Workspace", "The workspace itself: its details and its settings."),
    ("members", "People", "Who belongs to the workspace and what they may do."),
    ("agents", "Agents", "The agents in your company and how they are configured."),
    ("teams", "Teams", "How agents are grouped into teams."),
    ("chats", "Chats", "Conversations between people and agents."),
    ("tasks", "Tasks", "Work given to agents, and steering it while it runs."),
    ("runs", "Runs", "The execution history behind tasks: steps, tools, and timings."),
    ("apps", "Apps", "Connections to outside services such as GitHub or Slack."),
    ("automations", "Automations", "Triggers that start work on a schedule or an event."),
    ("skills", "Skills", "The reusable instruction packs agents can load."),
    ("memories", "Memories", "Curated long-term knowledge the company remembers."),
    ("approvals", "Approvals", "Actions paused until a person says yes or no."),
    ("reviews", "Reviews", "Second looks at agent work, and the policies that ask for them."),
    ("models", "Models", "AI model providers, profiles, and budgets."),
    ("spend", "Spend", "What the workspace is spending on model usage."),
    ("audit", "Audit log", "The permanent record of who changed what."),
    ("api_keys", "API keys", "Programmatic keys for this workspace."),
)

CATEGORIES: tuple[ScopeCategory, ...] = tuple(
    ScopeCategory(key=key, label=label, description=description)
    for key, label, description in _CATEGORY_ROWS
)

CATEGORY_BY_KEY: dict[str, ScopeCategory] = {category.key: category for category in CATEGORIES}


_SCOPE_ROWS: tuple[tuple[str, str, str, str, WorkspaceRole], ...] = (
    # category, action, label, description, min_role
    (
        "workspace",
        "read",
        "Read the workspace",
        "See the workspace name, settings, org chart, and people directory.",
        WorkspaceRole.VIEWER,
    ),
    (
        "workspace",
        "settings",
        "Change workspace settings",
        "Rename the workspace and change its timezone, budgets, and limits.",
        WorkspaceRole.ADMIN,
    ),
    (
        "members",
        "read",
        "See people",
        "List members, their roles, and pending invitations.",
        WorkspaceRole.VIEWER,
    ),
    (
        "members",
        "write",
        "Manage people",
        "Invite people, change roles, revoke invitations, and remove members.",
        WorkspaceRole.ADMIN,
    ),
    (
        "agents",
        "read",
        "See agents",
        "List agents and read their configuration, avatars, and reporting lines.",
        WorkspaceRole.VIEWER,
    ),
    (
        "agents",
        "write",
        "Manage agents",
        "Create, edit, pause, resume, and delete agents.",
        WorkspaceRole.ADMIN,
    ),
    (
        "agents",
        "admin",
        "Change what agents may do",
        "Grant or revoke an agent's capabilities and edit its autonomy policy.",
        WorkspaceRole.ADMIN,
    ),
    ("teams", "read", "See teams", "List teams and their members.", WorkspaceRole.VIEWER),
    (
        "teams",
        "write",
        "Manage teams",
        "Create, rename, and delete teams.",
        WorkspaceRole.ADMIN,
    ),
    (
        "chats",
        "read",
        "Read chats",
        "Read conversations and their messages.",
        WorkspaceRole.VIEWER,
    ),
    (
        "chats",
        "write",
        "Chat with agents",
        "Start conversations and send messages, which starts agent work.",
        WorkspaceRole.MEMBER,
    ),
    (
        "tasks",
        "read",
        "Read tasks",
        "List tasks and read their details, timelines, and messages.",
        WorkspaceRole.VIEWER,
    ),
    (
        "tasks",
        "write",
        "Start and steer tasks",
        "Give agents work, and pause, resume, cancel, or instruct a running task.",
        WorkspaceRole.MEMBER,
    ),
    (
        "runs",
        "read",
        "Read run history",
        "Read individual agent runs, their steps, tool calls, and cost.",
        WorkspaceRole.VIEWER,
    ),
    (
        "apps",
        "read",
        "See connected apps",
        "List connections and the tools they expose. Never exposes credentials.",
        WorkspaceRole.ADMIN,
    ),
    (
        "apps",
        "write",
        "Manage connected apps",
        "Connect, edit, disable, and disconnect outside services.",
        WorkspaceRole.ADMIN,
    ),
    (
        "automations",
        "read",
        "See automations",
        "List triggers and the invocations they produced.",
        WorkspaceRole.VIEWER,
    ),
    (
        "automations",
        "write",
        "Manage automations",
        "Create, edit, test, enable, and delete triggers.",
        WorkspaceRole.ADMIN,
    ),
    (
        "skills",
        "read",
        "See skills",
        "Browse the skill library and read skill contents.",
        WorkspaceRole.VIEWER,
    ),
    (
        "skills",
        "write",
        "Manage skills",
        "Install, import, edit, and remove skills, and assign them to agents.",
        WorkspaceRole.ADMIN,
    ),
    (
        "memories",
        "read",
        "Read memories",
        "Read the workspace's curated long-term memory.",
        WorkspaceRole.VIEWER,
    ),
    (
        "memories",
        "write",
        "Add memories",
        "Create, edit, pin, and contest memory records.",
        WorkspaceRole.MEMBER,
    ),
    (
        "memories",
        "admin",
        "Curate memories",
        "Approve, reject, forget, and de-duplicate memory records.",
        WorkspaceRole.ADMIN,
    ),
    (
        "approvals",
        "read",
        "See approvals",
        "List actions waiting on a human decision.",
        WorkspaceRole.VIEWER,
    ),
    (
        "approvals",
        "decide",
        "Decide approvals",
        "Approve or reject a paused action, which lets the agent proceed.",
        WorkspaceRole.MEMBER,
    ),
    (
        "reviews",
        "read",
        "See reviews",
        "Read work reviews, review policies, and work requests.",
        WorkspaceRole.VIEWER,
    ),
    (
        "reviews",
        "decide",
        "Decide reviews",
        "Submit a review verdict and answer work requests.",
        WorkspaceRole.MEMBER,
    ),
    (
        "reviews",
        "write",
        "Manage review policies",
        "Create, edit, and delete the policies that ask for a second look.",
        WorkspaceRole.ADMIN,
    ),
    (
        "models",
        "read",
        "See model setup",
        "List model providers and profiles. Never exposes provider keys.",
        WorkspaceRole.ADMIN,
    ),
    (
        "models",
        "write",
        "Manage model setup",
        "Add and edit model providers, profiles, and pricing.",
        WorkspaceRole.ADMIN,
    ),
    (
        "spend",
        "read",
        "See spend",
        "Read what the workspace has spent on model usage, and its budget.",
        WorkspaceRole.VIEWER,
    ),
    (
        "audit",
        "read",
        "Read the audit log",
        "Read the permanent record of who changed what, and when.",
        WorkspaceRole.ADMIN,
    ),
    (
        "api_keys",
        "read",
        "See API keys",
        "List this workspace's API keys and their usage log.",
        WorkspaceRole.VIEWER,
    ),
    (
        "api_keys",
        "write",
        "Manage API keys",
        "Create and revoke API keys, never above your own role.",
        WorkspaceRole.VIEWER,
    ),
)

SCOPES: tuple[ScopeDefinition, ...] = tuple(
    ScopeDefinition(
        key=f"{category}:{action}",
        category=category,
        action=action,
        label=label,
        description=description,
        min_role=min_role,
    )
    for category, action, label, description, min_role in _SCOPE_ROWS
)

SCOPE_BY_KEY: dict[str, ScopeDefinition] = {scope.key: scope for scope in SCOPES}

ALL_SCOPE_KEYS: frozenset[str] = frozenset(SCOPE_BY_KEY)

CATEGORY_SCOPES: dict[str, tuple[ScopeDefinition, ...]] = {
    category.key: tuple(scope for scope in SCOPES if scope.category == category.key)
    for category in CATEGORIES
}

# Every string a caller may legitimately send, granular plus wildcards.
GRANTABLE_SCOPE_KEYS: frozenset[str] = ALL_SCOPE_KEYS | frozenset(
    f"{category.key}:{WILDCARD_ACTION}" for category in CATEGORIES
)


def is_known_scope(scope: str) -> bool:
    """True for a granular scope or a category wildcard in the taxonomy."""
    return scope in GRANTABLE_SCOPE_KEYS


def expand_scopes(scopes: object) -> frozenset[str]:
    """Resolve requested scope strings to the granular scopes they mean.

    Unknown strings are dropped rather than raising: stored keys must keep
    working (with strictly *less* power) if a scope is ever retired. Input
    validation at the API boundary is what rejects unknown scopes up front.
    """
    if not isinstance(scopes, list | tuple | set | frozenset):
        return frozenset()
    resolved: set[str] = set()
    for entry in scopes:
        if not isinstance(entry, str):
            continue
        candidate = entry.strip()
        if candidate in ALL_SCOPE_KEYS:
            resolved.add(candidate)
            continue
        category, separator, action = candidate.partition(":")
        if separator and action == WILDCARD_ACTION and category in CATEGORY_SCOPES:
            resolved.update(scope.key for scope in CATEGORY_SCOPES[category])
    return frozenset(resolved)


def scopes_for_role(role: WorkspaceRole) -> frozenset[str]:
    """Every granular scope a holder of ``role`` is allowed to exercise."""
    return frozenset(scope.key for scope in SCOPES if role_satisfies(role, scope.min_role))


def effective_scopes(granted: object, role_ceiling: WorkspaceRole) -> frozenset[str]:
    """The ceiling rule, in one place.

    Effective permission is the intersection of the scopes written on the key
    and the scopes the key's role ceiling permits. Nothing else in the system
    may compute this differently.
    """
    return expand_scopes(granted) & scopes_for_role(role_ceiling)


def scopes_above_role(granted: object, role: WorkspaceRole) -> tuple[str, ...]:
    """Explicitly named scopes that ``role`` may not hold, for a helpful 422.

    Wildcards are deliberately exempt. ``memories:*`` means "everything in
    memories that I am allowed to delegate", so a member asking for it should
    get a member-shaped key, not an error about a scope they never typed.
    Naming ``memories:admin`` outright is a different act — the person is
    asking for something specific they cannot have, and should be told so.
    Either way the ceiling still applies: :func:`effective_scopes` caps the
    result, so the difference is only in the wording of the outcome.
    """
    allowed = scopes_for_role(role)
    explicit = (
        {entry.strip() for entry in granted if isinstance(entry, str)} & ALL_SCOPE_KEYS
        if isinstance(granted, list | tuple | set | frozenset)
        else set()
    )
    return tuple(sorted(explicit - allowed))
