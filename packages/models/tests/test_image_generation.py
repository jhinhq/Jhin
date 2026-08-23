"""Optional image-generation capability: OpenAI-compatible adapter against the
fake server, unsupported providers, and profile config parsing."""

from __future__ import annotations

import pytest

from jhin_models import (
    ImageGenerationConfig,
    ImageGenerationUnsupported,
    ModelProviderError,
    as_image_generation_client,
    build_model_client,
)
from jhin_models.testing import FakeOpenAIServer, deterministic_png


async def test_openai_compatible_generates_deterministic_png() -> None:
    with FakeOpenAIServer() as server:
        client = build_model_client("openai_compatible", base_url=server.base_url)
        try:
            images = as_image_generation_client(client)
            first = await images.generate_image("a calm owl", model="fake-image")
            second = await images.generate_image("a calm owl", model="fake-image")
        finally:
            await client.close()
    assert first.content_type == "image/png"
    assert first.data == second.data == deterministic_png("a calm owl")
    assert first.model == "fake-image"


async def test_openai_compatible_surfaces_provider_failure() -> None:
    with FakeOpenAIServer() as server:
        client = build_model_client("openai_compatible", base_url=server.base_url)
        try:
            with pytest.raises(ModelProviderError) as excinfo:
                await as_image_generation_client(client).generate_image("x", model="always-fails")
        finally:
            await client.close()
    assert excinfo.value.status_code == 500
    assert excinfo.value.retryable is True


async def test_anthropic_is_unsupported() -> None:
    client = build_model_client("anthropic", api_key="k")
    try:
        with pytest.raises(ImageGenerationUnsupported):
            as_image_generation_client(client)
    finally:
        await client.close()


def test_profile_config_parsing() -> None:
    assert ImageGenerationConfig.from_profile_config(None).enabled is False
    assert ImageGenerationConfig.from_profile_config({"image_generation": "no"}).enabled is False
    config = ImageGenerationConfig.from_profile_config(
        {"image_generation": {"enabled": True, "model": "gpt-image-1", "cost_micros": 40_000}}
    )
    assert config.enabled and config.model == "gpt-image-1"
    assert config.size == "1024x1024"
    assert config.cost_micros == 40_000


@pytest.mark.parametrize(
    ("model", "quality", "expect_response_format", "expect_quality"),
    [
        ("dall-e-3", None, True, None),
        ("fake-image", "hd", True, "hd"),
        ("gpt-image-1-mini", "low", False, "low"),
        ("GPT-Image-1", None, False, None),
        ("chatgpt-image-latest", "medium", False, "medium"),
    ],
)
async def test_openai_compatible_image_payload_matches_model_family(
    model: str, quality: str | None, expect_response_format: bool, expect_quality: str | None
) -> None:
    """``gpt-image-*`` rejects ``response_format`` (it is always inline), so
    the adapter omits it there; ``quality`` rides along only when set."""
    import base64
    import json

    import httpx

    from jhin_models.providers.openai_compatible import OpenAICompatibleClient

    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        png = base64.b64encode(deterministic_png("x")).decode()
        return httpx.Response(200, json={"data": [{"b64_json": png}], "model": model})

    client = OpenAICompatibleClient(
        base_url="http://fake/v1", transport=httpx.MockTransport(handler)
    )
    try:
        generated = await client.generate_image("x", model=model, size="1024x1024", quality=quality)
    finally:
        await client.close()
    assert generated.data == deterministic_png("x")
    assert seen["model"] == model and seen["size"] == "1024x1024" and seen["n"] == 1
    assert ("response_format" in seen) is expect_response_format
    assert seen.get("quality") == expect_quality


def test_profile_config_quality_is_optional() -> None:
    config = ImageGenerationConfig.from_profile_config(
        {"image_generation": {"enabled": True, "model": "gpt-image-1-mini", "quality": "low"}}
    )
    assert config.quality == "low"
    assert (
        ImageGenerationConfig.from_profile_config({"image_generation": {"enabled": True}}).quality
        is None
    )
