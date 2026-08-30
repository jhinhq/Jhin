"""Storing a client id an admin pasted, and never giving the secret back.

Client registrations are the one-time setup that makes every *later*
connection to the same server free. The service-level rules worth pinning:

* a secret goes in through ``SecretStore`` and comes back out only as a
  boolean;
* an issuer is put through the outbound policy like every other provider URL
  — a workspace admin is not a reason to skip SSRF validation;
* saving twice rotates the stored secret in place rather than orphaning
  ciphertext, and the registration keeps its id so connections pointing at it
  keep working.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_api.deps import WorkspaceContext
from jhin_api.oauth import service
from jhin_api.oauth.redirect import redirect_uri
from jhin_api.oauth.schemas import OAuthClientCreate
from jhin_api.settings import Settings
from jhin_db.models import OAuthClientRegistration, Secret
from jhin_domain import SecretType, new_uuid7
from jhin_oauth.persistence import OAuthClientStore
from jhin_secrets import SecretCrypto

ISSUER = "https://auth.example.com"
FAKE_SECRET = "not-a-real-client-secret-0000"
REQ = {"request_id": new_uuid7(), "ip_hash": "0" * 64}


def _settings() -> Settings:
    return Settings(_env_file=None, app_url="https://jhin.example.com")


async def test_a_pasted_client_is_stored_and_the_secret_never_comes_back(
    session: AsyncSession, crypto: SecretCrypto, admin_ctx: WorkspaceContext
) -> None:
    out = await service.create_client(
        session,
        crypto,
        admin_ctx,
        _settings(),
        OAuthClientCreate(
            issuer=ISSUER,
            client_id="public-client-id",
            client_secret=FAKE_SECRET,
            token_endpoint_auth_method="client_secret_post",
            scopes="read write",
        ),
        **REQ,  # type: ignore[arg-type]
    )

    assert out.client_id == "public-client-id"
    assert out.client_secret_configured is True
    assert out.source == "manual"
    assert out.redirect_uri == redirect_uri(_settings())
    assert FAKE_SECRET not in out.model_dump_json()


async def test_the_stored_secret_is_encrypted_under_its_own_secret_type(
    session: AsyncSession, crypto: SecretCrypto, admin_ctx: WorkspaceContext
) -> None:
    await service.create_client(
        session,
        crypto,
        admin_ctx,
        _settings(),
        OAuthClientCreate(
            issuer=ISSUER,
            client_id="id",
            client_secret=FAKE_SECRET,
            token_endpoint_auth_method="client_secret_basic",
        ),
        **REQ,  # type: ignore[arg-type]
    )

    stored = await session.scalar(
        select(Secret).where(Secret.workspace_id == admin_ctx.workspace_id)
    )
    assert stored is not None
    assert stored.type == SecretType.OAUTH_CLIENT.value
    assert FAKE_SECRET.encode() not in stored.ciphertext


async def test_saving_the_same_issuer_twice_rotates_in_place(
    session: AsyncSession, crypto: SecretCrypto, admin_ctx: WorkspaceContext
) -> None:
    """Re-registering must not leave orphan ciphertext or a second row.

    The registration id is what connections point at, so a second save has to
    keep it — replacing the row would strand every connection using it.
    """
    settings = _settings()
    first = await service.create_client(
        session,
        crypto,
        admin_ctx,
        settings,
        OAuthClientCreate(
            issuer=ISSUER,
            client_id="id-1",
            client_secret=FAKE_SECRET,
            token_endpoint_auth_method="client_secret_post",
        ),
        **REQ,  # type: ignore[arg-type]
    )
    second = await service.create_client(
        session,
        crypto,
        admin_ctx,
        settings,
        OAuthClientCreate(
            issuer=ISSUER,
            client_id="id-2",
            client_secret=f"{FAKE_SECRET}-rotated",
            token_endpoint_auth_method="client_secret_post",
        ),
        **REQ,  # type: ignore[arg-type]
    )

    assert first.id == second.id
    assert second.client_id == "id-2"
    rows = await session.scalar(
        select(func.count()).select_from(
            select(OAuthClientRegistration)
            .where(OAuthClientRegistration.workspace_id == admin_ctx.workspace_id)
            .subquery()
        )
    )
    assert rows == 1
    secrets_held = await session.scalar(
        select(func.count()).select_from(
            select(Secret).where(Secret.workspace_id == admin_ctx.workspace_id).subquery()
        )
    )
    assert secrets_held == 1


async def test_a_public_client_stores_no_secret_at_all(
    session: AsyncSession, crypto: SecretCrypto, admin_ctx: WorkspaceContext
) -> None:
    """Fewer secrets at rest is strictly better; PKCE carries a public client."""
    out = await service.create_client(
        session,
        crypto,
        admin_ctx,
        _settings(),
        OAuthClientCreate(issuer=ISSUER, client_id="public", token_endpoint_auth_method="none"),
        **REQ,  # type: ignore[arg-type]
    )

    assert out.client_secret_configured is False
    held = await session.scalar(
        select(func.count()).select_from(
            select(Secret).where(Secret.workspace_id == admin_ctx.workspace_id).subquery()
        )
    )
    assert held == 0


async def test_a_confidential_method_without_a_secret_is_refused(
    session: AsyncSession, crypto: SecretCrypto, admin_ctx: WorkspaceContext
) -> None:
    with pytest.raises(HTTPException) as excinfo:
        await service.create_client(
            session,
            crypto,
            admin_ctx,
            _settings(),
            OAuthClientCreate(
                issuer=ISSUER, client_id="id", token_endpoint_auth_method="client_secret_post"
            ),
            **REQ,  # type: ignore[arg-type]
        )
    assert excinfo.value.status_code == 400


@pytest.mark.parametrize(
    "issuer",
    [
        "http://169.254.169.254",
        "https://127.0.0.1",
        "http://auth.example.com",
        "not-a-url",
    ],
)
async def test_an_issuer_that_fails_the_outbound_policy_is_refused(
    session: AsyncSession,
    crypto: SecretCrypto,
    admin_ctx: WorkspaceContext,
    issuer: str,
) -> None:
    """The issuer becomes a host Jhin dials, so it goes through the policy."""
    with pytest.raises(HTTPException) as excinfo:
        await service.create_client(
            session,
            crypto,
            admin_ctx,
            _settings(),
            OAuthClientCreate(issuer=issuer, client_id="id"),
            **REQ,  # type: ignore[arg-type]
        )
    assert excinfo.value.status_code == 400


async def test_forgetting_a_registration_removes_its_secret_too(
    session: AsyncSession, crypto: SecretCrypto, admin_ctx: WorkspaceContext
) -> None:
    created = await service.create_client(
        session,
        crypto,
        admin_ctx,
        _settings(),
        OAuthClientCreate(
            issuer=ISSUER,
            client_id="id",
            client_secret=FAKE_SECRET,
            token_endpoint_auth_method="client_secret_post",
        ),
        **REQ,  # type: ignore[arg-type]
    )

    await service.delete_client(session, crypto, admin_ctx, created.id, **REQ)  # type: ignore[arg-type]

    assert await session.get(OAuthClientRegistration, created.id) is None
    remaining = await session.scalar(
        select(func.count()).select_from(
            select(Secret).where(Secret.workspace_id == admin_ctx.workspace_id).subquery()
        )
    )
    assert remaining == 0


async def test_deleting_a_registration_from_another_workspace_is_a_404(
    session: AsyncSession, crypto: SecretCrypto, admin_ctx: WorkspaceContext
) -> None:
    with pytest.raises(HTTPException) as excinfo:
        await service.delete_client(session, crypto, admin_ctx, uuid4(), **REQ)  # type: ignore[arg-type]
    assert excinfo.value.status_code == 404


async def test_listing_reports_how_many_connections_depend_on_each_client(
    session: AsyncSession, crypto: SecretCrypto, admin_ctx: WorkspaceContext
) -> None:
    """So an admin about to delete one can see what it would strand."""
    from jhin_db.models import Connection

    created = await service.create_client(
        session,
        crypto,
        admin_ctx,
        _settings(),
        OAuthClientCreate(issuer=ISSUER, client_id="id"),
        **REQ,  # type: ignore[arg-type]
    )
    session.add(
        Connection(
            workspace_id=admin_ctx.workspace_id,
            connector_type="mcp",
            name="Depends on it",
            auth_type="oauth",
            oauth_client_registration_id=created.id,
        )
    )
    await session.commit()

    listed = await service.list_clients(session, crypto, admin_ctx)

    assert [row.connection_count for row in listed] == [1]


async def test_credentials_round_trip_through_real_encryption(
    session: AsyncSession, crypto: SecretCrypto, admin_ctx: WorkspaceContext
) -> None:
    """The store is what the token endpoint reads from; it must decrypt."""
    settings = _settings()
    await service.create_client(
        session,
        crypto,
        admin_ctx,
        settings,
        OAuthClientCreate(
            issuer=ISSUER,
            client_id="id",
            client_secret=FAKE_SECRET,
            token_endpoint_auth_method="client_secret_basic",
        ),
        **REQ,  # type: ignore[arg-type]
    )

    found = await OAuthClientStore(session, crypto).get(
        admin_ctx.workspace_id, issuer=ISSUER, redirect_uri=redirect_uri(settings)
    )
    assert found is not None
    _row, credentials = found
    assert credentials.client_secret == FAKE_SECRET
    assert credentials.token_endpoint_auth_method == "client_secret_basic"
