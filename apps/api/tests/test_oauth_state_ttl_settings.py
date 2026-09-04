"""How long a pending authorization lives, and how long a spent one remembers.

Ten minutes was the number the operator's failed connect ran out of. One
round trip through Cloudflare Access, a GitHub sign-in with a second factor,
a consent screen somebody reads, and an installation picker is not a
ten-minute sequence, and the callback had no way to say so afterwards.

Thirty minutes is the fourth control in front of that route, never the first:
the handle is 256 bits, only its ``sha256`` is stored, the row is single-use,
and it is bound to the initiating user's session. Widening it widens the
window only for somebody who already holds both the handle and the victim's
live session — and holding both means the flow is already lost.

Both bounds are refused at startup rather than clamped, because a number
outside them means somebody meant something this subsystem cannot do.
"""

from __future__ import annotations

import pytest

from jhin_api.oauth import service
from jhin_api.settings import InsecureDeploymentError, Settings


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {"app_url": "http://localhost:3000"}
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[arg-type]


def test_the_state_ttl_defaults_to_thirty_minutes() -> None:
    assert _settings().oauth_state_ttl_seconds == 1800


def test_the_state_ttl_is_not_a_new_maximum_for_this_subsystem() -> None:
    """The device flow already allowed 1800; the manifest flow allows 3600."""
    assert _settings().oauth_state_ttl_seconds <= service.DEVICE_FLOW_MAX_TTL_SECONDS
    assert _settings().oauth_state_ttl_seconds < service.GITHUB_MANIFEST_TTL_SECONDS


@pytest.mark.parametrize("seconds", [0, 1, 59, -60, 3601, 86_400])
def test_an_absurd_state_ttl_is_refused_at_startup(seconds: int) -> None:
    with pytest.raises(InsecureDeploymentError, match="OAUTH_STATE_TTL_SECONDS"):
        _settings(oauth_state_ttl_seconds=seconds)


def test_the_receipt_ttl_defaults_to_ten_minutes() -> None:
    assert _settings().oauth_callback_receipt_ttl_seconds == 600


def test_the_receipt_ttl_may_be_zero_but_not_negative_or_a_day() -> None:
    """``0`` is "no receipts", which is a legitimate operator choice."""
    assert _settings(oauth_callback_receipt_ttl_seconds=0).oauth_callback_receipt_ttl_seconds == 0
    for bad in (-1, 3601, 86_400):
        with pytest.raises(InsecureDeploymentError, match="OAUTH_CALLBACK_RECEIPT_TTL_SECONDS"):
            _settings(oauth_callback_receipt_ttl_seconds=bad)


def test_the_receipt_window_is_clamped_in_code_as_well() -> None:
    """No configuration may turn a receipt into a long-lived token."""
    assert service.receipt_ttl(_settings()) == 600
    assert service.receipt_ttl(_settings(oauth_callback_receipt_ttl_seconds=0)) == 0
    assert service.receipt_ttl(_settings(oauth_callback_receipt_ttl_seconds=3600)) == 3600


def test_expired_rows_are_kept_long_enough_to_explain_themselves() -> None:
    """An operator reporting a failed connect an hour later still gets
    ``state_expired`` rather than an indistinguishable ``state_unknown``."""
    assert service.PURGE_OLDER_THAN_SECONDS == 14_400
