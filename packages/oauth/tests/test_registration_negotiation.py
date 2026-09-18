"""DCR uses the advertised authentication contract, including local installs."""

import json

import httpx
import pytest

from jhin_oauth.errors import RegistrationError
from jhin_oauth.registration import register_client
from jhin_oauth.types import AuthorizationServerMetadata


@pytest.mark.parametrize(
    ("methods", "expected"),
    [
        (("client_secret_basic", "client_secret_post"), "client_secret_basic"),
        (("client_secret_post",), "client_secret_post"),
        (("none", "client_secret_basic"), "none"),
        ((), "client_secret_basic"),
    ],
)
async def test_registration_negotiates_supported_authentication(methods, expected):
    requests = []

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        return httpx.Response(
            201,
            json={
                "client_id": "local-client",
                "token_endpoint_auth_method": expected,
                **({"client_secret": "issued-secret"} if expected != "none" else {}),
            },
        )

    metadata = AuthorizationServerMetadata(
        issuer="https://as.example.com",
        authorization_endpoint="https://as.example.com/authorize",
        token_endpoint="https://as.example.com/token",
        registration_endpoint="https://as.example.com/register",
        token_endpoint_auth_methods_supported=methods,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        credentials = await register_client(
            client,
            metadata,
            redirect_uri="http://localhost:3000/api/v1/oauth/callback",
            client_name="Jhin",
            application_type="native",
        )
    assert credentials.token_endpoint_auth_method == expected
    assert requests[0]["token_endpoint_auth_method"] == expected


@pytest.mark.parametrize(
    "payload",
    [
        {"token_endpoint_auth_method": "private_key_jwt", "client_secret": "secret"},
        {"token_endpoint_auth_method": 123, "client_secret": "secret"},
        {"token_endpoint_auth_method": "client_secret_basic"},
        {"token_endpoint_auth_method": "client_secret_post", "client_secret": "x" * 4097},
        {},
    ],
)
async def test_unusable_issued_auth_contract_is_refused(payload):
    metadata = AuthorizationServerMetadata(
        issuer="https://as.example.com",
        authorization_endpoint="https://as.example.com/authorize",
        token_endpoint="https://as.example.com/token",
        registration_endpoint="https://as.example.com/register",
        token_endpoint_auth_methods_supported=("none", "client_secret_basic"),
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(201, json={"client_id": "local-client", **payload})
        )
    ) as client:
        with pytest.raises(RegistrationError):
            await register_client(
                client, metadata, redirect_uri="http://localhost:3000/callback", client_name="Jhin"
            )


async def test_unsupported_advertised_methods_fail_before_registration():
    metadata = AuthorizationServerMetadata(
        issuer="https://as.example.com",
        authorization_endpoint="https://as.example.com/authorize",
        token_endpoint="https://as.example.com/token",
        registration_endpoint="https://as.example.com/register",
        token_endpoint_auth_methods_supported=("private_key_jwt",),
    )

    def unexpected(_):
        pytest.fail("unsupported registration must not be attempted")

    async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected)) as client:
        with pytest.raises(RegistrationError):
            await register_client(
                client, metadata, redirect_uri="http://localhost:3000/callback", client_name="Jhin"
            )


async def test_omitted_response_auth_method_uses_basic_with_issued_secret():
    metadata = AuthorizationServerMetadata(
        issuer="https://as.example.com",
        authorization_endpoint="https://as.example.com/authorize",
        token_endpoint="https://as.example.com/token",
        registration_endpoint="https://as.example.com/register",
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                201, json={"client_id": "local-client", "client_secret": "issued-secret"}
            )
        )
    ) as client:
        credentials = await register_client(
            client, metadata, redirect_uri="http://localhost:3000/callback", client_name="Jhin"
        )
    assert credentials.token_endpoint_auth_method == "client_secret_basic"
    assert credentials.client_secret == "issued-secret"
