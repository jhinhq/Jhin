"""Outbound connector clients enforce endpoint policy at the last safe boundary."""

from collections.abc import AsyncIterator, Callable, Sequence
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from jhin_connectors.github.auth import mint_installation_token
from jhin_connectors.github.client import GitHubApiError, github_request
from jhin_connectors.github.connector import GitHubConnector
from jhin_connectors.github.schemas import RepositoryReadInput
from jhin_connectors.http_client import MAX_PROVIDER_RESPONSE_BYTES
from jhin_connectors.linear.client import LinearApiError, linear_graphql
from jhin_connectors.linear.connector import LinearConnector
from jhin_connectors.linear.schemas import IssueReadInput
from jhin_db.models import Workspace
from jhin_tools.builtin import ToolExecutionContext


class _TrackingStream(httpx.AsyncByteStream):
    def __init__(self, chunks: Sequence[bytes]) -> None:
        self.chunks = chunks
        self.yielded = 0
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            self.yielded += 1
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


class _CloseFailTransport(httpx.AsyncBaseTransport):
    def __init__(self, response_status: int, response_json: dict[str, Any], secret: str) -> None:
        self.response_status = response_status
        self.response_json = response_json
        self.secret = secret

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(self.response_status, json=self.response_json, request=request)

    async def aclose(self) -> None:
        raise RuntimeError(f"transport close echoed {self.secret}")


def _mock_client_transport(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    """Keep client construction real while replacing only external I/O."""
    real_async_client = httpx.AsyncClient

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        return real_async_client(
            *args,
            **{**kwargs, "transport": httpx.MockTransport(handler)},
        )

    monkeypatch.setattr(httpx, "AsyncClient", factory)


def _mock_client_with_transport(
    monkeypatch: pytest.MonkeyPatch,
    transport: httpx.AsyncBaseTransport,
) -> None:
    real_async_client = httpx.AsyncClient

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        return real_async_client(*args, **{**kwargs, "transport": transport})

    monkeypatch.setattr(httpx, "AsyncClient", factory)


def _forbid_response_aread(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fail_aread(_response: httpx.Response) -> bytes:
        raise AssertionError("connector clients must use bounded streaming")

    monkeypatch.setattr(httpx.Response, "aread", fail_aread)


async def test_github_pat_rejects_unallowlisted_origin_before_sending_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "github-token-that-must-not-be-sent"
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"login": "attacker"})

    _mock_client_transport(monkeypatch, handler)

    with pytest.raises(GitHubApiError) as exc_info:
        await github_request("GET", "http://127.0.0.1:9000", "/user", token)

    assert requests == []
    assert token not in str(exc_info.value)


async def test_github_app_mint_rejects_unallowlisted_origin_before_sending_jwt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_key = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            201,
            json={"token": "attacker-token", "expires_at": "2099-01-01T00:00:00Z"},
        )

    _mock_client_transport(monkeypatch, handler)

    with pytest.raises(GitHubApiError) as exc_info:
        await mint_installation_token(
            "http://127.0.0.1:9000",
            {
                "app_id": "12345",
                "private_key": private_key,
                "installation_id": "77",
            },
        )

    assert requests == []
    assert private_key not in str(exc_info.value)


async def test_linear_rejects_unallowlisted_origin_before_sending_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api_key = "linear-key-that-must-not-be-sent"
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"data": {}})

    _mock_client_transport(monkeypatch, handler)

    with pytest.raises(LinearApiError) as exc_info:
        await linear_graphql(
            "http://127.0.0.1:9000",
            api_key,
            "query { viewer { id } }",
        )

    assert requests == []
    assert api_key not in str(exc_info.value)


async def test_github_client_bounds_streamed_provider_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _forbid_response_aread(monkeypatch)
    stream = _TrackingStream((b"x" * MAX_PROVIDER_RESPONSE_BYTES, b"offending"))

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream)

    _mock_client_transport(monkeypatch, handler)

    with pytest.raises(GitHubApiError):
        await github_request("GET", "https://api.github.com", "/user", "safe-test-token")

    assert stream.yielded == 2
    assert stream.closed is True


async def test_github_app_mint_bounds_streamed_provider_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _forbid_response_aread(monkeypatch)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_key = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    stream = _TrackingStream((b"x" * MAX_PROVIDER_RESPONSE_BYTES, b"offending"))

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, stream=stream)

    _mock_client_transport(monkeypatch, handler)

    with pytest.raises(GitHubApiError):
        await mint_installation_token(
            "https://api.github.com",
            {
                "app_id": "12345",
                "private_key": private_key,
                "installation_id": "77",
            },
        )

    assert stream.yielded == 2
    assert stream.closed is True


async def test_github_app_mint_requires_created_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_key = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"token": "unexpected-token", "expires_at": "2099-01-01T00:00:00Z"},
        )

    _mock_client_transport(monkeypatch, handler)

    with pytest.raises(GitHubApiError):
        await mint_installation_token(
            "https://api.github.com",
            {
                "app_id": "12345",
                "private_key": private_key,
                "installation_id": "77",
            },
        )


async def test_linear_client_bounds_streamed_provider_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _forbid_response_aread(monkeypatch)
    stream = _TrackingStream((b"x" * MAX_PROVIDER_RESPONSE_BYTES, b"offending"))

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream)

    _mock_client_transport(monkeypatch, handler)

    with pytest.raises(LinearApiError):
        await linear_graphql(
            "https://api.linear.app",
            "safe-test-key",
            "query { viewer { id } }",
        )

    assert stream.yielded == 2
    assert stream.closed is True


async def test_linear_graphql_error_cannot_echo_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api_key = "linear-key-echoed-by-provider"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"errors": [{"message": f"invalid credential: {api_key}"}]},
        )

    _mock_client_transport(monkeypatch, handler)

    with pytest.raises(LinearApiError) as exc_info:
        await linear_graphql(
            "https://api.linear.app",
            api_key,
            "query { viewer { id } }",
        )

    assert api_key not in str(exc_info.value)


async def test_github_client_close_error_is_credential_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "github-token-echoed-by-close"
    _mock_client_with_transport(
        monkeypatch,
        _CloseFailTransport(200, {"login": "octocat"}, token),
    )

    with pytest.raises(GitHubApiError) as exc_info:
        await github_request("GET", "https://api.github.com", "/user", token)

    assert token not in str(exc_info.value)


async def test_github_provider_error_does_not_echo_hostile_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hostile_marker = "path-secret-marker%0Aforged-log-entry"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "denied"})

    _mock_client_transport(monkeypatch, handler)

    with pytest.raises(GitHubApiError) as exc_info:
        await github_request(
            "GET",
            "https://api.github.com",
            f"/repos/octo/{hostile_marker}",
            "safe-test-token",
        )

    assert exc_info.value.status_code == 401
    assert str(exc_info.value) == "GitHub API request failed with status 401"
    assert hostile_marker not in str(exc_info.value)


async def test_github_app_client_close_error_is_credential_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    close_secret = "github-app-close-secret"
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_key = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    _mock_client_with_transport(
        monkeypatch,
        _CloseFailTransport(
            201,
            {"token": "installation-token", "expires_at": "2099-01-01T00:00:00Z"},
            close_secret,
        ),
    )

    with pytest.raises(GitHubApiError) as exc_info:
        await mint_installation_token(
            "https://api.github.com",
            {
                "app_id": "12345",
                "private_key": private_key,
                "installation_id": "77",
            },
        )

    assert close_secret not in str(exc_info.value)


async def test_linear_client_close_error_is_credential_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api_key = "linear-key-echoed-by-close"
    _mock_client_with_transport(
        monkeypatch,
        _CloseFailTransport(200, {"data": {}}, api_key),
    )

    with pytest.raises(LinearApiError) as exc_info:
        await linear_graphql(
            "https://api.linear.app",
            api_key,
            "query { viewer { id } }",
        )

    assert api_key not in str(exc_info.value)


async def test_legacy_github_connection_cannot_bypass_endpoint_policy(
    context: ToolExecutionContext,
    workspace: Workspace,
    make_connection: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "legacy-github-token-that-must-not-be-sent"
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "full_name": "octo/alpha",
                "description": "",
                "default_branch": "main",
                "private": False,
                "html_url": "https://example.test/octo/alpha",
                "open_issues_count": 0,
                "forks_count": 0,
                "stargazers_count": 0,
            },
        )

    _mock_client_transport(monkeypatch, handler)
    connection = await make_connection(
        workspace,
        credentials={"token": token},
        config={"base_url": "http://127.0.0.1:9000"},
    )
    executors = {definition.name: executor for definition, executor in GitHubConnector().tools()}

    with pytest.raises(GitHubApiError):
        await executors["github.repository.read"](
            context,
            RepositoryReadInput(
                connection_id=str(connection.id),
                repository="octo/alpha",
            ),
        )

    assert requests == []


async def test_legacy_linear_connection_cannot_bypass_endpoint_policy(
    context: ToolExecutionContext,
    workspace: Workspace,
    make_connection: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api_key = "legacy-linear-key-that-must-not-be-sent"
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "data": {
                    "issue": {
                        "id": "issue-1",
                        "identifier": "ENG-1",
                        "title": "Should never be read",
                        "description": "",
                        "priority": 0,
                        "url": "https://linear.app/issue/ENG-1",
                        "state": {"id": "state-1", "name": "Todo", "type": "started"},
                        "team": {"id": "team-1", "key": "ENG", "name": "Engineering"},
                        "assignee": None,
                        "labels": {"nodes": []},
                    }
                }
            },
        )

    _mock_client_transport(monkeypatch, handler)
    connection = await make_connection(
        workspace,
        connector_type="linear",
        auth_type="api_key",
        credentials={"api_key": api_key},
        config={"base_url": "http://127.0.0.1:9000"},
    )
    executors = {definition.name: executor for definition, executor in LinearConnector().tools()}

    with pytest.raises(LinearApiError):
        await executors["linear.issue.read"](
            context,
            IssueReadInput(connection_id=str(connection.id), issue="ENG-1"),
        )

    assert requests == []


def test_github_api_error_classifies_side_effects() -> None:
    """Reads and definitively rejected writes are ordinary failures; only a
    transport failure or 5xx on a write leaves the outcome unknown."""
    read_404 = GitHubApiError("missing", status_code=404, method="GET")
    assert read_404.code == "github_http_404"
    assert read_404.side_effect_possible is False
    write_422 = GitHubApiError("rejected", status_code=422, method="POST")
    assert write_422.side_effect_possible is False
    write_502 = GitHubApiError("upstream", status_code=502, method="POST")
    assert write_502.side_effect_possible is True
    write_network = GitHubApiError("network", method="POST")
    assert write_network.code == "github_request_failed"
    assert write_network.side_effect_possible is True


def test_linear_api_error_classifies_side_effects() -> None:
    assert LinearApiError("x", status_code=404).side_effect_possible is False
    assert (
        LinearApiError(
            "x", status_code=200, mutation=True, code="linear_graphql_error"
        ).side_effect_possible
        is False
    )
    assert LinearApiError("x", mutation=True).side_effect_possible is True
    assert LinearApiError("x", status_code=503, mutation=True).side_effect_possible is True
