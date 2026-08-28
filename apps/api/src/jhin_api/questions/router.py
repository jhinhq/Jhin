"""Routes for the questions agents ask people.

Reading is viewer+; answering is member+ — the same bar as deciding an
approval, because both are a person exercising authority an agent does not
have. A viewer sees the question and the answer, and cannot give one.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from jhin_api.deps import DbSession, MemberCtx, TemporalDep, ViewerCtx
from jhin_api.deps import client_ip_hash as ip_hash
from jhin_api.deps import get_request_id as req_id
from jhin_api.questions import service
from jhin_api.questions.schemas import (
    AnswerQuestionIn,
    AnswerQuestionOut,
    QuestionListOut,
    QuestionOut,
)
from jhin_api.security.csrf import csrf_protect
from jhin_domain import UserQuestionStatus

router = APIRouter(
    prefix="/api/v1/workspaces/{workspace_id}/questions",
    tags=["questions"],
    dependencies=[Depends(csrf_protect)],
)


def _valid_status(value: str | None) -> str | None:
    if value is not None and value not in {s.value for s in UserQuestionStatus}:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Unknown status '{value}'",
        )
    return value


@router.get("")
async def list_questions(
    ctx: ViewerCtx,
    db: DbSession,
    status: str | None = None,
    conversation_id: UUID | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> QuestionListOut:
    items, total = await service.list_questions(
        db,
        ctx.workspace_id,
        status_filter=_valid_status(status),
        conversation_id=conversation_id,
        limit=limit,
        offset=offset,
    )
    return QuestionListOut(items=items, total=total)


@router.get("/{question_id}")
async def get_question(question_id: UUID, ctx: ViewerCtx, db: DbSession) -> QuestionOut:
    return await service.get_question(db, ctx.workspace_id, question_id)


@router.post("/{question_id}/answer")
async def answer_question(
    question_id: UUID,
    payload: AnswerQuestionIn,
    request: Request,
    ctx: MemberCtx,
    db: DbSession,
    temporal: TemporalDep,
) -> AnswerQuestionOut:
    question, resumed = await service.answer(
        db,
        ctx,
        temporal,
        question_id,
        payload,
        request_id=req_id(request),
        ip_hash=ip_hash(request),
    )
    return AnswerQuestionOut(question=question, resumed=resumed)
