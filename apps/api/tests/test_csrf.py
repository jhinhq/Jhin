"""Unit tests for the CSRF double-submit dependency."""

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from jhin_api.security.csrf import csrf_protect
from jhin_api.settings import Settings


def make_app() -> FastAPI:
    app = FastAPI()
    app.state.settings = Settings()

    @app.post("/mutate", dependencies=[Depends(csrf_protect)])
    async def mutate() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/read", dependencies=[Depends(csrf_protect)])
    async def read() -> dict[str, bool]:
        return {"ok": True}

    return app


def test_mutating_request_without_header_is_rejected() -> None:
    client = TestClient(make_app())
    client.cookies.set("jhin_csrf", "token-value")
    assert client.post("/mutate").status_code == 403


def test_mutating_request_with_mismatched_header_is_rejected() -> None:
    client = TestClient(make_app())
    client.cookies.set("jhin_csrf", "token-value")
    response = client.post("/mutate", headers={"x-csrf-token": "different"})
    assert response.status_code == 403


def test_mutating_request_without_cookie_is_rejected() -> None:
    client = TestClient(make_app())
    response = client.post("/mutate", headers={"x-csrf-token": "token-value"})
    assert response.status_code == 403


def test_matching_cookie_and_header_passes() -> None:
    client = TestClient(make_app())
    client.cookies.set("jhin_csrf", "token-value")
    response = client.post("/mutate", headers={"x-csrf-token": "token-value"})
    assert response.status_code == 200


def test_safe_methods_skip_csrf() -> None:
    client = TestClient(make_app())
    assert client.get("/read").status_code == 200
