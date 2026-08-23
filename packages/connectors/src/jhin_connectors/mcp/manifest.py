"""Static declaration of the generic MCP connector (plan 11.1).

One connector type covers *any* remote Model Context Protocol server. The
tools themselves are not static: they are discovered from the server at
verification time and registered per connection as ``mcp.<server_slug>.<tool>``
(see :mod:`jhin_connectors.mcp.discovery`).

Auth schemes: none, bearer token, or a custom header carrying a secret.
OAuth 2.0 (authorization-code or client-credentials) is deliberately out of
scope for this release — see docs/architecture/mcp.md "Roadmap".
"""

from jhin_connectors.manifest import (
    AuthSchemeSpec,
    ConfigFieldSpec,
    ConnectorManifest,
    SecretFieldSpec,
)

MCP_CONNECTOR_TYPE = "mcp"

AUTH_NONE = "none"
AUTH_BEARER = "bearer"
AUTH_HEADER = "header"

TRANSPORT_AUTO = "auto"
TRANSPORT_STREAMABLE_HTTP = "streamable_http"
TRANSPORT_SSE = "sse"
TRANSPORTS: tuple[str, ...] = (TRANSPORT_AUTO, TRANSPORT_STREAMABLE_HTTP, TRANSPORT_SSE)

MCP_MANIFEST = ConnectorManifest(
    connector_type=MCP_CONNECTOR_TYPE,
    display_name="Any MCP server",
    icon="mcp",
    description=(
        "Connect any app that offers a remote Model Context Protocol (MCP) server. "
        "Jhin discovers the server's tools and offers them to agents with risk levels "
        "derived from the server's own annotations (read-only, destructive, …)."
    ),
    auth_schemes=(
        AuthSchemeSpec(
            type=AUTH_NONE,
            label="No authentication",
            description="The server accepts unauthenticated requests (local or test servers).",
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
            description=(
                "Sent as a header you name (e.g. `X-API-Key`) with the secret as its value. "
                "OAuth 2.0 sign-in is not supported yet; use a token the provider issues."
            ),
            secret_fields=(
                SecretFieldSpec(name="token", label="Header value (secret)", placeholder="…"),
            ),
        ),
    ),
    config_fields=(
        ConfigFieldSpec(
            name="server_url",
            label="Server URL",
            required=True,
            placeholder="https://mcp.example.com/mcp",
            help=(
                "The Streamable HTTP endpoint from the provider's docs. Public https "
                "servers are allowed; other origins must be allow-listed by the operator."
            ),
        ),
        ConfigFieldSpec(
            name="server_slug",
            label="Short name",
            required=True,
            placeholder="notion",
            help=(
                "Used in tool names as mcp.<short name>.<tool>. Lowercase letters, digits, "
                "and underscores, up to 32 characters."
            ),
        ),
        ConfigFieldSpec(
            name="transport",
            label="Transport",
            required=False,
            placeholder=TRANSPORT_AUTO,
            help="auto (Streamable HTTP, then SSE fallback), streamable_http, or sse.",
            default=TRANSPORT_AUTO,
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
    # Tools are discovered per connection; the capability family is ``mcp.*``.
    capabilities=(),
    docs_url="https://modelcontextprotocol.io/docs/concepts/tools",
    webhook_secret_mode="none",
)

__all__ = [
    "AUTH_BEARER",
    "AUTH_HEADER",
    "AUTH_NONE",
    "MCP_CONNECTOR_TYPE",
    "MCP_MANIFEST",
    "TRANSPORTS",
    "TRANSPORT_AUTO",
    "TRANSPORT_SSE",
    "TRANSPORT_STREAMABLE_HTTP",
]
