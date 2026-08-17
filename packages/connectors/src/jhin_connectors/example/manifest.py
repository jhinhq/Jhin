"""Static declaration of the example connector (plan 11.1)."""

from jhin_connectors.manifest import (
    AuthSchemeSpec,
    ConfigFieldSpec,
    ConnectorManifest,
    SecretFieldSpec,
)

EXAMPLE_MANIFEST = ConnectorManifest(
    connector_type="example",
    display_name="Example",
    icon="example",
    description="Template connector demonstrating the SDK surface.",
    auth_schemes=(
        AuthSchemeSpec(
            type="api_key",
            label="API key",
            description="A single API key credential.",
            secret_fields=(SecretFieldSpec(name="api_key", label="API key", placeholder="ex-..."),),
        ),
    ),
    config_fields=(
        ConfigFieldSpec(
            name="base_url",
            label="Base URL",
            required=False,
            placeholder="https://api.example.com",
            help="Override for self-hosted instances and tests.",
        ),
    ),
    webhook_events=("ping",),
    capabilities=("example.ping",),
    supports_webhooks=True,
)
