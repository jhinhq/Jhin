"""Route handlers for a person's own first-run onboarding state.

Filed under the ``workspaces`` tag because that is where it lives in the URL
space and in the reference; it is a separate module only so that a personal UI
preference does not accrete onto the workspace administration service.
"""

from fastapi import APIRouter, Depends

from jhin_api.deps import DbSession, ViewerCtx
from jhin_api.onboarding import service
from jhin_api.onboarding.schemas import OnboardingStateIn, OnboardingStateOut
from jhin_api.security.csrf import csrf_protect

router = APIRouter(
    prefix="/api/v1/workspaces", tags=["workspaces"], dependencies=[Depends(csrf_protect)]
)


@router.get("/{workspace_id}/onboarding")
async def get_onboarding(ctx: ViewerCtx, db: DbSession) -> OnboardingStateOut:
    """How far the *calling* user got through this workspace's introduction.

    Viewer-level on purpose: everyone who can open the app gets oriented, and
    the answer only ever describes the caller's own membership.
    """
    return await service.get_state(db, ctx.workspace_id, ctx.user.id)


@router.put("/{workspace_id}/onboarding")
async def set_onboarding(
    payload: OnboardingStateIn, ctx: ViewerCtx, db: DbSession
) -> OnboardingStateOut:
    """Record that the caller skipped, paused, or finished the introduction.

    Not audited: this is a personal preference about one person's own screen,
    with no bearing on who may do what.
    """
    return await service.set_state(
        db,
        ctx.workspace_id,
        ctx.user.id,
        new_status=payload.status,
        last_step=payload.last_step,
    )
