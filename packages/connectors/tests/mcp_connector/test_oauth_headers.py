"""The two mechanical guards on an MCP OAuth connection.

A stored access token becomes a request header only when the token itself is
usable *and* the endpoint about to be dialled is still the one the token was
issued for; and a server's ``WWW-Authenticate`` is read for exactly three
facts, none of which is free text. Both are enforced without touching a
network, so they are tested that way.
"""

import pytest

from jhin_connectors.mcp.oauth import (
    CHALLENGE_ERROR_CODES,
    MAX_SCOPE_ENTRIES,
    OAUTH_ISSUER_KEY,
    OAUTH_RESOURCE_KEY,
    OAUTH_SCOPE_KEY,
    OAUTH_STEP_UPS_KEY,
    UNKNOWN_CHALLENGE_ERROR,
    McpOAuthConfigError,
    challenge_from_response,
    merge_scope,
    oauth_auth_headers,
    parse_scope,
    resource_binding,
    validate_oauth_config,
)

SERVER = "https://mcp.example.com/mcp"
TOKEN = "fake-access-token-value-for-tests"
CONFIG = {OAUTH_RESOURCE_KEY: SERVER, OAUTH_ISSUER_KEY: "https://auth.example.com"}


def test_bearer_header_carries_the_stored_access_token() -> None:
    headers = oauth_auth_headers(
        {"access_token": TOKEN, "token_type": "Bearer"}, CONFIG, validated_server_url=SERVER
    )
    assert headers == {"Authorization": f"Bearer {TOKEN}"}
    # A provider that lower-cases the type is still speaking Bearer.
    assert oauth_auth_headers(
        {"access_token": TOKEN, "token_type": "bearer"}, CONFIG, validated_server_url=SERVER
    ) == {"Authorization": f"Bearer {TOKEN}"}
    # An absent token_type is the RFC default.
    assert oauth_auth_headers({"access_token": TOKEN}, CONFIG, validated_server_url=SERVER) == {
        "Authorization": f"Bearer {TOKEN}"
    }


@pytest.mark.parametrize("suffix", ["\r\nX-Evil: 1", "\nX-Evil: 1", "\0"])
def test_a_token_that_could_forge_a_header_is_refused(suffix: str) -> None:
    poisoned = TOKEN + suffix
    with pytest.raises(McpOAuthConfigError) as caught:
        oauth_auth_headers({"access_token": poisoned}, CONFIG, validated_server_url=SERVER)
    assert TOKEN not in str(caught.value)
    assert "malformed" in str(caught.value)


def test_a_missing_or_non_bearer_token_is_refused() -> None:
    with pytest.raises(McpOAuthConfigError, match="stores no OAuth access token"):
        oauth_auth_headers({}, CONFIG, validated_server_url=SERVER)
    with pytest.raises(McpOAuthConfigError, match="not a bearer token"):
        oauth_auth_headers(
            {"access_token": TOKEN, "token_type": "mac"}, CONFIG, validated_server_url=SERVER
        )


def test_a_token_is_never_sent_to_a_server_it_was_not_authorized_for() -> None:
    """The token-passthrough prohibition. Editing ``server_url`` after
    authorization must not silently re-point the credential."""
    with pytest.raises(McpOAuthConfigError) as caught:
        oauth_auth_headers(
            {"access_token": TOKEN},
            CONFIG,
            validated_server_url="https://evil.example.com/mcp",
        )
    message = str(caught.value)
    assert "no longer matches the account it was authorized for" in message
    assert TOKEN not in message
    assert "evil.example.com" not in message

    with pytest.raises(McpOAuthConfigError, match="records no authorized resource"):
        oauth_auth_headers({"access_token": TOKEN}, {}, validated_server_url=SERVER)


def test_the_audience_is_the_canonical_form_of_the_endpoint() -> None:
    assert resource_binding("HTTPS://MCP.Example.com:443/mcp/") == "https://mcp.example.com/mcp"
    # A trailing slash on the stored URL and on the dialled one agree.
    config = {OAUTH_RESOURCE_KEY: resource_binding(SERVER + "/")}
    assert oauth_auth_headers({"access_token": TOKEN}, config, validated_server_url=SERVER)


def test_a_401_challenge_yields_the_metadata_url_and_scope() -> None:
    challenge = challenge_from_response(
        401,
        {
            "WWW-Authenticate": (
                'Bearer realm="mcp", error="invalid_token", '
                'error_description="go away", '
                'resource_metadata="https://mcp.example.com/.well-known/'
                'oauth-protected-resource/mcp", scope="mcp:read mcp:write"'
            )
        },
    )
    assert challenge is not None
    assert challenge.status_code == 401
    assert challenge.error == "invalid_token"
    assert challenge.token_rejected is True
    assert challenge.needs_more_scope is False
    assert challenge.resource_metadata_url == (
        "https://mcp.example.com/.well-known/oauth-protected-resource/mcp"
    )
    assert challenge.scope == ("mcp:read", "mcp:write")
    # The server's prose is never carried anywhere.
    assert "go away" not in repr(challenge)


def test_an_insufficient_scope_403_is_a_step_up_not_a_refresh() -> None:
    challenge = challenge_from_response(
        403, {"www-authenticate": 'Bearer error="insufficient_scope", scope="mcp:admin"'}
    )
    assert challenge is not None
    assert challenge.needs_more_scope is True
    assert challenge.token_rejected is False
    assert challenge.scope == ("mcp:admin",)


def test_unknown_error_codes_and_missing_headers_stay_inside_the_vocabulary() -> None:
    unknown = challenge_from_response(401, {"www-authenticate": 'Bearer error="teapot"'})
    assert unknown is not None and unknown.error == UNKNOWN_CHALLENGE_ERROR
    assert unknown.error not in CHALLENGE_ERROR_CODES

    bare = challenge_from_response(401, {})
    assert bare is not None
    assert bare.error == "" and bare.scope == () and bare.resource_metadata_url is None
    # A bare 401 is still a rejected token: the status code says so on its own.
    assert bare.token_rejected is True

    garbage = challenge_from_response(403, {"www-authenticate": "!!! not a challenge"})
    assert garbage is not None and garbage.error == ""

    live = {"www-authenticate": 'Bearer error="invalid_token"'}
    assert challenge_from_response(200, live) is None
    assert challenge_from_response(500, {}) is None


def test_an_oversized_metadata_url_is_dropped_rather_than_carried() -> None:
    huge = "https://mcp.example.com/" + "a" * 3_000
    header = {"www-authenticate": f'Bearer resource_metadata="{huge}"'}
    challenge = challenge_from_response(401, header)
    assert challenge is not None
    assert challenge.resource_metadata_url is None


def test_scope_parsing_drops_wildcards_and_anything_outside_the_grammar() -> None:
    assert parse_scope("read write read") == ("read", "write")
    assert parse_scope("* all full-access read") == ("read",)
    assert parse_scope('bad"quote read') == ("read",)
    assert parse_scope(None) == () and parse_scope("") == ()
    assert parse_scope("x" * 200 + " read") == ("read",)
    crowded = " ".join(f"scope{index}" for index in range(200))
    assert len(parse_scope(crowded)) == MAX_SCOPE_ENTRIES


def test_merging_a_challenge_scope_keeps_what_the_connection_already_holds() -> None:
    assert merge_scope("read write", ["write", "admin"]) == "read write admin"
    assert merge_scope("", ["admin"]) == "admin"
    assert merge_scope("read", []) == "read"
    # A wildcard a server asks for is never carried into what we would request.
    assert merge_scope("read", ["*"]) == "read"


def test_stored_oauth_settings_are_shape_checked_not_trusted() -> None:
    checked = validate_oauth_config(
        {
            OAUTH_RESOURCE_KEY: SERVER,
            OAUTH_ISSUER_KEY: "https://auth.example.com",
            OAUTH_SCOPE_KEY: "read  *  write",
            OAUTH_STEP_UPS_KEY: {"mcp.fake.echo": "not-a-timestamp", 7: "x"},
        }
    )
    assert checked[OAUTH_SCOPE_KEY] == "read write"
    assert checked[OAUTH_STEP_UPS_KEY] == {}
    assert validate_oauth_config({}) == {}

    with pytest.raises(McpOAuthConfigError, match="oauth_resource"):
        validate_oauth_config({OAUTH_RESOURCE_KEY: 42})
    with pytest.raises(McpOAuthConfigError, match="oauth_issuer"):
        validate_oauth_config({OAUTH_ISSUER_KEY: "x" * 501})
