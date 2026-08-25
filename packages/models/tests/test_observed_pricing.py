"""Deriving real per-token rates from real spend.

The arithmetic here decides what a workspace is told its models cost, so the
tests pin down not just the happy path but every place the module refuses to
answer: too small a sample, a rate that cannot be split honestly, and a
measurement that looks contaminated by traffic Jhin never sent.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from jhin_models.observed_pricing import (
    CostLine,
    DerivedRate,
    ModelCostObservation,
    ModelTokenTotals,
    SkippedModel,
    billed_tokens,
    collect_observations,
    derive_rates,
)
from jhin_models.pricing import ModelPrice, lookup_price

# $2.50 in / $10.00 out per 1M tokens, the shape of a typical chat model.
_LIST = ModelPrice(
    input_cost_micros_per_million=2_500_000,
    output_cost_micros_per_million=10_000_000,
    context_window=128_000,
)


def _no_reference(_model_key: str) -> ModelPrice | None:
    return None


def _list_reference(_model_key: str) -> ModelPrice | None:
    return _LIST


def _tokens(**kwargs: int) -> ModelTokenTotals:
    return ModelTokenTotals(
        model_key=str(kwargs.pop("key", "gpt-4o")),  # type: ignore[arg-type]
        input_tokens=kwargs.get("input_tokens", 1_000_000),
        output_tokens=kwargs.get("output_tokens", 100_000),
        runs=kwargs.get("runs", 25),
    )


def _only(rates: list[DerivedRate]) -> DerivedRate:
    assert len(rates) == 1, rates
    return rates[0]


# --- Line-item parsing (the OpenAI Admin API's human-facing labels) ---


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("gpt-4o-2024-08-06, input", ("gpt-4o", "input")),
        ("gpt-4o-2024-08-06, output", ("gpt-4o", "output")),
        ("gpt-5.2, cached input", ("gpt-5.2", "input")),
        ("ft-gpt-4o-2024-08-06, input", ("ft-gpt-4o", "input")),
        # Surface-prefixed and non-model lines are not attributable: their
        # tokens never passed through Jhin and they can be priced differently.
        ("evals | gpt-4o-mini-2024-07-18, input", None),
        ("assistants api | file search", None),
        ("input_tokens", None),
        (None, None),
        ("", None),
        ("gpt-4o, images", None),
    ],
)
def test_line_item_parsing_only_accepts_plain_model_side_labels(
    label: object, expected: tuple[str, str] | None
) -> None:
    from jhin_models.observed_pricing import parse_cost_line_item

    assert parse_cost_line_item(label) == expected


@pytest.mark.parametrize(
    ("quantity", "unit", "expected"),
    [
        (10_000, "tokens", 10_000),
        (2.5, "1000_tokens", 2_500),
        (0, "tokens", None),
        (None, "tokens", None),
        (10, "images", None),
        (10, None, None),
        (True, "tokens", None),
    ],
)
def test_billed_tokens_only_honours_token_units(
    quantity: object, unit: object, expected: int | None
) -> None:
    assert billed_tokens(quantity, unit) == expected


def test_collect_observations_folds_sides_and_keeps_the_total() -> None:
    lines = [
        CostLine("gpt-4o", "input", 2_500_000, billed_tokens=1_000_000),
        CostLine("gpt-4o", "input", 2_500_000, billed_tokens=1_000_000),
        CostLine("gpt-4o", "output", 1_000_000, billed_tokens=100_000),
    ]
    observation = collect_observations(lines)[0]
    assert observation.total_cost_micros == 6_000_000
    assert observation.input_cost_micros == 5_000_000
    assert observation.input_billed_tokens == 2_000_000
    assert observation.output_billed_tokens == 100_000


# --- The four derivations ---


def test_provider_quantity_is_exact_and_needs_nothing_from_jhin() -> None:
    """The best case: the invoice carries both dollars and tokens.

    No correlation with Jhin's own counts, so sharing the organization with
    another application cannot skew the answer — and no sample-size floor
    applies, because the sample is the provider's, not ours.
    """
    costs = [
        ModelCostObservation(
            model_key="gpt-4o",
            total_cost_micros=3_500_000,
            input_cost_micros=2_500_000,
            output_cost_micros=1_000_000,
            input_billed_tokens=1_000_000,
            output_billed_tokens=100_000,
        )
    ]
    derived, skipped = derive_rates(costs=costs, tokens=[], reference_price=_no_reference)
    assert skipped == []
    rate = _only(derived)
    assert rate.derivation == "provider_quantity"
    assert rate.confidence == "high"
    assert rate.input_micros_per_million == 2_500_000  # $2.50 / 1M
    assert rate.output_micros_per_million == 10_000_000  # $10.00 / 1M
    assert rate.is_applicable


def test_split_costs_divide_by_jhins_own_token_counts() -> None:
    """Dollars itemised per side, no token counts: divide by what Jhin sent."""
    costs = [
        ModelCostObservation(
            model_key="gpt-4o",
            total_cost_micros=3_500_000,
            input_cost_micros=2_500_000,
            output_cost_micros=1_000_000,
        )
    ]
    rate = _only(derive_rates(costs=costs, tokens=[_tokens()], reference_price=_list_reference)[0])
    assert rate.derivation == "split"
    assert rate.input_micros_per_million == 2_500_000
    assert rate.output_micros_per_million == 10_000_000
    assert rate.runs == 25


def test_blended_cost_is_split_using_the_catalog_ratio() -> None:
    """One dollar figure, two unknowns, closed with the list price's ratio.

    With k = 10.0/2.5 = 4, tokens_in = 1_000_000 and tokens_out = 100_000::

        rate_in  = cost / (1_000_000 + 4 x 100_000) = cost / 1_400_000
        rate_out = 4 x rate_in

    At a 20% discount off list ($2.00 in / $8.00 out) the bill would be
    $2.00 + $0.80 = $2.80, so the derivation must recover exactly those rates.
    """
    costs = [ModelCostObservation(model_key="gpt-4o", total_cost_micros=2_800_000)]
    rate = _only(derive_rates(costs=costs, tokens=[_tokens()], reference_price=_list_reference)[0])
    assert rate.derivation == "catalog_ratio"
    assert rate.confidence == "medium", "the total is measured, the split is assumed"
    assert rate.input_micros_per_million == 2_000_000
    assert rate.output_micros_per_million == 8_000_000
    assert "assumes the public list ratio" in rate.note
    # The assumption must at least be self-consistent: the recovered rates
    # have to reproduce the bill we started from.
    reconstructed = (
        rate.input_micros_per_million * 1_000_000 // 1_000_000
        + rate.output_micros_per_million * 100_000 // 1_000_000
    )
    assert reconstructed == 2_800_000


def test_unknown_model_with_no_catalog_ratio_gets_an_honest_blended_rate() -> None:
    """The case that motivated the feature: a model no catalog has heard of.

    There is no defensible way to split input from output, so the module
    publishes one blended rate, says so, and refuses to let it be written
    into a profile's separate input/output columns.
    """
    costs = [ModelCostObservation(model_key="gpt-5.6-terra", total_cost_micros=2_200_000)]
    tokens = [
        ModelTokenTotals(
            model_key="gpt-5.6-terra", input_tokens=1_000_000, output_tokens=100_000, runs=25
        )
    ]
    rate = _only(derive_rates(costs=costs, tokens=tokens, reference_price=_no_reference)[0])
    assert rate.derivation == "blended"
    assert rate.blended_micros_per_million == 2_000_000  # $2.20 over 1.1M tokens
    assert rate.input_micros_per_million is None
    assert rate.output_micros_per_million is None
    assert not rate.is_applicable, "a blended rate must never become a guessed pair"
    assert "no public list price exists to split" in rate.note


def test_embedding_catalog_entry_falls_through_to_blended() -> None:
    """A zero output price gives a degenerate ratio, so it is not used."""
    embedding = ModelPrice(input_cost_micros_per_million=20_000, output_cost_micros_per_million=0)
    costs = [ModelCostObservation(model_key="text-embedding-3-small", total_cost_micros=22_000)]
    tokens = [
        ModelTokenTotals(
            model_key="text-embedding-3-small",
            input_tokens=1_000_000,
            output_tokens=100_000,
            runs=25,
        )
    ]
    rate = _only(derive_rates(costs=costs, tokens=tokens, reference_price=lambda _k: embedding)[0])
    assert rate.derivation == "blended"


# --- The guards ---


@pytest.mark.parametrize(
    ("totals", "cost_micros", "expected_fragment"),
    [
        (_tokens(runs=2), 3_500_000, "only 2 run(s)"),
        (_tokens(), 500, "too little to divide"),
        (_tokens(input_tokens=5_000), 3_500_000, "only 5,000 input tokens"),
        (_tokens(output_tokens=500), 3_500_000, "only 500 output tokens"),
    ],
)
def test_too_small_a_sample_is_skipped_with_its_numbers_quoted(
    totals: ModelTokenTotals, cost_micros: int, expected_fragment: str
) -> None:
    """Below the floor, rounding and lumpiness dominate — say so, don't guess."""
    costs = [
        ModelCostObservation(
            model_key="gpt-4o",
            total_cost_micros=cost_micros,
            input_cost_micros=cost_micros,
            output_cost_micros=0,
        )
    ]
    derived, skipped = derive_rates(costs=costs, tokens=[totals], reference_price=_list_reference)
    assert derived == []
    assert len(skipped) == 1
    assert expected_fragment in skipped[0].reason


def test_a_wildly_implausible_rate_is_refused_outright() -> None:
    """Org costs are org-wide; a 20x rate means someone else's traffic.

    Another application billing to the same OpenAI organization puts its
    dollars in our numerator without putting its tokens in our denominator.
    Beyond the hard bound we would rather report nothing.
    """
    costs = [
        ModelCostObservation(
            model_key="gpt-4o",
            total_cost_micros=60_000_000,
            input_cost_micros=50_000_000,  # $50 for 1M input tokens = 20x list
            output_cost_micros=10_000_000,
        )
    ]
    derived, skipped = derive_rates(
        costs=costs, tokens=[_tokens()], reference_price=_list_reference
    )
    assert derived == []
    assert "same organization" in skipped[0].reason


def test_a_moderately_off_rate_is_reported_but_not_applied() -> None:
    """Between the soft and hard bounds: show the number, distrust it."""
    costs = [
        ModelCostObservation(
            model_key="gpt-4o",
            total_cost_micros=11_000_000,
            input_cost_micros=10_000_000,  # $10 for 1M input tokens = 4x list
            output_cost_micros=1_000_000,
        )
    ]
    rate = _only(derive_rates(costs=costs, tokens=[_tokens()], reference_price=_list_reference)[0])
    assert rate.confidence == "low"
    assert not rate.is_applicable, "a suspect rate is never written onto a profile"
    assert "4.0x the public list price" in rate.note


def test_provider_quantity_is_immune_to_the_plausibility_check() -> None:
    """A contract discount can be large; the invoice's own division is exact."""
    costs = [
        ModelCostObservation(
            model_key="gpt-4o",
            total_cost_micros=150_000,
            input_cost_micros=100_000,  # $0.10 / 1M in — 25x *under* list
            output_cost_micros=50_000,
            input_billed_tokens=1_000_000,
            output_billed_tokens=100_000,
        )
    ]
    rate = _only(derive_rates(costs=costs, tokens=[], reference_price=_list_reference)[0])
    assert rate.confidence == "high"
    assert rate.input_micros_per_million == 100_000


def test_spend_without_usage_and_usage_without_spend_are_both_explained() -> None:
    costs = [
        ModelCostObservation(
            model_key="o3", total_cost_micros=5_000_000, input_cost_micros=5_000_000
        )
    ]
    tokens = [ModelTokenTotals(model_key="gpt-4o", input_tokens=99, output_tokens=9, runs=1)]
    derived, skipped = derive_rates(
        costs=costs, tokens=tokens, reference_price=_no_reference, period_label="last week"
    )
    assert derived == []
    reasons = {row.model_key: row.reason for row in skipped}
    assert "Jhin ran nothing on it" in reasons["o3"]
    assert "reported no cost line naming it" in reasons["gpt-4o"]
    assert "last week" in reasons["gpt-4o"]


def test_results_are_deterministic_and_sorted() -> None:
    costs = [
        ModelCostObservation(model_key=key, total_cost_micros=2_200_000)
        for key in ("o3", "gpt-4o", "claude-sonnet-4")
    ]
    tokens = [
        ModelTokenTotals(model_key=key, input_tokens=1_000_000, output_tokens=100_000, runs=25)
        for key in ("o3", "gpt-4o", "claude-sonnet-4")
    ]
    derived, _ = derive_rates(costs=costs, tokens=tokens, reference_price=_no_reference)
    assert [rate.model_key for rate in derived] == ["claude-sonnet-4", "gpt-4o", "o3"]


def test_the_real_catalog_supplies_a_usable_ratio_for_a_known_model() -> None:
    """Guards the seam: the shipped catalog must still expose input and output."""
    price = lookup_price("openai", "gpt-4o-2024-08-06")
    assert price is not None
    ratio = Decimal(price.output_cost_micros_per_million) / Decimal(
        price.input_cost_micros_per_million
    )
    assert ratio == 4


def test_skipped_model_carries_the_key_it_refused() -> None:
    skipped = SkippedModel(model_key="gpt-4o", reason="because")
    assert skipped.model_key == "gpt-4o"
