"""Connection service and route tests (plan 6.9, 48.1)."""

from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar, TypedDict
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_api.connections import router as connections_router_module
from jhin_api.connections import service
from jhin_api.connections.router import router as connections_router
from jhin_api.connections.schemas import ConnectionOut
from jhin_api.deps import AuthContext, WorkspaceContext, get_current_auth, get_db
from jhin_api.settings import Settings
from jhin_connectors import (
    AuthSchemeSpec,
    ConfigFieldSpec,
    ConnectionHealth,
    ConnectorManifest,
    SecretFieldSpec,
    VerifyContext,
)
from jhin_connectors.testing.fake_github import FakeGitHubServer
from jhin_db.models import (
    Agent,
    AgentCapabilityGrant,
    AuditEvent,
    Connection,
    Secret,
    User,
    UserSession,
    Workspace,
    WorkspaceMembership,
)
from jhin_domain import ConnectionStatus, WorkspaceRole, new_uuid7
from jhin_secrets import SecretCrypto, SecretStore  # type: ignore[import-untyped]


class _RequestAuditArgs(TypedDict):
    request_id: UUID
    ip_hash: str


REQ: _RequestAuditArgs = {"request_id": new_uuid7(), "ip_hash": "test"}
CSRF_TOKEN = "connection-route-csrf"
CSRF_HEADERS = {"x-csrf-token": CSRF_TOKEN}
MAX_SENSITIVE_CONNECTION_BODY_BYTES = 65_536


class ChunkedRequestBody(httpx.AsyncByteStream):
    def __init__(self, *chunks: bytes) -> None:
        self.chunks = chunks
        self.chunks_yielded = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            self.chunks_yielded += 1
            yield chunk


class TrackingBytearray(bytearray):
    extended_sizes: ClassVar[list[int]] = []

    def extend(self, value: Any) -> None:
        type(self).extended_sizes.append(len(value))
        super().extend(value)


class _ProviderSuppliedManifest:
    connector_type = "providerhooks"
    auth_schemes = (
        AuthSchemeSpec(
            type="token",
            label="Provider token",
            secret_fields=(SecretFieldSpec(name="token", label="Token"),),
        ),
    )
    config_fields: tuple[Any, ...] = ()
    webhook_events = ("deployment.ready",)
    supports_webhooks = True
    webhook_secret_mode = "provider_supplied"
    webhook_signature_algorithm = "sha1"
    webhook_setup_help = "Copy the provider-generated secret into Jhin."

    def auth_scheme(self, auth_type: str) -> AuthSchemeSpec | None:
        return self.auth_schemes[0] if auth_type == "token" else None


class _ProviderSuppliedConnector:
    manifest = _ProviderSuppliedManifest()

    def validate_settings(self, _auth_type: str, config: dict[str, Any]) -> dict[str, Any]:
        return dict(config)


class _TypedSettingsConnector:
    manifest = ConnectorManifest(
        connector_type="typedsettings",
        display_name="Typed settings",
        icon="settings",
        auth_schemes=(
            AuthSchemeSpec(
                type="token",
                label="Token",
                secret_fields=(SecretFieldSpec(name="token", label="Token"),),
            ),
        ),
        config_fields=(
            ConfigFieldSpec(
                name="statement_timeout_ms",
                label="Statement timeout",
                kind="integer",
                required=True,
                minimum=100,
                maximum=10_000,
            ),
            ConfigFieldSpec(
                name="allow_writes",
                label="Allow writes",
                kind="boolean",
                default=False,
            ),
        ),
    )

    def validate_settings(self, auth_type: str, config: dict[str, Any]) -> dict[str, Any]:
        return {**config, "validation_marker": f"{auth_type}:ok"}


@pytest.fixture
def typed_settings_connector(monkeypatch: pytest.MonkeyPatch) -> _TypedSettingsConnector:
    connector = _TypedSettingsConnector()
    original_get_connector = service.get_connector

    def get_connector(connector_type: str) -> Any:
        if connector_type == connector.manifest.connector_type:
            return connector
        return original_get_connector(connector_type)

    monkeypatch.setattr(service, "get_connector", get_connector)
    return connector


@pytest.fixture
def provider_supplied_connector(monkeypatch: pytest.MonkeyPatch) -> _ProviderSuppliedConnector:
    connector = _ProviderSuppliedConnector()
    original_get_connector = service.get_connector

    def get_connector(connector_type: str) -> Any:
        if connector_type == connector.manifest.connector_type:
            return connector
        return original_get_connector(connector_type)

    monkeypatch.setattr(service, "get_connector", get_connector)
    return connector


@dataclass
class ConnectionRouteHarness:
    client: httpx.AsyncClient
    actor: dict[str, User]
    users: dict[str, User]
    workspace_id: UUID


@pytest.fixture
async def connection_routes(
    session: AsyncSession,
    crypto: SecretCrypto,
    admin_ctx: WorkspaceContext,
) -> AsyncIterator[ConnectionRouteHarness]:
    viewer = User(
        email=f"connection-viewer-{new_uuid7().hex[:8]}@example.com",
        display_name="Connection Viewer",
        password_hash="x",
    )
    session.add(viewer)
    await session.flush()
    session.add_all(
        [
            WorkspaceMembership(
                workspace_id=admin_ctx.workspace_id,
                user_id=admin_ctx.user.id,
                role=WorkspaceRole.ADMIN.value,
            ),
            WorkspaceMembership(
                workspace_id=admin_ctx.workspace_id,
                user_id=viewer.id,
                role=WorkspaceRole.VIEWER.value,
            ),
        ]
    )
    await session.commit()

    users = {"admin": admin_ctx.user, "viewer": viewer}
    actor = {"user": users["admin"]}
    app = FastAPI()
    app.state.settings = Settings()
    app.state.secret_crypto = crypto

    @app.middleware("http")
    async def request_id(request: Request, call_next: Any) -> Any:
        request.state.request_id = new_uuid7()
        return await call_next(request)

    app.include_router(connections_router)

    async def override_db() -> AsyncIterator[AsyncSession]:
        yield session

    async def override_auth() -> AuthContext:
        user = actor["user"]
        return AuthContext(
            user=user,
            session_record=UserSession(
                user_id=user.id,
                token_hash=f"connection-route-{user.id}",
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            ),
        )

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_auth] = override_auth
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        client.cookies.set("jhin_csrf", CSRF_TOKEN)
        yield ConnectionRouteHarness(
            client=client,
            actor=actor,
            users=users,
            workspace_id=admin_ctx.workspace_id,
        )


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
def fake_github(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeGitHubServer]:
    with FakeGitHubServer() as server:
        monkeypatch.setenv("JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS", server.base_url)
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


async def create_provider_connection(
    routes: ConnectionRouteHarness,
    *,
    name: str = "Provider webhooks",
) -> httpx.Response:
    return await routes.client.post(
        f"/api/v1/workspaces/{routes.workspace_id}/connections",
        json={
            "connector_type": "providerhooks",
            "name": name,
            "auth_type": "token",
            "credentials": {"token": "provider-api-token"},
            "config": {},
        },
        headers=CSRF_HEADERS,
    )


def provider_webhook_secret_url(
    routes: ConnectionRouteHarness,
    connection_id: UUID,
) -> str:
    return f"/api/v1/workspaces/{routes.workspace_id}/connections/{connection_id}/webhook-secret"


async def assert_plaintext_marker_not_persisted(
    session: AsyncSession,
    workspace_id: UUID,
    plaintext_marker: str,
) -> None:
    secrets = (
        await session.scalars(select(Secret).where(Secret.workspace_id == workspace_id))
    ).all()
    secret_metadata = [
        (secret.name, secret.type, secret.secret_fingerprint, secret.masked_hint)
        for secret in secrets
    ]
    audits = (
        await session.scalars(select(AuditEvent).where(AuditEvent.workspace_id == workspace_id))
    ).all()
    connections = (
        await session.scalars(select(Connection).where(Connection.workspace_id == workspace_id))
    ).all()
    connection_metadata = [
        (
            connection.connector_type,
            connection.name,
            connection.auth_type,
            connection.config_json,
            connection.last_error,
        )
        for connection in connections
    ]
    assert plaintext_marker not in repr(secret_metadata)
    assert plaintext_marker not in repr([audit.metadata_json for audit in audits])
    assert plaintext_marker not in repr(connection_metadata)


async def assert_webhook_secret_not_persisted(
    session: AsyncSession,
    connection_id: UUID,
    plaintext_marker: str,
) -> None:
    connection = await session.get(Connection, connection_id)
    assert connection is not None
    await session.refresh(connection)
    assert connection.webhook_secret_id is None

    secrets = (await session.scalars(select(Secret))).all()
    assert all(secret.type != "webhook_secret" for secret in secrets)
    await assert_plaintext_marker_not_persisted(
        session,
        connection.workspace_id,
        plaintext_marker,
    )


def assert_credential_safe_error(
    response: httpx.Response,
    *,
    status_code: int,
    detail: str,
    plaintext_marker: str,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert response.status_code == status_code, response.text
    assert response.json() == {"detail": detail}
    captured = capsys.readouterr()
    assert plaintext_marker not in response.text
    assert plaintext_marker not in caplog.text
    assert plaintext_marker not in captured.out
    assert plaintext_marker not in captured.err


async def test_connection_create_invalid_credential_type_is_credential_safe(
    connection_routes: ConnectionRouteHarness,
    session: AsyncSession,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plaintext_marker = "invalid-create-credential-must-not-echo"
    connection_name = "Invalid create credential"

    response = await connection_routes.client.post(
        f"/api/v1/workspaces/{connection_routes.workspace_id}/connections",
        json={
            "connector_type": "github",
            "name": connection_name,
            "auth_type": "pat",
            "credentials": {"token": {"marker": plaintext_marker}},
            "config": {},
        },
        headers=CSRF_HEADERS,
    )

    assert_credential_safe_error(
        response,
        status_code=422,
        detail="Connection payload is invalid",
        plaintext_marker=plaintext_marker,
        caplog=caplog,
        capsys=capsys,
    )
    assert (
        await session.scalar(
            select(Connection).where(
                Connection.workspace_id == connection_routes.workspace_id,
                Connection.name == connection_name,
            )
        )
        is None
    )
    await assert_plaintext_marker_not_persisted(
        session,
        connection_routes.workspace_id,
        plaintext_marker,
    )


async def test_unknown_credential_key_is_credential_safe(
    connection_routes: ConnectionRouteHarness,
    session: AsyncSession,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plaintext_marker = "unknown-credential-key-must-not-echo"
    connection_name = "Unknown credential key"

    response = await connection_routes.client.post(
        f"/api/v1/workspaces/{connection_routes.workspace_id}/connections",
        json={
            "connector_type": "github",
            "name": connection_name,
            "auth_type": "pat",
            "credentials": {
                "token": "valid-token",
                plaintext_marker: "credential-value",
            },
            "config": {},
        },
        headers=CSRF_HEADERS,
    )

    assert_credential_safe_error(
        response,
        status_code=422,
        detail="Unknown credential fields",
        plaintext_marker=plaintext_marker,
        caplog=caplog,
        capsys=capsys,
    )
    assert (
        await session.scalar(
            select(Connection).where(
                Connection.workspace_id == connection_routes.workspace_id,
                Connection.name == connection_name,
            )
        )
        is None
    )
    await assert_plaintext_marker_not_persisted(
        session,
        connection_routes.workspace_id,
        plaintext_marker,
    )


async def test_connection_create_malformed_json_is_credential_safe(
    connection_routes: ConnectionRouteHarness,
    session: AsyncSession,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plaintext_marker = b"malformed-create-credential-must-not-echo"
    connection_name = "Malformed create credential"
    body = (
        b'{"connector_type":"github","name":"'
        + connection_name.encode()
        + b'","auth_type":"pat","credentials":{"token":"'
        + plaintext_marker
        + b'"'
    )

    response = await connection_routes.client.post(
        f"/api/v1/workspaces/{connection_routes.workspace_id}/connections",
        content=body,
        headers={**CSRF_HEADERS, "content-type": "application/json"},
    )

    marker = plaintext_marker.decode()
    assert_credential_safe_error(
        response,
        status_code=422,
        detail="Connection payload is invalid",
        plaintext_marker=marker,
        caplog=caplog,
        capsys=capsys,
    )
    assert (
        await session.scalar(
            select(Connection).where(
                Connection.workspace_id == connection_routes.workspace_id,
                Connection.name == connection_name,
            )
        )
        is None
    )
    await assert_plaintext_marker_not_persisted(
        session,
        connection_routes.workspace_id,
        marker,
    )


async def test_connection_create_large_credential_body_is_rejected_before_copy(
    connection_routes: ConnectionRouteHarness,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plaintext_marker = b"large-create-credential-must-not-echo"
    connection_name = "Large create credential"
    body = (
        b'{"connector_type":"github","name":"'
        + connection_name.encode()
        + b'","auth_type":"pat","credentials":{"token":"'
        + plaintext_marker
        + b"x" * MAX_SENSITIVE_CONNECTION_BODY_BYTES
        + b'"},"config":{}}'
    )
    request_body = ChunkedRequestBody(body)
    TrackingBytearray.extended_sizes = []
    monkeypatch.setattr(
        connections_router_module,
        "bytearray",
        TrackingBytearray,
        raising=False,
    )

    response = await connection_routes.client.post(
        f"/api/v1/workspaces/{connection_routes.workspace_id}/connections",
        content=request_body,
        headers={**CSRF_HEADERS, "content-type": "application/json"},
    )

    marker = plaintext_marker.decode()
    assert_credential_safe_error(
        response,
        status_code=413,
        detail="Connection payload is too large",
        plaintext_marker=marker,
        caplog=caplog,
        capsys=capsys,
    )
    assert request_body.chunks_yielded == 1
    assert TrackingBytearray.extended_sizes == []
    assert (
        await session.scalar(
            select(Connection).where(
                Connection.workspace_id == connection_routes.workspace_id,
                Connection.name == connection_name,
            )
        )
        is None
    )
    await assert_plaintext_marker_not_persisted(
        session,
        connection_routes.workspace_id,
        marker,
    )


async def test_sensitive_body_content_length_cap_rejects_before_stream_iteration(
    connection_routes: ConnectionRouteHarness,
    session: AsyncSession,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plaintext_marker = "declared-large-credential-must-not-echo"
    request_body = ChunkedRequestBody(plaintext_marker.encode())

    response = await connection_routes.client.post(
        f"/api/v1/workspaces/{connection_routes.workspace_id}/connections",
        content=request_body,
        headers={
            **CSRF_HEADERS,
            "content-type": "application/json",
            "content-length": str(MAX_SENSITIVE_CONNECTION_BODY_BYTES + 1),
        },
    )

    assert_credential_safe_error(
        response,
        status_code=413,
        detail="Connection payload is too large",
        plaintext_marker=plaintext_marker,
        caplog=caplog,
        capsys=capsys,
    )
    assert request_body.chunks_yielded == 0
    await assert_plaintext_marker_not_persisted(
        session,
        connection_routes.workspace_id,
        plaintext_marker,
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


async def test_generated_connector_returns_one_time_secret(
    connection_routes: ConnectionRouteHarness,
) -> None:
    response = await connection_routes.client.post(
        f"/api/v1/workspaces/{connection_routes.workspace_id}/connections",
        json={
            "connector_type": "github",
            "name": "Generated webhook secret",
            "auth_type": "pat",
            "credentials": {"token": "fake-github-pat"},
            "config": {},
        },
        headers=CSRF_HEADERS,
    )

    assert response.status_code == 201, response.text
    webhook = response.json()["webhook"]
    assert webhook["url_path"].startswith("/api/v1/webhooks/github/")
    assert len(webhook["secret"]) > 30
    assert webhook["secret_mode"] == "generated"
    assert webhook["signature_algorithm"] == "hmac-sha256"
    assert isinstance(webhook["help"], str)


async def test_provider_supplied_connector_returns_url_without_generated_secret(
    connection_routes: ConnectionRouteHarness,
    provider_supplied_connector: _ProviderSuppliedConnector,
    session: AsyncSession,
) -> None:
    response = await create_provider_connection(connection_routes)

    assert response.status_code == 201, response.text
    created = response.json()
    assert created["webhook"] == {
        "url_path": (f"/api/v1/webhooks/providerhooks/{created['connection']['public_id']}"),
        "secret": None,
        "secret_mode": "provider_supplied",
        "signature_algorithm": "sha1",
        "help": "Copy the provider-generated secret into Jhin.",
    }
    assert created["connection"]["webhook_secret_configured"] is False
    secrets = (await session.scalars(select(Secret))).all()
    assert [secret.type for secret in secrets] == ["connection_credentials"]


async def test_admin_can_store_and_rotate_provider_webhook_secret_without_readback(
    connection_routes: ConnectionRouteHarness,
    provider_supplied_connector: _ProviderSuppliedConnector,
    session: AsyncSession,
) -> None:
    created = await create_provider_connection(connection_routes, name="Rotated provider secret")
    assert created.status_code == 201, created.text
    connection_id = UUID(created.json()["connection"]["id"])
    url = (
        f"/api/v1/workspaces/{connection_routes.workspace_id}/connections/"
        f"{connection_id}/webhook-secret"
    )

    first_plaintext = "provider-webhook-secret-one"
    first = await connection_routes.client.put(
        url,
        json={"secret": first_plaintext},
        headers=CSRF_HEADERS,
    )
    assert first.status_code == 204, first.text
    assert first.content == b""
    assert first_plaintext not in first.text

    connection = await session.get(Connection, connection_id)
    assert connection is not None
    assert connection.webhook_secret_id is not None
    stored = await session.get(Secret, connection.webhook_secret_id)
    assert stored is not None
    first_ciphertext = stored.ciphertext

    second_plaintext = "provider-webhook-secret-two"
    second = await connection_routes.client.put(
        url,
        json={"secret": second_plaintext},
        headers=CSRF_HEADERS,
    )
    assert second.status_code == 204, second.text
    assert second.content == b""
    assert second_plaintext not in second.text
    await session.refresh(stored)
    assert stored.ciphertext != first_ciphertext

    audit_actions = list(
        await session.scalars(
            select(AuditEvent.action).where(
                AuditEvent.target_id == connection_id,
                AuditEvent.action.in_(
                    {
                        "connection.webhook_secret_configured",
                        "connection.webhook_secret_rotated",
                    }
                ),
            )
        )
    )
    assert audit_actions == [
        "connection.webhook_secret_configured",
        "connection.webhook_secret_rotated",
    ]


async def test_short_webhook_secret_validation_is_credential_safe(
    connection_routes: ConnectionRouteHarness,
    provider_supplied_connector: _ProviderSuppliedConnector,
    session: AsyncSession,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    created = await create_provider_connection(connection_routes, name="Short secret rejected")
    assert created.status_code == 201, created.text
    connection_id = UUID(created.json()["connection"]["id"])
    plaintext = "short-secret"

    response = await connection_routes.client.put(
        provider_webhook_secret_url(connection_routes, connection_id),
        json={"secret": plaintext},
        headers=CSRF_HEADERS,
    )

    assert_credential_safe_error(
        response,
        status_code=422,
        detail="Webhook secret payload is invalid",
        plaintext_marker=plaintext,
        caplog=caplog,
        capsys=capsys,
    )
    await assert_webhook_secret_not_persisted(session, connection_id, plaintext)


async def test_oversize_webhook_secret_validation_is_credential_safe(
    connection_routes: ConnectionRouteHarness,
    provider_supplied_connector: _ProviderSuppliedConnector,
    session: AsyncSession,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    created = await create_provider_connection(
        connection_routes,
        name="Oversize secret rejected",
    )
    assert created.status_code == 201, created.text
    connection_id = UUID(created.json()["connection"]["id"])
    plaintext_marker = "oversize-provider-secret-must-not-echo"
    plaintext = plaintext_marker + "x" * (4_097 - len(plaintext_marker))

    response = await connection_routes.client.put(
        provider_webhook_secret_url(connection_routes, connection_id),
        json={"secret": plaintext},
        headers=CSRF_HEADERS,
    )

    assert_credential_safe_error(
        response,
        status_code=422,
        detail="Webhook secret payload is invalid",
        plaintext_marker=plaintext_marker,
        caplog=caplog,
        capsys=capsys,
    )
    assert plaintext not in response.text
    await assert_webhook_secret_not_persisted(session, connection_id, plaintext_marker)


async def test_webhook_secret_payload_rejects_extra_fields_without_persisting(
    connection_routes: ConnectionRouteHarness,
    provider_supplied_connector: _ProviderSuppliedConnector,
    session: AsyncSession,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    created = await create_provider_connection(connection_routes, name="Extra field rejected")
    assert created.status_code == 201, created.text
    connection_id = UUID(created.json()["connection"]["id"])
    plaintext = "valid-primary-webhook-secret"
    extra_plaintext = "extra-field-secret-must-not-echo"

    response = await connection_routes.client.put(
        provider_webhook_secret_url(connection_routes, connection_id),
        json={"secret": plaintext, "confirmation": extra_plaintext},
        headers=CSRF_HEADERS,
    )

    assert_credential_safe_error(
        response,
        status_code=422,
        detail="Webhook secret payload is invalid",
        plaintext_marker=extra_plaintext,
        caplog=caplog,
        capsys=capsys,
    )
    assert plaintext not in response.text
    await assert_webhook_secret_not_persisted(session, connection_id, extra_plaintext)


async def test_webhook_secret_duplicate_json_key_is_credential_safe(
    connection_routes: ConnectionRouteHarness,
    provider_supplied_connector: _ProviderSuppliedConnector,
    session: AsyncSession,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    created = await create_provider_connection(connection_routes, name="Duplicate key rejected")
    assert created.status_code == 201, created.text
    connection_id = UUID(created.json()["connection"]["id"])
    first_plaintext = "first-provider-webhook-secret"
    duplicate_marker = "duplicate-key-secret-must-not-echo"
    body = (
        b'{"secret":"'
        + first_plaintext.encode()
        + b'","secret":"'
        + duplicate_marker.encode()
        + b'"}'
    )

    response = await connection_routes.client.put(
        provider_webhook_secret_url(connection_routes, connection_id),
        content=body,
        headers={**CSRF_HEADERS, "content-type": "application/json"},
    )

    assert_credential_safe_error(
        response,
        status_code=422,
        detail="Webhook secret payload is invalid",
        plaintext_marker=duplicate_marker,
        caplog=caplog,
        capsys=capsys,
    )
    assert first_plaintext not in response.text
    await assert_webhook_secret_not_persisted(session, connection_id, duplicate_marker)


async def test_webhook_secret_large_body_is_rejected_before_copy(
    connection_routes: ConnectionRouteHarness,
    provider_supplied_connector: _ProviderSuppliedConnector,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    created = await create_provider_connection(connection_routes, name="Large body rejected")
    assert created.status_code == 201, created.text
    connection_id = UUID(created.json()["connection"]["id"])
    plaintext_marker = b"huge-provider-secret-must-not-echo"
    body = b'{"secret":"' + plaintext_marker + b"x" * MAX_SENSITIVE_CONNECTION_BODY_BYTES + b'"}'
    request_body = ChunkedRequestBody(body)
    TrackingBytearray.extended_sizes = []
    monkeypatch.setattr(
        connections_router_module,
        "bytearray",
        TrackingBytearray,
        raising=False,
    )

    response = await connection_routes.client.put(
        provider_webhook_secret_url(connection_routes, connection_id),
        content=request_body,
        headers={**CSRF_HEADERS, "content-type": "application/json"},
    )

    marker = plaintext_marker.decode()
    assert_credential_safe_error(
        response,
        status_code=413,
        detail="Webhook secret payload is too large",
        plaintext_marker=marker,
        caplog=caplog,
        capsys=capsys,
    )
    assert request_body.chunks_yielded == 1
    assert TrackingBytearray.extended_sizes == []
    await assert_webhook_secret_not_persisted(session, connection_id, marker)


async def test_credential_rotation_invalid_type_is_credential_safe(
    connection_routes: ConnectionRouteHarness,
    provider_supplied_connector: _ProviderSuppliedConnector,
    session: AsyncSession,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    created = await create_provider_connection(connection_routes, name="Invalid rotation")
    assert created.status_code == 201, created.text
    connection_id = UUID(created.json()["connection"]["id"])
    connection = await session.get(Connection, connection_id)
    assert connection is not None
    assert connection.encrypted_secret_id is not None
    credential = await session.get(Secret, connection.encrypted_secret_id)
    assert credential is not None
    original_ciphertext = credential.ciphertext
    original_hint = credential.masked_hint
    plaintext_marker = "invalid-rotation-credential-must-not-echo"

    response = await connection_routes.client.post(
        (f"/api/v1/workspaces/{connection_routes.workspace_id}/connections/{connection_id}/rotate"),
        json={"credentials": {"token": {"marker": plaintext_marker}}},
        headers=CSRF_HEADERS,
    )

    assert_credential_safe_error(
        response,
        status_code=422,
        detail="Credential rotation payload is invalid",
        plaintext_marker=plaintext_marker,
        caplog=caplog,
        capsys=capsys,
    )
    await session.refresh(credential)
    assert credential.ciphertext == original_ciphertext
    assert credential.masked_hint == original_hint
    await assert_plaintext_marker_not_persisted(
        session,
        connection_routes.workspace_id,
        plaintext_marker,
    )


async def test_credential_rotation_large_body_is_rejected_before_copy(
    connection_routes: ConnectionRouteHarness,
    provider_supplied_connector: _ProviderSuppliedConnector,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    created = await create_provider_connection(connection_routes, name="Large rotation")
    assert created.status_code == 201, created.text
    connection_id = UUID(created.json()["connection"]["id"])
    connection = await session.get(Connection, connection_id)
    assert connection is not None
    assert connection.encrypted_secret_id is not None
    credential = await session.get(Secret, connection.encrypted_secret_id)
    assert credential is not None
    original_ciphertext = credential.ciphertext
    original_hint = credential.masked_hint
    plaintext_marker = b"large-rotation-credential-must-not-echo"
    body = (
        b'{"credentials":{"token":"'
        + plaintext_marker
        + b"x" * MAX_SENSITIVE_CONNECTION_BODY_BYTES
        + b'"}}'
    )
    request_body = ChunkedRequestBody(body)
    TrackingBytearray.extended_sizes = []
    monkeypatch.setattr(
        connections_router_module,
        "bytearray",
        TrackingBytearray,
        raising=False,
    )

    response = await connection_routes.client.post(
        (f"/api/v1/workspaces/{connection_routes.workspace_id}/connections/{connection_id}/rotate"),
        content=request_body,
        headers={**CSRF_HEADERS, "content-type": "application/json"},
    )

    marker = plaintext_marker.decode()
    assert_credential_safe_error(
        response,
        status_code=413,
        detail="Credential rotation payload is too large",
        plaintext_marker=marker,
        caplog=caplog,
        capsys=capsys,
    )
    assert request_body.chunks_yielded == 1
    assert TrackingBytearray.extended_sizes == []
    await session.refresh(credential)
    assert credential.ciphertext == original_ciphertext
    assert credential.masked_hint == original_hint
    await assert_plaintext_marker_not_persisted(
        session,
        connection_routes.workspace_id,
        marker,
    )


async def test_viewer_cannot_store_provider_webhook_secret(
    connection_routes: ConnectionRouteHarness,
    provider_supplied_connector: _ProviderSuppliedConnector,
    session: AsyncSession,
) -> None:
    created = await create_provider_connection(connection_routes, name="Viewer denied")
    assert created.status_code == 201, created.text
    connection_id = UUID(created.json()["connection"]["id"])
    connection_routes.actor["user"] = connection_routes.users["viewer"]

    response = await connection_routes.client.put(
        (
            f"/api/v1/workspaces/{connection_routes.workspace_id}/connections/"
            f"{connection_id}/webhook-secret"
        ),
        json={"secret": "viewer-cannot-store-this"},
        headers=CSRF_HEADERS,
    )

    assert response.status_code == 403, response.text
    connection = await session.get(Connection, connection_id)
    assert connection is not None
    assert connection.webhook_secret_id is None


async def test_webhook_secret_write_requires_csrf(
    connection_routes: ConnectionRouteHarness,
    provider_supplied_connector: _ProviderSuppliedConnector,
    session: AsyncSession,
) -> None:
    created = await create_provider_connection(connection_routes, name="CSRF denied")
    assert created.status_code == 201, created.text
    connection_id = UUID(created.json()["connection"]["id"])

    response = await connection_routes.client.put(
        (
            f"/api/v1/workspaces/{connection_routes.workspace_id}/connections/"
            f"{connection_id}/webhook-secret"
        ),
        json={"secret": "csrf-cannot-store-this"},
    )

    assert response.status_code == 403, response.text
    connection = await session.get(Connection, connection_id)
    assert connection is not None
    assert connection.webhook_secret_id is None


async def test_connection_output_only_exposes_webhook_secret_configured_boolean(
    connection_routes: ConnectionRouteHarness,
    provider_supplied_connector: _ProviderSuppliedConnector,
) -> None:
    submitted_secret = "write-only-provider-secret"
    created = await create_provider_connection(connection_routes, name="Write-only output")
    assert created.status_code == 201, created.text
    connection_id = created.json()["connection"]["id"]
    connection_url = (
        f"/api/v1/workspaces/{connection_routes.workspace_id}/connections/{connection_id}"
    )

    before_response = await connection_routes.client.get(connection_url)
    assert before_response.status_code == 200, before_response.text
    before = before_response.json()
    assert before["webhook_secret_configured"] is False

    stored = await connection_routes.client.put(
        f"{connection_url}/webhook-secret",
        json={"secret": submitted_secret},
        headers=CSRF_HEADERS,
    )
    assert stored.status_code == 204, stored.text
    after_response = await connection_routes.client.get(connection_url)
    assert after_response.status_code == 200, after_response.text
    after = after_response.json()

    assert after["webhook_secret_configured"] is True
    assert submitted_secret not in after_response.text
    assert "webhook_secret_id" not in after_response.text
    before_status = before.pop("webhook_secret_configured")
    after_status = after.pop("webhook_secret_configured")
    assert (before_status, after_status) == (False, True)
    assert after == before


async def test_connection_output_filters_unsafe_and_unknown_legacy_config(
    connection_routes: ConnectionRouteHarness,
    session: AsyncSession,
) -> None:
    url_credential_marker = "legacy-url-password-must-not-echo"
    unknown_marker = "legacy-unknown-config-must-not-echo"
    unsafe = Connection(
        workspace_id=connection_routes.workspace_id,
        connector_type="github",
        name="Unsafe legacy config",
        auth_type="pat",
        config_json={
            "base_url": f"https://legacy:{url_credential_marker}@api.github.com",
            "legacy_unknown": unknown_marker,
        },
        created_by_user_id=connection_routes.users["admin"].id,
    )
    valid = Connection(
        workspace_id=connection_routes.workspace_id,
        connector_type="github",
        name="Valid legacy config",
        auth_type="pat",
        config_json={
            "base_url": "https://api.github.com",
            "legacy_unknown": unknown_marker,
        },
        created_by_user_id=connection_routes.users["admin"].id,
    )
    session.add_all([unsafe, valid])
    await session.commit()

    unsafe_response = await connection_routes.client.get(
        f"/api/v1/workspaces/{connection_routes.workspace_id}/connections/{unsafe.id}"
    )
    valid_response = await connection_routes.client.get(
        f"/api/v1/workspaces/{connection_routes.workspace_id}/connections/{valid.id}"
    )

    assert unsafe_response.status_code == 200, unsafe_response.text
    assert valid_response.status_code == 200, valid_response.text
    assert unsafe_response.json()["config_json"] == {}
    assert valid_response.json()["config_json"] == {"base_url": "https://api.github.com"}
    serialized = unsafe_response.text + valid_response.text
    assert url_credential_marker not in serialized
    assert unknown_marker not in serialized


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


async def test_access_summary_reports_exact_relevant_grants_and_effective_access(
    connection_routes: ConnectionRouteHarness,
    session: AsyncSession,
) -> None:
    """The summary is connection-scoped and explains, but never widens, access."""
    connection_marker = "connection-config-secret-must-not-leak"
    connection = Connection(
        workspace_id=connection_routes.workspace_id,
        connector_type="vercel",
        name="Vercel production",
        auth_type="access_token",
        config_json={"private_marker": connection_marker},
    )
    authorized_agent = Agent(
        workspace_id=connection_routes.workspace_id,
        name="Ada Authorized",
        slug=f"ada-{new_uuid7().hex[:8]}",
        approval_policy_json=[{"private_policy": "must-not-leak"}],
    )
    incomplete_agent = Agent(
        workspace_id=connection_routes.workspace_id,
        name="Zed Incomplete",
        slug=f"zed-{new_uuid7().hex[:8]}",
    )
    other_connection = Connection(
        workspace_id=connection_routes.workspace_id,
        connector_type="vercel",
        name="Other Vercel",
        auth_type="access_token",
    )
    session.add_all([connection, authorized_agent, incomplete_agent, other_connection])
    await session.flush()
    connection_id = str(connection.id)
    project_scope = {"connection_id": connection_id, "project_id": "project-a"}
    session.add_all(
        [
            # An exact matching allow and deny prove deny precedence.
            AgentCapabilityGrant(
                workspace_id=connection_routes.workspace_id,
                agent_id=authorized_agent.id,
                capability="vercel.project.read",
                scope_json=project_scope,
                effect="allow",
            ),
            AgentCapabilityGrant(
                workspace_id=connection_routes.workspace_id,
                agent_id=authorized_agent.id,
                capability="vercel.project.read",
                scope_json=project_scope,
                effect="deny",
            ),
            # This exact connection scope remains authorized.
            AgentCapabilityGrant(
                workspace_id=connection_routes.workspace_id,
                agent_id=authorized_agent.id,
                capability="vercel.project.list",
                scope_json={"connection_id": connection_id},
                effect="allow",
            ),
            # A wildcard capability is relevant, but a deployment read grant
            # without deployment_id is not eligible for that tool.
            AgentCapabilityGrant(
                workspace_id=connection_routes.workspace_id,
                agent_id=authorized_agent.id,
                capability="vercel.*",
                scope_json=project_scope,
                effect="allow",
            ),
            AgentCapabilityGrant(
                workspace_id=connection_routes.workspace_id,
                agent_id=authorized_agent.id,
                capability="vercel.deployment.read",
                scope_json=project_scope,
                effect="allow",
            ),
            # Neither broad nor another-connection grants are relevant to
            # this connection summary.
            AgentCapabilityGrant(
                workspace_id=connection_routes.workspace_id,
                agent_id=authorized_agent.id,
                capability="vercel.project.read",
                scope_json={},
                effect="allow",
            ),
            AgentCapabilityGrant(
                workspace_id=connection_routes.workspace_id,
                agent_id=authorized_agent.id,
                capability="vercel.project.read",
                scope_json={"connection_id": str(other_connection.id), "project_id": "project-a"},
                effect="allow",
            ),
            AgentCapabilityGrant(
                workspace_id=connection_routes.workspace_id,
                agent_id=authorized_agent.id,
                capability="github.repository.read",
                scope_json={"connection_id": connection_id, "repository": "acme/app"},
                effect="allow",
            ),
            AgentCapabilityGrant(
                workspace_id=connection_routes.workspace_id,
                agent_id=incomplete_agent.id,
                capability="vercel.deployment.read",
                scope_json=project_scope,
                effect="allow",
            ),
        ]
    )
    await session.commit()

    response = await connection_routes.client.get(
        f"/api/v1/workspaces/{connection_routes.workspace_id}/connections/{connection.id}/access-summary"
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["connection_id"] == connection_id
    assert [agent["agent_name"] for agent in payload["agents"]] == [
        "Ada Authorized",
        "Zed Incomplete",
    ]
    ada, zed = payload["agents"]
    assert ada["authorized"] is True
    assert ada["authorized_tool_names"] == sorted(ada["authorized_tool_names"])
    assert "vercel.project.list" in ada["authorized_tool_names"]
    assert "vercel.project.read" not in ada["authorized_tool_names"]
    assert zed["authorized"] is False
    assert zed["authorized_tool_names"] == []

    ada_grants = {(grant["capability"], grant["effect"]): grant for grant in ada["grants"]}
    assert set(ada_grants) == {
        ("vercel.project.read", "allow"),
        ("vercel.project.read", "deny"),
        ("vercel.project.list", "allow"),
        ("vercel.*", "allow"),
        ("vercel.deployment.read", "allow"),
    }
    assert ada_grants[("vercel.project.read", "allow")]["scope"] == project_scope
    assert ada_grants[("vercel.project.read", "deny")]["scope"] == project_scope
    incomplete = ada_grants[("vercel.deployment.read", "allow")]
    assert incomplete["eligible_tool_names"] == []
    assert "deployment_id" in incomplete["eligibility_reason"]
    assert zed["grants"][0]["eligibility_reason"] is not None
    serialized = response.text
    for marker in (connection_marker, "must-not-leak", "encrypted_secret_id", "ciphertext"):
        assert marker not in serialized


async def test_access_summary_is_admin_only_and_hides_cross_workspace_connections(
    connection_routes: ConnectionRouteHarness,
    session: AsyncSession,
) -> None:
    connection = Connection(
        workspace_id=connection_routes.workspace_id,
        connector_type="vercel",
        name="Admin-only summary",
        auth_type="access_token",
    )
    other_workspace = Workspace(name="Other", slug=f"other-{new_uuid7().hex[:8]}")
    session.add_all([connection, other_workspace])
    await session.flush()
    foreign_connection = Connection(
        workspace_id=other_workspace.id,
        connector_type="vercel",
        name="Foreign connection",
        auth_type="access_token",
    )
    session.add(foreign_connection)
    await session.commit()
    base = f"/api/v1/workspaces/{connection_routes.workspace_id}/connections"

    connection_routes.actor["user"] = connection_routes.users["viewer"]
    forbidden = await connection_routes.client.get(f"{base}/{connection.id}/access-summary")
    assert forbidden.status_code == 403, forbidden.text

    connection_routes.actor["user"] = connection_routes.users["admin"]
    missing = await connection_routes.client.get(f"{base}/{foreign_connection.id}/access-summary")
    assert missing.status_code == 404, missing.text
    assert "Foreign connection" not in missing.text


async def test_access_summary_applies_unscoped_connector_deny_to_exact_allow(
    connection_routes: ConnectionRouteHarness,
    session: AsyncSession,
) -> None:
    connection = Connection(
        workspace_id=connection_routes.workspace_id,
        connector_type="vercel",
        name="Broad deny",
        auth_type="access_token",
    )
    agent = Agent(
        workspace_id=connection_routes.workspace_id,
        name="Denied agent",
        slug=f"denied-{new_uuid7().hex[:8]}",
    )
    session.add_all([connection, agent])
    await session.flush()
    scope = {"connection_id": str(connection.id), "project_id": "project-a"}
    session.add_all(
        [
            AgentCapabilityGrant(
                workspace_id=connection_routes.workspace_id,
                agent_id=agent.id,
                capability="vercel.project.read",
                scope_json=scope,
                effect="allow",
            ),
            AgentCapabilityGrant(
                workspace_id=connection_routes.workspace_id,
                agent_id=agent.id,
                capability="vercel.*",
                scope_json={},
                effect="deny",
            ),
        ]
    )
    await session.commit()

    response = await connection_routes.client.get(
        f"/api/v1/workspaces/{connection_routes.workspace_id}/connections/{connection.id}/access-summary"
    )

    assert response.status_code == 200, response.text
    summary = response.json()["agents"][0]
    assert summary["authorized"] is False
    assert summary["authorized_tool_names"] == []
    deny = next(grant for grant in summary["grants"] if grant["effect"] == "deny")
    assert "vercel.project.read" in deny["eligible_tool_names"]
    assert deny["eligibility_reason"] is None


async def test_access_summary_connection_only_deny_covers_tools_without_allow_scope_keys(
    connection_routes: ConnectionRouteHarness,
    session: AsyncSession,
) -> None:
    connection = Connection(
        workspace_id=connection_routes.workspace_id,
        connector_type="vercel",
        name="Connection-only deny",
        auth_type="access_token",
    )
    agent = Agent(
        workspace_id=connection_routes.workspace_id,
        name="Connection-only denied",
        slug=f"connection-denied-{new_uuid7().hex[:8]}",
    )
    session.add_all([connection, agent])
    await session.flush()
    connection_id = str(connection.id)
    session.add_all(
        [
            AgentCapabilityGrant(
                workspace_id=connection_routes.workspace_id,
                agent_id=agent.id,
                capability="vercel.deployment.read",
                scope_json={
                    "connection_id": connection_id,
                    "project_id": "project-a",
                    "deployment_id": "deployment-a",
                },
                effect="allow",
            ),
            AgentCapabilityGrant(
                workspace_id=connection_routes.workspace_id,
                agent_id=agent.id,
                capability="vercel.*",
                scope_json={"connection_id": connection_id},
                effect="deny",
            ),
        ]
    )
    await session.commit()

    response = await connection_routes.client.get(
        f"/api/v1/workspaces/{connection_routes.workspace_id}/connections/{connection.id}/access-summary"
    )

    assert response.status_code == 200, response.text
    summary = response.json()["agents"][0]
    assert summary["authorized"] is False
    deny = next(grant for grant in summary["grants"] if grant["effect"] == "deny")
    assert {"vercel.project.read", "vercel.deployment.read"}.issubset(deny["eligible_tool_names"])
    assert deny["eligibility_reason"] is None


async def test_access_summary_filters_unrelated_rows_before_enforcing_result_cap(
    connection_routes: ConnectionRouteHarness,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(service, "_MAX_CONNECTION_ACCESS_SUMMARY_ROWS", 1, raising=False)
    connection = Connection(
        workspace_id=connection_routes.workspace_id,
        connector_type="vercel",
        name="Bounded summary",
        auth_type="access_token",
    )
    agent = Agent(
        workspace_id=connection_routes.workspace_id,
        name="Bounded agent",
        slug=f"bounded-{new_uuid7().hex[:8]}",
    )
    session.add_all([connection, agent])
    await session.flush()
    connection_id = str(connection.id)
    session.add_all(
        [
            AgentCapabilityGrant(
                workspace_id=connection_routes.workspace_id,
                agent_id=agent.id,
                capability="github.repository.read",
                scope_json={"connection_id": connection_id, "repository": "acme/one"},
                effect="allow",
            ),
            AgentCapabilityGrant(
                workspace_id=connection_routes.workspace_id,
                agent_id=agent.id,
                capability="github.repository.read",
                scope_json={"connection_id": connection_id, "repository": "acme/two"},
                effect="allow",
            ),
            AgentCapabilityGrant(
                workspace_id=connection_routes.workspace_id,
                agent_id=agent.id,
                capability="vercel.project.list",
                scope_json={"connection_id": connection_id},
                effect="allow",
            ),
        ]
    )
    await session.commit()
    url = (
        f"/api/v1/workspaces/{connection_routes.workspace_id}/connections/"
        f"{connection.id}/access-summary"
    )

    at_cap = await connection_routes.client.get(url)
    assert at_cap.status_code == 200, at_cap.text
    assert [grant["capability"] for grant in at_cap.json()["agents"][0]["grants"]] == [
        "vercel.project.list"
    ]

    session.add(
        AgentCapabilityGrant(
            workspace_id=connection_routes.workspace_id,
            agent_id=agent.id,
            capability="vercel.project.read",
            scope_json={"connection_id": connection_id, "project_id": "project-a"},
            effect="allow",
        )
    )
    await session.commit()

    over_cap = await connection_routes.client.get(url)
    assert over_cap.status_code == 503, over_cap.text
    assert over_cap.json() == {"detail": "Connection access summary is temporarily unavailable"}


async def test_access_summary_does_not_count_nonmatching_glob_denies_toward_row_cap(
    connection_routes: ConnectionRouteHarness,
    session: AsyncSession,
) -> None:
    connection = Connection(
        workspace_id=connection_routes.workspace_id,
        connector_type="vercel",
        name="Glob cap exclusion",
        auth_type="access_token",
    )
    agent = Agent(
        workspace_id=connection_routes.workspace_id,
        name="Glob cap agent",
        slug=f"glob-cap-{new_uuid7().hex[:8]}",
    )
    session.add_all([connection, agent])
    await session.flush()
    connection_id = str(connection.id)
    session.add_all(
        [
            *[
                AgentCapabilityGrant(
                    workspace_id=connection_routes.workspace_id,
                    agent_id=agent.id,
                    capability="vercel.*",
                    scope_json={"connection_id": "not-this-connection-*"},
                    effect="deny",
                )
                for _ in range(257)
            ],
            AgentCapabilityGrant(
                workspace_id=connection_routes.workspace_id,
                agent_id=agent.id,
                capability="vercel.project.list",
                scope_json={"connection_id": connection_id},
                effect="allow",
            ),
        ]
    )
    await session.commit()

    response = await connection_routes.client.get(
        f"/api/v1/workspaces/{connection_routes.workspace_id}/connections/{connection.id}/access-summary"
    )

    assert response.status_code == 200, response.text
    summary = response.json()["agents"][0]
    assert summary["authorized_tool_names"] == ["vercel.project.list"]
    assert [grant["effect"] for grant in summary["grants"]] == ["allow"]


async def test_access_summary_fails_closed_at_257_exact_relevant_rows(
    connection_routes: ConnectionRouteHarness,
    session: AsyncSession,
) -> None:
    connection = Connection(
        workspace_id=connection_routes.workspace_id,
        connector_type="vercel",
        name="Exact cap",
        auth_type="access_token",
    )
    agent = Agent(
        workspace_id=connection_routes.workspace_id,
        name="Exact cap agent",
        slug=f"exact-cap-{new_uuid7().hex[:8]}",
    )
    session.add_all([connection, agent])
    await session.flush()
    session.add_all(
        [
            AgentCapabilityGrant(
                workspace_id=connection_routes.workspace_id,
                agent_id=agent.id,
                capability="vercel.project.list",
                scope_json={"connection_id": str(connection.id)},
                effect="allow",
            )
            for _ in range(257)
        ]
    )
    await session.commit()

    response = await connection_routes.client.get(
        f"/api/v1/workspaces/{connection_routes.workspace_id}/connections/{connection.id}/access-summary"
    )

    assert response.status_code == 503, response.text
    assert response.json() == {"detail": "Connection access summary is temporarily unavailable"}


async def test_access_summary_applies_bracket_connection_deny_and_lists_affected_tools(
    connection_routes: ConnectionRouteHarness,
    session: AsyncSession,
) -> None:
    connection = Connection(
        workspace_id=connection_routes.workspace_id,
        connector_type="vercel",
        name="Bracket deny",
        auth_type="access_token",
    )
    agent = Agent(
        workspace_id=connection_routes.workspace_id,
        name="Bracket deny agent",
        slug=f"bracket-deny-{new_uuid7().hex[:8]}",
    )
    session.add_all([connection, agent])
    await session.flush()
    connection_id = str(connection.id)
    matching_pattern = f"[{connection_id[0]}]{connection_id[1:]}"
    session.add_all(
        [
            AgentCapabilityGrant(
                workspace_id=connection_routes.workspace_id,
                agent_id=agent.id,
                capability="vercel.project.read",
                scope_json={"connection_id": connection_id, "project_id": "project-a"},
                effect="allow",
            ),
            AgentCapabilityGrant(
                workspace_id=connection_routes.workspace_id,
                agent_id=agent.id,
                capability="vercel.*",
                scope_json={"connection_id": matching_pattern},
                effect="deny",
            ),
        ]
    )
    await session.commit()

    response = await connection_routes.client.get(
        f"/api/v1/workspaces/{connection_routes.workspace_id}/connections/{connection.id}/access-summary"
    )

    assert response.status_code == 200, response.text
    summary = response.json()["agents"][0]
    assert summary["authorized"] is False
    deny = next(grant for grant in summary["grants"] if grant["effect"] == "deny")
    assert deny["scope"]["connection_id"] == matching_pattern
    assert "vercel.project.read" in deny["eligible_tool_names"]
    assert deny["eligibility_reason"] is None


async def test_access_summary_denies_equal_bracket_project_scope_patterns(
    connection_routes: ConnectionRouteHarness,
    session: AsyncSession,
) -> None:
    connection = Connection(
        workspace_id=connection_routes.workspace_id,
        connector_type="vercel",
        name="Bracket project overlap",
        auth_type="access_token",
    )
    agent = Agent(
        workspace_id=connection_routes.workspace_id,
        name="Bracket project agent",
        slug=f"bracket-project-{new_uuid7().hex[:8]}",
    )
    session.add_all([connection, agent])
    await session.flush()
    scope = {"connection_id": str(connection.id), "project_id": "project-[ab]"}
    session.add_all(
        [
            AgentCapabilityGrant(
                workspace_id=connection_routes.workspace_id,
                agent_id=agent.id,
                capability="vercel.project.read",
                scope_json=scope,
                effect="allow",
            ),
            AgentCapabilityGrant(
                workspace_id=connection_routes.workspace_id,
                agent_id=agent.id,
                capability="vercel.project.read",
                scope_json=scope,
                effect="deny",
            ),
        ]
    )
    await session.commit()

    response = await connection_routes.client.get(
        f"/api/v1/workspaces/{connection_routes.workspace_id}/connections/{connection.id}/access-summary"
    )

    assert response.status_code == 200, response.text
    summary = response.json()["agents"][0]
    assert summary["authorized"] is False
    assert "vercel.project.read" not in summary["authorized_tool_names"]


async def test_access_summary_keeps_disjoint_exact_project_scope_authorized(
    connection_routes: ConnectionRouteHarness,
    session: AsyncSession,
) -> None:
    connection = Connection(
        workspace_id=connection_routes.workspace_id,
        connector_type="vercel",
        name="Disjoint project scopes",
        auth_type="access_token",
    )
    agent = Agent(
        workspace_id=connection_routes.workspace_id,
        name="Disjoint project agent",
        slug=f"disjoint-project-{new_uuid7().hex[:8]}",
    )
    session.add_all([connection, agent])
    await session.flush()
    connection_id = str(connection.id)
    session.add_all(
        [
            AgentCapabilityGrant(
                workspace_id=connection_routes.workspace_id,
                agent_id=agent.id,
                capability="vercel.project.read",
                scope_json={"connection_id": connection_id, "project_id": "project-a"},
                effect="allow",
            ),
            AgentCapabilityGrant(
                workspace_id=connection_routes.workspace_id,
                agent_id=agent.id,
                capability="vercel.project.read",
                scope_json={"connection_id": connection_id, "project_id": "project-b"},
                effect="deny",
            ),
        ]
    )
    await session.commit()

    response = await connection_routes.client.get(
        f"/api/v1/workspaces/{connection_routes.workspace_id}/connections/{connection.id}/access-summary"
    )

    assert response.status_code == 200, response.text
    summary = response.json()["agents"][0]
    assert summary["authorized"] is True
    assert "vercel.project.read" in summary["authorized_tool_names"]


async def test_access_summary_fails_closed_for_list_valued_target_deny_scope(
    connection_routes: ConnectionRouteHarness,
    session: AsyncSession,
) -> None:
    connection = Connection(
        workspace_id=connection_routes.workspace_id,
        connector_type="vercel",
        name="List-valued deny",
        auth_type="access_token",
    )
    agent = Agent(
        workspace_id=connection_routes.workspace_id,
        name="List-valued deny agent",
        slug=f"list-deny-{new_uuid7().hex[:8]}",
    )
    session.add_all([connection, agent])
    await session.flush()
    connection_id = str(connection.id)
    session.add_all(
        [
            AgentCapabilityGrant(
                workspace_id=connection_routes.workspace_id,
                agent_id=agent.id,
                capability="vercel.project.read",
                scope_json={"connection_id": connection_id, "project_id": "project-a"},
                effect="allow",
            ),
            AgentCapabilityGrant(
                workspace_id=connection_routes.workspace_id,
                agent_id=agent.id,
                capability="vercel.project.read",
                scope_json={"connection_id": [connection_id], "project_id": ["project-a"]},
                effect="deny",
            ),
        ]
    )
    await session.commit()

    response = await connection_routes.client.get(
        f"/api/v1/workspaces/{connection_routes.workspace_id}/connections/{connection.id}/access-summary"
    )

    assert response.status_code == 503, response.text
    assert response.json() == {"detail": "Connection access summary is temporarily unavailable"}


async def test_access_summary_fails_closed_for_malformed_target_legacy_scope(
    connection_routes: ConnectionRouteHarness,
    session: AsyncSession,
) -> None:
    connection = Connection(
        workspace_id=connection_routes.workspace_id,
        connector_type="vercel",
        name="Malformed legacy deny",
        auth_type="access_token",
    )
    agent = Agent(
        workspace_id=connection_routes.workspace_id,
        name="Malformed legacy agent",
        slug=f"malformed-deny-{new_uuid7().hex[:8]}",
    )
    session.add_all([connection, agent])
    await session.flush()
    connection_id = str(connection.id)
    session.add_all(
        [
            AgentCapabilityGrant(
                workspace_id=connection_routes.workspace_id,
                agent_id=agent.id,
                capability="vercel.project.read",
                scope_json={"connection_id": connection_id, "project_id": "project-a"},
                effect="allow",
            ),
            AgentCapabilityGrant(
                workspace_id=connection_routes.workspace_id,
                agent_id=agent.id,
                capability="vercel.project.read",
                scope_json={"connection_id": connection_id, "project_id": 7},
                effect="deny",
            ),
        ]
    )
    await session.commit()

    response = await connection_routes.client.get(
        f"/api/v1/workspaces/{connection_routes.workspace_id}/connections/{connection.id}/access-summary"
    )

    assert response.status_code == 503, response.text
    assert response.json() == {"detail": "Connection access summary is temporarily unavailable"}


@pytest.mark.parametrize(
    ("kwargs", "fragment"),
    [
        ({"connector_type": "sharepoint"}, "Unknown connector type"),
        ({"auth_type": "oauth"}, "not supported"),
        ({"credentials": {}}, "Missing required credential fields: token"),
        ({"credentials": {"token": "x", "extra": "y"}}, "Unknown credential fields"),
        ({"config": {"bogus": 1}}, "bogus"),
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


async def test_create_stores_normalized_typed_config(
    session: AsyncSession,
    crypto: SecretCrypto,
    admin_ctx: WorkspaceContext,
    typed_settings_connector: _TypedSettingsConnector,
) -> None:
    connection, webhook_secret = await service.create_connection(
        session,
        crypto,
        admin_ctx,
        connector_type="typedsettings",
        name="Normalized settings",
        auth_type="token",
        credentials={"token": "typed-settings-token"},
        config={"statement_timeout_ms": "5000"},
        **REQ,
    )

    assert connection.config_json["statement_timeout_ms"] == 5_000
    assert connection.config_json["allow_writes"] is False
    assert webhook_secret is None


async def test_create_applies_connector_settings_validation(
    session: AsyncSession,
    crypto: SecretCrypto,
    admin_ctx: WorkspaceContext,
    typed_settings_connector: _TypedSettingsConnector,
) -> None:
    connection, _ = await service.create_connection(
        session,
        crypto,
        admin_ctx,
        connector_type="typedsettings",
        name="Validated settings",
        auth_type="token",
        credentials={"token": "typed-settings-token"},
        config={"statement_timeout_ms": 1_000},
        **REQ,
    )

    assert connection.config_json["validation_marker"] == "token:ok"


@pytest.mark.parametrize(
    ("connector_type", "auth_type", "credentials", "base_url", "marker"),
    [
        (
            "github",
            "pat",
            {"token": "safe-test-token"},
            "https://url-userinfo-marker@api.github.com",
            "url-userinfo-marker",
        ),
        (
            "github",
            "pat",
            {"token": "safe-test-token"},
            "http://unapproved-github-marker.invalid",
            "unapproved-github-marker",
        ),
        (
            "linear",
            "api_key",
            {"api_key": "safe-test-api-key"},
            "https://api.linear.app?token=query-secret-marker",
            "query-secret-marker",
        ),
        (
            "linear",
            "api_key",
            {"api_key": "safe-test-api-key"},
            "http://unapproved-linear-marker.invalid",
            "unapproved-linear-marker",
        ),
    ],
)
async def test_create_rejects_unsafe_provider_base_url_before_persistence(
    session: AsyncSession,
    crypto: SecretCrypto,
    admin_ctx: WorkspaceContext,
    connector_type: str,
    auth_type: str,
    credentials: dict[str, str],
    base_url: str,
    marker: str,
) -> None:
    before_connections = tuple(
        await session.scalars(
            select(Connection).where(Connection.workspace_id == admin_ctx.workspace_id)
        )
    )
    before_secrets = tuple(
        await session.scalars(select(Secret).where(Secret.workspace_id == admin_ctx.workspace_id))
    )

    with pytest.raises(HTTPException) as exc_info:
        await service.create_connection(
            session,
            crypto,
            admin_ctx,
            connector_type=connector_type,
            name=f"Rejected {connector_type} origin",
            auth_type=auth_type,
            credentials=credentials,
            config={"base_url": base_url},
            **REQ,
        )

    after_connections = tuple(
        await session.scalars(
            select(Connection).where(Connection.workspace_id == admin_ctx.workspace_id)
        )
    )
    after_secrets = tuple(
        await session.scalars(select(Secret).where(Secret.workspace_id == admin_ctx.workspace_id))
    )
    assert exc_info.value.status_code == 422
    assert marker not in str(exc_info.value.detail)
    assert after_connections == before_connections
    assert after_secrets == before_secrets


@pytest.mark.parametrize(
    ("connector_type", "auth_type", "credentials", "allowed_origin", "submitted"),
    [
        (
            "github",
            "pat",
            {"token": "safe-test-token"},
            "http://fake-github:8080",
            "HTTP://FAKE-GITHUB:8080/",
        ),
        (
            "linear",
            "api_key",
            {"api_key": "safe-test-api-key"},
            "http://fake-linear:8080",
            "HTTP://FAKE-LINEAR:8080/",
        ),
    ],
)
async def test_create_stores_normalized_approved_provider_base_url(
    session: AsyncSession,
    crypto: SecretCrypto,
    admin_ctx: WorkspaceContext,
    monkeypatch: pytest.MonkeyPatch,
    connector_type: str,
    auth_type: str,
    credentials: dict[str, str],
    allowed_origin: str,
    submitted: str,
) -> None:
    monkeypatch.setenv("JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS", allowed_origin)

    connection, _ = await service.create_connection(
        session,
        crypto,
        admin_ctx,
        connector_type=connector_type,
        name=f"Normalized {connector_type} origin",
        auth_type=auth_type,
        credentials=credentials,
        config={"base_url": submitted},
        **REQ,
    )

    stored = await session.scalar(select(Connection).where(Connection.id == connection.id))
    assert connection.config_json == {"base_url": allowed_origin}
    assert stored is not None
    assert stored.config_json == {"base_url": allowed_origin}


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
