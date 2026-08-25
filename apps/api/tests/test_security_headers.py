"""Response security headers, body-size limits, and safe validation errors."""

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field

from jhin_api.security.headers import API_CSP, HSTS_VALUE, SecurityHeadersMiddleware
from jhin_api.security.limits import RequestSizeLimitMiddleware
from jhin_api.security.validation import safe_validation_error_handler


def make_app(*, hsts: bool = False, max_body_bytes: int = 1024) -> FastAPI:
    app = FastAPI()

    class Payload(BaseModel):
        value: str = Field(max_length=8)

    @app.get("/thing")
    async def thing() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/cached")
    async def cached() -> dict[str, bool]:
        from starlette.responses import JSONResponse

        return JSONResponse({"ok": True}, headers={"cache-control": "public, max-age=60"})

    @app.post("/secret")
    async def secret(payload: Payload) -> dict[str, str]:
        return {"value": payload.value}

    from fastapi.exceptions import RequestValidationError

    app.add_exception_handler(RequestValidationError, safe_validation_error_handler)
    app.add_middleware(RequestSizeLimitMiddleware, max_body_bytes=max_body_bytes)
    app.add_middleware(SecurityHeadersMiddleware, hsts=hsts)
    return app


# --- headers ---------------------------------------------------------------


def test_baseline_headers_are_present() -> None:
    response = TestClient(make_app()).get("/thing")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "camera=()" in response.headers["permissions-policy"]
    assert response.headers["cross-origin-opener-policy"] == "same-origin"
    assert response.headers["cross-origin-resource-policy"] == "same-origin"


def test_api_csp_denies_everything_and_forbids_framing() -> None:
    response = TestClient(make_app()).get("/thing")
    policy = response.headers["content-security-policy"]
    assert policy == API_CSP
    assert "default-src 'none'" in policy
    assert "frame-ancestors 'none'" in policy


def test_authenticated_payloads_are_not_cacheable_by_default() -> None:
    response = TestClient(make_app()).get("/thing")
    assert response.headers["cache-control"] == "no-store"


def test_routes_that_set_their_own_cache_policy_keep_it() -> None:
    """Media deliberately caches; the middleware must not stamp over it."""
    response = TestClient(make_app()).get("/cached")
    assert response.headers["cache-control"] == "public, max-age=60"


def test_hsts_is_absent_by_default() -> None:
    response = TestClient(make_app(hsts=False)).get("/thing")
    assert "strict-transport-security" not in response.headers


def test_hsts_is_emitted_for_https_production() -> None:
    response = TestClient(make_app(hsts=True)).get("/thing")
    assert response.headers["strict-transport-security"] == HSTS_VALUE


# --- body size limit -------------------------------------------------------


def test_body_within_the_limit_is_accepted() -> None:
    client = TestClient(make_app(max_body_bytes=1024))
    assert client.post("/secret", json={"value": "ok"}).status_code == 200


def test_oversized_declared_body_is_rejected_with_413() -> None:
    client = TestClient(make_app(max_body_bytes=64))
    response = client.post("/secret", json={"value": "x" * 4096})
    assert response.status_code == 413
    assert response.json()["detail"] == "Request body is too large"


def test_oversized_body_is_rejected_when_length_is_not_declared() -> None:
    """A chunked upload that lies about (or omits) its size is cut off too."""

    def chunks() -> object:
        for _ in range(64):
            yield b"x" * 1024

    client = TestClient(make_app(max_body_bytes=2048))
    response = client.post("/secret", content=chunks())  # type: ignore[arg-type]
    assert response.status_code in {400, 413}


def test_get_requests_are_not_penalised() -> None:
    assert TestClient(make_app(max_body_bytes=1)).get("/thing").status_code == 200


# --- validation errors -----------------------------------------------------


def test_validation_error_does_not_echo_the_submitted_value() -> None:
    """A 422 on POST /secrets must not hand the credential back to the client."""
    client = TestClient(make_app(max_body_bytes=1_000_000))
    credential = "sk-live-do-not-echo-this-value-anywhere"
    response = client.post("/secret", json={"value": credential})
    assert response.status_code == 422
    assert credential not in response.text
    assert "do-not-echo" not in response.text


def test_validation_error_still_says_which_field_and_why() -> None:
    client = TestClient(make_app(max_body_bytes=1_000_000))
    response = client.post("/secret", json={"value": "x" * 99})
    detail = response.json()["detail"]
    assert detail[0]["loc"] == ["body", "value"]
    assert "at most 8" in detail[0]["msg"]
    assert "input" not in detail[0]


def test_missing_field_is_reported_without_a_body_echo() -> None:
    client = TestClient(make_app(max_body_bytes=1_000_000))
    response = client.post("/secret", json={"wrong": "shape"})
    assert response.status_code == 422
    assert "shape" not in response.text
