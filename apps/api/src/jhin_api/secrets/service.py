"""Secret management business logic (plan 13.4).

All operations run through :class:`jhin_secrets.SecretStore`; this module
adds workspace RBAC context, duplicate handling, and audit records. Plaintext
exists only transiently inside create/rotate calls and is never returned.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_api.audit import service as audit
from jhin_api.deps import WorkspaceContext
from jhin_db.models import ModelProvider, Secret
from jhin_domain import SecretType
from jhin_secrets import SecretCrypto, SecretStore
from jhin_secrets.store import SecretNotFoundError


def _not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Secret not found")


async def list_secrets(db: AsyncSession, workspace_id: UUID) -> list[Secret]:
    rows = await db.scalars(
        select(Secret).where(Secret.workspace_id == workspace_id).order_by(Secret.created_at)
    )
    return list(rows)


async def create_secret(
    db: AsyncSession,
    crypto: SecretCrypto,
    ctx: WorkspaceContext,
    *,
    name: str,
    value: str,
    secret_type: SecretType,
    request_id: UUID,
    ip_hash: str,
) -> Secret:
    store = SecretStore(db, crypto)
    try:
        secret = await store.create(
            workspace_id=ctx.workspace_id,
            name=name,
            plaintext=value,
            secret_type=secret_type,
            created_by_user_id=ctx.user.id,
        )
    except IntegrityError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A secret with this name already exists in the workspace",
        ) from exc
    audit.record(
        db,
        action="secret.created",
        target_type="secret",
        target_id=secret.id,
        workspace_id=ctx.workspace_id,
        actor_id=ctx.user.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={"name": secret.name, "type": secret.type},
    )
    await db.commit()
    return secret


async def rename_secret(
    db: AsyncSession,
    ctx: WorkspaceContext,
    secret_id: UUID,
    *,
    name: str,
    request_id: UUID,
    ip_hash: str,
) -> Secret:
    secret = await db.scalar(
        select(Secret).where(Secret.id == secret_id, Secret.workspace_id == ctx.workspace_id)
    )
    if secret is None:
        raise _not_found()
    previous = secret.name
    secret.name = name
    audit.record(
        db,
        action="secret.updated",
        target_type="secret",
        target_id=secret.id,
        workspace_id=ctx.workspace_id,
        actor_id=ctx.user.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={"from_name": previous, "to_name": name},
    )
    try:
        await db.commit()
    except IntegrityError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A secret with this name already exists in the workspace",
        ) from exc
    return secret


async def rotate_secret(
    db: AsyncSession,
    crypto: SecretCrypto,
    ctx: WorkspaceContext,
    secret_id: UUID,
    *,
    value: str,
    request_id: UUID,
    ip_hash: str,
) -> Secret:
    store = SecretStore(db, crypto)
    try:
        secret = await store.rotate(ctx.workspace_id, secret_id, value)
    except SecretNotFoundError as exc:
        raise _not_found() from exc
    audit.record(
        db,
        action="secret.rotated",
        target_type="secret",
        target_id=secret.id,
        workspace_id=ctx.workspace_id,
        actor_id=ctx.user.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={"name": secret.name},
    )
    await db.commit()
    return secret


async def delete_secret(
    db: AsyncSession,
    ctx: WorkspaceContext,
    secret_id: UUID,
    *,
    request_id: UUID,
    ip_hash: str,
) -> None:
    secret = await db.scalar(
        select(Secret).where(Secret.id == secret_id, Secret.workspace_id == ctx.workspace_id)
    )
    if secret is None:
        raise _not_found()
    in_use = await db.scalar(select(ModelProvider.id).where(ModelProvider.secret_id == secret.id))
    if in_use:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Secret is referenced by a model provider; delete or repoint it first",
        )
    audit.record(
        db,
        action="secret.deleted",
        target_type="secret",
        target_id=secret.id,
        workspace_id=ctx.workspace_id,
        actor_id=ctx.user.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={"name": secret.name, "type": secret.type},
    )
    await db.delete(secret)
    await db.commit()
