"""Static price catalog lookup/normalisation and OpenRouter price conversion."""

from __future__ import annotations

import pytest

from jhin_models.pricing import (
    CATALOG_UPDATED,
    lookup_price,
    normalize_model_id,
    per_token_usd_to_micros_per_million,
    usd_to_micros,
)


def test_catalog_marker_is_a_year_month() -> None:
    assert CATALOG_UPDATED == "2026-01"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("gpt-4o-2024-08-06", "gpt-4o"),
        ("openai/gpt-4o-mini", "gpt-4o-mini"),
        ("GPT-5-MINI", "gpt-5-mini"),
        ("gpt-5-chat-latest", "gpt-5-chat"),
        ("claude-sonnet-4-20250514", "claude-sonnet-4"),
        ("anthropic/claude-3-5-haiku-20241022", "claude-3-5-haiku"),
        ("claude-opus-4-1@20250805", "claude-opus-4-1"),
        ("gpt-4o-mini-realtime-preview-2024-12-17", "gpt-4o-mini-realtime"),
    ],
)
def test_normalize_strips_vendor_dates_and_aliases(raw: str, expected: str) -> None:
    assert normalize_model_id(raw) == expected


def test_openai_exact_and_snapshot_lookups() -> None:
    base = lookup_price("openai", "gpt-4o")
    assert base is not None
    assert base.input_cost_micros_per_million == 2_500_000
    assert base.output_cost_micros_per_million == 10_000_000
    assert base.context_window == 128_000
    assert lookup_price("openai", "gpt-4o-2024-08-06") == base
    assert lookup_price("openai", "openai/gpt-4o") == base
    mini = lookup_price("openai", "gpt-4o-mini-2024-07-18")
    assert mini is not None and mini.input_cost_micros_per_million == 150_000


def test_prefix_fallback_but_not_across_version_numbers() -> None:
    # A longer variant name falls back to its family ...
    assert lookup_price("openai", "gpt-5-chat-latest") == lookup_price("openai", "gpt-5-chat")
    assert lookup_price("openai", "gpt-4o-mini-audio") == lookup_price("openai", "gpt-4o-mini")
    # ... but a newer numeric version never inherits the older family's price.
    assert lookup_price("anthropic", "claude-opus-4-9") is None
    assert lookup_price("openai", "gpt-7") is None


def test_anthropic_catalog_covers_requested_models() -> None:
    opus = lookup_price("anthropic", "claude-opus-4-1-20250805")
    sonnet = lookup_price("anthropic", "claude-sonnet-4-20250514")
    haiku = lookup_price("anthropic", "claude-3-5-haiku-20241022")
    assert opus is not None and (
        opus.input_cost_micros_per_million,
        opus.output_cost_micros_per_million,
    ) == (15_000_000, 75_000_000)
    assert sonnet is not None and (
        sonnet.input_cost_micros_per_million,
        sonnet.output_cost_micros_per_million,
    ) == (3_000_000, 15_000_000)
    assert haiku is not None and (
        haiku.input_cost_micros_per_million,
        haiku.output_cost_micros_per_million,
    ) == (800_000, 4_000_000)


def test_embeddings_have_zero_output_price() -> None:
    small = lookup_price("openai", "text-embedding-3-small")
    assert small is not None
    assert small.input_cost_micros_per_million == 20_000
    assert small.output_cost_micros_per_million == 0


def test_unknown_provider_or_model_returns_none() -> None:
    assert lookup_price("ollama", "llama3") is None
    assert lookup_price("openai", "totally-unknown-model") is None
    assert lookup_price("openai", "") is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0.0000025", 2_500_000),  # $2.50 / 1M tokens
        ("0.00001", 10_000_000),
        ("0", 0),
        (0.00000015, 150_000),
        ("-1", None),  # OpenRouter's dynamic-pricing marker
        ("nope", None),
        (None, None),
        (True, None),
    ],
)
def test_openrouter_per_token_conversion(raw: object, expected: int | None) -> None:
    assert per_token_usd_to_micros_per_million(raw) == expected


def test_usd_to_micros_rounds_half_up() -> None:
    assert usd_to_micros(12.5) == 12_500_000
    assert usd_to_micros("0.0000005") == 1
    assert usd_to_micros("abc") is None
