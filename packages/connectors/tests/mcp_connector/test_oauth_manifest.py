"""What the MCP manifest promises about the OAuth scheme.

Two properties matter and neither is obvious from reading the manifest:
the scheme collects **no** secret fields, so the connections service does not
demand a token that will not exist until the authorization flow finishes; and
the settings OAuth writes into ``config_json`` are internal — not manifest
config fields, so they can never be submitted through the public create path
and never come back out of it.
"""

import pytest

from jhin_connectors.manifest import normalize_config
from jhin_connectors.mcp.connector import McpConnector
from jhin_connectors.mcp.manifest import (
    AUTH_BEARER,
    AUTH_HEADER,
    AUTH_NONE,
    AUTH_OAUTH,
    MCP_MANIFEST,
)
from jhin_connectors.mcp.oauth import (
    OAUTH_CONFIG_KEYS,
    OAUTH_ISSUER_KEY,
    OAUTH_RESOURCE_KEY,
    OAUTH_SCOPE_KEY,
    McpOAuthConfigError,
)

SERVER = "https://mcp.example.com/mcp"


def test_the_oauth_scheme_is_offered_first_and_asks_for_nothing() -> None:
    scheme = MCP_MANIFEST.auth_scheme(AUTH_OAUTH)
    assert scheme is not None
    assert MCP_MANIFEST.auth_schemes[0].type == AUTH_OAUTH
    # Zero secret fields: _validate_credentials has nothing to require, so a
    # create request that carries no credentials at all is valid.
    assert scheme.secret_fields == ()
    assert scheme.required_field_names() == ()


def test_the_static_schemes_are_untouched() -> None:
    for auth_type in (AUTH_BEARER, AUTH_HEADER):
        scheme = MCP_MANIFEST.auth_scheme(auth_type)
        assert scheme is not None
        assert scheme.required_field_names() == ("token",)
    none_scheme = MCP_MANIFEST.auth_scheme(AUTH_NONE)
    assert none_scheme is not None and none_scheme.secret_fields == ()


def test_the_oauth_settings_are_internal_and_unreachable_from_the_public_path() -> None:
    declared = {field.name for field in MCP_MANIFEST.config_fields}
    assert declared.isdisjoint(OAUTH_CONFIG_KEYS)
    # public_connection_config projects only manifest-declared fields, so a
    # key that is not one of them cannot be serialized to a client.
    with pytest.raises(ValueError, match=OAUTH_RESOURCE_KEY):
        normalize_config(
            MCP_MANIFEST,
            AUTH_OAUTH,
            {"server_url": SERVER, "server_slug": "x", OAUTH_RESOURCE_KEY: SERVER},
        )


def test_the_header_name_field_belongs_to_the_custom_header_scheme_only() -> None:
    field = next(item for item in MCP_MANIFEST.config_fields if item.name == "header_name")
    assert field.auth_types == (AUTH_HEADER,)
    normalized = normalize_config(
        MCP_MANIFEST, AUTH_OAUTH, {"server_url": SERVER, "server_slug": "x"}
    )
    assert "header_name" not in normalized


def test_an_oauth_connection_is_given_the_audience_its_endpoint_implies() -> None:
    settings = McpConnector().validate_settings(
        AUTH_OAUTH, {"server_url": "https://mcp.example.com:443/mcp/", "server_slug": "fake"}
    )
    assert settings[OAUTH_RESOURCE_KEY] == "https://mcp.example.com/mcp"


def test_settings_validation_keeps_the_values_the_authorization_flow_wrote() -> None:
    settings = McpConnector().validate_settings(
        AUTH_OAUTH,
        {
            "server_url": SERVER,
            "server_slug": "fake",
            OAUTH_RESOURCE_KEY: SERVER,
            OAUTH_ISSUER_KEY: "https://auth.example.com",
            OAUTH_SCOPE_KEY: "mcp:read  *  mcp:write",
        },
    )
    assert settings[OAUTH_RESOURCE_KEY] == SERVER
    assert settings[OAUTH_ISSUER_KEY] == "https://auth.example.com"
    assert settings[OAUTH_SCOPE_KEY] == "mcp:read mcp:write"


def test_a_tampered_oauth_setting_fails_validation_rather_than_being_used() -> None:
    with pytest.raises(McpOAuthConfigError, match=OAUTH_ISSUER_KEY):
        McpConnector().validate_settings(
            AUTH_OAUTH,
            {"server_url": SERVER, "server_slug": "fake", OAUTH_ISSUER_KEY: ["not", "text"]},
        )


def test_the_static_schemes_gain_no_oauth_settings() -> None:
    settings = McpConnector().validate_settings(
        AUTH_BEARER, {"server_url": SERVER, "server_slug": "fake"}
    )
    assert not set(settings) & set(OAUTH_CONFIG_KEYS)
