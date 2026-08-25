"""Reasoning effort vs. function tools: detection, payload wiring, errors.

Regression cover for the real failure: OpenAI's chat completions refuse to
combine function tools with a reasoning model's default ``reasoning_effort``,
which made every tool-carrying Jhin step fail on ``gpt-5.6-terra``.
"""

import json
from typing import Any

import httpx
import pytest

from jhin_models import (
    MODEL_INCOMPATIBLE_REQUEST,
    ModelMessage,
    ModelProviderError,
    ModelRequest,
    ReasoningConfig,
    ToolSchema,
    is_reasoning_model,
    reasoning_unsupported_reason,
)
from jhin_models.providers.anthropic import AnthropicClient
from jhin_models.providers.ollama import OllamaClient
from jhin_models.providers.openai import OpenAIClient
from jhin_models.providers.openai_compatible import OpenAICompatibleClient
from jhin_models.providers.openrouter import OpenRouterClient

TOOLS = (
    ToolSchema(
        name="organization.delegate_task",
        description="Delegate work",
        parameters={"type": "object", "properties": {}},
    ),
)

# Verbatim from the live 400 that broke the user's chat.
REAL_400_BODY = json.dumps(
    {
        "error": {
            "message": (
                "Function tools with reasoning_effort are not supported for "
                "gpt-5.6-terra in /v1/chat/completions. To use function tools, "
                "use /v1/responses or set reasoning_effort to 'none'."
            ),
            "type": "invalid_request_error",
            "param": "tools",
            "code": None,
        }
    }
)
BAD_EFFORT_400_BODY = json.dumps(
    {
        "error": {
            "message": (
                "Invalid value: 'minimal'. Supported values are: 'none', 'low', "
                "'medium', and 'high'."
            ),
            "type": "invalid_request_error",
            "param": "reasoning_effort",
            "code": "invalid_value",
        }
    }
)


def request_for(
    *,
    model: str = "gpt-5.6-terra",
    tools: tuple[ToolSchema, ...] = TOOLS,
    reasoning: ReasoningConfig | None = None,
) -> ModelRequest:
    return ModelRequest(
        model=model,
        messages=(ModelMessage(role="user", content="hi"),),
        tools=tools,
        reasoning=reasoning,
    )


def chat_response(model: str = "gpt-5.6-terra") -> dict[str, Any]:
    return {
        "id": "chatcmpl-1",
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "hello"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }


def recording_client(
    seen: dict[str, Any], *, status_code: int = 200, body: str | None = None
) -> OpenAIClient:
    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        if body is not None:
            return httpx.Response(status_code, content=body, headers={"content-type": "text/plain"})
        return httpx.Response(status_code, json=chat_response())

    return OpenAIClient(api_key="sk-test", transport=httpx.MockTransport(handler))


# --- model-name matcher --------------------------------------------------------


@pytest.mark.parametrize(
    "model",
    [
        "gpt-5.6-terra",
        "gpt-5",
        "gpt-5-mini",
        "gpt-5-nano",
        "gpt-5-2025-08-07",
        "gpt-5.1-codex",
        "GPT-5-Mini",
        "o1",
        "o1-mini",
        "o1-preview",
        "o3",
        "o3-mini",
        "o4-mini",
        "openai/gpt-5-mini",
        "openai/o3-mini:free",
    ],
)
def test_reasoning_models_are_detected(model: str) -> None:
    assert is_reasoning_model(model) is True


@pytest.mark.parametrize(
    "model",
    [
        "gpt-4o-mini",
        "gpt-4o",
        "gpt-4.1",
        "gpt-4.1-mini",
        "gpt-3.5-turbo",
        "gpt-5-chat-latest",
        "chatgpt-4o-latest",
        "claude-sonnet-4-5",
        "llama3",
        "fake-mini",
        "",
    ],
)
def test_non_reasoning_models_are_not_detected(model: str) -> None:
    assert is_reasoning_model(model) is False


# --- config parsing + save-time validation -------------------------------------


def test_config_parses_from_profile_config() -> None:
    config = ReasoningConfig.from_profile_config({"reasoning": {"effort": "low"}})
    assert config.effort == "low"
    assert config.is_set is True
    assert ReasoningConfig.from_profile_config(None).effort is None
    assert ReasoningConfig.from_profile_config({"reasoning": "nope"}).effort is None
    # Unknown efforts fall back to "no opinion" rather than raising here;
    # the API layer is what rejects them at save time.
    assert ReasoningConfig.from_profile_config({"reasoning": {"effort": "minimal"}}).effort is None
    assert ReasoningConfig.from_profile_config({"reasoning": {"effort": None}}).is_set is False
    assert (
        ReasoningConfig.from_profile_config({"reasoning": {"supports_reasoning": True}}).is_set
        is True
    )


def test_unsupported_reasons_per_provider_and_model() -> None:
    assert reasoning_unsupported_reason("openai", "gpt-5.6-terra") is None
    assert reasoning_unsupported_reason("openai_compatible", "o3-mini") is None
    assert reasoning_unsupported_reason("openrouter", "openai/gpt-5-mini") is None
    # Not a reasoning model → the provider would reject the parameter.
    not_reasoning = reasoning_unsupported_reason("openai", "gpt-4o-mini")
    assert not_reasoning is not None and "not a reasoning model" in not_reasoning
    # …unless the profile declares it one.
    assert reasoning_unsupported_reason("openai", "mystery-r1", supports_reasoning=True) is None
    for provider in ("anthropic", "ollama"):
        reason = reasoning_unsupported_reason(provider, "gpt-5")
        assert reason is not None and "does not accept a reasoning effort" in reason


# --- automatic compatibility ---------------------------------------------------


async def test_reasoning_model_with_tools_pins_effort_to_none() -> None:
    seen: dict[str, Any] = {}
    client = recording_client(seen)
    await client.generate(request_for())
    await client.close()
    assert seen["body"]["reasoning_effort"] == "none"
    assert seen["body"]["tools"]


async def test_non_reasoning_model_with_tools_sends_no_reasoning_effort() -> None:
    seen: dict[str, Any] = {}
    client = recording_client(seen)
    await client.generate(request_for(model="gpt-4o-mini"))
    await client.close()
    assert "reasoning_effort" not in seen["body"]


async def test_reasoning_model_without_tools_is_left_alone() -> None:
    """No tools, no conflict — OpenAI's own default effort is the better
    answer, so nothing is forced."""
    seen: dict[str, Any] = {}
    client = recording_client(seen)
    await client.generate(request_for(tools=()))
    await client.close()
    assert "reasoning_effort" not in seen["body"]


async def test_supports_reasoning_flag_forces_the_automatic_fix() -> None:
    seen: dict[str, Any] = {}
    client = recording_client(seen)
    await client.generate(
        request_for(model="mystery-r1", reasoning=ReasoningConfig(supports_reasoning=True))
    )
    await client.close()
    assert seen["body"]["reasoning_effort"] == "none"


async def test_generic_openai_compatible_adapter_gets_the_same_fix() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=chat_response())

    client = OpenAICompatibleClient(
        base_url="http://fake/v1", transport=httpx.MockTransport(handler)
    )
    await client.generate(request_for())
    await client.close()
    assert seen["body"]["reasoning_effort"] == "none"


async def test_streaming_gets_the_same_treatment_as_generate() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            content=b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\ndata: [DONE]\n\n',
            headers={"content-type": "text/event-stream"},
        )

    client = OpenAIClient(api_key="sk-test", transport=httpx.MockTransport(handler))
    chunks = [chunk async for chunk in client.stream(request_for())]
    await client.close()
    assert chunks == ["hi"]
    assert seen["body"]["reasoning_effort"] == "none"


# --- explicit profile override -------------------------------------------------


async def test_explicit_effort_overrides_the_automatic_value() -> None:
    """A tools-free agent may deliberately want real reasoning."""
    seen: dict[str, Any] = {}
    client = recording_client(seen)
    await client.generate(request_for(tools=(), reasoning=ReasoningConfig(effort="high")))
    await client.close()
    assert seen["body"]["reasoning_effort"] == "high"


async def test_explicit_none_is_sent_even_without_tools() -> None:
    seen: dict[str, Any] = {}
    client = recording_client(seen)
    await client.generate(request_for(tools=(), reasoning=ReasoningConfig(effort="none")))
    await client.close()
    assert seen["body"]["reasoning_effort"] == "none"


async def test_explicit_effort_with_tools_fails_before_the_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("a request Jhin knows is invalid must never be sent")

    client = OpenAIClient(api_key="sk-test", transport=httpx.MockTransport(handler))
    with pytest.raises(ModelProviderError) as excinfo:
        await client.generate(request_for(reasoning=ReasoningConfig(effort="medium")))
    await client.close()
    assert excinfo.value.error_code == MODEL_INCOMPATIBLE_REQUEST
    assert excinfo.value.retryable is False
    message = str(excinfo.value)
    assert "gpt-5.6-terra" in message
    assert "medium" in message
    assert "config_json.reasoning.effort" in message


async def test_explicit_effort_on_a_non_reasoning_model_is_passed_through() -> None:
    """No conflict to pre-empt: the provider is the authority there."""
    seen: dict[str, Any] = {}
    client = recording_client(seen)
    await client.generate(request_for(model="gpt-4o-mini", reasoning=ReasoningConfig(effort="low")))
    await client.close()
    assert seen["body"]["reasoning_effort"] == "low"


# --- per-provider translation --------------------------------------------------


async def test_openrouter_uses_its_native_reasoning_block_and_no_auto_fix() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=chat_response())

    client = OpenRouterClient(api_key="sk-or", transport=httpx.MockTransport(handler))
    await client.generate(request_for(model="openai/gpt-5-mini"))
    assert "reasoning" not in seen["body"]
    assert "reasoning_effort" not in seen["body"]

    await client.generate(
        request_for(model="openai/gpt-5-mini", reasoning=ReasoningConfig(effort="none"))
    )
    assert seen["body"]["reasoning"] == {"enabled": False}

    await client.generate(
        request_for(model="openai/gpt-5-mini", tools=(), reasoning=ReasoningConfig(effort="high"))
    )
    await client.close()
    assert seen["body"]["reasoning"] == {"effort": "high"}


async def test_ollama_never_sends_an_effort_and_rejects_an_explicit_one() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=chat_response("qwen3"))

    client = OllamaClient(transport=httpx.MockTransport(handler))
    await client.generate(request_for(model="qwen3"))
    assert "reasoning_effort" not in seen["body"]

    with pytest.raises(ModelProviderError) as excinfo:
        await client.generate(request_for(model="qwen3", reasoning=ReasoningConfig(effort="low")))
    await client.close()
    assert excinfo.value.error_code == MODEL_INCOMPATIBLE_REQUEST


async def test_anthropic_rejects_a_stale_reasoning_block() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request may be sent for an unsupported setting")

    client = AnthropicClient(api_key="sk-ant", transport=httpx.MockTransport(handler))
    with pytest.raises(ModelProviderError) as excinfo:
        await client.generate(
            request_for(model="claude-sonnet-4-5", reasoning=ReasoningConfig(effort="low"))
        )
    await client.close()
    assert excinfo.value.error_code == MODEL_INCOMPATIBLE_REQUEST


# --- error classification ------------------------------------------------------


async def test_the_real_world_400_becomes_a_friendly_incompatible_error() -> None:
    """Regression: the exact body OpenAI returned for the user's failed run."""
    seen: dict[str, Any] = {}
    client = recording_client(seen, status_code=400, body=REAL_400_BODY)
    with pytest.raises(ModelProviderError) as excinfo:
        await client.generate(request_for())
    await client.close()
    error = excinfo.value
    assert error.error_code == MODEL_INCOMPATIBLE_REQUEST
    assert error.status_code == 400
    assert error.retryable is False
    message = str(error)
    assert message.startswith("OpenAI rejected this request")
    assert 'reasoning.effort` to "none"' in message
    # The provider's own words are kept as supporting detail, not as the lead.
    assert "Function tools with reasoning_effort" in message


async def test_invalid_effort_value_400_is_classified_by_param() -> None:
    seen: dict[str, Any] = {}
    client = recording_client(seen, status_code=400, body=BAD_EFFORT_400_BODY)
    with pytest.raises(ModelProviderError) as excinfo:
        await client.generate(request_for(model="gpt-4o-mini"))
    await client.close()
    assert excinfo.value.error_code == MODEL_INCOMPATIBLE_REQUEST


async def test_unrelated_400_keeps_the_generic_classification() -> None:
    seen: dict[str, Any] = {}
    body = json.dumps({"error": {"message": "Unknown model", "type": "invalid_request_error"}})
    client = recording_client(seen, status_code=400, body=body)
    with pytest.raises(ModelProviderError) as excinfo:
        await client.generate(request_for(model="gpt-4o-mini"))
    await client.close()
    assert excinfo.value.error_code is None
    assert "Unknown model" in str(excinfo.value)


async def test_streaming_400_is_classified_too() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, content=REAL_400_BODY, headers={"content-type": "text/plain"})

    client = OpenAIClient(api_key="sk-test", transport=httpx.MockTransport(handler))
    with pytest.raises(ModelProviderError) as excinfo:
        [chunk async for chunk in client.stream(request_for())]
    await client.close()
    assert excinfo.value.error_code == MODEL_INCOMPATIBLE_REQUEST
