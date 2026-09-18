"""Private settings metadata and separately sealed sensitive writes."""

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from jhin_api.deps import AdminCtx, DbSession
from jhin_api.security.csrf import csrf_protect
from jhin_api.variables import service
from jhin_api.variables.schemas import (
    SensitiveVariableCreate,
    SensitiveVariableUpdate,
    VariableCopy,
    VariableCreate,
    VariableListOut,
    VariableOut,
    VariableScope,
    VariableUpdate,
)
from jhin_secrets.variables import VariableError, VariableStore

router = APIRouter(
    prefix="/api/v1/workspaces/{workspace_id}/variables",
    tags=["variables"],
    dependencies=[Depends(csrf_protect)],
)


@router.get("", response_model=VariableListOut)
async def list_variables(
    ctx: AdminCtx,
    db: DbSession,
    response: Response,
    scope: VariableScope | None = None,
    scope_id: UUID | None = None,
    limit: int = Query(default=100, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    try:
        store = VariableStore(db)
        rows = await store.list(
            service.actor(ctx), scope=scope, scope_id=scope_id, limit=limit, offset=offset
        )
        return {
            "items": [store.public(row) for row in rows],
            "total": await store.count(service.actor(ctx), scope=scope, scope_id=scope_id),
        }
    except VariableError as exc:
        raise HTTPException(exc.status_code, str(exc)) from None


@router.get("/{variable_id}", response_model=VariableOut)
async def get_variable(
    variable_id: UUID, ctx: AdminCtx, db: DbSession, response: Response
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    try:
        store = VariableStore(db)
        return store.public(await store.get(service.actor(ctx), variable_id))
    except VariableError as exc:
        raise HTTPException(exc.status_code, str(exc)) from None


@router.post("", response_model=VariableOut, status_code=201)
async def create_variable(
    payload: VariableCreate, ctx: AdminCtx, db: DbSession, request: Request
) -> dict[str, Any]:
    return await service.save(
        db, getattr(request.app.state, "secret_crypto", None), ctx, **payload.model_dump()
    )


@router.post("/secrets", response_model=VariableOut, status_code=201)
async def create_sensitive_variable(
    payload: SensitiveVariableCreate, ctx: AdminCtx, db: DbSession, request: Request
) -> dict[str, Any]:
    values = payload.model_dump(exclude={"value"})
    return await service.save(
        db,
        getattr(request.app.state, "secret_crypto", None),
        ctx,
        secret_write=True,
        value=payload.value.get_secret_value(),
        **values,
    )


@router.patch("/{variable_id}", response_model=VariableOut)
async def update_variable(
    variable_id: UUID, payload: VariableUpdate, ctx: AdminCtx, db: DbSession, request: Request
) -> dict[str, Any]:
    return await service.save(
        db,
        getattr(request.app.state, "secret_crypto", None),
        ctx,
        variable_id=variable_id,
        **payload.model_dump(exclude_unset=True),
    )


@router.put("/{variable_id}/secret", response_model=VariableOut)
async def replace_sensitive_variable(
    variable_id: UUID,
    payload: SensitiveVariableUpdate,
    ctx: AdminCtx,
    db: DbSession,
    request: Request,
) -> dict[str, Any]:
    return await service.save(
        db,
        getattr(request.app.state, "secret_crypto", None),
        ctx,
        variable_id=variable_id,
        secret_write=True,
        expected_version=payload.expected_version,
        value=payload.value.get_secret_value(),
    )


@router.delete("/{variable_id}", status_code=204)
async def delete_variable(
    variable_id: UUID, ctx: AdminCtx, db: DbSession, expected_version: int = Query(ge=1)
) -> None:
    await service.remove(db, ctx, variable_id, expected_version)


@router.post("/{variable_id}/copy", response_model=VariableOut, status_code=201)
async def copy_variable(
    variable_id: UUID, payload: VariableCopy, ctx: AdminCtx, db: DbSession, request: Request
) -> dict[str, Any]:
    return await service.copy(
        db,
        getattr(request.app.state, "secret_crypto", None),
        ctx,
        variable_id,
        **payload.model_dump(),
    )
