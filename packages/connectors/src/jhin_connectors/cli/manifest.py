"""Static declaration of the CLI connector (plan 11.1, 11.6)."""

from jhin_connectors.manifest import AuthSchemeSpec, ConfigFieldSpec, ConnectorManifest

CLI_CAPABILITIES: tuple[str, ...] = (
    "cli.command.execute",
    "cli.repository.checkout",
    "cli.repository.push",
    "cli.test.run",
    "cli.file.list",
    "cli.file.search",
    "cli.file.read",
    "cli.file.edit",
    "cli.file.write",
)

CLI_MANIFEST = ConnectorManifest(
    connector_type="cli",
    display_name="CLI Sandbox",
    icon="terminal",
    description=(
        "Run commands, tests, and repository work in ephemeral sandbox "
        "containers — never on the host."
    ),
    auth_schemes=(
        AuthSchemeSpec(
            type="none",
            label="No credential",
            description=(
                "Sandbox jobs need no stored credential. Repository jobs "
                "borrow a short-lived token from the GitHub connection "
                "referenced in the connection settings."
            ),
            secret_fields=(),
        ),
    ),
    config_fields=(
        ConfigFieldSpec(
            name="default_image",
            label="Default job image",
            required=False,
            placeholder="jhin-sandbox:latest",
            help="Container image used when a tool call does not name one.",
        ),
        ConfigFieldSpec(
            name="default_network",
            label="Default network policy",
            required=False,
            placeholder="none",
            help='"none" (fully isolated) or "internet" (dedicated sandbox bridge).',
        ),
        ConfigFieldSpec(
            name="git_connection_id",
            label="GitHub connection for repository jobs",
            required=False,
            placeholder="UUID of a GitHub connection",
            help=(
                "cli.repository.checkout and cli.repository.push mint a "
                "short-lived token from this connection. No tool call can "
                "choose a different one."
            ),
        ),
        ConfigFieldSpec(
            name="allowed_repositories",
            label="Repositories this sandbox may use",
            required=False,
            kind="string_list",
            help=(
                "owner/name, fnmatch allowed (octo/*). Empty means this "
                "connection cannot check out or push any repository. Scope the "
                "GitHub token to the same list: GitHub's allow-list is the one "
                "that cannot be argued with."
            ),
        ),
    ),
    webhook_events=(),
    capabilities=CLI_CAPABILITIES,
    docs_url="",
    supports_webhooks=False,
)
