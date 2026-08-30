"""The scheduled entry point: one release in, one active generation out.

Refreshing the catalog is a cron job, not a service. There is no Temporal
workflow and nothing long-running: ``jhin-catalog-sync`` resolves a release,
verifies it, loads it, swaps it in, prunes, and exits. Running it twice is
harmless — a release that is already active is a no-op — and running two at
once is harmless too, because the partial unique index over the active
status decides the swap and the loser exits non-zero without touching the
generation the winner published.

Exit codes are the interface a scheduler reads:

===== =========================================================
``0`` the catalog is current, whether or not this run changed it
``3`` the archive failed its integrity check
``4`` the release could not be fetched
``5`` the archive or a shard was malformed
``1`` anything else, including a lost swap race
===== =========================================================

``--json`` prints exactly one canonical JSON object of the outcome on
stdout, so a wrapper can record what happened without parsing prose.
Failures go to stderr and never carry upstream text.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Sequence
from typing import Final

from jhin_catalog_sync.archive import open_archive
from jhin_catalog_sync.fetch import (
    DEFAULT_SOURCE_REPO,
    SOURCE_REPO_ENV,
    TOTAL_TIMEOUT_SECONDS,
    data_asset_name,
    download_verified_archive,
    new_client,
    resolve_release,
)
from jhin_catalog_sync.loader import SyncOutcome, load_archive, prune_versions
from jhin_catalog_sync.types import (
    CatalogFetchError,
    CatalogFormatError,
    CatalogIntegrityError,
    CatalogSyncError,
)
from jhin_db.engine import create_engine, create_session_factory

EXIT_OK: Final = 0
EXIT_ERROR: Final = 1
EXIT_INTEGRITY: Final = 3
EXIT_FETCH: Final = 4
EXIT_FORMAT: Final = 5

DATABASE_URL_ENV: Final = "DATABASE_URL"


def reserved_slugs() -> frozenset[str]:
    """Slugs the hand-curated built-in library already owns.

    The sync package does not depend on :mod:`jhin_connectors`; when it is
    installed alongside (it is, in every deployment that serves the API) its
    50 curated slugs are reserved so a crawled server cannot be rewritten
    onto one. When it is not, the API's read-time gate still drops any
    collision — this is the first of the two, not the only one.
    """
    try:
        from jhin_connectors.catalog import load_catalog
    except ImportError:
        return frozenset()
    return frozenset(entry.slug for entry in load_catalog())


async def sync_once(*, database_url: str, repo: str, tag: str | None = None) -> SyncOutcome:
    """Fetch, verify, load, swap, and prune, once.

    The network phase runs under a whole-sync wall clock; the database phase
    is bounded by the pool and by the archive's own size, and is deliberately
    outside the deadline so a slow disk cannot abandon a half-loaded
    generation that would otherwise have finished.
    """
    async with new_client() as client:
        try:
            async with asyncio.timeout(TOTAL_TIMEOUT_SECONDS):
                release = await resolve_release(repo=repo, tag=tag, client=client)
                blob, digest = await download_verified_archive(release, client=client)
        except TimeoutError:
            raise CatalogFetchError("the catalog sync exceeded its time budget") from None

    archive = open_archive(blob)
    asset_url = release.assets.get(data_asset_name(release.tag), "")

    engine = create_engine(database_url, trace_sql=False)
    try:
        async with create_session_factory(engine)() as db:
            outcome = await load_archive(
                db,
                archive,
                release_tag=release.tag,
                data_sha256=digest,
                source_repo=repo,
                asset_url=asset_url,
                reserved_slugs=reserved_slugs(),
            )
            await prune_versions(db)
    finally:
        await engine.dispose()
    return outcome


def _outcome_json(outcome: SyncOutcome) -> str:
    return json.dumps(
        {
            "changed": outcome.changed,
            "data_sha256": outcome.data_sha256,
            "entry_count": outcome.entry_count,
            "mcp_count": outcome.mcp_count,
            "rejected_count": outcome.rejected_count,
            "release_tag": outcome.release_tag,
            "resumed": outcome.resumed,
            "skill_count": outcome.skill_count,
            "version_id": str(outcome.version_id),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _describe(outcome: SyncOutcome) -> str:
    if not outcome.changed:
        return f"catalog {outcome.release_tag} is already active ({outcome.entry_count} entries)"
    resumed = " (resumed)" if outcome.resumed else ""
    return (
        f"catalog {outcome.release_tag} activated{resumed}: "
        f"{outcome.entry_count} entries "
        f"({outcome.mcp_count} servers, {outcome.skill_count} skills), "
        f"{outcome.rejected_count} rejected"
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jhin-catalog-sync",
        description="Refresh the local index of MCP servers and agent skills.",
    )
    parser.add_argument(
        "--database-url",
        default=os.environ.get(DATABASE_URL_ENV, ""),
        help=f"async SQLAlchemy URL (default: ${DATABASE_URL_ENV})",
    )
    parser.add_argument(
        "--repo",
        default=os.environ.get(SOURCE_REPO_ENV) or DEFAULT_SOURCE_REPO,
        help=f"catalog source repository as owner/repo (default: ${SOURCE_REPO_ENV} "
        f"or {DEFAULT_SOURCE_REPO})",
    )
    parser.add_argument(
        "--tag",
        default=None,
        help="release tag to load; the latest release when omitted",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print one canonical JSON object describing the outcome",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run one sync and report it. Never raises; always returns an exit code."""
    args = _parser().parse_args(None if argv is None else list(argv))
    if not args.database_url:
        print(f"{DATABASE_URL_ENV} is required (or pass --database-url)", file=sys.stderr)
        return EXIT_ERROR

    try:
        outcome = asyncio.run(
            sync_once(database_url=args.database_url, repo=args.repo, tag=args.tag)
        )
    except CatalogIntegrityError as error:
        print(f"catalog sync failed: {error}", file=sys.stderr)
        return EXIT_INTEGRITY
    except CatalogFetchError as error:
        print(f"catalog sync failed: {error}", file=sys.stderr)
        return EXIT_FETCH
    except CatalogFormatError as error:
        print(f"catalog sync failed: {error}", file=sys.stderr)
        return EXIT_FORMAT
    except CatalogSyncError as error:
        print(f"catalog sync failed: {error}", file=sys.stderr)
        return EXIT_ERROR
    except Exception as error:
        # A scheduled job reports rather than crashes, and it reports a type
        # rather than a message: an unexpected exception is the one kind most
        # likely to be quoting something that came off the network.
        print(f"catalog sync failed: {type(error).__name__}", file=sys.stderr)
        return EXIT_ERROR

    print(_outcome_json(outcome) if args.json else _describe(outcome))
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())


__all__ = [
    "EXIT_ERROR",
    "EXIT_FETCH",
    "EXIT_FORMAT",
    "EXIT_INTEGRITY",
    "EXIT_OK",
    "main",
    "reserved_slugs",
    "sync_once",
]
