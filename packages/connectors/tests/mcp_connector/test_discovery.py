"""Pure MCP connector logic: slugs, risk mapping, persistence parsing, URL
policy, auth headers, and result conversion (no server)."""

from typing import Any

import pytest
from mcp import types as mcp_types

from jhin_connectors.base import VerifyContext
from jhin_connectors.catalog import CATALOG_CATEGORIES, CatalogApp, load_catalog
from jhin_connectors.endpoints import EndpointPolicyError
from jhin_connectors.mcp import (
    DISCOVERY_KEY,
    OVERRIDES_KEY,
    DiscoveredTool,
    McpConnector,
    McpToolAnnotations,
    convert_result,
    derive_risk,
    effective_risk,
    stored_overrides,
    stored_tools,
    tool_slug,
    validate_mcp_server_url,
)
from jhin_connectors.mcp.client import auth_headers, validate_header_name
from jhin_connectors.mcp.discovery import MAX_SCHEMA_BYTES, MAX_TOOLS, discovered_from_mcp
from jhin_connectors.mcp.tools import (
    MAX_TEXT_CHARS,
    UNTRUSTED_NOTICE,
    build_definition,
    connection_tool_definitions,
)
from jhin_connectors.registry import default_registry
from jhin_policy import RiskLevel


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("echo", "echo"),
        ("getIssue", "getissue"),
        ("list-repos", "list_repos"),
        ("  Weird name!! ", "weird_name"),
        ("___", None),
        ("", None),
        ("a" * 100, "a" * 64),
    ],
)
def test_tool_slug_normalizes_provider_names(name: str, expected: str | None) -> None:
    assert tool_slug(name) == expected


@pytest.mark.parametrize(
    ("annotations", "expected"),
    [
        (McpToolAnnotations(read_only_hint=True), RiskLevel.READ),
        (McpToolAnnotations(read_only_hint=True, destructive_hint=True), RiskLevel.READ),
        (McpToolAnnotations(destructive_hint=True), RiskLevel.DESTRUCTIVE),
        (McpToolAnnotations(destructive_hint=False), RiskLevel.WRITE),
        (McpToolAnnotations(), RiskLevel.WRITE),
    ],
)
def test_risk_is_derived_from_annotations(
    annotations: McpToolAnnotations, expected: RiskLevel
) -> None:
    assert derive_risk(annotations) == expected


def test_discovery_bounds_count_schema_and_dedupes_slugs() -> None:
    huge_schema = {"type": "object", "properties": {"x": {"description": "y" * MAX_SCHEMA_BYTES}}}
    tools = [
        mcp_types.Tool(name=f"tool-{index}", inputSchema={"type": "object"})
        for index in range(MAX_TOOLS + 10)
    ]
    tools.insert(0, mcp_types.Tool(name="Huge", inputSchema=huge_schema, description="d" * 5000))
    tools.insert(1, mcp_types.Tool(name="huge", inputSchema={"type": "object"}))
    discovered = discovered_from_mcp(tools)
    assert len(discovered) == MAX_TOOLS
    first = discovered[0]
    assert first.slug == "huge" and first.name == "Huge"
    assert first.schema_truncated and first.input_schema == {"type": "object"}
    assert len(first.description) == 1000
    assert [tool.slug for tool in discovered].count("huge") == 1


def test_stored_tools_and_overrides_ignore_malformed_entries() -> None:
    config: dict[str, Any] = {
        DISCOVERY_KEY: [
            {"name": "echo", "slug": "echo", "derived_risk": "read"},
            {"name": "dup", "slug": "echo", "derived_risk": "write"},
            {"name": "bad slug", "slug": "Bad Slug", "derived_risk": "write"},
            {"name": "no risk", "slug": "norisk"},
            "not a dict",
        ],
        OVERRIDES_KEY: {"echo": "destructive", "missing": "read", "echo2": "nope", 3: "read"},
    }
    tools = stored_tools(config)
    assert [tool.slug for tool in tools] == ["echo"]
    overrides = stored_overrides(config)
    assert overrides == {"echo": RiskLevel.DESTRUCTIVE, "missing": RiskLevel.READ}
    assert effective_risk(tools[0], overrides) is RiskLevel.DESTRUCTIVE
    assert effective_risk(tools[0], {}) is RiskLevel.READ
    assert stored_tools({}) == [] and stored_overrides({DISCOVERY_KEY: 1}) == {}


def test_definitions_carry_scope_keys_schema_and_override_risk() -> None:
    tool = DiscoveredTool(
        name="create_note",
        slug="create_note",
        description="Create a note.",
        input_schema={
            "type": "object",
            "properties": {"title": {"type": "string"}},
            "required": ["title"],
        },
        derived_risk=RiskLevel.WRITE,
    )
    definition = build_definition("fake", tool, {})
    assert definition.name == "mcp.fake.create_note"
    assert definition.required_capability == "mcp.fake.create_note"
    assert definition.scope_keys == ("connection_id", "tool")
    assert definition.supports_approval and definition.risk is RiskLevel.WRITE
    schema = definition.input_json_schema()
    assert schema["properties"]["arguments"]["properties"]["title"] == {"type": "string"}
    assert schema["properties"]["tool"]["const"] == "create_note"
    assert "connection_id" in schema["required"]
    validated = definition.input_model.model_validate(
        {"connection_id": "c", "arguments": {"title": "x"}}
    )
    assert validated.model_dump()["tool"] == "create_note"
    with pytest.raises(ValueError):
        definition.input_model.model_validate({"connection_id": "c", "tool": "other"})
    with pytest.raises(ValueError):
        definition.input_model.model_validate({"connection_id": "c", "extra": 1})

    overridden = build_definition("fake", tool, {"create_note": RiskLevel.DESTRUCTIVE})
    assert overridden.risk is RiskLevel.DESTRUCTIVE

    config = {
        "server_slug": "fake",
        DISCOVERY_KEY: [tool.model_dump(mode="json")],
        OVERRIDES_KEY: {"create_note": "read"},
    }
    names = [item.name for item in connection_tool_definitions(config)]
    assert names == ["mcp.fake.create_note"]
    assert connection_tool_definitions({"server_slug": "Bad"}) == ()
    assert McpConnector().connection_tool_definitions(config)[0].risk is RiskLevel.READ


@pytest.mark.parametrize(
    "url",
    [
        "https://mcp.example.com/mcp",
        "https://mcp.example.com:8443/v1/sse?x=1",
        "https://93.184.216.34/mcp",
    ],
)
def test_public_https_servers_are_allowed(url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS", raising=False)
    assert validate_mcp_server_url(url).startswith("https://")


@pytest.mark.parametrize(
    "url",
    [
        "http://mcp.example.com/mcp",
        "https://localhost/mcp",
        "https://127.0.0.1/mcp",
        "https://10.0.0.5/mcp",
        "https://[::1]/mcp",
        "https://user:pw@mcp.example.com/mcp",
        "https://mcp.example.com/mcp#frag",
        "ftp://mcp.example.com/mcp",
        " https://mcp.example.com/mcp",
        "",
    ],
)
def test_non_public_or_malformed_servers_are_rejected(
    url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS", raising=False)
    with pytest.raises(EndpointPolicyError) as excinfo:
        validate_mcp_server_url(url)
    assert "example" not in str(excinfo.value)


def test_operator_allowlist_admits_exact_private_origins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS", "http://fake-mcp:8080")
    assert validate_mcp_server_url("http://fake-mcp:8080/mcp") == "http://fake-mcp:8080/mcp"
    with pytest.raises(EndpointPolicyError):
        validate_mcp_server_url("http://fake-mcp:8081/mcp")


def test_connector_settings_validation_is_value_free(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS", raising=False)
    connector = McpConnector()
    good = connector.validate_settings(
        "bearer", {"server_url": "https://mcp.example.com/mcp", "server_slug": "ex"}
    )
    assert good["transport"] == "auto"
    with pytest.raises(ValueError, match="server_slug"):
        connector.validate_settings(
            "bearer", {"server_url": "https://mcp.example.com/mcp", "server_slug": "Bad-Slug"}
        )
    with pytest.raises(ValueError) as excinfo:
        connector.validate_settings(
            "bearer", {"server_url": "http://secret-host.internal/mcp", "server_slug": "ok"}
        )
    assert "secret-host" not in str(excinfo.value)
    with pytest.raises(ValueError, match="transport"):
        connector.validate_settings(
            "none",
            {"server_url": "https://mcp.example.com/mcp", "server_slug": "ok", "transport": "x"},
        )
    with pytest.raises(ValueError, match="header_name"):
        connector.validate_settings(
            "header",
            {
                "server_url": "https://mcp.example.com/mcp",
                "server_slug": "ok",
                "header_name": "Authorization",
            },
        )


def test_auth_headers_per_scheme() -> None:
    assert auth_headers("none", {}, {}) == {}
    assert auth_headers("bearer", {"token": "abc"}, {}) == {"Authorization": "Bearer abc"}
    assert auth_headers("header", {"token": "abc"}, {"header_name": "X-API-Key"}) == {
        "X-API-Key": "abc"
    }
    with pytest.raises(ValueError, match="no token"):
        auth_headers("bearer", {}, {})
    with pytest.raises(ValueError, match="malformed"):
        auth_headers("bearer", {"token": "a\r\nb"}, {})
    for reserved in ("Host", "content-length", "Mcp-Session-Id"):
        with pytest.raises(ValueError, match="reserved"):
            validate_header_name(reserved)


def test_convert_result_strips_binary_and_bounds_text() -> None:
    result = mcp_types.CallToolResult(
        content=[
            mcp_types.TextContent(type="text", text="x" * (MAX_TEXT_CHARS + 50)),
            mcp_types.ImageContent(type="image", data="AAAA", mimeType="image/png"),
            mcp_types.AudioContent(type="audio", data="AAAA", mimeType="audio/wav"),
            mcp_types.EmbeddedResource(
                type="resource",
                resource=mcp_types.BlobResourceContents(
                    uri="file:///blob", blob="AAAA", mimeType="application/octet-stream"
                ),
            ),
            mcp_types.ResourceLink(type="resource_link", uri="https://x/y", name="y"),
        ],
        structuredContent={"big": "z" * 9_000},
        isError=True,
    )
    output = convert_result("mcp.fake.echo", result)
    assert output.is_error and output.truncated
    assert len(output.text) == MAX_TEXT_CHARS and output.text.endswith("…[truncated]")
    assert output.structured_content is None
    kinds = [block["type"] for block in output.content]
    assert kinds == ["text", "image", "audio", "resource", "resource_link"]
    assert all(
        block.get("omitted")
        for block in output.content
        if block["type"] not in {"text", "resource_link"}
    )
    assert "AAAA" not in output.model_dump_json()
    assert output.notice == UNTRUSTED_NOTICE


def test_registry_ships_mcp_and_catalog_is_well_formed() -> None:
    registry = default_registry()
    connector = registry.get("mcp")
    assert connector is not None and connector.manifest.supports_webhooks is False
    assert connector.tools() == () and connector.tool_definitions() == ()
    entries = load_catalog()
    assert len(entries) >= 40
    assert {entry.category for entry in entries} <= set(CATALOG_CATEGORIES)
    for native in ("github", "linear", "vercel", "supabase"):
        entry = next(item for item in entries if item.slug == native)
        assert entry.connector_type == native
    # Every entry offers a way to connect: a native connector, a known MCP
    # endpoint, or an explicitly unverified URL the user supplies.
    assert all(entry.connector_type or entry.mcp_url or entry.url_unverified for entry in entries)
    assert all(entry.setup_note for entry in entries if entry.stdio_only)


def test_catalog_icon_url_accepts_only_the_github_avatar_shape() -> None:
    """The producer projects ``icon_url`` in exactly one shape (GitHub's owner
    avatar); the model here is the consumer's own reading of that rule, so a
    catalog.json from anything else cannot point the icon proxy off-host."""
    base = {
        "slug": "acme",
        "name": "Acme",
        "category": "Developer tools",
        "icon": "mcp",
        "description": "Acme tools.",
    }
    entry = CatalogApp.model_validate({**base, "icon_url": "https://github.com/acme.png?size=128"})
    assert entry.icon_url == "https://github.com/acme.png?size=128"
    assert CatalogApp.model_validate(base).icon_url == ""
    for hostile in (
        "https://evil.example/acme.png?size=128",
        "https://github.com/acme/repo.png?size=128",
        "https://github.com/acme.png",
        "javascript:alert(1)",
    ):
        with pytest.raises(ValueError):
            CatalogApp.model_validate({**base, "icon_url": hostile})


async def test_verify_reports_policy_failures_without_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS", raising=False)
    health = await McpConnector().verify_connection(
        VerifyContext(
            auth_type="bearer",
            credentials={"token": "super-secret"},
            config={"server_url": "http://10.1.2.3/mcp", "server_slug": "x"},
        )
    )
    assert not health.ok
    assert "super-secret" not in health.message and "10.1.2.3" not in health.message
