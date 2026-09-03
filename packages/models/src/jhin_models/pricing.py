"""Where a model's price comes from, and which source wins.

Jhin knows a price from up to five places. This module owns the built-in
static catalog, the parsing of a refreshed community catalog, and — the part
everything else defers to — the single precedence rule:

    user-entered > measured from spend > live from the provider
                 > refreshed catalog > built-in catalog
                 > assumed free (self-hosted providers only) > unknown

Rationale for the order. A price an admin typed is a contract fact and is
never overwritten by anything automatic. A rate measured from the
organization's own invoices (:mod:`jhin_models.observed_pricing`) beats any
list price because it reflects the discounts actually applied. A live price
from the provider's own ``/models`` response (OpenRouter) is authoritative
list data straight from the source. A community catalog refreshed from
LiteLLM is fresher than whatever shipped in this file. The built-in catalog
below is the offline floor. Anything else is honestly *unknown* — and an
unknown price is reported as unknown, never silently as zero, because a run
priced at $0.00 is a lie that quietly breaks budgets.

The one deliberate exception is a self-hosted provider — Ollama, or an
OpenAI-compatible endpoint the workspace runs itself — where nothing on the
far side meters tokens. A model there with no price is *assumed* free, and
said so in as many words rather than reported as unknown. The assumption is
a reading, not a price: it is never written to the row, so a price entered
later has nothing to displace, and clearing that price falls straight back
to the assumption. Any source that actually knows a number outranks it,
which is why ``self_hosted`` sits last in the precedence.

The built-in catalog holds *public list prices* for OpenAI and Anthropic,
which expose no pricing endpoint at all. Entries are micro-dollars per
million tokens (the ``model_profile`` unit). ``CATALOG_UPDATED`` is shown to
the user next to auto-filled prices so they know how fresh the numbers are.

Lookup normalises identifiers the way providers spell them in model lists:
vendor prefixes (``openai/gpt-4o``), dated snapshots (``gpt-4o-2024-08-06``,
``claude-sonnet-4-20250514``), ``-latest`` suffixes, and Vertex-style
``@20250514`` versions all resolve to their catalog family. A trailing
numeric segment is treated as a *version* (``claude-opus-4-9`` is not priced
as ``claude-opus-4``) so a newer unknown model returns no price rather than a
wrong one.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Literal

from jhin_domain import ModelProviderType

CATALOG_UPDATED = "2026-01"

# How long before the built-in list prices are old enough to warn about. Six
# months is roughly the cadence at which the frontier vendors reprice.
CATALOG_STALE_AFTER_DAYS = 183

MICROS_PER_DOLLAR = 1_000_000

# Where a human goes to check a price we could not determine. Real, current
# pricing pages — these are shown as links next to an unpriced model.
PRICING_PAGES: dict[str, str] = {
    "openai": "https://platform.openai.com/docs/pricing",
    "anthropic": "https://www.anthropic.com/pricing#api",
    "openrouter": "https://openrouter.ai/models",
}


@dataclass(frozen=True)
class ModelPrice:
    input_cost_micros_per_million: int
    output_cost_micros_per_million: int
    context_window: int | None = None


def _usd(input_per_million: str, output_per_million: str, context: int | None) -> ModelPrice:
    return ModelPrice(
        input_cost_micros_per_million=int(Decimal(input_per_million) * MICROS_PER_DOLLAR),
        output_cost_micros_per_million=int(Decimal(output_per_million) * MICROS_PER_DOLLAR),
        context_window=context,
    )


_OPENAI: dict[str, ModelPrice] = {
    "gpt-5": _usd("1.25", "10", 400_000),
    "gpt-5-mini": _usd("0.25", "2", 400_000),
    "gpt-5-nano": _usd("0.05", "0.40", 400_000),
    "gpt-5-chat": _usd("1.25", "10", 400_000),
    "gpt-5-pro": _usd("15", "120", 400_000),
    "gpt-5.1": _usd("1.25", "10", 400_000),
    "gpt-5.2": _usd("1.75", "14", 400_000),
    "gpt-4.1": _usd("2", "8", 1_047_576),
    "gpt-4.1-mini": _usd("0.40", "1.60", 1_047_576),
    "gpt-4.1-nano": _usd("0.10", "0.40", 1_047_576),
    "gpt-4o": _usd("2.50", "10", 128_000),
    "gpt-4o-mini": _usd("0.15", "0.60", 128_000),
    "chatgpt-4o": _usd("5", "15", 128_000),
    "o3": _usd("2", "8", 200_000),
    "o3-pro": _usd("20", "80", 200_000),
    "o3-mini": _usd("1.10", "4.40", 200_000),
    "o4-mini": _usd("1.10", "4.40", 200_000),
    "o1": _usd("15", "60", 200_000),
    "text-embedding-3-small": _usd("0.02", "0", 8_191),
    "text-embedding-3-large": _usd("0.13", "0", 8_191),
    "text-embedding-ada-002": _usd("0.10", "0", 8_191),
}

_ANTHROPIC: dict[str, ModelPrice] = {
    "claude-opus-4-1": _usd("15", "75", 200_000),
    "claude-opus-4": _usd("15", "75", 200_000),
    "claude-opus-4-0": _usd("15", "75", 200_000),
    "claude-opus-4-5": _usd("5", "25", 200_000),
    "claude-opus-4-6": _usd("5", "25", 1_000_000),
    "claude-opus-4-7": _usd("5", "25", 1_000_000),
    "claude-opus-4-8": _usd("5", "25", 1_000_000),
    "claude-opus-5": _usd("5", "25", 1_000_000),
    "claude-fable-5": _usd("10", "50", 1_000_000),
    "claude-sonnet-4": _usd("3", "15", 200_000),
    "claude-sonnet-4-0": _usd("3", "15", 200_000),
    "claude-sonnet-4-5": _usd("3", "15", 200_000),
    "claude-sonnet-4-6": _usd("3", "15", 1_000_000),
    "claude-sonnet-5": _usd("3", "15", 1_000_000),
    "claude-haiku-4-5": _usd("1", "5", 200_000),
    "claude-3-7-sonnet": _usd("3", "15", 200_000),
    "claude-3-5-sonnet": _usd("3", "15", 200_000),
    "claude-3-5-haiku": _usd("0.80", "4", 200_000),
    "claude-3-haiku": _usd("0.25", "1.25", 200_000),
    "claude-3-opus": _usd("15", "75", 200_000),
}

CATALOGS: dict[str, dict[str, ModelPrice]] = {"openai": _OPENAI, "anthropic": _ANTHROPIC}

_VENDOR_PREFIX_RE = re.compile(r"^(openai|anthropic)/")
_DATE_SUFFIX_RE = re.compile(r"[-@](\d{8}|\d{4}-\d{2}-\d{2})$")
_NUMERIC_SEGMENT_RE = re.compile(r"^\d+$")


def normalize_model_id(model_id: str) -> str:
    """Canonical catalog spelling: lowercase, no vendor prefix, no snapshot
    date, no ``-latest``/``-preview`` suffix."""
    key = model_id.strip().lower()
    key = _VENDOR_PREFIX_RE.sub("", key)
    for _ in range(3):  # a few suffixes may stack, e.g. "-preview-2025-01-01"
        before = key
        key = _DATE_SUFFIX_RE.sub("", key)
        for suffix in ("-latest", "-preview"):
            key = key.removesuffix(suffix)
        if key == before:
            break
    return key


def lookup_in_catalog(catalog: Mapping[str, ModelPrice], model_id: str) -> ModelPrice | None:
    """Price for ``model_id`` in one catalog layer, or ``None``.

    Exact match after normalisation first; then progressively shorter
    dash-delimited prefixes (``gpt-4o-mini-audio`` → ``gpt-4o-mini``) unless
    the dropped segment is a bare version number.
    """
    key = normalize_model_id(model_id)
    if not key:
        return None
    if key in catalog:
        return catalog[key]
    parts = key.split("-")
    while len(parts) > 1:
        dropped = parts.pop()
        if _NUMERIC_SEGMENT_RE.match(dropped):
            return None
        candidate = "-".join(parts)
        if candidate in catalog:
            return catalog[candidate]
    return None


def lookup_price(provider_type: str, model_id: str) -> ModelPrice | None:
    """Built-in catalog price for ``model_id`` on ``provider_type``."""
    catalog = CATALOGS.get(provider_type)
    if not catalog:
        return None
    return lookup_in_catalog(catalog, model_id)


def per_token_usd_to_micros_per_million(value: object) -> int | None:
    """OpenRouter-style ``pricing.prompt`` (USD per token as a string) to
    micro-dollars per million tokens: ``price x 1_000_000 tokens x 1_000_000
    micros``. Negative values (OpenRouter's "dynamic" marker) and garbage
    yield ``None``."""
    if value is None or isinstance(value, bool):
        return None
    try:
        price = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not price.is_finite() or price < 0:
        return None
    micros = (price * MICROS_PER_DOLLAR * 1_000_000).to_integral_value(rounding=ROUND_HALF_UP)
    return int(micros)


def usd_to_micros(value: object) -> int | None:
    """Dollar amount (number or numeric string) to micro-dollars."""
    if value is None or isinstance(value, bool):
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not amount.is_finite():
        return None
    return int((amount * MICROS_PER_DOLLAR).to_integral_value(rounding=ROUND_HALF_UP))


# --- Catalog freshness ---


def catalog_updated_date(catalog_updated: str = CATALOG_UPDATED) -> date | None:
    """The ``YYYY-MM`` stamp as the first of that month, or ``None``."""
    try:
        return datetime.strptime(catalog_updated.strip(), "%Y-%m").replace(tzinfo=UTC).date()
    except ValueError:
        return None


def catalog_is_stale(*, today: date | None = None, catalog_updated: str = CATALOG_UPDATED) -> bool:
    """Whether the built-in list prices are old enough to warn about.

    List prices move. Showing a two-year-old number without saying so is how
    a spend total quietly becomes fiction, so the UI nudges the admin to
    check once the catalog passes :data:`CATALOG_STALE_AFTER_DAYS`.
    """
    stamped = catalog_updated_date(catalog_updated)
    if stamped is None:
        return True
    current = today or datetime.now(UTC).date()
    return (current - stamped).days > CATALOG_STALE_AFTER_DAYS


# --- The refreshed community catalog (LiteLLM) ---

# LiteLLM maintains the most complete open price map there is, covering models
# the moment they ship. Verified live: both this path and the packaged
# ``litellm/model_prices_and_context_window_backup.json`` copy serve the same
# ~1.8 MB document.
#
# Licensing: the LiteLLM repository is dual-licensed — everything under
# ``enterprise/`` carries its own terms, everything else is MIT. This file
# lives at the repository root, so it is MIT and may be cached and
# redistributed provided the notice below travels with it. Jhin therefore
# fetches it at run time rather than vendoring it, and stamps
# :data:`LITELLM_ATTRIBUTION` onto every stored snapshot. Never fetch anything
# under ``enterprise/``.
LITELLM_PRICE_MAP_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
)
LITELLM_PROJECT_URL = "https://github.com/BerriAI/litellm"
LITELLM_ATTRIBUTION = "LiteLLM model price map, MIT License, Copyright (c) 2023 Berri AI"
# Generous headroom over the ~1.8 MB the document weighs today, so a normal
# month of growth does not start failing refreshes.
LITELLM_MAX_BYTES = 8 * 1024 * 1024
LITELLM_CATALOG_SOURCE = "litellm"

# Top-level keys that are documentation or routing rules rather than models.
# ``sample_spec`` even carries a ``litellm_provider`` value, so "looks like a
# model" is not a safe filter — these are excluded by name.
_LITELLM_NON_MODEL_KEYS = frozenset({"sample_spec", "fallback_generalizations"})
# ``litellm_provider`` value -> the Jhin provider type it prices for.
_LITELLM_PROVIDERS = {"openai": "openai", "anthropic": "anthropic", "openrouter": "openrouter"}
# Per-token modes only; image, audio, and session-billed entries price in
# units the profile has no column for.
_LITELLM_TOKEN_MODES = frozenset({"chat", "completion", "responses", "embedding"})

RefreshedCatalog = dict[str, dict[str, ModelPrice]]


def _litellm_price(entry: Mapping[str, Any]) -> ModelPrice | None:
    input_micros = per_token_usd_to_micros_per_million(entry.get("input_cost_per_token"))
    output_micros = per_token_usd_to_micros_per_million(entry.get("output_cost_per_token"))
    if input_micros is None and output_micros is None:
        return None
    context = entry.get("max_input_tokens")
    window = int(context) if isinstance(context, int | float) and context > 0 else None
    return ModelPrice(
        input_cost_micros_per_million=input_micros or 0,
        output_cost_micros_per_million=output_micros or 0,
        context_window=window,
    )


def parse_litellm_price_map(payload: object) -> RefreshedCatalog:
    """A provider-keyed catalog from LiteLLM's ``model_prices_and_context_window.json``.

    The document is a flat ``model id -> entry`` object of a few thousand
    keys. Only ``litellm_provider`` is present on every entry, so every other
    field is treated as optional and a malformed entry is dropped rather than
    raising — a community file must never be able to break pricing.

    Keys are folded to the same normalised spelling the built-in catalog uses
    (:func:`normalize_model_id`), which collapses dated snapshots onto their
    family. When several raw keys collapse together the undated alias wins,
    because that is the entry LiteLLM keeps current.
    """
    if not isinstance(payload, dict):
        return {}
    catalogs: RefreshedCatalog = {}
    # (provider, normalised key) -> was the entry we kept the undated alias?
    from_alias: dict[tuple[str, str], bool] = {}
    for raw_key in sorted(str(k) for k in payload):
        if raw_key in _LITELLM_NON_MODEL_KEYS:
            continue
        entry = payload.get(raw_key)
        if not isinstance(entry, dict):
            continue
        litellm_provider = str(entry.get("litellm_provider", ""))
        provider_type = _LITELLM_PROVIDERS.get(litellm_provider)
        if provider_type is None:
            continue
        # LiteLLM prefixes routed entries with their provider
        # (``openrouter/anthropic/claude-3.5-sonnet``). Jhin stores the model
        # name the way the provider's own API spells it, so drop the prefix
        # before normalising or nothing would ever match.
        bare_key = raw_key.removeprefix(f"{litellm_provider}/")
        mode = entry.get("mode")
        if mode is not None and str(mode) not in _LITELLM_TOKEN_MODES:
            continue
        price = _litellm_price(entry)
        if price is None:
            continue
        key = normalize_model_id(bare_key)
        if not key:
            continue
        seat = (provider_type, key)
        is_alias = bare_key.strip().lower() == key
        if seat in from_alias and (from_alias[seat] or not is_alias):
            continue  # keep the alias we already have, or the first snapshot
        catalogs.setdefault(provider_type, {})[key] = price
        from_alias[seat] = is_alias
    return catalogs


def refreshed_catalog_to_json(catalog: RefreshedCatalog) -> dict[str, dict[str, list[int | None]]]:
    """Compact ``[input, output, context]`` triples for storage."""
    return {
        provider_type: {
            key: [
                price.input_cost_micros_per_million,
                price.output_cost_micros_per_million,
                price.context_window,
            ]
            for key, price in sorted(entries.items())
        }
        for provider_type, entries in sorted(catalog.items())
    }


def refreshed_catalog_from_json(payload: object) -> RefreshedCatalog:
    """Inverse of :func:`refreshed_catalog_to_json`, tolerant of junk."""
    if not isinstance(payload, dict):
        return {}
    catalog: RefreshedCatalog = {}
    for provider_type, entries in payload.items():
        if not isinstance(entries, dict):
            continue
        parsed: dict[str, ModelPrice] = {}
        for key, triple in entries.items():
            if not isinstance(triple, list | tuple) or len(triple) < 2:
                continue
            first, second = triple[0], triple[1]
            if not isinstance(first, int) or not isinstance(second, int):
                continue
            context = triple[2] if len(triple) > 2 else None
            parsed[str(key)] = ModelPrice(
                input_cost_micros_per_million=first,
                output_cost_micros_per_million=second,
                context_window=context if isinstance(context, int) else None,
            )
        if parsed:
            catalog[str(provider_type)] = parsed
    return catalog


def lookup_refreshed_price(
    catalog: RefreshedCatalog, provider_type: str, model_id: str
) -> ModelPrice | None:
    """Refreshed-catalog price for ``model_id`` on ``provider_type``."""
    entries = catalog.get(provider_type)
    if not entries:
        return None
    return lookup_in_catalog(entries, model_id)


# --- Precedence: the one place that decides which source wins ---

#: Provider types that point at an endpoint the workspace runs for itself.
#: Nothing on the far side meters tokens, so a model there with no price is
#: assumed free rather than reported as unknown. Defined once, here, because
#: the API, the web prefill, and the spend report all have to agree on
#: exactly which providers the assumption covers.
SELF_HOSTED_PROVIDER_TYPES: frozenset[str] = frozenset(
    {ModelProviderType.OLLAMA.value, ModelProviderType.OPENAI_COMPATIBLE.value}
)


def is_self_hosted(provider_type: str) -> bool:
    """Whether an unpriced model on ``provider_type`` is assumed to be free."""
    return provider_type in SELF_HOSTED_PROVIDER_TYPES


PriceSource = Literal["user", "observed", "provider", "refreshed_catalog", "catalog", "self_hosted"]

#: Highest authority first. Every surface that has to choose between two
#: known prices consults this order and nothing else. ``self_hosted`` is the
#: $0 an unpriced model on a self-hosted provider resolves to; it is last so
#: that any source which actually knows a number beats it, and it is never
#: stored on a row — clearing a stored price falls back to it.
PRICE_SOURCE_PRECEDENCE: tuple[PriceSource, ...] = (
    "user",
    "observed",
    "provider",
    "refreshed_catalog",
    "catalog",
    "self_hosted",
)


@dataclass(frozen=True)
class PriceCandidate:
    """A price one source is offering, before precedence is applied."""

    source: PriceSource
    input_cost_micros_per_million: int | None = None
    output_cost_micros_per_million: int | None = None
    context_window: int | None = None
    detail: str = ""

    @property
    def is_usable(self) -> bool:
        """Both halves known. A half-price would silently undercount a run,
        so it does not count as knowing the price."""
        return (
            self.input_cost_micros_per_million is not None
            and self.output_cost_micros_per_million is not None
        )


def _self_hosted_candidate(provider_type: str | None) -> PriceCandidate | None:
    if provider_type is None or not is_self_hosted(provider_type):
        return None
    return PriceCandidate(
        source="self_hosted", input_cost_micros_per_million=0, output_cost_micros_per_million=0
    )


def resolve_price(
    candidates: list[PriceCandidate], *, provider_type: str | None = None
) -> PriceCandidate | None:
    """The winning candidate under :data:`PRICE_SOURCE_PRECEDENCE`.

    Order of the input list is irrelevant; only the declared source matters.
    Passing ``provider_type`` adds the $0 ``self_hosted`` candidate for a
    self-hosted provider, so it wins only when nothing else knows a price.
    ``None`` means no source knew the price — which is reported as *unknown*,
    never as free.
    """
    usable = [candidate for candidate in candidates if candidate.is_usable]
    assumed = _self_hosted_candidate(provider_type)
    if assumed is not None:
        usable.append(assumed)
    for source in PRICE_SOURCE_PRECEDENCE:
        for candidate in usable:
            if candidate.source == source:
                return candidate
    return None


def effective_price(
    provider_type: str,
    input_micros: int | None,
    output_micros: int | None,
    stored_source: PriceSource | None,
) -> PriceCandidate | None:
    """The price a stored profile *reports*, with the self-hosted assumption applied.

    A stored pair wins, carrying whatever provenance it has — a row that
    predates provenance tracking counts as user-entered, the same reading the
    write guard and :func:`describe_price_source` give it. With no stored
    pair, a self-hosted provider resolves to the $0 assumption and anything
    else resolves to nothing. The assumption lives only in this return value:
    the row's nulls stay null, so a price entered later has nothing to
    displace and clearing it falls straight back here.
    """
    stored = PriceCandidate(
        source=stored_source if stored_source is not None else "user",
        input_cost_micros_per_million=input_micros,
        output_cost_micros_per_million=output_micros,
    )
    return resolve_price([stored], provider_type=provider_type)


def describe_price_source(
    source: PriceSource | None,
    *,
    priced: bool = False,
    catalog_updated: str = CATALOG_UPDATED,
    refreshed_at: date | None = None,
) -> str:
    """The sentence the UI shows under a price, naming where it came from.

    ``priced`` distinguishes the two ways a source can be unknown: a model
    with no price at all, and a price whose provenance was never recorded
    (rows predating the column, or a price posted straight to the API). The
    second is treated as user-entered, and saying so is what stops the row
    reading as "no price known" when a perfectly good number is sitting there.
    """
    if source == "user":
        return "Entered by an admin in this workspace"
    if source == "observed":
        return "Measured from your actual provider spend"
    if source == "provider":
        return "Live from the provider's own model list"
    if source == "refreshed_catalog":
        when = f" on {refreshed_at.isoformat()}" if refreshed_at is not None else ""
        return (
            "From the community-maintained LiteLLM price catalog (MIT), refreshed"
            f"{when} — community figures can be stale or wrong, which is why anything "
            "you enter or Jhin measures outranks them"
        )
    if source == "catalog":
        return f"Public list price, catalog {catalog_updated}"
    if source == "self_hosted":
        return (
            "Assumed free: a self-hosted endpoint has no per-token price. "
            "Enter prices if this endpoint bills you."
        )
    if priced:
        return (
            "Set before Jhin recorded where prices come from — treated as yours, "
            "so nothing automatic will change it"
        )
    return "No price is known for this model"


__all__ = [
    "CATALOGS",
    "CATALOG_STALE_AFTER_DAYS",
    "CATALOG_UPDATED",
    "LITELLM_ATTRIBUTION",
    "LITELLM_CATALOG_SOURCE",
    "LITELLM_MAX_BYTES",
    "LITELLM_PRICE_MAP_URL",
    "LITELLM_PROJECT_URL",
    "MICROS_PER_DOLLAR",
    "PRICE_SOURCE_PRECEDENCE",
    "PRICING_PAGES",
    "SELF_HOSTED_PROVIDER_TYPES",
    "ModelPrice",
    "PriceCandidate",
    "PriceSource",
    "RefreshedCatalog",
    "catalog_is_stale",
    "catalog_updated_date",
    "describe_price_source",
    "effective_price",
    "is_self_hosted",
    "lookup_in_catalog",
    "lookup_price",
    "lookup_refreshed_price",
    "normalize_model_id",
    "parse_litellm_price_map",
    "per_token_usd_to_micros_per_million",
    "refreshed_catalog_from_json",
    "refreshed_catalog_to_json",
    "resolve_price",
    "usd_to_micros",
]
