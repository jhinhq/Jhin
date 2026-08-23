"""Static declaration of the generic HTTP connector (plan 11.7).

One connector type covers any HTTP API behind a fixed base URL. There is no
arbitrary-URL access: every request is joined to the connection's validated
``base_url`` (public https, or an operator-allow-listed origin), and grants
can pin the method and glob the path.
"""

from jhin_connectors.manifest import (
    AuthSchemeSpec,
    ConfigFieldSpec,
    ConnectorManifest,
    SecretFieldSpec,
)

HTTP_CONNECTOR_TYPE = "http"

AUTH_NONE = "none"
AUTH_BEARER = "bearer"
AUTH_HEADER = "header"
AUTH_BASIC = "basic"

HTTP_CAPABILITIES: tuple[str, ...] = ("http.get", "http.request")

HTTP_MANIFEST = ConnectorManifest(
    connector_type=HTTP_CONNECTOR_TYPE,
    display_name="Any HTTP API",
    icon="http",
    description=(
        "Call any HTTP API from a fixed base URL. Reads (GET/HEAD) and writes "
        "(POST/PUT/PATCH/DELETE) are separate tools, so approval policies and "
        "grants can treat them differently."
    ),
    auth_schemes=(
        AuthSchemeSpec(
            type=AUTH_NONE,
            label="No authentication",
            description="The API accepts unauthenticated requests.",
        ),
        AuthSchemeSpec(
            type=AUTH_BEARER,
            label="Bearer token",
            description="Sent as `Authorization: Bearer <token>` on every request.",
            secret_fields=(
                SecretFieldSpec(name="token", label="Access token", placeholder="token…"),
            ),
        ),
        AuthSchemeSpec(
            type=AUTH_HEADER,
            label="Custom header",
            description="Sent as a header you name (e.g. `X-API-Key`) with the secret value.",
            secret_fields=(
                SecretFieldSpec(name="token", label="Header value (secret)", placeholder="…"),
            ),
        ),
        AuthSchemeSpec(
            type=AUTH_BASIC,
            label="Basic auth",
            description="Sent as `Authorization: Basic …` built from a username and password.",
            secret_fields=(
                SecretFieldSpec(name="username", label="Username"),
                SecretFieldSpec(name="password", label="Password"),
            ),
        ),
    ),
    config_fields=(
        ConfigFieldSpec(
            name="base_url",
            label="Base URL",
            required=True,
            placeholder="https://api.example.com",
            help=(
                "Every request path is joined to this URL. Public https URLs are "
                "allowed; other origins must be allow-listed by the operator."
            ),
        ),
        ConfigFieldSpec(
            name="default_headers",
            label="Default headers",
            required=False,
            kind="string_list",
            placeholder="Accept: application/vnd.api+json",
            help=(
                "Optional non-secret headers sent on every request, one per line "
                "as `Name: value`. Authentication and cookie headers are not "
                "allowed here — use an auth scheme."
            ),
        ),
        ConfigFieldSpec(
            name="header_name",
            label="Header name",
            required=True,
            placeholder="X-API-Key",
            help="The request header that carries the secret value.",
            auth_types=(AUTH_HEADER,),
        ),
    ),
    capabilities=HTTP_CAPABILITIES,
    webhook_secret_mode="none",
)

__all__ = [
    "AUTH_BASIC",
    "AUTH_BEARER",
    "AUTH_HEADER",
    "AUTH_NONE",
    "HTTP_CAPABILITIES",
    "HTTP_CONNECTOR_TYPE",
    "HTTP_MANIFEST",
]
