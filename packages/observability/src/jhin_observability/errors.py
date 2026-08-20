"""Closed, payload-free errors safe for telemetry boundaries."""

import re
from dataclasses import dataclass
from enum import StrEnum


class SafeErrorCode(StrEnum):
    INTERNAL_ERROR = "internal_error"
    INVALID_REQUEST = "invalid_request"
    AUTHENTICATION_FAILED = "authentication_failed"
    AUTHORIZATION_FAILED = "authorization_failed"
    RATE_LIMITED = "rate_limited"
    UPSTREAM_UNAVAILABLE = "upstream_unavailable"
    TIMEOUT = "timeout"
    CONFLICT = "conflict"
    EXECUTION_UNKNOWN = "execution_unknown"


@dataclass(frozen=True)
class SafeError:
    type: str
    code: SafeErrorCode


_ERROR_TYPE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]{0,127}$")


def safe_error(exc: BaseException, *, code: SafeErrorCode) -> SafeError:
    """Return only a bounded exception class name and caller-selected code."""
    error_type = type(exc).__name__
    return SafeError(
        type=error_type if _ERROR_TYPE_RE.fullmatch(error_type) else "Error",
        code=code,
    )
