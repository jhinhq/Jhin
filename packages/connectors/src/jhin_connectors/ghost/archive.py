"""Durable full-archive inventory, reconciled coverage and bounded local retrieval.

Two consecutive complete scans must agree, post for post, before coverage is
complete: every post in a *complete* corpus was read twice, in consecutive
passes, and was byte-identical both times, and a complete corpus names no gaps.
A post first seen in the settling pass has been read once, so it postpones
completion rather than entering it. This detects ordinary insertion/deletion/
update drift in an offset-paginated API; it is a recorded observation, not a
provider-wide transactional snapshot. Search is explicitly lexical; editorial
originality remains an agent judgment.

Coverage is not all-or-nothing. A body that could not be read, a post deleted
mid-scan, or a scan that never settled leaves the corpus *partial*: readable,
version-bound, and carrying the identity and reason of every gap, but never
evidence of a completed originality check. A pass that ended short of the
provider's own stated inventory total cannot name what it missed, so it earns no
corpus version at all and stays unreadable; ending above that total is drift in
the provider's inventory rather than a gap in this one, and stays readable.

A complete corpus is also an answer. A sync request for a blog that already has a
complete, current corpus attaches to that corpus and reads no page at all; only
coverage that never settled, a corpus past the reconciliation bound, or a caller
asking for a refresh starts the scan again. Every receipt says which of those
happened, so a reading taken yesterday is never mistaken for one taken just now.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Awaitable, Callable, Iterable
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from typing import Any, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_connectors.ghost.assignments import require_assignment
from jhin_connectors.ghost.client import GhostApiError, post_revision
from jhin_db.models import Connection
from jhin_db.models.blog_corpus import BlogCorpusDocument, BlogCorpusSync
from jhin_policy import RiskLevel, ToolDefinition
from jhin_tools.builtin import ToolExecutionContext
from jhin_tools.sanitize import sanitize_payload

_MAX_PASSES = 4
_INDEX_VERSION = "lexical-v1"
# How long a complete corpus stands in for a fresh reading of the blog. The plan's
# own freshness defaults set this: it refreshes changes at assignment start and
# reconciles every ID "at least daily while actively used", so a day is the longest
# this archive is meant to go unre-read while work continues against it. Inside that
# day a resumed step attaches to the corpus it already has instead of reading
# thousands of posts again; past it, the scan happens again. Work that needs a
# reading newer than its own age — the check for posts published since, before final
# review — asks for one with refresh rather than waiting for this bound to expire.
_REUSE_MAX_AGE = timedelta(hours=24)
# What this particular sync request did. A corpus read from the blog just now and
# one read yesterday are both 'complete', so the receipt has to say which it holds.
_SCAN_NOTES = {
    "new_scan": (
        "A new scan of the blog was started for this request; it carries no corpus "
        "version yet and nothing is searchable until its status reaches complete or "
        "partial."
    ),
    "attached_running_scan": (
        "A scan of this blog was already running; this request attached to it and "
        "started no second scan."
    ),
    "reused_complete_corpus": (
        "No page of the blog was read for this request: an existing complete corpus "
        "was reused as it stands. corpus_age_seconds is how old that reading is, and "
        "refresh=true scans again instead of reusing it."
    ),
    "retry_of_this_request": (
        "This request had already started a scan; the receipt is that scan's, "
        "unchanged, and no second scan was started."
    ),
}
# Sized from the only real measurements taken against the live 4,498-post
# archive: a 30-post page decoded to 1,128,823 bytes (37.6 KB/post) and a
# 100-post page to 14,189,214 bytes (141.9 KB/post). The heavier one binds.
# Against the client's 16 MiB (16,777,216-byte) response bound:
#   100 posts x 141.9 KB = 14.19 MB = 85% of the bound (1.2x) — the live
#      incident, one long article away from failing an entire sync;
#    20 posts x 141.9 KB =  2.84 MB = 17% of the bound (5.9x), and on the
#      lighter measured content 0.75 MB (22x).
# So the page size stays where the incident settled it, and fetch_page halves a
# page that still comes back too large instead of betting a sync on one number.
ARCHIVE_PAGE_SIZE = 20
_MAX_POSTS = 100_000
# A gap list is evidence, not a payload: the receipt names this many and counts
# the rest. Every gap row is retained, so the count never under-reports.
_MAX_GAP_IDS = 50
_READABLE_COVERAGE = frozenset({"complete", "partial"})
# Deleted posts are kept as named gaps outside every pass's live inventory.
_GAP_PASS = 0


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.ignored = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self.ignored += 1
        if tag in {"p", "div", "li", "br", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"}:
            self.ignored = max(0, self.ignored - 1)

    def handle_data(self, data: str) -> None:
        if not self.ignored:
            self.parts.append(data)


def _text(html: str) -> str:
    parser = _Text()
    parser.feed(html)
    parser.close()
    return " ".join("".join(parser.parts).split())


def _skip_reason(post: dict[str, Any]) -> str | None:
    """Why one post is outside the indexed corpus, or None when it is inside it."""
    if not isinstance(post.get("html"), str):
        return "body_missing"
    if post.get("status") != "published":
        return "not_published"
    if post.get("html_truncated"):
        return "body_truncated"
    if post.get("complete") is False:
        return "provider_marked_incomplete"
    return None


def _pagination(payload: dict[str, Any]) -> tuple[list[Any], int]:
    pagination = payload.get("meta", {}).get("pagination", {}) if isinstance(payload, dict) else {}
    posts = payload.get("posts") if isinstance(payload, dict) else None
    total = pagination.get("total") if isinstance(pagination, dict) else None
    if not isinstance(posts, list) or not isinstance(total, int) or isinstance(total, bool):
        raise GhostApiError(
            "Ghost archive pagination is invalid", code="ghost_archive_page_invalid"
        )
    return posts, total


async def fetch_page(
    read: Callable[[int, int], Awaitable[dict[str, Any]]],
    number: int,
    *,
    size: int = ARCHIVE_PAGE_SIZE,
) -> dict[str, Any]:
    """Read one archive page, halving it on an oversized response instead of failing.

    Page ``number`` at ``size`` covers exactly the posts covered by pages
    ``2 * number - 1`` and ``2 * number`` at half that size, so a page the
    provider cannot deliver under its response bound is re-read as smaller pages
    and rejoined under the caller's numbering. The durable cursor therefore never
    has to know which size a page was actually read at, and one pathological
    article costs a retry rather than the whole sync. Only the client's truthful
    too-large classification triggers this; every other failure still propagates,
    and a single post that will not fit is a failure, never a silent omission.
    """
    if size < 1:
        raise GhostApiError("Ghost archive page size is invalid", code="ghost_archive_page_invalid")
    try:
        return await read(number, size)
    except GhostApiError as error:
        if error.code != "ghost_response_too_large" or size == 1:
            raise
    # An odd page size cannot be split evenly, so it drops straight to single posts;
    # every step keeps the sub-size a divisor of the size above it.
    half = size // 2 if size % 2 == 0 else 1
    posts: list[Any] = []
    total = 0
    for sub in range((number - 1) * size // half + 1, number * size // half + 1):
        chunk = await fetch_page(read, sub, size=half)
        chunk_posts, total = _pagination(chunk)
        posts.extend(chunk_posts)
        if len(posts) >= size or sub * half >= total:
            break
    return {
        "posts": posts,
        "meta": {
            "pagination": {
                "page": number,
                "total": total,
                "next": number + 1 if number * size < total else None,
            }
        },
    }


def _elapsed(moment: datetime, now: datetime) -> timedelta:
    """Age of a stored timestamp. These are UTC, but not every driver says so."""
    return now - (moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC))


async def _corpus_age(session: AsyncSession, sync: BlogCorpusSync) -> int | None:
    """Seconds since this corpus last changed, or None before it has a version.

    Read back through a query rather than off the loaded row: ``updated_at`` is
    filled in by the database, so on a row this transaction has just written it is
    unloaded, and touching it there would be blocking IO inside an async executor.
    """
    if not sync.corpus_hash:
        return None
    changed = await session.scalar(
        select(BlogCorpusSync.updated_at).where(BlogCorpusSync.id == sync.id)
    )
    if changed is None:
        return None
    return int(_elapsed(changed, datetime.now(UTC)).total_seconds())


async def gap_inventory(
    session: AsyncSession, sync: BlogCorpusSync, *, limit: int = _MAX_GAP_IDS
) -> tuple[list[dict[str, str]], int]:
    """Name the posts this corpus does not cover, so an agent can say what it missed."""
    total = await session.scalar(
        select(func.count())
        .select_from(BlogCorpusDocument)
        .where(BlogCorpusDocument.sync_id == sync.id, BlogCorpusDocument.complete.is_(False))
    )
    rows = await session.execute(
        select(BlogCorpusDocument.post_id, BlogCorpusDocument.metadata_json)
        .where(BlogCorpusDocument.sync_id == sync.id, BlogCorpusDocument.complete.is_(False))
        .order_by(BlogCorpusDocument.post_id)
        .limit(limit)
    )
    gaps = [
        {"post_id": post_id, "reason": str((metadata or {}).get("skip_reason", "unknown"))}
        for post_id, metadata in rows
    ]
    return gaps, int(total or 0)


async def receipt(
    session: AsyncSession,
    sync: BlogCorpusSync,
    assignment_id: str | None = None,
    *,
    origin: str | None = None,
) -> dict[str, Any]:
    gaps, gap_total = await gap_inventory(session, sync)
    card: dict[str, Any] = {
        "assignment_id": assignment_id or str(sync.assignment_id),
        "sync_id": str(sync.id),
        "corpus_sync_id": str(sync.id),
        "connection_id": str(sync.connection_id),
        "status": sync.status,
        "discovered": sync.discovered,
        "expected_total": sync.expected_total,
        "indexed": sync.indexed,
        "failed": sync.failed,
        "gaps": gaps,
        "gap_total": gap_total,
        "gaps_truncated": gap_total > len(gaps),
        "corpus_hash": sync.corpus_hash,
        "next_page": sync.next_page,
        "pass_number": sync.pass_number,
        "index_version": sync.index_version,
        "corpus_age_seconds": await _corpus_age(session, sync),
        "error_code": sync.error_code,
        "coverage_note": (
            "Accessible published posts. Complete means two consecutive scans read every "
            "post and agreed exactly, with no gap of any kind; it is not a transactional "
            "snapshot. Partial coverage is readable but is not a completed originality "
            "check; gaps names the posts it does not cover, and gap_total counts them all "
            "even when the list is truncated."
        ),
    }
    if origin is not None:
        card["scan_origin"] = origin
        card["reused"] = origin == "reused_complete_corpus"
        card["scan_note"] = _SCAN_NOTES[origin]
    return card


def _manifest(entries: Iterable[tuple[str, str, str, bool]]) -> str:
    return hashlib.sha256(json.dumps(sorted(entries), separators=(",", ":")).encode()).hexdigest()


async def _record_deletions(
    session: AsyncSession, sync: BlogCorpusSync, deleted: list[BlogCorpusDocument]
) -> None:
    """Keep every post that vanished between passes as a named, bodiless gap.

    The receipt names at most ``_MAX_GAP_IDS`` of them, but discarding the rest
    would make ``gap_total`` — a count of the retained rows — quietly wrong just
    when the archive is moving most. A bounded named list is evidence; a bounded
    total is a lie, so the rows stay and only the listing is bounded.
    """
    if not deleted:
        return
    for row in sorted(deleted, key=lambda row: row.post_id):
        row.seen_pass, row.complete = _GAP_PASS, False
        # Clearing the content hash lets a reinstated post rebuild its body text.
        row.body_text, row.content_hash = "", ""
        row.metadata_json = {**row.metadata_json, "skip_reason": "deleted_during_scan"}
    # Posts vanished mid-scan: this corpus is a record of a moving archive, and
    # the named gaps keep it from ever presenting itself as a complete one.
    sync.error_code = "archive_changed_during_scan"
    await session.flush()


async def persist_page(
    session: AsyncSession, sync: BlogCorpusSync, payload: dict[str, Any]
) -> None:
    """Checkpoint one bounded page, atomically; committed-page retries are no-ops."""
    if sync.status not in {"queued", "running"}:
        return
    posts = payload.get("posts")
    pagination = payload.get("meta", {}).get("pagination", {})
    number, total, next_page = (
        pagination.get("page"),
        pagination.get("total"),
        pagination.get("next"),
    )
    if (
        not isinstance(posts, list)
        or len(posts) > ARCHIVE_PAGE_SIZE
        or not isinstance(number, int)
        or not isinstance(total, int)
        or total < 0
        or total > _MAX_POSTS
        or number < 1
        or (next_page is not None and next_page != number + 1)
    ):
        raise GhostApiError(
            "Ghost archive pagination is invalid", code="ghost_archive_page_invalid"
        )
    if number < sync.next_page:
        return
    if number != sync.next_page:
        raise GhostApiError("Ghost archive page is out of order", code="ghost_archive_page_invalid")
    sync.status = "running"
    sync.expected_total = total
    ids = [str(post.get("id", "")) for post in posts if isinstance(post, dict)]
    if len(ids) != len(posts) or any(
        re.fullmatch(r"[a-fA-F0-9]{24}", value) is None for value in ids
    ):
        raise GhostApiError(
            "Ghost archive contains invalid post identities", code="ghost_archive_page_invalid"
        )
    existing = {
        row.post_id: row
        for row in await session.scalars(
            select(BlogCorpusDocument).where(
                BlogCorpusDocument.sync_id == sync.id, BlogCorpusDocument.post_id.in_(ids)
            )
        )
    }
    for post in posts:
        post_id = str(post["id"])
        reason = _skip_reason(post)
        complete = reason is None
        body = _text(post["html"]) if complete else ""
        revision = post_revision(post)
        content_hash = hashlib.sha256(
            json.dumps(
                {
                    "title": post.get("title", ""),
                    "body": body,
                    "tags": post.get("tags", []),
                    "complete": complete,
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        row = existing.get(post_id)
        if row is None:
            row = BlogCorpusDocument(
                workspace_id=sync.workspace_id,
                sync_id=sync.id,
                post_id=post_id,
                title=str(post.get("title", "")),
                content_hash=content_hash,
                provider_revision=revision,
                seen_pass=sync.pass_number,
            )
            session.add(row)
            existing[post_id] = row
        # Unchanged pages reuse extracted content; versioned syncs stay immutable once complete.
        if row.content_hash != content_hash or row.body_text is None or row.id is None:
            row.body_text = body
        row.title = str(post.get("title", ""))
        row.url = str(post.get("url", ""))
        row.content_hash, row.provider_revision = content_hash, revision
        row.complete, row.seen_pass = complete, sync.pass_number
        row.metadata_json = {
            "slug": post.get("slug", ""),
            "updated_at": post.get("updated_at", ""),
            "tags": post.get("tags", []),
            "html_chars": len(post.get("html") or ""),
            **({"skip_reason": reason} if reason else {}),
        }
    await session.flush()
    current = list(
        await session.execute(
            select(
                BlogCorpusDocument.post_id,
                BlogCorpusDocument.provider_revision,
                BlogCorpusDocument.content_hash,
                BlogCorpusDocument.complete,
            ).where(
                BlogCorpusDocument.sync_id == sync.id,
                BlogCorpusDocument.seen_pass == sync.pass_number,
            )
        )
    )
    sync.discovered = len(current)
    sync.indexed = sum(row.complete for row in current)
    sync.failed = sync.discovered - sync.indexed
    if next_page is not None:
        # Pagination must terminate, but the bound has to follow what the scan is
        # actually consuming rather than the largest page it could have been served:
        # every accepted page carries at least one post, so page N is legitimate
        # only once N posts have been read this pass. A provider answering a
        # legitimately large archive in short pages keeps making progress under
        # that bound; one that keeps paging without delivering a post does not.
        if len(posts) == 0 or number > sync.discovered or sync.discovered > _MAX_POSTS:
            raise GhostApiError(
                "Ghost archive pagination did not converge", code="ghost_archive_page_invalid"
            )
        sync.next_page = next_page
        return
    manifest = _manifest(
        (row.post_id, row.provider_revision, row.content_hash, row.complete) for row in current
    )
    deleted = list(
        await session.scalars(
            select(BlogCorpusDocument).where(
                BlogCorpusDocument.sync_id == sync.id,
                BlogCorpusDocument.seen_pass != sync.pass_number,
                BlogCorpusDocument.seen_pass != _GAP_PASS,
            )
        )
    )
    await _record_deletions(session, sync, deleted)
    gap_total = int(
        await session.scalar(
            select(func.count())
            .select_from(BlogCorpusDocument)
            .where(BlogCorpusDocument.sync_id == sync.id, BlogCorpusDocument.complete.is_(False))
        )
        or 0
    )
    # Coverage settles only when this pass read the provider's whole stated
    # inventory and reproduced the previous pass exactly: same posts, same
    # revisions, same bodies. A post published mid-scan changes the manifest, so
    # it is recorded and costs one more pass rather than entering a corpus it was
    # only ever read into once — 'complete' is what require_research consumes as
    # originality evidence, and it must mean twice-read, gap-free coverage.
    if sync.discovered == total and not deleted and manifest == sync.previous_manifest:
        sync.status = "partial" if sync.failed or sync.error_code or gap_total else "complete"
        sync.corpus_hash = manifest
        sync.active_key = None
    elif sync.pass_number >= _MAX_PASSES:
        # The reconciliation bound is a promise, not a failure: an archive that
        # keeps moving still yields a readable, version-bound corpus whose gaps
        # are named, rather than a sync that restarts from page one forever.
        # Holding *more* than the provider's stated total counts as reaching it:
        # a post unpublished mid-pass shrinks that total under a scan that had
        # already read it, and a superset is drift to record, not coverage to
        # refuse — it is still never called complete. A pass that ended short is
        # the exception: the posts it missed have no identity here, so there is
        # nothing to name and no corpus version to stamp, and the receipt says so.
        sync.status, sync.active_key = "partial", None
        if sync.discovered >= total:
            sync.error_code = sync.error_code or "archive_changed_during_scan"
            sync.corpus_hash = manifest
        else:
            sync.error_code = "archive_inventory_incomplete"
    else:
        sync.previous_manifest = manifest
        sync.pass_number += 1
        sync.next_page = 1
    await session.flush()


async def search_documents(
    session: AsyncSession, sync: BlogCorpusSync, query: str, *, limit: int
) -> list[dict[str, Any]]:
    terms = set(re.findall(r"\w+", query.casefold()))
    matches = []
    # Every inventory row is considered locally; only bounded excerpts enter model context.
    rows = await session.stream_scalars(
        select(BlogCorpusDocument).where(
            BlogCorpusDocument.sync_id == sync.id, BlogCorpusDocument.complete.is_(True)
        )
    )
    async for row in rows:
        title = row.title.casefold()
        body = row.body_text.casefold()
        score = sum((3 if term in title else 0) + math.log1p(body.count(term)) for term in terms)
        if score:
            matches.append(
                (
                    score,
                    row.post_id,
                    {
                        "post_id": row.post_id,
                        "title": row.title[:500],
                        "url": row.url,
                        "revision": row.provider_revision,
                        "corpus_hash": sync.corpus_hash,
                        "sync_id": str(sync.id),
                        "score": round(score, 3),
                        "excerpt": row.body_text[:800],
                    },
                )
            )
    return [match[2] for match in sorted(matches, key=lambda item: (-item[0], item[1]))[:limit]]


async def read_document(
    session: AsyncSession, sync: BlogCorpusSync, post_id: str, *, offset: int, limit: int
) -> dict[str, Any]:
    row = await session.scalar(
        select(BlogCorpusDocument).where(
            BlogCorpusDocument.sync_id == sync.id,
            BlogCorpusDocument.post_id == post_id,
            BlogCorpusDocument.workspace_id == sync.workspace_id,
        )
    )
    if row is None or not row.complete:
        raise GhostApiError(
            "Archive article body is unavailable", code="ghost_archive_body_unavailable"
        )
    if offset > len(row.body_text):
        raise GhostApiError("Archive read offset exceeds body", code="ghost_archive_read_offset")
    end = min(offset + limit, len(row.body_text))
    return {
        "post_id": row.post_id,
        "title": row.title[:500],
        "text": row.body_text[offset:end],
        "offset": offset,
        "total_chars": len(row.body_text),
        "next_offset": end if end < len(row.body_text) else None,
        "revision": row.provider_revision,
        "sync_id": str(sync.id),
        "corpus_hash": sync.corpus_hash,
    }


class ArchiveScope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    connection_id: str
    assignment_id: str


class ArchiveInput(ArchiveScope):
    refresh: bool = Field(
        default=False,
        description=(
            "Scan the blog again even when a recent complete corpus already exists. Use "
            "it when the corpus has to be newer than it is — the check for posts published "
            "since it was built, before final review."
        ),
    )


class ArchiveStatusInput(ArchiveScope):
    sync_id: str


class ArchiveSearchInput(ArchiveStatusInput):
    query: str = Field(min_length=2, max_length=1000)
    limit: int = Field(default=10, ge=1, le=20)


class ArchiveReadInput(ArchiveStatusInput):
    post_id: str = Field(pattern=r"^[a-fA-F0-9]{24}$")
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=6000, ge=100, le=6000)


class ArchiveOutput(BaseModel):
    model_config = ConfigDict(extra="allow")
    assignment_id: str
    sync_id: str
    corpus_sync_id: str
    status: str
    discovered: int
    indexed: int
    failed: int
    gaps: list[dict[str, str]] = Field(default_factory=list)
    gap_total: int = 0
    gaps_truncated: bool = False
    corpus_hash: str | None
    corpus_age_seconds: int | None = None
    # Only a sync request decides between reusing a corpus and scanning again, so
    # only its receipt carries the answer; a status read leaves these unset.
    reused: bool | None = None
    scan_origin: str | None = None
    scan_note: str | None = None


class ArchiveSearchOutput(BaseModel):
    sync_id: str
    corpus_hash: str
    search_mode: str = "lexical_full_corpus"
    semantic_search_available: bool = False
    coverage_status: str = "complete"
    indexed: int = 0
    failed: int = 0
    gap_total: int = 0
    matches: list[dict[str, Any]]


class ArchiveReadOutput(BaseModel):
    model_config = ConfigDict(extra="allow")
    sync_id: str
    post_id: str
    text: str
    corpus_hash: str


async def _reusable(
    session: AsyncSession,
    workspace_id: UUID,
    connection_id: UUID,
    assignment_id: UUID,
) -> BlogCorpusSync | None:
    """The newest complete corpus for this assignment that is still current.

    Only *complete* qualifies. A partial or failed sync records coverage this blog
    never got, and handing one back in place of a scan would quietly turn a named
    gap into a finished originality check.

    Scoped to the assignment, not merely to the blog, because the research gate
    binds evidence that way: ``require_research`` accepts an archive receipt only
    from a sync whose ``assignment_id`` is the one under review. A corpus borrowed
    from a neighbouring article would hand back a receipt reading complete,
    reused, indexed == discovered, no gaps -- and then be refused at review, which
    is a worse failure than the rescan it saved.
    """
    row = (
        await session.execute(
            select(BlogCorpusSync, BlogCorpusSync.updated_at)
            .where(
                BlogCorpusSync.workspace_id == workspace_id,
                BlogCorpusSync.connection_id == connection_id,
                BlogCorpusSync.assignment_id == assignment_id,
                BlogCorpusSync.index_version == _INDEX_VERSION,
                BlogCorpusSync.status == "complete",
                BlogCorpusSync.corpus_hash.is_not(None),
            )
            .order_by(BlogCorpusSync.updated_at.desc(), BlogCorpusSync.id.desc())
            .limit(1)
        )
    ).first()
    if row is None:
        return None
    sync, changed = row
    return sync if _elapsed(changed, datetime.now(UTC)) <= _REUSE_MAX_AGE else None


async def _sync(ctx: ToolExecutionContext, payload: BaseModel) -> ArchiveOutput:
    data = cast(ArchiveInput, payload)
    await require_assignment(ctx, data.assignment_id, data.connection_id)
    connection_id = UUID(data.connection_id)
    held = await ctx.session.scalar(
        select(Connection)
        .where(Connection.id == connection_id, Connection.workspace_id == ctx.workspace_id)
        .with_for_update()
    )
    if held is None:
        # No row to hold is no lock, and nothing then serialises two requests for one
        # blog — which costs a second end-to-end scan of the whole archive.
        raise GhostApiError("Ghost connection is unavailable", code="ghost_archive_unavailable")
    # Keyed on the parsed identity rather than the caller's spelling of it: the
    # uniqueness of this key is what stops a second live scan of one archive, and two
    # spellings of one connection id were two different keys and so two scans.
    active_key = f"{connection_id}:{_INDEX_VERSION}"
    request_key = f"{data.assignment_id}:{ctx.tool_call_id or ctx.run_id}"
    previous = await ctx.session.scalar(
        select(BlogCorpusSync).where(
            BlogCorpusSync.workspace_id == ctx.workspace_id,
            BlogCorpusSync.request_key == request_key,
        )
    )
    if previous is not None:
        return ArchiveOutput(
            **await receipt(
                ctx.session, previous, data.assignment_id, origin="retry_of_this_request"
            )
        )
    sync = await ctx.session.scalar(
        select(BlogCorpusSync).where(
            BlogCorpusSync.workspace_id == ctx.workspace_id, BlogCorpusSync.active_key == active_key
        )
    )
    origin = "attached_running_scan"
    if sync is None and not data.refresh:
        sync = await _reusable(
            ctx.session, ctx.workspace_id, connection_id, UUID(data.assignment_id)
        )
        origin = "reused_complete_corpus"
    if sync is None:
        sync = BlogCorpusSync(
            workspace_id=ctx.workspace_id,
            connection_id=connection_id,
            assignment_id=UUID(data.assignment_id),
            agent_id=ctx.agent_id,
            task_id=ctx.task_id,
            run_id=ctx.run_id,
            index_version=_INDEX_VERSION,
            active_key=active_key,
            request_key=request_key,
        )
        ctx.session.add(sync)
        await ctx.session.flush()
        origin = "new_scan"
    return ArchiveOutput(**await receipt(ctx.session, sync, data.assignment_id, origin=origin))


async def _available(ctx: ToolExecutionContext, data: ArchiveStatusInput) -> BlogCorpusSync:
    await require_assignment(ctx, data.assignment_id, data.connection_id)
    sync = await ctx.session.scalar(
        select(BlogCorpusSync).where(
            BlogCorpusSync.id == UUID(data.sync_id),
            BlogCorpusSync.workspace_id == ctx.workspace_id,
            BlogCorpusSync.connection_id == UUID(data.connection_id),
        )
    )
    if sync is None:
        raise GhostApiError("Archive sync is unavailable", code="ghost_archive_unavailable")
    return sync


async def _status(ctx: ToolExecutionContext, payload: BaseModel) -> ArchiveOutput:
    data = cast(ArchiveStatusInput, payload)
    sync = await _available(ctx, data)
    return ArchiveOutput(**await receipt(ctx.session, sync, data.assignment_id))


async def _readable(ctx: ToolExecutionContext, data: ArchiveStatusInput) -> BlogCorpusSync:
    """Partial coverage is still a corpus; only an unfinished one is unreadable."""
    sync = await _available(ctx, data)
    if sync.status not in _READABLE_COVERAGE or not sync.corpus_hash:
        raise GhostApiError("Archive coverage is not readable yet", code="ghost_archive_incomplete")
    return sync


async def _search(ctx: ToolExecutionContext, payload: BaseModel) -> ArchiveSearchOutput:
    data = cast(ArchiveSearchInput, payload)
    sync = await _readable(ctx, data)
    _, gap_total = await gap_inventory(ctx.session, sync, limit=0)
    result = ArchiveSearchOutput(
        sync_id=str(sync.id),
        corpus_hash=cast(str, sync.corpus_hash),
        coverage_status=sync.status,
        indexed=sync.indexed,
        failed=sync.failed,
        gap_total=gap_total,
        matches=await search_documents(ctx.session, sync, data.query, limit=data.limit),
    )
    if sanitize_payload(result.model_dump(mode="json")) != result.model_dump(mode="json"):
        raise GhostApiError(
            "Archive matches exceed delivery limits; request fewer matches",
            code="ghost_archive_output_limit",
        )
    return result


async def _read(ctx: ToolExecutionContext, payload: BaseModel) -> ArchiveReadOutput:
    data = cast(ArchiveReadInput, payload)
    sync = await _readable(ctx, data)
    result = ArchiveReadOutput(
        coverage_status=sync.status,
        **await read_document(
            ctx.session, sync, data.post_id, offset=data.offset, limit=data.limit
        ),
    )
    if sanitize_payload(result.model_dump(mode="json")) != result.model_dump(mode="json"):
        raise GhostApiError(
            "Archive chunk exceeds delivery limits; request a smaller chunk",
            code="ghost_archive_output_limit",
        )
    return result


def _definition(
    name: str, description: str, model: type[BaseModel], output: type[BaseModel]
) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=description,
        risk=RiskLevel.READ,
        input_model=model,
        output_model=output,
        required_capability=name,
        scope_keys=("connection_id",),
        defers_scope=True,
        redispatch_is_safe=True,
    )


ARCHIVE_TOOLS = (
    (
        _definition(
            "ghost.archive.sync",
            "Start or attach to durable full published-archive ingestion. A complete "
            "corpus indexed within the last day is handed back as it stands and starts "
            "no new scan: reused and corpus_age_seconds say which you got, and refresh=true "
            "forces a fresh scan. Read ghost.archive.status after completion; partial "
            "coverage is searchable but is not originality evidence, and gaps names the "
            "posts it misses.",
            ArchiveInput,
            ArchiveOutput,
        ),
        _sync,
    ),
    (
        _definition(
            "ghost.archive.status",
            "Read persisted inventory coverage, named coverage gaps and corpus version receipt.",
            ArchiveStatusInput,
            ArchiveOutput,
        ),
        _status,
    ),
    (
        _definition(
            "ghost.archive.search",
            "Search the whole indexed local corpus lexically. Read closest matches in depth; "
            "lexical scores do not prove originality, and coverage_status reports whether "
            "any post is missing from the corpus searched.",
            ArchiveSearchInput,
            ArchiveSearchOutput,
        ),
        _search,
    ),
    (
        _definition(
            "ghost.archive.read",
            "Read a version-bound indexed archive body in chunks; follow next_offset. "
            "A post recorded as a coverage gap has no body to read.",
            ArchiveReadInput,
            ArchiveReadOutput,
        ),
        _read,
    ),
)
