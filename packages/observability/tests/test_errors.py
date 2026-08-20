"""Safe public error-boundary regressions."""

from dataclasses import asdict

from jhin_observability import SafeErrorCode, safe_error


def test_safe_error_never_contains_exception_text_or_arguments() -> None:
    error = safe_error(
        RuntimeError("provider response body canary", {"token": "nested-canary"}),
        code=SafeErrorCode.UPSTREAM_UNAVAILABLE,
    )
    assert asdict(error) == {
        "type": "RuntimeError",
        "code": SafeErrorCode.UPSTREAM_UNAVAILABLE,
    }
