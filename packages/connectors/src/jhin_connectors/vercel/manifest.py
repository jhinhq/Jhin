"""Static declaration of the Vercel connector."""

from jhin_connectors.manifest import (
    AuthSchemeSpec,
    ConfigFieldSpec,
    ConnectorManifest,
    SecretFieldSpec,
)

DEFAULT_BASE_URL = "https://api.vercel.com"

VERCEL_CAPABILITIES: tuple[str, ...] = (
    "vercel.project.list",
    "vercel.project.read",
    "vercel.deployment.list",
    "vercel.deployment.read",
    "vercel.deployment.logs.read",
    "vercel.environment_metadata.read",
    "vercel.deployment.preview.create",
    "vercel.deployment.redeploy",
    "vercel.deployment.promote",
    "vercel.deployment.alias.assign",
)

VERCEL_MANIFEST = ConnectorManifest(
    connector_type="vercel",
    display_name="Vercel",
    icon="vercel",
    description="Projects, deployment build logs, environment metadata, and governed releases.",
    auth_schemes=(
        AuthSchemeSpec(
            type="access_token",
            label="Access token",
            description="A Vercel access token with access to the selected account or team.",
            secret_fields=(
                SecretFieldSpec(name="token", label="Access token", placeholder="Vercel token"),
            ),
        ),
    ),
    config_fields=(
        ConfigFieldSpec(
            name="team_id",
            label="Team ID",
            required=False,
            placeholder="team_…",
            help="Optional Vercel team whose projects this connection may access.",
        ),
        ConfigFieldSpec(
            name="base_url",
            label="API base URL",
            required=False,
            default=DEFAULT_BASE_URL,
            placeholder=DEFAULT_BASE_URL,
            help="Override only for an operator-approved self-hosted or development endpoint.",
        ),
    ),
    capabilities=VERCEL_CAPABILITIES,
    docs_url="https://vercel.com/docs/rest-api",
    webhook_secret_mode="none",
    supports_webhooks=False,
)
