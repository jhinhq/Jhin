"""Authorization URLs, code exchange, refresh, revocation.

The flow is driven end to end against a server that verifies PKCE for real:
the authorization URL is built, handed to the fake's ``/authorize``, and the
code that comes back is exchanged. A wrong verifier fails, which is the only
way to know the challenge was ever checked.

The classification tests matter as much as the happy path. ``invalid_grant``
must be terminal and ``invalid_client`` must be recoverable-by-re-registration,
because the refresh loop branches on exactly that distinction, and getting it
backwards either strands a working connection or hammers a provider that has
already said no.
"""

from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import parse_qs, parse_qsl, urlsplit

import httpx
import pytest
from packages.oauth.tests.conftest import StartServer

from jhin_connectors.testing.fake_oauth import FakeAsConfig, FakeAuthorizationServer
from jhin_oauth.discovery import discover_authorization_server
from jhin_oauth.errors import (
    ClientForgottenError,
    InvalidGrantError,
    TokenError,
    TransientOAuthError,
)
from jhin_oauth.pkce import generate_pkce, generate_state
from jhin_oauth.registration import register_client
from jhin_oauth.tokens import (
    build_authorization_url,
    exchange_code,
    refresh_access_token,
    revoke_token,
)
from jhin_oauth.types import (
    AuthorizationServerMetadata,
    ClientCredentials,
    PkcePair,
    TokenResponse,
)
from jhin_secrets.redaction import get_redactor

REDIRECT_URI = "https://jhin.example.com/api/v1/oauth/callback"
RESOURCE = "https://mcp.example.com/mcp"

STATIC_METADATA = AuthorizationServerMetadata(
    issuer="https://as.example.com",
    authorization_endpoint="https://as.example.com/authorize",
    token_endpoint="https://as.example.com/token",
    code_challenge_methods_supported=("S256",),
)


def _query(url: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(url).query, keep_blank_values=True)


def _code_from(location: str) -> str:
    return _query(location)["code"][0]


# --- the authorization request --------------------------------------------


def test_the_authorization_url_carries_state_pkce_and_the_audience() -> None:
    pkce = generate_pkce()
    state = generate_state()

    url = build_authorization_url(
        STATIC_METADATA,
        client_id="client-123",
        redirect_uri=REDIRECT_URI,
        state=state,
        pkce=pkce,
        scope="read write",
        resource=RESOURCE,
    )

    params = _query(url)
    assert url.startswith("https://as.example.com/authorize?")
    assert params["response_type"] == ["code"]
    assert params["client_id"] == ["client-123"]
    assert params["redirect_uri"] == [REDIRECT_URI]
    assert params["state"] == [state]
    assert params["code_challenge"] == [pkce.challenge]
    assert params["code_challenge_method"] == ["S256"]
    assert params["resource"] == [RESOURCE]
    assert params["scope"] == ["read write"]


def test_the_authorization_url_never_carries_a_secret() -> None:
    pkce = generate_pkce()
    url = build_authorization_url(
        STATIC_METADATA,
        client_id="client-123",
        redirect_uri=REDIRECT_URI,
        state=generate_state(),
        pkce=pkce,
        scope="",
        resource=RESOURCE,
    )
    assert pkce.verifier not in url
    assert "scope=" not in url
    for forbidden in ("access_token", "refresh_token", "client_secret", "code_verifier"):
        assert forbidden not in url


def test_the_challenge_is_sent_even_when_the_provider_ignores_pkce() -> None:
    # Notion and Atlassian have no PKCE. There is deliberately no branch that
    # skips generating or sending one: an unknown parameter costs nothing, and
    # a branch is something that can be taken by mistake.
    no_pkce = AuthorizationServerMetadata(
        issuer="https://as.example.com",
        authorization_endpoint="https://as.example.com/authorize",
        token_endpoint="https://as.example.com/token",
        code_challenge_methods_supported=(),
    )
    url = build_authorization_url(
        no_pkce,
        client_id="c",
        redirect_uri=REDIRECT_URI,
        state=generate_state(),
        pkce=generate_pkce(),
        scope="",
        resource=RESOURCE,
    )
    assert "code_challenge_method=S256" in url


def test_extra_params_carry_provider_quirks() -> None:
    url = build_authorization_url(
        STATIC_METADATA,
        client_id="c",
        redirect_uri=REDIRECT_URI,
        state=generate_state(),
        pkce=generate_pkce(),
        scope="",
        resource=RESOURCE,
        extra_params={"prompt": "consent", "owner": "user"},
    )
    params = _query(url)
    assert params["prompt"] == ["consent"]
    assert params["owner"] == ["user"]


@pytest.mark.parametrize(
    "key", ["state", "code_challenge", "redirect_uri", "client_id", "resource", "scope"]
)
def test_extra_params_may_not_shadow_a_parameter_the_builder_owns(key: str) -> None:
    with pytest.raises(ValueError):
        build_authorization_url(
            STATIC_METADATA,
            client_id="c",
            redirect_uri=REDIRECT_URI,
            state=generate_state(),
            pkce=generate_pkce(),
            scope="",
            resource=RESOURCE,
            extra_params={key: "attacker-chosen"},
        )


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"state": ""}, "no state"),
        ({"state": "s" * 300}, "over-long state"),
        ({"resource": "r" * 1200}, "over-long resource"),
        ({"scope": "s" * 3000}, "over-long scope"),
        ({"client_id": ""}, "no client id"),
        ({"redirect_uri": ""}, "no redirect URI"),
    ],
)
def test_the_builder_refuses_a_request_it_cannot_bind(kwargs: dict[str, str], reason: str) -> None:
    arguments: dict[str, object] = {
        "client_id": "c",
        "redirect_uri": REDIRECT_URI,
        "state": generate_state(),
        "pkce": generate_pkce(),
        "scope": "",
        "resource": RESOURCE,
    }
    arguments.update(kwargs)
    with pytest.raises(ValueError):
        build_authorization_url(STATIC_METADATA, **arguments)  # type: ignore[arg-type]


def test_a_provider_with_no_resource_concept_gets_no_resource_parameter() -> None:
    """An empty resource is omitted, not sent blank.

    A statically-known provider like GitHub has no RFC 8707 audience at all.
    RFC 8707 requires an absolute URI, so ``resource=`` is malformed rather
    than harmlessly ignored, and a strict server answers ``invalid_target``.
    """
    url = build_authorization_url(
        STATIC_METADATA,
        client_id="c",
        redirect_uri=REDIRECT_URI,
        state=generate_state(),
        pkce=generate_pkce(),
        scope="",
        resource="",
    )
    query = dict(parse_qsl(urlsplit(url).query, keep_blank_values=True))
    assert "resource" not in query
    # The rest of the binding is untouched by the omission.
    assert query["code_challenge_method"] == "S256"
    assert query["response_type"] == "code"


def test_an_endpoint_query_string_is_preserved() -> None:
    metadata = AuthorizationServerMetadata(
        issuer="https://as.example.com",
        authorization_endpoint="https://as.example.com/authorize?tenant=acme",
        token_endpoint="https://as.example.com/token",
    )
    url = build_authorization_url(
        metadata,
        client_id="c",
        redirect_uri=REDIRECT_URI,
        state=generate_state(),
        pkce=generate_pkce(),
        scope="",
        resource=RESOURCE,
    )
    assert _query(url)["tenant"] == ["acme"]


# --- the round trip --------------------------------------------------------


async def _register(
    http_client: httpx.AsyncClient, server: FakeAuthorizationServer
) -> tuple[AuthorizationServerMetadata, ClientCredentials]:
    metadata = await discover_authorization_server(http_client, server.issuer)
    credentials = await register_client(
        http_client, metadata, redirect_uri=REDIRECT_URI, client_name="Jhin"
    )
    return metadata, credentials


async def _authorize(
    server: FakeAuthorizationServer,
    metadata: AuthorizationServerMetadata,
    credentials: ClientCredentials,
    pkce: PkcePair,
    *,
    scope: str = "read",
) -> str:
    url = build_authorization_url(
        metadata,
        client_id=credentials.client_id,
        redirect_uri=REDIRECT_URI,
        state=generate_state(),
        pkce=pkce,
        scope=scope,
        resource=RESOURCE,
    )
    return _code_from(server.authorize(url))


async def test_a_full_authorization_round_trip(
    http_client: httpx.AsyncClient, fake_as: FakeAuthorizationServer
) -> None:
    metadata, credentials = await _register(http_client, fake_as)
    pkce = generate_pkce()
    code = await _authorize(fake_as, metadata, credentials, pkce)

    tokens = await exchange_code(
        http_client,
        metadata,
        credentials=credentials,
        code=code,
        redirect_uri=REDIRECT_URI,
        code_verifier=pkce.verifier,
        resource=RESOURCE,
    )

    assert isinstance(tokens, TokenResponse)
    assert tokens.access_token
    assert tokens.refresh_token
    assert tokens.token_type == "Bearer"
    assert tokens.issuer == metadata.issuer
    assert tokens.expires_at is not None
    assert tokens.expires_at > datetime.now(UTC)
    assert tokens.scope == "read"

    body = fake_as.recorded_requests(path_suffix="/token")[-1]["body"]
    assert body["grant_type"] == "authorization_code"
    assert body["resource"] == RESOURCE
    assert body["code_verifier"] == pkce.verifier
    assert body["redirect_uri"] == REDIRECT_URI


async def test_the_pkce_challenge_is_really_verified(
    http_client: httpx.AsyncClient, fake_as: FakeAuthorizationServer
) -> None:
    metadata, credentials = await _register(http_client, fake_as)
    code = await _authorize(fake_as, metadata, credentials, generate_pkce())

    with pytest.raises(InvalidGrantError):
        await exchange_code(
            http_client,
            metadata,
            credentials=credentials,
            code=code,
            redirect_uri=REDIRECT_URI,
            code_verifier=generate_pkce().verifier,
            resource=RESOURCE,
        )


async def test_a_code_is_single_use(
    http_client: httpx.AsyncClient, fake_as: FakeAuthorizationServer
) -> None:
    metadata, credentials = await _register(http_client, fake_as)
    pkce = generate_pkce()
    code = await _authorize(fake_as, metadata, credentials, pkce)

    exchange = {
        "credentials": credentials,
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "code_verifier": pkce.verifier,
        "resource": RESOURCE,
    }
    await exchange_code(http_client, metadata, **exchange)  # type: ignore[arg-type]
    with pytest.raises(InvalidGrantError):
        await exchange_code(http_client, metadata, **exchange)  # type: ignore[arg-type]


async def test_tokens_are_registered_for_redaction_at_first_possession(
    http_client: httpx.AsyncClient, fake_as: FakeAuthorizationServer
) -> None:
    metadata, credentials = await _register(http_client, fake_as)
    pkce = generate_pkce()
    code = await _authorize(fake_as, metadata, credentials, pkce)
    tokens = await exchange_code(
        http_client,
        metadata,
        credentials=credentials,
        code=code,
        redirect_uri=REDIRECT_URI,
        code_verifier=pkce.verifier,
        resource=RESOURCE,
    )

    redactor = get_redactor()
    assert tokens.refresh_token is not None
    for value in (tokens.access_token, tokens.refresh_token):
        assert value not in redactor.redact_text(f"a log line with {value} in it")


# --- refresh ---------------------------------------------------------------


async def _tokens(
    http_client: httpx.AsyncClient, server: FakeAuthorizationServer
) -> tuple[AuthorizationServerMetadata, ClientCredentials, TokenResponse]:
    metadata, credentials = await _register(http_client, server)
    pkce = generate_pkce()
    code = await _authorize(server, metadata, credentials, pkce)
    tokens = await exchange_code(
        http_client,
        metadata,
        credentials=credentials,
        code=code,
        redirect_uri=REDIRECT_URI,
        code_verifier=pkce.verifier,
        resource=RESOURCE,
    )
    return metadata, credentials, tokens


async def test_a_rotating_server_replaces_the_refresh_token(
    http_client: httpx.AsyncClient, fake_as: FakeAuthorizationServer
) -> None:
    metadata, credentials, tokens = await _tokens(http_client, fake_as)
    assert tokens.refresh_token is not None

    refreshed = await refresh_access_token(
        http_client,
        metadata,
        credentials=credentials,
        refresh_token=tokens.refresh_token,
        resource=RESOURCE,
    )

    assert refreshed.access_token != tokens.access_token
    assert refreshed.refresh_token is not None
    assert refreshed.refresh_token != tokens.refresh_token


async def test_a_non_rotating_server_carries_the_old_refresh_token_forward(
    http_client: httpx.AsyncClient, start_server: StartServer
) -> None:
    # Most providers do not rotate. Treating a missing refresh_token as "we no
    # longer have one" would turn every one of them into a re-authorization an
    # hour later.
    server = start_server(FakeAsConfig(rotates_refresh_tokens=False))
    metadata, credentials, tokens = await _tokens(http_client, server)
    assert tokens.refresh_token is not None

    refreshed = await refresh_access_token(
        http_client,
        metadata,
        credentials=credentials,
        refresh_token=tokens.refresh_token,
        resource=RESOURCE,
    )

    assert refreshed.refresh_token == tokens.refresh_token
    assert refreshed.access_token != tokens.access_token


async def test_refresh_sends_the_audience_again(
    http_client: httpx.AsyncClient, fake_as: FakeAuthorizationServer
) -> None:
    metadata, credentials, tokens = await _tokens(http_client, fake_as)
    assert tokens.refresh_token is not None
    await refresh_access_token(
        http_client,
        metadata,
        credentials=credentials,
        refresh_token=tokens.refresh_token,
        resource=RESOURCE,
        scope="read",
    )
    body = fake_as.recorded_requests(path_suffix="/token")[-1]["body"]
    assert body["grant_type"] == "refresh_token"
    assert body["resource"] == RESOURCE
    assert body["scope"] == "read"


async def test_a_refresh_token_lifetime_becomes_an_instant(
    http_client: httpx.AsyncClient, start_server: StartServer
) -> None:
    server = start_server(FakeAsConfig(refresh_token_lifetime_seconds=15_897_600))
    _metadata, _credentials, tokens = await _tokens(http_client, server)
    assert tokens.refresh_expires_at is not None
    assert tokens.refresh_expires_at > datetime.now(UTC)


# --- refusal classification ------------------------------------------------


async def test_invalid_grant_is_terminal(
    http_client: httpx.AsyncClient, start_server: StartServer
) -> None:
    server = start_server(FakeAsConfig(fail_token_with="invalid_grant"))
    metadata = await discover_authorization_server(http_client, server.issuer)
    server.register_static_client()

    with pytest.raises(InvalidGrantError) as caught:
        await exchange_code(
            http_client,
            metadata,
            credentials=ClientCredentials(client_id="fake-static-client"),
            code="fake-code",
            redirect_uri=REDIRECT_URI,
            code_verifier=generate_pkce().verifier,
            resource=RESOURCE,
        )
    assert caught.value.error_code == "invalid_grant"


async def test_invalid_client_is_a_forgotten_registration(
    http_client: httpx.AsyncClient, start_server: StartServer
) -> None:
    server = start_server(FakeAsConfig(forget_client_after=0))
    metadata, credentials = await _register(http_client, server)

    with pytest.raises(ClientForgottenError):
        await exchange_code(
            http_client,
            metadata,
            credentials=credentials,
            code="fake-code",
            redirect_uri=REDIRECT_URI,
            code_verifier=generate_pkce().verifier,
            resource=RESOURCE,
        )


@pytest.mark.parametrize("status", [429, 500, 503])
async def test_rate_limits_and_server_errors_are_transient(
    http_client: httpx.AsyncClient, start_server: StartServer, status: int
) -> None:
    server = start_server(
        FakeAsConfig(fail_token_with="temporarily_unavailable", token_error_status=status)
    )
    metadata = await discover_authorization_server(http_client, server.issuer)
    server.register_static_client()

    with pytest.raises(TransientOAuthError):
        await exchange_code(
            http_client,
            metadata,
            credentials=ClientCredentials(client_id="fake-static-client"),
            code="fake-code",
            redirect_uri=REDIRECT_URI,
            code_verifier=generate_pkce().verifier,
            resource=RESOURCE,
        )


async def test_an_unreachable_token_endpoint_is_transient(
    http_client: httpx.AsyncClient, fake_as: FakeAuthorizationServer
) -> None:
    metadata, credentials = await _register(http_client, fake_as)
    fake_as.stop()

    with pytest.raises(TransientOAuthError):
        await refresh_access_token(
            http_client,
            metadata,
            credentials=credentials,
            refresh_token="fake-refresh-token",
            resource=RESOURCE,
        )


async def test_a_rejected_audience_is_not_retried_without_one(
    http_client: httpx.AsyncClient, start_server: StartServer
) -> None:
    # Dropping the audience binding to make a flow succeed is the exact
    # regression the resource parameter exists to prevent.
    server = start_server(FakeAsConfig(reject_resource=True))
    metadata, credentials = await _register(http_client, server)
    pkce = generate_pkce()
    code = await _authorize(server, metadata, credentials, pkce)

    with pytest.raises(TokenError) as caught:
        await exchange_code(
            http_client,
            metadata,
            credentials=credentials,
            code=code,
            redirect_uri=REDIRECT_URI,
            code_verifier=pkce.verifier,
            resource=RESOURCE,
        )
    assert caught.value.error_code == "invalid_target"

    token_requests = server.recorded_requests(path_suffix="/token")
    assert len(token_requests) == 1
    assert all(request["body"].get("resource") for request in token_requests)


async def test_a_refusal_never_carries_provider_prose(
    http_client: httpx.AsyncClient, start_server: StartServer
) -> None:
    server = start_server(FakeAsConfig(fail_token_with="invalid_grant"))
    metadata = await discover_authorization_server(http_client, server.issuer)
    server.register_static_client()

    with pytest.raises(TokenError) as caught:
        await exchange_code(
            http_client,
            metadata,
            credentials=ClientCredentials(client_id="fake-static-client"),
            code="fake-code",
            redirect_uri=REDIRECT_URI,
            code_verifier=generate_pkce().verifier,
            resource=RESOURCE,
        )
    # "fake failure" is the description the fake server sends with every error.
    assert "fake failure" not in str(caught.value)


def test_an_unknown_error_code_degrades_to_unknown() -> None:
    assert TokenError("refused", error_code="something_invented").error_code == "unknown"
    assert TokenError("refused", error_code="invalid_scope").error_code == "invalid_scope"


# --- client authentication -------------------------------------------------


async def test_a_public_client_sends_its_id_and_no_secret(
    http_client: httpx.AsyncClient, fake_as: FakeAuthorizationServer
) -> None:
    _metadata, credentials, _issued = await _tokens(http_client, fake_as)
    body = fake_as.recorded_requests(path_suffix="/token")[-1]["body"]
    assert body["client_id"] == credentials.client_id
    assert "client_secret" not in body
    assert not fake_as.recorded_requests(path_suffix="/token")[-1]["authorization"]


async def test_client_secret_post_puts_the_secret_in_the_body(
    http_client: httpx.AsyncClient, start_server: StartServer
) -> None:
    server = start_server(FakeAsConfig())
    metadata = await discover_authorization_server(http_client, server.issuer)
    server.register_static_client(
        client_id="post-client",
        client_secret="fake-client-secret-post",
        token_endpoint_auth_method="client_secret_post",
    )
    credentials = ClientCredentials(
        client_id="post-client",
        client_secret="fake-client-secret-post",
        token_endpoint_auth_method="client_secret_post",
    )
    pkce = generate_pkce()
    code = await _authorize(server, metadata, credentials, pkce)

    await exchange_code(
        http_client,
        metadata,
        credentials=credentials,
        code=code,
        redirect_uri=REDIRECT_URI,
        code_verifier=pkce.verifier,
        resource=RESOURCE,
    )

    record = server.recorded_requests(path_suffix="/token")[-1]
    assert record["body"]["client_secret"] == "fake-client-secret-post"
    assert not record["authorization"]


async def test_client_secret_basic_puts_the_secret_in_the_header(
    http_client: httpx.AsyncClient, start_server: StartServer
) -> None:
    server = start_server(FakeAsConfig())
    metadata = await discover_authorization_server(http_client, server.issuer)
    server.register_static_client(
        client_id="basic-client",
        client_secret="fake-client-secret-basic",
        token_endpoint_auth_method="client_secret_basic",
    )
    credentials = ClientCredentials(
        client_id="basic-client",
        client_secret="fake-client-secret-basic",
        token_endpoint_auth_method="client_secret_basic",
    )
    pkce = generate_pkce()
    code = await _authorize(server, metadata, credentials, pkce)

    await exchange_code(
        http_client,
        metadata,
        credentials=credentials,
        code=code,
        redirect_uri=REDIRECT_URI,
        code_verifier=pkce.verifier,
        resource=RESOURCE,
    )

    record = server.recorded_requests(path_suffix="/token")[-1]
    assert record["authorization"].startswith("Basic ")
    assert "client_secret" not in record["body"]
    # The id stays in the body too: several servers read it from there.
    assert record["body"]["client_id"] == "basic-client"


async def test_a_confidential_client_with_no_stored_secret_refuses_before_the_request(
    http_client: httpx.AsyncClient, fake_as: FakeAuthorizationServer
) -> None:
    metadata = await discover_authorization_server(http_client, fake_as.issuer)
    before = len(fake_as.recorded_requests(path_suffix="/token"))

    with pytest.raises(TokenError) as caught:
        await refresh_access_token(
            http_client,
            metadata,
            credentials=ClientCredentials(
                client_id="c", token_endpoint_auth_method="client_secret_post"
            ),
            refresh_token="fake-refresh-token",
            resource=RESOURCE,
        )

    assert caught.value.error_code == "invalid_client"
    assert len(fake_as.recorded_requests(path_suffix="/token")) == before


# --- revocation ------------------------------------------------------------


async def test_revocation_reaches_the_provider(
    http_client: httpx.AsyncClient, fake_as: FakeAuthorizationServer
) -> None:
    metadata, credentials, tokens = await _tokens(http_client, fake_as)
    assert tokens.refresh_token is not None

    await revoke_token(
        http_client,
        metadata,
        credentials=credentials,
        token=tokens.refresh_token,
        token_type_hint="refresh_token",
    )

    assert tokens.refresh_token in fake_as.revoked_tokens()


async def test_revocation_never_raises(
    http_client: httpx.AsyncClient, start_server: StartServer
) -> None:
    # A disconnect that fails because the provider was briefly down would be a
    # worse product than one that leaves a token to expire on its own.
    without_endpoint = start_server(FakeAsConfig(supports_revocation=False))
    metadata = await discover_authorization_server(http_client, without_endpoint.issuer)
    assert metadata.revocation_endpoint is None
    await revoke_token(
        http_client,
        metadata,
        credentials=ClientCredentials(client_id="c"),
        token="fake-token",
        token_type_hint="access_token",
    )

    working = start_server(FakeAsConfig())
    working_metadata = await discover_authorization_server(http_client, working.issuer)
    working.stop()
    await revoke_token(
        http_client,
        working_metadata,
        credentials=ClientCredentials(client_id="c"),
        token="fake-token",
        token_type_hint="access_token",
    )
