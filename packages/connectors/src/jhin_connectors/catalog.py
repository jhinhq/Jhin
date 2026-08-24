"""Curated app catalog for the Apps library (docs/architecture/mcp.md).

The catalog is static, public data (no secrets, no per-workspace state):
well-known apps with either a native Jhin connector, a known official remote
MCP endpoint, or setup notes for servers you must host yourself. Entries
whose endpoint could not be confirmed against the provider's documentation
carry ``url_unverified=True`` so the UI asks for the URL from the provider's
docs instead of pre-filling a guess. Servers that only ship as stdio
processes are flagged ``stdio_only`` — Jhin does not spawn stdio servers yet.
"""

from __future__ import annotations

import json
from functools import cache
from importlib import resources
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

from jhin_connectors.mcp.discovery import SERVER_SLUG_RE

AuthHint = Literal["none", "bearer", "header", "oauth"]
TransportHint = Literal["streamable_http", "sse", "unknown"]

CATALOG_CATEGORIES: tuple[str, ...] = (
    "Developer tools",
    "Project management",
    "Communication",
    "Documents & knowledge",
    "Payments & commerce",
    "CRM & support",
    "Design",
    "Search & web",
    "Data & infrastructure",
    "Automation",
    "Productivity",
    "Storage",
)


class CatalogApp(BaseModel):
    """One library entry. Everything here is safe for any authenticated user."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    slug: str
    name: str
    category: str
    icon: str
    description: str
    # Native Jhin connector type when one exists (GitHub, Linear, …).
    connector_type: str | None = None
    # Official remote MCP endpoint when known.
    mcp_url: str | None = None
    url_unverified: bool = False
    transport: TransportHint = "unknown"
    auth_hint: AuthHint = "bearer"
    auth_note: str = ""
    docs_url: str = ""
    setup_note: str = ""
    stdio_only: bool = False
    # Non-secret connection config values the Connect dialog pre-fills for a
    # native connector (e.g. the web connector's pre-selected search_backend).
    connector_config: dict[str, str] = {}

    @field_validator("connector_config")
    @classmethod
    def _connector_config(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > 10:
            raise ValueError("connector_config accepts at most 10 entries")
        for key, entry in value.items():
            if not key or len(key) > 64 or len(entry) > 500:
                raise ValueError("connector_config entries are too long")
        return value

    @field_validator("slug")
    @classmethod
    def _slug(cls, value: str) -> str:
        if not SERVER_SLUG_RE.fullmatch(value):
            raise ValueError("catalog slug must match [a-z0-9_]{1,32}")
        return value

    @field_validator("category")
    @classmethod
    def _category(cls, value: str) -> str:
        if value not in CATALOG_CATEGORIES:
            raise ValueError(f"unknown catalog category {value!r}")
        return value

    @field_validator("mcp_url")
    @classmethod
    def _url(cls, value: str | None) -> str | None:
        if value is not None and not value.startswith("https://"):
            raise ValueError("catalog MCP endpoints must be https")
        return value

    @property
    def connectable(self) -> bool:
        """Whether the Connect button can do something today."""
        return self.connector_type is not None or not self.stdio_only


@cache
def load_catalog() -> tuple[CatalogApp, ...]:
    raw = resources.files("jhin_connectors").joinpath("catalog.json").read_text(encoding="utf-8")
    entries = tuple(CatalogApp.model_validate(item) for item in json.loads(raw))
    slugs = [entry.slug for entry in entries]
    if len(slugs) != len(set(slugs)):
        raise ValueError("catalog slugs must be unique")
    return entries


def catalog_by_slug(slug: str) -> CatalogApp | None:
    for entry in load_catalog():
        if entry.slug == slug:
            return entry
    return None


__all__ = ["CATALOG_CATEGORIES", "AuthHint", "CatalogApp", "catalog_by_slug", "load_catalog"]
