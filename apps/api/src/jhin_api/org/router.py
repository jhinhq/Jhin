"""Route handler for the organization graph (plan 17.4, Phase 2 item 5)."""

from fastapi import APIRouter

from jhin_api.deps import DbSession, ViewerCtx
from jhin_api.org import service
from jhin_api.org.schemas import OrgGraph

router = APIRouter(prefix="/api/v1/workspaces/{workspace_id}", tags=["organization"])


@router.get("/org-graph")
async def get_org_graph(ctx: ViewerCtx, db: DbSession) -> OrgGraph:
    return await service.get_org_graph(db, ctx.workspace_id)
