"""Unit tests for the session-bound CSRF double-submit dependency."""

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from jhin_api.security.csrf import csrf_protect
from jhin_api.security.tokens import csrf_token_for_session, new_session_token
from jhin_api.settings import Settings

SESSION = "session-token-under-test"
BOUND = csrf_token_for_session(SESSION)


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


def test_matching_cookie_and_header_passes_without_a_session() -> None:
    """No session cookie: plain double submit. Such routes require auth anyway."""
    client = TestClient(make_app())
    client.cookies.set("jhin_csrf", "token-value")
    response = client.post("/mutate", headers={"x-csrf-token": "token-value"})
    assert response.status_code == 200


def test_safe_methods_skip_csrf() -> None:
    client = TestClient(make_app())
    assert client.get("/read").status_code == 200


def test_token_bound_to_the_session_is_accepted() -> None:
    client = TestClient(make_app())
    client.cookies.set("jhin_session", SESSION)
    client.cookies.set("jhin_csrf", BOUND)
    assert client.post("/mutate", headers={"x-csrf-token": BOUND}).status_code == 200


def test_attacker_planted_token_is_rejected_even_when_it_double_submits() -> None:
    """The core of the binding.

    A cookie-tossing attacker (hostile sibling subdomain, or a network position
    on a plaintext deployment) can overwrite the CSRF cookie with a value they
    know and echo it in a header. Without the session binding that is a valid
    double submit; with it, the token has to be the HMAC of a session token the
    attacker does not have.
    """
    client = TestClient(make_app())
    client.cookies.set("jhin_session", SESSION)
    client.cookies.set("jhin_csrf", "attacker-chosen-value")
    response = client.post("/mutate", headers={"x-csrf-token": "attacker-chosen-value"})
    assert response.status_code == 403


def test_token_from_a_previous_session_is_rejected() -> None:
    """Session rotation invalidates the old CSRF token too."""
    client = TestClient(make_app())
    stale = csrf_token_for_session(new_session_token())
    client.cookies.set("jhin_session", SESSION)
    client.cookies.set("jhin_csrf", stale)
    assert client.post("/mutate", headers={"x-csrf-token": stale}).status_code == 403
