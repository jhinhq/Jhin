"""The fake provider must behave like a real OpenAI-compatible endpoint
through our own adapter — otherwise it proves nothing (plan 32.2)."""

import pytest

from jhin_models import ModelMessage, ModelProviderError, ModelRequest, build_model_client
from jhin_models.testing import FakeOpenAIServer
from jhin_models.testing.fake_openai import FAIL_MODEL, build_completion


async def test_adapter_roundtrip_against_fake_server() -> None:
    with FakeOpenAIServer() as server:
        client = build_model_client("openai_compatible", base_url=server.base_url)
        try:
            response = await client.generate(
                ModelRequest(
                    model="fake-mini",
                    messages=(ModelMessage(role="user", content="Say hello to Jhin"),),
                )
            )
        finally:
            await client.close()

    assert response.text.startswith("[fake-mini] Completed:")
    assert "Say hello to Jhin" in response.text
    assert response.usage.input_tokens > 0
    assert response.usage.output_tokens > 0
    assert response.finish_reason == "stop"


async def test_verify_lists_models() -> None:
    with FakeOpenAIServer() as server:
        client = build_model_client("openai_compatible", base_url=server.base_url)
        try:
            detail = await client.verify()
        finally:
            await client.close()
    assert "2 models" in detail


async def test_fail_model_surfaces_provider_error() -> None:
    with FakeOpenAIServer() as server:
        client = build_model_client("openai_compatible", base_url=server.base_url)
        try:
            with pytest.raises(ModelProviderError):
                await client.generate(
                    ModelRequest(
                        model=FAIL_MODEL,
                        messages=(ModelMessage(role="user", content="boom"),),
                    )
                )
        finally:
            await client.close()


def test_completion_is_deterministic() -> None:
    body = {"model": "fake-pro", "messages": [{"role": "user", "content": "same input"}]}
    assert build_completion(body) == build_completion(body)
