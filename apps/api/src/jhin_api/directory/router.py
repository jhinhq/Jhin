"""GET /api/v1/workspaces/{workspace_id}/directory

Returns the same ``DirectoryEntry`` allowlist the agent runtime sees:
id, name, slug, role, public purpose, expertise, availability, primary
team, manager id. Never system prompts, grants, model config, memories, or
conversations. Any workspace member (viewer and up) may read it.
"""

from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel

from jhin_api.deps import DbSession, ViewerCtx
from jhin_tools.directory import DIRECTORY_MAX_RESULTS, DirectoryEntry, search_directory

router = APIRouter(prefix="/api/v1/workspaces/{workspace_id}", tags=["directory"])


class DirectoryOut(BaseModel):
    items: list[DirectoryEntry]
    has_more: bool


@router.get("/directory")
async def get_directory(
    ctx: ViewerCtx,
    db: DbSession,
    q: str | None = None,
    team_id: UUID | None = None,
    expertise: str | None = None,
    limit: int = DIRECTORY_MAX_RESULTS,
) -> DirectoryOut:
    items, has_more = await search_directory(
        db, ctx.workspace_id, q=q, team_id=team_id, expertise=expertise, limit=limit
    )
    return DirectoryOut(items=items, has_more=has_more)
