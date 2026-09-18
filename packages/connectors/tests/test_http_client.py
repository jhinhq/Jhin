"""The shared provider HTTP primitive bounds and safely parses JSON responses."""

from collections.abc import AsyncIterator, Sequence

import httpx
import pytest

from jhin_connectors.http_client import (
    MAX_PROVIDER_MESSAGE_CHARS,
    MAX_PROVIDER_RESPONSE_BYTES,
    ProviderHTTPError,
    bounded_provider_message,
    send_bounded_json,
)
from jhin_secrets.redaction import SecretRedactor
from jhin_tools.sanitize import sanitize_payload


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
    # The failed body *is* read now, bounded, so that a provider's own reason
    # ("Resource not accessible by integration") survives the boundary. The
    # exception's message is unchanged, which is what everything that renders
    # or logs one uses; the body travels on its own channel and is redacted
    # where it is stored.
    assert stream.yielded == 1
    assert stream.closed is True


async def test_a_provider_error_body_is_bounded_flattened_and_redactable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What survives of a failed response, and what does not.

    Read to a cap, reduced to the provider's sentence, stripped of anything a
    terminal would act on — and, where it is stored, run through the process
    redactor that already knows every credential this run decrypted.
    """
    _forbid_response_aread(monkeypatch)
    token = "ghs_installation_token_that_must_not_leak"
    body = (
        '{"message":"Resource not\\u001b[31m accessible by integration '
        f'(token {token})","documentation_url":"https://docs.example"}}'
    ).encode()
    stream = TrackingStream((body,))

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, stream=stream)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        request = client.build_request("POST", "https://provider.example/pulls")
        with pytest.raises(ProviderHTTPError) as exc_info:
            await send_bounded_json(client, request)

    message = exc_info.value.provider_message
    assert exc_info.value.status_code == 403
    # The sentence, and only the sentence: the documentation URL and every
    # other key the body carried are not what a failure is being asked for.
    # The escape that would have started a terminal sequence is gone; what is
    # left of it is ordinary text.
    assert message == f"Resource not [31m accessible by integration (token {token})"
    assert "\x1b" not in message
    assert stream.closed is True

    redactor = SecretRedactor()
    redactor.register(token)
    assert token not in str(sanitize_payload({"detail": message}, redactor=redactor))


async def test_a_json_body_with_no_sentence_hands_over_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A JSON error document is read for its sentence or not at all.

    The fallback to raw text used to run whenever no recognised key matched,
    not only when the body was unparseable — so a provider that echoed the
    request back handed over the whole echo, four hundred characters of it,
    ``secret`` field included. ``_readable_message``'s promise that it does not
    search the whole document was true of that function and untrue of the one
    that called it.
    """
    _forbid_response_aread(monkeypatch)
    body = b'{"echo":{"secret":"echoed-canary","query":"select 1"},"status":"rejected"}'
    stream = TrackingStream((body,))

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, stream=stream)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        request = client.build_request("POST", "https://provider.example/query")
        with pytest.raises(ProviderHTTPError) as exc_info:
            await send_bounded_json(client, request)

    assert exc_info.value.provider_message == ""
    assert "echoed-canary" not in str(exc_info.value)

    # A body that is not JSON at all is still worth one line: that is where a
    # proxy puts its error page, and the page is what the fallback is for.
    assert bounded_provider_message(b"<html><body>502 Bad Gateway</body></html>") == (
        "<html><body>502 Bad Gateway</body></html>"
    )


async def test_a_huge_provider_error_body_is_cut_off_rather_than_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A body too big to be an error message is not read like one.

    The read stops at its cap, so the JSON never parses and the leading text
    stands in — bounded to the same few hundred characters. The point is the
    bound, not the recovery: nothing about a failure justifies a second
    transfer.
    """
    _forbid_response_aread(monkeypatch)
    body = ('{"message":"Bad credentials","padding":"' + "x" * 200_000 + '"}').encode()
    stream = TrackingStream(
        tuple(body[index : index + 2048] for index in range(0, len(body), 2048))
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, stream=stream)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        request = client.build_request("GET", "https://provider.example/data")
        with pytest.raises(ProviderHTTPError) as exc_info:
            await send_bounded_json(client, request)

    assert len(exc_info.value.provider_message) <= MAX_PROVIDER_MESSAGE_CHARS
    assert "Bad credentials" in exc_info.value.provider_message
    assert stream.yielded <= 3
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
