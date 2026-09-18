"""Curated app catalog for the Apps library (docs/architecture/mcp.md).

The catalog is static, public data (no secrets, no per-workspace state):
well-known apps with either a native Jhin connector, a known official remote
MCP endpoint, or setup notes for servers you must host yourself. Entries
whose endpoint could not be confirmed against the provider's documentation
carry ``url_unverified=True`` so the UI asks for the URL from the provider's
docs instead of pre-filling a guess. Servers that only ship as stdio
processes are flagged ``stdio_only`` — Jhin does not spawn stdio servers yet.

``sign_in`` is the curated answer to "how does a person connect this?" for
the entries where the fields above would mislead: an app whose own remote
server signs you in, and a provider that has no sign-in at all and really
does want a pasted key.
"""

from __future__ import annotations

import json
import re
from functools import cache
from importlib import resources
from typing import Final, Literal, Self

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from jhin_connectors.mcp.discovery import SERVER_SLUG_RE

AuthHint = Literal["none", "bearer", "header", "oauth"]
TransportHint = Literal["streamable_http", "sse", "unknown"]
SignIn = Literal["auto", "remote_mcp", "key", "none"]

# The one URL shape ``icon_url`` may hold here: GitHub's owner avatar,
# byte-identical to ``ICON_URL_GITHUB_RE`` in the producer that writes
# ``catalog.json``. The bound is the SSRF posture — the icon proxy dials
# whatever this field names, so the field names one reviewed host and
# nothing a publisher typed into a manifest.
ICON_URL_GITHUB_RE: Final[re.Pattern[str]] = re.compile(
    r"^https://github\.com/[A-Za-z0-9-]{1,39}\.png\?size=128$"
)

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
    # Upstream logo URL for the API's icon proxy to fetch; "" when the entry
    # ships no logo. Never handed to a browser — readers publish the
    # same-origin proxy path instead.
    icon_url: str = ""
    # Native Jhin connector type when one exists (GitHub, Linear, …).
    connector_type: str | None = None
    composio_toolkit: str | None = None
    # Official remote MCP endpoint when known.
    mcp_url: str | None = None
    url_unverified: bool = False
    # How a person actually connects this app, when the fields above cannot
    # say. Preferring a native connector is the right guess for most entries,
    # but it cannot tell a provider you sign in to at its own MCP server from
    # one that only ever takes a pasted key. "auto" is that guess, unchanged.
    sign_in: SignIn = "auto"
    transport: TransportHint = "unknown"
    auth_hint: AuthHint = "bearer"
    auth_note: str = ""
    docs_url: str = ""
    setup_note: str = ""
    stdio_only: bool = False
    # True for the dev stack's test doubles ("Fake MCP (dev)", …). The API
    # drops these from every listing on a production-like install so a real
    # deployment never advertises a fake service.
    dev_only: bool = False
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

    @field_validator("icon_url")
    @classmethod
    def _icon_url(cls, value: str) -> str:
        if value and ICON_URL_GITHUB_RE.fullmatch(value) is None:
            raise ValueError("catalog icon_url must be a GitHub owner avatar URL")
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

    @model_validator(mode="after")
    def _validate_sign_in(self) -> Self:
        if self.sign_in == "remote_mcp" and (
            self.mcp_url is None or self.url_unverified or self.stdio_only
        ):
            # The promise "sign in with your account" is only honest when
            # there is a confirmed endpoint to send the browser to. Without
            # one the reader would redirect nobody and land on a blank URL
            # form — worse than the paste form the entry started with.
            raise ValueError("sign_in 'remote_mcp' needs a verified remote MCP endpoint")
        if self.sign_in == "none" and self.auth_hint != "none":
            raise ValueError("sign_in 'none' needs auth_hint 'none'")
        return self

    @property
    def connectable(self) -> bool:
        """Whether the Connect button can do something today."""
        return bool(self.connector_type or self.composio_toolkit or not self.stdio_only)


@cache
def load_catalog() -> tuple[CatalogApp, ...]:
    from jhin_connectors.composio.catalog import CATALOG_TOOLKITS

    raw = resources.files("jhin_connectors").joinpath("catalog.json").read_text(encoding="utf-8")
    entries = tuple(
        CatalogApp.model_validate(
            {
                **item,
                "composio_toolkit": CATALOG_TOOLKITS.get(item["slug"])
                if not item.get("connector_type")
                else None,
            }
        )
        for item in json.loads(raw)
    )
    slugs = [entry.slug for entry in entries]
    if len(slugs) != len(set(slugs)):
        raise ValueError("catalog slugs must be unique")
    return entries


__all__ = [
    "CATALOG_CATEGORIES",
    "ICON_URL_GITHUB_RE",
    "AuthHint",
    "CatalogApp",
    "SignIn",
    "load_catalog",
]
