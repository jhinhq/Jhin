"""Ghost draft, review, and director-only publication operations.

The reviewed snapshot and publishing identity are authoritative records.
Approving a normal WorkReview does not authorize publication: the designated
publisher must inspect and decide this exact provider revision through here.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from html import unescape
from typing import Any, NamedTuple, cast
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import select, update

from jhin_connectors.endpoints import EndpointPolicyError, validate_public_http_url
from jhin_connectors.execution import resolve_connection
from jhin_connectors.ghost.assignments import (
    ensure_package,
    evidence_snapshot,
    manifest_revision,
    package_manifest,
    require_assignment,
    require_research,
)
from jhin_connectors.ghost.authority import installation_authority
from jhin_connectors.ghost.client import (
    GhostApiError,
    admin_origin,
    ghost_request,
    one_post,
    post_path,
    post_revision,
    validate_admin_url,
)
from jhin_connectors.ghost.schemas import (
    DraftCreateInput,
    DraftUpdateInput,
    PostListInput,
    PostListOutput,
    PostOutput,
    PostReadInput,
    PublishInput,
    ReviewDecisionInput,
    ReviewOutput,
    ReviewPackageOutput,
    ReviewReadInput,
    ReviewRequestInput,
)
from jhin_db.models import (
    Agent,
    Connection,
    GhostEditorialReview,
    Task,
    WorkRequest,
    WorkReview,
    Workspace,
)
from jhin_db.models.editorial import (
    EditorialAssignment,
    EditorialReviewPackage,
    GhostReviewReadReceipt,
)
from jhin_db.models.editorial_assets import EditorialAsset
from jhin_domain import ReviewMode
from jhin_policy import RiskLevel, ToolDefinition
from jhin_policy.reviews import ReviewerSelector
from jhin_policy.work_requests import coordination_settings
from jhin_tools.builtin import ToolExecutionContext
from jhin_tools.reviews import open_review
from jhin_tools.sanitize import sanitize_payload
from jhin_tools.work_requests import activate_work_request, create_work_request

# Browsing the blog answers "what has already been written", so this listing
# asks Ghost for identity and routing only. Whole article bodies — and the
# Lexical source, which is larger still — belong to ghost.post.read and the
# archive; a page of them against a real blog exceeds the response bound.
_LIST_FIELDS = "id,title,slug,status,updated_at,url,custom_excerpt,visibility,feature_image"
_LIST_PAGE_SIZE = 10
_LIST_NOTE = (
    "This row carries no article body and no revision, because the listing did not read "
    "either one. Call ghost.post.read with this post_id for the article text and for the "
    "revision that editing and review require."
)
# Mirror DraftCreateInput.html / .feature_image_caption max_length: a credit
# appended after validation must not push either field past the bound the input
# schema declares.
_MAX_DRAFT_HTML_CHARS = 100_000
_MAX_DRAFT_CAPTION_CHARS = 1_000
_TAG = re.compile(r"<[^>]*>")
_PARAGRAPH = re.compile(r"<p\b[^>]*>(.*?)</p\s*>", re.IGNORECASE | re.DOTALL)


async def _connection(ctx: ToolExecutionContext, connection_id: str) -> Connection:
    try:
        identifier = UUID(connection_id)
    except ValueError:
        raise GhostApiError(
            "Ghost connection is unavailable", code="ghost_connection_unavailable"
        ) from None
    connection = await ctx.session.scalar(
        select(Connection)
        .where(
            Connection.workspace_id == ctx.workspace_id,
            Connection.id == identifier,
            Connection.connector_type == "ghost",
        )
        .with_for_update(key_share=True)
        .execution_options(populate_existing=True)
    )
    if connection is None or connection.status != "active":
        raise GhostApiError("Ghost connection is unavailable", code="ghost_connection_unavailable")
    return connection


async def _api(ctx: ToolExecutionContext, connection_id: str) -> tuple[Connection, str, str]:
    connection = await _connection(ctx, connection_id)
    base = validate_admin_url(str(connection.config_json.get("admin_url", "")))
    variable_id = connection.config_json.get("admin_key_variable_id")
    if variable_id:
        # A variable is resolved only for its persisted connection/field/origin
        # binding, and the caller's CURRENT audience membership is rechecked.
        from jhin_secrets.variables import resolve_internal

        key = await resolve_internal(
            ctx,
            UUID(str(variable_id)),
            connection_id=connection.id,
            credential_field="admin_key",
            approved_origin=admin_origin(base),
        )
    else:
        resolved = await resolve_connection(ctx, connection_id, connector_type="ghost")
        key = resolved.credentials.get("admin_key", "")
    return connection, base, key


async def _publisher(ctx: ToolExecutionContext, connection: Connection) -> Agent:
    try:
        identifier = UUID(str(connection.config_json.get("publisher_agent_id", "")))
    except ValueError:
        raise GhostApiError(
            "A workspace admin must designate the publishing agent before requesting review",
            code="ghost_publisher_required",
        ) from None
    publisher = await ctx.session.scalar(
        select(Agent).where(
            Agent.id == identifier,
            Agent.workspace_id == ctx.workspace_id,
            Agent.status == "active",
        )
    )
    if publisher is None:
        raise GhostApiError(
            "The designated publishing agent is unavailable", code="ghost_publisher_unavailable"
        )
    await installation_authority(
        ctx.session,
        ctx.workspace_id,
        str(connection.config_json.get("admin_url", "")),
        publisher.id,
    )
    return publisher


def _post_output(post: dict[str, Any]) -> PostOutput:
    return PostOutput(
        post_id=str(post["id"]),
        title=str(post.get("title", "")),
        slug=str(post.get("slug", "")),
        status=str(post.get("status", "")),
        updated_at=str(post["updated_at"]),
        revision=post_revision(post),
        url=str(post.get("url", "")),
        html=str(post.get("html") or ""),
        html_total_chars=len(str(post.get("html") or "")),
        feature_image=post.get("feature_image"),
        feature_image_alt=str(post.get("feature_image_alt") or ""),
        feature_image_caption=str(post.get("feature_image_caption") or ""),
        custom_excerpt=str(post.get("custom_excerpt") or ""),
        tags=post.get("tags") or [],
        authors=post.get("authors") or [],
        meta_title=str(post.get("meta_title") or ""),
        meta_description=str(post.get("meta_description") or ""),
        canonical_url=post.get("canonical_url"),
        visibility=str(post.get("visibility") or "public"),
        metadata={
            name: post[name]
            for name in (
                "og_image",
                "og_title",
                "og_description",
                "twitter_image",
                "twitter_title",
                "twitter_description",
                "codeinjection_head",
                "codeinjection_foot",
                "custom_template",
                "featured",
                "email_subject",
                "email_only",
                "newsletter",
            )
            if name in post
        },
    )


def _list_entry(post: dict[str, Any]) -> PostOutput:
    """Describe a post the listing deliberately did not read.

    The body was never fetched, so this row must not read as a finished empty
    article: nothing is delivered, the first unread character is still the
    first, and `complete` stays false. The revision is left empty for the same
    reason — a hash over the handful of fields a listing asks for covers a
    strict subset of what post_revision binds, so it could never equal the
    revision the same post yields from ghost.post.read, and an agent that fed
    it back would be refused at the schema. Both come from ghost.post.read.
    """
    result = _post_output(post)
    result.html = ""
    result.html_total_chars = 0
    result.html_offset = 0
    result.next_offset = 0
    result.complete = False
    result.revision = ""
    result.metadata = {**result.metadata, "note": _LIST_NOTE}
    return result


async def _read(base: str, key: str, post_id: str) -> dict[str, Any]:
    return one_post(
        await ghost_request(
            base,
            key,
            "GET",
            post_path(post_id),
            params={"formats": "html,lexical", "include": "authors,tags"},
        )
    )


async def _post_read(ctx: ToolExecutionContext, payload: BaseModel) -> PostOutput:
    data = cast(PostReadInput, payload)
    _, base, key = await _api(ctx, data.connection_id)
    post = await _read(base, key, data.post_id)
    result = _post_output(post)
    end = min(data.offset + data.limit, len(result.html))
    if data.offset > len(result.html):
        raise GhostApiError("Read offset exceeds the current article", code="ghost_read_offset")
    result.html = result.html[data.offset : end]
    result.html_offset = data.offset
    result.next_offset = end if end < result.html_total_chars else None
    result.complete = data.offset == 0 and end == result.html_total_chars
    # Do not attest delivery if the gateway's public sanitizer would truncate
    # any part of this chunk or its metadata.
    delivered = result.model_dump(mode="json")
    if sanitize_payload(delivered) != delivered:
        raise GhostApiError(
            "Review metadata exceeds the safe delivery limit; reduce metadata before review",
            code="ghost_review_metadata_incomplete",
        )
    ctx.session.add(
        GhostReviewReadReceipt(
            workspace_id=ctx.workspace_id,
            connection_id=UUID(data.connection_id),
            agent_id=ctx.agent_id,
            post_id=data.post_id,
            revision=result.revision,
            start_offset=data.offset,
            end_offset=end,
            total_chars=result.html_total_chars,
        )
    )
    await ctx.session.flush()
    return result


async def _post_list(ctx: ToolExecutionContext, payload: BaseModel) -> PostListOutput:
    data = cast(PostListInput, payload)
    _, base, key = await _api(ctx, data.connection_id)
    params: dict[str, Any] = {
        "limit": min(data.limit, _LIST_PAGE_SIZE),
        "page": data.page,
        "order": "updated_at desc",
        "fields": _LIST_FIELDS,
    }
    if data.status != "all":
        params["filter"] = f"status:{data.status}"
    result = await ghost_request(base, key, "GET", "posts/", params=params)
    posts = result.get("posts")
    if not isinstance(posts, list):
        raise GhostApiError("Ghost did not return posts", code="ghost_bad_response")
    meta = result.get("meta", {})
    pagination = meta.get("pagination", {}) if isinstance(meta, dict) else {}
    next_page = pagination.get("next") if isinstance(pagination, dict) else None
    listing = PostListOutput(
        posts=[_list_entry(one_post({"posts": [post]})) for post in posts],
        page=data.page,
        next_page=next_page if isinstance(next_page, int) else None,
    )
    # A page of long titles and excerpts can still outgrow what the gateway
    # will hand back whole. Say so rather than deliver a page whose rows the
    # sanitizer quietly cut down — a truncated slug or id reads as a real one.
    delivered = listing.model_dump(mode="json")
    if sanitize_payload(delivered) != delivered:
        raise GhostApiError(
            "This page of posts exceeds the safe delivery limit; "
            "ask for a smaller limit and follow next_page",
            code="ghost_list_page_too_large",
        )
    return listing


def _draft_fields(data: DraftCreateInput) -> dict[str, Any]:
    body: dict[str, Any] = {
        "title": data.title,
        "html": data.html,
        "slug": data.slug,
        "status": "draft",
        "custom_excerpt": data.excerpt,
        "tags": [{"name": tag} for tag in data.tags],
        "feature_image_caption": data.feature_image_caption,
        "meta_title": data.meta_title,
        "meta_description": data.meta_description,
    }
    if data.feature_image is not None:
        body["feature_image"] = validate_public_http_url(
            data.feature_image, kind="Feature image URL"
        )
        body["feature_image_alt"] = data.feature_image_alt
    if data.authors:
        body["authors"] = [{"id": author} for author in data.authors]
    if data.canonical_url is not None:
        body["canonical_url"] = validate_public_http_url(data.canonical_url, kind="Canonical URL")
    return body


class _Cover(NamedTuple):
    """The assignment's cover photo, or the reason there is not one yet."""

    asset: EditorialAsset | None
    awaiting_confirmation: bool


async def _selected_cover(ctx: ToolExecutionContext, assignment: EditorialAssignment) -> _Cover:
    """Return the person's current photo choice for this assignment, if any.

    The latest selection is the one that counts. Until its Unsplash tracking
    reservation is confirmed it yields no cover at all — an unconfirmed photo
    never reaches Ghost, and an earlier confirmed photo does not stand in for
    it, or a re-pick whose tracking is stuck would be silently superseded by
    the very cover the person replaced. Writing prose is not what that
    reservation gates, so a draft can still be saved without a cover; the
    callers decide what a pending selection blocks, and the review gate is
    where an unresolved choice stops the article and says so.
    """
    latest = await ctx.session.scalar(
        select(EditorialAsset)
        .where(
            EditorialAsset.workspace_id == ctx.workspace_id,
            EditorialAsset.assignment_id == assignment.id,
        )
        .order_by(EditorialAsset.selected_at.desc(), EditorialAsset.id.desc())
        .limit(1)
        .execution_options(populate_existing=True)
    )
    if latest is None:
        return _Cover(None, False)
    if latest.status == "confirmed":
        return _Cover(latest, False)
    return _Cover(None, True)


def _visible_text(markup: str) -> str:
    """The reader-visible text of a fragment, with markup and spacing normalized."""
    return " ".join(unescape(_TAG.sub(" ", markup)).split())


def _credit_present(html: str, credit: str) -> bool:
    """Whether this body already carries the credit block as its own paragraph.

    Drafts are written with source=html, so Ghost parses the body into lexical
    and renders it back: attribute order, quoting, an added rel and ampersand
    escaping are all the renderer's to choose. Testing for the exact credit
    string would therefore miss the credit in a body a writer read back, and
    every update would append another copy. What no re-render rewrites is the
    block's visible text, so that is what identifies it. Matching instead on
    the photographer's URL anywhere in the body would also match prose that
    legitimately links the photographer, and then the attribution Unsplash
    requires would be dropped — worse than repeating it.
    """
    wanted = _visible_text(credit)
    return bool(wanted) and any(
        _visible_text(block) == wanted for block in _PARAGRAPH.findall(html)
    )


def _attach_cover(body: dict[str, Any], asset: EditorialAsset, requested_alt: str) -> None:
    """Carry the chosen photo, its alt text and its required credit into the draft.

    Unsplash licenses the hotlink rather than a copy, so the stored URL goes
    through unchanged, query string included. Crediting the photographer and
    Unsplash with referral links is a condition of that licence, and Ghost
    themes do not reliably render a feature-image caption — so the credit is
    written into the article body too, where a reader will actually meet it.
    """
    metadata = asset.metadata_json
    image = str(metadata.get("image_url") or "")
    credit = str(metadata.get("attribution_html") or "")
    try:
        usable = bool(image) and validate_public_http_url(image, kind="Feature image URL") == image
    except (EndpointPolicyError, ValueError):
        usable = False
    if not usable or not credit:
        raise GhostApiError(
            "The selected photo is missing a usable hotlink or its required credit",
            code="ghost_image_metadata_invalid",
        )
    if body.get("feature_image") not in (None, image):
        raise GhostApiError(
            "This assignment already has a cover photo chosen by a person; "
            "do not substitute a different feature image",
            code="ghost_image_not_selected",
        )
    alt = requested_alt.strip() or str(metadata.get("suggested_alt") or "").strip()
    if not alt:
        # Unsplash had no description for this photo. Describing it is the
        # writer's job; inventing alt text here would be a guess about an image.
        raise GhostApiError(
            "The chosen photo carries no description; supply feature_image_alt for the cover",
            code="ghost_image_alt_required",
        )
    body["feature_image"] = image
    body["feature_image_alt"] = alt[:300]
    caption = str(body.get("feature_image_caption") or "")
    # Ghost stores the caption verbatim, so the exact credit both round-trips
    # and is what the review gate looks for there.
    if credit not in caption:
        caption = f"{caption} {credit}".strip()
    if len(caption) > _MAX_DRAFT_CAPTION_CHARS:
        raise GhostApiError(
            "The caption plus the photo credit Unsplash requires exceeds the "
            f"{_MAX_DRAFT_CAPTION_CHARS} character limit; shorten feature_image_caption and retry",
            code="ghost_caption_too_long",
        )
    body["feature_image_caption"] = caption
    html = str(body.get("html") or "")
    if not _credit_present(html, credit):
        html = f"{html}\n<p>{credit}</p>"
    if len(html) > _MAX_DRAFT_HTML_CHARS:
        raise GhostApiError(
            "The article body plus the photo credit Unsplash requires exceeds the "
            f"{_MAX_DRAFT_HTML_CHARS} character limit; shorten the body and retry",
            code="ghost_body_too_long",
        )
    body["html"] = html


async def _draft_body(
    ctx: ToolExecutionContext, assignment: EditorialAssignment, data: DraftCreateInput
) -> dict[str, Any]:
    body = _draft_fields(data)
    cover = await _selected_cover(ctx, assignment)
    if cover.asset is not None:
        _attach_cover(body, cover.asset, data.feature_image_alt)
    elif cover.awaiting_confirmation and data.feature_image is not None:
        # The prose may be saved while a person's choice waits for its tracking
        # receipt, but the cover slot that choice reserved is not the agent's
        # to fill with something else.
        raise GhostApiError(
            "A person's photo choice for this assignment has no confirmed Unsplash tracking "
            "receipt yet; save the draft without a cover instead of substituting another image",
            code="ghost_image_selection_unconfirmed",
        )
    return body


async def _draft_create(ctx: ToolExecutionContext, payload: BaseModel) -> PostOutput:
    data = cast(DraftCreateInput, payload)
    await _connection(ctx, data.connection_id)
    assignment = await require_assignment(
        ctx,
        data.assignment_id,
        data.connection_id,
        expected_version=data.expected_editorial_version,
    )
    if assignment.post_id is not None:
        raise GhostApiError(
            "This assignment already owns a draft; read it before retrying",
            code="ghost_duplicate_post",
        )
    _, base, key = await _api(ctx, data.connection_id)
    body = await _draft_body(ctx, assignment, data)
    # _api locks the connection before the credential; a unique slug is stable across
    # model retries. A found post must be inspected, never silently overwritten.
    existing = await ghost_request(
        base, key, "GET", "posts/", params={"filter": f"slug:{data.slug}", "limit": 1}
    )
    if not isinstance(existing.get("posts"), list):
        raise GhostApiError("Could not check for an existing post", code="ghost_bad_response")
    if existing["posts"]:
        raise GhostApiError(
            "A post with this slug already exists. Read it instead of creating a duplicate.",
            code="ghost_duplicate_post",
        )
    post = one_post(
        await ghost_request(
            base,
            key,
            "POST",
            "posts/",
            body={"posts": [body]},
            params={"source": "html", "formats": "html,lexical", "include": "authors,tags"},
        ),
        mutation=True,
    )
    if post.get("status") != "draft":
        raise GhostApiError(
            "Ghost did not confirm draft status", mutation=True, code="ghost_unexpected_status"
        )
    assignment.post_id = str(post["id"])
    assignment.phase = "draft"
    assignment.editorial_version += 1
    assignment.version += 1
    reread = await _read(base, key, assignment.post_id)
    if post_revision(reread) != post_revision(post):
        changed_fields = ", ".join(
            sorted(name for name in set(post) | set(reread) if post.get(name) != reread.get(name))
        )
        raise GhostApiError(
            f"Draft read-back does not match the saved revision ({changed_fields})",
            mutation=True,
            code="ghost_readback_mismatch",
        )
    await ctx.session.flush()
    result = _post_output(reread)
    result.assignment_id = str(assignment.id)
    result.editorial_version = assignment.editorial_version
    return result


async def _draft_update(ctx: ToolExecutionContext, payload: BaseModel) -> PostOutput:
    data = cast(DraftUpdateInput, payload)
    await _connection(ctx, data.connection_id)
    assignment = await require_assignment(
        ctx,
        data.assignment_id,
        data.connection_id,
        post_id=data.post_id,
        expected_version=data.expected_editorial_version,
    )
    _, base, key = await _api(ctx, data.connection_id)
    current = await _read(base, key, data.post_id)
    if current.get("status") != "draft":
        raise GhostApiError("This operation edits drafts only", code="ghost_not_draft")
    if str(current["updated_at"]) != data.expected_updated_at:
        raise GhostApiError(
            "Draft changed; read the latest revision before editing", code="ghost_revision_conflict"
        )
    fields = {**await _draft_body(ctx, assignment, data), "updated_at": data.expected_updated_at}
    post = one_post(
        await ghost_request(
            base,
            key,
            "PUT",
            post_path(data.post_id),
            body={"posts": [fields]},
            params={"source": "html", "formats": "html,lexical", "include": "authors,tags"},
        ),
        mutation=True,
    )
    if post.get("status") != "draft":
        raise GhostApiError(
            "Ghost did not confirm draft status", mutation=True, code="ghost_unexpected_status"
        )
    reread = await _read(base, key, data.post_id)
    if post_revision(reread) != post_revision(post):
        raise GhostApiError(
            "Draft read-back does not match the saved revision",
            mutation=True,
            code="ghost_readback_mismatch",
        )
    assignment.editorial_version += 1
    assignment.version += 1
    assignment.phase = "draft"
    await ctx.session.flush()
    result = _post_output(reread)
    result.assignment_id = str(assignment.id)
    result.editorial_version = assignment.editorial_version
    return result


async def _review(
    ctx: ToolExecutionContext, data: PublishInput | ReviewDecisionInput
) -> GhostEditorialReview:
    try:
        review_id = UUID(data.review_id)
    except ValueError:
        raise GhostApiError(
            "Editorial review is unavailable", code="ghost_review_unavailable"
        ) from None
    row = await ctx.session.scalar(
        select(GhostEditorialReview).where(
            GhostEditorialReview.workspace_id == ctx.workspace_id,
            GhostEditorialReview.connection_id == UUID(data.connection_id),
            GhostEditorialReview.id == review_id,
        )
    )
    if row is None:
        raise GhostApiError("Editorial review is unavailable", code="ghost_review_unavailable")
    return row


def _review_output(row: GhostEditorialReview, **extra: Any) -> ReviewOutput:
    return ReviewOutput(
        review_id=str(row.id),
        post_id=row.post_id,
        revision=row.revision,
        status=row.status,
        publisher_agent_id=str(row.publisher_agent_id),
        work_request_id=str(row.work_request_id) if row.work_request_id else None,
        feedback=row.feedback,
        assignment_id=str(row.assignment_id) if row.assignment_id else None,
        package_id=str(row.package_id) if row.package_id else None,
        release_intent=row.release_intent,
        revision_round=row.revision_round,
        **extra,
    )


async def _review_request(ctx: ToolExecutionContext, payload: BaseModel) -> ReviewOutput:
    data = cast(ReviewRequestInput, payload)
    await _connection(ctx, data.connection_id)
    assignment = await require_assignment(
        ctx,
        data.assignment_id,
        data.connection_id,
        post_id=data.post_id,
        expected_version=data.expected_editorial_version,
    )
    # A draft may be written while a photo choice waits for its Unsplash
    # tracking receipt; sending that draft on for a decision may not be, or the
    # reviewer would be asked to judge an article whose cover is unresolved.
    if (await _selected_cover(ctx, assignment)).awaiting_confirmation:
        raise GhostApiError(
            "The selected photo has no confirmed Unsplash tracking receipt; "
            "resolve that selection before sending this draft for review",
            code="ghost_image_selection_unconfirmed",
        )
    await require_research(ctx, assignment)
    connection, base, key = await _api(ctx, data.connection_id)
    publisher = await _publisher(ctx, connection)
    if publisher.id != assignment.publisher_agent_id:
        raise GhostApiError(
            "Assignment publisher changed; obtain a new assignment", code="ghost_assignment_stale"
        )
    if publisher.id == ctx.agent_id:
        raise GhostApiError(
            "The draft author cannot review their own submission", code="ghost_self_review"
        )
    post = await _read(base, key, data.post_id)
    await require_research(ctx, assignment, post)
    revision = post_revision(post)
    if post.get("status") != "draft" or revision != data.expected_revision:
        raise GhostApiError(
            "Read the current draft before requesting review", code="ghost_revision_conflict"
        )
    package = await ensure_package(ctx, assignment, revision, base)
    row = await ctx.session.scalar(
        select(GhostEditorialReview).where(
            GhostEditorialReview.connection_id == connection.id,
            GhostEditorialReview.post_id == data.post_id,
            GhostEditorialReview.revision == revision,
            GhostEditorialReview.publisher_agent_id == publisher.id,
            GhostEditorialReview.package_id == package.id,
        )
    )
    if row is None:
        previous = list(
            await ctx.session.scalars(
                select(GhostEditorialReview)
                .where(GhostEditorialReview.assignment_id == assignment.id)
                .order_by(GhostEditorialReview.created_at.desc(), GhostEditorialReview.id.desc())
            )
        )
        if sum(item.status == "changes_requested" for item in previous) >= 3:
            assignment.phase = "blocked"
            assignment.blocked_reason = "Three revision rounds require editorial escalation"
            await ctx.session.flush()
            raise GhostApiError(
                "Three revision rounds require editorial escalation", code="ghost_revision_limit"
            )
        snapshot = sanitize_payload(post, max_string_chars=2_000_000, max_document_bytes=4_194_304)
        if snapshot.get("truncated") or any(
            "…[truncated]" in str(value) for value in snapshot.values()
        ):
            raise GhostApiError(
                "Draft exceeds complete review storage limits", code="ghost_review_incomplete"
            )
        row = GhostEditorialReview(
            workspace_id=ctx.workspace_id,
            connection_id=connection.id,
            post_id=data.post_id,
            revision=revision,
            admin_url=base,
            provider_updated_at=str(post["updated_at"]),
            snapshot_json=snapshot,
            author_agent_id=ctx.agent_id,
            publisher_agent_id=publisher.id,
            assignment_id=assignment.id,
            package_id=package.id,
            assignment_editorial_version=assignment.editorial_version,
            release_intent=assignment.release_intent,
            revision_round=len(previous) + 1,
            prior_review_id=previous[0].id if previous else None,
        )
        ctx.session.add(row)
        await ctx.session.flush()
        work_review, _ = await open_review(
            ctx.session,
            workspace_id=ctx.workspace_id,
            subject_agent_id=ctx.agent_id,
            trigger_key=f"ghost:{row.id}",
            mode=ReviewMode.BEFORE_CLOSE,
            selector=ReviewerSelector(kind="agent", agent_id=str(publisher.id)),
            fail_closed=True,
            task_id=ctx.task_id,
            run_id=ctx.run_id,
            tool_call_id=ctx.tool_call_id,
            evidence={
                "kind": "ghost_editorial",
                "editorial_review_id": str(row.id),
                "connection_id": str(connection.id),
                "post_id": data.post_id,
                "revision": revision,
                "title": str(post.get("title", "")),
                "summary": data.summary,
            },
        )
        row.work_review_id = work_review.id
        requester = await ctx.session.get(Agent, ctx.agent_id)
        task = await ctx.session.get(Task, ctx.task_id)
        if requester is None:
            raise GhostApiError("Draft author is unavailable", code="ghost_author_unavailable")
        request, _ = await create_work_request(
            ctx.session,
            workspace_id=ctx.workspace_id,
            requester=requester,
            target=publisher,
            requester_task=task,
            requester_run_id=ctx.run_id,
            title=f"Review Ghost draft: {str(post.get('title', ''))[:180]}",
            description=(
                f"Review the exact draft using ghost.post.read with connection_id={connection.id} "
                f"post_id={data.post_id}. Editorial review_id={row.id}, revision={revision}. "
                "Read the immutable evidence package with ghost.review.read using this review_id, "
                "following next_offset until complete. Check accuracy, originality, audience, "
                "citations, images, and the standing brief. "
                "Use ghost.review.decide to approve or request changes with specific feedback. "
                f"Persisted release intent: {assignment.release_intent}. "
                + (
                    "If approved, keep the post as an approved draft. Do not publish or schedule. "
                    if assignment.release_intent == "draft_only"
                    else "After approval you may use ghost.post.publish for this exact review. "
                )
                + "Read all article chunks and metadata before deciding. "
                "Do not delegate publication or use a terminal/HTTP bypass.\n\n"
                + f"Author's handoff: {data.summary}"
            ),
            expected_output=(
                "Review verdict, actionable issue IDs, and confirmed draft status "
                "or required changes."
            ),
            idempotency_key=f"ghost-review:{row.id}",
        )
        request.metadata_json = {
            **request.metadata_json,
            "editorial_assignment_id": str(assignment.id),
            "editorial_review_id": str(row.id),
            "editorial_package_id": str(package.id),
            "revision_round": row.revision_round,
            "prior_review_id": str(row.prior_review_id) if row.prior_review_id else None,
        }
        row.work_request_id = request.id
        work_review.work_request_id = request.id
        await ctx.session.flush()
    handoff = (
        await ctx.session.get(WorkRequest, row.work_request_id) if row.work_request_id else None
    )
    if handoff is None:
        raise GhostApiError("Review handoff is unavailable", code="ghost_review_handoff_missing")
    workspace = await ctx.session.get(Workspace, ctx.workspace_id)
    activation = await activate_work_request(
        ctx.session,
        handoff,
        settings=coordination_settings(workspace.settings_json if workspace else None),
        target_name=publisher.name,
    )
    return _review_output(
        row,
        created_task_id=activation.task_id,
        agent_id=str(publisher.id),
        activated=activation.activated,
        detail=activation.detail,
    )


async def _check_publisher(
    ctx: ToolExecutionContext,
    connection: Connection,
    row: GhostEditorialReview,
    base: str,
) -> None:
    publisher = await _publisher(ctx, connection)
    if (
        publisher.id != ctx.agent_id
        or row.publisher_agent_id != publisher.id
        or row.admin_url != base
    ):
        raise GhostApiError(
            "Only the designated publisher may decide or publish this revision",
            code="ghost_publisher_only",
        )
    if row.author_agent_id == ctx.agent_id:
        raise GhostApiError("The author cannot approve their own draft", code="ghost_self_review")


async def _check_assignment_review(
    ctx: ToolExecutionContext, row: GhostEditorialReview, *, publish: bool = False
) -> EditorialAssignment:
    assignment = await require_assignment(
        ctx,
        str(row.assignment_id) if row.assignment_id else None,
        str(row.connection_id),
        post_id=row.post_id,
        expected_version=row.assignment_editorial_version,
    )
    if publish and (
        row.release_intent != "publish_after_ashley_review"
        or assignment.release_intent != row.release_intent
    ):
        raise GhostApiError("This draft-only assignment cannot publish", code="ghost_draft_only")
    package = (
        await ctx.session.get(EditorialReviewPackage, row.package_id) if row.package_id else None
    )
    await require_research(ctx, assignment, row.snapshot_json)
    current_manifest = package_manifest(assignment, row.revision, row.admin_url)
    current_manifest["evidence"] = await evidence_snapshot(
        ctx, assignment, assignment.evidence_tool_call_ids
    )
    if (
        package is None
        or package.workspace_id != ctx.workspace_id
        or package.assignment_id != assignment.id
        or assignment.publisher_agent_id != row.publisher_agent_id
        or assignment.release_intent != row.release_intent
        or manifest_revision(package.manifest_json) != package.revision
        or manifest_revision(current_manifest) != package.revision
    ):
        raise GhostApiError(
            "Editorial assignment package changed; request a new review", code="ghost_package_stale"
        )
    return assignment


async def _require_read_receipts(ctx: ToolExecutionContext, row: GhostEditorialReview) -> None:
    receipts = list(
        await ctx.session.scalars(
            select(GhostReviewReadReceipt)
            .where(
                GhostReviewReadReceipt.workspace_id == ctx.workspace_id,
                GhostReviewReadReceipt.connection_id == row.connection_id,
                GhostReviewReadReceipt.agent_id == ctx.agent_id,
                GhostReviewReadReceipt.post_id == row.post_id,
                GhostReviewReadReceipt.revision == row.revision,
                GhostReviewReadReceipt.package_id.is_(None),
            )
            .order_by(GhostReviewReadReceipt.start_offset)
        )
    )
    length = len(str(row.snapshot_json.get("html") or ""))
    covered = 0
    for receipt in receipts:
        if receipt.total_chars == length and receipt.start_offset <= covered:
            covered = max(covered, receipt.end_offset)
    if not receipts or covered != length:
        raise GhostApiError(
            "Read every article chunk and complete metadata before deciding",
            code="ghost_review_read_incomplete",
        )
    package = await ctx.session.get(EditorialReviewPackage, row.package_id)
    if package is None:
        raise GhostApiError("Review package unavailable", code="ghost_package_stale")
    package_length = len(_package_text(package))
    receipts = list(
        await ctx.session.scalars(
            select(GhostReviewReadReceipt)
            .where(
                GhostReviewReadReceipt.workspace_id == ctx.workspace_id,
                GhostReviewReadReceipt.agent_id == ctx.agent_id,
                GhostReviewReadReceipt.package_id == package.id,
                GhostReviewReadReceipt.revision == package.revision,
            )
            .order_by(GhostReviewReadReceipt.start_offset)
        )
    )
    covered = 0
    for receipt in receipts:
        if receipt.total_chars == package_length and receipt.start_offset <= covered:
            covered = max(covered, receipt.end_offset)
    if not receipts or covered != package_length:
        raise GhostApiError(
            "Read every review package chunk before deciding", code="ghost_package_read_incomplete"
        )


def _package_text(package: EditorialReviewPackage) -> str:
    return json.dumps(
        package.manifest_json, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )


async def _review_read(ctx: ToolExecutionContext, payload: BaseModel) -> ReviewPackageOutput:
    data = cast(ReviewReadInput, payload)
    row = await _review(ctx, data)
    await _check_assignment_review(ctx, row)
    package = await ctx.session.get(EditorialReviewPackage, row.package_id)
    assert package is not None
    content = _package_text(package)
    if data.offset > len(content):
        raise GhostApiError("Package offset exceeds its length", code="ghost_read_offset")
    end = min(data.offset + data.limit, len(content))
    result = ReviewPackageOutput(
        review_id=str(row.id),
        package_id=str(package.id),
        revision=package.revision,
        package_json=content[data.offset : end],
        offset=data.offset,
        total_chars=len(content),
        next_offset=end if end < len(content) else None,
        complete=data.offset == 0 and end == len(content),
    )
    delivered = result.model_dump(mode="json")
    if sanitize_payload(delivered) != delivered:
        raise GhostApiError(
            "Package chunk exceeds delivery limits; read a smaller chunk",
            code="ghost_package_read_incomplete",
        )
    ctx.session.add(
        GhostReviewReadReceipt(
            workspace_id=ctx.workspace_id,
            connection_id=row.connection_id,
            agent_id=ctx.agent_id,
            post_id=row.post_id,
            revision=package.revision,
            package_id=package.id,
            start_offset=data.offset,
            end_offset=end,
            total_chars=len(content),
        )
    )
    await ctx.session.flush()
    return result


async def _review_decide(ctx: ToolExecutionContext, payload: BaseModel) -> ReviewOutput:
    data = cast(ReviewDecisionInput, payload)
    connection = await _connection(ctx, data.connection_id)
    row = await _review(ctx, data)
    base = validate_admin_url(str(connection.config_json.get("admin_url", "")))
    await _check_publisher(ctx, connection, row, base)
    await _check_assignment_review(ctx, row)
    if row.status != "pending":
        if row.status == data.verdict and row.feedback == data.feedback:
            return _review_output(row)
        raise GhostApiError("This review is already decided", code="ghost_review_decided")
    _, base, key = await _api(ctx, data.connection_id)
    current = await _read(base, key, row.post_id)
    if post_revision(current) != row.revision or current.get("status") != "draft":
        row.status = "stale"
        await ctx.session.flush()
        raise GhostApiError(
            "The draft changed; request a new review", code="ghost_revision_conflict"
        )
    await _require_read_receipts(ctx, row)
    claimed = await ctx.session.scalar(
        update(GhostEditorialReview)
        .where(
            GhostEditorialReview.id == row.id,
            GhostEditorialReview.status == "pending",
            GhostEditorialReview.revision == row.revision,
        )
        .values(status=data.verdict, feedback=data.feedback, decided_at=datetime.now(UTC))
        .returning(GhostEditorialReview.id)
    )
    if claimed is None:
        raise GhostApiError(
            "Another decision already claimed this review", code="ghost_review_decided"
        )
    await ctx.session.refresh(row)
    if row.work_review_id:
        review = await ctx.session.get(WorkReview, row.work_review_id)
        if review:
            review.status = data.verdict
            review.verdict = "approve" if data.verdict == "approved" else "changes_requested"
            review.feedback = data.feedback
            review.decided_at = row.decided_at
            review.decided_by_agent_id = ctx.agent_id
    await ctx.session.flush()
    return _review_output(row)


async def _publish(ctx: ToolExecutionContext, payload: BaseModel) -> PostOutput:
    data = cast(PublishInput, payload)
    connection = await _connection(ctx, data.connection_id)
    row = await _review(ctx, data)
    base = validate_admin_url(str(connection.config_json.get("admin_url", "")))
    await _check_publisher(ctx, connection, row, base)
    await _check_assignment_review(ctx, row, publish=True)
    if row.status != "approved":
        raise GhostApiError(
            "Publication requires an approved, unused review; "
            "pending or uncertain sends must be reconciled",
            code="ghost_review_not_publishable",
        )
    if ctx.session_factory is None or ctx.tool_call_id is None:
        raise GhostApiError(
            "Durable publication reservation is unavailable", code="ghost_publication_unavailable"
        )
    _, base, key = await _api(ctx, data.connection_id)
    current = await _read(base, key, row.post_id)
    if post_revision(current) != row.revision or current.get("status") != "draft":
        row.status = "stale"
        await ctx.session.flush()
        raise GhostApiError(
            "Draft changed after review; obtain a new review", code="ghost_revision_conflict"
        )
    # Commit a one-use reservation BEFORE the external write, independently
    # of the gateway's result transaction. A crash cannot reopen approval.
    async with ctx.session_factory() as reservation:
        result = await reservation.scalar(
            update(GhostEditorialReview)
            .where(
                GhostEditorialReview.id == row.id,
                GhostEditorialReview.workspace_id == ctx.workspace_id,
                GhostEditorialReview.status == "approved",
                GhostEditorialReview.revision == row.revision,
            )
            .values(status="publishing", publication_tool_call_id=ctx.tool_call_id)
            .returning(GhostEditorialReview.id)
        )
        if result is None:
            raise GhostApiError(
                "This publication was already claimed", code="ghost_publication_claimed"
            )
        await reservation.commit()
    try:
        post = one_post(
            await ghost_request(
                base,
                key,
                "PUT",
                post_path(row.post_id),
                body={"posts": [{"status": "published", "updated_at": row.provider_updated_at}]},
                params={"formats": "html,lexical"},
            ),
            mutation=True,
        )
        if post.get("status") != "published":
            raise GhostApiError(
                "Ghost did not confirm publication",
                mutation=True,
                code="ghost_publication_unconfirmed",
            )
    except GhostApiError as error:
        async with ctx.session_factory() as reservation:
            await reservation.execute(
                update(GhostEditorialReview)
                .where(
                    GhostEditorialReview.id == row.id,
                    GhostEditorialReview.publication_tool_call_id == ctx.tool_call_id,
                )
                .values(status="uncertain" if error.side_effect_possible else "stale")
            )
            await reservation.commit()
        raise
    # Refresh avoids overwriting the committed reservation with a stale ORM row.
    await ctx.session.refresh(row)
    row.status = "published"
    row.published_at = datetime.now(UTC)
    await ctx.session.flush()
    return _post_output(post)


def _definition(
    name: str,
    description: str,
    model: type[BaseModel],
    output: type[BaseModel],
    *,
    write: bool = False,
) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=description,
        risk=RiskLevel.WRITE if write else RiskLevel.READ,
        input_model=model,
        output_model=output,
        required_capability=name,
        scope_keys=("connection_id",),
        defers_scope=True,
        supports_approval=write,
        redispatch_is_safe=not write,
    )


GHOST_TOOLS = (
    (
        _definition(
            "ghost.review.read",
            "Read the immutable review package and source receipts in chunks. "
            "Follow next_offset; all chunks must be read before deciding.",
            ReviewReadInput,
            ReviewPackageOutput,
        ),
        _review_read,
    ),
    (
        _definition(
            "ghost.post.list",
            "List actual Ghost posts before choosing a nonduplicate topic. "
            "Identity and routing only: rows carry no article body and no revision "
            "(html is empty, complete is false), so use ghost.post.read for either. "
            f"A page holds at most {_LIST_PAGE_SIZE} posts however large limit is; "
            "follow next_page for the rest. Uses only the configured Admin URL.",
            PostListInput,
            PostListOutput,
        ),
        _post_list,
    ),
    (
        _definition(
            "ghost.post.read",
            "Read a Ghost post and its current revision for editing or editorial review.",
            PostReadInput,
            PostOutput,
        ),
        _post_read,
    ),
    (
        _definition(
            "ghost.draft.create",
            "Create a draft only. Choose a stable unique slug; "
            "existing slugs are refused. Never publishes.",
            DraftCreateInput,
            PostOutput,
            write=True,
        ),
        _draft_create,
    ),
    (
        _definition(
            "ghost.draft.update",
            "Edit an existing draft with the expected updated_at revision. "
            "Cannot edit published posts or publish.",
            DraftUpdateInput,
            PostOutput,
            write=True,
        ),
        _draft_update,
    ),
    (
        _definition(
            "ghost.review.request",
            "Send the exact current draft to the configured publishing director for review "
            "under the persisted assignment release intent. "
            "Starts their work and records the immutable draft revision. "
            "Do not also delegate the same review.",
            ReviewRequestInput,
            ReviewOutput,
            write=True,
        ),
        _review_request,
    ),
    (
        _definition(
            "ghost.review.decide",
            "Designated publisher only: approve or request changes on the exact current draft. "
            "Read the post and assess its evidence first.",
            ReviewDecisionInput,
            ReviewOutput,
            write=True,
        ),
        _review_decide,
    ),
    (
        _definition(
            "ghost.post.publish",
            "Designated publisher only: publish the exact approved draft once. "
            "Changed drafts need a new review. Never retry uncertain publication automatically.",
            PublishInput,
            PostOutput,
            write=True,
        ),
        _publish,
    ),
)
