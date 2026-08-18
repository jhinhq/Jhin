"""SecretStore registers independently leakable credential fragments."""

import json
import secrets as stdlib_secrets
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from jhin_db.base import Base
from jhin_db.models import Secret
from jhin_domain import new_uuid7
from jhin_secrets import (
    MAX_SECRET_MATERIAL_BYTES,
    MAX_SECRET_MATERIAL_DEPTH,
    MAX_SECRET_MATERIAL_FRAGMENTS,
    MAX_SECRET_URL_QUERY_FIELDS,
    MasterKey,
    SecretCrypto,
    SecretMaterialError,
    SecretStore,
    decode_string_secret_map,
    get_redactor,
    register_secret_material,
)


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as db_session:
        yield db_session
    await engine.dispose()


def _credential_blob(token: str, password: str, query_secret: str, nested: str) -> str:
    encoded_password = password.replace("@", "%40")
    return json.dumps(
        {
            "access_token": token,
            "database_url": (
                "postgresql://service_user:"
                f"{encoded_password}@db.example.test/app?sslmode=require&access_token={query_secret}"
            ),
            "nested_json": json.dumps({"inner_token": nested}),
        }
    )


def test_decode_secret_mapping_is_strict_and_does_not_echo_material() -> None:
    assert decode_string_secret_map('{"token": "secret-value"}') == {"token": "secret-value"}
    for malformed in (
        "not-json-secret-value",
        '["secret-value"]',
        '{"token": 42}',
        '{"token": {"nested": "secret-value"}}',
    ):
        with pytest.raises(SecretMaterialError) as excinfo:
            decode_string_secret_map(malformed)
        assert "secret-value" not in str(excinfo.value)


def test_duplicate_json_keys_fail_closed_without_registering_hidden_value() -> None:
    material = '{"token":"first-secret-value","token":"second-secret-value"}'
    redactor = get_redactor()
    redactor.clear()

    with pytest.raises(SecretMaterialError, match="duplicate key"):
        decode_string_secret_map(material)
    with pytest.raises(SecretMaterialError, match="duplicate key"):
        register_secret_material(material)

    assert redactor.redact_text("first-secret-value second-secret-value") == (
        "first-secret-value second-secret-value"
    )
    redactor.clear()


def test_url_registration_decodes_every_nonempty_repeated_query_value() -> None:
    redactor = get_redactor()
    redactor.clear()
    material = (
        "postgresql://encoded%5Fuser:p%40ss%2Fword@db.example.test/app"
        "?mode=generic-secret-one&mode=generic-secret-two&empty="
    )
    try:
        register_secret_material(material)
        rendered = redactor.redact_text(
            "encoded_user p@ss/word generic-secret-one generic-secret-two"
        )
        assert rendered == "[REDACTED] [REDACTED] [REDACTED] [REDACTED]"
    finally:
        redactor.clear()


def test_authorityless_dsn_registers_every_query_secret() -> None:
    redactor = get_redactor()
    redactor.clear()
    material = (
        "postgresql:///app?password=authorityless-password"
        "&token=authorityless-token&token=authorityless-token-two"
    )
    try:
        register_secret_material(material)
        assert (
            redactor.redact_text(
                "authorityless-password authorityless-token authorityless-token-two"
            )
            == "[REDACTED] [REDACTED] [REDACTED]"
        )
    finally:
        redactor.clear()


def test_secret_material_exact_size_and_query_bounds_are_accepted() -> None:
    redactor = get_redactor()
    redactor.clear()
    exact_bytes = "s" * MAX_SECRET_MATERIAL_BYTES
    exact_query = "postgresql:///app?" + "&".join(
        f"field_{index}=query-secret-{index}" for index in range(MAX_SECRET_URL_QUERY_FIELDS)
    )
    try:
        register_secret_material(exact_bytes)
        register_secret_material(exact_query)
        assert redactor.redact_text("query-secret-0") == "[REDACTED]"
    finally:
        redactor.clear()


def test_secret_material_exact_fragment_and_depth_bounds_are_accepted() -> None:
    redactor = get_redactor()
    redactor.clear()
    # The full JSON blob is one fragment, leaving exactly N - 1 unique leaves.
    exact_fragments = json.dumps(
        {
            f"key_{index}": f"bounded-secret-{index}"
            for index in range(MAX_SECRET_MATERIAL_FRAGMENTS - 1)
        }
    )
    exact_depth: object = "exact-depth-secret"
    for _ in range(MAX_SECRET_MATERIAL_DEPTH - 1):
        exact_depth = [exact_depth]
    try:
        register_secret_material(exact_fragments)
        register_secret_material(json.dumps(exact_depth))
        assert redactor.redact_text("bounded-secret-254 exact-depth-secret") == (
            "[REDACTED] [REDACTED]"
        )
    finally:
        redactor.clear()


@pytest.mark.parametrize(
    ("material", "error_fragment"),
    [
        ("x" * (MAX_SECRET_MATERIAL_BYTES + 1), "size limit"),
        (
            json.dumps(
                {
                    f"key_{index}": f"bounded-secret-{index}"
                    for index in range(MAX_SECRET_MATERIAL_FRAGMENTS + 1)
                }
            ),
            "fragment limit",
        ),
        (
            "https://user:password-value@example.test/?"
            + "&".join(
                f"field_{index}=query-secret-{index}"
                for index in range(MAX_SECRET_URL_QUERY_FIELDS + 1)
            ),
            "query-field limit",
        ),
    ],
)
def test_secret_material_boundaries_fail_closed(material: str, error_fragment: str) -> None:
    get_redactor().clear()
    with pytest.raises(SecretMaterialError, match=error_fragment):
        register_secret_material(material)
    get_redactor().clear()


def test_secret_material_traversal_depth_is_bounded() -> None:
    nested: object = "deep-secret-value"
    for _ in range(MAX_SECRET_MATERIAL_DEPTH + 2):
        nested = [nested]

    with pytest.raises(SecretMaterialError, match="depth limit"):
        register_secret_material(json.dumps(nested))
    get_redactor().clear()


async def test_store_does_not_persist_material_that_exceeds_bounds(
    session: AsyncSession,
) -> None:
    crypto = SecretCrypto(MasterKey(key=stdlib_secrets.token_bytes(32)))
    store = SecretStore(session, crypto)

    with pytest.raises(SecretMaterialError, match="size limit"):
        await store.create(
            workspace_id=new_uuid7(),
            name="Oversized",
            plaintext="x" * (MAX_SECRET_MATERIAL_BYTES + 1),
        )

    assert (await session.scalars(select(Secret))).all() == []


async def test_failed_over_bound_rotation_leaves_original_secret_unchanged(
    session: AsyncSession,
) -> None:
    crypto = SecretCrypto(MasterKey(key=stdlib_secrets.token_bytes(32)))
    store = SecretStore(session, crypto)
    workspace_id = new_uuid7()
    original = "original-credential-value"
    secret = await store.create(
        workspace_id=workspace_id,
        name="Rotation boundary",
        plaintext=original,
    )
    before = (
        secret.ciphertext,
        secret.nonce,
        secret.wrapped_data_key,
        secret.secret_fingerprint,
        secret.masked_hint,
    )

    with pytest.raises(SecretMaterialError, match="size limit"):
        await store.rotate(
            workspace_id,
            secret.id,
            "x" * (MAX_SECRET_MATERIAL_BYTES + 1),
        )
    await session.commit()
    await session.refresh(secret)

    assert (
        secret.ciphertext,
        secret.nonce,
        secret.wrapped_data_key,
        secret.secret_fingerprint,
        secret.masked_hint,
    ) == before
    assert await store.reveal(workspace_id, secret.id) == original


def _assert_fragments_registered(
    *, token: str, password: str, query_secret: str, nested: str
) -> None:
    redacted = get_redactor().redact_text(
        f"token={token} user=service_user password={password} query={query_secret} nested={nested}"
    )
    for material in (token, "service_user", password, query_secret, nested):
        assert material not in redacted
    assert redacted.count("[REDACTED]") == 5


async def test_create_reveal_and_rotate_register_nested_and_url_secret_material(
    session: AsyncSession,
) -> None:
    redactor = get_redactor()
    redactor.clear()
    crypto = SecretCrypto(MasterKey(key=stdlib_secrets.token_bytes(32)))
    store = SecretStore(session, crypto)
    workspace_id = new_uuid7()
    initial = {
        "token": "initial-token-value",
        "password": "embedded@password",
        "query_secret": "initial-query-secret",
        "nested": "initial-nested-token",
    }
    rotated = {
        "token": "rotated-token-value",
        "password": "rotated@password",
        "query_secret": "rotated-query-secret",
        "nested": "rotated-nested-token",
    }
    initial_blob = _credential_blob(**initial)
    rotated_blob = _credential_blob(**rotated)

    try:
        secret = await store.create(
            workspace_id=workspace_id,
            name="Supabase credentials",
            plaintext=initial_blob,
        )
        _assert_fragments_registered(**initial)
        assert initial_blob.encode() not in secret.ciphertext

        redactor.clear()
        assert await store.reveal(workspace_id, secret.id) == initial_blob
        _assert_fragments_registered(**initial)

        redactor.clear()
        await store.rotate(workspace_id, secret.id, rotated_blob)
        _assert_fragments_registered(**rotated)
        assert rotated_blob.encode() not in secret.ciphertext
    finally:
        redactor.clear()
