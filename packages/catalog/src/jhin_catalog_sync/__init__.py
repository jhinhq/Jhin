"""``jhin-catalog-sync``: the cron job that refreshes the local index of
public MCP servers and agent skills (docs/architecture/catalog.md).

The package is deliberately thin at the top. Importing it must not drag in a
database engine or an HTTP client, so the modules are imported directly —
:mod:`~jhin_catalog_sync.fetch` for the one network boundary,
:mod:`~jhin_catalog_sync.archive` for reading a tarball without extracting it,
:mod:`~jhin_catalog_sync.wire` for turning an upstream record into a row, and
:mod:`~jhin_catalog_sync.loader` for the generation swap. Only the error
family and the schema version are re-exported, because those are what a
caller catches and compares against.
"""

from __future__ import annotations

from jhin_catalog_sync.types import (
    SUPPORTED_SCHEMA_VERSION,
    CatalogFetchError,
    CatalogFormatError,
    CatalogIntegrityError,
    CatalogSyncError,
)

__all__ = [
    "SUPPORTED_SCHEMA_VERSION",
    "CatalogFetchError",
    "CatalogFormatError",
    "CatalogIntegrityError",
    "CatalogSyncError",
]
