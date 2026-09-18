"""Provider boundaries: safe URLs, real request serialization, and visible credits."""

import httpx
import pytest

from jhin_connectors.unsplash.client import UnsplashError, photo_metadata, request


def sample():
    return {
        "id": "photo-1",
        "width": 1200,
        "height": 800,
        "alt_description": "A notebook on a desk",
        "urls": {"regular": "https://images.unsplash.com/photo-1?ixid=test&w=1200"},
        "links": {
            "html": "https://unsplash.com/photos/photo-1",
            "download_location": "https://api.unsplash.com/photos/photo-1/download?ixid=test",
        },
        "user": {"name": "A Photographer", "links": {"html": "https://unsplash.com/@author"}},
    }


def test_photo_metadata_preserves_hotlink_and_attribution():
    result = photo_metadata(sample())
    assert result["image_url"] == "https://images.unsplash.com/photo-1?ixid=test&w=1200"
    assert "A Photographer" in result["attribution_html"]
    assert "utm_source=jhin" in result["photographer_url"]
    assert result["width"] == 1200


@pytest.mark.parametrize("field", ["image", "download", "profile"])
def test_provider_supplied_arbitrary_hosts_are_rejected(field):
    photo = sample()
    if field == "image":
        photo["urls"]["regular"] = "https://attacker.example/steal"
    elif field == "download":
        photo["links"]["download_location"] = "https://attacker.example/steal"
    else:
        photo["user"]["links"]["html"] = "https://attacker.example/steal"
    with pytest.raises(UnsplashError):
        photo_metadata(photo)


async def test_request_uses_header_and_never_follows_redirect():
    received = []

    def handler(req):
        received.append(req)
        return httpx.Response(302, headers={"location": "https://attacker.example"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(UnsplashError, match="redirect"):
            await request("secret-test", "/search/photos", {"query": "desk"}, client=client)
    assert len(received) == 1
    assert received[0].headers["Authorization"] == "Client-ID secret-test"
    assert "secret-test" not in str(received[0].url)


async def test_rate_limit_is_sanitized_and_not_retried():
    count = 0

    def handler(req):
        nonlocal count
        count += 1
        return httpx.Response(429, text="secret-test", headers={"Retry-After": "60"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(UnsplashError, match="rate limit") as error:
            await request("secret-test", "/search/photos", {"query": "desk"}, client=client)
    assert count == 1
    assert "secret-test" not in str(error.value)
    assert error.value.retry_after == "60"


async def test_request_refuses_non_typed_path_before_network():
    with pytest.raises(UnsplashError):
        await request("secret-test", "/../users", {})
