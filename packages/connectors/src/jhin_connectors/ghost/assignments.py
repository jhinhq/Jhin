"""Persisted editorial briefs; agent tools cannot grant publication intent."""

import hashlib
import json
from typing import Any, Literal, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from jhin_connectors.ghost.client import GhostApiError
from jhin_connectors.ghost.schemas import GhostInput
from jhin_db.models import Agent, AgentRun, Task, ToolCall, Workspace
from jhin_db.models.editorial import EditorialAssignment, EditorialReviewPackage
from jhin_policy import RiskLevel, ToolDefinition
from jhin_tools.builtin import ToolExecutionContext
from jhin_tools.sanitize import sanitize_payload


class EditorialBrief(BaseModel):
    model_config = ConfigDict(extra="forbid")
    topic_mode: Literal["specified", "delegated"] = "delegated"
    topic: str | None = Field(default=None, max_length=1000)
    angle: str | None = Field(default=None, max_length=2000)
    audience: str | None = Field(default=None, max_length=1000)
    goal: str | None = Field(default=None, max_length=1000)
    cta: str | None = Field(default=None, max_length=1000)
    tone: str | None = Field(default=None, max_length=1000)
    language: str | None = Field(default=None, max_length=100)
    word_count_min: int | None = Field(default=None, ge=0, le=50_000)
    word_count_max: int | None = Field(default=None, ge=1, le=50_000)
    image_mode: Literal["none", "cover", "cover_and_inline"] = "none"
    image_selection_mode: Literal["human", "approved_automation"] = "human"
    must_include: list[str] = Field(default_factory=list, max_length=30)
    must_avoid: list[str] = Field(default_factory=list, max_length=30)
    preferred_sources: list[str] = Field(default_factory=list, max_length=30)
    keywords: list[str] = Field(default_factory=list, max_length=30)
    due_at: str | None = Field(default=None, max_length=50)
    timezone: str | None = Field(default=None, max_length=100)


class AssignmentCreateInput(GhostInput):
    brief: EditorialBrief
    public_blog_url: str = Field(default="", max_length=2000)


class AssignmentReadInput(GhostInput):
    assignment_id: str


class AssignmentReviseInput(AssignmentReadInput):
    expected_version: int = Field(ge=1)
    brief: EditorialBrief


class AssignmentCancelInput(AssignmentReadInput):
    expected_version: int = Field(ge=1)


class AssignmentEvidenceInput(AssignmentCancelInput):
    evidence_tool_call_ids: list[UUID] = Field(max_length=100)


class AssignmentOutput(BaseModel):
    assignment_id: str
    connection_id: str
    writer_agent_id: str
    publisher_agent_id: str
    release_intent: str
    brief: dict[str, Any]
    brief_version: int
    editorial_version: int
    version: int
    phase: str
    post_id: str | None
    evidence_tool_call_ids: list[str] = Field(default_factory=list)


def assignment_output(row: EditorialAssignment) -> AssignmentOutput:
    return AssignmentOutput(
        assignment_id=str(row.id),
        connection_id=str(row.connection_id),
        writer_agent_id=str(row.writer_agent_id),
        publisher_agent_id=str(row.publisher_agent_id),
        release_intent=row.release_intent,
        brief=row.brief_json,
        brief_version=row.brief_version,
        editorial_version=row.editorial_version,
        version=row.version,
        phase=row.phase,
        post_id=row.post_id,
        evidence_tool_call_ids=row.evidence_tool_call_ids,
    )


async def require_assignment(
    ctx: ToolExecutionContext,
    assignment_id: str | None,
    connection_id: str,
    *,
    post_id: str | None = None,
    expected_version: int | None = None,
    allow_cancelled: bool = False,
) -> EditorialAssignment:
    try:
        identifier, connection = UUID(str(assignment_id)), UUID(connection_id)
    except ValueError:
        raise GhostApiError(
            "A bound editorial assignment is required", code="ghost_assignment_required"
        ) from None
    row = await ctx.session.scalar(
        select(EditorialAssignment)
        .where(
            EditorialAssignment.id == identifier,
            EditorialAssignment.workspace_id == ctx.workspace_id,
            EditorialAssignment.connection_id == connection,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None or ctx.agent_id not in {row.writer_agent_id, row.publisher_agent_id}:
        raise GhostApiError(
            "Editorial assignment is unavailable to this actor", code="ghost_assignment_denied"
        )
    if row.phase == "cancelled" and not allow_cancelled:
        raise GhostApiError("Editorial assignment is cancelled", code="ghost_assignment_cancelled")
    task = await ctx.session.get(Task, ctx.task_id)
    if not allow_cancelled and task is not None and task.state in {"cancelled", "stopped"}:
        raise GhostApiError("Editorial task is cancelled", code="ghost_assignment_cancelled")
    if post_id is not None and row.post_id != post_id:
        raise GhostApiError(
            "Post is not owned by this assignment", code="ghost_assignment_post_mismatch"
        )
    if expected_version is not None and row.editorial_version != expected_version:
        raise GhostApiError("Editorial assignment version changed", code="ghost_assignment_stale")
    return row


async def create_assignment(ctx: ToolExecutionContext, payload: BaseModel) -> AssignmentOutput:
    from jhin_connectors.ghost.authority import installation_authority
    from jhin_connectors.ghost.tools import _connection, _publisher

    data = cast(AssignmentCreateInput, payload)
    await ctx.session.scalar(
        select(Workspace.id).where(Workspace.id == ctx.workspace_id).with_for_update()
    )
    # Establish installation authority before connection/assignment row locks.
    connection = await _connection(ctx, data.connection_id)
    publisher = await _publisher(ctx, connection)
    if publisher.id == ctx.agent_id:
        raise GhostApiError("Writer and publisher must be distinct", code="ghost_self_review")
    await installation_authority(
        ctx.session,
        ctx.workspace_id,
        str(connection.config_json["admin_url"]),
        publisher.id,
        establish=True,
    )
    existing = await ctx.session.scalar(
        select(EditorialAssignment).where(
            EditorialAssignment.workspace_id == ctx.workspace_id,
            EditorialAssignment.connection_id == connection.id,
            EditorialAssignment.task_id == ctx.task_id,
            EditorialAssignment.writer_agent_id == ctx.agent_id,
        )
    )
    if existing is not None:
        return assignment_output(existing)
    task = await ctx.session.get(Task, ctx.task_id)
    writer = await ctx.session.get(Agent, ctx.agent_id)
    if writer is None or writer.workspace_id != ctx.workspace_id or writer.status != "active":
        raise GhostApiError("Writer is unavailable", code="ghost_assignment_denied")
    brief = data.brief.model_dump(mode="json")
    row = EditorialAssignment(
        workspace_id=ctx.workspace_id,
        connection_id=connection.id,
        writer_agent_id=ctx.agent_id,
        publisher_agent_id=publisher.id,
        task_id=task.id if task and task.workspace_id == ctx.workspace_id else None,
        conversation_id=task.conversation_id if task else None,
        team_id=writer.team_id,
        public_blog_url=data.public_blog_url,
        brief_json=sanitize_payload(brief, max_string_chars=4000, max_document_bytes=65_536),
        decision_provenance={
            "kind": "agent_recorded_brief",
            "task_id": str(ctx.task_id),
            "research_required": True,
        },
        release_intent="draft_only",
    )
    ctx.session.add(row)
    await ctx.session.flush()
    return assignment_output(row)


async def read_assignment(ctx: ToolExecutionContext, payload: BaseModel) -> AssignmentOutput:
    data = cast(AssignmentReadInput, payload)
    return assignment_output(
        await require_assignment(ctx, data.assignment_id, data.connection_id, allow_cancelled=True)
    )


async def revise_assignment(ctx: ToolExecutionContext, payload: BaseModel) -> AssignmentOutput:
    data = cast(AssignmentReviseInput, payload)
    row = await require_assignment(ctx, data.assignment_id, data.connection_id)
    if row.version != data.expected_version:
        raise GhostApiError("Assignment version changed", code="ghost_assignment_stale")
    brief = sanitize_payload(
        data.brief.model_dump(mode="json"), max_string_chars=4000, max_document_bytes=65_536
    )
    if row.brief_json != brief:
        row.brief_json = brief
        row.brief_version += 1
        row.editorial_version += 1
        row.version += 1
    await ctx.session.flush()
    return assignment_output(row)


async def cancel_assignment(ctx: ToolExecutionContext, payload: BaseModel) -> AssignmentOutput:
    data = cast(AssignmentCancelInput, payload)
    row = await require_assignment(
        ctx, data.assignment_id, data.connection_id, allow_cancelled=True
    )
    if row.version != data.expected_version:
        raise GhostApiError("Assignment version changed", code="ghost_assignment_stale")
    if row.phase != "cancelled":
        row.phase = "cancelled"
        row.version += 1
    await ctx.session.flush()
    return assignment_output(row)


def package_manifest(
    assignment: EditorialAssignment, provider_revision: str, admin_url: str
) -> dict[str, Any]:
    return {
        "assignment_id": str(assignment.id),
        "editorial_version": assignment.editorial_version,
        "brief": assignment.brief_json,
        "release_intent": assignment.release_intent,
        "post_id": assignment.post_id,
        "provider_revision": provider_revision,
        "connection_id": str(assignment.connection_id),
        "admin_url": admin_url,
        "public_blog_url": assignment.public_blog_url,
        "writer_agent_id": str(assignment.writer_agent_id),
        "publisher_agent_id": str(assignment.publisher_agent_id),
        "evidence_tool_call_ids": sorted(assignment.evidence_tool_call_ids),
    }


def manifest_revision(manifest: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(manifest, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode(
            # Same reason as post_revision: provider text can contain an
            # unpaired surrogate, and a package must not become unhashable
            # because one article does.
            errors="surrogatepass"
        )
    ).hexdigest()


async def ensure_package(
    ctx: ToolExecutionContext,
    assignment: EditorialAssignment,
    provider_revision: str,
    admin_url: str,
) -> EditorialReviewPackage:
    manifest = package_manifest(assignment, provider_revision, admin_url)
    manifest["evidence"] = await evidence_snapshot(
        ctx, assignment, assignment.evidence_tool_call_ids
    )
    revision = manifest_revision(manifest)
    row = await ctx.session.scalar(
        select(EditorialReviewPackage).where(
            EditorialReviewPackage.assignment_id == assignment.id,
            EditorialReviewPackage.revision == revision,
        )
    )
    if row is None:
        row = EditorialReviewPackage(
            workspace_id=ctx.workspace_id,
            assignment_id=assignment.id,
            editorial_version=assignment.editorial_version,
            revision=revision,
            manifest_json=manifest,
        )
        ctx.session.add(row)
        await ctx.session.flush()
    return row


async def evidence_snapshot(
    ctx: ToolExecutionContext, assignment: EditorialAssignment, references: list[str]
) -> list[dict[str, Any]]:
    """Only completed native retrieval receipts in this assignment's task lineage."""
    result = []
    allowed = {
        "web.fetch",
        "ghost.archive.sync",
        "ghost.archive.status",
        "ghost.archive.search",
        "ghost.archive.read",
        "unsplash.photos.select",
    }
    for identifier in sorted(set(references)):
        call = await ctx.session.scalar(
            select(ToolCall)
            .where(
                ToolCall.workspace_id == ctx.workspace_id,
                ToolCall.id == UUID(identifier),
                ToolCall.status == "completed",
                ToolCall.tool_name.in_(allowed),
                ToolCall.agent_id.in_((assignment.writer_agent_id, assignment.publisher_agent_id)),
            )
            .execution_options(populate_existing=True)
        )
        if call is None:
            raise GhostApiError(
                "Source evidence is unavailable or not a completed native retrieval",
                code="ghost_evidence_invalid",
            )
        run = await ctx.session.get(AgentRun, call.run_id)
        task = await ctx.session.get(Task, run.task_id) if run and run.task_id else None
        lineage = bool(
            task
            and task.workspace_id == ctx.workspace_id
            and (
                task.id == assignment.task_id
                or task.metadata_json.get("editorial_assignment_id") == str(assignment.id)
                or task.metadata_json.get("work_request", {}).get("editorial_assignment_id")
                == str(assignment.id)
            )
        )
        bound = call.sanitized_input_json.get("assignment_id")
        if not lineage or (call.tool_name != "web.fetch" and bound != str(assignment.id)):
            raise GhostApiError(
                "Source evidence belongs to another assignment", code="ghost_evidence_invalid"
            )
        result.append(
            {
                "tool_call_id": str(call.id),
                "tool_name": call.tool_name,
                "input": call.sanitized_input_json,
                "output": call.sanitized_output_json,
                "completed_at": call.completed_at.isoformat() if call.completed_at else None,
            }
        )
    return result


async def attach_evidence(ctx: ToolExecutionContext, payload: BaseModel) -> AssignmentOutput:
    data = cast(AssignmentEvidenceInput, payload)
    row = await require_assignment(ctx, data.assignment_id, data.connection_id)
    if row.version != data.expected_version:
        raise GhostApiError("Assignment version changed", code="ghost_assignment_stale")
    references = sorted({str(identifier) for identifier in data.evidence_tool_call_ids})
    await evidence_snapshot(ctx, row, references)
    if references != sorted(row.evidence_tool_call_ids):
        row.evidence_tool_call_ids = references
        row.editorial_version += 1
        row.version += 1
    await ctx.session.flush()
    return assignment_output(row)


async def require_research(
    ctx: ToolExecutionContext, assignment: EditorialAssignment, post: dict[str, Any] | None = None
) -> None:
    # Set only by the server on new assignments, outside model-editable brief.
    if not assignment.decision_provenance.get("research_required"):
        return
    evidence = await evidence_snapshot(ctx, assignment, assignment.evidence_tool_call_ids)
    archive = []
    for item in evidence:
        output = item["output"]
        data = output.get("data", output)
        if (
            item["tool_name"] in {"ghost.archive.sync", "ghost.archive.status"}
            and isinstance(data, dict)
            and data.get("status") == "complete"
            and data.get("failed") == 0
            and isinstance(data.get("indexed"), int)
            and data.get("indexed") == data.get("discovered")
            and data.get("corpus_hash")
        ):
            from jhin_db.models.blog_corpus import BlogCorpusSync

            try:
                sync_id = UUID(str(data.get("sync_id")))
            except ValueError:
                continue
            sync = await ctx.session.scalar(
                select(BlogCorpusSync)
                .where(
                    BlogCorpusSync.id == sync_id,
                    BlogCorpusSync.workspace_id == ctx.workspace_id,
                    BlogCorpusSync.assignment_id == assignment.id,
                    BlogCorpusSync.connection_id == assignment.connection_id,
                )
                .execution_options(populate_existing=True)
            )
            if (
                sync is not None
                and sync.status == "complete"
                and sync.corpus_hash == data["corpus_hash"]
                and sync.indexed == data["indexed"]
                and sync.failed == 0
            ):
                archive.append(data)
    if not archive or not any(item["tool_name"] == "web.fetch" for item in evidence):
        raise GhostApiError(
            "Complete archive research and fetched source receipts are required before review",
            code="ghost_research_incomplete",
        )
    if post is not None and assignment.brief_json.get("image_mode", "none") != "none":
        selected = []
        for item in evidence:
            output = item["output"]
            data = output.get("data", output)
            if (
                item["tool_name"] == "unsplash.photos.select"
                and isinstance(data, dict)
                and data.get("tracking_status") == "confirmed"
            ):
                selected.append(data)
        if not any(
            asset.get("image_url") == post.get("feature_image")
            and post.get("feature_image_alt")
            and asset.get("attribution_html")
            and str(asset["attribution_html"]) in str(post.get("feature_image_caption", ""))
            for asset in selected
        ):
            raise GhostApiError(
                "Image-required brief needs a selected, credited photo receipt matching the draft",
                code="ghost_image_evidence_incomplete",
            )


ASSIGNMENT_TOOLS = tuple(
    (
        ToolDefinition(
            name=name,
            description=description,
            risk=RiskLevel.WRITE if write else RiskLevel.READ,
            input_model=model,
            output_model=AssignmentOutput,
            required_capability=name,
            scope_keys=("connection_id",),
            defers_scope=True,
            supports_approval=write,
            redispatch_is_safe=True,
        ),
        executor,
    )
    for name, description, model, executor, write in (
        (
            "ghost.assignment.create",
            "Persist this task's editorial brief and designated reviewer. "
            "Always starts draft-only; agents cannot grant publication intent.",
            AssignmentCreateInput,
            create_assignment,
            True,
        ),
        (
            "ghost.assignment.read",
            "Read the current authorized editorial assignment and versions.",
            AssignmentReadInput,
            read_assignment,
            False,
        ),
        (
            "ghost.assignment.revise",
            "Revise the brief using its current version; invalidates prior readiness and approval.",
            AssignmentReviseInput,
            revise_assignment,
            True,
        ),
        (
            "ghost.assignment.cancel",
            "Cancel an editorial assignment; queued writes and publication are denied.",
            AssignmentCancelInput,
            cancel_assignment,
            True,
        ),
        (
            "ghost.assignment.attach_evidence",
            "Bind completed native source/archive/photo ToolCall receipts to this assignment. "
            "Fetched evidence is verified by the server; changed references invalidate approval.",
            AssignmentEvidenceInput,
            attach_evidence,
            True,
        ),
    )
)
