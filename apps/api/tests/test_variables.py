"""Human variable CRUD exposes sensitive metadata without any readback path."""

from dataclasses import replace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from jhin_domain import WorkspaceRole


@pytest.fixture
async def client(session, admin_ctx, crypto):
    from fastapi.exceptions import RequestValidationError

    from jhin_api.deps import AdminCtx, get_db
    from jhin_api.security.csrf import csrf_protect
    from jhin_api.security.validation import safe_validation_error_handler
    from jhin_api.variables.router import router

    app = FastAPI()
    app.include_router(router)
    app.state.secret_crypto = crypto
    app.add_exception_handler(RequestValidationError, safe_validation_error_handler)
    await session.commit()

    async def context():
        await session.refresh(admin_ctx.user)
        return admin_ctx

    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[AdminCtx.__metadata__[0].dependency] = context
    app.dependency_overrides[csrf_protect] = lambda: None
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http:
        yield http


async def test_http_plaintext_and_secret_lifecycles(client, session, admin_ctx):
    base = f"/api/v1/workspaces/{admin_ctx.workspace_id}/variables"
    body = {
        "name": "blog.tone",
        "scope": "company",
        "scope_id": str(admin_ctx.workspace_id),
        "value": "Warm",
    }
    response = await client.post(base, json=body)
    assert response.status_code == 201
    row = response.json()
    assert row["value"] == "Warm" and row["version"] == 1
    saved = await client.patch(
        base + "/" + row["id"], json={"expected_version": 1, "value": "Direct"}
    )
    assert saved.status_code == 200 and saved.json()["version"] == 2
    assert (
        await client.patch(base + "/" + row["id"], json={"expected_version": 1, "value": "Old"})
    ).status_code == 409
    secret = "synthetic-private-value-abcdef"
    response = await client.post(
        base + "/secrets", json={**body, "name": "ghost.key", "sensitive": True, "value": secret}
    )
    assert response.status_code == 201
    confidential = response.json()
    assert "value" not in confidential and "masked_hint" not in confidential
    assert secret not in response.text
    secret_url = base + "/" + confidential["id"]
    assert (
        await client.patch(secret_url, json={"expected_version": 1, "value": "wrong-channel"})
    ).status_code == 403
    replaced = await client.put(
        secret_url + "/secret", json={"expected_version": 1, "value": "replacement-private-value"}
    )
    assert replaced.status_code == 200 and replaced.json()["version"] == 2
    listing = await client.get(base)
    assert listing.status_code == 200 and listing.headers["cache-control"] == "no-store"
    assert secret not in listing.text and "replacement-private-value" not in listing.text
    assert (await client.get(secret_url)).json()["configured"] is True
    assert (await client.delete(secret_url, params={"expected_version": 1})).status_code == 409
    assert (await client.delete(secret_url, params={"expected_version": 2})).status_code == 204
    assert (await client.get(secret_url)).status_code == 404


async def test_foreign_scopes_and_non_admin_are_denied(session, admin_ctx, crypto):
    from fastapi import HTTPException

    from jhin_api.variables.service import save

    await session.commit()
    with pytest.raises(HTTPException) as foreign:
        await save(
            session, crypto, admin_ctx, name="private", scope="agent", scope_id=uuid4(), value="a"
        )
    assert foreign.value.status_code == 404
    await session.refresh(admin_ctx.user)
    with pytest.raises(HTTPException) as denied:
        await save(
            session,
            crypto,
            replace(admin_ctx, role=WorkspaceRole.MEMBER),
            name="private",
            scope="company",
            scope_id=admin_ctx.workspace_id,
            value="a",
        )
    assert denied.value.status_code == 403


async def test_sensitive_request_validation_does_not_echo_input(client, admin_ctx):
    secret = "oversized-private-value" * 600
    response = await client.post(
        f"/api/v1/workspaces/{admin_ctx.workspace_id}/variables/secrets",
        json={
            "name": "key",
            "scope": "company",
            "scope_id": str(admin_ctx.workspace_id),
            "value": secret,
        },
    )
    assert response.status_code == 422 and "oversized-private-value" not in response.text


async def test_http_sensitive_copy_preserves_metadata_only_and_namespace(
    client, session, admin_ctx
):
    from jhin_db.models import Agent

    agent = Agent(workspace_id=admin_ctx.workspace_id, name="Writer", slug="copy-writer")
    session.add(agent)
    await session.commit()
    base = f"/api/v1/workspaces/{admin_ctx.workspace_id}/variables"
    created = await client.post(
        base + "/secrets",
        json={
            "name": "blog.key",
            "scope": "agent",
            "scope_id": str(agent.id),
            "value": "synthetic-secret-to-copy",
        },
    )
    assert created.status_code == 201
    original = created.json()
    destination = {
        "expected_version": 1,
        "scope": "company",
        "scope_id": str(admin_ctx.workspace_id),
    }
    copied = await client.post(base + "/" + original["id"] + "/copy", json=destination)
    assert copied.status_code == 201 and "value" not in copied.json()
    assert copied.json()["source_variable_id"] == original["id"]
    repeated = await client.post(base + "/" + original["id"] + "/copy", json=destination)
    assert repeated.json()["id"] == copied.json()["id"]
    assert (await client.get(base + "/" + original["id"])).json()["scope"] == "agent"
    assert "synthetic-secret-to-copy" not in copied.text


async def test_sensitive_write_rejects_api_keys_even_with_admin_role(session, admin_ctx, crypto):
    from types import SimpleNamespace

    from fastapi import HTTPException

    from jhin_api.variables.service import save

    await session.commit()
    with pytest.raises(HTTPException) as error:
        await save(
            session,
            crypto,
            replace(admin_ctx, api_key=SimpleNamespace()),
            secret_write=True,
            name="key",
            scope="company",
            scope_id=admin_ctx.workspace_id,
            sensitive=True,
            value="synthetic-private",
        )
    assert error.value.status_code == 403 and "synthetic-private" not in str(error.value)
