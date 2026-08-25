"""Derive a workspace's *real* effective per-token rates from actual spend.

Neither OpenAI nor Anthropic exposes a pricing endpoint, so
:mod:`jhin_models.pricing` can only offer public list prices — wrong for
anyone on a negotiated contract, and absent entirely for a model the catalog
has never heard of. But OpenAI's organization **Admin API** reports the
dollars it actually billed, and Jhin already records exact token counts per
run. Dividing one by the other yields the rate the workspace is really
paying, measured rather than assumed.

This module is the pure part: line-item parsing and arithmetic, no HTTP and
no database. Callers hand it :class:`ModelCostObservation` rows (what the
provider billed, per model) and :class:`ModelTokenTotals` rows (what Jhin
spent, per model, over the *same* period) and get back :class:`DerivedRate`
rows plus an explicit :class:`SkippedModel` for everything it refused to
guess at.

## What OpenAI actually gives us

``GET /v1/organization/costs?group_by=line_item`` returns one result per
invoice line. A line's ``line_item`` is a human-facing label, not a schema —
observed shapes include ``"gpt-4o-2024-08-06, input"``,
``"ft-gpt-4o-2024-08-06, output"``, ``"evals | gpt-4o-mini, input"``,
``"assistants api | file search"``, and a bare ``"input_tokens"``. Only the
plain ``"<model>, <side>"`` shape is used; everything else is counted into an
ignored bucket and reported, never guessed at. When the API also fills
``quantity``/``quantity_unit`` the line carries the billed **token count**,
which is the good case (below).

## The four derivations, best first

``provider_quantity``
    The provider reported both dollars *and* billed tokens for the line, so
    the rate is a straight division of its own numbers. Nothing is assumed
    and — crucially — it is immune to the attribution problem below, because
    numerator and denominator cover the same traffic.

``split``
    The provider itemised input and output dollars but no token counts, so
    each side is divided by Jhin's own token counts.

``catalog_ratio``
    The provider reported one blended dollar figure for the model. That is a
    single equation with two unknowns::

        cost = rate_in x tokens_in + rate_out x tokens_out

    We close it with the *ratio* ``k = rate_out / rate_in`` taken from the
    public catalog — providers discount input and output roughly in step, so
    the ratio survives a contract discount far better than either absolute
    price does. Then::

        rate_in  = cost / (tokens_in + k x tokens_out)
        rate_out = k x rate_in

    The total is exact; only the split between the two is inferred.

``blended``
    No catalog entry exists for the model (the case that motivated all of
    this), so there is no defensible ratio. Rather than invent one we publish
    a single blended rate over *all* tokens and say so. Applied to a run's
    total tokens it is honest and close; it is never presented as an
    input/output pair, and it is never written into a profile's separate
    input/output columns.

## Guards, because a confidently wrong number is worse than an honest gap

* A model with too little traffic in the period is skipped with its numbers
  quoted, not extrapolated from rounding noise. (Not applied to
  ``provider_quantity``, which does not depend on Jhin's traffic at all.)
* **Attribution:** organization costs cover the whole OpenAI org, not just
  Jhin. If another application shares the org, its dollars land in our
  numerator while its tokens never reach our denominator, so the derived rate
  reads high. We cannot detect that directly, so we compare against the
  catalog: a mild deviation lowers confidence, an extreme one skips the model.
* Callers must exclude the current (incomplete) UTC day from both sides —
  cost buckets for today are still filling while Jhin's token counts for
  today are already complete, which would understate every rate.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

from jhin_models.pricing import ModelPrice, normalize_model_id

Derivation = Literal["provider_quantity", "split", "catalog_ratio", "blended"]
Confidence = Literal["high", "medium", "low"]
TokenSide = Literal["input", "output"]

# A model needs at least this much traffic in the period before dividing by
# Jhin's own token counts is meaningful. Below it the provider's rounding of
# the dollar figure and the lumpiness of a handful of runs dominate.
MIN_RUNS = 3
MIN_INPUT_TOKENS = 10_000
MIN_OUTPUT_TOKENS = 1_000
MIN_COST_MICROS = 1_000  # $0.001

# Deviation from the public list price beyond which we stop trusting a
# correlated measurement: other traffic on the same organization is the usual
# cause. Not applied to ``provider_quantity``, which cannot be contaminated.
SOFT_DEVIATION_FACTOR = Decimal(3)  # -> confidence "low", still reported
HARD_DEVIATION_FACTOR = Decimal(10)  # -> skipped entirely

_TOKENS_PER_MILLION = Decimal(1_000_000)

# "<model>, <side>" — the only line-item shape we attribute to a model. A
# leading "<surface> | " (evals, batch, assistants) is deliberately *not*
# accepted: those surfaces can be priced differently from plain completions,
# and their tokens never pass through Jhin, so both halves of the division
# would be wrong.
_LINE_ITEM_RE = re.compile(r"^(?P<model>[^|,]+),\s*(?P<side>[^|,]+)$")
_TOKEN_UNITS = {"tokens", "1000_tokens"}
_TOKENS_PER_UNIT = {"tokens": 1, "1000_tokens": 1000}


@dataclass(frozen=True)
class CostLine:
    """One parsed, model-attributable invoice line."""

    model_key: str
    side: TokenSide | None
    cost_micros: int
    billed_tokens: int | None = None


def parse_cost_line_item(line_item: object) -> tuple[str, TokenSide | None] | None:
    """``("gpt-4o", "input")`` from ``"gpt-4o-2024-08-06, input"``.

    Returns ``None`` for anything that is not a plain ``"<model>, <side>"``
    label — a surface-prefixed line (``"evals | ..."``), a non-model service
    line (``"assistants api | file search"``), a bare ``"input_tokens"``, or
    an ungrouped ``null``. Callers count those into an "ignored" bucket and
    say so rather than attributing them to a model.

    A ``", cached input"`` variant resolves to ``input``: Jhin's recorded
    input token count includes cached tokens, so folding the discounted line
    in is what makes the measured rate an *effective* rate.
    """
    if not isinstance(line_item, str):
        return None
    match = _LINE_ITEM_RE.match(line_item.strip())
    if match is None:
        return None
    model = match.group("model").strip()
    side_text = match.group("side").strip().lower()
    if not model:
        return None
    side: TokenSide | None = None
    if side_text.endswith("output"):
        side = "output"
    elif side_text.endswith("input"):
        side = "input"
    else:
        return None
    return normalize_model_id(model), side


def billed_tokens(quantity: object, quantity_unit: object) -> int | None:
    """Token count from a cost line's ``quantity``/``quantity_unit`` pair.

    Only token units are honoured; a line billed per image or per second
    carries no token count and yields ``None``.
    """
    if not isinstance(quantity_unit, str) or quantity_unit not in _TOKEN_UNITS:
        return None
    if quantity is None or isinstance(quantity, bool) or not isinstance(quantity, int | float):
        return None
    count = int(Decimal(str(quantity)) * _TOKENS_PER_UNIT[quantity_unit])
    return count if count > 0 else None


@dataclass(frozen=True)
class ModelCostReport:
    """One provider's itemised spend over a closed period.

    ``ignored_*`` carries everything the invoice billed that could not be
    attributed to a model — non-model services, surface-prefixed lines, and
    ungrouped totals. It is reported rather than dropped so the admin can see
    how much of their bill this reconciliation did *not* explain.
    """

    lines: list[CostLine]
    total_micros: int
    ignored_micros: int
    ignored_labels: list[str]


@dataclass(frozen=True)
class ModelCostObservation:
    """What the provider billed for one model over the period.

    ``input_*``/``output_*`` are filled only when the provider itemised the
    two sides; ``total_cost_micros`` is always the whole amount billed for the
    model and is what the blended arithmetic anchors on. ``*_billed_tokens``
    come from the provider's own ``quantity`` field when it supplies one.
    """

    model_key: str
    total_cost_micros: int
    input_cost_micros: int | None = None
    output_cost_micros: int | None = None
    input_billed_tokens: int | None = None
    output_billed_tokens: int | None = None


@dataclass
class _Accumulator:
    total_cost_micros: int = 0
    input_cost_micros: int | None = None
    output_cost_micros: int | None = None
    input_billed_tokens: int | None = None
    output_billed_tokens: int | None = None

    def add(self, line: CostLine) -> None:
        self.total_cost_micros += line.cost_micros
        if line.side == "input":
            self.input_cost_micros = (self.input_cost_micros or 0) + line.cost_micros
            if line.billed_tokens is not None:
                self.input_billed_tokens = (self.input_billed_tokens or 0) + line.billed_tokens
        elif line.side == "output":
            self.output_cost_micros = (self.output_cost_micros or 0) + line.cost_micros
            if line.billed_tokens is not None:
                self.output_billed_tokens = (self.output_billed_tokens or 0) + line.billed_tokens


def collect_observations(lines: list[CostLine]) -> list[ModelCostObservation]:
    """Fold parsed cost lines into one observation per model."""
    totals: dict[str, _Accumulator] = {}
    for line in lines:
        totals.setdefault(line.model_key, _Accumulator()).add(line)
    return [
        ModelCostObservation(
            model_key=key,
            total_cost_micros=acc.total_cost_micros,
            input_cost_micros=acc.input_cost_micros,
            output_cost_micros=acc.output_cost_micros,
            input_billed_tokens=acc.input_billed_tokens,
            output_billed_tokens=acc.output_billed_tokens,
        )
        for key, acc in sorted(totals.items())
    ]


@dataclass(frozen=True)
class ModelTokenTotals:
    """Tokens Jhin itself sent/received for one model over the same period."""

    model_key: str
    input_tokens: int
    output_tokens: int
    runs: int

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True)
class DerivedRate:
    """One measured rate, with the evidence that produced it."""

    model_key: str
    derivation: Derivation
    confidence: Confidence
    note: str
    input_micros_per_million: int | None = None
    output_micros_per_million: int | None = None
    blended_micros_per_million: int | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    runs: int = 0
    cost_micros: int = 0

    @property
    def is_applicable(self) -> bool:
        """Whether this rate may overwrite a non-user-entered profile price.

        A ``blended`` rate carries no input/output split, and low confidence
        means we suspect the measurement itself, so neither is written into a
        profile automatically — both are still reported so the admin can see
        the number and decide for themselves.
        """
        return (
            self.confidence != "low"
            and self.input_micros_per_million is not None
            and self.output_micros_per_million is not None
        )


@dataclass(frozen=True)
class SkippedModel:
    """A model we deliberately did not price, and why (shown to the admin)."""

    model_key: str
    reason: str


def _rate_micros_per_million(cost_micros: int | Decimal, tokens: int) -> int | None:
    """Micro-dollars per million tokens from ``cost_micros`` over ``tokens``."""
    if tokens <= 0:
        return None
    rate = Decimal(cost_micros) * _TOKENS_PER_MILLION / Decimal(tokens)
    if rate < 0:
        return None
    return int(rate.to_integral_value(rounding=ROUND_HALF_UP))


def _dollars(micros: int) -> str:
    return f"${micros / 1_000_000:.4f}"


def _sample_shortfall(totals: ModelTokenTotals, cost_micros: int) -> str | None:
    """Why this sample is too small to divide, or ``None`` when it is enough."""
    if totals.runs < MIN_RUNS:
        return (
            f"only {totals.runs} run(s) in the period "
            f"(at least {MIN_RUNS} are needed to measure a rate)"
        )
    if cost_micros < MIN_COST_MICROS:
        return (
            f"the provider billed under {_dollars(MIN_COST_MICROS)} for it in the period, "
            "which is too little to divide reliably"
        )
    if totals.input_tokens < MIN_INPUT_TOKENS:
        return (
            f"only {totals.input_tokens:,} input tokens in the period "
            f"(at least {MIN_INPUT_TOKENS:,} are needed)"
        )
    if totals.output_tokens < MIN_OUTPUT_TOKENS:
        return (
            f"only {totals.output_tokens:,} output tokens in the period "
            f"(at least {MIN_OUTPUT_TOKENS:,} are needed)"
        )
    return None


def _deviation(measured: int, reference: int) -> Decimal:
    """How many times ``measured`` is off ``reference``, in either direction."""
    if reference <= 0 or measured <= 0:
        return Decimal(1)
    ratio = Decimal(measured) / Decimal(reference)
    return ratio if ratio >= 1 else 1 / ratio


def _catalog_ratio(price: ModelPrice | None) -> Decimal | None:
    """``output / input`` from the catalog, or ``None`` when it is unusable.

    Embedding models list a zero output price, which would make the split
    degenerate; those fall through to a blended rate instead.
    """
    if price is None:
        return None
    if price.input_cost_micros_per_million <= 0 or price.output_cost_micros_per_million <= 0:
        return None
    return Decimal(price.output_cost_micros_per_million) / Decimal(
        price.input_cost_micros_per_million
    )


def derive_rates(
    *,
    costs: list[ModelCostObservation],
    tokens: list[ModelTokenTotals],
    reference_price: Callable[[str], ModelPrice | None],
    period_label: str = "the period",
) -> tuple[list[DerivedRate], list[SkippedModel]]:
    """Measured rates per model, plus everything skipped with its reason.

    ``reference_price`` supplies the public list price for a model key and is
    used for exactly two things: the input/output *ratio* that closes a
    blended observation, and a plausibility check on a correlated result. It
    never becomes the answer itself.
    """
    tokens_by_key = {row.model_key: row for row in tokens}
    cost_keys = {row.model_key for row in costs}
    derived: list[DerivedRate] = []
    skipped: list[SkippedModel] = []

    for cost in sorted(costs, key=lambda row: row.model_key):
        reference = reference_price(cost.model_key)
        exact = _from_billed_tokens(cost, period_label)
        if exact is not None:
            derived.append(exact)
            continue

        totals = tokens_by_key.get(cost.model_key)
        if totals is None:
            skipped.append(
                SkippedModel(
                    model_key=cost.model_key,
                    reason=(
                        f"the provider billed for it in {period_label} but Jhin ran nothing on "
                        "it and reported no token counts, so there is nothing to divide by"
                    ),
                )
            )
            continue
        shortfall = _sample_shortfall(totals, cost.total_cost_micros)
        if shortfall is not None:
            skipped.append(SkippedModel(model_key=cost.model_key, reason=shortfall))
            continue

        result = _correlate(cost, totals, reference, period_label)
        if isinstance(result, SkippedModel):
            skipped.append(result)
        else:
            derived.append(result)

    for row in sorted(tokens_by_key.values(), key=lambda item: item.model_key):
        if row.model_key in cost_keys:
            continue
        skipped.append(
            SkippedModel(
                model_key=row.model_key,
                reason=(
                    f"Jhin ran {row.runs} time(s) on it in {period_label} but the provider "
                    "reported no cost line naming it, so there are no dollars to divide"
                ),
            )
        )
    return derived, skipped


def _from_billed_tokens(cost: ModelCostObservation, period_label: str) -> DerivedRate | None:
    """The exact rate, when the provider itemised dollars *and* tokens.

    This is the only derivation that needs nothing from Jhin: both halves come
    from the provider's own invoice, so sharing the organization with another
    application cannot skew it.
    """
    if cost.input_cost_micros is None or cost.output_cost_micros is None:
        return None
    if cost.input_billed_tokens is None or cost.output_billed_tokens is None:
        return None
    rate_in = _rate_micros_per_million(cost.input_cost_micros, cost.input_billed_tokens)
    rate_out = _rate_micros_per_million(cost.output_cost_micros, cost.output_billed_tokens)
    if rate_in is None or rate_out is None:
        return None
    return DerivedRate(
        model_key=cost.model_key,
        derivation="provider_quantity",
        confidence="high",
        note=(
            "Measured exactly: your provider itemised both the dollars and the token counts it "
            f"billed in {period_label} ({cost.input_billed_tokens:,} in / "
            f"{cost.output_billed_tokens:,} out tokens for "
            f"{_dollars(cost.total_cost_micros)})."
        ),
        input_micros_per_million=rate_in,
        output_micros_per_million=rate_out,
        input_tokens=cost.input_billed_tokens,
        output_tokens=cost.output_billed_tokens,
        cost_micros=cost.total_cost_micros,
    )


def _correlate(
    cost: ModelCostObservation,
    totals: ModelTokenTotals,
    reference: ModelPrice | None,
    period_label: str,
) -> DerivedRate | SkippedModel:
    """Provider dollars over Jhin's token counts, when the provider gave no
    token counts of its own."""
    sample = (
        f"{totals.runs} run(s), {totals.input_tokens:,} in / {totals.output_tokens:,} out "
        f"tokens against {_dollars(cost.total_cost_micros)} billed in {period_label}"
    )

    if cost.input_cost_micros is not None and cost.output_cost_micros is not None:
        rate_in = _rate_micros_per_million(cost.input_cost_micros, totals.input_tokens)
        rate_out = _rate_micros_per_million(cost.output_cost_micros, totals.output_tokens)
        if rate_in is not None and rate_out is not None:
            return _with_plausibility(
                DerivedRate(
                    model_key=cost.model_key,
                    derivation="split",
                    confidence="high",
                    note=(
                        "Measured from your provider's itemised input and output spend, divided "
                        f"by Jhin's own token counts ({sample})."
                    ),
                    input_micros_per_million=rate_in,
                    output_micros_per_million=rate_out,
                    input_tokens=totals.input_tokens,
                    output_tokens=totals.output_tokens,
                    runs=totals.runs,
                    cost_micros=cost.total_cost_micros,
                ),
                reference,
            )

    ratio = _catalog_ratio(reference)
    if ratio is not None:
        weighted = Decimal(totals.input_tokens) + ratio * Decimal(totals.output_tokens)
        if weighted > 0:
            rate_in = _rate_micros_per_million(
                Decimal(cost.total_cost_micros) * Decimal(totals.input_tokens) / weighted,
                totals.input_tokens,
            )
            if rate_in is not None:
                rate_out = int((Decimal(rate_in) * ratio).to_integral_value(rounding=ROUND_HALF_UP))
                return _with_plausibility(
                    DerivedRate(
                        model_key=cost.model_key,
                        derivation="catalog_ratio",
                        confidence="medium",
                        note=(
                            "Your provider reported one blended cost for this model, so the "
                            "total is measured but the input/output split assumes the public "
                            f"list ratio of {ratio.normalize()}x ({sample})."
                        ),
                        input_micros_per_million=rate_in,
                        output_micros_per_million=rate_out,
                        input_tokens=totals.input_tokens,
                        output_tokens=totals.output_tokens,
                        runs=totals.runs,
                        cost_micros=cost.total_cost_micros,
                    ),
                    reference,
                )

    blended = _rate_micros_per_million(cost.total_cost_micros, totals.total_tokens)
    if blended is None:
        return SkippedModel(
            model_key=cost.model_key,
            reason=f"no tokens were recorded for it in {period_label}",
        )
    return DerivedRate(
        model_key=cost.model_key,
        derivation="blended",
        confidence="medium",
        note=(
            "Measured as one blended rate across all tokens: your provider reported a single "
            "cost for this model and no public list price exists to split input from output "
            f"({sample})."
        ),
        blended_micros_per_million=blended,
        input_tokens=totals.input_tokens,
        output_tokens=totals.output_tokens,
        runs=totals.runs,
        cost_micros=cost.total_cost_micros,
    )


def _with_plausibility(
    rate: DerivedRate, reference: ModelPrice | None
) -> DerivedRate | SkippedModel:
    """Sanity-check a correlated rate against the public list price.

    Organization costs are org-wide. When another application shares the same
    OpenAI organization its dollars land in our numerator while its tokens
    never reach our denominator, so the measured rate reads high. A moderate
    gap is legitimate (contract discounts, cached input, tiering); a wild one
    almost never is.
    """
    if reference is None or rate.input_micros_per_million is None:
        return rate
    deviation = _deviation(rate.input_micros_per_million, reference.input_cost_micros_per_million)
    if deviation >= HARD_DEVIATION_FACTOR:
        return SkippedModel(
            model_key=rate.model_key,
            reason=(
                f"the measured input rate came out {deviation.quantize(Decimal('0.1'))}x the "
                "public list price, which usually means something other than Jhin bills to the "
                "same organization; refusing to use a figure that far out"
            ),
        )
    if deviation >= SOFT_DEVIATION_FACTOR:
        return replace(
            rate,
            confidence="low",
            note=(
                f"{rate.note} This is {deviation.quantize(Decimal('0.1'))}x the public list "
                "price — check whether anything else bills to the same organization."
            ),
        )
    return rate


__all__ = [
    "HARD_DEVIATION_FACTOR",
    "MIN_COST_MICROS",
    "MIN_INPUT_TOKENS",
    "MIN_OUTPUT_TOKENS",
    "MIN_RUNS",
    "SOFT_DEVIATION_FACTOR",
    "Confidence",
    "CostLine",
    "Derivation",
    "DerivedRate",
    "ModelCostObservation",
    "ModelCostReport",
    "ModelTokenTotals",
    "SkippedModel",
    "TokenSide",
    "billed_tokens",
    "collect_observations",
    "derive_rates",
    "parse_cost_line_item",
]
