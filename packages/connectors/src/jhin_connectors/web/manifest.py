"""Static declaration of the web connector (docs/architecture/web.md).

One connector type gives agents deny-by-default internet access through two
read-only tools: ``web.search`` (a search API the workspace brings a key
for — Tavily, Brave, or Exa) and ``web.fetch`` (bounded readable-text
retrieval of public https pages). A connection with the ``none`` auth scheme
is fetch-only: no search backend, no key.
"""

from jhin_connectors.manifest import (
    AuthSchemeSpec,
    ConfigFieldSpec,
    ConnectorManifest,
    SecretFieldSpec,
)

WEB_CONNECTOR_TYPE = "web"

AUTH_NONE = "none"
AUTH_BEARER = "bearer"

BACKEND_TAVILY = "tavily"
BACKEND_BRAVE = "brave"
BACKEND_EXA = "exa"
SEARCH_BACKENDS: tuple[str, ...] = (BACKEND_TAVILY, BACKEND_BRAVE, BACKEND_EXA)

WEB_CAPABILITIES: tuple[str, ...] = ("web.search", "web.fetch")

WEB_MANIFEST = ConnectorManifest(
    connector_type=WEB_CONNECTOR_TYPE,
    display_name="Web search & fetch",
    icon="web",
    description=(
        "Search the public web (with a Tavily, Brave, or Exa API key) and read "
        "pages as bounded plain text. Everything that comes back is untrusted "
        "external content; fetch can be limited to specific domains."
    ),
    auth_schemes=(
        AuthSchemeSpec(
            type=AUTH_NONE,
            label="No search key (fetch only)",
            description="Agents can read public pages with web.fetch but cannot search.",
        ),
        AuthSchemeSpec(
            type=AUTH_BEARER,
            label="Search API key",
            description="The API key for the selected search backend (Tavily, Brave, or Exa).",
            secret_fields=(
                SecretFieldSpec(name="token", label="API key", placeholder="tvly-… / BSA… / …"),
            ),
        ),
    ),
    config_fields=(
        ConfigFieldSpec(
            name="search_backend",
            label="Search backend",
            required=True,
            default=BACKEND_TAVILY,
            placeholder=BACKEND_TAVILY,
            help="Which search API the key belongs to: tavily, brave, or exa.",
            auth_types=(AUTH_BEARER,),
        ),
        ConfigFieldSpec(
            name="base_url",
            label="API base URL override",
            required=False,
            placeholder="https://api.tavily.com",
            help=(
                "Leave empty for the backend's official endpoint. Overrides must "
                "be public https origins or operator-allow-listed (dev doubles)."
            ),
        ),
        ConfigFieldSpec(
            name="allowed_domains",
            label="Allowed fetch domains",
            required=False,
            kind="string_list",
            placeholder="docs.python.org\n*.wikipedia.org",
            help=(
                "Optional host patterns (glob, one per line). When set, web.fetch "
                "may only read pages whose host matches one of them."
            ),
        ),
    ),
    capabilities=WEB_CAPABILITIES,
    webhook_secret_mode="none",
)

__all__ = [
    "AUTH_BEARER",
    "AUTH_NONE",
    "BACKEND_BRAVE",
    "BACKEND_EXA",
    "BACKEND_TAVILY",
    "SEARCH_BACKENDS",
    "WEB_CAPABILITIES",
    "WEB_CONNECTOR_TYPE",
    "WEB_MANIFEST",
]
