"""The shared provider HTTP primitive bounds and safely parses JSON responses."""

from collections.abc import AsyncIterator, Sequence

import httpx
import pytest

from jhin_connectors.http_client import (
    MAX_PROVIDER_RESPONSE_BYTES,
    ProviderHTTPError,
    send_bounded_json,
)


class TrackingStream(httpx.AsyncByteStream):
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


def _forbid_response_aread(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fail_aread(_response: httpx.Response) -> bytes:
        raise AssertionError("send_bounded_json must stream instead of calling Response.aread()")

    monkeypatch.setattr(httpx.Response, "aread", fail_aread)


async def test_redirect_response_is_rejected_without_following(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _forbid_response_aread(monkeypatch)
    stream = TrackingStream((b'{"redirect": true}',))
    requested_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_urls.append(str(request.url))
        return httpx.Response(
            302,
            headers={"location": "https://redirect.example/credential?token=secret"},
            stream=stream,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        request = client.build_request("GET", "https://provider.example/data")
        with pytest.raises(ProviderHTTPError, match="redirect"):
            await send_bounded_json(client, request)

    assert requested_urls == ["https://provider.example/data"]
    assert stream.yielded == 0
    assert stream.closed is True


async def test_content_length_over_512_kib_is_rejected_before_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _forbid_response_aread(monkeypatch)
    stream = TrackingStream((b"must not be read",))

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-length": str(MAX_PROVIDER_RESPONSE_BYTES + 1)},
            stream=stream,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        request = client.build_request("GET", "https://provider.example/data")
        with pytest.raises(ProviderHTTPError, match="too large"):
            await send_bounded_json(client, request)

    assert stream.yielded == 0
    assert stream.closed is True


async def test_chunked_response_stops_before_buffering_over_512_kib(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _forbid_response_aread(monkeypatch)
    stream = TrackingStream((b"x" * MAX_PROVIDER_RESPONSE_BYTES, b"offending-chunk"))

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        request = client.build_request("GET", "https://provider.example/data")
        with pytest.raises(ProviderHTTPError, match="too large"):
            await send_bounded_json(client, request)

    assert stream.yielded == 2
    assert stream.closed is True


async def test_exact_512_kib_json_response_is_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _forbid_response_aread(monkeypatch)
    encoded = b'"' + (b"a" * (MAX_PROVIDER_RESPONSE_BYTES - 2)) + b'"'
    assert len(encoded) == 524_288
    stream = TrackingStream((encoded[:300_000], encoded[300_000:]))

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-length": str(MAX_PROVIDER_RESPONSE_BYTES)},
            stream=stream,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        request = client.build_request("GET", "https://provider.example/data")
        payload = await send_bounded_json(client, request)

    assert payload == "a" * (MAX_PROVIDER_RESPONSE_BYTES - 2)
    assert stream.closed is True


@pytest.mark.parametrize(
    "document",
    [
        b'{"value":NaN}',
        b'{"value":Infinity}',
        b'{"value":1,"value":2}',
    ],
)
async def test_non_strict_provider_json_is_rejected(
    document: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _forbid_response_aread(monkeypatch)
    stream = TrackingStream((document,))

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        request = client.build_request("GET", "https://provider.example/data")
        with pytest.raises(ProviderHTTPError, match="invalid JSON"):
            await send_bounded_json(client, request)

    assert stream.closed is True


async def test_provider_error_is_credential_safe(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _forbid_response_aread(monkeypatch)
    bearer_token = "bearer-token-that-must-not-leak"
    url_password = "url-password-that-must-not-leak"
    stream = TrackingStream(
        ((f'{{"error":"Bearer {bearer_token}; password={url_password}"}}').encode(),)
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, stream=stream)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        request = client.build_request(
            "GET",
            "https://provider.example/data",
            headers={"authorization": f"Bearer {bearer_token}"},
        )
        with pytest.raises(ProviderHTTPError) as exc_info:
            await send_bounded_json(client, request)

        credential_url_request = client.build_request(
            "GET",
            f"https://url-user:{url_password}@provider.example/data",
        )
        with pytest.raises(ProviderHTTPError) as url_exc_info:
            await send_bounded_json(client, credential_url_request)

    captured = caplog.text
    for rendered in (str(exc_info.value), str(url_exc_info.value), captured):
        assert bearer_token not in rendered
        assert url_password not in rendered
    assert stream.yielded == 0
    assert stream.closed is True


async def test_expected_status_code_rejects_a_different_success_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _forbid_response_aread(monkeypatch)
    stream = TrackingStream((b'{"token":"bounded"}',))

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        request = client.build_request("POST", "https://provider.example/token")
        with pytest.raises(ProviderHTTPError, match="status 200") as exc_info:
            await send_bounded_json(client, request, expected_status_codes=(201,))

    assert exc_info.value.status_code == 200
    assert stream.yielded == 0
    assert stream.closed is True
