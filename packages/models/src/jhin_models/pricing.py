"""Static public price list for first-party providers (OpenAI, Anthropic).

OpenRouter reports prices live from its ``/models`` endpoint; OpenAI and
Anthropic do not expose pricing through their APIs, so the profile picker
falls back to this catalog of *public list prices*. Entries are micro-dollars
per million tokens (the ``model_profile`` unit). ``CATALOG_UPDATED`` is shown
to the user next to auto-filled prices so they know how fresh the numbers are
and can override them when their contract differs.

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
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

CATALOG_UPDATED = "2026-01"

MICROS_PER_DOLLAR = 1_000_000


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


def lookup_price(provider_type: str, model_id: str) -> ModelPrice | None:
    """Catalog price for ``model_id`` on ``provider_type`` or ``None``.

    Exact match after normalisation first; then progressively shorter
    dash-delimited prefixes (``gpt-4o-mini-audio`` → ``gpt-4o-mini``) unless
    the dropped segment is a bare version number.
    """
    catalog = CATALOGS.get(provider_type)
    if not catalog:
        return None
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


__all__ = [
    "CATALOGS",
    "CATALOG_UPDATED",
    "MICROS_PER_DOLLAR",
    "ModelPrice",
    "lookup_price",
    "normalize_model_id",
    "per_token_usd_to_micros_per_million",
    "usd_to_micros",
]
