"""The published OpenAPI document: metadata, auth schemes, and per-operation
scope annotation.

Nothing here describes the API a second time. The paths, parameters, and
schemas come from the routers and Pydantic models exactly as FastAPI generates
them; this module only adds the things a generated document cannot know —
what the product is, how to authenticate, which tag means what, and (read out
of :mod:`jhin_api.access.route_scopes`, the one table that already decides it)
which scope each operation needs. That derivation is the point: a route whose
scope changes changes the document on the next request, with no second place
to update and therefore no way to drift.

The document is served three ways:

* ``/openapi.json``, ``/docs``, ``/redoc`` — anonymous, and only when
  ``settings.expose_api_docs`` is true, which it never is in staging or
  production. Handy locally; not a map of the surface handed to the internet.
* ``GET /api/v1/openapi.json`` — the same document behind a session cookie.
  Always available, because a self-hoster's integrators still need a reference
  in production, and a workspace member is not an anonymous visitor.
* ``/api-docs`` in the web app, which reads the endpoint above.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, FastAPI, Request
from fastapi.openapi.utils import get_openapi

from jhin_api.access.route_scopes import ROUTE_SCOPES, route_signature
from jhin_api.deps import CurrentAuth

#: The version segment every route is published under. Bumped only by adding
#: ``/api/v2`` beside ``/api/v1`` (docs/architecture/api-versioning.md).
API_VERSION = "v1"

API_PREFIX = f"/api/{API_VERSION}"

LICENSE_INFO = {
    "name": "Apache License 2.0",
    "identifier": "Apache-2.0",
    "url": "https://www.apache.org/licenses/LICENSE-2.0",
}

CONTACT_INFO = {
    "name": "Jhin on GitHub",
    "url": "https://github.com/Teachmetech/Jhin",
}

SUMMARY = "Run and observe an AI company: agents, teams, tasks, and the tools they use."

DESCRIPTION = """\
The Jhin control plane. Everything the web app can do, a script can do: agents
and teams, the work they are given, the conversations they hold, the skills and
connected apps they draw on, and the record of what they did.

## Base URL

Every route lives under `/api/v1` on your own Jhin install — for a local
development stack, `http://localhost:3000/api/v1`. There is no hosted service
and no other origin to call.

## Authenticating

Two credentials are accepted, and each operation below says which of them it
takes.

**API key** — the credential for scripts, CI jobs, and other systems. Create
one under *Advanced → API keys*, then send it as a bearer token:

```
curl -H "Authorization: Bearer jhin_<prefix>_<secret>" \\
     https://your-jhin-host/api/v1/workspaces/<workspace_id>/agents
```

The key is shown once, at creation. Presenting a bad one is rate limited on a
decaying ladder, and a bad key never falls back to a cookie you happen to hold.

**Session cookie** — what the browser uses. Mutating requests additionally
carry the `X-CSRF-Token` double-submit header. A handful of operations accept
*only* this: the credential surfaces (workspace secrets, connection credential
rotation, webhook signing secrets, draft provider verification) are sealed
against API keys forever, and are marked as session-only below.

## Scopes

An API key carries an explicit set of `<category>:<action>` scopes and is
capped by the workspace role of the person who made it — a key can never do
more than its creator could. Every operation that a key may call names its
required scope in its description and in the `x-jhin-scope` extension, and the
full catalogue is served live at
`GET /api/v1/workspaces/{workspace_id}/api-keys/scopes`.

Effective permission is the intersection of the key's scopes, the scopes its
role ceiling allows, and the creator's role *today*: demote the creator and the
key loses the matching power on its next call.

## Errors

Failures return a JSON body with a `detail` string:

```json
{ "detail": "Not authenticated" }
```

`422` responses use FastAPI's validation shape (`detail` is a list of field
errors). Some subsystems add a machine-readable `code` alongside `detail`.
Every response — success or failure — carries an `X-Request-ID` header; quote
it when reporting a problem.

## Limits

Request bodies are capped (`MAX_REQUEST_BODY_BYTES`, 1 MiB by default) and
rejected with `413`. Failed logins and failed API-key presentations are rate
limited per credential and per source address, and return `429` while blocked.
There is no throughput quota on successful calls: this is your own install.

## Compatibility

`/api/v1` is a stable contract. Fields and endpoints are added, never
removed or retyped, and a breaking change ships as `/api/v2` alongside `v1`
rather than in place of it. The rules, the deprecation process, and the
machine-checked snapshot that enforces them are in
`docs/architecture/api-versioning.md`. `GET /api/v1/health` reports both the
app version and the API version, so an integrator can detect capability
without parsing this document.
"""

#: One line per tag, in the order they should be read. Tags are the sections
#: of the rendered reference; an undescribed tag is a bare slug on the page,
#: so ``test_openapi_metadata`` fails when a router introduces a new one.
TAG_DESCRIPTIONS: dict[str, str] = {
    "health": (
        "Liveness, readiness, and the app and API versions this install is running. "
        "The only routes that need no credential at all."
    ),
    "auth": (
        "Sign in, sign out, change a password, and read the current user. "
        "Session cookies only — an API key cannot mint or manage sessions — "
        "except `GET /auth/identity`, which either credential may call to "
        "discover who it is and which workspace it may act in."
    ),
    "workspaces": (
        "The workspace itself: its name, timezone, budget, members, and the roles "
        "that decide who may do what inside it."
    ),
    "invitations": "Invite someone into a workspace, and the token flow they use to accept.",
    "api-keys": (
        "Scoped bearer keys for scripts and other systems, the scope catalogue they "
        "are drawn from, and the log of every call each key has made."
    ),
    "organization": "The org chart: who reports to whom, agents and people together.",
    "directory": "A flat, searchable list of everyone in the workspace, human and agent.",
    "agents": (
        "Create and configure agents, their reporting lines, avatars, capability "
        "grants, and autonomy policy; hand one a task or a message directly."
    ),
    "teams": "Groups of agents that share a brief, and their membership.",
    "tasks": (
        "Work items: create, steer, pause, resume, cancel, and read the message "
        "trail and delegation tree of each one."
    ),
    "runs": (
        "Individual agent executions behind a task — the timeline of steps, the "
        "tool calls made, and what each one cost."
    ),
    "conversations": (
        "Chats between people and agents, their messages and turns, and the "
        "workspace activity feed they roll up into."
    ),
    "coordination": (
        "Everything waiting on a person: approvals, review policies and verdicts, "
        "work requests, and the attention queue that gathers them."
    ),
    "approvals": "Actions an agent paused on until somebody approves or rejects them.",
    "questions": (
        "Questions an agent asked the person it is talking to, and their answers — "
        "a choice among the options it offered, or something typed instead."
    ),
    "policy": "Autonomy policy and the capability grants that bound what an agent may do.",
    "memory": (
        "Curated long-term memory: what agents have learned, what was pinned, "
        "contested, approved, or forgotten."
    ),
    "skills": (
        "The skill library — install, import, browse, and attach reusable instructions to agents."
    ),
    "connectors": "The catalogue of connectable app types and the tools each one exposes.",
    "catalog": (
        "The searchable index of MCP servers and agent skills, synced from the "
        "public jhin-catalog. An index only: nothing here is connected until "
        "somebody connects it."
    ),
    "connections": (
        "Live connections to outside apps: credentials, the tools they enable, "
        "access summaries, and their call history."
    ),
    "oauth": (
        "Connecting an app by signing in to it instead of pasting a key: what a "
        "server offers, starting an authorization, the callback it returns to, "
        "and the app registrations a workspace holds. No route here returns a "
        "token, a client secret, or a device code."
    ),
    "triggers": (
        "Schedules and incoming events that start work on their own, plus the "
        "record of every invocation."
    ),
    "webhooks": (
        "Signature-verified inbound endpoints that connected apps post to. "
        "Called by those apps, not by you."
    ),
    "model-providers": "Model providers, their credentials, catalogues, and balances.",
    "model-profiles": "Named model configurations agents are assigned, and their pricing.",
    "activity": "The chronological feed of what happened in the workspace.",
    "audit": "The permanent, append-only record of who changed what, and when.",
    "secrets": ("Workspace secrets. Session-only forever: no API key reaches these at any scope."),
    "media": "Files and images produced or consumed by agents.",
    "meta": "The API's own description: the OpenAPI document that generated this page.",
}

SECURITY_SCHEMES: dict[str, dict[str, Any]] = {
    "ApiKeyBearer": {
        "type": "http",
        "scheme": "bearer",
        "bearerFormat": "jhin_<prefix>_<secret>",
        "description": (
            "A workspace API key, created under Advanced → API keys and sent as "
            "`Authorization: Bearer jhin_...`. Carries an explicit scope set capped "
            "by the role of the person who created it."
        ),
    },
    "SessionCookie": {
        "type": "apiKey",
        "in": "cookie",
        "name": "jhin_session",
        "description": (
            "The HttpOnly session cookie set by `POST /api/v1/auth/login`. Mutating "
            "requests must also send the `X-CSRF-Token` double-submit header, read "
            "from the `jhin_csrf` cookie."
        ),
    },
}

#: Operations that take no credential at all. Kept as an explicit list rather
#: than inferred, because "this endpoint is reachable by anyone on the network"
#: is exactly the claim that should be written down and reviewed.
PUBLIC_OPERATIONS: frozenset[tuple[str, str]] = frozenset(
    {
        ("get", f"{API_PREFIX}/health"),
        ("get", f"{API_PREFIX}/health/ready"),
        ("post", f"{API_PREFIX}/auth/login"),
        ("post", f"{API_PREFIX}/auth/bootstrap"),
        ("get", f"{API_PREFIX}/auth/bootstrap-status"),
        # Invitation tokens are the credential; the recipient has no account yet.
        ("get", f"{API_PREFIX}/invitations/{{token}}"),
        ("post", f"{API_PREFIX}/invitations/{{token}}/accept"),
        # Authenticated by the connector's own request signature, not by ours.
        ("post", f"{API_PREFIX}/webhooks/{{connector_type}}/{{public_id}}"),
    }
)

_SESSION_ONLY: list[dict[str, list[str]]] = [{"SessionCookie": []}]

#: Operations outside `/workspaces/{workspace_id}` that an API key may still
#: call. Everything else off that prefix is session-only by construction (see
#: ``_security_for``), which is the safe default — but identity has to be
#: readable *before* a caller knows its workspace, or a key-only client can
#: never make a first call. Kept explicit, and short, for the same reason
#: ``PUBLIC_OPERATIONS`` is: widening what a key reaches should be a diff
#: somebody reviews.
_DUAL_CREDENTIAL_OPERATIONS: frozenset[tuple[str, str]] = frozenset(
    {
        ("get", f"{API_PREFIX}/auth/identity"),
    }
)

_SCOPE_NOTE = "**Scope.** An API key needs `{scope}`."

_SESSION_ONLY_NOTE = (
    "**Session only.** This endpoint touches credential material and is sealed "
    "against API keys at every scope; it needs a browser session."
)

# A route whose other methods a key may call, but whose DELETE is sealed: the
# credential-material sentence would be simply untrue there.
_SEALED_DELETE_NOTE = (
    "**Session only.** This deletion is irreversible and is sealed against API keys "
    "at every scope; it needs a browser session. The other methods on this path stay "
    "available to a key with the scope they name."
)

_PUBLIC_NOTE = "**Auth.** None: this endpoint is reachable without a credential."

_DUAL_CREDENTIAL_NOTE = (
    "**Auth.** A browser session or any API key, at any scope. A key sees only "
    "the one workspace it is bound to."
)


def tag_metadata() -> list[dict[str, str]]:
    """Tag descriptions in reading order, for ``FastAPI(openapi_tags=...)``."""
    return [{"name": name, "description": text} for name, text in TAG_DESCRIPTIONS.items()]


def _security_for(method: str, path: str) -> tuple[list[dict[str, list[str]]], str, str | None]:
    """The security requirement, the note to append, and the scope, if any."""
    if (method, path) in PUBLIC_OPERATIONS:
        return [], _PUBLIC_NOTE, None

    if (method, path) in _DUAL_CREDENTIAL_OPERATIONS:
        return [{"SessionCookie": []}, {"ApiKeyBearer": []}], _DUAL_CREDENTIAL_NOTE, None

    signature = route_signature(path)
    if signature is None:
        # Not workspace-scoped: session-only by construction (auth, meta).
        return _SESSION_ONLY, _SESSION_ONLY_NOTE, None

    rule = ROUTE_SCOPES.get(signature)
    scope = None
    if rule is not None:
        # Per method, not per read/write: a rule may seal DELETE on its own.
        scope = rule.scope_for(method)
    if scope is None:
        if rule is not None and method.upper() == "DELETE" and rule.write is not None:
            return _SESSION_ONLY, _SEALED_DELETE_NOTE, None
        return _SESSION_ONLY, _SESSION_ONLY_NOTE, None
    return (
        [{"SessionCookie": []}, {"ApiKeyBearer": [scope]}],
        _SCOPE_NOTE.format(scope=scope),
        scope,
    )


def _annotate(operation: dict[str, Any], method: str, path: str) -> None:
    security, note, scope = _security_for(method, path)
    operation["security"] = security
    if scope is not None:
        operation["x-jhin-scope"] = scope
    existing = operation.get("description")
    operation["description"] = f"{existing}\n\n{note}" if existing else note


def build_openapi(app: FastAPI) -> dict[str, Any]:
    """Generate the document, then add what generation cannot know.

    Cached on ``app.openapi_schema`` the way FastAPI's own implementation is,
    so the annotation pass runs once per process rather than once per request.
    """
    if app.openapi_schema is not None:
        return app.openapi_schema

    schema = get_openapi(
        title=app.title,
        version=app.version,
        summary=app.summary,
        description=app.description,
        routes=app.routes,
        tags=app.openapi_tags,
        servers=app.servers,
        contact=CONTACT_INFO,
        license_info=LICENSE_INFO,
    )
    schema.setdefault("components", {})["securitySchemes"] = SECURITY_SCHEMES
    schema["info"]["x-api-version"] = API_VERSION

    for path, operations in schema["paths"].items():
        for method, operation in operations.items():
            if method in {"parameters", "servers", "summary", "description"}:
                continue
            _annotate(operation, method, path)

    app.openapi_schema = schema
    return schema


router = APIRouter(prefix=API_PREFIX, tags=["meta"])


@router.get(
    "/openapi.json",
    summary="This OpenAPI document",
    description=(
        "The OpenAPI 3.1 description of this exact running install, generated "
        "from its routes. Available to any signed-in user, including in "
        "production where the anonymous `/openapi.json` and `/docs` are off. "
        "This is what the in-app API reference at `/api-docs` renders."
    ),
    response_model=None,
)
async def openapi_document(request: Request, _auth: CurrentAuth) -> dict[str, Any]:
    app: FastAPI = request.app
    document: dict[str, Any] = app.openapi()
    return document
