"""The published document says what the API actually is, and who may read it.

The paths and schemas in the document are FastAPI's job. What is tested here
is everything a generated document cannot know and that a person reading the
reference depends on: the prose, the licence, the auth schemes, a description
for every tag, and — the part that would otherwise rot — the scope each
operation names being the same scope ``route_scopes`` enforces.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

import jhin_api.main as main_module
from jhin_api import __version__
from jhin_api.access.route_scopes import ROUTE_SCOPES, route_signature
from jhin_api.main import create_app
from jhin_api.openapi import API_VERSION, PUBLIC_OPERATIONS, TAG_DESCRIPTIONS
from jhin_api.settings import Settings


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "app_env": "test",
        "app_name": "Jhin",
        "app_url": "http://test",
        "database_url": "sqlite+aiosqlite:///:memory:",
    }
    base.update(overrides)
    return Settings(**base)


@pytest.fixture(autouse=True)
def _deterministic_secret_loader(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main_module, "_load_secret_crypto", lambda: None)


@pytest.fixture(scope="module")
def spec() -> dict[str, Any]:
    document: dict[str, Any] = create_app(
        Settings(
            app_env="test",
            app_name="Jhin",
            app_url="http://test",
            database_url="sqlite+aiosqlite:///:memory:",
        )
    ).openapi()
    return document


def _operations(spec: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    found: list[tuple[str, str, dict[str, Any]]] = []
    for path, operations in spec["paths"].items():
        for method, operation in operations.items():
            if method in {"parameters", "servers", "summary", "description"}:
                continue
            found.append((method, path, operation))
    return found


# --------------------------------------------------------------------------
# Metadata
# --------------------------------------------------------------------------


def test_info_carries_the_app_version_and_the_api_version(spec: dict[str, Any]) -> None:
    assert spec["info"]["version"] == __version__
    assert spec["info"]["x-api-version"] == API_VERSION


def test_the_description_explains_auth_scopes_errors_and_limits(spec: dict[str, Any]) -> None:
    description = spec["info"]["description"]
    for expected in (
        "Authorization: Bearer jhin_",
        "/api/v1",
        "X-CSRF-Token",
        "scopes",
        "X-Request-ID",
        "429",
        "docs/architecture/api-versioning.md",
    ):
        assert expected in description, f"the API description never mentions {expected!r}"
    assert spec["info"]["summary"]


def test_the_licence_and_contact_are_declared(spec: dict[str, Any]) -> None:
    assert spec["info"]["license"]["identifier"] == "Apache-2.0"
    assert spec["info"]["contact"]["url"].startswith("https://github.com/")


def test_the_server_is_relative_so_it_is_right_behind_any_proxy(spec: dict[str, Any]) -> None:
    assert [server["url"] for server in spec["servers"]] == ["/"]


def test_every_tag_in_use_has_a_description(spec: dict[str, Any]) -> None:
    described = {tag["name"] for tag in spec["tags"]}
    used = {tag for _, _, op in _operations(spec) for tag in op.get("tags", [])}
    assert used - described == set(), "tags render as bare slugs without a description"
    assert described - used == set(), "described tags that no route uses"
    assert all(TAG_DESCRIPTIONS[name].strip() for name in used)


def test_every_operation_says_how_to_authenticate(spec: dict[str, Any]) -> None:
    for method, path, operation in _operations(spec):
        description = operation.get("description", "")
        assert description, f"{method.upper()} {path} has no description"
        assert any(
            marker in description for marker in ("**Scope.**", "**Session only.**", "**Auth.**")
        ), f"{method.upper()} {path} never says what credential it takes"


# --------------------------------------------------------------------------
# Security schemes
# --------------------------------------------------------------------------


def test_both_credentials_are_documented_as_security_schemes(spec: dict[str, Any]) -> None:
    schemes = spec["components"]["securitySchemes"]
    assert schemes["ApiKeyBearer"]["type"] == "http"
    assert schemes["ApiKeyBearer"]["scheme"] == "bearer"
    assert schemes["ApiKeyBearer"]["bearerFormat"].startswith("jhin_")
    assert schemes["SessionCookie"] == {
        "type": "apiKey",
        "in": "cookie",
        "name": "jhin_session",
        "description": schemes["SessionCookie"]["description"],
    }
    assert "X-CSRF-Token" in schemes["SessionCookie"]["description"]


def test_public_operations_declare_no_credential(spec: dict[str, Any]) -> None:
    for method, path, operation in _operations(spec):
        if (method, path) in PUBLIC_OPERATIONS:
            assert operation["security"] == [], f"{method.upper()} {path} should be public"
        else:
            assert operation["security"], f"{method.upper()} {path} claims to need no credential"


def test_the_declared_scope_is_the_scope_the_api_enforces(spec: dict[str, Any]) -> None:
    """The one assertion that keeps the reference honest as routes change."""
    checked = 0
    for method, path, operation in _operations(spec):
        signature = route_signature(path)
        if signature is None:
            continue
        rule = ROUTE_SCOPES.get(signature)
        expected = None
        if rule is not None:
            expected = rule.scope_for(method)
        declared = operation.get("x-jhin-scope")
        assert declared == expected, (
            f"{method.upper()} {path} documents {declared!r}, enforces {expected!r}"
        )
        if expected is None:
            # A sealed or unmapped workspace route: no key reaches it at all.
            assert operation["security"] == [{"SessionCookie": []}]
            assert "**Session only.**" in operation["description"]
        else:
            assert operation["security"] == [
                {"SessionCookie": []},
                {"ApiKeyBearer": [expected]},
            ]
            assert f"`{expected}`" in operation["description"]
            checked += 1
    assert checked > 100, f"only {checked} scoped operations found"


def test_deleting_a_workspace_is_documented_as_session_only(spec: dict[str, Any]) -> None:
    """The reference has to show the seal, and show it for the right reason.

    Reading the rule per read/write instead of per method documented DELETE
    with the settings scope PATCH needs — telling integrators that a budget
    key could destroy the workspace, which is exactly what it could do.
    """
    operations = spec["paths"]["/api/v1/workspaces/{workspace_id}"]
    deletion = operations["delete"]
    assert deletion["security"] == [{"SessionCookie": []}]
    assert "x-jhin-scope" not in deletion
    assert "irreversible" in deletion["description"]
    # This one is sealed for being destructive, not for touching secrets.
    assert "credential material" not in deletion["description"]
    assert operations["patch"]["x-jhin-scope"] == "workspace:settings"
    assert operations["get"]["x-jhin-scope"] == "workspace:read"


def test_the_credential_surfaces_stay_session_only(spec: dict[str, Any]) -> None:
    sealed = [
        "/api/v1/workspaces/{workspace_id}/secrets",
        "/api/v1/workspaces/{workspace_id}/connections/{connection_id}/rotate",
        "/api/v1/workspaces/{workspace_id}/model-providers/verify-draft",
    ]
    for path in sealed:
        assert path in spec["paths"], path
        for _, operation in spec["paths"][path].items():
            assert operation["security"] == [{"SessionCookie": []}]


# --------------------------------------------------------------------------
# Serving it
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_reports_the_app_and_api_versions() -> None:
    app = create_app(_settings())
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/api/v1/health")
    body = response.json()
    assert body["version"] == __version__
    assert body["api_version"] == API_VERSION


@pytest.mark.asyncio
async def test_the_anonymous_docs_are_on_in_development() -> None:
    app = create_app(_settings())
    assert app.docs_url == "/docs"
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.get("/openapi.json")).status_code == 200


@pytest.mark.asyncio
async def test_the_anonymous_docs_are_off_in_production_but_the_reference_is_not() -> None:
    """Production hides the surface from strangers, not from its own users."""
    app = create_app(
        _settings(app_env="production", app_url="https://jhin.example", cookie_secure=True)
    )
    assert app.docs_url is None and app.redoc_url is None and app.openapi_url is None
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="https://jhin.example"
        ) as client:
            assert (await client.get("/openapi.json")).status_code == 404
            assert (await client.get("/docs")).status_code == 404
            # Present, and asking for a session rather than pretending not to exist.
            assert (await client.get("/api/v1/openapi.json")).status_code == 401


@pytest.mark.asyncio
async def test_the_session_document_is_the_same_document() -> None:
    app = create_app(_settings())
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            anonymous = await client.get("/openapi.json")
            unauthenticated = await client.get("/api/v1/openapi.json")
    assert unauthenticated.status_code == 401
    assert anonymous.json()["paths"]["/api/v1/openapi.json"]["get"]["security"] == [
        {"SessionCookie": []}
    ]


def test_the_document_is_generated_once_and_cached() -> None:
    app = create_app(_settings())
    assert app.openapi() is app.openapi()
