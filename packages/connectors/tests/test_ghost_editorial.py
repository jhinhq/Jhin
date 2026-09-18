"""Versioned review gates and real encrypted credential boundary, isolated DB."""

from dataclasses import replace

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import async_sessionmaker

from jhin_connectors.ghost.assignments import ensure_package
from jhin_connectors.ghost.client import GhostApiError, post_revision
from jhin_connectors.ghost.schemas import (
    DraftCreateInput,
    DraftUpdateInput,
    PostReadInput,
    PublishInput,
    ReviewDecisionInput,
    ReviewReadInput,
    ReviewRequestInput,
)
from jhin_connectors.ghost.tools import (
    _draft_create,
    _draft_update,
    _post_read,
    _publish,
    _review_decide,
    _review_read,
    _review_request,
)
from jhin_db.models import Agent, GhostEditorialReview
from jhin_db.models.editorial import EditorialAssignment
from jhin_domain import new_uuid7

POST_ID = "c" * 24
KEY = "a" * 24 + ":" + "b" * 64


@pytest.fixture
async def editorial(context, workspace, make_connection, monkeypatch):
    publisher = Agent(
        workspace_id=workspace.id,
        name="Marketing Director",
        slug="director",
        role_title="Director",
        status="active",
    )
    author = Agent(
        id=context.agent_id,
        workspace_id=workspace.id,
        name="Blogger",
        slug="blogger",
        role_title="Writer",
        status="active",
    )
    context.session.add_all([publisher, author])
    await context.session.flush()
    monkeypatch.setenv("JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS", "http://ghost:2368")
    connection = await make_connection(
        workspace,
        connector_type="ghost",
        auth_type="api_key",
        credentials={"admin_key": KEY},
        config={"admin_url": "http://ghost:2368", "publisher_agent_id": str(publisher.id)},
    )
    assignment = EditorialAssignment(
        workspace_id=context.workspace_id,
        connection_id=connection.id,
        writer_agent_id=context.agent_id,
        publisher_agent_id=publisher.id,
        post_id=POST_ID,
        release_intent="publish_after_ashley_review",
    )
    context.session.add(assignment)
    await context.session.flush()
    connection._test_assignment = assignment
    post = {
        "id": POST_ID,
        "title": "A useful article",
        "slug": "useful-article",
        "html": "<p>Evidence</p>",
        "lexical": "{}",
        "status": "draft",
        "updated_at": "2026-09-12T12:00:00.000Z",
    }
    calls = []

    async def request(base, key, method, path, *, body=None, params=None):
        assert base == "http://ghost:2368" and key == KEY
        calls.append((method, path, body))
        if method == "PUT":
            assert body["posts"][0]["updated_at"] == post["updated_at"]
            post.update(body["posts"][0])
            post["updated_at"] = "2026-09-12T12:01:00.000Z"
        if method == "GET" and path == "posts/":
            return {"posts": []}
        if method == "POST":
            assert body["posts"][0]["status"] == "draft"
            post.update(body["posts"][0])
        return {"posts": [dict(post)]}

    monkeypatch.setattr("jhin_connectors.ghost.tools.ghost_request", request)
    return context, connection, publisher, post, calls


async def test_draft_create_explicitly_cannot_publish(editorial):
    ctx, conn, _, _, calls = editorial
    conn._test_assignment.post_id = None
    await ctx.session.flush()
    result = await _draft_create(
        ctx,
        DraftCreateInput(
            assignment_id=str(conn._test_assignment.id),
            expected_editorial_version=1,
            connection_id=str(conn.id),
            title="Article",
            slug="article",
            html="<p>Article</p>",
        ),
    )
    assert result.status == "draft"
    assert (
        next(body for method, _, body in calls if method == "POST")["posts"][0]["status"] == "draft"
    )


async def test_draft_update_rejects_published_post_and_stale_version(editorial):
    ctx, conn, _, post, calls = editorial
    data = DraftUpdateInput(
        assignment_id=str(conn._test_assignment.id),
        expected_editorial_version=1,
        connection_id=str(conn.id),
        title="Change",
        slug="article",
        html="<p>Change</p>",
        post_id=POST_ID,
        expected_updated_at="old",
    )
    with pytest.raises(GhostApiError, match="latest revision"):
        await _draft_update(ctx, data)
    post["status"] = "published"
    with pytest.raises(GhostApiError, match="drafts only"):
        await _draft_update(ctx, data)
    assert all(method == "GET" for method, _, _ in calls)


async def make_review(editorial):
    ctx, conn, publisher, post, _ = editorial
    assignment = conn._test_assignment
    package = await ensure_package(ctx, assignment, post_revision(post), "http://ghost:2368")
    row = GhostEditorialReview(
        workspace_id=ctx.workspace_id,
        connection_id=conn.id,
        post_id=POST_ID,
        revision=post_revision(post),
        admin_url="http://ghost:2368",
        provider_updated_at=post["updated_at"],
        snapshot_json=dict(post),
        author_agent_id=ctx.agent_id,
        publisher_agent_id=publisher.id,
        assignment_id=assignment.id,
        package_id=package.id,
        assignment_editorial_version=assignment.editorial_version,
        release_intent=assignment.release_intent,
    )
    ctx.session.add(row)
    await ctx.session.commit()
    return row


async def test_blogger_cannot_review_or_publish_even_with_tool_access(editorial):
    ctx, conn, _, _, calls = editorial
    row = await make_review(editorial)
    with pytest.raises(GhostApiError, match="Only the designated"):
        await _review_decide(
            ctx,
            ReviewDecisionInput(
                connection_id=str(conn.id),
                review_id=str(row.id),
                verdict="approved",
                feedback="Good",
            ),
        )
    row.status = "approved"
    with pytest.raises(GhostApiError, match="Only the designated"):
        await _publish(ctx, PublishInput(connection_id=str(conn.id), review_id=str(row.id)))
    assert calls == []


async def test_director_review_rejects_out_of_band_edit(editorial):
    ctx, conn, publisher, post, calls = editorial
    row = await make_review(editorial)
    post["html"] = "Changed even with same timestamp"
    director = replace(ctx, agent_id=publisher.id)
    with pytest.raises(GhostApiError, match="draft changed"):
        await _review_decide(
            director,
            ReviewDecisionInput(
                connection_id=str(conn.id),
                review_id=str(row.id),
                verdict="approved",
                feedback="Good",
            ),
        )
    assert row.status == "stale"
    assert all(method == "GET" for method, _, _ in calls)


async def test_approved_revision_publishes_once_under_director_identity(editorial):
    ctx, conn, publisher, _, calls = editorial
    row = await make_review(editorial)
    director = replace(
        ctx,
        agent_id=publisher.id,
        tool_call_id=new_uuid7(),
        session_factory=async_sessionmaker(ctx.session.bind, expire_on_commit=False),
    )
    await _post_read(director, PostReadInput(connection_id=str(conn.id), post_id=POST_ID))
    await _review_read(director, ReviewReadInput(connection_id=str(conn.id), review_id=str(row.id)))
    result = await _review_decide(
        director,
        ReviewDecisionInput(
            connection_id=str(conn.id),
            review_id=str(row.id),
            verdict="approved",
            feedback="Sources checked; meets brief",
        ),
    )
    assert result.status == "approved"
    await ctx.session.commit()
    result = await _publish(
        director, PublishInput(connection_id=str(conn.id), review_id=str(row.id))
    )
    assert result.status == "published" and row.status == "published"
    assert row.publication_tool_call_id == director.tool_call_id
    with pytest.raises(GhostApiError, match="unused review"):
        await _publish(director, PublishInput(connection_id=str(conn.id), review_id=str(row.id)))
    assert sum(method == "PUT" for method, _, _ in calls) == 1


async def test_uncertain_send_does_not_reopen_approval(editorial, monkeypatch):
    ctx, conn, publisher, post, calls = editorial
    row = await make_review(editorial)
    row.status = "approved"
    await ctx.session.commit()
    director = replace(
        ctx,
        agent_id=publisher.id,
        tool_call_id=new_uuid7(),
        session_factory=async_sessionmaker(ctx.session.bind, expire_on_commit=False),
    )

    async def request(base, key, method, path, **kw):
        calls.append((method, path, kw))
        if method == "PUT":
            raise GhostApiError("Connection lost after send", mutation=True)
        return {"posts": [dict(post)]}

    monkeypatch.setattr("jhin_connectors.ghost.tools.ghost_request", request)
    with pytest.raises(GhostApiError, match="Connection lost"):
        await _publish(director, PublishInput(connection_id=str(conn.id), review_id=str(row.id)))
    await ctx.session.refresh(row)
    assert row.status == "uncertain"
    with pytest.raises(GhostApiError, match="unused review"):
        await _publish(
            replace(director, tool_call_id=new_uuid7()),
            PublishInput(connection_id=str(conn.id), review_id=str(row.id)),
        )
    assert sum(method == "PUT" for method, _, _ in calls) == 1


async def test_review_request_persists_handoff_to_director_only(editorial):
    ctx, conn, publisher, post, _ = editorial
    result = await _review_request(
        ctx,
        ReviewRequestInput(
            assignment_id=str(conn._test_assignment.id),
            expected_editorial_version=1,
            connection_id=str(conn.id),
            post_id=POST_ID,
            expected_revision=post_revision(post),
            summary="Please check source claims and publish if ready.",
        ),
    )
    assert result.publisher_agent_id == str(publisher.id)
    assert result.work_request_id and result.created_task_id
    assert result.agent_id == str(publisher.id)
    again = await _review_request(
        ctx,
        ReviewRequestInput(
            assignment_id=str(conn._test_assignment.id),
            expected_editorial_version=1,
            connection_id=str(conn.id),
            post_id=POST_ID,
            expected_revision=post_revision(post),
            summary="Please check source claims and publish if ready.",
        ),
    )
    assert again.review_id == result.review_id
    assert again.created_task_id == result.created_task_id


async def test_unbound_draft_write_is_denied_before_provider(editorial):
    ctx, conn, _, _, calls = editorial
    with pytest.raises((GhostApiError, ValueError), match="assignment"):
        await _draft_create(
            ctx,
            DraftCreateInput(
                connection_id=str(conn.id), title="Unsafe", slug="unsafe", html="<p>Unsafe</p>"
            ),
        )
    assert calls == []


async def test_draft_only_review_cannot_publish(editorial):
    ctx, conn, publisher, _, calls = editorial
    row = await make_review(editorial)
    row.status = "approved"
    row.release_intent = "draft_only"
    director = replace(ctx, agent_id=publisher.id)
    with pytest.raises(GhostApiError, match=r"draft.only|assignment"):
        await _publish(director, PublishInput(connection_id=str(conn.id), review_id=str(row.id)))
    assert calls == []


def test_post_output_does_not_silently_truncate_review_content():
    from jhin_connectors.ghost.tools import _post_output

    html = "<p>" + "x" * 70_000 + "</p>"
    result = _post_output({"id": POST_ID, "updated_at": "now", "html": html})
    assert result.html == html


async def test_conflicting_installation_publisher_is_denied(editorial, make_connection, workspace):
    from jhin_connectors.ghost.tools import _publisher

    ctx, conn, _, _, calls = editorial
    await make_connection(
        workspace,
        connector_type="ghost",
        name="Conflicting blog",
        auth_type="api_key",
        config={"admin_url": "http://ghost:2368/ghost", "publisher_agent_id": str(ctx.agent_id)},
    )
    with pytest.raises(GhostApiError, match="conflict"):
        await _publisher(ctx, conn)
    assert calls == []


async def test_draft_only_handoff_cannot_turn_author_request_into_release(editorial):
    from jhin_db.models import WorkRequest

    ctx, conn, _, post, calls = editorial
    conn._test_assignment.release_intent = "draft_only"
    result = await _review_request(
        ctx,
        ReviewRequestInput(
            connection_id=str(conn.id),
            assignment_id=str(conn._test_assignment.id),
            expected_editorial_version=1,
            post_id=POST_ID,
            expected_revision=post_revision(post),
            summary="Publish it now!",
        ),
    )
    request = await ctx.session.get(WorkRequest, __import__("uuid").UUID(result.work_request_id))
    assert "keep the post as an approved draft" in request.description
    assert result.release_intent == "draft_only"
    assert request.metadata_json["editorial_assignment_id"] == str(conn._test_assignment.id)
    assert all(method == "GET" for method, _, _ in calls)


@pytest.mark.parametrize("phase,post_id", [("cancelled", POST_ID), ("draft", "d" * 24)])
async def test_cancelled_and_cross_assignment_writes_make_zero_requests(editorial, phase, post_id):
    ctx, conn, _, post, calls = editorial
    conn._test_assignment.phase = phase
    with pytest.raises(GhostApiError, match=r"cancelled|not owned"):
        await _draft_update(
            ctx,
            DraftUpdateInput(
                connection_id=str(conn.id),
                assignment_id=str(conn._test_assignment.id),
                expected_editorial_version=1,
                post_id=post_id,
                expected_updated_at=post["updated_at"],
                title="Changed",
                slug="changed",
                html="<p>Changed</p>",
            ),
        )
    assert calls == []


async def test_director_must_read_every_chunk_of_actual_revision(editorial):
    ctx, conn, publisher, post, _ = editorial
    post["html"] = "x" * 12_001
    post["feature_image_caption"] = "Photo credit"
    row = await make_review(editorial)
    director = replace(ctx, agent_id=publisher.id)
    decision = ReviewDecisionInput(
        connection_id=str(conn.id),
        review_id=str(row.id),
        verdict="approved",
        feedback="Read all sections",
    )
    first = await _post_read(director, PostReadInput(connection_id=str(conn.id), post_id=POST_ID))
    assert not first.complete and first.next_offset == 6000
    assert first.html_total_chars == 12_001 and first.feature_image_caption == "Photo credit"
    with pytest.raises(GhostApiError, match="every article chunk"):
        await _review_decide(director, decision)
    for offset in (6000, 12_000):
        await _post_read(
            director, PostReadInput(connection_id=str(conn.id), post_id=POST_ID, offset=offset)
        )
    await _review_read(director, ReviewReadInput(connection_id=str(conn.id), review_id=str(row.id)))
    assert (await _review_decide(director, decision)).status == "approved"


@pytest.mark.parametrize("change", ["brief", "intent", "cancelled", "publisher"])
async def test_changed_assignment_denies_publication_before_provider(editorial, change):
    ctx, conn, publisher, _, calls = editorial
    row = await make_review(editorial)
    row.status = "approved"
    assignment = conn._test_assignment
    if change == "brief":
        assignment.brief_json = {"angle": "new angle"}
    elif change == "intent":
        assignment.release_intent = "draft_only"
    elif change == "cancelled":
        assignment.phase = "cancelled"
    else:
        assignment.publisher_agent_id = ctx.agent_id
    with pytest.raises(GhostApiError):
        await _publish(
            replace(ctx, agent_id=publisher.id),
            PublishInput(connection_id=str(conn.id), review_id=str(row.id)),
        )
    assert calls == []


async def test_progress_does_not_invalidate_package(editorial):
    from jhin_connectors.ghost.tools import _check_assignment_review

    ctx, conn, publisher, _, _ = editorial
    row = await make_review(editorial)
    conn._test_assignment.phase = "awaiting_review"
    conn._test_assignment.version += 1
    await _check_assignment_review(replace(ctx, agent_id=publisher.id), row)


async def test_article_read_without_package_read_cannot_approve(editorial):
    ctx, conn, publisher, _, _ = editorial
    row = await make_review(editorial)
    director = replace(ctx, agent_id=publisher.id)
    await _post_read(director, PostReadInput(connection_id=str(conn.id), post_id=POST_ID))
    with pytest.raises(GhostApiError, match="package"):
        await _review_decide(
            director,
            ReviewDecisionInput(
                connection_id=str(conn.id),
                review_id=str(row.id),
                verdict="approved",
                feedback="Only read article",
            ),
        )


PHOTO = {
    "photo_id": "photo1",
    "image_url": "https://images.unsplash.com/photo-1?ixid=abc&w=1080&q=80",
    "photo_url": "https://unsplash.com/photos/photo1?utm_source=jhin&utm_medium=referral",
    "download_location": "https://api.unsplash.com/photos/photo1/download",
    "photographer": "A Photographer",
    "photographer_url": "https://unsplash.com/@ap?utm_source=jhin&utm_medium=referral",
    "attribution_html": (
        'Photo by <a href="https://unsplash.com/@ap?utm_source=jhin&amp;utm_medium=referral">'
        "A Photographer</a> on "
        '<a href="https://unsplash.com/?utm_source=jhin&amp;utm_medium=referral">Unsplash</a>'
    ),
    "suggested_alt": "A desk with a laptop",
    "width": 1080,
    "height": 720,
}


@pytest.fixture
def selected_photo(context, monkeypatch):
    from datetime import UTC, datetime

    from jhin_db.models.editorial_assets import EditorialAsset

    monkeypatch.setenv("JHIN_CONNECTOR_SKIP_DNS_CHECK", "1")

    async def make(assignment, *, status="confirmed", metadata=None):
        asset = EditorialAsset(
            workspace_id=context.workspace_id,
            assignment_id=assignment.id,
            connection_id=assignment.connection_id,
            question_id=new_uuid7(),
            photo_id="photo1",
            selected_by_user_id=new_uuid7(),
            selected_at=datetime.now(UTC),
            status=status,
            metadata_json=dict(metadata or PHOTO),
        )
        context.session.add(asset)
        await context.session.flush()
        return asset

    return make


async def test_post_list_does_not_pull_whole_article_bodies(editorial, monkeypatch):
    from jhin_connectors.ghost.schemas import PostListInput
    from jhin_connectors.ghost.tools import _post_list

    ctx, conn, _, post, _ = editorial
    seen: dict = {}

    async def request(base, key, method, path, *, body=None, params=None):
        seen.update(params or {})
        return {"posts": [dict(post)], "meta": {"pagination": {"next": None}}}

    monkeypatch.setattr("jhin_connectors.ghost.tools.ghost_request", request)
    result = await _post_list(ctx, PostListInput(connection_id=str(conn.id), limit=30))
    requested = f"{seen.get('formats', '')},{seen.get('fields', '')}"
    assert "lexical" not in requested
    assert "html" not in requested
    assert seen["limit"] <= 10
    assert {"id", "title", "slug", "status", "updated_at"} <= set(
        str(seen.get("fields", "")).split(",")
    )
    assert result.posts[0].title == "A useful article"


async def test_draft_create_attaches_the_chosen_cover_with_a_visible_credit(
    editorial, selected_photo
):
    ctx, conn, _, _, calls = editorial
    assignment = conn._test_assignment
    assignment.post_id = None
    await selected_photo(assignment)
    result = await _draft_create(
        ctx,
        DraftCreateInput(
            assignment_id=str(assignment.id),
            expected_editorial_version=assignment.editorial_version,
            connection_id=str(conn.id),
            title="Article",
            slug="article",
            html="<p>Article</p>",
        ),
    )
    body = next(body for method, _, body in calls if method == "POST")["posts"][0]
    assert body["feature_image"] == PHOTO["image_url"]
    assert body["feature_image_alt"] == PHOTO["suggested_alt"]
    assert PHOTO["attribution_html"] in body["feature_image_caption"]
    # Ghost themes do not reliably render the caption; a reader must still see it.
    assert PHOTO["attribution_html"] in body["html"]
    assert result.feature_image == PHOTO["image_url"]


async def test_draft_update_carries_the_cover_and_refuses_a_substitute(editorial, selected_photo):
    ctx, conn, _, post, calls = editorial
    assignment = conn._test_assignment
    await selected_photo(assignment)
    fields = {
        "assignment_id": str(assignment.id),
        "expected_editorial_version": assignment.editorial_version,
        "connection_id": str(conn.id),
        "post_id": POST_ID,
        "expected_updated_at": post["updated_at"],
        "title": "Article",
        "slug": "article",
        "html": "<p>Article</p>",
    }
    with pytest.raises(GhostApiError, match="chosen by a person"):
        await _draft_update(
            ctx,
            DraftUpdateInput(**fields, feature_image="https://images.unsplash.com/other"),
        )
    assert all(method == "GET" for method, _, _ in calls)
    result = await _draft_update(ctx, DraftUpdateInput(**fields))
    sent = next(body for method, _, body in calls if method == "PUT")["posts"][0]
    assert sent["feature_image"] == PHOTO["image_url"]
    assert PHOTO["attribution_html"] in sent["feature_image_caption"]
    assert PHOTO["attribution_html"] in sent["html"]
    assert result.feature_image_alt == PHOTO["suggested_alt"]


async def test_photo_without_a_description_asks_the_writer_for_alt_text(editorial, selected_photo):
    ctx, conn, _, post, calls = editorial
    assignment = conn._test_assignment
    await selected_photo(assignment, metadata={**PHOTO, "suggested_alt": ""})
    fields = {
        "assignment_id": str(assignment.id),
        "expected_editorial_version": assignment.editorial_version,
        "connection_id": str(conn.id),
        "post_id": POST_ID,
        "expected_updated_at": post["updated_at"],
        "title": "Article",
        "slug": "article",
        "html": "<p>Article</p>",
    }
    with pytest.raises(GhostApiError, match="no description"):
        await _draft_update(ctx, DraftUpdateInput(**fields))
    assert all(method == "GET" for method, _, _ in calls)
    result = await _draft_update(
        ctx, DraftUpdateInput(**fields, feature_image_alt="Chosen by the editor")
    )
    assert result.feature_image_alt == "Chosen by the editor"
    assert result.feature_image == PHOTO["image_url"]


async def test_listing_row_never_claims_to_be_a_complete_article(editorial, monkeypatch):
    """A row whose body was never fetched must not read as an empty finished article."""
    from jhin_connectors.ghost.schemas import PostListInput
    from jhin_connectors.ghost.tools import _post_list

    ctx, conn, _, post, _ = editorial

    async def request(base, key, method, path, *, body=None, params=None):
        # Ghost answers the listing's `fields` request, so no body comes back.
        listed = {name: post[name] for name in ("id", "title", "slug", "status", "updated_at")}
        return {"posts": [listed], "meta": {"pagination": {"next": None}}}

    monkeypatch.setattr("jhin_connectors.ghost.tools.ghost_request", request)
    result = await _post_list(ctx, PostListInput(connection_id=str(conn.id)))
    row = result.posts[0]
    assert row.html == ""
    # "complete" would assert that an empty body is the whole article.
    assert row.complete is False
    assert row.next_offset == 0
    assert "ghost.post.read" in " ".join(str(value) for value in row.metadata.values())


async def test_listing_row_hands_back_no_revision_it_cannot_honour(editorial, monkeypatch):
    """A listing hash could never equal the revision the same post reads back with."""
    from jhin_connectors.ghost.schemas import PostListInput
    from jhin_connectors.ghost.tools import _post_list

    ctx, conn, _, post, _ = editorial

    async def request(base, key, method, path, *, body=None, params=None):
        listed = {name: post[name] for name in ("id", "title", "slug", "status", "updated_at")}
        return {"posts": [listed], "meta": {"pagination": {"next": None}}}

    monkeypatch.setattr("jhin_connectors.ghost.tools.ghost_request", request)
    row = (await _post_list(ctx, PostListInput(connection_id=str(conn.id)))).posts[0]
    assert row.revision == ""
    assert row.revision != post_revision(post)
    with pytest.raises(ValidationError):
        ReviewRequestInput(
            connection_id=str(conn.id),
            assignment_id=str(conn._test_assignment.id),
            expected_editorial_version=1,
            post_id=POST_ID,
            expected_revision=row.revision,
            summary="Using the listing revision",
        )


def test_post_list_states_the_page_size_it_actually_delivers():
    from jhin_connectors.ghost.tools import _LIST_PAGE_SIZE, GHOST_TOOLS

    definition = next(item for item, _ in GHOST_TOOLS if item.name == "ghost.post.list")
    described = definition.description.lower()
    assert str(_LIST_PAGE_SIZE) in described
    assert "no article body" in described


async def test_required_credit_cannot_push_the_body_past_its_declared_bound(
    editorial, selected_photo
):
    ctx, conn, _, post, calls = editorial
    assignment = conn._test_assignment
    await selected_photo(assignment)
    with pytest.raises(GhostApiError, match="character limit"):
        await _draft_update(
            ctx,
            DraftUpdateInput(
                assignment_id=str(assignment.id),
                expected_editorial_version=assignment.editorial_version,
                connection_id=str(conn.id),
                post_id=POST_ID,
                expected_updated_at=post["updated_at"],
                title="Article",
                slug="article",
                html="<p>" + "x" * 99_993 + "</p>",
            ),
        )
    assert all(method == "GET" for method, _, _ in calls)


async def test_rerendered_credit_does_not_collect_a_second_credit(editorial, selected_photo):
    """Ghost round-trips the body through lexical; the credit comes back re-rendered."""
    ctx, conn, _, post, calls = editorial
    assignment = conn._test_assignment
    await selected_photo(assignment)
    rendered = (
        '<p>Photo by <a href="https://unsplash.com/@ap?utm_source=jhin&amp;utm_medium=referral" '
        'rel="noopener noreferrer">A Photographer</a> on '
        '<a href="https://unsplash.com/?utm_source=jhin&amp;utm_medium=referral" '
        'rel="noopener noreferrer">Unsplash</a></p>'
    )
    assert PHOTO["attribution_html"] not in rendered
    await _draft_update(
        ctx,
        DraftUpdateInput(
            assignment_id=str(assignment.id),
            expected_editorial_version=assignment.editorial_version,
            connection_id=str(conn.id),
            post_id=POST_ID,
            expected_updated_at=post["updated_at"],
            title="Article",
            slug="article",
            html=f"<p>Article</p>\n{rendered}",
        ),
    )
    sent = next(body for method, _, body in calls if method == "PUT")["posts"][0]
    assert sent["html"].count("https://unsplash.com/@ap") == 1


async def test_required_credit_cannot_push_the_caption_past_its_declared_bound(
    editorial, selected_photo
):
    ctx, conn, _, post, calls = editorial
    assignment = conn._test_assignment
    await selected_photo(assignment)
    with pytest.raises(GhostApiError, match="character limit"):
        await _draft_update(
            ctx,
            DraftUpdateInput(
                assignment_id=str(assignment.id),
                expected_editorial_version=assignment.editorial_version,
                connection_id=str(conn.id),
                post_id=POST_ID,
                expected_updated_at=post["updated_at"],
                title="Article",
                slug="article",
                html="<p>Article</p>",
                feature_image_caption="y" * 990,
            ),
        )
    assert all(method == "GET" for method, _, _ in calls)


async def test_prose_that_links_the_photographer_still_carries_its_own_credit(
    editorial, selected_photo
):
    """A writer may cite the photographer; that is not Jhin's required credit block."""
    ctx, conn, _, post, calls = editorial
    assignment = conn._test_assignment
    await selected_photo(assignment)
    prose = (
        '<p>As <a href="https://unsplash.com/@ap?utm_source=jhin&amp;utm_medium=referral">'
        "A Photographer</a> explained to us, the desk matters.</p>"
    )
    await _draft_update(
        ctx,
        DraftUpdateInput(
            assignment_id=str(assignment.id),
            expected_editorial_version=assignment.editorial_version,
            connection_id=str(conn.id),
            post_id=POST_ID,
            expected_updated_at=post["updated_at"],
            title="Article",
            slug="article",
            html=f"<p>Article</p>\n{prose}",
        ),
    )
    sent = next(body for method, _, body in calls if method == "PUT")["posts"][0]
    assert PHOTO["attribution_html"] in sent["html"]


async def test_a_newer_stuck_selection_is_not_covered_by_the_previous_photo(
    editorial, selected_photo
):
    """A person's latest pick is the choice that counts, even while its tracking is stuck."""
    from datetime import timedelta

    ctx, conn, _, post, calls = editorial
    assignment = conn._test_assignment
    superseded = await selected_photo(assignment)
    repick = await selected_photo(assignment, status="tracking")
    repick.selected_at = superseded.selected_at + timedelta(minutes=5)
    await ctx.session.flush()
    await _draft_update(
        ctx,
        DraftUpdateInput(
            assignment_id=str(assignment.id),
            expected_editorial_version=assignment.editorial_version,
            connection_id=str(conn.id),
            post_id=POST_ID,
            expected_updated_at=post["updated_at"],
            title="Article",
            slug="article",
            html="<p>Article</p>",
        ),
    )
    sent = next(body for method, _, body in calls if method == "PUT")["posts"][0]
    assert PHOTO["image_url"] not in str(sent)
    with pytest.raises(GhostApiError, match="tracking receipt"):
        await _review_request(
            ctx,
            ReviewRequestInput(
                connection_id=str(conn.id),
                assignment_id=str(assignment.id),
                expected_editorial_version=assignment.editorial_version,
                post_id=POST_ID,
                expected_revision=post_revision(post),
                summary="Ready for review while the new cover is stuck.",
            ),
        )


async def test_oversize_listing_page_is_refused_rather_than_silently_truncated(
    editorial, monkeypatch
):
    """The listing must not attest a page the gateway sanitizer would cut down."""
    from jhin_connectors.ghost.schemas import PostListInput
    from jhin_connectors.ghost.tools import _post_list

    ctx, conn, _, post, _ = editorial

    async def request(base, key, method, path, *, body=None, params=None):
        listed = {
            **{name: post[name] for name in ("id", "slug", "status", "updated_at")},
            "title": "x" * 5_000,
        }
        return {"posts": [dict(listed) for _ in range(10)], "meta": {"pagination": {"next": 2}}}

    monkeypatch.setattr("jhin_connectors.ghost.tools.ghost_request", request)
    with pytest.raises(GhostApiError, match="delivery limit"):
        await _post_list(ctx, PostListInput(connection_id=str(conn.id)))


async def test_unconfirmed_tracking_blocks_the_cover_and_review_not_the_prose(
    editorial, selected_photo
):
    """A stuck reservation withholds the cover and the review, but a draft can still be saved."""
    ctx, conn, _, post, calls = editorial
    assignment = conn._test_assignment
    await selected_photo(assignment, status="tracking")
    fields = {
        "assignment_id": str(assignment.id),
        "expected_editorial_version": assignment.editorial_version,
        "connection_id": str(conn.id),
        "post_id": POST_ID,
        "expected_updated_at": post["updated_at"],
        "title": "Article",
        "slug": "article",
        "html": "<p>Article</p>",
    }
    with pytest.raises(GhostApiError, match="tracking receipt"):
        await _draft_update(
            ctx, DraftUpdateInput(**fields, feature_image="https://images.unsplash.com/other")
        )
    assert all(method == "GET" for method, _, _ in calls)
    result = await _draft_update(ctx, DraftUpdateInput(**fields))
    sent = next(body for method, _, body in calls if method == "PUT")["posts"][0]
    assert "feature_image" not in sent
    assert PHOTO["image_url"] not in str(sent)
    assert result.status == "draft"
    with pytest.raises(GhostApiError, match="tracking receipt"):
        await _review_request(
            ctx,
            ReviewRequestInput(
                connection_id=str(conn.id),
                assignment_id=str(assignment.id),
                expected_editorial_version=assignment.editorial_version,
                post_id=POST_ID,
                expected_revision=post_revision(post),
                summary="Ready for review without the chosen cover.",
            ),
        )
