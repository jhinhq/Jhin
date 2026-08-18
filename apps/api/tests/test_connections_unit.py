"""Connection service tests (plan 6.9, 48.1): manifest-driven validation,
encrypted credential storage, once-only webhook secret, verify/rotate/delete
lifecycle — all against in-memory SQLite and the in-process fake GitHub."""

from collections.abc import Iterator
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_api.connections import service
from jhin_api.connections.schemas import ConnectionOut
from jhin_api.deps import WorkspaceContext
from jhin_connectors import ConnectionHealth, VerifyContext
from jhin_connectors.testing.fake_github import FakeGitHubServer
from jhin_db.models import Connection, Secret
from jhin_domain import ConnectionStatus, new_uuid7
from jhin_secrets import SecretCrypto, SecretStore

REQ = {"request_id": new_uuid7(), "ip_hash": "test"}


class _EchoingProviderConnector:
    """Provider double that deliberately reflects credentials in its output."""

    async def verify_connection(self, ctx: VerifyContext) -> ConnectionHealth:
        token = ctx.credentials["token"]
        return ConnectionHealth(
            ok=False,
            message=f"{'m' * 1_990}{token}:{'m' * 3_000}",
            details={
                f"{'k' * 1_990}{token}:{'k' * 3_000}": f"{token}:{'d' * 3_000}",
                f"{'k' * 1_990}[REDACTED]": "collision-value",
            },
        )

    async def fetch_metadata(self, ctx: VerifyContext) -> dict[Any, Any]:
        token = ctx.credentials["token"]
        metadata: dict[Any, Any] = {
            _ProviderMetadataKey(f"{'k' * 1_990}{token}:{'k' * 3_000}"): token,
            7: "integer-key-value",
            "7": "string-key-value",
            "nested": [
                {"label": f"{token}:{'n' * 3_000}"},
                (token, f"{'t' * 1_990}{token}:{'t' * 3_000}"),
            ],
        }
        return metadata


class _ProviderMetadataKey:
    """Hashable provider key whose string form deliberately contains a secret."""

    def __init__(self, value: str) -> None:
        self._value = value

    def __str__(self) -> str:
        return self._value


class _OversizedProviderConnector:
    def __init__(self, metadata: dict[str, Any] | None = None) -> None:
        self._metadata = metadata or {}

    async def verify_connection(self, _ctx: VerifyContext) -> ConnectionHealth:
        return ConnectionHealth(
            ok=True,
            message="ok",
            details={f"detail-{index}": "value" for index in range(300)},
        )

    async def fetch_metadata(self, _ctx: VerifyContext) -> dict[str, Any]:
        return self._metadata


class _RaisingProviderConnector:
    async def verify_connection(self, ctx: VerifyContext) -> ConnectionHealth:
        raise RuntimeError(f"provider reflected credential {ctx.credentials['token']}")


def _deep_provider_metadata(depth: int) -> dict[str, Any]:
    value: dict[str, Any] = {"leaf": "value"}
    for _ in range(depth):
        value = {"nested": value}
    return value


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


async def test_verify_redacts_then_bounds_all_provider_health_strings(
    session: AsyncSession,
    crypto: SecretCrypto,
    admin_ctx: WorkspaceContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = f"provider-echo-secret-{new_uuid7()}"
    connection, _ = await create_github_connection(
        session,
        crypto,
        admin_ctx,
        token=token,
        name="Echoing provider health",
    )
    connector = _EchoingProviderConnector()
    monkeypatch.setattr(service, "get_connector", lambda _connector_type: connector)

    updated, health = await service.verify_connection(
        session, crypto, admin_ctx, connection.id, **REQ
    )

    assert health.ok is False
    assert token not in health.message
    assert len(health.message) == 2_000
    assert health.message.endswith("[REDACTED]")
    assert len(health.details) == 2
    [(detail_key, detail_value), (collision_key, collision_value)] = health.details.items()
    assert token not in detail_key
    assert token not in detail_value
    assert len(detail_key) == 2_000
    assert detail_key.endswith("[REDACTED]")
    assert len(detail_value) == 2_000
    assert collision_key.endswith("#2")
    assert collision_value == "collision-value"
    assert updated.last_error == health.message
    assert updated.last_error is not None
    assert token not in updated.last_error
    assert len(updated.last_error) == 2_000


async def test_verify_hides_arbitrary_provider_exception_and_raw_cause(
    session: AsyncSession,
    crypto: SecretCrypto,
    admin_ctx: WorkspaceContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = f"verify-exception-secret-{new_uuid7()}"
    connection, _ = await create_github_connection(
        session,
        crypto,
        admin_ctx,
        token=token,
        name="Raising provider health",
    )
    monkeypatch.setattr(
        service,
        "get_connector",
        lambda _connector_type: _RaisingProviderConnector(),
    )

    with pytest.raises(HTTPException) as excinfo:
        await service.verify_connection(session, crypto, admin_ctx, connection.id, **REQ)

    assert excinfo.value.status_code == 502
    assert excinfo.value.detail == "Provider connection verification failed"
    assert token not in str(excinfo.value)
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__suppress_context__ is True
    assert connection.last_error is None


async def test_verify_rejects_malformed_stored_credentials_without_leaking(
    session: AsyncSession, crypto: SecretCrypto, admin_ctx: WorkspaceContext
) -> None:
    connection, _ = await create_github_connection(session, crypto, admin_ctx)
    assert connection.encrypted_secret_id is not None
    malformed = '{"token": {"nested": "never-leak-this-secret"}}'
    await SecretStore(session, crypto).rotate(
        admin_ctx.workspace_id, connection.encrypted_secret_id, malformed
    )

    with pytest.raises(HTTPException) as excinfo:
        await service.verify_connection(session, crypto, admin_ctx, connection.id, **REQ)

    assert excinfo.value.status_code == 422
    assert "malformed" in str(excinfo.value.detail).lower()
    assert "never-leak-this-secret" not in str(excinfo.value.detail)


async def test_metadata_redacts_then_bounds_nested_provider_strings(
    session: AsyncSession,
    crypto: SecretCrypto,
    admin_ctx: WorkspaceContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = f"metadata-echo-secret-{new_uuid7()}"
    connection, _ = await create_github_connection(
        session,
        crypto,
        admin_ctx,
        token=token,
        name="Echoing provider metadata",
    )
    connector = _EchoingProviderConnector()
    monkeypatch.setattr(service, "get_connector", lambda _connector_type: connector)

    metadata = await service.fetch_metadata(session, crypto, admin_ctx, connection.id)

    [(redacted_key, redacted_value), *other_items] = metadata.items()
    assert token not in redacted_key
    assert len(redacted_key) == 2_000
    assert redacted_key.endswith("[REDACTED]")
    assert redacted_value == "[REDACTED]"
    assert all(isinstance(key, str) for key in metadata)
    assert metadata["7"] == "integer-key-value"
    assert metadata["7#2"] == "string-key-value"
    assert len(other_items) == 3
    nested_value = metadata["nested"]
    assert isinstance(nested_value, list)
    assert isinstance(nested_value[0], dict)
    nested_label = nested_value[0]["label"]
    assert isinstance(nested_label, str)
    assert token not in nested_label
    assert len(nested_label) == 2_000
    assert isinstance(nested_value[1], tuple)
    assert nested_value[1][0] == "[REDACTED]"
    assert token not in nested_value[1][1]
    assert len(nested_value[1][1]) == 2_000
    assert nested_value[1][1].endswith("[REDACTED]")


async def test_metadata_rejects_malformed_stored_credentials_as_422(
    session: AsyncSession, crypto: SecretCrypto, admin_ctx: WorkspaceContext
) -> None:
    connection, _ = await create_github_connection(
        session, crypto, admin_ctx, name="Malformed metadata credential"
    )
    assert connection.encrypted_secret_id is not None
    malformed = '{"token": {"nested": "metadata-never-leak-secret"}}'
    await SecretStore(session, crypto).rotate(
        admin_ctx.workspace_id, connection.encrypted_secret_id, malformed
    )

    with pytest.raises(HTTPException) as excinfo:
        await service.fetch_metadata(session, crypto, admin_ctx, connection.id)

    assert excinfo.value.status_code == 422
    assert "malformed" in str(excinfo.value.detail).lower()
    assert "metadata-never-leak-secret" not in str(excinfo.value.detail)


@pytest.mark.parametrize(
    "metadata",
    [
        {f"item-{index}": index for index in range(300)},
        {"nested": {f"k-{i}": "x" * 2_000 for i in range(20)}},
        _deep_provider_metadata(20),
    ],
    ids=["too-many-items", "too-many-total-bytes", "too-deep"],
)
async def test_metadata_rejects_oversized_provider_collections(
    session: AsyncSession,
    crypto: SecretCrypto,
    admin_ctx: WorkspaceContext,
    monkeypatch: pytest.MonkeyPatch,
    metadata: dict[str, Any],
) -> None:
    connection, _ = await create_github_connection(session, crypto, admin_ctx)
    connector = _OversizedProviderConnector(metadata)
    monkeypatch.setattr(service, "get_connector", lambda _connector_type: connector)

    with pytest.raises(HTTPException) as excinfo:
        await service.fetch_metadata(session, crypto, admin_ctx, connection.id)

    assert excinfo.value.status_code == 502
    assert "provider" in str(excinfo.value.detail).lower()


async def test_verify_rejects_oversized_provider_details_without_persisting_them(
    session: AsyncSession,
    crypto: SecretCrypto,
    admin_ctx: WorkspaceContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection, _ = await create_github_connection(session, crypto, admin_ctx)
    connector = _OversizedProviderConnector()
    monkeypatch.setattr(service, "get_connector", lambda _connector_type: connector)

    with pytest.raises(HTTPException) as excinfo:
        await service.verify_connection(session, crypto, admin_ctx, connection.id, **REQ)

    assert excinfo.value.status_code == 502
    assert connection.last_error is None


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
