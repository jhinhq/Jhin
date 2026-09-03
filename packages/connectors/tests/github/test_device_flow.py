"""A device-flow token is an ordinary GitHub connection.

The connector verifies a ``device``-auth connection through the same branch
as a browser OAuth one; this exercises that branch with ``AUTH_DEVICE`` on
the wire, against the in-process fake.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from jhin_connectors.base import VerifyContext
from jhin_connectors.github.auth import AUTH_DEVICE
from jhin_connectors.github.connector import GitHubConnector
from jhin_connectors.testing.fake_github import FakeGitHubServer

ALLOWLIST_ENV = "JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS"


@pytest.fixture(autouse=True)
def _restore_allowlist() -> Iterator[None]:
    """Every test allow-lists its fake's origin and puts the env back after."""
    previous = os.environ.get(ALLOWLIST_ENV)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(ALLOWLIST_ENV, None)
        else:
            os.environ[ALLOWLIST_ENV] = previous


def _allow(*origins: str) -> None:
    current = [entry for entry in os.environ.get(ALLOWLIST_ENV, "").split(",") if entry]
    os.environ[ALLOWLIST_ENV] = ",".join([*current, *origins])


async def test_a_device_flow_token_is_a_working_github_connection() -> None:
    token = "ghu_fake_device_token"
    with FakeGitHubServer(token=token) as api:
        _allow(api.base_url)
        health = await GitHubConnector().verify_connection(
            VerifyContext(
                auth_type=AUTH_DEVICE,
                credentials={"access_token": token},
                config={"base_url": api.base_url},
            )
        )
    assert health.ok
    assert health.details["auth"] == AUTH_DEVICE
    assert token not in health.message
    # Installed somewhere, so the message stays the plain one.
    assert health.message == "Authenticated as fake-user"
    assert health.details["installations"] == "1"


async def test_an_app_installed_nowhere_is_named_as_the_reason_it_reaches_nothing() -> None:
    """GitHub gives no API for the install itself; ``verify`` says when it is
    still missing. The token is real, so the connection stays ``ok``."""
    token = "ghu_fake_uninstalled_token"
    with FakeGitHubServer(token=token, installation_count=0) as api:
        _allow(api.base_url)
        health = await GitHubConnector().verify_connection(
            VerifyContext(
                auth_type=AUTH_DEVICE,
                credentials={"access_token": token},
                config={"base_url": api.base_url},
            )
        )
    assert health.ok
    assert health.message.startswith("Authenticated as fake-user.")
    assert "not installed on any of your GitHub accounts" in health.message
    assert "GitHub Apps on github.com" in health.message
    assert health.details["installations"] == "0"
    assert token not in health.message


async def test_a_token_that_cannot_list_installations_still_verifies_plainly() -> None:
    """A classic OAuth App token gets 403 from ``/user/installations``. That
    is a question the token cannot answer, not a broken connection."""
    token = "ghu_fake_classic_token"
    with FakeGitHubServer(token=token, installations_forbidden=True) as api:
        _allow(api.base_url)
        health = await GitHubConnector().verify_connection(
            VerifyContext(
                auth_type=AUTH_DEVICE,
                credentials={"access_token": token},
                config={"base_url": api.base_url},
            )
        )
    assert health.ok
    assert health.message == "Authenticated as fake-user"
    assert "installations" not in health.details
