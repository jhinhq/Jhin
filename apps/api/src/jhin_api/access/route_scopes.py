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

A rule's ``delete`` side follows its ``write`` side unless it says otherwise,
which is right whenever the scope's own description covers deleting the thing
("Create, rename, and delete teams"). It does not follow where DELETE destroys
something the write scope never promised: ``workspace:settings`` is offered as
renaming and budgets, so ``delete=None`` seals DELETE on the workspace root and
destroying a workspace stays an owner's browser-session act.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

WORKSPACE_PREFIX = "/api/v1/workspaces/{workspace_id}"
READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


class _Inherit(Enum):
    """Sentinel so an unstated ``delete`` side stays distinguishable from an
    explicit ``None``: the first means "same as write", the second means
    "sealed against every key", and conflating them would silently unseal."""

    TOKEN = "inherit"


INHERIT_WRITE = _Inherit.TOKEN


@dataclass(frozen=True, slots=True)
class RouteRule:
    read: str | None
    write: str | None
    delete: str | _Inherit | None = INHERIT_WRITE

    def scope_for(self, method: str) -> str | None:
        """The scope this rule requires of ``method``, or None when sealed."""
        upper = method.upper()
        if upper in READ_METHODS:
            return self.read
        if upper == "DELETE" and self.delete is not INHERIT_WRITE:
            return self.delete
        return self.write


def _rule(
    read: str | None,
    write: str | None,
    *,
    delete: str | _Inherit | None = INHERIT_WRITE,
) -> RouteRule:
    return RouteRule(read=read, write=write, delete=delete)


# Credential surfaces: present, deliberately unreachable with a bearer key.
_SEALED = _rule(None, None)


ROUTE_SCOPES: dict[tuple[str, ...], RouteRule] = {
    # PATCH renames the workspace and sets budgets; DELETE destroys it and
    # everything in it. One scope must not buy both, so DELETE is sealed —
    # the same treatment ("deletion-summary",) below already gets.
    (): _rule("workspace:read", "workspace:settings", delete=None),
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
    # A question an agent asked lives in its chat and is read and answered by
    # whoever is in that chat, so it takes the chat scopes rather than a pair
    # of its own: there is no way to hold a question without holding the
    # conversation it was asked in.
    ("questions",): _rule("chats:read", None),
    ("questions", "answer"): _rule(None, "chats:write"),
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
    # Re-authorizing mints a fresh grant and a fresh token for a connection:
    # credential material, on the same footing as rotating one by hand.
    ("connections", "reauthorize"): _SEALED,
    ("connections", "webhook-secret"): _SEALED,
    # Raising a connection's tools to the floor its catalog entry implies is a
    # change to how that app may be used, so it takes the same scope as editing
    # the app itself. There is no read side: the catalog is read off
    # /api/v1/catalog, which is not workspace-scoped and has no rule here.
    ("catalog", "apply-risk-floor"): _rule(None, "apps:write"),
    # OAuth (docs/architecture/oauth.md). Everything that mints, holds, or
    # hands out material that becomes a credential is sealed: starting an
    # authorization returns a URL that a browser turns into a token, the
    # device routes hold a device code, /clients stores a client secret, and
    # the GitHub manifest route produces a form that creates one. All four are
    # browser-session-only, forever.
    ("oauth", "start"): _SEALED,
    ("oauth", "device", "start"): _SEALED,
    ("oauth", "device", "poll"): _SEALED,
    ("oauth", "clients"): _SEALED,
    ("oauth", "github-app", "manifest"): _SEALED,
    # Probing returns no credential material — it answers "does this server
    # speak OAuth?" — and is a legitimate step for a script setting an
    # instance up, so it takes the same scope as editing an app.
    ("oauth", "probe"): _rule(None, "apps:write"),
    ("secrets",): _SEALED,
    ("secrets", "rotate"): _SEALED,
    ("tools",): _rule("agents:read", None),
    ("model-providers",): _rule("models:read", "models:write"),
    ("model-providers", "models"): _rule("models:read", None),
    ("model-providers", "balance"): _rule("models:read", None),
    # Local models on an Ollama host: listing is a models read, loading and
    # unloading change what the host holds in memory, so they take the write.
    ("model-providers", "ollama", "models"): _rule("models:read", None),
    ("model-providers", "ollama", "loaded"): _rule("models:read", None),
    ("model-providers", "ollama", "load"): _rule(None, "models:write"),
    ("model-providers", "ollama", "unload"): _rule(None, "models:write"),
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
    return rule.scope_for(method)
