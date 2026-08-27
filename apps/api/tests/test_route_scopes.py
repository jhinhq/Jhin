"""Every workspace-scoped route is classified, and classified sensibly.

This is the audit from docs/architecture/api-keys.md made durable: a new
workspace route that nobody thought about is unreachable with an API key
*and* fails this test, so the omission is noticed rather than shipped.
"""

from __future__ import annotations

import pytest

from jhin_api.access.route_scopes import (
    ROUTE_SCOPES,
    WORKSPACE_PREFIX,
    required_scope,
    route_signature,
)
from jhin_api.main import create_app
from jhin_domain import ALL_SCOPE_KEYS, SCOPE_BY_KEY, WorkspaceRole

# Credential surfaces that must stay browser-session-only, forever.
SEALED_SIGNATURES = {
    ("secrets",),
    ("secrets", "rotate"),
    ("connections", "rotate"),
    ("connections", "webhook-secret"),
    ("model-providers", "verify-draft"),
}

# Signatures whose DELETE alone is sealed: the route's write scope buys the
# other methods, but destruction here is not something any key may do.
SEALED_DELETE_SIGNATURES = {
    (),
}


def _workspace_routes() -> list[tuple[str, str, tuple[str, ...]]]:
    """Read the surface from the generated OpenAPI document.

    Deliberately not by walking ``app.routes``: FastAPI wraps included routers
    in an opaque object whose shape is an implementation detail, while the
    OpenAPI paths are the published contract and always carry the full
    template.
    """
    paths = create_app().openapi()["paths"]
    seen: list[tuple[str, str, tuple[str, ...]]] = []
    for path, operations in paths.items():
        signature = route_signature(path)
        if signature is None:
            continue
        for method in sorted(operations):
            upper = method.upper()
            if upper in {"HEAD", "OPTIONS"}:
                continue
            seen.append((upper, path, signature))
    return seen


ROUTES = _workspace_routes()


def test_the_app_actually_exposes_workspace_routes() -> None:
    """Guards the rest of this module against silently testing nothing."""
    assert len(ROUTES) > 100, f"only found {len(ROUTES)} workspace routes"


@pytest.mark.parametrize(("method", "path", "signature"), ROUTES, ids=lambda value: str(value))
def test_every_workspace_route_is_classified(
    method: str, path: str, signature: tuple[str, ...]
) -> None:
    rule = ROUTE_SCOPES.get(signature)
    assert rule is not None, (
        f"{method} {path} has no entry in ROUTE_SCOPES. Add {signature!r} with the "
        "scope it needs (or a sealed rule if no API key may ever call it)."
    )
    side = rule.scope_for(method)
    if signature in SEALED_SIGNATURES or (
        method == "DELETE" and signature in SEALED_DELETE_SIGNATURES
    ):
        assert side is None
        return
    assert side is not None, f"{method} {path} maps to {signature!r}, which has no scope for it"
    assert side in ALL_SCOPE_KEYS


def test_credential_routes_are_unreachable_with_any_api_key() -> None:
    for method, path, signature in ROUTES:
        if signature in SEALED_SIGNATURES:
            assert required_scope(method, path) is None, f"{method} {path} leaked a scope"


def test_no_api_key_can_delete_a_workspace() -> None:
    """Destroying a workspace is an owner's browser-session act.

    ``workspace:settings`` is offered as "rename the workspace and change its
    timezone, budgets, and limits". Keying the rule by signature alone once
    let that same scope reach DELETE on the workspace root, so a key handed
    out to adjust a budget could destroy the workspace and everything in it.
    """
    assert required_scope("DELETE", WORKSPACE_PREFIX) is None
    assert required_scope("PATCH", WORKSPACE_PREFIX) == "workspace:settings"
    assert required_scope("GET", WORKSPACE_PREFIX) == "workspace:read"


def test_a_sealed_delete_does_not_seal_the_other_writes() -> None:
    """The sentinel keeps "unstated" and "explicitly sealed" apart, so every
    other rule still lets its write scope delete what that scope describes."""
    for signature, rule in ROUTE_SCOPES.items():
        if signature in SEALED_SIGNATURES or signature in SEALED_DELETE_SIGNATURES:
            continue
        assert rule.scope_for("DELETE") == rule.write, signature


def test_unmapped_routes_fail_closed() -> None:
    assert required_scope("GET", f"{WORKSPACE_PREFIX}/something-brand-new") is None
    assert required_scope("GET", "/api/v1/auth/me") is None


def test_route_signature_drops_path_parameters() -> None:
    assert route_signature(f"{WORKSPACE_PREFIX}/agents/{{agent_id}}/grants/{{grant_id}}") == (
        "agents",
        "grants",
    )
    assert route_signature(WORKSPACE_PREFIX) == ()
    assert route_signature("/api/v1/health") is None


def test_write_side_never_maps_to_a_read_only_scope() -> None:
    for signature, rule in ROUTE_SCOPES.items():
        if rule.write is None:
            continue
        assert SCOPE_BY_KEY[rule.write].action != "read", (
            f"{signature!r} lets a read-only scope perform a mutation"
        )


def test_secret_reading_routes_require_at_least_admin_scopes() -> None:
    """Anything an API key can reach that touches app configuration is admin."""
    for signature in (("connections",), ("model-providers",), ("audit-events",)):
        rule = ROUTE_SCOPES[signature]
        for scope in (rule.read, rule.write):
            if scope is None:
                continue
            assert SCOPE_BY_KEY[scope].min_role == WorkspaceRole.ADMIN, signature
