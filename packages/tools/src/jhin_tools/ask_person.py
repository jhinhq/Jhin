"""``organization.ask_person``: the one way an agent can ask a person something.

Everything else an agent says to a person is an answer. This is the inverse —
the agent puts a small, bounded choice on the person's screen and holds its
turn open until they pick one or type their own words. It exists because the
alternative is guessing, and a guess filed as a memory is worse than a
question: "we deploy on Mondays" is either this team's practice or the whole
company's, and only the person knows which.

Three things make it safe to give every agent:

- it reaches nobody outside a chat a person opened (``validate_ask_person``);
- it is bounded — three questions a run, six an hour in one conversation, and
  a repeat is refused without reaching anyone;
- the *authority* an answer carries is written by the API from the answering
  person's RBAC (``user_question.granted_scope``), never by anything the model
  says about the conversation.

The executor writes the durable ``user_question`` row and the chat message
that renders it. The park, the resume, and the observation belong to the
workflow and the agent worker (``ASK_PERSON_WAIT_PATCH``,
``deliver_question_answer``).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from jhin_db.models import Agent, AuditEvent, Conversation, Message, Task, Team, UserQuestion
from jhin_domain import (
    ActorType,
    MemoryScope,
    MessageType,
    MessageVisibility,
    RecipientType,
    SenderType,
    UserQuestionStatus,
    new_uuid7,
    structured_content,
)
from jhin_memory.policy import normalize_content
from jhin_policy import (
    ASK_PERSON_CAPABILITY,
    DecisionType,
    Grant,
    PolicyDecision,
    RiskLevel,
    ToolDefinition,
)
from jhin_tools.builtin import (
    ToolExecutionContext,
    ToolExecutor,
    ToolValidator,
    task_has_a_person_watching,
)

# How long the question stays live. It matches ``_PERSON_ANSWER_WAIT`` in the
# agent-task workflow on purpose: the row's expiry and the run's wait are the
# same promise seen from two sides, and a test pins them equal.
PERSON_ANSWER_WAIT = timedelta(minutes=30)

# One clarification is normal, a second is a follow-up, a third is the ceiling
# before asking stops being help and starts being an interview.
MAX_QUESTIONS_PER_RUN = 3
# The per-run cap alone would let three consecutive runs ask nine times. This
# is the hard stop on a loop that keeps re-asking across runs after an expiry.
MAX_QUESTIONS_PER_CONVERSATION_HOUR = 6

_CONVERSATION_WINDOW = timedelta(hours=1)

# The scopes a memory-scope question may offer. They are the MemoryScope
# values, named here so a malformed option is refused by the schema rather
# than by the memory policy three steps later.
_MEMORY_SCOPE_VALUES = frozenset(scope.value for scope in MemoryScope)

# The free-text row the card renders under the options. It is not an option
# value, which is why "other" is reserved.
_OTHER_VALUE = "other"
OTHER_LABEL = "Something else"
OTHER_PLACEHOLDER = "Tell me in your own words…"

_DETAIL_ASKED = (
    "Asked. Required questions wait until answered or the task is "
    "stopped. Optional questions wait 30 minutes, and "
    "their answer comes back as this call's result. Do not ask again, do not "
    "answer on their behalf, and do not tell them to watch for a follow-up."
)
_DETAIL_RUN_BUDGET = (
    "Not asked: you have already put three questions to this person in this "
    "run, which is the limit. Decide it yourself, say plainly what you "
    "assumed only for optional preferences. Required inputs remain blocked; report what is missing."
)
_DETAIL_CONVERSATION_BUDGET = (
    "Not asked: this conversation has had six questions in the last hour, "
    "which is the limit. Required inputs remain blocked; report what is missing."
)
_DETAIL_ALREADY_ASKED = (
    "Not asked again: this exact question is already on their screen waiting "
    "for an answer. Say that you are waiting on it rather than asking twice."
)
_DETAIL_CLOSED = (
    "Not asked: this question was already closed without an answer. Decide "
    "only if it is optional. Required inputs remain blocked; report what is missing."
)
_DETAIL_ALREADY_ANSWERED = (
    "Not asked again: you already asked this here and they answered: {answer}. Use that answer."
)

_DENY_NO_PERSON_WATCHING = (
    "this work is not a chat with a person, so there is nobody to ask; "
    "return the missing required inputs to your requester with "
    "organization.report_result; do not guess or perform dependent "
    "work"
)
_DENY_NO_TEAM_FOR_SCOPE = (
    "you are not on a team, so there is no team memory to offer; ask about "
    "company-wide instead, or remember it yourself"
)


class AskPersonOption(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1, max_length=80)
    value: str = Field(
        min_length=1,
        max_length=64,
        description="Text choice ID, or the actual timezone, HH:MM time, or URL.",
    )
    detail: str = Field(default="", max_length=140)


class AskPersonInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(
        min_length=1,
        max_length=1000,
        description="A concise question including the setup details you need.",
    )
    context: str = Field(
        default="",
        max_length=2000,
        description="Supplementary context explaining why the answer is needed.",
    )
    options: list[AskPersonOption] = Field(default_factory=list, max_length=4)
    required: bool = True
    input_key: str = Field(default="", max_length=100, pattern=r"^[a-z0-9_]*$")
    value_type: Literal["text", "url", "timezone", "time"] = "text"
    kind: Literal["open", "memory_scope"] = "open"
    allow_other: bool = True

    @model_validator(mode="before")
    @classmethod
    def _reserved_schedule_review(cls, value: Any) -> Any:
        if not isinstance(value, dict) or not str(value.get("input_key", "")).startswith(
            "schedule_activate_"
        ):
            return value
        # None of these model-authored words will be shown. Normalize before
        # field validation so copied long briefs and invented options cannot
        # cause a pointless retry. The executor separately validates the key
        # and builds the full review from the locked, persisted schedule.
        return {
            **value,
            "question": "Activate this recurring work?",
            "context": "",
            "options": [],
            "required": False,
            "value_type": "text",
            "kind": "open",
            "allow_other": True,
        }

    @model_validator(mode="before")
    @classmethod
    def _typed_choices(cls, value: Any) -> Any:
        if not isinstance(value, dict) or value.get("value_type", "text") == "text":
            return value
        value_type = value.get("value_type")
        options = value.get("options", [])
        if value_type not in {"timezone", "time", "url"} or not isinstance(options, list):
            return value
        if len(options) > 4:
            return value  # Preserve the explicit choice-count validation.
        from jhin_tools.readiness import validate_answer

        canonical = []
        seen = set()
        for option in options:
            raw = option.model_dump() if isinstance(option, AskPersonOption) else option
            if not isinstance(raw, dict) or not isinstance(raw.get("value"), str):
                continue
            try:
                actual = validate_answer(raw["value"], value_type)
            except ValueError:
                continue
            if len(actual) > 64 or actual in seen:
                continue
            # Display exactly the value that will be stored; never interpret
            # an alias or let an explanatory label misrepresent that value.
            canonical.append({"label": actual, "value": actual, "detail": ""})
            seen.add(actual)
        return {
            **value,
            "options": canonical if len(canonical) >= 2 else [],
            "allow_other": True,
        }

    @model_validator(mode="after")
    def _canonical_setup_question(self) -> AskPersonInput:
        # These answers authorize connector setup. The platform must own the
        # wording so a model cannot ask which destination to avoid, then bind it.
        if self.input_key == "ghost_admin_url":
            self.question = "What is the actual Ghost Admin URL to connect?"
            self.value_type = "url"
        elif self.input_key == "ghost_publisher_agent_id":
            self.question = "Which agent may review and publish Ghost drafts?"
            self.value_type = "text"
        elif self.input_key in ("unsplash_connection_setup", "unsplash_setup"):
            # The answer to this one binds a stored Access Key to a live
            # connection, so it belongs with the two above: the person reads
            # the platform's question, not one the asking agent composed.
            self.question = (
                "Do you want to connect Unsplash with the stored access key? "
                "Type 'connect unsplash' to confirm."
            )
            self.value_type = "text"
        else:
            return self
        self.context = ""
        self.required = True
        self.kind = "open"
        self.options = []
        self.allow_other = True
        return self

    @model_validator(mode="after")
    def _distinct_options(self) -> AskPersonInput:
        if not self.options and not self.allow_other:
            raise ValueError("A free-text question must allow a typed answer")
        if len(self.options) == 1:
            raise ValueError("Offer at least two options, or use free text")
        values = [option.value for option in self.options]
        if len(set(values)) != len(values):
            raise ValueError("option values must be unique")
        if self.value_type == "text" and any(
            re.fullmatch(r"[a-z0-9_]+", value) is None for value in values
        ):
            raise ValueError("Text option values must use lowercase letters, digits or underscores")
        return self

    @model_validator(mode="after")
    def _scope_questions_offer_scopes(self) -> AskPersonInput:
        """A scope question may only offer real scopes, the platform writes
        what each one says, and the person must always be able to escape them.

        The label is not the model's to choose. It authored both the words a
        person reads and the value the API grants, and nothing bound the two
        together -- so an option reading "Only the Engineering team" could
        carry ``workspace`` and turn one person's click into a company-wide
        memory. The value decides the words, so what is clicked is what is
        granted. A choice nobody can decline is a leading question, so the
        free-text row is not optional here either.
        """
        if self.kind != "memory_scope":
            return self
        if len(self.options) < 2:
            raise ValueError("A memory scope question must offer at least two scopes")
        canonical: list[AskPersonOption] = []
        for option in self.options:
            if option.value not in _MEMORY_SCOPE_VALUES:
                raise ValueError(
                    "a memory_scope question's option values must be "
                    "'agent', 'team', or 'workspace'"
                )
            label, detail = _SCOPE_WORDS[option.value]
            canonical.append(AskPersonOption(value=option.value, label=label, detail=detail))
        self.options = canonical
        self.allow_other = True
        return self

    @model_validator(mode="after")
    def _other_is_reserved(self) -> AskPersonInput:
        if self.kind == "open" and any(option.value == _OTHER_VALUE for option in self.options):
            raise ValueError("'other' is reserved for the free-text row; name the option instead")
        return self


# What each scope offers a person, written here rather than by the model.
# The team's real name is substituted when the question is written, where the
# asking agent's team is known.
_SCOPE_WORDS: dict[str, tuple[str, str]] = {
    "agent": ("Just this agent", "Only the agent you are talking to will remember it."),
    "team": ("This agent's team", "Everyone on the team will remember it."),
    "workspace": ("Everyone in the workspace", "The whole company will remember it."),
}


class AskPersonOutput(BaseModel):
    required: bool = False
    status: str  # "asked" | "already_answered" | "already_asked" | "not_asked"
    question_id: str = ""
    answer_kind: str = ""  # "" | "option" | "other"
    option_value: str = ""
    answer: str = ""
    granted_scope: str = ""  # "" | "agent" | "team" | "workspace"
    detail: str


ASK_PERSON_TOOL = "organization.ask_person"


def asked_question_id(output: Mapping[str, Any] | None, *, tool_name: str) -> str:
    """The id of a question this call actually put on somebody's screen, or
    ``""`` for anything else.

    The single predicate for "did this step park on a person?", read by the
    tool worker's ``stop_reason``, the step projection's suppression, and the
    projection's lift into ``StepResult.person_questions``. One function so
    the three can never disagree: a step whose ``tool_result`` is suppressed
    but which does not park would leave the model with a call that has no
    observation at all.

    An ask refused as a repeat or over budget is not one of these. Its
    observation is useful immediately and belongs in the same step.
    """
    if tool_name != ASK_PERSON_TOOL or not output:
        return ""
    if output.get("status") != "asked":
        return ""
    question_id = output.get("question_id")
    return question_id if isinstance(question_id, str) else ""


def question_dedupe_hash(question: str, option_values: list[str]) -> str:
    """The repeat guard's key: the same words with the same choices.

    Normalized the way memory normalizes content (NFKC, casefold, whitespace,
    trailing punctuation) so re-asking with a comma moved is still a repeat.
    """
    payload = "v1|" + normalize_content(question) + "|" + "|".join(sorted(option_values))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(UTC)


async def validate_ask_person(
    ctx: ToolExecutionContext,
    payload: BaseModel,
    grants: Sequence[Grant],
) -> PolicyDecision | None:
    """The real lock. Advertisement (``PERSON_FACING_TOOLS``) is prompt
    economy; this runs in the gateway after grant evaluation and is what
    stops a trigger-fired run, a delegated child, or an accepted work
    request from putting a box on somebody's screen."""
    data = cast(AskPersonInput, payload)
    task = await ctx.session.get(Task, ctx.task_id)
    if not task_has_a_person_watching(task):
        return PolicyDecision(
            decision=DecisionType.DENY,
            code="no_person_watching",
            reason=_DENY_NO_PERSON_WATCHING,
        )
    # A scope question that offers a team the agent does not have would be
    # answered into a memory nothing could store. Refusing before the row
    # exists is cheaper for everyone than refusing after the person answers.
    if data.kind == "memory_scope" and any(
        option.value == MemoryScope.TEAM.value for option in data.options
    ):
        team_id = await ctx.session.scalar(
            select(Agent.team_id).where(
                Agent.id == ctx.agent_id, Agent.workspace_id == ctx.workspace_id
            )
        )
        if team_id is None:
            return PolicyDecision(
                decision=DecisionType.DENY,
                code="no_team_for_scope",
                reason=_DENY_NO_TEAM_FOR_SCOPE,
            )
    return None


async def _questions_this_run(ctx: ToolExecutionContext) -> int:
    return int(
        await ctx.session.scalar(
            select(func.count())
            .select_from(UserQuestion)
            .where(
                UserQuestion.workspace_id == ctx.workspace_id,
                UserQuestion.run_id == ctx.run_id,
            )
        )
        or 0
    )


async def _questions_this_hour(ctx: ToolExecutionContext, conversation_id: UUID) -> int:
    return int(
        await ctx.session.scalar(
            select(func.count())
            .select_from(UserQuestion)
            .where(
                UserQuestion.workspace_id == ctx.workspace_id,
                UserQuestion.conversation_id == conversation_id,
                UserQuestion.asked_at > _now() - _CONVERSATION_WINDOW,
            )
        )
        or 0
    )


async def _newest_twin(
    ctx: ToolExecutionContext, conversation_id: UUID | None, dedupe_hash: str, data: AskPersonInput
) -> UserQuestion | None:
    twin: UserQuestion | None = await ctx.session.scalar(
        select(UserQuestion)
        .where(
            UserQuestion.workspace_id == ctx.workspace_id,
            UserQuestion.conversation_id == conversation_id,
            UserQuestion.agent_id == ctx.agent_id,
            UserQuestion.dedupe_hash == dedupe_hash,
            UserQuestion.input_key == data.input_key,
            UserQuestion.value_type == data.value_type,
            UserQuestion.required == data.required,
        )
        .order_by(UserQuestion.asked_at.desc(), UserQuestion.id.desc())
        .limit(1)
    )
    return twin


def _answered_output(question: UserQuestion, *, run_id: UUID) -> AskPersonOutput:
    return AskPersonOutput(
        status="already_answered",
        question_id=str(question.id),
        answer_kind=question.answer_kind,
        option_value=question.answer_option_value,
        answer=question.answer_text,
        # An answer only authorises a memory inside the run it was given in;
        # the same check runs again in memory.propose against the row itself.
        granted_scope=question.granted_scope if question.run_id == run_id else "",
        detail=_DETAIL_ALREADY_ANSWERED.format(answer=question.answer_text or "(no words)"),
    )


async def _name_the_team(
    ctx: ToolExecutionContext, options: list[AskPersonOption]
) -> list[AskPersonOption]:
    """Substitute the asking agent's real team name into the team option."""
    if not any(option.value == "team" for option in options):
        return options
    team_name = await ctx.session.scalar(
        select(Team.name)
        .join(Agent, Agent.team_id == Team.id)
        .where(Agent.id == ctx.agent_id, Agent.workspace_id == ctx.workspace_id)
    )
    if not team_name:
        return options
    return [
        AskPersonOption(
            value=option.value,
            label=f"The {team_name} team",
            detail=f"Everyone on {team_name} will remember it.",
        )
        if option.value == "team"
        else option
        for option in options
    ]


def _question_content(
    question: UserQuestion, *, options: list[AskPersonOption], agent_name: str
) -> dict[str, object]:
    """The chat row's ``content_json`` — the whole UI contract for the card.

    ``delivered: "observation"`` from the moment it is written: the answer
    reaches the model once, as this tool call's result, and rendering the
    question again as structured JSON inside its own task would make the
    agent read its own question as somebody else's message.
    """
    return structured_content(
        question.question,
        recommended_next_action="await_answer",
        kind="user_question",
        question_id=str(question.id),
        question=question.question,
        context=question.context,
        question_kind=question.kind,
        required=question.required,
        input_key=question.input_key,
        value_type=question.value_type,
        options=[option.model_dump() for option in options],
        allow_other=question.allow_other,
        other_label=OTHER_LABEL,
        other_placeholder=OTHER_PLACEHOLDER,
        status=question.status,
        expires_at=question.expires_at.isoformat(),
        asked_by_agent_name=agent_name,
        delivered="observation",
        text=question.question,
    )


async def _ask_person(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    data = cast(AskPersonInput, payload)
    from jhin_tools.schedule_confirmation import canonicalize_schedule_question

    asked_at = _now()
    await canonicalize_schedule_question(ctx, data, now=asked_at)
    task = await ctx.session.get(Task, ctx.task_id)
    conversation_id = task.conversation_id if task is not None else None
    if data.required:
        from jhin_tools.readiness import record_required_input

        if not data.input_key:
            data.input_key = "question_" + question_dedupe_hash(data.question, [])[:16]
        await record_required_input(ctx, data.input_key, data.question, data.value_type)

    dedupe_hash = question_dedupe_hash(data.question, [option.value for option in data.options])
    twin = await _newest_twin(ctx, conversation_id, dedupe_hash, data)
    if twin is not None and twin.status == UserQuestionStatus.ANSWERED.value:
        if data.required and task is not None:
            from jhin_tools.readiness import validate_answer

            value = validate_answer(
                twin.answer_option_value
                if twin.answer_kind == "option" and data.value_type != "text"
                else twin.answer_text,
                data.value_type,
            )
            metadata = dict(task.metadata_json or {})
            task.metadata_json = {
                **metadata,
                "required_inputs": [
                    item
                    for item in metadata.get("required_inputs", [])
                    if isinstance(item, dict) and item.get("key") != data.input_key
                ],
                "resolved_inputs": {**metadata.get("resolved_inputs", {}), data.input_key: value},
            }
        return _answered_output(twin, run_id=ctx.run_id)
    if twin is not None and twin.status == UserQuestionStatus.PENDING.value:
        return AskPersonOutput(
            status="already_asked", question_id=str(twin.id), detail=_DETAIL_ALREADY_ASKED
        )

    if await _questions_this_run(ctx) >= MAX_QUESTIONS_PER_RUN:
        return AskPersonOutput(status="not_asked", detail=_DETAIL_RUN_BUDGET)
    # A chat turn without a conversation row has no thread to rate-limit; the
    # per-run cap still bounds it, and the validator already established that
    # somebody is watching.
    if (
        conversation_id is not None
        and await _questions_this_hour(ctx, conversation_id) >= MAX_QUESTIONS_PER_CONVERSATION_HOUR
    ):
        return AskPersonOutput(status="not_asked", detail=_DETAIL_CONVERSATION_BUDGET)

    # Name the team on the scope option, where the asking agent has one. "The
    # Platform team" is a decision somebody can make; "this agent's team" is a
    # riddle. The platform still owns the words -- only the team's real name is
    # substituted, never anything the model wrote.
    options = await _name_the_team(ctx, data.options)

    question = UserQuestion(
        id=new_uuid7(),
        workspace_id=ctx.workspace_id,
        conversation_id=conversation_id,
        task_id=ctx.task_id,
        run_id=ctx.run_id,
        agent_id=ctx.agent_id,
        kind=data.kind,
        required=data.required,
        input_key=data.input_key,
        value_type=data.value_type,
        question=data.question,
        context=data.context,
        options_json=[option.model_dump() for option in options],
        allow_other=data.allow_other,
        dedupe_hash=dedupe_hash,
        idempotency_key=f"{ctx.run_id}:{ctx.tool_call_id}"[:200],
        status=UserQuestionStatus.PENDING.value,
        asked_at=asked_at,
        expires_at=asked_at + PERSON_ANSWER_WAIT,
    )
    try:
        # A SAVEPOINT, so losing the race for the unique key rolls back this
        # insert alone and not the tool call the gateway is recording.
        async with ctx.session.begin_nested():
            ctx.session.add(question)
            await ctx.session.flush()
    except IntegrityError:
        replay = await ctx.session.scalar(
            select(UserQuestion).where(
                UserQuestion.workspace_id == ctx.workspace_id,
                UserQuestion.idempotency_key == question.idempotency_key,
            )
        )
        if replay is None:
            raise
        # Mirror the row rather than assuming it is still live: parking again
        # on a question that is already closed would buy the run a thirty
        # minute wait for an answer that can never come.
        if replay.status == UserQuestionStatus.ANSWERED.value:
            return _answered_output(replay, run_id=ctx.run_id)
        if replay.status == UserQuestionStatus.PENDING.value:
            return AskPersonOutput(
                status="asked",
                question_id=str(replay.id),
                required=replay.required,
                detail=_DETAIL_ASKED,
            )
        return AskPersonOutput(
            status="not_asked", question_id=str(replay.id), detail=_DETAIL_CLOSED
        )

    recipient_id: UUID | None = None
    if conversation_id is not None:
        recipient_id = await ctx.session.scalar(
            select(Conversation.created_by_user_id).where(
                Conversation.id == conversation_id,
                Conversation.workspace_id == ctx.workspace_id,
            )
        )
    message = Message(
        id=new_uuid7(),
        workspace_id=ctx.workspace_id,
        task_id=ctx.task_id,
        run_id=ctx.run_id,
        conversation_id=conversation_id,
        sender_type=SenderType.AGENT.value,
        sender_id=ctx.agent_id,
        recipient_type=RecipientType.USER.value,
        recipient_id=recipient_id,
        message_type=MessageType.QUESTION.value,
        content_json=_question_content(question, options=options, agent_name=ctx.agent_name),
        visibility=MessageVisibility.VISIBLE.value,
    )
    ctx.session.add(message)
    await ctx.session.flush()
    question.message_id = message.id

    ctx.session.add(
        AuditEvent(
            workspace_id=ctx.workspace_id,
            actor_type=ActorType.AGENT.value,
            actor_id=ctx.agent_id,
            action="question.asked",
            target_type="user_question",
            target_id=question.id,
            # The words are in the message and the question row, where
            # forgetting a conversation removes them; audit keeps the shape.
            metadata_json={
                "kind": data.kind,
                "options": [option.value for option in data.options],
                "run_id": str(ctx.run_id),
                "conversation_id": str(conversation_id) if conversation_id else None,
            },
        )
    )
    await ctx.session.flush()
    return AskPersonOutput(
        status="asked",
        question_id=str(question.id),
        required=question.required,
        detail=_DETAIL_ASKED,
    )


ASK_PERSON_TOOLS: tuple[tuple[ToolDefinition, ToolExecutor, ToolValidator | None], ...] = (
    (
        ToolDefinition(
            name="organization.ask_person",
            description=(
                "Ask the person you are talking to one short question, with "
                "options=[] for free text, or two to four choices with room to type "
                "their own. Set required=true for prerequisites. Use it when work needs a "
                "detail you do not have and guessing would be worse than "
                "asking -- and in particular before you remember a fact for "
                "anyone but yourself: send kind 'memory_scope' with the "
                "options 'team' and 'workspace' (labelled in their words, "
                "e.g. 'Only the Engineering team' and 'Company wide') and "
                "their answer is what authorises the wider memory. Your turn "
                "stays open until required input is answered or the task is stopped; "
                "optional questions wait thirty minutes. Reserved setup keys "
                "ghost_admin_url and ghost_publisher_agent_id use platform-owned wording. "
                "the answer comes back as this call's result, so ask once "
                "and then use what they said. Do not use it to check in, to "
                "confirm something you were already told, or to ask "
                "permission for work you can simply do."
            ),
            risk=RiskLevel.WRITE,
            input_model=AskPersonInput,
            output_model=AskPersonOutput,
            required_capability=ASK_PERSON_CAPABILITY,
            supports_approval=True,
        ),
        _ask_person,
        validate_ask_person,
    ),
)
