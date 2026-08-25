"""The one table that says which scope each workspace route requires.

Scope enforcement is central, not per-endpoint: ``require_workspace_role`` (the
dependency every workspace-scoped route already goes through) looks the current
route up here and refuses the request if the key's effective scopes do not
cover it. Adding a route therefore needs one line here, and
``test_route_scopes.py`` fails until it gets one.

The table is keyed by a *signature*: the route template with the
``/api/v1/workspaces/{workspace_id}`` prefix and every ``{path_param}``
removed, so ``/agents/{agent_id}/grants/{grant_id}`` is ``("agents", "grants")``.

``None`` on either side means "no API key may do this, at any scope". That is
reserved for credential material: workspace secrets, connection credentials,
and webhook signing secrets are browser-session-only, forever.
"""

from __future__ import annotations

from dataclasses import dataclass

WORKSPACE_PREFIX = "/api/v1/workspaces/{workspace_id}"
READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


@dataclass(frozen=True, slots=True)
class RouteRule:
    read: str | None
    write: str | None


def _rule(read: str | None, write: str | None) -> RouteRule:
    return RouteRule(read=read, write=write)


# Credential surfaces: present, deliberately unreachable with a bearer key.
_SEALED = _rule(None, None)


ROUTE_SCOPES: dict[tuple[str, ...], RouteRule] = {
    (): _rule("workspace:read", "workspace:settings"),
    # Owner-only by role (see the router); the scope keeps a key from reading
    # a whole-workspace inventory on a read-everything token.
    ("deletion-summary",): _rule("workspace:read", None),
    # The caller's own first-run tour state. The write side takes the settings
    # scope because there is no narrower workspace write scope — a strictly
    # conservative choice: a browser session (which is how the tour is
    # actually used) is unaffected, and a read-everything key cannot quietly
    # mark somebody's introduction as finished.
    ("onboarding",): _rule("workspace:read", "workspace:settings"),
    ("directory",): _rule("workspace:read", None),
    ("org-graph",): _rule("workspace:read", None),
    ("members",): _rule("members:read", "members:write"),
    ("invitations",): _rule("members:read", "members:write"),
    ("agents",): _rule("agents:read", "agents:write"),
    ("agents", "pause"): _rule(None, "agents:write"),
    ("agents", "resume"): _rule(None, "agents:write"),
    ("agents", "grants"): _rule("agents:read", "agents:admin"),
    ("agents", "policy"): _rule("agents:read", "agents:admin"),
    ("agents", "relationships"): _rule("agents:read", "agents:write"),
    ("agents", "memberships"): _rule("teams:read", "teams:write"),
    ("agents", "skills"): _rule("skills:read", "skills:write"),
    ("agents", "avatar"): _rule("agents:read", "agents:write"),
    ("agents", "avatar", "shape"): _rule(None, "agents:write"),
    ("agents", "avatar", "generate"): _rule(None, "agents:write"),
    ("agents", "avatar", "generation"): _rule("agents:read", None),
    ("agents", "rollup"): _rule("agents:read", None),
    ("agents", "assign-task"): _rule(None, "tasks:write"),
    ("agents", "message"): _rule(None, "tasks:write"),
    ("teams",): _rule("teams:read", "teams:write"),
    ("tasks",): _rule("tasks:read", "tasks:write"),
    ("tasks", "tree"): _rule("tasks:read", None),
    ("tasks", "messages"): _rule("tasks:read", None),
    ("tasks", "timeline"): _rule("runs:read", None),
    ("tasks", "acknowledge"): _rule(None, "tasks:write"),
    ("tasks", "cancel"): _rule(None, "tasks:write"),
    ("tasks", "instruction"): _rule(None, "tasks:write"),
    ("tasks", "pause"): _rule(None, "tasks:write"),
    ("tasks", "resume"): _rule(None, "tasks:write"),
    ("runs",): _rule("runs:read", None),
    ("runs", "timeline"): _rule("runs:read", None),
    ("runs", "tool-calls"): _rule("runs:read", None),
    ("conversations",): _rule("chats:read", "chats:write"),
    ("conversations", "messages"): _rule("chats:read", None),
    ("conversations", "turns"): _rule(None, "chats:write"),
    ("conversations", "activity"): _rule("chats:read", None),
    ("activity",): _rule("chats:read", None),
    ("attention",): _rule("tasks:read", None),
    ("attention", "acknowledge-failures"): _rule(None, "tasks:write"),
    ("approvals",): _rule("approvals:read", None),
    ("approvals", "approve"): _rule(None, "approvals:decide"),
    ("approvals", "reject"): _rule(None, "approvals:decide"),
    ("reviews",): _rule("reviews:read", None),
    ("reviews", "decide"): _rule(None, "reviews:decide"),
    ("review-policies",): _rule("reviews:read", "reviews:write"),
    ("work-requests",): _rule("reviews:read", "reviews:decide"),
    ("work-requests", "accept"): _rule(None, "reviews:decide"),
    ("work-requests", "clarify"): _rule(None, "reviews:decide"),
    ("work-requests", "decline"): _rule(None, "reviews:decide"),
    ("memories",): _rule("memories:read", "memories:write"),
    ("memories", "pin"): _rule(None, "memories:write"),
    ("memories", "contest"): _rule(None, "memories:write"),
    ("memories", "approve"): _rule(None, "memories:admin"),
    ("memories", "reject"): _rule(None, "memories:admin"),
    ("memories", "forget"): _rule(None, "memories:admin"),
    ("memories", "deduplicate"): _rule(None, "memories:admin"),
    ("memories", "embed-missing"): _rule(None, "memories:admin"),
    ("skills",): _rule("skills:read", "skills:write"),
    ("skills", "browse"): _rule("skills:read", None),
    ("skills", "browse", "install"): _rule(None, "skills:write"),
    ("skills", "import"): _rule(None, "skills:write"),
    ("skills", "import-zip"): _rule(None, "skills:write"),
    ("skills", "install-builtins"): _rule(None, "skills:write"),
    ("skill-sources",): _rule("skills:read", "skills:write"),
    ("triggers",): _rule("automations:read", "automations:write"),
    ("triggers", "invocations"): _rule("automations:read", None),
    ("triggers", "test"): _rule(None, "automations:write"),
    ("triggers", "enable"): _rule(None, "automations:write"),
    ("triggers", "disable"): _rule(None, "automations:write"),
    ("connections",): _rule("apps:read", "apps:write"),
    ("connections", "tools"): _rule("apps:read", "apps:write"),
    ("connections", "access-summary"): _rule("apps:read", None),
    ("connections", "metadata"): _rule("apps:read", None),
    ("connections", "tool-calls"): _rule("runs:read", None),
    ("connections", "verify"): _rule(None, "apps:write"),
    ("connections", "enable"): _rule(None, "apps:write"),
    ("connections", "disable"): _rule(None, "apps:write"),
    ("connections", "rotate"): _SEALED,
    ("connections", "webhook-secret"): _SEALED,
    ("secrets",): _SEALED,
    ("secrets", "rotate"): _SEALED,
    ("tools",): _rule("agents:read", None),
    ("model-providers",): _rule("models:read", "models:write"),
    ("model-providers", "models"): _rule("models:read", None),
    ("model-providers", "balance"): _rule("models:read", None),
    ("model-providers", "verify"): _rule(None, "models:write"),
    # Verifies a provider key posted in the request body: credential material.
    ("model-providers", "verify-draft"): _SEALED,
    ("model-profiles",): _rule("models:read", "models:write"),
    ("model-profiles", "refresh-pricing"): _rule(None, "models:write"),
    ("model-profiles", "pricing-status"): _rule("models:read", None),
    # Reconciliation reads the provider's billing API with the admin key and
    # writes prices back onto profiles: a models *write*, and a spend read.
    ("model-profiles", "reconcile-pricing"): _rule(None, "models:write"),
    ("model-profiles", "refresh-catalog"): _rule(None, "models:write"),
    ("spend",): _rule("spend:read", None),
    ("audit-events",): _rule("audit:read", None),
    ("media",): _rule("agents:read", None),
    ("api-keys",): _rule("api_keys:read", "api_keys:write"),
    ("api-keys", "usage"): _rule("api_keys:read", None),
    ("api-keys", "scopes"): _rule("api_keys:read", None),
}


def route_signature(path_template: str) -> tuple[str, ...] | None:
    """Signature for a workspace-scoped route template, or None if it is not one."""
    if not path_template.startswith(WORKSPACE_PREFIX):
        return None
    remainder = path_template[len(WORKSPACE_PREFIX) :]
    return tuple(
        segment
        for segment in remainder.strip("/").split("/")
        if segment and not segment.startswith("{")
    )


def required_scope(method: str, path_template: str) -> str | None:
    """The scope this request needs, or None when no API key may make it.

    Fail-closed by construction: an unmapped route returns None, so a route
    added without a rule here is unreachable with an API key rather than
    silently reachable with no scope check.
    """
    signature = route_signature(path_template)
    if signature is None:
        return None
    rule = ROUTE_SCOPES.get(signature)
    if rule is None:
        return None
    return rule.read if method.upper() in READ_METHODS else rule.write
