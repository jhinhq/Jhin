import pytest
from fastapi import HTTPException

from jhin_sandbox_runner.preview_transport import forwarded_headers, validate_preview_request


def test_preview_request_never_selects_host_or_port():
    value = validate_preview_request(
        {
            "method": "POST",
            "path": "/api/value?x=1",
            "headers": {
                "Cookie": "secret",
                "Authorization": "Bearer secret",
                "Content-Type": "application/json",
            },
            "body_base64": "e30=",
        }
    )
    assert value["path"] == "/api/value?x=1"
    assert value["headers"] == {"Content-Type": "application/json"}
    for path in ["https://evil.test", "//evil.test/path", "/a\r\nHost: evil", "/a\\b"]:
        with pytest.raises(HTTPException):
            validate_preview_request({"method": "GET", "path": path})


def test_preview_cannot_forward_application_credentials():
    headers = forwarded_headers(
        {
            "authorization": "private",
            "cookie": "session=x",
            "host": "control-plane",
            "x-forwarded-for": "private",
            "accept": "text/html",
            "content-type": "text/plain",
        }
    )
    assert headers == {"accept": "text/html", "content-type": "text/plain"}


def test_preview_body_is_bounded():
    with pytest.raises(HTTPException):
        validate_preview_request({"method": "GET", "path": "/", "body_base64": "a" * 3_000_000})
