"""Static declaration of the GitHub connector (plan 11.1, 11.2)."""

from jhin_connectors.github.client import DEFAULT_BASE_URL
from jhin_connectors.github.webhook import WEBHOOK_EVENTS
from jhin_connectors.manifest import (
    AuthSchemeSpec,
    ConfigFieldSpec,
    ConnectorManifest,
    SecretFieldSpec,
)

GITHUB_CAPABILITIES: tuple[str, ...] = (
    "github.repository.read",
    "github.branch.create",
    "github.issue.read",
    "github.issue.comment",
    "github.pull_request.create",
    "github.pull_request.read",
    "github.pull_request.comment",
    "github.pull_request.merge",
    "github.check.read",
    "github.workflow.dispatch",
    "github.workflow_run.read",
)

GITHUB_MANIFEST = ConnectorManifest(
    connector_type="github",
    display_name="GitHub",
    icon="github",
    description="Repositories, branches, issues, pull requests, checks, and Actions.",
    auth_schemes=(
        AuthSchemeSpec(
            type="pat",
            label="Personal access token",
            description="Simple self-hosted path: one fine-grained or classic PAT.",
            secret_fields=(
                SecretFieldSpec(name="token", label="Personal access token", placeholder="ghp_…"),
            ),
        ),
        AuthSchemeSpec(
            type="github_app",
            label="GitHub App",
            description=(
                "Recommended for production: scopeable, revocable, short-lived "
                "installation tokens minted on demand."
            ),
            secret_fields=(
                SecretFieldSpec(
                    name="app_id", label="App ID or Client ID", placeholder="Iv23… or 12345"
                ),
                SecretFieldSpec(
                    name="private_key",
                    label="Private key (PEM)",
                    placeholder="-----BEGIN RSA PRIVATE KEY-----",
                    multiline=True,
                ),
                SecretFieldSpec(name="installation_id", label="Installation ID"),
            ),
        ),
    ),
    config_fields=(
        ConfigFieldSpec(
            name="base_url",
            label="API base URL",
            required=False,
            placeholder="https://api.github.com",
            help="Override for GitHub Enterprise Server or a test server.",
            kind="text",
            default=DEFAULT_BASE_URL,
        ),
    ),
    webhook_events=WEBHOOK_EVENTS,
    capabilities=GITHUB_CAPABILITIES,
    docs_url="https://docs.github.com/en/rest",
    webhook_secret_mode="generated",
    webhook_signature_algorithm="hmac-sha256",
    webhook_setup_help="Use the generated secret when configuring this webhook in GitHub.",
    supports_webhooks=True,
)
