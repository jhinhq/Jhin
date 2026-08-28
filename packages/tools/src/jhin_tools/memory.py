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
    # The reason codes above are for audit; on their own they taught agents to
    # repeat words like "non_amplification" back to the person who asked. This
    # says what happened and what would work instead.
    detail: str = ""


# Plain language for the outcomes an agent can actually do something about.
# Anything unlisted falls back to the generic line below, which is still a
# sentence rather than a code.
_REASON_DETAIL: dict[str, str] = {
    "non_amplification": (
        "Not saved. This conversation is only visible to you and the person in "
        "it, so what is said here can become your own memory but not the "
        "team's. Propose it again with requested_scope 'agent' to remember it "
        "yourself, and tell the person that a team-wide memory has to be added "
        "by someone on the Memories page."
    ),
    "insufficient_authority": (
        "Not saved: that scope is wider than the person asking is allowed to "
        "set. Remember it at 'agent' scope instead, and say who would need to "
        "record it more widely."
    ),
    "no_team_for_scope": (
        "Not saved: you are not on a team, so there is no team memory to add "
        "to. Propose it with requested_scope 'agent' instead."
    ),
    "no_agent_for_scope": "Not saved: this memory has no agent to belong to.",
    "low_information": (
        "Not saved: too vague to be useful later. Only propose something a "
        "colleague could act on months from now without this conversation."
    ),
    "self_reference": ("Not saved: it describes this conversation rather than a durable fact."),
    "source_internal": ("Not saved: it came from hidden reasoning, which never becomes memory."),
    "duplicate": "Already remembered; nothing new was stored.",
    "near_duplicate": "Already remembered in nearly these words; nothing new was stored.",
    "adjudicated_same": "Already remembered; nothing new was stored.",
    "contradiction": (
        "Not saved: it contradicts something already remembered. Say so to the "
        "person rather than overwriting it yourself."
    ),
    "workspace_promotion_requires_review": (
        "Saved, but a person has to approve it before it becomes "
        "workspace-wide. Tell them it is waiting for review."
    ),
}


def _propose_detail(outcome: str, status: str, reasons: tuple[str, ...] | list[str]) -> str:
    for reason in reasons:
        detail = _REASON_DETAIL.get(reason)
        if detail:
            return detail
    if outcome == "reject":
        return "Not saved, and the reason is not one you can act on."
    if status == "active":
        return "Remembered."
    return "Recorded."


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
        outcome=decision.outcome,
        status=status,
        memory_id=record_id,
        reasons=list(decision.reasons),
        detail=_propose_detail(decision.outcome, status, decision.reasons),
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
                "Propose one concise, durable memory from the current task. "
                "**When a person corrects something you have remembered, or "
                "tells you a stored fact has changed, call this** with the new "
                'wording -- saying "got it, I\'ll use that from now on" '
                "records nothing, and you will state the old value again in the "
                "next conversation. A correction supersedes the memory it "
                "replaces; propose it the same way you proposed the original. "
                "Use "
                "requested_scope 'agent' unless you know the source was wider: "
                "an ordinary chat with a person is private to the two of you, "
                "so it can only become your own memory. Team memory needs a "
                "team-visible source such as work shared with a teammate, and "
                "workspace memory is queued for human review. A rejected "
                "proposal comes back with a `detail` sentence saying what would "
                "work instead -- relay that, not the reason codes. Never "
                "include secrets or credentials."
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
