"""Static declaration of the Supabase connector and its authority planes."""

from jhin_connectors.manifest import (
    AuthSchemeSpec,
    ConfigFieldSpec,
    ConnectorManifest,
    SecretFieldSpec,
)

DEFAULT_BASE_URL = "https://api.supabase.com"

SUPABASE_CAPABILITIES: tuple[str, ...] = (
    "supabase.project.read",
    "supabase.logs.read",
    "supabase.function.list",
    "supabase.function.deploy",
    "supabase.function.delete",
    "supabase.database.read",
    "supabase.database.write",
    "supabase.database.destructive",
)

SUPABASE_MANIFEST = ConnectorManifest(
    connector_type="supabase",
    display_name="Supabase",
    icon="supabase",
    description=(
        "Project logs and Edge Functions through the Management API, plus a separate "
        "least-privilege PostgreSQL connection."
    ),
    auth_schemes=(
        AuthSchemeSpec(
            type="management_token",
            label="Management API token",
            description="A Supabase access token scoped to the selected project.",
            secret_fields=(
                SecretFieldSpec(
                    name="access_token",
                    label="Access token",
                    placeholder="Supabase access token",
                ),
            ),
        ),
        AuthSchemeSpec(
            type="postgres",
            label="PostgreSQL database",
            description="A dedicated low-privilege PostgreSQL login for this project.",
            secret_fields=(
                SecretFieldSpec(
                    name="database_url",
                    label="Database URL",
                    placeholder="postgresql://…",
                ),
            ),
        ),
    ),
    config_fields=(
        ConfigFieldSpec(
            name="project_ref",
            label="Project reference",
            required=True,
            placeholder="abcdefghijklmnopqrst",
            auth_types=("management_token", "postgres"),
        ),
        ConfigFieldSpec(
            name="base_url",
            label="Management API base URL",
            default=DEFAULT_BASE_URL,
            placeholder=DEFAULT_BASE_URL,
            help="Override only for an operator-approved development endpoint.",
            auth_types=("management_token",),
        ),
        ConfigFieldSpec(
            name="allowed_schemas",
            label="Allowed schemas",
            kind="string_list",
            default=["public"],
            auth_types=("postgres",),
        ),
        ConfigFieldSpec(
            name="allow_writes",
            label="Allow database writes",
            kind="boolean",
            default=False,
            auth_types=("postgres",),
        ),
        ConfigFieldSpec(
            name="statement_timeout_ms",
            label="Statement timeout (ms)",
            kind="integer",
            default=5_000,
            minimum=250,
            maximum=30_000,
            auth_types=("postgres",),
        ),
        ConfigFieldSpec(
            name="lock_timeout_ms",
            label="Lock timeout (ms)",
            kind="integer",
            default=1_000,
            minimum=100,
            maximum=5_000,
            auth_types=("postgres",),
        ),
        ConfigFieldSpec(
            name="max_rows",
            label="Maximum rows",
            kind="integer",
            default=200,
            minimum=1,
            maximum=1_000,
            auth_types=("postgres",),
        ),
        ConfigFieldSpec(
            name="max_cell_bytes",
            label="Maximum bytes per cell",
            kind="integer",
            default=4_096,
            minimum=256,
            maximum=8_000,
            auth_types=("postgres",),
        ),
        ConfigFieldSpec(
            name="max_result_bytes",
            label="Maximum result bytes",
            kind="integer",
            default=24_000,
            minimum=4_096,
            maximum=30_000,
            auth_types=("postgres",),
        ),
    ),
    capabilities=SUPABASE_CAPABILITIES,
    docs_url="https://supabase.com/docs/reference/api/introduction",
    webhook_secret_mode="none",
    supports_webhooks=False,
)
