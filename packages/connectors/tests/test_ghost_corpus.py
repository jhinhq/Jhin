"""Full inventory evidence cannot call missing or unstable pages complete."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from jhin_connectors.ghost.archive import (
    _REUSE_MAX_AGE,
    ARCHIVE_PAGE_SIZE,
    ArchiveInput,
    _sync,
    fetch_page,
    persist_page,
    read_document,
    receipt,
    search_documents,
)
from jhin_connectors.ghost.client import GhostApiError
from jhin_db.base import Base
from jhin_db.models import Workspace
from jhin_db.models.blog_corpus import BlogCorpusDocument, BlogCorpusSync
from jhin_db.models.editorial import EditorialAssignment
from jhin_domain import new_uuid7


@pytest.fixture
async def corpus():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        workspace = Workspace(name="Corpus", slug="corpus")
        db.add(workspace)
        await db.flush()
        sync = BlogCorpusSync(
            workspace_id=workspace.id,
            connection_id=new_uuid7(),
            assignment_id=new_uuid7(),
            agent_id=new_uuid7(),
            task_id=new_uuid7(),
            run_id=new_uuid7(),
            status="running",
        )
        db.add(sync)
        await db.commit()
        yield db, sync
    await engine.dispose()


def post(index, *, html=None):
    return {
        "id": f"{index:024x}",
        "title": "Same title",
        "slug": f"article-{index}",
        "status": "published",
        "updated_at": "2026-09-15T10:00:00Z",
        "html": html if html is not None else f"<p>Fan community evidence {index}</p>",
    }


def page(posts, number, total, size=ARCHIVE_PAGE_SIZE):
    pages = max(1, (total + size - 1) // size)
    return {
        "posts": posts,
        "meta": {
            "pagination": {
                "page": number,
                "pages": pages,
                "total": total,
                "next": number + 1 if number < pages else None,
            }
        },
    }


async def test_4500_posts_require_reconciled_exact_inventory_and_keep_long_body(corpus):
    db, sync = corpus
    long = "<p>Community building " + "evidence " * 8000 + "</p>"
    size, pages = ARCHIVE_PAGE_SIZE, -(-4500 // ARCHIVE_PAGE_SIZE)
    for _pass in range(2):
        for number in range(1, pages + 1):
            posts = [
                post(i, html=long if i == 1 else None)
                for i in range((number - 1) * size + 1, number * size + 1)
            ]
            await persist_page(db, sync, page(posts, number, 4500))
            await db.commit()
        if _pass == 0:
            assert sync.status == "running"
    assert sync.status == "complete"
    assert sync.discovered == sync.indexed == 4500
    assert sync.failed == 0
    assert len(sync.corpus_hash) == 64
    docs = list(
        await db.scalars(select(BlogCorpusDocument).where(BlogCorpusDocument.sync_id == sync.id))
    )
    assert len(docs) == 4500  # identical titles never collapse stable provider IDs
    matches = await search_documents(db, sync, "community building", limit=5)
    assert matches[0]["post_id"] == f"{1:024x}"
    assert all(match["corpus_hash"] == sync.corpus_hash for match in matches)
    chunk = await read_document(db, sync, f"{1:024x}", offset=60000, limit=10000)
    assert chunk["total_chars"] > 60000
    assert len(chunk["text"]) <= 10000
    assert chunk["offset"] == 60000


async def test_missing_body_is_partial_not_silently_omitted(corpus):
    db, sync = corpus
    inaccessible = post(2)
    inaccessible.pop("html")
    for _ in range(2):
        await persist_page(db, sync, page([post(1), inaccessible], 1, 2))
    assert sync.status == "partial"
    assert sync.discovered == 2 and sync.indexed == 1 and sync.failed == 1
    card = await receipt(db, sync)
    assert card["gaps"] == [{"post_id": f"{2:024x}", "reason": "body_missing"}]
    assert card["gap_total"] == 1 and card["gaps_truncated"] is False


async def test_changed_inventory_needs_another_pass_and_records_deleted_entries(corpus):
    db, sync = corpus
    await persist_page(db, sync, page([post(1), post(2)], 1, 2))
    await persist_page(db, sync, page([post(2), post(3)], 1, 2))
    assert sync.status == "running"
    await persist_page(db, sync, page([post(2), post(3)], 1, 2))
    ids = list(
        await db.scalars(
            select(BlogCorpusDocument.post_id).where(
                BlogCorpusDocument.sync_id == sync.id, BlogCorpusDocument.complete.is_(True)
            )
        )
    )
    assert sorted(ids) == [f"{2:024x}", f"{3:024x}"]
    # The post that disappeared mid-scan is named, not silently dropped.
    card = await receipt(db, sync)
    assert card["gaps"] == [{"post_id": f"{1:024x}", "reason": "deleted_during_scan"}]
    assert sync.indexed == sync.discovered == 2
    # require_research reads "complete" as proof of a verified originality check.
    # A corpus still naming a post it could not cover has not earned that word.
    assert card["gap_total"] == 1
    assert sync.status == "partial"
    assert sync.error_code == "archive_changed_during_scan"
    assert len(sync.corpus_hash) == 64  # readable and version-bound, just not complete


async def test_no_corpus_with_a_named_gap_can_present_itself_as_complete(corpus):
    """'complete' is originality evidence downstream; gaps and completeness are exclusive."""
    db, sync = corpus
    # Settles cleanly, with nothing missing: this is what complete has to mean.
    for _ in range(2):
        await persist_page(db, sync, page([post(1), post(2)], 1, 2))
    card = await receipt(db, sync)
    assert (sync.status, card["gap_total"], card["gaps"]) == ("complete", 0, [])
    # One unreadable body is enough to withdraw the claim.
    other = BlogCorpusSync(
        workspace_id=sync.workspace_id,
        connection_id=sync.connection_id,
        assignment_id=sync.assignment_id,
        agent_id=sync.agent_id,
        task_id=sync.task_id,
        run_id=sync.run_id,
        status="running",
    )
    db.add(other)
    await db.flush()
    silent = post(2)
    silent.pop("html")
    for _ in range(2):
        await persist_page(db, other, page([post(1), silent], 1, 2))
    gapped = await receipt(db, other)
    assert gapped["status"] == "partial" and gapped["gap_total"] == 1


async def _scan(db, sync, total, *, unreadable=frozenset(), size=ARCHIVE_PAGE_SIZE):
    for number in range(1, (total + size - 1) // size + 1):
        posts = []
        for index in range((number - 1) * size + 1, min(number * size, total) + 1):
            item = post(index)
            if index in unreadable:
                item.pop("html")
            posts.append(item)
        await persist_page(db, sync, page(posts, number, total, size))
        await db.commit()


async def test_two_unreadable_posts_do_not_cost_coverage_of_4496_readable_ones(corpus):
    db, sync = corpus
    unreadable = {7, 2500}
    for _pass in range(2):
        await _scan(db, sync, 4498, unreadable=unreadable)
    assert sync.status == "partial"
    assert sync.discovered == 4498 and sync.indexed == 4496 and sync.failed == 2
    assert len(sync.corpus_hash) == 64
    card = await receipt(db, sync)
    assert card["gaps"] == [
        {"post_id": f"{index:024x}", "reason": "body_missing"} for index in sorted(unreadable)
    ]
    assert card["gap_total"] == 2 and card["expected_total"] == 4498
    # A named gap must not cost the agent the other 4,496 posts.
    matches = await search_documents(db, sync, "fan community evidence", limit=5)
    assert matches and all(match["corpus_hash"] == sync.corpus_hash for match in matches)
    assert (await read_document(db, sync, f"{9:024x}", offset=0, limit=500))["total_chars"] > 0
    with pytest.raises(GhostApiError) as unavailable:
        await read_document(db, sync, f"{7:024x}", offset=0, limit=500)
    assert unavailable.value.code == "ghost_archive_body_unavailable"


async def test_gap_inventory_is_bounded_but_its_count_stays_truthful(corpus):
    db, sync = corpus
    unreadable = set(range(1, 121))
    for _pass in range(2):
        await _scan(db, sync, 200, unreadable=unreadable)
    assert sync.status == "partial"
    assert sync.discovered == 200 and sync.indexed == 80 and sync.failed == 120
    card = await receipt(db, sync)
    assert card["gap_total"] == 120
    assert card["gaps_truncated"] is True
    assert len(card["gaps"]) == 50
    assert card["gaps"][0] == {"post_id": f"{1:024x}", "reason": "body_missing"}


async def test_posts_published_during_the_scan_do_not_restart_the_scan_forever(corpus):
    db, sync = corpus
    await persist_page(db, sync, page([post(1), post(2)], 1, 2))
    assert sync.status == "running" and sync.pass_number == 2
    # A post published mid-scan has been read exactly once, so it cannot enter a
    # corpus called complete: it is recorded, and it costs one more pass.
    await persist_page(db, sync, page([post(1), post(2), post(3)], 1, 3))
    assert sync.status == "running" and sync.pass_number == 3
    assert sync.discovered == 3 and sync.corpus_hash is None
    # The extra pass reads it a second time; that is what completes the corpus.
    await persist_page(db, sync, page([post(1), post(2), post(3)], 1, 3))
    assert sync.status == "complete"
    assert sync.discovered == 3 and sync.indexed == 3 and len(sync.corpus_hash) == 64
    # And the scan still settles: publishing does not restart it from page one forever.
    assert sync.pass_number == 3 <= 4


async def test_a_post_read_only_once_never_enters_a_complete_corpus(corpus):
    """Every post in a complete corpus was read twice, in consecutive passes."""
    db, sync = corpus
    # A steady stream of new posts, one per pass: the scan must never settle on a
    # pass whose inventory it has seen only once, however many such posts arrive.
    for extra in range(4):
        await persist_page(db, sync, page([post(i) for i in range(1, 3 + extra)], 1, 2 + extra))
        assert sync.status != "complete"
    # Four passes without two agreeing reads: readable, named, and not complete.
    assert sync.status == "partial"
    assert sync.error_code == "archive_changed_during_scan"
    assert sync.corpus_hash is not None


async def test_more_deletions_than_a_receipt_can_name_still_count_truthfully(corpus):
    """A bounded named list is evidence; a bounded total would be a false one."""
    db, sync = corpus
    await _scan(db, sync, 120)
    for _ in range(2):
        await _scan(db, sync, 60)
    card = await receipt(db, sync)
    assert card["gap_total"] == 60  # not 50: every vanished post is still counted
    assert len(card["gaps"]) == 50 and card["gaps_truncated"] is True
    assert {gap["reason"] for gap in card["gaps"]} == {"deleted_during_scan"}
    assert card["status"] == "partial" and sync.error_code == "archive_changed_during_scan"
    retained = list(
        await db.scalars(
            select(BlogCorpusDocument.post_id).where(
                BlogCorpusDocument.sync_id == sync.id, BlogCorpusDocument.complete.is_(False)
            )
        )
    )
    assert len(retained) == 60


async def test_a_pass_that_never_saw_the_whole_inventory_gets_no_corpus_version(corpus):
    """Posts never discovered cannot be named, so there is nothing to stamp a version on."""
    db, sync = corpus
    for _ in range(4):
        # The provider keeps reporting five posts and keeps delivering three.
        await persist_page(db, sync, page([post(1), post(2), post(3)], 1, 5))
    assert sync.status == "partial"
    assert sync.discovered == 3 and sync.expected_total == 5
    assert sync.error_code == "archive_inventory_incomplete"
    # No corpus_hash: an unreadable corpus, not a version-bound one with named gaps.
    assert sync.corpus_hash is None
    card = await receipt(db, sync)
    assert card["corpus_hash"] is None and card["gap_total"] == 0


async def test_a_shrinking_provider_total_leaves_a_readable_superset(corpus):
    """Reading more than the provider now claims exists is drift, not a missed post."""
    db, sync = corpus
    for _ in range(4):
        await persist_page(db, sync, page([post(1), post(2), post(3)], 1, 5, size=3))
        # By the last page the provider says four posts exist: one was unpublished
        # after this pass had already read it, so the stated total shrank under us.
        await persist_page(db, sync, page([post(4), post(5)], 2, 4, size=3))
    assert sync.status == "partial"
    assert sync.discovered == 5 and sync.expected_total == 4
    # A superset is the inventory moving, not a gap in this corpus: it names no
    # gap, it is not called complete, and the receipt carries both counts.
    assert sync.error_code == "archive_changed_during_scan"
    assert len(sync.corpus_hash) == 64
    card = await receipt(db, sync)
    assert card["gaps"] == [] and card["gap_total"] == 0
    assert (card["discovered"], card["expected_total"]) == (5, 4)
    # Every post read stays reachable; a complete scan is not thrown away.
    matches = await search_documents(db, sync, "fan community evidence", limit=10)
    assert sorted(match["post_id"] for match in matches) == [f"{i:024x}" for i in range(1, 6)]


async def test_a_short_paged_archive_is_not_refused_before_it_can_finish(corpus):
    """The non-convergence bound follows the posts actually read, not the best-case page."""
    db, sync = corpus
    # Five thousand single-post pages already checkpointed: what the durable cursor
    # holds when a provider answers a 30,000-post archive one post at a time.
    db.add_all(
        [
            BlogCorpusDocument(
                workspace_id=sync.workspace_id,
                sync_id=sync.id,
                post_id=f"{index:024x}",
                title="Same title",
                content_hash=f"{index:064x}",
                provider_revision=f"{index:064x}",
                seen_pass=sync.pass_number,
            )
            for index in range(1, 5001)
        ]
    )
    sync.next_page = 5001
    await db.flush()
    await persist_page(db, sync, page([post(5001)], 5001, 30000, size=1))
    assert sync.status == "running"
    assert sync.next_page == 5002 and sync.discovered == 5001


async def test_pagination_that_consumes_nothing_new_never_converges(corpus):
    """Pagination still has to terminate: an advancing cursor is not progress."""
    db, sync = corpus
    await persist_page(db, sync, page([post(1), post(2)], 1, 100, size=2))
    with pytest.raises(GhostApiError) as refused:
        for number in range(2, 6):
            # The same two posts, page after page: the cursor moves, the corpus does not.
            await persist_page(db, sync, page([post(1), post(2)], number, 100, size=2))
    assert refused.value.code == "ghost_archive_page_invalid"
    assert sync.discovered == 2


async def test_a_corpus_without_a_version_is_refused_by_search_and_read(
    context, make_connection, workspace
):
    from jhin_connectors.ghost.archive import (
        ArchiveReadInput,
        ArchiveSearchInput,
        _read,
        _search,
    )

    connection = await make_connection(workspace, connector_type="ghost", auth_type="api_key")
    assignment = EditorialAssignment(
        workspace_id=context.workspace_id,
        connection_id=connection.id,
        writer_agent_id=context.agent_id,
        publisher_agent_id=new_uuid7(),
    )
    context.session.add(assignment)
    await context.session.flush()
    sync = BlogCorpusSync(
        workspace_id=context.workspace_id,
        connection_id=connection.id,
        assignment_id=assignment.id,
        agent_id=context.agent_id,
        task_id=context.task_id,
        run_id=context.run_id,
        status="partial",
        corpus_hash=None,
        error_code="archive_inventory_incomplete",
        discovered=3,
        expected_total=5,
    )
    context.session.add(sync)
    await context.session.flush()
    keys = {
        "connection_id": str(connection.id),
        "assignment_id": str(assignment.id),
        "sync_id": str(sync.id),
    }
    for call in (
        _search(context, ArchiveSearchInput(query="fan community", **keys)),
        _read(context, ArchiveReadInput(post_id="a" * 24, **keys)),
    ):
        with pytest.raises(GhostApiError) as refused:
            await call
        assert refused.value.code == "ghost_archive_incomplete"


def _too_large() -> GhostApiError:
    return GhostApiError(
        "Ghost response exceeded the 16 MiB limit. Reduce the requested page size; "
        "no content was truncated.",
        status_code=200,
        code="ghost_response_too_large",
    )


def _reader(weights, bound, seen, total=60):
    async def read(number, limit):
        seen.append((number, limit))
        window = [post(i) for i in range((number - 1) * limit + 1, min(number * limit, total) + 1)]
        if sum(weights.get(int(item["id"], 16), 1) for item in window) > bound:
            raise _too_large()
        return {
            "posts": window,
            "meta": {
                "pagination": {
                    "page": number,
                    "total": total,
                    "next": number + 1 if number * limit < total else None,
                }
            },
        }

    return read


async def test_an_oversized_page_is_halved_and_rejoined_instead_of_failing_the_sync(corpus):
    db, sync = corpus
    seen = []
    # One article heavy enough that neither the full page nor its first half fits.
    read = _reader({27: 96}, 100, seen)
    payload = await fetch_page(read, 2, size=20)
    assert seen == [(2, 20), (3, 10), (5, 5), (6, 5), (4, 10)]
    # The same posts the caller asked for, in order, under the caller's numbering.
    assert [item["id"] for item in payload["posts"]] == [f"{i:024x}" for i in range(21, 41)]
    assert payload["meta"]["pagination"] == {"page": 2, "total": 60, "next": 3}
    # And the rejoined page is an ordinary checkpoint for the durable cursor.
    sync.next_page = 2
    await persist_page(db, sync, payload)
    assert sync.discovered == 20 and sync.next_page == 3 and sync.status == "running"


async def test_one_post_that_will_not_fit_fails_loudly_rather_than_going_missing():
    seen = []
    read = _reader({27: 500}, 100, seen)
    with pytest.raises(GhostApiError) as refused:
        await fetch_page(read, 2, size=20)
    assert refused.value.code == "ghost_response_too_large"
    # Halved to a single post and still refused: no page is ever silently shortened.
    assert (27, 1) in seen
    assert all(limit >= 1 for _number, limit in seen)


async def test_only_an_oversized_response_is_retried_smaller():
    seen = []

    async def read(number, limit):
        seen.append((number, limit))
        raise GhostApiError("Ghost rate limited the request", status_code=429)

    with pytest.raises(GhostApiError) as refused:
        await fetch_page(read, 1, size=20)
    assert refused.value.status_code == 429
    assert seen == [(1, 20)]  # a rate limit is not a size problem


def test_archive_page_size_keeps_a_fourfold_margin_under_the_measured_worst_case():
    # Guards the arithmetic behind ARCHIVE_PAGE_SIZE: 20 posts at the heaviest
    # measured rate (141,892 bytes/post) is 2,837,840 bytes, 17% of the client's
    # 16 MiB bound, where the 100-post page measured 14,189,214 bytes — 85% of it.
    assert ARCHIVE_PAGE_SIZE * 141_892 * 4 < 16 * 1024 * 1024


async def test_a_blog_deleting_through_every_pass_ends_usable_with_named_ids(corpus):
    db, sync = corpus
    for gone in range(4):
        await persist_page(
            db, sync, page([post(index) for index in range(gone + 1, 6)], 1, 5 - gone)
        )
    assert sync.status == "partial"
    assert sync.error_code == "archive_changed_during_scan"
    assert len(sync.corpus_hash) == 64  # a reconciliation bound still yields a readable corpus
    card = await receipt(db, sync)
    assert card["gaps"] == [
        {"post_id": f"{index:024x}", "reason": "deleted_during_scan"} for index in (1, 2, 3)
    ]
    matches = await search_documents(db, sync, "fan community evidence", limit=5)
    assert sorted(match["post_id"] for match in matches) == [f"{4:024x}", f"{5:024x}"]


async def test_partial_coverage_is_searchable_and_readable_but_never_called_complete(
    context, make_connection, workspace
):
    from jhin_connectors.ghost.archive import (
        ArchiveReadInput,
        ArchiveSearchInput,
        _read,
        _search,
    )

    connection = await make_connection(workspace, connector_type="ghost", auth_type="api_key")
    assignment = EditorialAssignment(
        workspace_id=context.workspace_id,
        connection_id=connection.id,
        writer_agent_id=context.agent_id,
        publisher_agent_id=new_uuid7(),
    )
    context.session.add(assignment)
    await context.session.flush()
    sync = BlogCorpusSync(
        workspace_id=context.workspace_id,
        connection_id=connection.id,
        assignment_id=assignment.id,
        agent_id=context.agent_id,
        task_id=context.task_id,
        run_id=context.run_id,
        status="partial",
        corpus_hash="a" * 64,
        discovered=4498,
        indexed=4497,
        failed=1,
    )
    context.session.add(sync)
    await context.session.flush()
    context.session.add_all(
        [
            BlogCorpusDocument(
                workspace_id=context.workspace_id,
                sync_id=sync.id,
                post_id="a" * 24,
                title="Fan community history",
                body_text="Fan community history of the founding roster",
                content_hash="b" * 64,
                provider_revision="c" * 64,
                seen_pass=2,
                complete=True,
            ),
            BlogCorpusDocument(
                workspace_id=context.workspace_id,
                sync_id=sync.id,
                post_id="b" * 24,
                title="Unreadable",
                content_hash="",
                provider_revision="d" * 64,
                seen_pass=2,
                complete=False,
                metadata_json={"skip_reason": "body_missing"},
            ),
        ]
    )
    await context.session.flush()
    keys = {
        "connection_id": str(connection.id),
        "assignment_id": str(assignment.id),
        "sync_id": str(sync.id),
    }
    found = await _search(context, ArchiveSearchInput(query="fan community", **keys))
    assert [match["post_id"] for match in found.matches] == ["a" * 24]
    assert found.coverage_status == "partial"
    assert found.indexed == 4497 and found.failed == 1
    chunk = await _read(context, ArchiveReadInput(post_id="a" * 24, **keys))
    assert chunk.text.startswith("Fan community history")
    with pytest.raises(GhostApiError) as unavailable:
        await _read(context, ArchiveReadInput(post_id="b" * 24, **keys))
    assert unavailable.value.code == "ghost_archive_body_unavailable"


async def test_retry_of_committed_page_does_not_double_count(corpus):
    db, sync = corpus
    first = page([post(i) for i in range(1, ARCHIVE_PAGE_SIZE + 1)], 1, ARCHIVE_PAGE_SIZE + 1)
    await persist_page(db, sync, first)
    await persist_page(db, sync, first)
    assert sync.next_page == 2
    assert sync.discovered == ARCHIVE_PAGE_SIZE


async def test_retried_sync_tool_returns_same_completed_job(context, make_connection, workspace):
    connection = await make_connection(workspace, connector_type="ghost", auth_type="api_key")
    assignment = EditorialAssignment(
        workspace_id=context.workspace_id,
        connection_id=connection.id,
        writer_agent_id=context.agent_id,
        publisher_agent_id=new_uuid7(),
    )
    context.session.add(assignment)
    await context.session.flush()
    data = ArchiveInput(connection_id=str(connection.id), assignment_id=str(assignment.id))
    first = await _sync(context, data)
    row = await context.session.get(BlogCorpusSync, UUID(first.sync_id))
    row.status, row.active_key = "complete", None
    await context.session.flush()
    retry = await _sync(context, data)
    assert retry.sync_id == first.sync_id
    assert retry.status == "complete"


async def test_archive_chunks_survive_gateway_sanitizer_without_losing_body(
    context, make_connection, workspace
):
    from jhin_connectors.ghost.archive import ArchiveReadInput, _read
    from jhin_tools.sanitize import sanitize_payload

    connection = await make_connection(workspace, connector_type="ghost", auth_type="api_key")
    assignment = EditorialAssignment(
        workspace_id=context.workspace_id,
        connection_id=connection.id,
        writer_agent_id=context.agent_id,
        publisher_agent_id=new_uuid7(),
    )
    context.session.add(assignment)
    await context.session.flush()
    sync = BlogCorpusSync(
        workspace_id=context.workspace_id,
        connection_id=connection.id,
        assignment_id=assignment.id,
        agent_id=context.agent_id,
        task_id=context.task_id,
        run_id=context.run_id,
        status="complete",
        corpus_hash="a" * 64,
    )
    context.session.add(sync)
    await context.session.flush()
    body = "研究資料" * 3000
    context.session.add(
        BlogCorpusDocument(
            workspace_id=context.workspace_id,
            sync_id=sync.id,
            post_id="a" * 24,
            title="Archive title",
            body_text=body,
            content_hash="b" * 64,
            provider_revision="c" * 64,
            seen_pass=2,
            complete=True,
        )
    )
    await context.session.flush()
    offset, collected = 0, ""
    while True:
        result = await _read(
            context,
            ArchiveReadInput(
                connection_id=str(connection.id),
                assignment_id=str(assignment.id),
                sync_id=str(sync.id),
                post_id="a" * 24,
                offset=offset,
            ),
        )
        data = result.model_dump(mode="json")
        assert sanitize_payload(data) == data
        collected += result.text
        if data["next_offset"] is None:
            break
        offset = data["next_offset"]
    assert collected == body


async def _editorial(context, make_connection, workspace):
    connection = await make_connection(workspace, connector_type="ghost", auth_type="api_key")
    assignment = EditorialAssignment(
        workspace_id=context.workspace_id,
        connection_id=connection.id,
        writer_agent_id=context.agent_id,
        publisher_agent_id=new_uuid7(),
    )
    context.session.add(assignment)
    await context.session.flush()
    return connection, assignment


def _resume(context):
    """A resumed step: same agent, same run, a new tool call — so a new request key."""
    return replace(context, tool_call_id=new_uuid7())


async def _pages_read(session, workspace_id, posts):
    """Count every page the corpus worker would read for syncs still awaiting a scan.

    The worker only ever advances a sync whose status is queued or running, so
    this is what a sync request costs the user's live blog in provider requests.
    """
    for reads in range(64):
        sync = await session.scalar(
            select(BlogCorpusSync)
            .where(
                BlogCorpusSync.workspace_id == workspace_id,
                BlogCorpusSync.status.in_(("queued", "running")),
            )
            .order_by(BlogCorpusSync.created_at, BlogCorpusSync.id)
        )
        if sync is None:
            return reads
        await persist_page(session, sync, page(posts, sync.next_page, len(posts)))
    raise AssertionError("corpus sync never settled")


async def _syncs_for(session, connection):
    return int(
        await session.scalar(
            select(func.count())
            .select_from(BlogCorpusSync)
            .where(BlogCorpusSync.connection_id == connection.id)
        )
        or 0
    )


async def test_a_resumed_step_reuses_a_fresh_complete_corpus_instead_of_rescanning(
    context, make_connection, workspace
):
    """One live session started eleven scans of one 4,498-post blog; a corpus is an answer."""
    connection, assignment = await _editorial(context, make_connection, workspace)
    data = ArchiveInput(connection_id=str(connection.id), assignment_id=str(assignment.id))
    posts = [post(1), post(2)]
    first = await _sync(context, data)
    assert (first.status, first.reused, first.scan_origin) == ("queued", False, "new_scan")
    assert await _pages_read(context.session, context.workspace_id, posts) == 2
    built = await context.session.get(BlogCorpusSync, UUID(first.sync_id))
    assert built.status == "complete"
    resumed = await _sync(_resume(context), data)
    assert resumed.sync_id == first.sync_id
    assert resumed.status == "complete"
    assert resumed.reused is True and resumed.scan_origin == "reused_complete_corpus"
    assert resumed.corpus_age_seconds is not None
    assert "reused" in resumed.scan_note.lower() and resumed.scan_note != first.scan_note
    # Nothing is left queued or running, so the blog is not read again at all.
    assert await _pages_read(context.session, context.workspace_id, posts) == 0
    assert await _syncs_for(context.session, connection) == 1


async def test_another_assignments_corpus_is_never_borrowed(context, make_connection, workspace):
    """require_research accepts an archive receipt only from a sync bound to the
    assignment under review. Borrowing the neighbouring article's corpus would hand
    back a receipt reading complete, reused, no gaps -- and then be refused at
    review, which is worse than the rescan it saved."""
    connection, assignment = await _editorial(context, make_connection, workspace)
    posts = [post(1), post(2)]
    first = await _sync(
        context, ArchiveInput(connection_id=str(connection.id), assignment_id=str(assignment.id))
    )
    assert await _pages_read(context.session, context.workspace_id, posts) == 2
    built = await context.session.get(BlogCorpusSync, UUID(first.sync_id))
    assert built.status == "complete"

    # A second article on the same blog, moments later: the corpus is fresh and
    # complete, but it belongs to the first assignment.
    second = EditorialAssignment(
        workspace_id=context.workspace_id,
        connection_id=connection.id,
        writer_agent_id=context.agent_id,
        publisher_agent_id=new_uuid7(),
    )
    context.session.add(second)
    await context.session.flush()

    borrowed = await _sync(
        _resume(context),
        ArchiveInput(connection_id=str(connection.id), assignment_id=str(second.id)),
    )

    assert borrowed.sync_id != first.sync_id
    assert borrowed.reused is False and borrowed.scan_origin == "new_scan"


async def test_a_corpus_older_than_the_daily_reconciliation_bound_is_scanned_again(
    context, make_connection, workspace
):
    """Reuse is bounded by the plan's own freshness default, not by convenience."""
    connection, assignment = await _editorial(context, make_connection, workspace)
    data = ArchiveInput(connection_id=str(connection.id), assignment_id=str(assignment.id))
    posts = [post(1), post(2)]
    first = await _sync(context, data)
    await _pages_read(context.session, context.workspace_id, posts)
    built = await context.session.get(BlogCorpusSync, UUID(first.sync_id))
    built.updated_at = (
        datetime.now(UTC).replace(tzinfo=None) - _REUSE_MAX_AGE - timedelta(minutes=1)
    )
    await context.session.flush()
    stale = await _sync(_resume(context), data)
    assert stale.sync_id != first.sync_id
    assert (stale.status, stale.reused, stale.scan_origin) == ("queued", False, "new_scan")
    assert await _pages_read(context.session, context.workspace_id, posts) == 2


async def test_coverage_that_never_settled_is_never_handed_back_as_a_corpus(
    context, make_connection, workspace
):
    """Partial and failed coverage are not originality evidence, so they are not reusable."""
    connection, assignment = await _editorial(context, make_connection, workspace)
    data = ArchiveInput(connection_id=str(connection.id), assignment_id=str(assignment.id))
    unreadable = post(2)
    unreadable.pop("html")
    first = await _sync(context, data)
    assert await _pages_read(context.session, context.workspace_id, [post(1), unreadable]) == 2
    built = await context.session.get(BlogCorpusSync, UUID(first.sync_id))
    assert built.status == "partial" and built.corpus_hash is not None
    after_partial = await _sync(_resume(context), data)
    assert after_partial.sync_id != first.sync_id and after_partial.reused is False
    failed = await context.session.get(BlogCorpusSync, UUID(after_partial.sync_id))
    failed.status, failed.active_key = "failed", None
    failed.corpus_hash, failed.error_code = "f" * 64, "ghost_archive_page_failed"
    await context.session.flush()
    after_failure = await _sync(_resume(context), data)
    assert after_failure.sync_id not in {first.sync_id, after_partial.sync_id}
    assert after_failure.reused is False and after_failure.status == "queued"


async def test_a_forced_refresh_rescans_a_corpus_that_reuse_would_have_kept(
    context, make_connection, workspace
):
    """Checking for newly relevant posts before final review has to be possible."""
    connection, assignment = await _editorial(context, make_connection, workspace)
    keys = {"connection_id": str(connection.id), "assignment_id": str(assignment.id)}
    posts = [post(1), post(2)]
    first = await _sync(context, ArchiveInput(**keys))
    await _pages_read(context.session, context.workspace_id, posts)
    forced = await _sync(_resume(context), ArchiveInput(refresh=True, **keys))
    assert forced.sync_id != first.sync_id
    assert (forced.status, forced.reused, forced.scan_origin) == ("queued", False, "new_scan")
    assert await _pages_read(context.session, context.workspace_id, posts) == 2
    # The forced scan is the current corpus now, and the next resume reuses that one.
    resumed = await _sync(_resume(context), ArchiveInput(**keys))
    assert resumed.sync_id == forced.sync_id and resumed.reused is True


async def test_two_sync_requests_for_one_blog_never_start_two_scans(
    context, make_connection, workspace
):
    """The uniqueness that stops a second scan must not depend on how an id is spelled."""
    connection, assignment = await _editorial(context, make_connection, workspace)
    first = await _sync(
        context,
        ArchiveInput(connection_id=str(connection.id), assignment_id=str(assignment.id)),
    )
    second = await _sync(
        _resume(context),
        ArchiveInput(connection_id=str(connection.id).upper(), assignment_id=str(assignment.id)),
    )
    assert second.sync_id == first.sync_id
    assert second.scan_origin == "attached_running_scan" and second.reused is False
    assert await _syncs_for(context.session, connection) == 1
    assert await _pages_read(context.session, context.workspace_id, [post(1), post(2)]) == 2
