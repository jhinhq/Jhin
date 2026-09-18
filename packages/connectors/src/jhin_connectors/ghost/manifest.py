"""Self-hosted Ghost Admin connection declaration."""

from jhin_connectors.manifest import (
    AuthSchemeSpec,
    ConfigFieldSpec,
    ConnectorManifest,
    SecretFieldSpec,
)

GHOST_MANIFEST = ConnectorManifest(
    connector_type="ghost",
    display_name="Ghost",
    icon="ghost",
    description=(
        "Read posts, prepare drafts, and send versioned work "
        "to your designated publishing director."
    ),
    auth_schemes=(
        AuthSchemeSpec(
            type="api_key",
            label="Admin integration key",
            description=(
                "Use an Admin API key from Ghost Settings → Integrations. "
                "The Admin URL may differ from the public website."
            ),
            secret_fields=(SecretFieldSpec(name="admin_key", label="Admin API key"),),
        ),
    ),
    config_fields=(
        ConfigFieldSpec(
            name="admin_url",
            label="Ghost Admin URL",
            required=True,
            help="The actual Ghost site or Admin URL. Jhin never guesses this address.",
        ),
        ConfigFieldSpec(
            name="publisher_agent_id",
            label="Publishing agent ID",
            help=(
                "Only this agent can approve and publish reviewed drafts. "
                "Leave empty to allow drafts only."
            ),
        ),
    ),
    capabilities=(
        "ghost.connection.bind",
        "ghost.post.list",
        "ghost.post.read",
        "ghost.draft.create",
        "ghost.draft.update",
        "ghost.review.request",
        "ghost.review.read",
        "ghost.review.decide",
        "ghost.post.publish",
        "ghost.assignment.create",
        "ghost.assignment.read",
        "ghost.assignment.revise",
        "ghost.assignment.cancel",
        "ghost.assignment.attach_evidence",
        "ghost.archive.sync",
        "ghost.archive.status",
        "ghost.archive.search",
        "ghost.archive.read",
    ),
    docs_url="https://docs.ghost.org/admin-api",
)
