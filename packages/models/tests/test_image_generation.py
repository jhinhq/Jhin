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
