"""Balance/spend adapters, provider-supplied pricing, and out-of-credit
classification against an httpx MockTransport."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from jhin_models import (
    INSUFFICIENT_FUNDS,
    AccountStatusUnsupported,
    ModelMessage,
    ModelProviderError,
    ModelRequest,
    build_model_client,
)
from jhin_models.providers.anthropic import AnthropicClient
from jhin_models.providers.openai import OpenAIClient, sum_cost_buckets
from jhin_models.providers.openai_compatible import OpenAICompatibleClient
from jhin_models.providers.openrouter import OpenRouterClient, parse_credits
from jhin_models.testing.fake_openai import (
    FAKE_TOTAL_CREDITS,
    FAKE_TOTAL_USAGE,
    NO_CREDIT_MODEL,
    FakeOpenAIServer,
)


def _request(model: str = "fake-mini") -> ModelRequest:
    return ModelRequest(model=model, messages=(ModelMessage(role="user", content="hi"),))


# --- OpenRouter credits ---


async def test_openrouter_credits_become_remaining_micros() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"data": {"total_credits": 50, "total_usage": 12.5}})

    client = OpenRouterClient(api_key="k", transport=httpx.MockTransport(handler))
    status = await client.get_account_status()
    await client.close()
    assert seen["url"] == "https://openrouter.ai/api/v1/credits"
    assert status is not None
    assert status.remaining_micros == 37_500_000
    assert status.granted_micros == 50_000_000
    assert status.spent_month_micros is None
    assert status.source == "openrouter"


def test_parse_credits_rejects_malformed_payloads() -> None:
    with pytest.raises(ModelProviderError):
        parse_credits({"data": {"total_credits": "x"}})
    with pytest.raises(ModelProviderError):
        parse_credits({})


async def test_openrouter_credits_http_error_is_a_provider_error() -> None:
    client = OpenRouterClient(
        api_key="k",
        transport=httpx.MockTransport(lambda r: httpx.Response(401, json={"error": "bad"})),
    )
    with pytest.raises(ModelProviderError) as excinfo:
        await client.get_account_status()
    await client.close()
    assert excinfo.value.status_code == 401


# --- OpenAI admin costs ---


def _costs_page(values: list[float], *, has_more: bool = False, next_page: str | None = None):
    return {
        "object": "page",
        "data": [
            {"object": "bucket", "results": [{"amount": {"value": v, "currency": "usd"}}]}
            for v in values
        ],
        "has_more": has_more,
        "next_page": next_page,
    }


def test_sum_cost_buckets_tolerates_garbage() -> None:
    payload = _costs_page([1.25, 0.75])
    payload["data"].append({"object": "bucket", "results": [{"amount": "nope"}, 3]})
    payload["data"].append("junk")
    assert sum_cost_buckets(payload) == 2_000_000


async def test_openai_costs_sum_across_pages_with_month_start() -> None:
    calls: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url)
        assert request.headers["authorization"] == "Bearer admin-key"
        if request.url.params.get("page") == "p2":
            return httpx.Response(200, json=_costs_page([0.5]))
        return httpx.Response(200, json=_costs_page([1.25, 1.25], has_more=True, next_page="p2"))

    client = OpenAIClient(
        api_key="sk", admin_api_key="admin-key", transport=httpx.MockTransport(handler)
    )
    status = await client.get_account_status()
    await client.close()
    assert status is not None
    assert status.spent_month_micros == 3_000_000
    assert status.remaining_micros is None
    assert status.source == "openai_admin"
    assert status.period_start == datetime.now(UTC).date().replace(day=1)
    first = calls[0]
    assert first.path == "/v1/organization/costs"
    assert first.params["bucket_width"] == "1d"
    assert first.params["limit"] == "31"
    assert int(first.params["start_time"]) % 86_400 == 0
    assert calls[1].params["page"] == "p2"


async def test_openai_without_admin_key_is_unsupported_not_none() -> None:
    client = OpenAIClient(
        api_key="sk", transport=httpx.MockTransport(lambda r: httpx.Response(500))
    )
    assert client.has_admin_key is False
    with pytest.raises(AccountStatusUnsupported):
        await client.get_account_status()
    await client.close()


async def test_openai_admin_http_error_is_a_provider_error() -> None:
    client = OpenAIClient(
        api_key="sk",
        admin_api_key="admin",
        transport=httpx.MockTransport(
            lambda r: httpx.Response(403, json={"error": {"message": "admin scope required"}})
        ),
    )
    with pytest.raises(ModelProviderError) as excinfo:
        await client.get_account_status()
    await client.close()
    assert "admin scope required" in str(excinfo.value)
    assert excinfo.value.status_code == 403


# --- Unsupported providers return None cleanly ---


async def test_anthropic_ollama_and_generic_have_no_balance_api() -> None:
    transport = httpx.MockTransport(lambda r: httpx.Response(500))
    for client in (
        AnthropicClient(api_key="k", transport=transport),
        OpenAICompatibleClient(base_url="http://fake/v1", transport=transport),
        build_model_client("ollama", transport=transport),
    ):
        assert await client.get_account_status() is None
        await client.close()


# --- Out-of-credit classification ---


async def test_openai_insufficient_quota_429_is_insufficient_funds() -> None:
    body = {
        "error": {
            "message": "quota exceeded",
            "type": "insufficient_quota",
            "code": "insufficient_quota",
        }
    }
    client = OpenAIClient(
        api_key="sk", transport=httpx.MockTransport(lambda r: httpx.Response(429, json=body))
    )
    with pytest.raises(ModelProviderError) as excinfo:
        await client.generate(_request("gpt-5"))
    await client.close()
    error = excinfo.value
    assert error.error_code == INSUFFICIENT_FUNDS
    assert error.retryable is False
    assert str(error) == (
        "Your OpenAI account is out of credit. Add funds at "
        "https://platform.openai.com/settings/organization/billing, then retry."
    )


async def test_plain_429_stays_retryable_without_error_code() -> None:
    client = OpenAIClient(
        api_key="sk",
        transport=httpx.MockTransport(
            lambda r: httpx.Response(429, json={"error": {"code": "rate_limit_exceeded"}})
        ),
    )
    with pytest.raises(ModelProviderError) as excinfo:
        await client.generate(_request("gpt-5"))
    await client.close()
    assert excinfo.value.error_code is None
    assert excinfo.value.retryable is True


async def test_openrouter_402_is_insufficient_funds_on_generate_and_stream() -> None:
    transport = httpx.MockTransport(
        lambda r: httpx.Response(402, json={"error": {"message": "Insufficient credits"}})
    )
    client = OpenRouterClient(api_key="k", transport=transport)
    with pytest.raises(ModelProviderError) as excinfo:
        await client.generate(_request("openai/gpt-4o"))
    assert excinfo.value.error_code == INSUFFICIENT_FUNDS
    assert "OpenRouter" in str(excinfo.value)
    assert "https://openrouter.ai/settings/credits" in str(excinfo.value)
    with pytest.raises(ModelProviderError) as stream_info:
        async for _ in client.stream(_request("openai/gpt-4o")):
            pass
    await client.close()
    assert stream_info.value.error_code == INSUFFICIENT_FUNDS


# --- Provider-supplied pricing on the model list ---


async def test_openrouter_model_list_carries_live_prices() -> None:
    payload = {
        "data": [
            {
                "id": "openai/gpt-4o",
                "context_length": 128000,
                "pricing": {"prompt": "0.0000025", "completion": "0.00001"},
            },
            {"id": "free/model", "pricing": {"prompt": "0", "completion": "0"}},
            {"id": "dynamic/model", "pricing": {"prompt": "-1", "completion": "-1"}},
            {"id": "no-pricing/model"},
        ]
    }
    client = OpenRouterClient(
        api_key="k", transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload))
    )
    listings = {entry.id: entry for entry in await client.list_models_detailed()}
    await client.close()
    priced = listings["openai/gpt-4o"]
    assert priced.input_cost_micros_per_million == 2_500_000
    assert priced.output_cost_micros_per_million == 10_000_000
    assert priced.context_window == 128000
    assert priced.source == "provider"
    assert listings["free/model"].input_cost_micros_per_million == 0
    assert listings["free/model"].source == "provider"
    assert listings["dynamic/model"].source is None
    assert listings["no-pricing/model"].source is None


async def test_openai_model_list_uses_the_catalog() -> None:
    payload = {"data": [{"id": "gpt-4o-2024-08-06"}, {"id": "ft:custom-model"}]}
    client = OpenAIClient(
        api_key="sk", transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload))
    )
    listings = {entry.id: entry for entry in await client.list_models_detailed()}
    await client.close()
    assert listings["gpt-4o-2024-08-06"].source == "catalog"
    assert listings["gpt-4o-2024-08-06"].input_cost_micros_per_million == 2_500_000
    assert listings["gpt-4o-2024-08-06"].context_window == 128_000
    assert listings["ft:custom-model"].source is None


async def test_anthropic_model_list_uses_the_catalog() -> None:
    payload = {"data": [{"id": "claude-sonnet-4-20250514"}, {"id": "claude-mystery-9"}]}
    client = AnthropicClient(
        api_key="k", transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload))
    )
    listings = {entry.id: entry for entry in await client.list_models_detailed()}
    await client.close()
    assert listings["claude-sonnet-4-20250514"].source == "catalog"
    assert listings["claude-sonnet-4-20250514"].output_cost_micros_per_million == 15_000_000
    assert listings["claude-mystery-9"].source is None


# --- Fake provider end to end (through the instrumented factory) ---


async def test_fake_provider_exposes_billing_endpoints_and_pricing() -> None:
    with FakeOpenAIServer() as server:
        openrouter = build_model_client("openrouter", base_url=server.base_url, api_key="k")
        openai = build_model_client(
            "openai", base_url=server.base_url, api_key="k", admin_api_key="admin"
        )
        generic = build_model_client("openai_compatible", base_url=server.base_url)
        try:
            credits = await openrouter.get_account_status()
            costs = await openai.get_account_status()
            listings = await generic.list_models_detailed()
            with pytest.raises(ModelProviderError) as excinfo:
                await generic.generate(_request(NO_CREDIT_MODEL))
        finally:
            await openrouter.close()
            await openai.close()
            await generic.close()
    assert credits is not None
    assert credits.remaining_micros == int((FAKE_TOTAL_CREDITS - FAKE_TOTAL_USAGE) * 1_000_000)
    assert costs is not None and costs.spent_month_micros == 3_750_000
    by_id = {entry.id: entry for entry in listings}
    assert by_id["fake-mini"].source == "provider"
    assert by_id["fake-mini"].input_cost_micros_per_million == 150_000
    assert by_id["fake-mini"].output_cost_micros_per_million == 600_000
    assert by_id["fake-mini"].context_window == 128_000
    assert excinfo.value.error_code == INSUFFICIENT_FUNDS


async def test_base_list_models_detailed_defaults_to_bare_ids() -> None:
    from jhin_models.base import ModelClient, ModelResponse

    class Bare(ModelClient):
        async def generate(self, request: ModelRequest) -> ModelResponse:
            raise NotImplementedError

        def stream(self, request: ModelRequest):  # type: ignore[no-untyped-def]
            raise NotImplementedError

        async def verify(self) -> str:
            return "ok"

        async def list_models(self) -> list[str]:
            return ["a", "b"]

        async def close(self) -> None:
            pass

    listings = await Bare().list_models_detailed()
    assert [entry.id for entry in listings] == ["a", "b"]
    assert all(entry.source is None for entry in listings)
    assert json.loads(listings[0].model_dump_json())["input_cost_micros_per_million"] is None
