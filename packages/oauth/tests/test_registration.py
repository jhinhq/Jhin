"""RFC 7591 registration against a server that really registers clients.

The two assertions worth the most here are about fields RFC 7591 defaults
badly. ``grant_types`` defaults to ``["authorization_code"]``, so a client that
does not send it explicitly cannot refresh and finds out an hour later;
``token_endpoint_auth_method`` defaults to ``client_secret_basic``, so a client
that does not send it explicitly is handed a secret it then has to store
forever. Both are asserted against the request body the server actually
received.
"""

from __future__ import annotations

import httpx
import pytest
from packages.oauth.tests.conftest import StartServer

from jhin_connectors.testing.fake_oauth import FakeAsConfig, FakeAuthorizationServer
from jhin_oauth.discovery import discover_authorization_server
from jhin_oauth.errors import RegistrationError, TransientOAuthError
from jhin_oauth.registration import delete_registration, register_client
from jhin_oauth.types import AuthorizationServerMetadata, ClientCredentials
from jhin_secrets.redaction import get_redactor

REDIRECT_URI = "https://jhin.example.com/api/v1/oauth/callback"


async def _metadata(
    http_client: httpx.AsyncClient, server: FakeAuthorizationServer
) -> AuthorizationServerMetadata:
    return await discover_authorization_server(http_client, server.issuer)


async def test_registration_sends_grant_types_and_auth_method_explicitly(
    http_client: httpx.AsyncClient, fake_as: FakeAuthorizationServer
) -> None:
    metadata = await _metadata(http_client, fake_as)

    credentials = await register_client(
        http_client,
        metadata,
        redirect_uri=REDIRECT_URI,
        client_name="Jhin",
        client_uri="https://jhin.example.com",
        scopes="read write",
    )

    assert credentials.client_id
    body = fake_as.recorded_requests(path_suffix="/register")[-1]["body"]
    assert body["redirect_uris"] == [REDIRECT_URI]
    assert body["grant_types"] == ["authorization_code", "refresh_token"]
    assert body["response_types"] == ["code"]
    assert body["token_endpoint_auth_method"] == "none"
    assert body["application_type"] == "web"
    assert body["client_name"] == "Jhin"
    assert body["client_uri"] == "https://jhin.example.com"
    assert body["scope"] == "read write"


async def test_a_public_client_is_registered_with_no_secret_at_all(
    http_client: httpx.AsyncClient, fake_as: FakeAuthorizationServer
) -> None:
    metadata = await _metadata(http_client, fake_as)
    credentials = await register_client(
        http_client, metadata, redirect_uri=REDIRECT_URI, client_name="Jhin"
    )
    assert credentials.client_secret is None
    assert credentials.token_endpoint_auth_method == "none"


@pytest.mark.parametrize("status", [200, 201])
async def test_both_success_statuses_are_accepted(
    http_client: httpx.AsyncClient, start_server: StartServer, status: int
) -> None:
    server = start_server(FakeAsConfig(registration_status=status))
    metadata = await _metadata(http_client, server)
    credentials = await register_client(
        http_client, metadata, redirect_uri=REDIRECT_URI, client_name="Jhin"
    )
    assert credentials.client_id


async def test_rfc_7592_fields_are_captured_when_present(
    http_client: httpx.AsyncClient, fake_as: FakeAuthorizationServer
) -> None:
    metadata = await _metadata(http_client, fake_as)
    credentials = await register_client(
        http_client, metadata, redirect_uri=REDIRECT_URI, client_name="Jhin"
    )
    assert credentials.registration_access_token is not None
    assert credentials.registration_client_uri is not None
    assert credentials.registration_client_uri.startswith(fake_as.base_url)


async def test_registration_succeeds_when_rfc_7592_fields_are_absent(
    http_client: httpx.AsyncClient, start_server: StartServer
) -> None:
    # They are RFC 7592 extensions, not RFC 7591 fields. A server that does not
    # offer them has still registered the client.
    server = start_server(FakeAsConfig(registration_includes_rfc7592=False))
    metadata = await _metadata(http_client, server)
    credentials = await register_client(
        http_client, metadata, redirect_uri=REDIRECT_URI, client_name="Jhin"
    )
    assert credentials.client_id
    assert credentials.registration_access_token is None
    assert credentials.registration_client_uri is None


async def test_an_issued_secret_is_honoured_and_registered_for_redaction(
    http_client: httpx.AsyncClient, start_server: StartServer
) -> None:
    server = start_server(
        FakeAsConfig(registration_issues_secret=True, registration_auth_method="client_secret_post")
    )
    metadata = await _metadata(http_client, server)
    credentials = await register_client(
        http_client, metadata, redirect_uri=REDIRECT_URI, client_name="Jhin"
    )

    assert credentials.client_secret is not None
    assert credentials.token_endpoint_auth_method == "client_secret_post"
    assert credentials.client_secret_expires_at is None  # 0 means "never"
    scrubbed = get_redactor().redact_text(f"leaked {credentials.client_secret}")
    assert credentials.client_secret not in scrubbed


async def test_a_secret_without_a_declared_method_defaults_to_basic(
    http_client: httpx.AsyncClient, start_server: StartServer
) -> None:
    # RFC 7591 §2's own default. Believing our own "none" request instead would
    # send an unauthenticated token request to a confidential client.
    server = start_server(
        FakeAsConfig(registration_issues_secret=True, registration_auth_method="unknown-method")
    )
    metadata = await _metadata(http_client, server)
    credentials = await register_client(
        http_client, metadata, redirect_uri=REDIRECT_URI, client_name="Jhin"
    )
    assert credentials.token_endpoint_auth_method == "client_secret_basic"


async def test_invalid_redirect_uri_retries_exactly_once_as_a_native_application(
    http_client: httpx.AsyncClient, start_server: StartServer
) -> None:
    server = start_server(FakeAsConfig(reject_web_redirect_uri=True))
    metadata = await _metadata(http_client, server)

    credentials = await register_client(
        http_client, metadata, redirect_uri=REDIRECT_URI, client_name="Jhin"
    )

    assert credentials.client_id
    attempts = server.recorded_requests(path_suffix="/register")
    assert [attempt["body"]["application_type"] for attempt in attempts] == ["web", "native"]


async def test_the_native_retry_is_not_repeated(
    http_client: httpx.AsyncClient, start_server: StartServer
) -> None:
    server = start_server(FakeAsConfig(registration_error="invalid_redirect_uri"))
    metadata = await _metadata(http_client, server)

    with pytest.raises(RegistrationError):
        await register_client(http_client, metadata, redirect_uri=REDIRECT_URI, client_name="Jhin")

    assert len(server.recorded_requests(path_suffix="/register")) == 2


async def test_another_registration_error_is_not_retried(
    http_client: httpx.AsyncClient, start_server: StartServer
) -> None:
    server = start_server(FakeAsConfig(registration_error="invalid_client_metadata"))
    metadata = await _metadata(http_client, server)

    with pytest.raises(RegistrationError):
        await register_client(http_client, metadata, redirect_uri=REDIRECT_URI, client_name="Jhin")

    assert len(server.recorded_requests(path_suffix="/register")) == 1


async def test_a_server_with_no_registration_endpoint_refuses_immediately(
    http_client: httpx.AsyncClient, start_server: StartServer
) -> None:
    server = start_server(FakeAsConfig(supports_dcr=False))
    metadata = await _metadata(http_client, server)
    assert metadata.registration_endpoint is None

    with pytest.raises(RegistrationError):
        await register_client(http_client, metadata, redirect_uri=REDIRECT_URI, client_name="Jhin")


async def test_an_unreachable_registration_endpoint_is_transient(
    http_client: httpx.AsyncClient, fake_as: FakeAuthorizationServer
) -> None:
    metadata = await _metadata(http_client, fake_as)
    fake_as.stop()

    with pytest.raises(TransientOAuthError):
        await register_client(http_client, metadata, redirect_uri=REDIRECT_URI, client_name="Jhin")


async def test_delete_registration_is_best_effort_and_never_raises(
    http_client: httpx.AsyncClient, fake_as: FakeAuthorizationServer
) -> None:
    metadata = await _metadata(http_client, fake_as)
    credentials = await register_client(
        http_client, metadata, redirect_uri=REDIRECT_URI, client_name="Jhin"
    )

    await delete_registration(http_client, credentials)
    assert credentials.client_id not in fake_as.registered_clients()

    # Nothing to delete, an unreachable server, and a refusing server are all
    # the same outcome: silence.
    await delete_registration(http_client, ClientCredentials(client_id="x"))
    await delete_registration(http_client, credentials)
    fake_as.stop()
    await delete_registration(http_client, credentials)
