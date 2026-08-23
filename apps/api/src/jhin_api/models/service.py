"""Model provider/profile business logic (plan 6.7, 6.8, 15).

Verification decrypts the provider credential in process memory, makes one
cheap live call through the adapter, and discards the plaintext (plan 13.5).
The credential never appears in the response, the audit row, or logs (the
process redactor knows the value after decryption).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from opentelemetry.trace import Tracer
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_api.audit import service as audit
from jhin_api.deps import WorkspaceContext
from jhin_db.models import Agent, ModelProfile, ModelProvider, Secret, Workspace
from jhin_models import ModelClient, ModelProviderError, build_model_client
from jhin_models.factory import ProviderConfigError
from jhin_observability import JhinMetrics
from jhin_secrets import SecretCrypto, SecretStore
from jhin_secrets.redaction import redact_text


def _provider_not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Model provider not found")


def _profile_not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Model profile not found")


def _build_verification_client(
    provider_type: str,
    base_url: str | None,
    api_key: str | None,
    metrics: JhinMetrics,
    tracer: Tracer,
) -> ModelClient:
    """The single factory call site for provider checks (audited by telemetry tests)."""
    return build_model_client(
        provider_type,
        base_url=base_url,
        api_key=api_key,
        metrics=metrics,
        tracer=tracer,
    )


async def _validate_secret(db: AsyncSession, workspace_id: UUID, secret_id: UUID | None) -> None:
    if secret_id is None:
        return
    exists = await db.scalar(
        select(Secret.id).where(Secret.id == secret_id, Secret.workspace_id == workspace_id)
    )
    if not exists:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="secret_id does not reference a secret in this workspace",
        )


# --- Providers ---


async def list_providers(db: AsyncSession, workspace_id: UUID) -> list[ModelProvider]:
    rows = await db.scalars(
        select(ModelProvider)
        .where(ModelProvider.workspace_id == workspace_id)
        .order_by(ModelProvider.created_at)
    )
    return list(rows)


async def get_provider(db: AsyncSession, workspace_id: UUID, provider_id: UUID) -> ModelProvider:
    provider = await db.scalar(
        select(ModelProvider).where(
            ModelProvider.id == provider_id, ModelProvider.workspace_id == workspace_id
        )
    )
    if provider is None:
        raise _provider_not_found()
    return provider


async def create_provider(
    db: AsyncSession,
    ctx: WorkspaceContext,
    *,
    values: dict[str, Any],
    request_id: UUID,
    ip_hash: str,
) -> ModelProvider:
    await _validate_secret(db, ctx.workspace_id, values.get("secret_id"))
    provider = ModelProvider(workspace_id=ctx.workspace_id, **values)
    db.add(provider)
    try:
        await db.flush()
    except IntegrityError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A provider with this name already exists in the workspace",
        ) from exc
    audit.record(
        db,
        action="provider.created",
        target_type="model_provider",
        target_id=provider.id,
        workspace_id=ctx.workspace_id,
        actor_id=ctx.user.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={"display_name": provider.display_name, "type": provider.type},
    )
    await db.commit()
    return provider


async def update_provider(
    db: AsyncSession,
    ctx: WorkspaceContext,
    provider_id: UUID,
    *,
    changes: dict[str, Any],
    request_id: UUID,
    ip_hash: str,
) -> ModelProvider:
    provider = await get_provider(db, ctx.workspace_id, provider_id)
    if "secret_id" in changes:
        await _validate_secret(db, ctx.workspace_id, changes["secret_id"])
    for field, value in changes.items():
        setattr(provider, field, value)
    audit.record(
        db,
        action="provider.updated",
        target_type="model_provider",
        target_id=provider.id,
        workspace_id=ctx.workspace_id,
        actor_id=ctx.user.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={"changed_fields": sorted(changes)},
    )
    try:
        await db.commit()
    except IntegrityError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A provider with this name already exists in the workspace",
        ) from exc
    return provider


async def delete_provider(
    db: AsyncSession,
    ctx: WorkspaceContext,
    provider_id: UUID,
    *,
    request_id: UUID,
    ip_hash: str,
) -> None:
    provider = await get_provider(db, ctx.workspace_id, provider_id)
    profile_ids = list(
        await db.scalars(select(ModelProfile.id).where(ModelProfile.provider_id == provider.id))
    )
    if profile_ids:
        # Agents pinned to one of this provider's profiles keep it alive; name
        # them so the admin knows what to change. The workspace default is
        # just cleared — profiles cascade away with the provider.
        agent_names = list(
            await db.scalars(
                select(Agent.name)
                .where(Agent.model_profile_id.in_(profile_ids))
                .order_by(Agent.name)
                .limit(5)
            )
        )
        if agent_names:
            listed = ", ".join(agent_names)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"Agents still use this provider's models ({listed}). "
                    "Change their model first, then delete the provider."
                ),
            )
        workspace = await db.scalar(
            select(Workspace).where(Workspace.default_model_profile_id.in_(profile_ids))
        )
        if workspace is not None:
            workspace.default_model_profile_id = None
    audit.record(
        db,
        action="provider.deleted",
        target_type="model_provider",
        target_id=provider.id,
        workspace_id=ctx.workspace_id,
        actor_id=ctx.user.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={"display_name": provider.display_name, "type": provider.type},
    )
    await db.delete(provider)
    await db.commit()


async def verify_draft(
    db: AsyncSession,
    crypto: SecretCrypto,
    ctx: WorkspaceContext,
    *,
    provider_type: str,
    base_url: str | None,
    api_key: str | None,
    secret_id: UUID | None,
    metrics: JhinMetrics,
    tracer: Tracer,
) -> tuple[bool, str]:
    """Live credential check for a provider that has not been saved yet.

    The key travels only through this request; nothing is written. When
    ``secret_id`` is given instead, the stored secret is used (workspace
    scoped, 422 when unknown).
    """
    key = api_key
    if key is None and secret_id is not None:
        await _validate_secret(db, ctx.workspace_id, secret_id)
        key = await SecretStore(db, crypto).reveal(ctx.workspace_id, secret_id)
    try:
        client = _build_verification_client(provider_type, base_url, key, metrics, tracer)
    except ProviderConfigError as exc:
        return False, str(exc)
    try:
        return True, await client.verify()
    except ModelProviderError as exc:
        return False, redact_text(str(exc))
    finally:
        await client.close()


async def list_provider_models(
    db: AsyncSession,
    crypto: SecretCrypto,
    ctx: WorkspaceContext,
    provider_id: UUID,
    metrics: JhinMetrics,
    tracer: Tracer,
) -> tuple[list[str], str | None]:
    """Model identifiers from the provider, or an explanation when unavailable.

    Read-only: nothing is stored on the provider row. A provider that cannot
    list models (or rejects the credentials) yields an empty list plus a
    redacted detail so the UI can fall back to free-text entry.
    """
    provider = await get_provider(db, ctx.workspace_id, provider_id)

    api_key: str | None = None
    if provider.secret_id is not None:
        api_key = await SecretStore(db, crypto).reveal(ctx.workspace_id, provider.secret_id)

    try:
        client = _build_verification_client(
            provider.type, provider.base_url, api_key, metrics, tracer
        )
    except ProviderConfigError as exc:
        return [], str(exc)
    try:
        return await client.list_models(), None
    except ModelProviderError as exc:
        return [], redact_text(str(exc))
    finally:
        await client.close()


async def verify_provider(
    db: AsyncSession,
    crypto: SecretCrypto,
    ctx: WorkspaceContext,
    provider_id: UUID,
    metrics: JhinMetrics,
    tracer: Tracer,
    *,
    request_id: UUID,
    ip_hash: str,
) -> tuple[bool, str]:
    """One cheap live call through the adapter; result stored on the row."""
    provider = await get_provider(db, ctx.workspace_id, provider_id)

    api_key: str | None = None
    if provider.secret_id is not None:
        api_key = await SecretStore(db, crypto).reveal(ctx.workspace_id, provider.secret_id)

    ok, detail = True, ""
    try:
        client = _build_verification_client(
            provider.type, provider.base_url, api_key, metrics, tracer
        )
    except ProviderConfigError as exc:
        ok, detail = False, str(exc)
    else:
        try:
            detail = await client.verify()
        except ModelProviderError as exc:
            ok, detail = False, redact_text(str(exc))
        finally:
            await client.close()

    now = datetime.now(UTC)
    provider.last_verified_at = now if ok else provider.last_verified_at
    provider.last_error = None if ok else detail
    audit.record(
        db,
        action="provider.verified" if ok else "provider.verify_failed",
        target_type="model_provider",
        target_id=provider.id,
        workspace_id=ctx.workspace_id,
        actor_id=ctx.user.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={"display_name": provider.display_name, "ok": ok},
    )
    await db.commit()
    return ok, detail


# --- Profiles ---


async def list_profiles(db: AsyncSession, workspace_id: UUID) -> list[ModelProfile]:
    rows = await db.scalars(
        select(ModelProfile)
        .where(ModelProfile.workspace_id == workspace_id)
        .order_by(ModelProfile.created_at)
    )
    return list(rows)


async def get_profile(db: AsyncSession, workspace_id: UUID, profile_id: UUID) -> ModelProfile:
    profile = await db.scalar(
        select(ModelProfile).where(
            ModelProfile.id == profile_id, ModelProfile.workspace_id == workspace_id
        )
    )
    if profile is None:
        raise _profile_not_found()
    return profile


async def create_profile(
    db: AsyncSession,
    ctx: WorkspaceContext,
    *,
    values: dict[str, Any],
    request_id: UUID,
    ip_hash: str,
) -> ModelProfile:
    await get_provider(db, ctx.workspace_id, values["provider_id"])  # workspace scope check
    profile = ModelProfile(workspace_id=ctx.workspace_id, **values)
    db.add(profile)
    try:
        await db.flush()
    except IntegrityError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A profile with this name already exists in the workspace",
        ) from exc
    audit.record(
        db,
        action="model_profile.created",
        target_type="model_profile",
        target_id=profile.id,
        workspace_id=ctx.workspace_id,
        actor_id=ctx.user.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={"display_name": profile.display_name, "model_name": profile.model_name},
    )
    await db.commit()
    return profile


async def update_profile(
    db: AsyncSession,
    ctx: WorkspaceContext,
    profile_id: UUID,
    *,
    changes: dict[str, Any],
    request_id: UUID,
    ip_hash: str,
) -> ModelProfile:
    profile = await get_profile(db, ctx.workspace_id, profile_id)
    if "provider_id" in changes:
        await get_provider(db, ctx.workspace_id, changes["provider_id"])
    for field, value in changes.items():
        setattr(profile, field, value)
    audit.record(
        db,
        action="model_profile.updated",
        target_type="model_profile",
        target_id=profile.id,
        workspace_id=ctx.workspace_id,
        actor_id=ctx.user.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={"changed_fields": sorted(changes)},
    )
    try:
        await db.commit()
    except IntegrityError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A profile with this name already exists in the workspace",
        ) from exc
    return profile


async def delete_profile(
    db: AsyncSession,
    ctx: WorkspaceContext,
    profile_id: UUID,
    *,
    request_id: UUID,
    ip_hash: str,
) -> None:
    profile = await get_profile(db, ctx.workspace_id, profile_id)
    in_use_by_agent = await db.scalar(
        select(Agent.id).where(Agent.model_profile_id == profile.id).limit(1)
    )
    if in_use_by_agent:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Profile is in use by agents; change their model first",
        )
    # Deleting the workspace default simply clears the default; agents fall
    # back to "no default" until an admin picks another profile.
    workspace = await db.scalar(
        select(Workspace).where(Workspace.default_model_profile_id == profile.id)
    )
    if workspace is not None:
        workspace.default_model_profile_id = None
    audit.record(
        db,
        action="model_profile.deleted",
        target_type="model_profile",
        target_id=profile.id,
        workspace_id=ctx.workspace_id,
        actor_id=ctx.user.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={"display_name": profile.display_name},
    )
    await db.delete(profile)
    await db.commit()
