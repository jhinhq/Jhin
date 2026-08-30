"""The shipped provider table, and the shape every consumer reads it through.

A statically-known provider is the one authorization server nobody can ask,
so the table is the only source of truth for its protocol facts. These tests
pin the attributes callers actually reach for — a missing one is an
``AttributeError`` at request time, not a type error at build time, because
the dataclass is ``slots=True`` and the read sites are ordinary attribute
access.
"""

from __future__ import annotations

import pytest

from jhin_connectors.oauth_providers import (
    STATIC_PROVIDERS,
    StaticOAuthProvider,
    provider_for_connector,
    provider_metadata,
)

#: Every attribute the API's OAuth service reads off a provider. Kept as a
#: literal list so that adding a read site without a field is caught here
#: rather than by a 500 on the device-start route.
READ_BY_THE_API = (
    "key",
    "connector_type",
    "issuer",
    "authorization_endpoint",
    "token_endpoint",
    "device_authorization_endpoint",
    "revocation_endpoint",
    "default_scopes",
    "requires_client_secret",
    "supports_refresh",
    "extra_authorize_params",
)


@pytest.mark.parametrize("attribute", READ_BY_THE_API)
@pytest.mark.parametrize("key", sorted(STATIC_PROVIDERS))
def test_every_provider_answers_every_attribute_the_api_reads(key: str, attribute: str) -> None:
    getattr(STATIC_PROVIDERS[key], attribute)


def test_github_publishes_no_revocation_endpoint() -> None:
    """Empty is the real answer, and it must stay expressible.

    GitHub retires a token through an authenticated REST call on the app, not
    an RFC 7009 endpoint. ``start_device_flow`` passes this straight into the
    pending row, whose column is nullable, so the empty string has to become
    ``None`` rather than a stored blank URL.
    """
    provider = STATIC_PROVIDERS["github"]
    assert provider.revocation_endpoint == ""
    assert provider_metadata(provider).revocation_endpoint is None


def test_a_provider_is_reachable_by_connector_type_without_its_key() -> None:
    """The panel names a connector, never this table's internal key."""
    provider = provider_for_connector("github")
    assert provider is not None
    assert provider.key == "github"
    assert provider_for_connector("definitely-not-a-connector") is None


def test_metadata_never_claims_dynamic_registration() -> None:
    """A provider is in this table precisely because it has no DCR."""
    for provider in STATIC_PROVIDERS.values():
        metadata = provider_metadata(provider)
        assert metadata.registration_endpoint is None
        assert not metadata.supports_dcr()
        # The issuer must survive round-tripping byte-identically: client
        # registrations are keyed by it.
        assert metadata.issuer == provider.issuer


def test_a_revocation_endpoint_is_validated_on_the_way_out() -> None:
    """Outbound policy is applied at use, not at import.

    A module-level constant that raised on import would take the API process
    down for a policy the operator can change at runtime.
    """
    hostile = StaticOAuthProvider(
        key="hostile",
        connector_type="hostile",
        issuer="https://as.example.com",
        authorization_endpoint="https://as.example.com/authorize",
        token_endpoint="https://as.example.com/token",
        revocation_endpoint="http://169.254.169.254/revoke",
    )
    with pytest.raises(Exception) as excinfo:
        provider_metadata(hostile)
    assert "169.254" not in str(excinfo.value)
