"""Scoped settings never turn an opaque secret reference into model-visible bytes."""

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from jhin_db.base import Base
from jhin_db.models import Agent, AgentTeamMembership, Connection, Secret, Team, Workspace
from jhin_secrets import MasterKey, SecretCrypto


@pytest.fixture
async def state():
    from jhin_db.models.variables import ScopedVariable  # noqa: F401
    from jhin_secrets.variables import VariableActor, VariableStore

    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        w, team, agent, other, user = (uuid4() for _ in range(5))
        db.add(Workspace(id=w, name="Company", slug=str(w)))
        db.add(Team(id=team, workspace_id=w, name="Marketing"))
        db.add_all(
            [
                Agent(id=agent, workspace_id=w, name="Writer", slug="writer", team_id=team),
                Agent(id=other, workspace_id=w, name="Peer", slug="peer", team_id=team),
                AgentTeamMembership(workspace_id=w, agent_id=agent, team_id=team, is_primary=True),
                AgentTeamMembership(workspace_id=w, agent_id=other, team_id=team, is_primary=True),
            ]
        )
        await db.flush()
        crypto = SecretCrypto(MasterKey(key=b"q" * 32))
        admin = VariableActor(w, "user", user, is_admin=True)
        yield db, VariableStore(db, crypto), admin, (w, team, agent, other, user)
    await engine.dispose()


async def test_plaintext_crud_has_revision_conflicts_and_namespace_uniqueness(state):
    from jhin_secrets.variables import VariableError

    _db, store, admin, (_w, _team, agent, other, _user) = state
    row = await store.set(admin, name="blog.tone", scope="agent", scope_id=agent, value="Warm")
    assert store.public(row)["value"] == "Warm"
    with pytest.raises(VariableError, match="version"):
        await store.set(admin, variable_id=row.id, expected_version=0, value="Stale")
    row = await store.set(admin, variable_id=row.id, expected_version=1, value="Concise")
    assert row.version == 2 and row.plaintext == "Concise"
    with pytest.raises(VariableError, match="exists"):
        await store.set(admin, name="blog.tone", scope="agent", scope_id=agent, value="Duplicate")
    another = await store.set(admin, name="blog.tone", scope="agent", scope_id=other, value="Other")
    await store.delete(admin, row.id, expected_version=2)
    assert await store.get(admin, another.id) is another
    with pytest.raises(VariableError):
        await store.get(admin, row.id)


async def test_sensitive_metadata_has_no_value_hint_or_secret_id(state):
    db, store, admin, (_w, _team, agent, _other, _user) = state
    value = "private-value-never-return-this"
    row = await store.set(
        admin, name="ghost.key", scope="agent", scope_id=agent, sensitive=True, value=value
    )
    public = store.public(row)
    assert not ({"value", "plaintext", "secret_id", "masked_hint", "ciphertext"} & public.keys())
    assert public["configured"] is True and public["sensitive"] is True
    secret = await db.get(Secret, row.secret_id)
    assert row.plaintext is None and value.encode() not in secret.ciphertext
    assert secret.masked_hint == ""
    row = await store.set(admin, variable_id=row.id, expected_version=1, value="replacement-secret")
    assert row.version == 2 and "replacement-secret" not in json.dumps(
        store.public(row), default=str
    )


async def test_live_scope_access_and_shared_writes_require_authority(state):
    from jhin_secrets.variables import VariableActor, VariableError

    db, store, admin, (w, team, agent, other, _user) = state
    own = await store.set(admin, name="private", scope="agent", scope_id=agent, value="a")
    shared = await store.set(admin, name="team", scope="team", scope_id=team, value="b")
    company = await store.set(admin, name="company", scope="company", scope_id=w, value="c")
    writer = VariableActor(w, "agent", agent)
    peer = VariableActor(w, "agent", other)
    assert [r.id for r in await store.list(writer)] == [own.id, shared.id, company.id]
    with pytest.raises(VariableError):
        await store.get(peer, own.id)
    with pytest.raises(VariableError, match="authority"):
        await store.set(peer, variable_id=shared.id, expected_version=1, value="bad")
    membership = await db.scalar(
        select(AgentTeamMembership).where(AgentTeamMembership.agent_id == other)
    )
    membership.left_at = datetime.now(UTC)
    await db.flush()
    with pytest.raises(VariableError):
        await store.get(peer, shared.id)
    outsider = VariableActor(uuid4(), "agent", agent)
    with pytest.raises(VariableError):
        await store.get(outsider, company.id)


async def test_secret_capture_is_idempotent_private_and_binding_checks_origin(state):
    from jhin_db.models import Conversation
    from jhin_db.models.variables import SecureInputCapture
    from jhin_secrets.intake import capture_input
    from jhin_secrets.variables import VariableActor, VariableError

    db, store, admin, (w, _team, agent, other, user) = state
    chat = Conversation(
        workspace_id=w,
        primary_agent_id=agent,
        created_by_user_id=user,
        title="Safe",
        last_activity_at=datetime.now(UTC),
    )
    db.add(chat)
    await db.flush()
    secret = "1234567890abcdef12345678:" + "a1" * 32
    original = "Here is my Ghost API key: " + secret + " please set up my blog"
    first = await capture_input(
        db,
        store.crypto,
        workspace_id=w,
        conversation_id=chat.id,
        agent_id=agent,
        user_id=user,
        text=original,
    )
    second = await capture_input(
        db,
        store.crypto,
        workspace_id=w,
        conversation_id=chat.id,
        agent_id=agent,
        user_id=user,
        text=original,
    )
    assert first.text == second.text and secret not in first.text
    assert len(first.references) == 1 and first.requires_ghost_url
    ref = first.references[0]["secret_ref"]
    assert len((await db.scalars(select(SecureInputCapture))).all()) == 1
    with pytest.raises(VariableError):
        await store.set(
            VariableActor(w, "agent", other),
            name="stolen",
            scope="agent",
            scope_id=other,
            secret_ref=ref,
            sensitive=True,
        )
    writer = VariableActor(w, "agent", agent, conversation_id=chat.id)
    row = await store.set(
        writer, name="ghost.key", scope="agent", scope_id=agent, sensitive=True, secret_ref=ref
    )
    again = await store.set(
        writer, name="ghost.key", scope="agent", scope_id=agent, sensitive=True, secret_ref=ref
    )
    assert row.id == again.id and row.version == 1
    connection = Connection(
        workspace_id=w,
        name="Ghost",
        connector_type="ghost",
        auth_type="api_key",
        config_json={"admin_url": "https://blog.example.test"},
    )
    db.add(connection)
    await db.flush()
    await store.bind(
        writer,
        row.id,
        connection.id,
        credential_field="admin_key",
        approved_origin="https://blog.example.test",
    )
    assert (
        await store.resolve_bound(
            writer,
            row.id,
            connection.id,
            credential_field="admin_key",
            approved_origin="https://blog.example.test",
        )
        == secret
    )
    connection.config_json = {"admin_url": "https://elsewhere.example.test"}
    await db.flush()
    with pytest.raises(VariableError, match="origin"):
        await store.resolve_bound(
            writer,
            row.id,
            connection.id,
            credential_field="admin_key",
            approved_origin="https://elsewhere.example.test",
        )
    from jhin_db.models.variables import VariableConnectionBinding

    variable_id = row.id
    await store.delete(admin, variable_id, expected_version=1)
    assert connection.status == "disabled"
    assert not (await db.scalars(select(VariableConnectionBinding))).all()
    assert not (await db.scalars(select(SecureInputCapture))).all()
    assert not (await db.scalars(select(Secret))).all()
    with pytest.raises(VariableError, match="not found"):
        await store.resolve_bound(
            writer,
            variable_id,
            connection.id,
            credential_field="admin_key",
            approved_origin="https://blog.example.test",
        )


@pytest.mark.parametrize(
    "text,value",
    [
        ("API key: abc-secret-long-value", "abc-secret-long-value"),
        ("password is short!", "short!"),
        ('client_secret="abc-private-secret"', "abc-private-secret"),
        ("Authorization: Bearer abc.def-long-private", "abc.def-long-private"),
        ("Here is the key\n" + "ab" * 12 + ":" + "12" * 32, "ab" * 12 + ":" + "12" * 32),
    ],
)
def test_intake_spans_capture_recognizable_and_declared_secrets(text, value):
    from jhin_secrets.intake import secret_spans

    spans = secret_spans(text)
    assert any(text[start:end] == value for start, end, _ in spans)


def test_intake_does_not_misclassify_normal_prose_or_requirements():
    from jhin_secrets.intake import secret_spans

    assert not secret_spans("Please find my API key in settings and use it to connect.")
    assert not secret_spans("Use https://blog.example.test. The password is missing.")


@pytest.mark.parametrize(
    "text,expected",
    [
        (
            "Store the key, then connect Ghost at http://jhin-ghost-acceptance:2368 "
            "using that new variable.",
            ["http://jhin-ghost-acceptance:2368"],
        ),
        (
            "Ghost Admin URL: https://blog.example.test/publication/ghost/",
            ["https://blog.example.test/publication/ghost/"],
        ),
        ("My Ghost Admin origin is https://blog.example.test", ["https://blog.example.test"]),
        ("Connect Ghost at `http://cms:2368`", ["http://cms:2368"]),
        (
            "Confirm the original is intact by reconnecting its existing Ghost connection "
            "at http://jhin-ghost-acceptance:2368. This private connection is Drafts only.",
            ["http://jhin-ghost-acceptance:2368"],
        ),
        ("Reconnect to Ghost at https://cms.example/blog", ["https://cms.example/blog"]),
        (
            "Verify the existing Ghost connection at `https://cms.example/blog`.",
            ["https://cms.example/blog"],
        ),
        (
            'Please verify my Ghost Admin connection at "https://cms.example/blog".',
            ["https://cms.example/blog"],
        ),
        (
            "Check it by verifying its existing Ghost connection at http://cms:2368.",
            ["http://cms:2368"],
        ),
        ("Do not reconnect its existing Ghost connection at https://cms.example/blog.", []),
        ("Never verify the existing Ghost connection at https://cms.example/blog.", []),
        ('"Reconnect its existing Ghost connection at https://cms.example/blog"', []),
        ("`Verify the existing Ghost connection at https://cms.example/blog`", []),
        ("Maybe reconnect its existing Ghost connection at https://cms.example/blog.", []),
        (
            "Verify the existing Ghost connection at https://cms.example/blog "
            "or https://other.example/blog.",
            [],
        ),
        ("Verify my marketing website at https://cms.example/blog.", []),
        ('Ghost Admin URL: "https://cms.example/blog"', ["https://cms.example/blog"]),
        ('"Connect Ghost at https://cms.example/blog"', []),
        ('```Ghost Admin URL: "https://cms.example/blog"```', []),
        ("My marketing website is https://blog.example.test", []),
        ("Do not connect Ghost at https://blog.example.test", []),
        ('Example: "connect Ghost at https://blog.example.test"', []),
        ("> Ghost Admin URL: https://blog.example.test", []),
        ("```Ghost Admin URL: https://blog.example.test```", []),
        ("Maybe connect Ghost at https://blog.example.test", []),
        ("Connect Ghost at https://blog.example.test or https://other.example.test", []),
        (
            "Ghost Admin URL: https://first.example.test; do not connect Ghost at https://first.example.test/ghost",
            [],
        ),
        (
            "Don't connect Ghost at https://wrong.example.test; connect Ghost at https://right.example.test",
            ["https://right.example.test"],
        ),
    ],
)
def test_ghost_url_declarations_use_explicit_positive_unquoted_clauses(text, expected):
    from jhin_secrets.intake import supplied_ghost_admin_urls

    assert supplied_ghost_admin_urls([text]) == expected


@pytest.mark.parametrize("label", ["admin key", "API key", "private key"])
@pytest.mark.parametrize(
    "state",
    [
        "untouched",
        "unchanged",
        "intact",
        "configured",
        "saved",
        "encrypted",
        "valid",
        "working",
        "active",
        "inactive",
        "verified",
    ],
)
def test_key_status_prose_is_not_secret_but_explicit_values_still_are(label, state):
    from jhin_secrets.intake import redact_legacy_text, secret_spans

    sentence = f"Deleted the test variable; your original {label} is {state}."
    assert not secret_spans(sentence) and redact_legacy_text(sentence) == sentence
    for supplied in (
        f'{label} is "{state}"',
        f"{label} is '{state}'",
        f"{label}: {state}",
        f"{label} = {state}",
        f"password is {state}",
        f'password is "{state}"',
    ):
        assert secret_spans(supplied)
        assert "REDACTED" in redact_legacy_text(supplied)


@pytest.mark.parametrize("prefix", ["still", "currently"])
@pytest.mark.parametrize(
    "state", ["valid", "working", "active", "inactive", "verified", "unchanged"]
)
def test_key_status_adverb_requires_a_following_recognized_unquoted_state(prefix, state):
    from jhin_secrets.intake import redact_legacy_text, secret_spans

    sentence = f"The stored admin key is {prefix} {state} and working."
    assert not secret_spans(sentence)
    assert redact_legacy_text(sentence) == sentence
    for supplied in (
        f'admin key is "{prefix} {state}"',
        f"admin key: {prefix} {state}",
        f"admin key = {prefix} {state}",
        f"password is {prefix} {state}",
    ):
        assert secret_spans(supplied)


@pytest.mark.parametrize(
    "value", ["still", "currently", "still puzzling", 'still "valid"', "still valid-token123"]
)
def test_unrecognized_key_state_does_not_gain_status_exemption(value):
    from jhin_secrets.intake import secret_spans

    assert secret_spans("The admin key is " + value)


def test_status_adverb_does_not_hide_high_confidence_credentials():
    from jhin_secrets.intake import redact_legacy_text, secret_spans

    key = "ab" * 12 + ":" + "cd" * 32
    text = "The stored admin key is still " + key
    assert any(text[start:end] == key for start, end, _kind in secret_spans(text))
    assert key not in redact_legacy_text(text)


@pytest.mark.parametrize(
    "value",
    [
        "c42f10eb07523a9946bbfd3c3e34967dbd96bce8a20135ff8c7c5cf5471b20b2",
        "-----BEGIN PRIVATE KEY-----\nsynthetic-fixture-content\n-----END PRIVATE KEY-----",
    ],
)
def test_private_key_status_exemption_never_excludes_supplied_key_material(value):
    from jhin_secrets.intake import redact_legacy_text, secret_spans

    text = "Your original private key is unchanged. New private key is " + value
    spans = secret_spans(text)
    assert any(text[start:end] == value for start, end, _kind in spans)
    assert value not in redact_legacy_text(text)


async def test_copy_secret_shares_only_with_authority_and_keeps_original(state):
    from jhin_secrets.variables import VariableActor, VariableError

    db, store, admin, (w, team, agent, other, _user) = state
    original = await store.set(
        admin,
        name="ghost.key",
        scope="agent",
        scope_id=agent,
        sensitive=True,
        value="synthetic-sharing-secret",
    )
    writer = VariableActor(w, "agent", agent)
    with pytest.raises(VariableError, match="authority"):
        await store.copy(writer, original.id, expected_version=1, scope="team", scope_id=team)
    allowed = VariableActor(w, "agent", agent, write_scopes=frozenset({("team", team)}))
    copied = await store.copy(allowed, original.id, expected_version=1, scope="team", scope_id=team)
    repeated = await store.copy(
        allowed, original.id, expected_version=1, scope="team", scope_id=team
    )
    assert repeated.id == copied.id and copied.version == 1
    assert copied.source_variable_id == original.id and copied.source_version == 1
    assert original.secret_id != copied.secret_id and original.scope == "agent"
    assert "value" not in store.public(copied)
    assert await store.get(VariableActor(w, "agent", other), copied.id) is copied
    with pytest.raises(VariableError, match="not found"):
        await store.get(VariableActor(w, "agent", other), original.id)
    await store.set(admin, variable_id=copied.id, expected_version=1, value="rotated-shared-key")
    with pytest.raises(VariableError, match="exists"):
        await store.copy(allowed, original.id, expected_version=1, scope="team", scope_id=team)
    await store.delete(admin, original.id, expected_version=1)
    assert await db.get(Secret, copied.secret_id) is not None


async def test_secure_refs_cannot_be_taken_by_another_chat_or_user_and_expire(state):
    from datetime import timedelta

    from jhin_db.models import Conversation
    from jhin_db.models.variables import SecureInputCapture
    from jhin_secrets.intake import capture_input
    from jhin_secrets.variables import VariableActor, VariableError

    db, store, _admin, (w, _team, agent, _other, user) = state
    chat = Conversation(
        workspace_id=w,
        primary_agent_id=agent,
        created_by_user_id=user,
        title="Inputs",
        last_activity_at=datetime.now(UTC),
    )
    db.add(chat)
    await db.flush()
    captured = await capture_input(
        db,
        store.crypto,
        workspace_id=w,
        conversation_id=chat.id,
        agent_id=agent,
        user_id=user,
        text="password is synthetic-password",
    )
    reference = captured.references[0]["secret_ref"]
    args = {
        "name": "password",
        "scope": "agent",
        "scope_id": agent,
        "sensitive": True,
        "secret_ref": reference,
    }
    for thief in (
        VariableActor(w, "agent", agent, conversation_id=uuid4()),
        VariableActor(w, "user", uuid4(), is_admin=True),
    ):
        with pytest.raises(VariableError, match="not found"):
            await store.set(thief, **args)
    record = await db.scalar(select(SecureInputCapture))
    record.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db.flush()
    with pytest.raises(VariableError, match="expired"):
        await store.set(VariableActor(w, "agent", agent, conversation_id=chat.id), **args)


async def test_listing_filters_audience_before_paginating_and_agent_revocation(state):
    from jhin_secrets.variables import VariableActor, VariableError

    db, store, admin, (w, _team, agent, other, _user) = state
    await store.set(admin, name="private.other", scope="agent", scope_id=other, value="other")
    own = await store.set(admin, name="private.own", scope="agent", scope_id=agent, value="own")
    actor = VariableActor(w, "agent", agent)
    assert [r.id for r in await store.list(actor, limit=1)] == [own.id]
    assert await store.count(actor) == 1
    (await db.get(Agent, agent)).status = "disabled"
    await db.flush()
    with pytest.raises(VariableError):
        await store.get(actor, own.id)


@pytest.mark.parametrize("value", ["nul\x00value", "bad\ud800unicode"])
async def test_invalid_sensitive_bytes_fail_before_mutation(state, value):
    from jhin_secrets.variables import VariableError

    db, store, admin, (_w, _team, agent, _other, _user) = state
    with pytest.raises(VariableError, match="format"):
        await store.set(
            admin, name="invalid", scope="agent", scope_id=agent, sensitive=True, value=value
        )
    assert not (await db.scalars(select(Secret))).all()


async def test_internal_consumption_refreshes_rotated_secret_and_connection_origin(state):
    from sqlalchemy import update

    from jhin_secrets.variables import VariableActor, VariableError

    db, store, admin, (w, _team, agent, _other, _user) = state
    variable = await store.set(
        admin,
        name="key",
        scope="agent",
        scope_id=agent,
        sensitive=True,
        value="synthetic-before-rotation",
    )
    connection = Connection(
        workspace_id=w,
        name="Ghost",
        connector_type="ghost",
        auth_type="api_key",
        config_json={"admin_url": "https://blog.example.test"},
    )
    db.add(connection)
    await db.flush()
    caller = VariableActor(w, "agent", agent)
    arguments = {"credential_field": "admin_key", "approved_origin": "https://blog.example.test"}
    await store.bind(caller, variable.id, connection.id, **arguments)
    assert (
        await store.resolve_bound(caller, variable.id, connection.id, **arguments)
        == "synthetic-before-rotation"
    )
    cached_secret = await db.get(Secret, variable.secret_id)
    encrypted = store.crypto.encrypt("synthetic-after-rotation")
    await db.execute(
        update(Secret)
        .where(Secret.id == variable.secret_id)
        .values(
            ciphertext=encrypted.ciphertext,
            nonce=encrypted.nonce,
            wrapped_data_key=encrypted.wrapped_data_key,
            secret_fingerprint=encrypted.fingerprint,
        )
        .execution_options(synchronize_session=False)
    )
    assert cached_secret.ciphertext != encrypted.ciphertext
    assert (
        await store.resolve_bound(caller, variable.id, connection.id, **arguments)
        == "synthetic-after-rotation"
    )
    await db.execute(
        update(Connection)
        .where(Connection.id == connection.id)
        .values(
            config_json={"admin_url": "https://changed.example.test"},
        )
        .execution_options(synchronize_session=False)
    )
    with pytest.raises(VariableError, match="origin"):
        await store.resolve_bound(caller, variable.id, connection.id, **arguments)


async def test_binding_pins_exact_install_path_and_accepts_equivalent_admin_suffix(state):
    from jhin_secrets.variables import VariableActor, VariableError

    db, store, admin, (w, _team, agent, _other, _user) = state
    variable = await store.set(
        admin, name="key", scope="agent", scope_id=agent, sensitive=True, value="synthetic-key"
    )
    connection = Connection(
        workspace_id=w,
        name="Ghost",
        connector_type="ghost",
        auth_type="api_key",
        config_json={"admin_url": "https://blog.example.test:443/publication/ghost/api/admin/"},
    )
    db.add(connection)
    await db.flush()
    caller = VariableActor(w, "agent", agent)
    arguments = {"credential_field": "admin_key", "approved_origin": "https://blog.example.test"}
    from jhin_db.models.variables import VariableConnectionBinding

    await store.bind(caller, variable.id, connection.id, **arguments)
    binding = await db.scalar(select(VariableConnectionBinding))
    assert binding.approved_admin_url == "https://blog.example.test/publication"
    connection.config_json = {"admin_url": "https://blog.example.test/publication/ghost"}
    await db.flush()
    assert (
        await store.resolve_bound(caller, variable.id, connection.id, **arguments)
        == "synthetic-key"
    )
    for target in (
        "https://blog.example.test/another",
        "https://blog.example.test/publication/%2e",
    ):
        connection.config_json = {"admin_url": target}
        await db.flush()
        with pytest.raises(VariableError):
            await store.resolve_bound(caller, variable.id, connection.id, **arguments)


def test_legacy_projection_is_bounded_idempotent_and_preserves_opaque_references():
    from jhin_secrets.intake import redact_legacy_payload, redact_legacy_text

    key = "ab" * 12 + ":" + "cd" * 32
    text = "x" * 65500 + " API key: " + key
    reference = "[secure_input:12345678-1234-1234-1234-123456789012]"
    payload = {"text": text, "nested": [{"ref": reference}]}
    projected = redact_legacy_payload(payload)
    assert key not in projected["text"] and key in payload["text"]
    assert projected["nested"][0]["ref"] == reference
    assert redact_legacy_payload(projected) == projected
    assert "ab" * 10 not in redact_legacy_text("x" * 199980 + key)
