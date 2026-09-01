"""Re-export of the outbound target policy, implemented in :mod:`jhin_domain.endpoints`.

The implementation moved down into the domain package so the OAuth core can
validate URLs without depending on this one: the agent worker imports
``jhin_oauth`` for background token refresh, and it must not be able to import
an executable connector or the credentials one resolves. Connector code keeps
reaching the policy through this module, which is where it has always been.
"""

from __future__ import annotations

from jhin_domain.endpoints import (
    EndpointPolicyError,
    validate_http_origin,
    validate_postgres_target,
    validate_public_http_url,
)

__all__ = [
    "EndpointPolicyError",
    "validate_http_origin",
    "validate_postgres_target",
    "validate_public_http_url",
]
