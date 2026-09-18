"""Real Ghost 6 provider contract; exact disposable loopback destination only.

This is a connector integration rehearsal, not generated-agent/editorial-research
acceptance. Identity, assignments and review state use the isolated DB fixtures.
No production URL or credential is accepted by this test.
"""

import os
from dataclasses import replace

import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from jhin_connectors.ghost.client import GhostApiError, ghost_request, post_path
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
from jhin_db.models import Agent
from jhin_db.models.editorial import EditorialAssignment
from jhin_domain import new_uuid7

URL = os.environ.get("SHOWCASE_GHOST_URL", "")
pytestmark = pytest.mark.skipif(not URL, reason="disposable SHOWCASE_GHOST_URL required")


async def isolated_admin_key() -> str:
    if URL != "http://127.0.0.1:2369":
        raise RuntimeError("Only the reserved disposable Ghost fixture is permitted")
    # Synthetic fixture-only credentials; never used on a real site or emitted.
    email, password = "jhin-fixture@example.invalid", "Fixture-Only-Not-A-Real-Account-2026!"
    async with httpx.AsyncClient(
        base_url=URL, headers={"Origin": "http://localhost:2369"}, follow_redirects=False
    ) as client:
        setup = await client.get("/ghost/api/admin/authentication/setup/")
        assert setup.status_code == 200
        if not setup.json()["setup"][0]["status"]:
            created = await client.post(
                "/ghost/api/admin/authentication/setup/",
                json={
                    "setup": [
                        {
                            "name": "Isolated Owner",
                            "email": email,
                            "password": password,
                            "blogTitle": "Disposable Jhin acceptance",
                        }
                    ]
                },
            )
            assert created.status_code in {200, 201}
        session = await client.post(
            "/ghost/api/admin/session/", json={"username": email, "password": password}
        )
        assert session.status_code in {200, 201}, (
            session.json().get("errors", [{}])[0].get("message")
        )
        # Revoke keys left by an interrupted fixture run. This exact disposable
        # installation is reserved for these tests; no post is deleted.
        previous = await client.get("/ghost/api/admin/integrations/")
        assert previous.status_code == 200
        for item in previous.json()["integrations"]:
            if item["name"].startswith("Jhin isolated "):
                removed = await client.delete(f"/ghost/api/admin/integrations/{item['id']}/")
                assert removed.status_code == 204
        integration = await client.post(
            "/ghost/api/admin/integrations/",
            json={
                "integrations": [
                    {
                        "name": f"Jhin isolated {new_uuid7().hex[:12]}",
                    }
                ]
            },
        )
        assert integration.status_code in {200, 201}, (
            integration.json().get("errors", [{}])[0].get("message")
        )
        keys = integration.json()["integrations"][0]["api_keys"]
        admin = next(key for key in keys if key["type"] == "admin")
        return admin["secret"] if ":" in admin["secret"] else f"{admin['id']}:{admin['secret']}"


async def test_real_ghost_draft_revision_review_and_designated_publication(
    context, workspace, make_connection, monkeypatch
):
    key = await isolated_admin_key()
    monkeypatch.setenv("JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS", URL)
    writer = Agent(
        id=context.agent_id, workspace_id=workspace.id, name="Mindy fixture", slug="writer"
    )
    director = Agent(workspace_id=workspace.id, name="Ashley fixture", slug="director")
    context.session.add_all([writer, director])
    await context.session.flush()
    conn = await make_connection(
        workspace,
        connector_type="ghost",
        auth_type="api_key",
        credentials={"admin_key": key},
        config={"admin_url": URL, "publisher_agent_id": str(director.id)},
    )
    assignment = EditorialAssignment(
        workspace_id=workspace.id,
        connection_id=conn.id,
        writer_agent_id=writer.id,
        publisher_agent_id=director.id,
    )
    context.session.add(assignment)
    await context.session.commit()
    director_ctx = replace(
        context,
        agent_id=director.id,
        tool_call_id=new_uuid7(),
        session_factory=async_sessionmaker(context.session.bind, expire_on_commit=False),
    )
    slug = f"jhin-isolated-{new_uuid7().hex}"
    draft = await _draft_create(
        context,
        DraftCreateInput(
            connection_id=str(conn.id),
            assignment_id=str(assignment.id),
            expected_editorial_version=1,
            title="Isolated connector review fixture",
            slug=slug,
            html="<p>Initial fixture draft.</p>",
        ),
    )
    assert draft.status == "draft" and "Initial fixture draft" in draft.html

    async def request_review(post):
        return await _review_request(
            context,
            ReviewRequestInput(
                connection_id=str(conn.id),
                assignment_id=str(assignment.id),
                expected_editorial_version=assignment.editorial_version,
                post_id=post.post_id,
                expected_revision=post.revision,
                summary="Review isolated fixture",
            ),
        )

    async def read_review(review, post):
        await _post_read(
            director_ctx, PostReadInput(connection_id=str(conn.id), post_id=post.post_id)
        )
        offset = 0
        while True:
            page = await _review_read(
                director_ctx,
                ReviewReadInput(
                    connection_id=str(conn.id), review_id=review.review_id, offset=offset
                ),
            )
            if page.next_offset is None:
                break
            offset = page.next_offset

    review = await request_review(draft)
    await read_review(review, draft)
    changed = await _review_decide(
        director_ctx,
        ReviewDecisionInput(
            connection_id=str(conn.id),
            review_id=review.review_id,
            verdict="changes_requested",
            feedback="ISSUE-1: Add the fixture conclusion.",
        ),
    )
    assert changed.status == "changes_requested"
    revised = await _draft_update(
        context,
        DraftUpdateInput(
            connection_id=str(conn.id),
            assignment_id=str(assignment.id),
            expected_editorial_version=assignment.editorial_version,
            post_id=draft.post_id,
            expected_updated_at=draft.updated_at,
            title=draft.title,
            slug=slug,
            html="<p>Initial fixture draft.</p><p>Fixture conclusion.</p>",
            meta_title="Verified isolated metadata",
            meta_description="Real Ghost read-back",
        ),
    )
    assert revised.status == "draft" and revised.meta_title == "Verified isolated metadata"
    review = await request_review(revised)
    await read_review(review, revised)
    await _review_decide(
        director_ctx,
        ReviewDecisionInput(
            connection_id=str(conn.id),
            review_id=review.review_id,
            verdict="approved",
            feedback="ISSUE-1 resolved; approved draft.",
        ),
    )
    await context.session.commit()
    with pytest.raises(GhostApiError, match="draft-only"):
        await _publish(
            director_ctx, PublishInput(connection_id=str(conn.id), review_id=review.review_id)
        )
    assert (
        await _post_read(context, PostReadInput(connection_id=str(conn.id), post_id=draft.post_id))
    ).status == "draft"

    # Owner-authorized separate release in this disposable fixture. A fresh
    # package/review is required even though the provider body is unchanged.
    assignment.release_intent = "publish_after_ashley_review"
    assignment.editorial_version += 1
    await context.session.flush()
    release_review = await request_review(revised)
    assert release_review.review_id != review.review_id
    await read_review(release_review, revised)
    await _review_decide(
        director_ctx,
        ReviewDecisionInput(
            connection_id=str(conn.id),
            review_id=release_review.review_id,
            verdict="approved",
            feedback="Approved exact fixture release.",
        ),
    )
    await context.session.commit()
    with pytest.raises(GhostApiError, match="Only the designated"):
        await _publish(
            context, PublishInput(connection_id=str(conn.id), review_id=release_review.review_id)
        )
    # A real out-of-band metadata edit must invalidate the approved version.
    await ghost_request(
        URL,
        key,
        "PUT",
        post_path(revised.post_id),
        body={
            "posts": [
                {"updated_at": revised.updated_at, "meta_description": "External fixture edit"}
            ]
        },
        params={"formats": "html,lexical", "include": "authors,tags"},
    )
    with pytest.raises(GhostApiError, match="Draft changed after review"):
        await _publish(
            director_ctx,
            PublishInput(connection_id=str(conn.id), review_id=release_review.review_id),
        )
    fresh = await _post_read(
        context, PostReadInput(connection_id=str(conn.id), post_id=draft.post_id)
    )
    assert fresh.status == "draft"
    release_review = await request_review(fresh)
    await read_review(release_review, fresh)
    await _review_decide(
        director_ctx,
        ReviewDecisionInput(
            connection_id=str(conn.id),
            review_id=release_review.review_id,
            verdict="approved",
            feedback="Reviewed external metadata change.",
        ),
    )
    await context.session.commit()
    published = await _publish(
        director_ctx, PublishInput(connection_id=str(conn.id), review_id=release_review.review_id)
    )
    assert published.status == "published"
    actual = await ghost_request(
        URL, key, "GET", post_path(draft.post_id), params={"formats": "html,lexical"}
    )
    assert actual["posts"][0]["status"] == "published"
    assert "Fixture conclusion." in actual["posts"][0]["html"]
