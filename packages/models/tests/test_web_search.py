"""Model-native web search: config gating, adapter wiring, citations."""

import json
from typing import Any

import httpx
import pytest

from jhin_models import (
    ModelMessage,
    ModelProviderError,
    ModelRequest,
    WebSearchConfig,
    web_search_unsupported_reason,
)
from jhin_models.providers.anthropic import WEB_SEARCH_TOOL_TYPE, AnthropicClient
from jhin_models.providers.openai import OpenAIClient
from jhin_models.providers.openai_compatible import OpenAICompatibleClient
from jhin_models.providers.openrouter import OpenRouterClient
from jhin_models.web_search import WebCitation, render_citations


def request_for(*, max_uses: int | None = None, enabled: bool = True) -> ModelRequest:
    return ModelRequest(
        model="test-model",
        messages=(ModelMessage(role="user", content="What happened today?"),),
        web_search=WebSearchConfig(enabled=enabled, max_uses=max_uses),
    )


def chat_response(annotations: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": "Fresh news."}
    if annotations is not None:
        message["annotations"] = annotations
    return {
        "id": "chatcmpl-1",
        "model": "test-model",
        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }


# --- config + validation -------------------------------------------------------


def test_config_parses_from_profile_config() -> None:
    config = WebSearchConfig.from_profile_config({"web_search": {"enabled": True, "max_uses": 3}})
    assert config.enabled is True
    assert config.max_uses == 3
    assert WebSearchConfig.from_profile_config(None).enabled is False
    assert WebSearchConfig.from_profile_config({"web_search": "nope"}).enabled is False
    # Out-of-range max_uses falls back to disabled rather than raising.
    assert WebSearchConfig.from_profile_config({"web_search": {"max_uses": 99}}).enabled is False


def test_unsupported_reasons_per_provider() -> None:
    assert web_search_unsupported_reason("anthropic", "claude-x") is None
    assert web_search_unsupported_reason("openrouter", "meta/llama") is None
    assert web_search_unsupported_reason("openai", "gpt-4o-mini-search-preview") is None
    assert web_search_unsupported_reason("openai", "gpt-5-search-api") is None
    openai_reason = web_search_unsupported_reason("openai", "gpt-4o-mini")
    assert openai_reason is not None and "search-preview" in openai_reason
    assert web_search_unsupported_reason("ollama", "llama3") is not None
    assert web_search_unsupported_reason("openai_compatible", "fake-mini") is not None


def test_render_citations_deduplicates_and_labels() -> None:
    block = render_citations(
        [
            WebCitation(url="https://a.example/x", title="A"),
            WebCitation(url="https://a.example/x", title="A again"),
            WebCitation(url="https://b.example/y"),
        ]
    )
    assert "Sources (provider web search):" in block
    assert block.count("https://a.example/x") == 1
    assert "- https://b.example/y" in block
    assert render_citations([]) == ""


# --- adapter payload wiring ----------------------------------------------------


async def test_openai_adapter_sends_web_search_options() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=chat_response())

    client = OpenAIClient(api_key="sk-test", transport=httpx.MockTransport(handler))
    await client.generate(request_for(max_uses=2))
    await client.close()
    assert seen["body"]["web_search_options"] == {}
    assert "plugins" not in seen["body"]


async def test_openrouter_adapter_sends_web_plugin() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=chat_response())

    client = OpenRouterClient(api_key="sk-or", transport=httpx.MockTransport(handler))
    await client.generate(request_for(max_uses=2))
    await client.close()
    assert seen["body"]["plugins"] == [{"id": "web", "max_results": 2}]


async def test_generic_adapter_rejects_web_search_before_any_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request may be sent for an unsupported flag")

    client = OpenAICompatibleClient(
        base_url="http://fake/v1", transport=httpx.MockTransport(handler)
    )
    with pytest.raises(ModelProviderError, match="web search is not supported"):
        await client.generate(request_for())
    await client.close()


async def test_disabled_config_adds_nothing_to_the_payload() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=chat_response())

    client = OpenAICompatibleClient(
        base_url="http://fake/v1", transport=httpx.MockTransport(handler)
    )
    await client.generate(request_for(enabled=False))
    await client.close()
    assert "web_search_options" not in seen["body"]
    assert "plugins" not in seen["body"]


async def test_anthropic_adapter_appends_the_server_tool() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "msg_1",
                "model": "claude-test",
                "content": [{"type": "text", "text": "Fresh news."}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )

    client = AnthropicClient(api_key="sk-ant", transport=httpx.MockTransport(handler))
    await client.generate(request_for(max_uses=4))
    await client.close()
    assert seen["body"]["tools"] == [
        {"type": WEB_SEARCH_TOOL_TYPE, "name": "web_search", "max_uses": 4}
    ]


# --- citations -----------------------------------------------------------------


async def test_openai_citations_become_a_visible_sources_block() -> None:
    annotations = [
        {
            "type": "url_citation",
            "url_citation": {"url": "https://news.example/a", "title": "News A"},
        },
        {"type": "other"},
        {"type": "url_citation", "url_citation": {"title": "no url"}},
    ]

    client = OpenAIClient(
        api_key="sk-test",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=chat_response(annotations))
        ),
    )
    response = await client.generate(request_for())
    await client.close()
    assert response.text.startswith("Fresh news.")
    assert "Sources (provider web search):" in response.text
    assert "News A — https://news.example/a" in response.text


async def test_anthropic_citations_become_a_visible_sources_block() -> None:
    body = {
        "id": "msg_1",
        "model": "claude-test",
        "content": [
            {"type": "server_tool_use", "id": "st_1", "name": "web_search", "input": {}},
            {"type": "web_search_tool_result", "tool_use_id": "st_1", "content": []},
            {
                "type": "text",
                "text": "Fresh news.",
                "citations": [
                    {
                        "type": "web_search_result_location",
                        "url": "https://news.example/b",
                        "title": "News B",
                        "cited_text": "…",
                    },
                    {"type": "char_location"},
                ],
            },
        ],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    client = AnthropicClient(
        api_key="sk-ant",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body)),
    )
    response = await client.generate(request_for())
    await client.close()
    assert response.text.startswith("Fresh news.")
    assert "News B — https://news.example/b" in response.text
