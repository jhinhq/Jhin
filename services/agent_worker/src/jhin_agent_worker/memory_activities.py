"""Activities behind MemoryMaintenanceWorkflow (experience design, Memory).

``extract_memory_candidates`` loads a *bounded, visible-only* source text
(one message plus its recent visible context, or a completed task's
structured outcome), resolves the agent's model profile, and asks the model
for strict structured candidates. ``apply_memory_candidates`` runs the
deterministic policy (``jhin_memory``) and persists survivors.

Neither activity ever touches INTERNAL messages, run transcripts, hidden
reasoning, or credentials beyond the provider key decrypted at the moment of
use. Both return typed results instead of raising for expected failures, so
the workflow (and the originating chat/task) never fails because of memory.

Worker integration points for the Phase 10 activities merge:

- before a model step: ``jhin_memory.build_memory_context`` → append
  ``MemoryContext.text`` to the system prompt and log the provenance with
  ``jhin_memory.record_retrieval_provenance`` (run event ``memory.retrieved``);
- after a visible agent reply / task completion:
  ``jhin_workflows.memory_maintenance.start_memory_maintenance`` (best-effort).
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio import activity

from jhin_agent_worker.resources import Resources
from jhin_agents.snapshot import SnapshotError, resolve_snapshot
from jhin_db.models import Agent, AuditEvent, MemoryRecord, Message, Task
from jhin_domain import ActorType, MemoryScope, MessageVisibility, SenderType
from jhin_memory import (
    ActorFacts,
    ExtractionResult,
    MemoryCandidate,
    apply_candidates,
    derive_source_facts,
    extract_candidates,
    resolve_memory_embedder,
)
from jhin_models import build_model_client
from jhin_observability import get_logger
from jhin_secrets import SecretStore
from jhin_secrets.redaction import redact_text
from jhin_workflows.memory_maintenance import (
    ACTIVITY_APPLY_MEMORY_CANDIDATES,
    ACTIVITY_EXTRACT_MEMORY_CANDIDATES,
    SOURCE_KIND_MESSAGE,
    SOURCE_KIND_TASK_OUTCOME,
    ApplyMemoryCandidatesInput,
    ApplyMemoryCandidatesResult,
    ExtractMemoryCandidatesInput,
    ExtractMemoryCandidatesResult,
)

logger = get_logger(__name__)

MAX_CONTEXT_MESSAGES = 12
MAX_SOURCE_CHARS = 12_000
MEMORY_MAINTAINED_AUDIT_ACTION = "memory.maintained"


def _message_text(message: Message) -> str:
    content = message.content_json or {}
    for key in ("text", "summary", "instructions", "body"):
        value = content.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    try:
        return json.dumps(content, sort_keys=True)[:1_000]
    except (TypeError, ValueError):
        return ""


def _render_messages(messages: list[Message], agent_id: UUID) -> str:
    lines: list[str] = []
    for message in messages:
        if message.sender_type == SenderType.AGENT.value:
            role = "assistant" if message.sender_id == agent_id else "other_agent"
        elif message.sender_type == SenderType.USER.value:
            role = "user"
        else:
            role = "system"
        text = _message_text(message)
        if text:
            lines.append(f"{role}: {text}")
    return "\n".join(lines)


async def load_source_text(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    agent_id: UUID,
    source_kind: str,
    source_id: UUID,
) -> tuple[str, dict[str, str]] | None:
    """Bounded, visible-only source text plus resolved source references.

    Returns ``None`` when the source does not exist in the workspace or is
    not visible (INTERNAL messages are never memory sources).
    """
    refs: dict[str, str] = {}
    if source_kind == SOURCE_KIND_MESSAGE:
        message = await session.scalar(
            select(Message).where(Message.id == source_id, Message.workspace_id == workspace_id)
        )
        if message is None or message.visibility != MessageVisibility.VISIBLE.value:
            return None
        if message.task_id is not None:
            refs["task_id"] = str(message.task_id)
        if message.conversation_id is not None:
            refs["conversation_id"] = str(message.conversation_id)
        query = select(Message).where(
            Message.workspace_id == workspace_id,
            Message.visibility == MessageVisibility.VISIBLE.value,
            Message.created_at <= message.created_at,
        )
        if message.conversation_id is not None:
            query = query.where(Message.conversation_id == message.conversation_id)
        elif message.task_id is not None:
            query = query.where(Message.task_id == message.task_id)
        else:
            query = query.where(Message.id == message.id)
        rows = list(
            await session.scalars(
                query.order_by(Message.created_at.desc(), Message.id.desc()).limit(
                    MAX_CONTEXT_MESSAGES
                )
            )
        )
        rows.reverse()
        if all(row.id != message.id for row in rows):
            rows.append(message)
        text = _render_messages(rows, agent_id)
    elif source_kind == SOURCE_KIND_TASK_OUTCOME:
        task = await session.scalar(
            select(Task).where(Task.id == source_id, Task.workspace_id == workspace_id)
        )
        if task is None:
            return None
        refs["task_id"] = str(task.id)
        if task.conversation_id is not None:
            refs["conversation_id"] = str(task.conversation_id)
        parts = [f"task: {task.title}"]
        if task.description:
            parts.append(f"description: {task.description[:2_000]}")
        result = task.metadata_json.get("result") if task.metadata_json else None
        if isinstance(result, dict):
            parts.append("outcome: " + json.dumps(result, sort_keys=True)[:3_000])
        rows = list(
            await session.scalars(
                select(Message)
                .where(
                    Message.workspace_id == workspace_id,
                    Message.task_id == task.id,
                    Message.visibility == MessageVisibility.VISIBLE.value,
                )
                .order_by(Message.created_at.desc(), Message.id.desc())
                .limit(MAX_CONTEXT_MESSAGES)
            )
        )
        rows.reverse()
        rendered = _render_messages(rows, agent_id)
        if rendered:
            parts.append(rendered)
        text = "\n".join(parts)
    else:
        return None
    # Defence in depth: the source may contain a pasted credential; the
    # extraction prompt forbids copying it and policy screens candidates, but
    # we also never send known secret values to the model.
    return redact_text(text)[-MAX_SOURCE_CHARS:], refs


class MemoryActivities:
    def __init__(self, resources: Resources) -> None:
        self._resources = resources
        self._metrics = resources.runtime.metrics
        self._tracer = resources.runtime.tracer

    @activity.defn(name=ACTIVITY_EXTRACT_MEMORY_CANDIDATES)
    async def extract_memory_candidates_activity(
        self, params: ExtractMemoryCandidatesInput
    ) -> ExtractMemoryCandidatesResult:
        workspace_id = UUID(params.workspace_id)
        agent_id = UUID(params.agent_id)
        async with self._resources.session_factory() as session:
            loaded = await load_source_text(
                session,
                workspace_id=workspace_id,
                agent_id=agent_id,
                source_kind=params.source_kind,
                source_id=UUID(params.source_id),
            )
            if loaded is None:
                return ExtractMemoryCandidatesResult(ok=False, error="source_not_visible")
            source_text, _refs = loaded
            if not source_text.strip():
                return ExtractMemoryCandidatesResult(ok=True, candidates_json=[])

            try:
                snapshot = await resolve_snapshot(session, workspace_id, agent_id)
            except SnapshotError as exc:
                return ExtractMemoryCandidatesResult(ok=False, error=f"snapshot:{exc.code}")

            api_key: str | None = None
            if snapshot.model_profile.secret_id is not None:
                try:
                    api_key = await SecretStore(session, self._resources.crypto).reveal(
                        workspace_id, snapshot.model_profile.secret_id
                    )
                except Exception as exc:
                    return ExtractMemoryCandidatesResult(
                        ok=False, error=f"secret:{type(exc).__name__}"
                    )
            try:
                client = build_model_client(
                    snapshot.model_profile.provider_type,
                    base_url=snapshot.model_profile.base_url,
                    api_key=api_key,
                    metrics=self._metrics,
                    tracer=self._tracer,
                )
            except Exception as exc:
                return ExtractMemoryCandidatesResult(
                    ok=False, error=redact_text(f"provider_config:{exc}")[:200]
                )
            del api_key

            try:
                result: ExtractionResult = await extract_candidates(
                    client,
                    model=snapshot.model_profile.model_name,
                    source_text=source_text,
                    agent_name=snapshot.name,
                )
            finally:
                await client.close()

        return ExtractMemoryCandidatesResult(
            ok=result.ok,
            candidates_json=[c.model_dump(mode="json") for c in result.candidates],
            error=redact_text(result.error)[:200],
            model=snapshot.model_profile.model_name,
            source_chars=len(source_text),
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
        )

    async def _embed_created(
        self,
        session: AsyncSession,
        *,
        workspace_id: UUID,
        agent_id: UUID,
        records: list[MemoryRecord],
    ) -> int:
        """Best-effort embeddings for newly persisted live records. A failure
        (no embedding profile, provider error) leaves the records without an
        embedding and is logged by the embedder; it never fails the apply."""
        if not records:
            return 0
        embedder = await resolve_memory_embedder(
            session,
            self._resources.crypto,
            workspace_id=workspace_id,
            agent_id=agent_id,
            metrics=self._metrics,
            tracer=self._tracer,
        )
        if embedder is None:
            return 0
        try:
            return await embedder.embed_records(session, records, workspace_id=workspace_id)
        finally:
            await embedder.close()

    @activity.defn(name=ACTIVITY_APPLY_MEMORY_CANDIDATES)
    async def apply_memory_candidates_activity(
        self, params: ApplyMemoryCandidatesInput
    ) -> ApplyMemoryCandidatesResult:
        workspace_id = UUID(params.workspace_id)
        agent_id = UUID(params.agent_id)
        try:
            candidates = [MemoryCandidate.model_validate(c) for c in params.candidates_json]
        except ValidationError as exc:
            return ApplyMemoryCandidatesResult(
                ok=False, error=f"invalid_candidates:{exc.error_count()}"
            )

        requested_scope: MemoryScope | None = None
        if params.remember_enabled and params.requested_scope:
            try:
                requested_scope = MemoryScope(params.requested_scope)
            except ValueError:
                return ApplyMemoryCandidatesResult(ok=False, error="invalid_requested_scope")
            candidates = [
                c.model_copy(update={"requested_scope": requested_scope}) for c in candidates
            ]

        async with self._resources.session_factory() as session:
            agent = await session.scalar(
                select(Agent).where(Agent.id == agent_id, Agent.workspace_id == workspace_id)
            )
            if agent is None:
                return ApplyMemoryCandidatesResult(ok=False, error="agent_not_found")
            source = await derive_source_facts(
                session,
                workspace_id=workspace_id,
                agent_id=agent_id,
                message_id=UUID(params.source_id)
                if params.source_kind == SOURCE_KIND_MESSAGE
                else None,
                task_id=(
                    UUID(params.source_id)
                    if params.source_kind == SOURCE_KIND_TASK_OUTCOME
                    else UUID(params.task_id)
                    if params.task_id
                    else None
                ),
                conversation_id=UUID(params.conversation_id) if params.conversation_id else None,
            )
            if source is None:
                return ApplyMemoryCandidatesResult(ok=False, error="source_not_found")

            if params.remember_enabled and params.actor_user_id:
                try:
                    authority = MemoryScope(params.actor_authority or "agent")
                except ValueError:
                    authority = MemoryScope.AGENT
                actor = ActorFacts(
                    actor_type=ActorType.USER,
                    actor_id=UUID(params.actor_user_id),
                    explicit=True,
                    authority=authority,
                )
            else:
                actor = ActorFacts(actor_type=ActorType.AGENT, actor_id=agent_id)

            applied = await apply_candidates(
                session, candidates=candidates, source=source, actor=actor
            )
            summary: dict[str, Any] = applied.summary()
            summary["embedded"] = await self._embed_created(
                session, workspace_id=workspace_id, agent_id=agent_id, records=applied.created
            )
            session.add(
                AuditEvent(
                    workspace_id=workspace_id,
                    actor_type=ActorType.SYSTEM.value,
                    actor_id=None,
                    action=MEMORY_MAINTAINED_AUDIT_ACTION,
                    target_type="agent",
                    target_id=agent_id,
                    metadata_json={
                        "source_kind": params.source_kind,
                        "source_id": params.source_id,
                        "idempotency_key": params.idempotency_key,
                        "remember_enabled": params.remember_enabled,
                        **summary,
                    },
                )
            )
            await session.commit()

        logger.info(
            "memory.maintained",
            workspace_id=str(workspace_id),
            agent_id=str(agent_id),
            source_kind=params.source_kind,
            activated=summary["activated"],
            proposed=summary["proposed"],
            rejected=summary["rejected"],
        )
        return ApplyMemoryCandidatesResult(
            ok=True,
            created_ids=summary["created"],
            activated=summary["activated"],
            proposed=summary["proposed"],
            contested=summary["contested"],
            rejected=summary["rejected"],
            duplicates=summary["duplicates"],
            reasons=summary["reasons"],
        )
