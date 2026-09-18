"""Live standing-authority checks shared by tools and background extraction.

The class proposed by a model is a hint, never authority. Exact source evidence,
current grants, membership, grantor role, time and destination must all match.
"""

import json
import re
from datetime import UTC, datetime
from fnmatch import fnmatchcase
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_db.memberships import active_team_ids
from jhin_db.models import AgentCapabilityGrant, Message, User, WorkspaceMembership
from jhin_db.models.memory_capture import MemoryCapturePolicy
from jhin_domain import MemoryScope
from jhin_memory.types import ActorFacts, MemoryCandidate, SourceFacts


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _matches(granted: Any, requested: str) -> bool:
    if isinstance(granted, list):
        return any(_matches(value, requested) for value in granted)
    return isinstance(granted, str) and fnmatchcase(requested, granted)


async def capture_write_allowed(
    session: AsyncSession,
    source: SourceFacts,
    scope: MemoryScope,
    scope_id: UUID,
) -> bool:
    if source.agent_id is None:
        return False
    if scope is MemoryScope.AGENT and scope_id != source.agent_id:
        return False
    if scope is MemoryScope.WORKSPACE and scope_id != source.workspace_id:
        return False
    if scope is MemoryScope.TEAM and scope_id not in await active_team_ids(
        session, source.workspace_id, source.agent_id
    ):
        return False
    grants = await session.scalars(
        select(AgentCapabilityGrant)
        .where(
            AgentCapabilityGrant.workspace_id == source.workspace_id,
            AgentCapabilityGrant.agent_id == source.agent_id,
        )
        .execution_options(populate_existing=True)
    )
    dimensions = {"requested_scope": scope.value, "scope": scope.value, "scope_id": str(scope_id)}
    allowed = False
    for grant in grants:
        if not fnmatchcase("memory.propose", grant.capability):
            continue
        if not all(
            key in dimensions and _matches(value, dimensions[key])
            for key, value in grant.scope_json.items()
        ):
            continue
        if grant.effect == "deny":
            return False
        allowed = allowed or grant.effect == "allow"
    return allowed


def eligible_capture(candidate: MemoryCandidate) -> bool:
    """Conservative privacy veto before considering a standing class grant."""
    text = candidate.content.casefold()
    if re.search(
        r"\b(private|personal|confidential|performance feedback|keep .{0,20}between)\b", text
    ):
        return False
    return candidate.capture_class in {
        "editorial_style",
        "recurring_preference",
        "company_fact",
        "editorial_lesson",
    }


async def resolve_capture_actor(
    session: AsyncSession,
    candidate: MemoryCandidate,
    source: SourceFacts,
    actor: ActorFacts,
    evidence: dict[str, Any] | None,
    *,
    now: datetime,
) -> ActorFacts:
    if not eligible_capture(candidate) or source.internal or candidate.scope_id is None:
        return actor
    if not evidence:
        return actor
    if not await capture_write_allowed(
        session, source, candidate.requested_scope, candidate.scope_id
    ):
        return actor
    source_user_id = None
    source_agent_id = None
    if evidence.get("kind") == "approved_editorial_review":
        source_agent_id = evidence["reviewer_agent_id"]
        source_created_at = datetime.fromisoformat(evidence["decided_at"])
        source_conversation_id = (
            UUID(evidence["conversation_id"]) if evidence["conversation_id"] else None
        )
        source_id = UUID(evidence["review_id"])
    elif (
        evidence.get("kind") == "human_statement" and candidate.capture_class != "editorial_lesson"
    ):
        message = await session.scalar(
            select(Message)
            .where(
                Message.id == UUID(evidence["message_id"]),
                Message.workspace_id == source.workspace_id,
                Message.visibility == "visible",
                Message.sender_type == "user",
            )
            .execution_options(populate_existing=True)
        )
        if message is None or not eligible_capture(
            candidate.model_copy(
                update={"content": json.dumps(message.content_json, ensure_ascii=False)}
            )
        ):
            return actor
        source_user_id = message.sender_id
        source_created_at = message.created_at
        source_conversation_id = message.conversation_id
        source_id = message.id
    else:
        return actor
    policies = await session.scalars(
        select(MemoryCapturePolicy)
        .where(
            MemoryCapturePolicy.workspace_id == source.workspace_id,
            MemoryCapturePolicy.scope == candidate.requested_scope.value,
            MemoryCapturePolicy.scope_id == candidate.scope_id,
            MemoryCapturePolicy.revoked_at.is_(None),
        )
        .order_by(MemoryCapturePolicy.created_at, MemoryCapturePolicy.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    for policy in policies:
        if (
            str(source.agent_id) not in policy.actor_ids_json
            or candidate.capture_class not in policy.allowed_classes_json
            or (source_agent_id is None and source_user_id != policy.source_user_id)
            or (
                source_agent_id is not None
                and source_agent_id not in policy.allowed_source_agent_ids_json
            )
            or (
                policy.source_conversation_id is not None
                and source_conversation_id != policy.source_conversation_id
            )
            or _utc(source_created_at) <= _utc(policy.effective_from)
            or _utc(now) < _utc(policy.effective_from)
            or (policy.expires_at is not None and _utc(now) >= _utc(policy.expires_at))
        ):
            continue
        if policy.source_after_message_id is not None:
            boundary = await session.get(Message, policy.source_after_message_id)
            if (
                boundary is None
                or boundary.workspace_id != source.workspace_id
                or source_conversation_id != boundary.conversation_id
                or (_utc(source_created_at), source_id.int)
                <= (_utc(boundary.created_at), boundary.id.int)
            ):
                continue
        role = await session.scalar(
            select(WorkspaceMembership.role)
            .join(User, User.id == WorkspaceMembership.user_id)
            .where(
                WorkspaceMembership.workspace_id == source.workspace_id,
                WorkspaceMembership.user_id == policy.granted_by_user_id,
                User.status == "active",
            )
        )
        if role not in {"owner", "admin"}:
            continue
        return actor.model_copy(
            update={
                "capture_policy_id": policy.id,
                "capture_scope": candidate.requested_scope,
                "capture_scope_id": candidate.scope_id,
            }
        )
    return actor


async def available_capture_policies(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    agent_id: UUID,
) -> list[dict[str, Any]]:
    """Content-free planning hints. Every write resolves authority again."""
    now = datetime.now(UTC)
    rows = await session.scalars(
        select(MemoryCapturePolicy)
        .where(
            MemoryCapturePolicy.workspace_id == workspace_id,
            MemoryCapturePolicy.revoked_at.is_(None),
        )
        .order_by(MemoryCapturePolicy.created_at.desc())
        .limit(200)
    )
    available = []
    source = SourceFacts(workspace_id=workspace_id, agent_id=agent_id)
    for row in rows:
        if (
            str(agent_id) not in row.actor_ids_json
            or _utc(row.effective_from) > now
            or (row.expires_at is not None and _utc(row.expires_at) <= now)
        ):
            continue
        if not await capture_write_allowed(session, source, MemoryScope(row.scope), row.scope_id):
            continue
        role = await session.scalar(
            select(WorkspaceMembership.role)
            .join(User, User.id == WorkspaceMembership.user_id)
            .where(
                WorkspaceMembership.workspace_id == workspace_id,
                WorkspaceMembership.user_id == row.granted_by_user_id,
                User.status == "active",
            )
        )
        if role not in {"owner", "admin"}:
            continue
        available.append(
            {
                "policy_id": str(row.id),
                "requested_scope": row.scope,
                "scope_id": str(row.scope_id),
                "allowed_classes": row.allowed_classes_json,
                "allowed_source_agent_ids": row.allowed_source_agent_ids_json,
                "source_user_id": str(row.source_user_id),
                "source_conversation_id": str(row.source_conversation_id)
                if row.source_conversation_id
                else None,
                "effective_from": _utc(row.effective_from).isoformat(),
            }
        )
    return available
