"""The OAuth surface's shape: no credential leaves it, and no key reaches it.

Three properties, checked mechanically rather than by reading:

* **no response model carries credential material.** The field names are
  audited by pattern, so a future field called ``refresh_token`` fails this
  file rather than shipping.
* **every credential-bearing route is sealed against API keys** at every
  scope, and probing — which returns no credential material — is not.
* **admin, not member.** A non-admin gets the same 403 the rest of the
  connections surface gives, and a non-member gets a 404 that does not
  confirm the workspace exists.
"""

from __future__ import annotations

import re
from typing import Any, get_args, get_origin

import pytest
from pydantic import BaseModel

from jhin_api.access.route_scopes import ROUTE_SCOPES, required_scope
from jhin_api.oauth import redirect as redirect_module
from jhin_api.oauth import schemas, service
from jhin_api.oauth.router import oauth_public_router, oauth_router
from jhin_domain import ALL_SCOPE_KEYS

# Anything whose name matches this is credential material or a step toward
# it. ``client_id`` is the single deliberate exception: a client id is sent in
# a URL the user can read, and is public by definition.
CREDENTIAL_FIELD = re.compile(r"token|secret|password|verifier|credential|(^|_)code($|_)")
ALLOWED_CREDENTIAL_SHAPED_FIELDS = {
    # Public by definition (OAuth 2.0 §2.2) and needed by the browser.
    "client_id",
    # A boolean, never the secret.
    "client_secret_configured",
    # The display code a person types into a provider's website. Useless
    # without the device code, which never leaves the server.
    "user_code",
    # Names a method, not a secret.
    "token_endpoint_auth_method",
    # Booleans that describe whether a secret exists, so the UI can ask for
    # one. Never the value.
    "requires_client_secret",
    "webhook_secret_configured",
}

RESPONSE_MODELS: list[type[BaseModel]] = [
    schemas.OAuthRedirectOut,
    schemas.OAuthProbeOut,
    schemas.ProbeFlow,
    schemas.OAuthStartOut,
    schemas.OAuthDeviceStartOut,
    schemas.OAuthDevicePollOut,
    schemas.OAuthClientOut,
    schemas.GitHubAppManifestOut,
]

SEALED = [
    ("oauth", "start"),
    ("oauth", "device", "start"),
    ("oauth", "device", "poll"),
    ("oauth", "clients"),
    ("oauth", "github-app", "manifest"),
    ("connections", "reauthorize"),
]


def _field_names(model: type[BaseModel]) -> set[str]:
    """Every field name in a model and, recursively, its nested models."""
    names: set[str] = set()
    for name, field in model.model_fields.items():
        names.add(name)
        annotation = field.annotation
        candidates: list[Any] = [annotation, *get_args(annotation)]
        if get_origin(annotation) is not None:
            candidates.extend(get_args(annotation))
        for candidate in candidates:
            if isinstance(candidate, type) and issubclass(candidate, BaseModel):
                names |= _field_names(candidate)
    return names


@pytest.mark.parametrize("model", RESPONSE_MODELS, ids=lambda m: m.__name__)
def test_no_oauth_response_model_carries_credential_material(model: type[BaseModel]) -> None:
    offenders = {
        name
        for name in _field_names(model)
        if CREDENTIAL_FIELD.search(name) and name not in ALLOWED_CREDENTIAL_SHAPED_FIELDS
    }
    assert offenders == set(), (
        f"{model.__name__} exposes {sorted(offenders)}. No OAuth response may carry a token, "
        "a secret, an authorization code, or a device code."
    )


def test_the_device_start_response_does_not_contain_the_device_code() -> None:
    """``handle`` is Jhin's own poll token; the device code stays encrypted."""
    fields = set(schemas.OAuthDeviceStartOut.model_fields)
    assert "device_code" not in fields
    assert "handle" in fields
    assert "user_code" in fields


def test_the_start_response_carries_a_url_and_no_secret() -> None:
    fields = set(schemas.OAuthStartOut.model_fields)
    assert "authorization_url" in fields
    assert fields & {"code_verifier", "client_secret", "code"} == set()


def test_the_client_response_reports_a_secret_without_returning_one() -> None:
    fields = schemas.OAuthClientOut.model_fields
    assert "client_secret_configured" in fields
    assert fields["client_secret_configured"].annotation is bool
    assert "client_secret" not in fields


@pytest.mark.parametrize("signature", SEALED, ids=lambda s: "/".join(s))
def test_credential_routes_are_sealed_against_every_api_key(
    signature: tuple[str, ...],
) -> None:
    rule = ROUTE_SCOPES[signature]
    for method in ("GET", "POST", "PUT", "PATCH", "DELETE"):
        assert rule.scope_for(method) is None, f"{signature} leaked a scope for {method}"


def test_probing_is_reachable_with_the_apps_write_scope() -> None:
    """A script asking "does this server speak OAuth?" is legitimate automation.

    It returns no credential material, so sealing it would buy nothing and
    cost an operator the ability to set an instance up from a script.
    """
    rule = ROUTE_SCOPES[("oauth", "probe")]
    assert rule.write == "apps:write"
    assert rule.write in ALL_SCOPE_KEYS
    assert rule.read is None


def test_every_workspace_oauth_route_has_a_rule() -> None:
    """Fail-closed is the default; this makes the omission visible anyway."""
    for route in oauth_router.routes:
        path = getattr(route, "path", "")
        signature = tuple(
            segment
            for segment in path.removeprefix("/api/v1/workspaces/{workspace_id}")
            .strip("/")
            .split("/")
            if segment and not segment.startswith("{")
        )
        assert signature in ROUTE_SCOPES, f"{path} has no ROUTE_SCOPES entry"


def test_the_global_routes_are_not_workspace_scoped() -> None:
    """They are session-only by construction: no scope table entry applies.

    A provider redirecting a browser knows nothing about workspaces, so the
    workspace comes out of the pending row rather than the URL — and an API
    key cannot satisfy the session dependency these routes require.
    """
    for route in oauth_public_router.routes:
        path = getattr(route, "path", "")
        assert path.startswith("/api/v1/oauth/")
        assert required_scope("GET", path) is None


def test_no_oauth_route_accepts_a_redirect_uri_or_client_id_from_a_request() -> None:
    """Jhin is a leaf client, not a proxy, and this keeps it one.

    An endpoint that took a ``redirect_uri`` or a ``client_id`` from a request
    and forwarded a user agent to a third-party authorization server would be
    the missing half of the confused-deputy attack. There is no such endpoint,
    and this test is what keeps it that way.
    """
    request_models = [
        schemas.OAuthProbeIn,
        schemas.OAuthStartIn,
        schemas.OAuthDeviceStartIn,
        schemas.OAuthDevicePollIn,
        schemas.GitHubAppManifestIn,
    ]
    for model in request_models:
        fields = set(model.model_fields)
        assert "redirect_uri" not in fields, model.__name__
        assert "client_id" not in fields, model.__name__
        assert fields & {"next", "return_to", "return_url"} == set(), model.__name__


def test_the_public_callbacks_declare_no_error_body() -> None:
    """A browser-facing route with a response model is a JSON body waiting to happen.

    Both callbacks answer a bare ``Response`` — a 303 with an empty body — on
    every path, success and refusal alike. That is the shape the operator's
    dead end came from breaking.
    """
    callbacks = [
        route
        for route in oauth_public_router.routes
        if getattr(route, "path", "").endswith("callback")
    ]
    assert len(callbacks) == 2
    for route in callbacks:
        assert route.status_code == 303, route.path  # type: ignore[attr-defined]
        assert route.response_model is None, route.path  # type: ignore[attr-defined]


def test_the_return_flag_vocabulary_is_the_one_that_was_analysed() -> None:
    """Nine flags, two tiers. A tenth means redoing the leak analysis.

    ``signed_out`` and ``expired`` are the pre-claim tier — reachable by any
    caller, so they say nothing. The other seven are reachable only past a
    claim, which needs the raw 256-bit handle *and* the owning session.
    """
    assert get_args(redirect_module.OAuthReturnError) == (
        "signed_out",
        "expired",
        "denied",
        "failed",
        "issuer_mismatch",
        "client_rejected",
        "callback_mismatch",
        "redirect_changed",
        "registration_gone",
    )


def test_the_admin_only_landings_are_a_subset_of_the_vocabulary() -> None:
    """The four that name a configuration fact, gated on current membership."""
    assert set(get_args(redirect_module.OAuthReturnError)) >= service._ADMIN_ONLY_LANDINGS
    assert {
        "redirect_changed",
        "registration_gone",
        "client_rejected",
        "callback_mismatch",
    } == service._ADMIN_ONLY_LANDINGS


def test_every_stored_outcome_renders_as_a_flag_or_as_nothing() -> None:
    """No receipt can name a landing the redirect builder does not know."""
    renderable = set(get_args(redirect_module.OAuthReturnError)) | {"connected"}
    for outcome in service.CALLBACK_OUTCOMES:
        if outcome.startswith("github_app_"):
            continue
        assert outcome in renderable, outcome


def test_the_only_request_model_with_a_client_id_is_the_manual_registration() -> None:
    """An admin pasting their own app's id is not a request-supplied redirect.

    It is stored against ``(workspace, issuer, redirect URI)`` and used to
    identify Jhin at that one server; it never becomes a destination.
    """
    assert "client_id" in schemas.OAuthClientCreate.model_fields
    assert "redirect_uri" not in schemas.OAuthClientCreate.model_fields
