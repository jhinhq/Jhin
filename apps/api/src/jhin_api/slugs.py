"""URL-safe slug generation for workspaces and agents."""

from __future__ import annotations

import re
import secrets

_INVALID = re.compile(r"[^a-z0-9]+")


def slugify(value: str, *, max_length: int = 60) -> str:
    slug = _INVALID.sub("-", value.strip().lower()).strip("-")[:max_length].strip("-")
    return slug or f"item-{secrets.token_hex(3)}"


def with_suffix(slug: str) -> str:
    """Disambiguate a taken slug with a short random suffix."""
    return f"{slug}-{secrets.token_hex(3)}"
