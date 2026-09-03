"""Local model management on an Ollama provider: the list merge, loading and
unloading with their audit rows, the response-budget hand-off, and the
refusal every other provider type gets.

The real adapter runs against an httpx MockTransport, wired through the same
factory call site the API uses, so what is tested is the service *and* the
wire shape the web lane reads.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

import httpx
import pytest
from fastapi import HTTPException
from opentelemetry.trace import Tracer
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_api.deps import WorkspaceContext
from jhin_api.models import ollama_service, service
from jhin_api.models.schemas import (
    OllamaLoadedModelOut,
    OllamaLoadIn,
    OllamaModelDetailsOut,
    OllamaModelOut,
)
from jhin_db.models import AuditEvent, ModelProvider
from jhin_domain import new_uuid7
from jhin_models import ModelClient, build_model_client
from jhin_observability import JhinMetrics, noop_metrics, noop_tracer
from jhin_secrets import SecretCrypto
from jhin_secrets.material import register_secret_material

BASE_URL = "http://192.168.1.79:11434/v1"

TAGS: dict[str, Any] = {
    "models": [
        {
            "name": "muse-glimmer:latest",
            "model": "muse-glimmer:latest",
            "modified_at": "2026-08-30T12:34:56.123456789-07:00",
            "size": 18200000000,
            "digest": "abc",
            "details": {
                "family": "llama",
                "families": ["llama"],
                "parameter_size": "27.9B",
                "quantization_level": "Q4_K_M",
                "format": "gguf",
            },
        },
        {
            "name": "qwen3.8:latest",
            "model": "qwen3.8:latest",
            "modified_at": "2026-08-30T12:00:00Z",
            "size": 17700000000,
            "digest": "def",
            "details": {
                "family": "qwen3",
                "parameter_size": "27.3B",
                "quantization_level": "Q4_K_M",
            },
        },
    ]
}

PS: dict[str, Any] = {
    "models": [
        {
            "name": "muse-glimmer:latest",
            "model": "muse-glimmer:latest",
            "size": 18200000000,
            "size_vram": 18200000000,
            "expires_at": "2026-09-02T10:05:00Z",
            "context_length": 8192,
            "details": {"family": "llama"},
        }
    ]
}

SHOWS: dict[str, dict[str, Any]] = {
    "muse-glimmer:latest": {
        "license": "LLAMA COMMUNITY LICENSE\n...",
        "details": {"family": "llama", "parameter_size": "27.9B", "quantization_level": "Q4_K_M"},
        "model_info": {"general.architecture": "llama", "llama.context_length": 131072},
        "capabilities": ["completion", "tools"],
    },
    "qwen3.8:latest": {
        "license": "Apache License 2.0\n...",
        "details": {"family": "qwen3", "parameter_size": "27.3B", "quantization_level": "Q4_K_M"},
        "model_info": {"general.architecture": "qwen3", "qwen3.context_length": 40960},
        "capabilities": ["completion", "tools", "thinking"],
    },
}

EXPECTED_MODELS = [
    {
        "name": "muse-glimmer:latest",
        "size_bytes": 18200000000,
        "family": "llama",
        "parameter_size": "27.9B",
        "quantization": "Q4_K_M",
        "modified_at": "2026-08-30T19:34:56.123456Z",
        "context_length": 131072,
        "capabilities": ["completion", "tools"],
        "loaded": True,
        "size_vram_bytes": 18200000000,
        "expires_at": "2026-09-02T10:05:00Z",
        "keeps_loaded": False,
    },
    {
        "name": "qwen3.8:latest",
        "size_bytes": 17700000000,
        "family": "qwen3",
        "parameter_size": "27.3B",
        "quantization": "Q4_K_M",
        "modified_at": "2026-08-30T12:00:00Z",
        "context_length": 40960,
        "capabilities": ["completion", "tools", "thinking"],
        "loaded": False,
        "size_vram_bytes": None,
        "expires_at": None,
        "keeps_loaded": False,
    },
]

NOT_OLLAMA = (
    "This provider is not an Ollama endpoint; local model management only applies to "
    "providers of type ollama."
)


def _handler(
    seen: list[httpx.Request],
    *,
    ps: Any = PS,
    failing: dict[str, int] | None = None,
    delay: dict[str, float] | None = None,
) -> Any:
    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if delay and path in delay:
            await asyncio.sleep(delay[path])
        if failing and path in failing:
            return httpx.Response(failing[path], json={"error": "boom"})
        if path == "/api/tags":
            return httpx.Response(200, json=TAGS)
        if path == "/api/ps":
            return httpx.Response(200, json=ps)
        if path == "/api/show":
            model = json.loads(request.content)["model"]
            return httpx.Response(200, json=SHOWS[model])
        if path == "/api/generate":
            body = json.loads(request.content)
            if body["model"] == "nope:latest":
                return httpx.Response(
                    404, json={"error": "model 'nope:latest' not found, try pulling it first"}
                )
            if body["model"] == "secret:latest":
                return httpx.Response(
                    502, json={"error": "proxy refused token hunter2-proxy-token"}
                )
            return httpx.Response(
                200,
                json={
                    "model": body["model"],
                    "created_at": "2026-09-02T10:00:00Z",
                    "response": "",
                    "done": True,
                    "done_reason": "unload" if body["keep_alive"] == "0" else "load",
                },
            )
        return httpx.Response(404, json={"error": f"unexpected path {path}"})

    return handler


def _install(monkeypatch: pytest.MonkeyPatch, handler: Any) -> list[ModelClient]:
    """Route the service's one factory call site through ``handler``."""
    built: list[ModelClient] = []

    def build(provider_type, *, base_url, api_key, metrics, tracer, admin_api_key=None):  # type: ignore[no-untyped-def]
        client = build_model_client(
            provider_type,
            base_url=base_url,
            api_key=api_key,
            admin_api_key=admin_api_key,
            transport=httpx.MockTransport(handler),
            metrics=metrics,
            tracer=tracer,
        )
        built.append(client)
        return client

    monkeypatch.setattr(service, "build_model_client", build)
    return built


def _closed(client: ModelClient) -> bool:
    raw = cast(Any, client)._wrapped
    return bool(raw._client.is_closed and raw._native.is_closed)


async def _provider(
    session: AsyncSession, ctx: WorkspaceContext, *, provider_type: str = "ollama"
) -> ModelProvider:
    provider = ModelProvider(
        workspace_id=ctx.workspace_id,
        type=provider_type,
        display_name="Ollama Main",
        base_url=BASE_URL,
    )
    session.add(provider)
    await session.flush()
    return provider


async def _audit_rows(session: AsyncSession, ctx: WorkspaceContext) -> list[AuditEvent]:
    rows = await session.scalars(
        select(AuditEvent)
        .where(AuditEvent.workspace_id == ctx.workspace_id)
        .order_by(AuditEvent.created_at, AuditEvent.id)
    )
    return list(rows)


def _args(
    session: AsyncSession, crypto: SecretCrypto, ctx: WorkspaceContext, provider_id: UUID
) -> tuple[AsyncSession, SecretCrypto, WorkspaceContext, UUID, JhinMetrics, Tracer]:
    return (session, crypto, ctx, provider_id, noop_metrics(), noop_tracer())


async def test_list_merges_loaded_flag_and_show_details(
    session: AsyncSession,
    admin_ctx: WorkspaceContext,
    crypto: SecretCrypto,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[httpx.Request] = []
    built = _install(monkeypatch, _handler(seen))
    provider = await _provider(session, admin_ctx)

    snapshot = await ollama_service.list_ollama_models(
        *_args(session, crypto, admin_ctx, provider.id)
    )

    rows = [OllamaModelOut.model_validate(row, from_attributes=True) for row in snapshot.models]
    assert [row.model_dump(mode="json") for row in rows] == EXPECTED_MODELS
    assert snapshot.detail is None
    assert snapshot.fetched_at.tzinfo is not None
    # Two list reads plus one show per installed model, all on the origin.
    assert sorted(str(request.url) for request in seen) == [
        "http://192.168.1.79:11434/api/ps",
        "http://192.168.1.79:11434/api/show",
        "http://192.168.1.79:11434/api/show",
        "http://192.168.1.79:11434/api/tags",
    ]
    assert len(built) == 1 and _closed(built[0])
    # Reads leave no audit trail, like /models and /balance.
    assert await _audit_rows(session, admin_ctx) == []


async def test_list_degrades_when_ps_fails_and_when_show_fails(
    session: AsyncSession,
    admin_ctx: WorkspaceContext,
    crypto: SecretCrypto,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, _handler([], failing={"/api/ps": 500, "/api/show": 500}))
    provider = await _provider(session, admin_ctx)

    snapshot = await ollama_service.list_ollama_models(
        *_args(session, crypto, admin_ctx, provider.id)
    )

    assert snapshot.detail == "ollama: HTTP 500: boom"
    assert [row.name for row in snapshot.models] == ["muse-glimmer:latest", "qwen3.8:latest"]
    for row in snapshot.models:
        assert row.loaded is False
        assert row.size_vram_bytes is None
        assert row.context_length is None
        assert row.capabilities == []
        assert row.keeps_loaded is False


async def test_list_reports_an_unreachable_host_instead_of_failing(
    session: AsyncSession,
    admin_ctx: WorkspaceContext,
    crypto: SecretCrypto,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refused(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    built = _install(monkeypatch, refused)
    provider = await _provider(session, admin_ctx)

    models = await ollama_service.list_ollama_models(
        *_args(session, crypto, admin_ctx, provider.id)
    )
    loaded = await ollama_service.list_loaded_models(
        *_args(session, crypto, admin_ctx, provider.id)
    )
    details, detail = await ollama_service.show_ollama_model(
        *_args(session, crypto, admin_ctx, provider.id), name="qwen3.8:latest"
    )

    assert models.models == [] and models.detail == "ollama: network error: ConnectError"
    assert loaded.models == [] and loaded.detail == "ollama: network error: ConnectError"
    assert details is None and detail == "ollama: network error: ConnectError"
    assert len(built) == 3 and all(_closed(client) for client in built)


async def test_list_times_out_when_the_host_hangs(
    session: AsyncSession,
    admin_ctx: WorkspaceContext,
    crypto: SecretCrypto,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ollama_service, "OLLAMA_LIST_TIMEOUT_SECONDS", 0.05)
    built = _install(monkeypatch, _handler([], delay={"/api/ps": 0.5}))
    provider = await _provider(session, admin_ctx)

    loaded = await ollama_service.list_loaded_models(
        *_args(session, crypto, admin_ctx, provider.id)
    )
    assert loaded.models == []
    assert loaded.detail == "Ollama did not answer in time"

    # In the full list a hanging /api/ps only costs the loaded flags.
    models = await ollama_service.list_ollama_models(
        *_args(session, crypto, admin_ctx, provider.id)
    )
    assert [row.name for row in models.models] == ["muse-glimmer:latest", "qwen3.8:latest"]
    assert models.detail == "Ollama did not answer in time"
    assert all(row.loaded is False for row in models.models)
    assert models.models[1].context_length == 40960
    assert all(_closed(client) for client in built)


async def test_loaded_and_show_expose_the_wire_shape(
    session: AsyncSession,
    admin_ctx: WorkspaceContext,
    crypto: SecretCrypto,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, _handler([]))
    provider = await _provider(session, admin_ctx)

    loaded = await ollama_service.list_loaded_models(
        *_args(session, crypto, admin_ctx, provider.id)
    )
    assert loaded.detail is None
    assert [
        OllamaLoadedModelOut.model_validate(row, from_attributes=True).model_dump(mode="json")
        for row in loaded.models
    ] == [
        {
            "name": "muse-glimmer:latest",
            "size_bytes": 18200000000,
            "size_vram_bytes": 18200000000,
            "expires_at": "2026-09-02T10:05:00Z",
            "keeps_loaded": False,
            "context_length": 8192,
        }
    ]

    details, detail = await ollama_service.show_ollama_model(
        *_args(session, crypto, admin_ctx, provider.id), name="qwen3.8:latest"
    )
    assert detail is None
    assert details is not None
    assert OllamaModelDetailsOut.model_validate(details, from_attributes=True).model_dump(
        mode="json"
    ) == {
        "name": "qwen3.8:latest",
        "family": "qwen3",
        "parameter_size": "27.3B",
        "quantization": "Q4_K_M",
        "context_length": 40960,
        "capabilities": ["completion", "tools", "thinking"],
        "license": "Apache License 2.0",
    }


async def test_list_refuses_a_non_ollama_provider_with_409(
    session: AsyncSession,
    admin_ctx: WorkspaceContext,
    crypto: SecretCrypto,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def never(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no client may be built for a non-Ollama provider")

    built = _install(monkeypatch, never)
    provider = await _provider(session, admin_ctx, provider_type="openai")
    args = _args(session, crypto, admin_ctx, provider.id)
    request_id = new_uuid7()

    calls = [
        ollama_service.list_ollama_models(*args),
        ollama_service.list_loaded_models(*args),
        ollama_service.show_ollama_model(*args, name="gpt-5"),
        ollama_service.load_ollama_model(
            *args, model="gpt-5", keep_alive="5m", request_id=request_id, ip_hash="hash"
        ),
        ollama_service.unload_ollama_model(
            *args, model="gpt-5", request_id=request_id, ip_hash="hash"
        ),
    ]
    for call in calls:
        with pytest.raises(HTTPException) as excinfo:
            await call
        assert excinfo.value.status_code == 409
        assert excinfo.value.detail == NOT_OLLAMA
    assert built == []
    assert await _audit_rows(session, admin_ctx) == []

    # An unknown provider id is still a plain 404, checked before the type.
    with pytest.raises(HTTPException) as missing:
        await ollama_service.list_ollama_models(*_args(session, crypto, admin_ctx, new_uuid7()))
    assert missing.value.status_code == 404


async def test_load_records_audit_and_returns_loaded(
    session: AsyncSession,
    admin_ctx: WorkspaceContext,
    crypto: SecretCrypto,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[httpx.Request] = []
    built = _install(monkeypatch, _handler(seen))
    provider = await _provider(session, admin_ctx)
    request_id = new_uuid7()

    outcome = await ollama_service.load_ollama_model(
        *_args(session, crypto, admin_ctx, provider.id),
        model="qwen3.8:latest",
        keep_alive="5m",
        request_id=request_id,
        ip_hash="hash",
    )

    assert outcome.ok is True
    assert outcome.status == "loaded"
    assert outcome.model == "qwen3.8:latest"
    assert outcome.keep_alive == "5m"
    assert outcome.detail == (
        "qwen3.8:latest is loaded and stays in memory for 5m after its last request."
    )
    assert str(seen[0].url) == "http://192.168.1.79:11434/api/generate"
    assert json.loads(seen[0].content) == {
        "model": "qwen3.8:latest",
        "keep_alive": "5m",
        "stream": False,
    }
    assert _closed(built[0])

    forever = await ollama_service.load_ollama_model(
        *_args(session, crypto, admin_ctx, provider.id),
        model="qwen3.8:latest",
        keep_alive="-1",
        request_id=new_uuid7(),
        ip_hash="hash",
    )
    assert forever.detail == "qwen3.8:latest is loaded and stays in memory until you unload it."

    rows = await _audit_rows(session, admin_ctx)
    assert [row.action for row in rows] == ["provider.ollama_model_loaded"] * 2
    assert rows[0].metadata_json == {
        "display_name": "Ollama Main",
        "model": "qwen3.8:latest",
        "keep_alive": "5m",
        "status": "loaded",
    }
    assert rows[1].metadata_json["keep_alive"] == "-1"
    assert rows[0].target_type == "model_provider"
    assert rows[0].target_id == provider.id
    assert rows[0].actor_id == admin_ctx.user.id
    assert rows[0].request_id == request_id
    assert rows[0].ip_hash == "hash"
    # A load is not a verification: the row's own status is untouched.
    await session.refresh(provider)
    assert provider.last_error is None
    assert provider.last_verified_at is None


async def test_load_hands_off_after_the_response_budget(
    session: AsyncSession,
    admin_ctx: WorkspaceContext,
    crypto: SecretCrypto,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ollama_service, "OLLAMA_LOAD_RESPONSE_BUDGET_SECONDS", 0.05)
    built = _install(monkeypatch, _handler([], delay={"/api/generate": 0.4}))
    provider = await _provider(session, admin_ctx)

    started = asyncio.get_running_loop().time()
    outcome = await ollama_service.load_ollama_model(
        *_args(session, crypto, admin_ctx, provider.id),
        model="qwen3.8:latest",
        keep_alive="1h",
        request_id=new_uuid7(),
        ip_hash="hash",
    )
    elapsed = asyncio.get_running_loop().time() - started

    assert elapsed < 0.35, "the request must answer within the budget, not wait for the load"
    assert outcome.ok is True
    assert outcome.status == "loading"
    assert outcome.keep_alive == "1h"
    assert outcome.detail == (
        "Ollama is still loading qwen3.8:latest. It will show as loaded when it finishes."
    )
    pending = list(ollama_service._BACKGROUND_LOADS)
    assert len(pending) == 1 and not pending[0].done()
    # The task owns the client now; the request path must not have closed it
    # under a load that is still in flight.
    assert not _closed(built[0])

    result = await pending[0]
    assert result.done_reason == "load"
    assert pending[0] not in ollama_service._BACKGROUND_LOADS
    assert _closed(built[0])

    rows = await _audit_rows(session, admin_ctx)
    assert [row.action for row in rows] == ["provider.ollama_model_loaded"]
    assert rows[0].metadata_json == {
        "display_name": "Ollama Main",
        "model": "qwen3.8:latest",
        "keep_alive": "1h",
        "status": "loading",
    }


async def test_a_handed_off_load_that_fails_is_retrieved_and_closes_the_client(
    session: AsyncSession,
    admin_ctx: WorkspaceContext,
    crypto: SecretCrypto,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ollama_service, "OLLAMA_LOAD_RESPONSE_BUDGET_SECONDS", 0.05)
    built = _install(monkeypatch, _handler([], delay={"/api/generate": 0.3}))
    provider = await _provider(session, admin_ctx)

    outcome = await ollama_service.load_ollama_model(
        *_args(session, crypto, admin_ctx, provider.id),
        model="nope:latest",
        keep_alive="5m",
        request_id=new_uuid7(),
        ip_hash="hash",
    )
    assert outcome.status == "loading"
    pending = list(ollama_service._BACKGROUND_LOADS)
    assert len(pending) == 1

    (error,) = await asyncio.gather(pending[0], return_exceptions=True)
    assert isinstance(error, Exception)
    assert "nope:latest" in str(error)
    assert pending[0] not in ollama_service._BACKGROUND_LOADS
    assert _closed(built[0])


async def test_load_failure_is_ok_false_with_redacted_detail_and_failed_audit(
    session: AsyncSession,
    admin_ctx: WorkspaceContext,
    crypto: SecretCrypto,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built = _install(monkeypatch, _handler([]))
    provider = await _provider(session, admin_ctx)

    outcome = await ollama_service.load_ollama_model(
        *_args(session, crypto, admin_ctx, provider.id),
        model="nope:latest",
        keep_alive="5m",
        request_id=new_uuid7(),
        ip_hash="hash",
    )
    assert outcome.ok is False
    assert outcome.status == "failed"
    assert outcome.model == "nope:latest"
    assert outcome.keep_alive == "5m"
    assert outcome.detail == "ollama: HTTP 404: model 'nope:latest' not found, try pulling it first"
    assert _closed(built[0])

    # Whatever a proxy in front of Ollama echoes back goes through the same
    # redactor as every other provider error.
    register_secret_material("hunter2-proxy-token")
    leaked = await ollama_service.load_ollama_model(
        *_args(session, crypto, admin_ctx, provider.id),
        model="secret:latest",
        keep_alive="5m",
        request_id=new_uuid7(),
        ip_hash="hash",
    )
    assert leaked.ok is False
    assert "hunter2-proxy-token" not in leaked.detail
    assert "[REDACTED]" in leaked.detail

    rows = await _audit_rows(session, admin_ctx)
    assert [row.action for row in rows] == ["provider.ollama_model_load_failed"] * 2
    assert rows[0].metadata_json == {
        "display_name": "Ollama Main",
        "model": "nope:latest",
        "keep_alive": "5m",
        "detail": "ollama: HTTP 404: model 'nope:latest' not found, try pulling it first",
    }
    assert "hunter2-proxy-token" not in json.dumps(rows[1].metadata_json)
    await session.refresh(provider)
    assert provider.last_error is None


async def test_unload_records_audit(
    session: AsyncSession,
    admin_ctx: WorkspaceContext,
    crypto: SecretCrypto,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[httpx.Request] = []
    built = _install(monkeypatch, _handler(seen))
    provider = await _provider(session, admin_ctx)

    outcome = await ollama_service.unload_ollama_model(
        *_args(session, crypto, admin_ctx, provider.id),
        model="muse-glimmer:latest",
        request_id=new_uuid7(),
        ip_hash="hash",
    )
    assert outcome.ok is True
    assert outcome.status == "unloaded"
    assert outcome.keep_alive is None
    assert outcome.detail == "muse-glimmer:latest was unloaded."
    # The host is asked what is resident before the unload, so the generate
    # call is no longer the first request - and the sentinel is a JSON number.
    assert [request.url.path for request in seen] == ["/api/ps", "/api/generate"]
    assert json.loads(seen[1].content) == {
        "model": "muse-glimmer:latest",
        "keep_alive": 0,
        "stream": False,
    }
    assert _closed(built[0])

    failed = await ollama_service.unload_ollama_model(
        *_args(session, crypto, admin_ctx, provider.id),
        model="nope:latest",
        request_id=new_uuid7(),
        ip_hash="hash",
    )
    assert failed.ok is False
    assert failed.status == "failed"
    assert failed.detail == "ollama: HTTP 404: model 'nope:latest' not found, try pulling it first"

    rows = await _audit_rows(session, admin_ctx)
    assert [row.action for row in rows] == [
        "provider.ollama_model_unloaded",
        "provider.ollama_model_unload_failed",
    ]
    assert rows[0].metadata_json == {"display_name": "Ollama Main", "model": "muse-glimmer:latest"}
    assert rows[1].metadata_json == {
        "display_name": "Ollama Main",
        "model": "nope:latest",
        "detail": "ollama: HTTP 404: model 'nope:latest' not found, try pulling it first",
    }


def test_keep_alive_zero_is_rejected_by_the_schema() -> None:
    assert OllamaLoadIn(model="qwen3.8:latest").keep_alive == "5m"
    assert OllamaLoadIn(model="qwen3.8:latest", keep_alive=" 1h ").keep_alive == "1h"
    assert OllamaLoadIn(model="qwen3.8:latest", keep_alive="-1").keep_alive == "-1"

    with pytest.raises(ValidationError) as excinfo:
        OllamaLoadIn(model="qwen3.8:latest", keep_alive="0")
    assert "keep_alive 0 unloads a model; call unload instead" in str(excinfo.value)

    with pytest.raises(ValidationError) as rejected:
        OllamaLoadIn(model="qwen3.8:latest", keep_alive="5d")
    assert "keep_alive must be a duration like 5m or 1h" in str(rejected.value)

    with pytest.raises(ValidationError):
        OllamaLoadIn(model="", keep_alive="5m")


def test_keeps_loaded_threshold() -> None:
    now = datetime(2026, 9, 2, 10, 0, tzinfo=UTC)
    assert ollama_service.keeps_loaded(None, now) is True
    assert ollama_service.keeps_loaded(now + timedelta(minutes=5), now) is False
    assert ollama_service.keeps_loaded(now + timedelta(days=364), now) is False
    assert ollama_service.keeps_loaded(now + timedelta(days=366), now) is True
    # What Ollama actually reports for keep_alive -1: centuries out.
    assert ollama_service.keeps_loaded(datetime(2292, 1, 1, tzinfo=UTC), now) is True
    assert ollama_service.keeps_loaded(now - timedelta(minutes=1), now) is False
