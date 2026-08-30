"""Projection from the wire onto a database row.

This module is where third-party text stops being third-party text and becomes
a bounded, redacted, control-character-free string of a known maximum length in
a column of that same length. Two rules run through everything here:

* a bad line never costs the shard — an index that refuses to load because one
  crawled repository had a 40 KB README is an index that stops refreshing, so
  :func:`parse_shard` rejects and counts rather than raising;
* nothing upstream says can decide anything structural — not a field
  definition, not a slug that belongs to a curated entry, not which shard a
  record was filed in.

The one thing that *does* abort a shard is a payload that is not UTF-8 at all.
That is not a bad record, it is not a shard.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping
from typing import Any, Final

from jhin_catalog_sync.risk import default_risk, syncable_tier, trust_rank
from jhin_catalog_sync.types import (
    SUPPORTED_SCHEMA_VERSION,
    CatalogFormatError,
    CatalogRecord,
    EntryKind,
    McpEntry,
    SkillEntry,
    shard_for,
)
from jhin_catalog_sync.vocab import CATALOG_CATEGORIES, CATALOG_ICONS
from jhin_secrets.redaction import get_redactor
from jhin_tools.sanitize import sanitize_payload, strict_json_loads

# One JSONL line. Generous next to a real record (a couple of KB) and far
# below anything that could be called a document.
MAX_LINE_BYTES: int = 65_536

# Column widths, named where they are enforced rather than read off the model
# at runtime: the cap has to hold even if the two ever disagree.
MAX_TEXT_CHARS: int = 500
MAX_NAME_CHARS: int = 120
MAX_SUMMARY_CHARS: int = 200
MAX_URL_CHARS: int = 512
MAX_CATEGORY_CHARS: int = 64
MAX_ICON_CHARS: int = 32
MAX_LICENSE_CHARS: int = 64
MAX_SLUG_CHARS: int = 32
MAX_TAG_CHARS: int = 40
MAX_SEARCH_TEXT_CHARS: int = 2_000

# The body of a summary before its ellipsis, so the whole thing fits the
# ``summary`` column with room to spare.
SUMMARY_BODY_CHARS: int = 160
_ELLIPSIS: Final = "…"

# Kind-specific detail blobs. Tighter than the tool-output defaults because
# nobody has to read this text to work — it is provenance shown on a card.
MAX_DETAIL_STRING_CHARS: int = 512
MAX_DETAIL_DOCUMENT_BYTES: int = 8_192

MAX_TAGS: int = 20
MAX_ALIAS_KEYS: int = 20
# These two match the read side's ``_MAX_SOURCES`` / ``_MAX_CONFIG_ENTRIES``
# in ``jhin_api.catalog.service`` exactly, the way MAX_TAGS already matches
# ``_MAX_TAGS``. Persisting more than the API will ever project is not a
# wider bound, it is a silent truncation on the way out: the last entries
# would be stored, indexed, and then dropped with no signal to anybody.
MAX_SOURCES: int = 10
MAX_CONFIG_ENTRIES: int = 10
# A prefill key is a connector manifest field name, not an icon. It shared
# MAX_ICON_CHARS by accident, which coupled the two to no purpose; this is
# the read side's ``_MAX_CONFIG_KEY_CHARS``.
MAX_CONFIG_KEY_CHARS: int = 64

# Six hex digits of the upstream identity, plus the underscore joining them
# to the stem, is what a rewritten slug costs.
_SLUG_SUFFIX_CHARS: Final = 6
_SLUG_ATTEMPTS: Final = 64

_CONTROL_RE: Final = re.compile(r"[\x00-\x1f\x7f-\x9f]")

# The only two URL shapes the producer is permitted to emit for an icon, and
# so the only two the icon proxy will ever dial. Anchored end to end: a host
# spoof (``api.smithery.ai.evil.com``), a userinfo trick, a query bolted onto
# the Smithery shape, or a scheme downgrade all fail the match and the column
# is stored blank. The avatars prefix covers the one redirect target the
# proxy's GitHub shape is allowed to land on.
_ICON_URL_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"^https://api\.smithery\.ai/servers/[^/?#\s]+(/[^/?#\s]+)*/icon$"),
    re.compile(r"^https://github\.com/[A-Za-z0-9-]{1,39}\.png\?size=128$"),
)
ICON_URL_REDIRECT_PREFIX: Final = "https://avatars.githubusercontent.com/"
_FENCE_RE: Final = re.compile(r"```.*?```", re.S)
_IMAGE_RE: Final = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK_RE: Final = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_INLINE_CODE_RE: Final = re.compile(r"`([^`]*)`")
_EMPHASIS_RE: Final = re.compile(r"(\*\*|__|\*|~~)")
_BLOCK_PREFIX_RE: Final = re.compile(r"^\s{0,3}(?:#{1,6}\s+|>\s?|[-*+]\s+|\d+\.\s+)", re.M)

_MCP_DETAIL_FIELDS: Final = frozenset(
    {
        "repo",
        "packages",
        "remotes",
        "tool_count",
        "registry_name",
        "smithery_qualified_name",
        "npm_package",
        "verified_upstream",
        "popularity_signals",
    }
)
_SKILL_DETAIL_FIELDS: Final = frozenset(
    {
        "repo",
        "skill_name",
        "source_ref",
        "skill_path",
        "plugin",
        "commit_sha",
        "model_invocable",
        "allowed_tools",
        "skill_version",
        "frontmatter_bytes",
    }
)

# Canned, in the source, in English. A rejection reason is written here or it
# is not written at all: the alternative is relaying the very text the reason
# exists to keep out of the logs.
_REASON_TOO_LONG: Final = "line exceeds the maximum record length"
_REASON_NOT_JSON: Final = "line is not valid JSON"
_REASON_NOT_OBJECT: Final = "line is not a JSON object"
_REASON_SCHEMA_VERSION: Final = "record declares an unsupported schema version"
_REASON_WRONG_KIND: Final = "record is not the kind this shard holds"
_REASON_WRONG_SHARD: Final = "record is filed in the wrong shard"
_REASON_DUPLICATE: Final = "record repeats a canonical key already seen in this shard"
_REASON_INVALID: Final = "record does not match the wire schema"


def clean_text(value: str, *, max_chars: int = MAX_TEXT_CHARS) -> str:
    """One upstream string, made safe to store and boring to read.

    Redaction runs first, so a secret split across a control character is
    still matched as the value it is; control characters then become spaces
    rather than disappearing, so two words either side of one do not fuse;
    whitespace collapses; and the result is cut to the column width.

    The text is *not* escaped. It is rendered as text by a browser that
    escapes it there, and mangling it here would only make an entry harder to
    read without making it safer.
    """
    if not value:
        return ""
    redacted = get_redactor().redact_text(value)
    stripped = _CONTROL_RE.sub(" ", redacted)
    return " ".join(stripped.split())[:max_chars]


def _flatten_markdown(value: str) -> str:
    """Markdown to one plain line, keeping the words and dropping the marks."""
    without_code = _FENCE_RE.sub(" ", value).replace("```", " ")
    without_images = _IMAGE_RE.sub(" ", without_code)
    with_link_text = _LINK_RE.sub(r"\1", without_images)
    without_ticks = _INLINE_CODE_RE.sub(r"\1", with_link_text).replace("`", " ")
    without_blocks = _BLOCK_PREFIX_RE.sub("", without_ticks)
    return _EMPHASIS_RE.sub("", without_blocks)


def summarize(description: str) -> str:
    """The card's one line: flattened, cleaned, and cut between words.

    A cut lands on a word boundary and says so with an ellipsis, because a
    summary that ends mid-word reads as a rendering bug rather than as a
    deliberately short description.
    """
    flattened = clean_text(_flatten_markdown(description), max_chars=MAX_SUMMARY_CHARS * 4)
    if len(flattened) <= SUMMARY_BODY_CHARS:
        return flattened
    head = flattened[:SUMMARY_BODY_CHARS]
    boundary, separator, _tail = head.rpartition(" ")
    body = (boundary if separator else head).rstrip()
    return f"{body}{_ELLIPSIS}"


def safe_slug(slug: str, canonical_key: str, reserved: frozenset[str]) -> str:
    """A slug no reviewed entry already owns.

    Gate one of three against impersonation. A crawled server that calls
    itself ``github`` is rewritten onto a deterministic, digest-suffixed
    variant of the same stem: the entry keeps a recognisable name, the card
    people recognise stays with the reviewed entry, and two impostors wanting
    the same stem do not land on each other either, because the digest is
    taken over their differing upstream identities.
    """
    if slug not in reserved:
        return slug
    stem = slug[: MAX_SLUG_CHARS - _SLUG_SUFFIX_CHARS - 1]
    candidate = slug
    for attempt in range(_SLUG_ATTEMPTS):
        seed = canonical_key if attempt == 0 else f"{canonical_key}#{attempt}"
        digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:_SLUG_SUFFIX_CHARS]
        candidate = f"{stem}_{digest}"
        if candidate not in reserved:
            return candidate
    return candidate


def safe_icon_url(value: str) -> str:
    """The stored icon URL, or "" for anything the proxy must never dial.

    The producer validates the same two shapes before publishing; this is the
    consumer's own reading of the rule, so a record from anything that was not
    that producer still cannot point the proxy at an arbitrary host. Length is
    the column width, and a control character anywhere is disqualifying — the
    URL patterns exclude whitespace but not every byte the redactor's cousins
    worry about.
    """
    if not value or len(value) > MAX_URL_CHARS or _CONTROL_RE.search(value):
        return ""
    # A dot segment fits the Smithery pattern's character class but never a
    # real qualified name; refusing it here means path normalisation at the
    # far end cannot walk the request off the shape that was matched.
    if any(segment in {".", ".."} for segment in value.split("/")):
        return ""
    if value.startswith(ICON_URL_REDIRECT_PREFIX):
        return value
    if any(pattern.fullmatch(value) for pattern in _ICON_URL_PATTERNS):
        return value
    return ""


def is_publishable(record: CatalogRecord) -> bool:
    """Whether the entry is complete enough to show as a connectable app.

    Everything else is still indexed and still searchable; publishable is the
    narrower claim that a card for it would say something true. A skill is
    never publishable — it is not an app you connect.
    """
    if not isinstance(record, McpEntry):
        return False
    if record.deprecated:
        return False
    if trust_rank(syncable_tier(record.trust_tier)) >= trust_rank("indexed"):
        return False
    if not record.description.strip():
        return False
    if record.icon not in CATALOG_ICONS or record.category not in CATALOG_CATEGORIES:
        return False
    return bool(record.connector_type or record.mcp_url or record.stdio_only)


def _detail(record: CatalogRecord, fields: frozenset[str]) -> dict[str, Any]:
    """One kind-specific blob, sanitised and byte-capped at ingest."""
    blob: dict[str, Any] = record.model_dump(mode="json", include=set(fields))
    return sanitize_payload(
        blob,
        max_string_chars=MAX_DETAIL_STRING_CHARS,
        max_document_bytes=MAX_DETAIL_DOCUMENT_BYTES,
    )


def _clean_list(values: Iterable[str], *, limit: int, max_chars: int) -> list[str]:
    out: list[str] = []
    for value in values:
        if len(out) >= limit:
            break
        cleaned = clean_text(value, max_chars=max_chars)
        if cleaned:
            out.append(cleaned)
    return out


def _search_text(*, name: str, slug: str, category: str, description: str, tags: list[str]) -> str:
    """Lowercased "name slug category description tags", for the GIN index on
    PostgreSQL and the ILIKE fallback everywhere else.

    Built from the *cleaned* fields, so a secret the redactor caught in the
    description cannot come back through the search column.
    """
    parts = [name, slug, category, description, *tags]
    return " ".join(part for part in parts if part).lower()[:MAX_SEARCH_TEXT_CHARS]


def to_row(record: CatalogRecord, *, reserved_slugs: frozenset[str]) -> dict[str, Any]:
    """One validated wire record as one ``catalog_entry`` row.

    The row carries every column the loader does not own — it adds identity
    and the load timestamp — and nothing else, so a schema change has to be
    made deliberately on both sides rather than silently dropping a column.
    """
    mcp = record if isinstance(record, McpEntry) else None
    url_unverified = mcp.url_unverified if mcp is not None else False
    # Provenance is upstream's claim about itself, so it is bounded here the
    # same way a slug is: "curated" and "reviewed" are Jhin's words, not the
    # index's.
    tier = syncable_tier(record.trust_tier)
    # The one path onto the ``reviewed`` tier. The wire may not assert the
    # tier itself; it may carry the ``marketplace_reviewed`` flag, and only a
    # skill that would otherwise land on ``indexed`` is lifted by it — an MCP
    # server or a tier somebody already verified is left exactly as it is.
    if record.kind == "skill" and record.marketplace_reviewed and tier == "indexed":
        tier = "reviewed"

    name = clean_text(record.name, max_chars=MAX_NAME_CHARS)
    slug = safe_slug(record.slug, record.canonical_key, reserved_slugs)
    description = clean_text(record.description, max_chars=MAX_TEXT_CHARS)
    category = clean_text(record.category, max_chars=MAX_CATEGORY_CHARS)
    tags = _clean_list(record.tags, limit=MAX_TAGS, max_chars=MAX_TAG_CHARS)

    return {
        "canonical_key": record.canonical_key,
        "kind": record.kind,
        "slug": slug,
        "name": name,
        "description": description,
        "summary": summarize(record.description),
        "homepage": clean_text(record.homepage, max_chars=MAX_URL_CHARS),
        "docs_url": clean_text(record.docs_url, max_chars=MAX_URL_CHARS),
        "icon_url": safe_icon_url(record.icon_url),
        "trust_tier": tier,
        "trust_rank": trust_rank(tier),
        "default_risk": default_risk(tier, url_unverified=url_unverified).value,
        "popularity": float(record.popularity),
        "category": category,
        "icon": clean_text(record.icon, max_chars=MAX_ICON_CHARS),
        "connector_type": (
            clean_text(mcp.connector_type, max_chars=MAX_ICON_CHARS) or None
            if mcp is not None and mcp.connector_type
            else None
        ),
        "mcp_url": (
            clean_text(mcp.mcp_url, max_chars=MAX_URL_CHARS) or None
            if mcp is not None and mcp.mcp_url
            else None
        ),
        "url_unverified": url_unverified,
        "transport": mcp.transport if mcp is not None else "unknown",
        # A skill has nothing to authenticate against; "none" is the honest
        # member of the closed vocabulary for it.
        "auth_hint": mcp.auth_hint if mcp is not None else "none",
        "auth_note": clean_text(mcp.auth_note, max_chars=MAX_TEXT_CHARS) if mcp else "",
        "setup_note": clean_text(mcp.setup_note, max_chars=MAX_TEXT_CHARS) if mcp else "",
        "stdio_only": mcp.stdio_only if mcp is not None else False,
        "deprecated": record.deprecated,
        "publishable": is_publishable(record),
        "license": clean_text(record.license, max_chars=MAX_LICENSE_CHARS),
        "tags_json": tags,
        "alias_keys_json": _clean_list(
            record.alias_keys, limit=MAX_ALIAS_KEYS, max_chars=MAX_NAME_CHARS
        ),
        "sources_json": [source.model_dump(mode="json") for source in record.sources[:MAX_SOURCES]],
        "connector_config_json": _connector_config(mcp.connector_config if mcp else {}),
        "mcp_json": _detail(record, _MCP_DETAIL_FIELDS) if mcp is not None else {},
        "skill_json": (
            _detail(record, _SKILL_DETAIL_FIELDS) if isinstance(record, SkillEntry) else {}
        ),
        "search_text": _search_text(
            name=name, slug=slug, category=category, description=description, tags=tags
        ),
    }


def _connector_config(raw: Mapping[str, Any]) -> dict[str, str]:
    """Prefill *values* for known connector fields. Never field definitions:
    the API builds the form from installed manifests and ignores every key it
    does not already know."""
    out: dict[str, str] = {}
    for key, value in raw.items():
        if len(out) >= MAX_CONFIG_ENTRIES:
            break
        name = clean_text(str(key), max_chars=MAX_CONFIG_KEY_CHARS)
        if name:
            out[name] = clean_text(str(value), max_chars=MAX_TEXT_CHARS)
    return out


def _model_for(kind: EntryKind) -> type[CatalogRecord]:
    return McpEntry if kind == "mcp" else SkillEntry


def parse_shard(
    raw: bytes, *, kind: EntryKind, shard: str
) -> tuple[tuple[CatalogRecord, ...], tuple[str, ...]]:
    """One shard's JSONL as records plus the reasons the rest were dropped.

    Every gate here is a rejection, not an exception: one crawled repository
    with a 40 KB README must not cost the refresh. The reasons are canned
    strings chosen in this file — a reason that quoted the record would relay
    exactly the text it exists to keep out.
    """
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise CatalogFormatError("a catalog shard is not valid UTF-8") from None

    model = _model_for(kind)
    records: list[CatalogRecord] = []
    reasons: list[str] = []
    seen: set[str] = set()

    for line in text.splitlines():
        if not line.strip():
            continue
        if len(line.encode("utf-8")) > MAX_LINE_BYTES:
            reasons.append(_REASON_TOO_LONG)
            continue
        try:
            payload = strict_json_loads(line)
        except ValueError:
            reasons.append(_REASON_NOT_JSON)
            continue
        if not isinstance(payload, dict):
            reasons.append(_REASON_NOT_OBJECT)
            continue
        if payload.get("schema_version") != SUPPORTED_SCHEMA_VERSION:
            reasons.append(_REASON_SCHEMA_VERSION)
            continue
        if payload.get("kind") != kind:
            reasons.append(_REASON_WRONG_KIND)
            continue
        canonical_key = payload.get("canonical_key")
        if not isinstance(canonical_key, str) or shard_for(canonical_key) != shard:
            reasons.append(_REASON_WRONG_SHARD)
            continue
        if canonical_key in seen:
            reasons.append(_REASON_DUPLICATE)
            continue
        try:
            record = model.model_validate(payload)
        except Exception:
            reasons.append(_REASON_INVALID)
            continue
        seen.add(canonical_key)
        records.append(record)

    return tuple(records), tuple(reasons)


__all__ = [
    "ICON_URL_REDIRECT_PREFIX",
    "MAX_LINE_BYTES",
    "MAX_SEARCH_TEXT_CHARS",
    "MAX_SUMMARY_CHARS",
    "MAX_TEXT_CHARS",
    "clean_text",
    "is_publishable",
    "parse_shard",
    "safe_icon_url",
    "safe_slug",
    "summarize",
    "to_row",
]
