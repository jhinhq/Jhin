"""Connection service tests (plan 6.9, 48.1): manifest-driven validation,
encrypted credential storage, once-only webhook secret, verify/rotate/delete
lifecycle — all against in-memory SQLite and the in-process fake GitHub."""

from collections.abc import Iterator
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_api.connections import service
from jhin_api.connections.schemas import ConnectionOut
from jhin_api.deps import WorkspaceContext
from jhin_connectors.testing.fake_github import FakeGitHubServer
from jhin_db.models import Connection, Secret
from jhin_domain import ConnectionStatus, new_uuid7
from jhin_secrets import SecretCrypto

REQ = {"request_id": new_uuid7(), "ip_hash": "test"}


@pytest.fixture
def fake_github() -> Iterator[FakeGitHubServer]:
    with FakeGitHubServer() as server:
        yield server


async def create_github_connection(
    session: AsyncSession,
    crypto: SecretCrypto,
    ctx: WorkspaceContext,
    *,
    token: str = "fake-github-pat",
    base_url: str = "",
    name: str = "GitHub main",
) -> tuple[Connection, str | None]:
    config: dict[str, object] = {"base_url": base_url} if base_url else {}
    return await service.create_connection(
        session,
        crypto,
        ctx,
        connector_type="github",
        name=name,
        auth_type="pat",
        credentials={"token": token},
        config=config,
        **REQ,
    )


async def test_create_stores_encrypted_credentials_and_webhook_secret(
    session: AsyncSession, crypto: SecretCrypto, admin_ctx: WorkspaceContext
) -> None:
    connection, webhook_secret = await create_github_connection(session, crypto, admin_ctx)
    assert connection.status == ConnectionStatus.ACTIVE.value
    assert len(connection.public_id) == 32
    # GitHub supports webhooks: signing secret returned exactly once.
    assert webhook_secret and len(webhook_secret) > 30

    secrets = (await session.scalars(select(Secret))).all()
    assert {s.type for s in secrets} == {"connection_credentials", "webhook_secret"}
    blob = b"".join(s.ciphertext for s in secrets)
    assert b"fake-github-pat" not in blob  # encrypted, not embedded


async def test_connection_out_never_exposes_secret_material(
    session: AsyncSession, crypto: SecretCrypto, admin_ctx: WorkspaceContext
) -> None:
    connection, webhook_secret = await create_github_connection(session, crypto, admin_ctx)
    out = ConnectionOut.model_validate(connection, from_attributes=True)
    serialized = out.model_dump_json()
    assert "fake-github-pat" not in serialized
    assert webhook_secret is not None and webhook_secret not in serialized
    forbidden = {"credentials", "encrypted_secret_id", "webhook_secret_id", "ciphertext"}
    assert set(ConnectionOut.model_fields) & forbidden == set()


@pytest.mark.parametrize(
    ("kwargs", "fragment"),
    [
        ({"connector_type": "sharepoint"}, "Unknown connector type"),
        ({"auth_type": "oauth"}, "not supported"),
        ({"credentials": {}}, "Missing required credential fields: token"),
        ({"credentials": {"token": "x", "extra": "y"}}, "Unknown credential fields: extra"),
        ({"config": {"bogus": 1}}, "Unknown config fields: bogus"),
    ],
)
async def test_create_validates_against_manifest(
    session: AsyncSession,
    crypto: SecretCrypto,
    admin_ctx: WorkspaceContext,
    kwargs: dict[str, object],
    fragment: str,
) -> None:
    base: dict[str, object] = {
        "connector_type": "github",
        "name": "Bad",
        "auth_type": "pat",
        "credentials": {"token": "t"},
        "config": {},
    }
    base.update(kwargs)
    with pytest.raises(HTTPException) as excinfo:
        await service.create_connection(session, crypto, admin_ctx, **base, **REQ)  # type: ignore[arg-type]
    assert excinfo.value.status_code == 422
    assert fragment in str(excinfo.value.detail)


async def test_duplicate_name_conflicts(
    session: AsyncSession, crypto: SecretCrypto, admin_ctx: WorkspaceContext
) -> None:
    await create_github_connection(session, crypto, admin_ctx, name="Same")
    with pytest.raises(HTTPException) as excinfo:
        await create_github_connection(session, crypto, admin_ctx, name="Same")
    assert excinfo.value.status_code == 409


async def test_verify_updates_health_fields(
    session: AsyncSession,
    crypto: SecretCrypto,
    admin_ctx: WorkspaceContext,
    fake_github: FakeGitHubServer,
) -> None:
    connection, _ = await create_github_connection(
        session, crypto, admin_ctx, base_url=fake_github.base_url
    )
    updated, health = await service.verify_connection(
        session, crypto, admin_ctx, connection.id, **REQ
    )
    assert health.ok
    assert updated.status == ConnectionStatus.ACTIVE.value
    assert updated.last_verified_at is not None
    assert updated.last_error is None


async def test_verify_failure_sets_error_status_without_leaking_token(
    session: AsyncSession,
    crypto: SecretCrypto,
    admin_ctx: WorkspaceContext,
    fake_github: FakeGitHubServer,
) -> None:
    connection, _ = await create_github_connection(
        session, crypto, admin_ctx, token="wrong-token-abc", base_url=fake_github.base_url
    )
    updated, health = await service.verify_connection(
        session, crypto, admin_ctx, connection.id, **REQ
    )
    assert not health.ok
    assert updated.status == ConnectionStatus.ERROR.value
    assert updated.last_error is not None
    assert "wrong-token-abc" not in updated.last_error


async def test_rotate_replaces_credential_and_resets_health(
    session: AsyncSession,
    crypto: SecretCrypto,
    admin_ctx: WorkspaceContext,
    fake_github: FakeGitHubServer,
) -> None:
    connection, _ = await create_github_connection(
        session, crypto, admin_ctx, token="wrong", base_url=fake_github.base_url
    )
    await service.verify_connection(session, crypto, admin_ctx, connection.id, **REQ)
    assert connection.status == ConnectionStatus.ERROR.value

    rotated = await service.rotate_credentials(
        session, crypto, admin_ctx, connection.id, credentials={"token": "fake-github-pat"}, **REQ
    )
    assert rotated.status == ConnectionStatus.ACTIVE.value
    assert rotated.last_verified_at is None  # unproven until re-verified

    _, health = await service.verify_connection(session, crypto, admin_ctx, connection.id, **REQ)
    assert health.ok


async def test_delete_removes_connection_and_its_secrets(
    session: AsyncSession, crypto: SecretCrypto, admin_ctx: WorkspaceContext
) -> None:
    connection, _ = await create_github_connection(session, crypto, admin_ctx)
    await service.delete_connection(session, admin_ctx, connection.id, **REQ)
    assert (await session.scalars(select(Connection))).all() == []
    assert (await session.scalars(select(Secret))).all() == []


async def test_workspace_isolation_on_get(
    session: AsyncSession, crypto: SecretCrypto, admin_ctx: WorkspaceContext
) -> None:
    connection, _ = await create_github_connection(session, crypto, admin_ctx)
    with pytest.raises(HTTPException) as excinfo:
        await service.get_connection(session, uuid4(), connection.id)
    assert excinfo.value.status_code == 404


async def test_unknown_connection_verify_404(
    session: AsyncSession, crypto: SecretCrypto, admin_ctx: WorkspaceContext
) -> None:
    with pytest.raises(HTTPException) as excinfo:
        await service.verify_connection(
            session, crypto, admin_ctx, UUID("00000000-0000-0000-0000-000000000001"), **REQ
        )
    assert excinfo.value.status_code == 404
