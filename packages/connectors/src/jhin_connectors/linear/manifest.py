"""Static declaration of the Linear connector (plan 11.1, 11.3)."""

from jhin_connectors.linear.client import DEFAULT_BASE_URL
from jhin_connectors.linear.webhook import WEBHOOK_EVENTS
from jhin_connectors.manifest import (
    AuthSchemeSpec,
    ConfigFieldSpec,
    ConnectorManifest,
    SecretFieldSpec,
)

LINEAR_CAPABILITIES: tuple[str, ...] = (
    "linear.issue.read",
    "linear.issue.search",
    "linear.issue.create",
    "linear.issue.update",
    "linear.comment.create",
    "linear.metadata.read",
)

LINEAR_MANIFEST = ConnectorManifest(
    connector_type="linear",
    display_name="Linear",
    icon="linear",
    description="Issues, comments, teams, and workflow states over Linear's GraphQL API.",
    auth_schemes=(
        AuthSchemeSpec(
            type="api_key",
            label="Personal API key",
            description=(
                "Personal/self-hosted path: a Linear API key created under "
                "Settings → Security & access. Sent as the bare Authorization "
                "header value (Linear's documented scheme)."
            ),
            secret_fields=(
                SecretFieldSpec(name="api_key", label="API key", placeholder="lin_api_…"),
            ),
        ),
        AuthSchemeSpec(
            type="oauth",
            label="OAuth 2.0",
            description="Connect a Linear account with browser sign-in.",
            secret_fields=(
                SecretFieldSpec(
                    name="access_token", label="Access token", placeholder="lin_oauth_…"
                ),
            ),
        ),
    ),
    config_fields=(
        ConfigFieldSpec(
            name="base_url",
            label="API base URL",
            required=False,
            placeholder="https://api.linear.app",
            help="Override for a test server (e.g. the dev stack's fake Linear).",
            kind="text",
            default=DEFAULT_BASE_URL,
        ),
    ),
    webhook_events=WEBHOOK_EVENTS,
    canonical_events=(
        "connector.linear.issue.created",
        "connector.linear.issue.updated",
        "connector.linear.issue.removed",
        "connector.linear.comment.created",
    ),
    capabilities=LINEAR_CAPABILITIES,
    docs_url="https://linear.app/developers/graphql",
    webhook_secret_mode="generated",
    webhook_signature_algorithm="hmac-sha256",
    webhook_setup_help="Use the generated secret when configuring this webhook in Linear.",
    supports_webhooks=True,
)
