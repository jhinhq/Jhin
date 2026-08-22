"""Built-in memory tools: ``memory.search`` and ``memory.propose``.

Both are scoped to the *calling* agent by construction: the executor uses
``ctx.agent_id`` and the agent's live team memberships as the authorization
subject, so another agent's private memory can never be returned, and a
proposal is routed through deterministic policy with the current task as its
source (the model cannot pick a source, activate workspace memory, or
broaden visibility).
"""

from __future__ import annotations

from typing import cast

from pydantic import BaseModel, ConfigDict, Field

from jhin_domain import ActorType, MemoryKind, MemoryScope
from jhin_memory import (
    ActorFacts,
    MemoryCandidate,
    apply_candidates,
    build_memory_context,
    derive_source_facts,
)
from jhin_memory.types import MAX_CANDIDATE_CHARS
from jhin_policy import MEMORY_PROPOSE_CAPABILITY, MEMORY_READ_CAPABILITY, RiskLevel, ToolDefinition
from jhin_tools.builtin import ToolExecutionContext, ToolExecutor, ToolValidator
from jhin_tools.errors import ToolExecutionError

_SEARCH_MAX_CHARS = 4_000


class MemorySearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=2_000)
    limit: int = Field(default=8, ge=1, le=20)


class MemorySearchItem(BaseModel):
    id: str
    version: int
    kind: str
    scope: str
    status: str
    content: str
    pinned: bool


class MemorySearchOutput(BaseModel):
    items: list[MemorySearchItem]
    mode: str
    degraded: bool
    context_hash: str


async def _memory_search(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    data = cast(MemorySearchInput, payload)
    context = await build_memory_context(
        ctx.session,
        workspace_id=ctx.workspace_id,
        agent_id=ctx.agent_id,
        query=data.query,
        max_records=data.limit,
        max_chars=_SEARCH_MAX_CHARS,
    )
    return MemorySearchOutput(
        items=[
            MemorySearchItem(
                id=str(item.id),
                version=item.version,
                kind=item.kind.value,
                scope=item.scope.value,
                status=item.status.value,
                content=item.content,
                pinned=item.pinned,
            )
            for item in context.items
        ],
        mode=context.provenance.mode,
        degraded=context.provenance.degraded,
        context_hash=context.provenance.context_hash,
    )


class MemoryProposeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1, max_length=MAX_CANDIDATE_CHARS)
    kind: MemoryKind = MemoryKind.FACT
    subject: str | None = Field(default=None, max_length=200)
    tags: list[str] = Field(default_factory=list, max_length=10)
    confidence: float = Field(default=0.7, ge=0.0, le=1.0)
    importance: float = Field(default=0.5, ge=0.0, le=1.0)
    requested_scope: MemoryScope = MemoryScope.AGENT


class MemoryProposeOutput(BaseModel):
    outcome: str
    status: str
    memory_id: str
    reasons: list[str]


async def _memory_propose(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    data = cast(MemoryProposeInput, payload)
    source = await derive_source_facts(
        ctx.session, workspace_id=ctx.workspace_id, agent_id=ctx.agent_id, task_id=ctx.task_id
    )
    if source is None:
        raise ToolExecutionError(
            "current task not found", code="memory_source_missing", side_effect_possible=False
        )
    candidate = MemoryCandidate(
        content=data.content,
        kind=data.kind,
        subject=data.subject,
        tags=tuple(data.tags),
        confidence=data.confidence,
        importance=data.importance,
        requested_scope=data.requested_scope,
    )
    result = await apply_candidates(
        ctx.session,
        candidates=[candidate],
        source=source,
        actor=ActorFacts(actor_type=ActorType.AGENT, actor_id=ctx.agent_id),
    )
    decision = result.decisions[0]
    record_id = ""
    status = "none"
    if result.created:
        record_id = str(result.created[0].id)
        status = result.created[0].status
    elif decision.duplicate_of is not None:
        record_id = str(decision.duplicate_of)
        status = "duplicate"
    return MemoryProposeOutput(
        outcome=decision.outcome, status=status, memory_id=record_id, reasons=list(decision.reasons)
    )


MEMORY_TOOLS: tuple[tuple[ToolDefinition, ToolExecutor, ToolValidator | None], ...] = (
    (
        ToolDefinition(
            name="memory.search",
            description=(
                "Search your curated long-term memory (your private memory, your "
                "teams' memory, and company knowledge) for records relevant to a "
                "query. Returns only records you are authorized to see right now."
            ),
            risk=RiskLevel.READ,
            input_model=MemorySearchInput,
            output_model=MemorySearchOutput,
            required_capability=MEMORY_READ_CAPABILITY,
        ),
        _memory_search,
        None,
    ),
    (
        ToolDefinition(
            name="memory.propose",
            description=(
                "Propose one concise, durable memory from the current task. Private "
                "(agent) memory activates automatically; team memory activates only "
                "when the task was team-visible; workspace memory is queued for "
                "human review. Never include secrets or credentials."
            ),
            risk=RiskLevel.WRITE,
            input_model=MemoryProposeInput,
            output_model=MemoryProposeOutput,
            required_capability=MEMORY_PROPOSE_CAPABILITY,
            supports_approval=True,
        ),
        _memory_propose,
        None,
    ),
)
