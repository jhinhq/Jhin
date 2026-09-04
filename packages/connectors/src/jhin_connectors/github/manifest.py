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
        # Ordered by what the product wants people to reach for first. A
        # fine-grained token leads because it is the whole setup for the
        # code-work path and the only scheme whose blast radius the operator
        # writes down themselves; the browser sign-ins and the app follow.
        AuthSchemeSpec(
            type="pat",
            label="Personal access token",
            description=(
                "One fine-grained token, scoped to only the repositories agents "
                "may write to. Jhin checks its own allow-list too, but GitHub's "
                "is the one that cannot be argued with. Contents: read and "
                "write, Pull requests: read and write, Metadata: read."
            ),
            secret_fields=(
                SecretFieldSpec(name="token", label="Personal access token", placeholder="ghp_…"),
            ),
        ),
        AuthSchemeSpec(
            type="oauth",
            label="Sign in with GitHub",
            description=(
                "Approve Jhin in your browser. Nothing to paste, and you can "
                "withdraw the access from GitHub at any time."
            ),
            # None: the tokens arrive from the callback and are written
            # straight into the encrypted store. There is no field for a
            # person to fill in, and no credential a person ever sees.
            secret_fields=(),
        ),
        AuthSchemeSpec(
            type="device",
            label="Sign in with a device code",
            description=(
                "For instances GitHub cannot redirect a browser back to: Jhin "
                "shows a short code you enter on github.com."
            ),
            secret_fields=(),
        ),
        AuthSchemeSpec(
            type="github_app",
            label="GitHub App",
            description=(
                "Scopeable and revocable, with short-lived installation tokens "
                "minted on demand. Jhin can create the app for you in one click."
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
