"""HTTP policy adapter; shared variable storage never commits itself."""

from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_api.deps import WorkspaceContext
from jhin_domain import WorkspaceRole, role_satisfies
from jhin_secrets import SecretCrypto
from jhin_secrets.variables import VariableActor, VariableError, VariableStore


def actor(ctx: WorkspaceContext) -> VariableActor:
    return VariableActor(
        ctx.workspace_id,
        "user",
        ctx.user.id,
        is_admin=role_satisfies(ctx.role, WorkspaceRole.ADMIN),
    )


async def save(
    db: AsyncSession,
    crypto: SecretCrypto | None,
    ctx: WorkspaceContext,
    *,
    variable_id: UUID | None = None,
    secret_write: bool = False,
    **values: Any,
) -> dict[str, Any]:
    store = VariableStore(db, crypto)
    try:
        if secret_write and ctx.api_key is not None:
            raise VariableError("Sensitive input requires a signed-in browser session")
        if variable_id is not None:
            existing = await store.get(actor(ctx), variable_id)
            if (
                existing.sensitive
                and "value" in values
                and values["value"] is not None
                and not secret_write
            ):
                raise VariableError("Replace sensitive values through the secure input endpoint")
            if secret_write and not existing.sensitive:
                raise VariableError("This variable is not sensitive", 422)
        row = await store.set(actor(ctx), variable_id=variable_id, **values)
        await db.commit()
        return store.public(row)
    except VariableError as exc:
        await db.rollback()
        raise HTTPException(exc.status_code, str(exc)) from None


async def remove(
    db: AsyncSession, ctx: WorkspaceContext, variable_id: UUID, expected_version: int
) -> None:
    try:
        await VariableStore(db).delete(actor(ctx), variable_id, expected_version=expected_version)
        await db.commit()
    except VariableError as exc:
        await db.rollback()
        raise HTTPException(exc.status_code, str(exc)) from None


async def copy(
    db: AsyncSession,
    crypto: SecretCrypto | None,
    ctx: WorkspaceContext,
    variable_id: UUID,
    **values: Any,
) -> dict[str, Any]:
    try:
        store = VariableStore(db, crypto)
        row = await store.copy(actor(ctx), variable_id, **values)
        await db.commit()
        return store.public(row)
    except VariableError as exc:
        await db.rollback()
        raise HTTPException(exc.status_code, str(exc)) from None
