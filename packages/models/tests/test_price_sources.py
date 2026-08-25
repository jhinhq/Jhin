"""Precedence between price sources, catalog staleness, and the LiteLLM map.

The fixture below is trimmed from a live fetch of
``model_prices_and_context_window.json`` (verified 2026-08-24, ~1.8 MB,
3,176 top-level keys) and keeps the shapes that actually break naive
parsers: the ``sample_spec`` documentation entry, the newer
``fallback_generalizations`` routing block, a provider-prefixed key, an
embedding entry whose output price is ``0.0``, and entries missing the
optional fields.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from jhin_models.pricing import (
    CATALOG_STALE_AFTER_DAYS,
    LITELLM_PRICE_MAP_URL,
    PRICE_SOURCE_PRECEDENCE,
    PRICING_PAGES,
    ModelPrice,
    PriceCandidate,
    catalog_is_stale,
    describe_price_source,
    lookup_refreshed_price,
    parse_litellm_price_map,
    refreshed_catalog_from_json,
    refreshed_catalog_to_json,
    resolve_price,
)
from jhin_models.testing.price_catalog import SAMPLE_LITELLM_PRICE_MAP as LITELLM_FIXTURE

# --- LiteLLM map parsing ---


def test_parses_the_real_shape_and_converts_per_token_usd_to_micros() -> None:
    catalog = parse_litellm_price_map(LITELLM_FIXTURE)
    gpt4o = catalog["openai"]["gpt-4o"]
    assert gpt4o.input_cost_micros_per_million == 2_500_000  # 2.5e-06 USD/token
    assert gpt4o.output_cost_micros_per_million == 10_000_000
    assert gpt4o.context_window == 128_000


def test_non_model_keys_are_skipped_by_name() -> None:
    """``sample_spec`` carries a ``litellm_provider``, so shape is no filter.

    ``fallback_generalizations`` was added to the document later and has no
    cost fields at all — a naive loop over the map would raise on it.
    """
    catalog = parse_litellm_price_map(LITELLM_FIXTURE)
    for entries in catalog.values():
        assert "sample_spec" not in entries
        assert "fallback_generalizations" not in entries


def test_only_the_providers_jhin_speaks_are_kept() -> None:
    catalog = parse_litellm_price_map(LITELLM_FIXTURE)
    assert set(catalog) == {"openai", "anthropic", "openrouter"}
    assert "gemini-2.5-pro" not in catalog.get("openai", {})


def test_non_token_modes_and_priceless_entries_are_dropped() -> None:
    """A profile has one price per token; image and session billing has no home."""
    catalog = parse_litellm_price_map(LITELLM_FIXTURE)
    assert "dall-e-3" not in catalog["openai"]
    assert "container" not in catalog["openai"], "no cost fields at all"


def test_a_malformed_entry_never_breaks_the_whole_catalog() -> None:
    catalog = parse_litellm_price_map(LITELLM_FIXTURE)
    assert "broken-entry" not in catalog["openai"]
    assert "gpt-4o" in catalog["openai"], "one bad entry must not lose the good ones"


def test_dated_snapshots_collapse_onto_the_undated_alias() -> None:
    """``gpt-4o`` and ``gpt-4o-2024-05-13`` share a normalised key.

    The alias is the entry LiteLLM keeps current, so it must win regardless
    of which one the iteration reaches first.
    """
    catalog = parse_litellm_price_map(LITELLM_FIXTURE)
    assert catalog["openai"]["gpt-4o"].input_cost_micros_per_million == 2_500_000


def test_provider_prefixed_keys_lose_their_prefix_so_lookups_match() -> None:
    """Jhin stores ``anthropic/claude-3.5-sonnet``, LiteLLM prefixes it."""
    catalog = parse_litellm_price_map(LITELLM_FIXTURE)
    found = lookup_refreshed_price(catalog, "openrouter", "anthropic/claude-3.5-sonnet")
    assert found is not None
    assert found.input_cost_micros_per_million == 3_000_000


def test_lookup_normalises_dated_ids_like_the_builtin_catalog() -> None:
    catalog = parse_litellm_price_map(LITELLM_FIXTURE)
    assert lookup_refreshed_price(catalog, "openai", "gpt-4o-2024-08-06") is not None
    assert lookup_refreshed_price(catalog, "anthropic", "claude-sonnet-4-20250514") is not None
    assert lookup_refreshed_price(catalog, "openai", "no-such-model") is None
    assert lookup_refreshed_price({}, "openai", "gpt-4o") is None


def test_the_map_covers_a_model_the_builtin_catalog_has_never_heard_of() -> None:
    """The whole point: a brand-new model gets a price instead of a shrug."""
    from jhin_models.pricing import lookup_price

    assert lookup_price("openai", "gpt-5.6-terra") is None
    catalog = parse_litellm_price_map(LITELLM_FIXTURE)
    found = lookup_refreshed_price(catalog, "openai", "gpt-5.6-terra")
    assert found is not None
    assert found.input_cost_micros_per_million == 2_000_000
    assert found.output_cost_micros_per_million == 12_000_000
    assert found.context_window == 922_000


def test_embedding_entries_keep_their_zero_output_price() -> None:
    catalog = parse_litellm_price_map(LITELLM_FIXTURE)
    embedding = catalog["openai"]["text-embedding-3-small"]
    assert embedding.input_cost_micros_per_million == 20_000
    assert embedding.output_cost_micros_per_million == 0


@pytest.mark.parametrize("payload", [None, [], "nope", 42, {}])
def test_a_garbage_document_yields_an_empty_catalog_rather_than_raising(payload: object) -> None:
    """A refresh failure must degrade to the previous catalog, never explode."""
    assert parse_litellm_price_map(payload) == {}


def test_the_catalog_round_trips_through_its_stored_form() -> None:
    catalog = parse_litellm_price_map(LITELLM_FIXTURE)
    assert refreshed_catalog_from_json(refreshed_catalog_to_json(catalog)) == catalog


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {"openai": "nope"},
        {"openai": {"gpt-4o": [1]}},
        {"openai": {"gpt-4o": ["a", "b"]}},
        {"openai": {}},
    ],
)
def test_a_corrupted_stored_catalog_is_read_as_empty(payload: object) -> None:
    assert refreshed_catalog_from_json(payload) == {}


def test_the_source_url_is_the_verified_raw_github_path() -> None:
    assert LITELLM_PRICE_MAP_URL.startswith("https://raw.githubusercontent.com/BerriAI/litellm/")
    assert LITELLM_PRICE_MAP_URL.endswith("model_prices_and_context_window.json")


# --- Precedence ---


def _candidate(
    source: str, input_micros: int | None = 1, output_micros: int | None = 2
) -> PriceCandidate:
    return PriceCandidate(
        source=source,  # type: ignore[arg-type]
        input_cost_micros_per_million=input_micros,
        output_cost_micros_per_million=output_micros,
    )


def test_the_precedence_order_is_the_documented_one() -> None:
    assert PRICE_SOURCE_PRECEDENCE == (
        "user",
        "observed",
        "provider",
        "refreshed_catalog",
        "catalog",
    )


def test_every_source_beats_every_lower_one_pairwise() -> None:
    """Exhaustive rather than illustrative: precedence is the whole feature."""
    for high_index, high in enumerate(PRICE_SOURCE_PRECEDENCE):
        for low in PRICE_SOURCE_PRECEDENCE[high_index + 1 :]:
            winner = resolve_price([_candidate(low), _candidate(high)])
            assert winner is not None
            assert winner.source == high, f"{high} must beat {low}"


def test_input_order_does_not_affect_the_winner() -> None:
    forward = resolve_price([_candidate("catalog"), _candidate("user")])
    backward = resolve_price([_candidate("user"), _candidate("catalog")])
    assert forward is not None and backward is not None
    assert forward.source == backward.source == "user"


def test_a_half_known_price_does_not_count_as_known() -> None:
    """One column filled would silently undercount every run on that model."""
    half = _candidate("user", input_micros=5, output_micros=None)
    assert not half.is_usable
    winner = resolve_price([half, _candidate("catalog")])
    assert winner is not None and winner.source == "catalog"


def test_no_candidates_means_unknown_not_free() -> None:
    assert resolve_price([]) is None
    assert resolve_price([_candidate("user", None, None)]) is None


def test_zero_is_a_real_price_and_is_kept() -> None:
    """A genuinely free model must not be confused with an unpriced one."""
    free = _candidate("user", input_micros=0, output_micros=0)
    assert free.is_usable
    winner = resolve_price([free])
    assert winner is not None and winner.input_cost_micros_per_million == 0


# --- Source labels and staleness ---


def test_each_source_gets_a_sentence_that_names_where_the_number_came_from() -> None:
    assert describe_price_source("user") == "Entered by an admin in this workspace"
    assert "actual provider spend" in describe_price_source("observed")
    assert "provider's own model list" in describe_price_source("provider")
    assert "2026-01" in describe_price_source("catalog", catalog_updated="2026-01")
    assert "No price is known" in describe_price_source(None)


def test_a_priced_row_with_no_recorded_source_is_not_called_unpriced() -> None:
    """The two kinds of "unknown" read very differently to a human.

    A row that predates provenance tracking holds a real number and is
    protected like a typed one; calling it "no price known" would be a lie
    about data sitting right there.
    """
    assert "treated as yours" in describe_price_source(None, priced=True)
    assert describe_price_source(None, priced=False) == "No price is known for this model"


def test_a_refreshed_catalog_label_carries_its_fetch_date() -> None:
    label = describe_price_source("refreshed_catalog", refreshed_at=date(2026, 8, 24))
    assert "LiteLLM" in label
    assert "2026-08-24" in label


def test_staleness_flips_after_the_documented_window() -> None:
    fresh = date(2026, 1, 1) + timedelta(days=CATALOG_STALE_AFTER_DAYS)
    stale = fresh + timedelta(days=1)
    assert not catalog_is_stale(today=fresh, catalog_updated="2026-01")
    assert catalog_is_stale(today=stale, catalog_updated="2026-01")


def test_an_unparseable_catalog_stamp_is_treated_as_stale() -> None:
    """Not knowing how old the prices are is itself a reason to warn."""
    assert catalog_is_stale(today=date(2026, 1, 2), catalog_updated="whenever")


def test_pricing_pages_are_real_urls_for_every_priced_provider() -> None:
    assert set(PRICING_PAGES) == {"openai", "anthropic", "openrouter"}
    for url in PRICING_PAGES.values():
        assert url.startswith("https://")


def test_model_price_is_hashable_so_it_can_be_cached() -> None:
    assert hash(ModelPrice(1, 2)) == hash(ModelPrice(1, 2))
