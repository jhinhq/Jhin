"""Request/response contracts for the questions agents ask people.

``QuestionOut`` deliberately projects less than the row holds: ``run_id``,
``dedupe_hash``, ``idempotency_key``, ``granted_authority``, and the
grant-consumption columns are how the platform decides whether an answer may
widen a memory, and none of them are anyone's business over HTTP.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from jhin_memory.types import MAX_CANDIDATE_CHARS


class QuestionOptionOut(BaseModel):
    """One offered choice. ``value`` is the machine key the answer names."""

    value: str
    label: str
    detail: str


class QuestionOut(BaseModel):
    id: UUID
    workspace_id: UUID
    conversation_id: UUID | None
    task_id: UUID | None
    message_id: UUID | None
    agent_id: UUID
    agent_name: str | None
    # "open" | "memory_scope"
    kind: str
    question: str
    context: str
    options: list[QuestionOptionOut]
    allow_other: bool
    # "pending" | "answered" | "expired" | "cancelled"
    status: str
    asked_at: datetime
    expires_at: datetime
    answered_at: datetime | None
    answered_by_user_id: UUID | None
    answered_by_name: str | None
    # "" | "option" | "other"
    answer_kind: str
    answer_option_value: str
    answer_text: str
    # "" | "agent" | "team" | "workspace" — what this answer authorised, if
    # anything. Empty with a reason is the normal case, not an error.
    granted_scope: str
    grant_denied_reason: str


class QuestionListOut(BaseModel):
    items: list[QuestionOut]
    total: int


class AnswerQuestionIn(BaseModel):
    """Exactly one of the two, never both, never neither.

    Which field arrives is what makes a picked option distinguishable from a
    typed one, forever: the service never infers ``answer_kind`` by comparing
    the text to the labels, so a person who types the exact words of an option
    still typed them — and typing grants no memory scope.
    """

    model_config = ConfigDict(extra="forbid")

    option_value: str | None = Field(default=None, max_length=64)
    # Exactly MAX_CANDIDATE_CHARS so a typed answer can become a memory
    # verbatim rather than being truncated between the two systems.
    other_text: str | None = Field(default=None, max_length=MAX_CANDIDATE_CHARS)

    @model_validator(mode="after")
    def _exactly_one(self) -> AnswerQuestionIn:
        if (self.option_value is None) == (self.other_text is None):
            raise ValueError("send exactly one of option_value or other_text")
        if self.other_text is not None and not self.other_text.strip():
            raise ValueError("other_text must not be blank")
        return self


class AnswerQuestionOut(BaseModel):
    question: QuestionOut
    # False when the answer is recorded but the waiting run could not be woken
    # (it had already stopped). The person can simply say it in the composer.
    resumed: bool
