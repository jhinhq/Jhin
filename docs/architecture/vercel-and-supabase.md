# Vercel and Supabase Connectors

Phase 9 adds production Vercel and Supabase integrations without giving an
agent provider credentials or ambient account authority. Every call still
passes through Jhin's normal workspace isolation, capability grants, approval
policy, durable invocation claim, output sanitization, and audit path.

## Authority planes

Each connection row represents one credential and one authority plane.

| Plane | Connection auth type | Credential | What it can reach |
| --- | --- | --- | --- |
| Vercel REST API | `access_token` | Vercel access token | Projects and deployments visible to that token and optional `team_id`. |
| Supabase Management API | `management_token` | Supabase access token | One configured `project_ref`: project metadata, selected logs, and Edge Functions. |
| Supabase PostgreSQL | `postgres` | Dedicated PostgreSQL DSN | Explicitly qualified ordinary tables allowed to the database role and configured schema. |

The two Supabase planes require two independent connection rows. A Management
API token is never accepted by a database tool, a PostgreSQL DSN is never
accepted by a management tool, and neither credential crosses planes. A
Vercel token is similarly resolved only for a Vercel connection in the
executing workspace. Credentials are decrypted at execution time, registered
with the active redactor, and never placed in a model prompt or returned by an
API response.

## Grants and approvals

An allow grant must match the tool capability and every minimum grant key
below. Additional matchable keys can narrow a grant further. An explicit
matching deny wins over an allow.

### Vercel tools

| Tool | Risk | Minimum grant keys | Additional matchable keys | Approval-capable |
| --- | --- | --- | --- | --- |
| `vercel.project.list` | READ | `connection_id` | — | No |
| `vercel.project.read` | READ | `connection_id`, `project_id` | — | No |
| `vercel.deployment.list` | READ | `connection_id`, `project_id` | — | No |
| `vercel.deployment.read` | READ | `connection_id`, `project_id`, `deployment_id` | — | No |
| `vercel.deployment.logs.read` | READ | `connection_id`, `project_id`, `deployment_id` | — | No |
| `vercel.environment_metadata.read` | READ | `connection_id`, `project_id` | — | No |
| `vercel.deployment.preview.create` | ELEVATED | `connection_id`, `project_id`, `environment`, `repository_id` | `ref` | Yes |
| `vercel.deployment.redeploy` | DESTRUCTIVE | `connection_id`, `project_id`, `deployment_id`, `environment` | — | Yes |
| `vercel.deployment.promote` | DESTRUCTIVE | `connection_id`, `project_id`, `deployment_id`, `environment` | — | Yes |
| `vercel.deployment.alias.assign` | DESTRUCTIVE | `connection_id`, `project_id`, `deployment_id`, `environment`, `alias` | — | Yes |

Vercel mutations verify the current project, deployment ownership,
environment, and linked repository before dispatch. Environment inspection
returns variable names and metadata, never values.

### Supabase tools

| Tool | Plane | Risk | Minimum grant keys | Additional matchable keys | Approval-capable |
| --- | --- | --- | --- | --- | --- |
| `supabase.project.read` | Management | READ | `connection_id`, `project_ref` | — | No |
| `supabase.logs.read` | Management | READ | `connection_id`, `project_ref` | `source` | No |
| `supabase.function.list` | Management | READ | `connection_id`, `project_ref` | — | No |
| `supabase.function.deploy` | Management | DESTRUCTIVE | `connection_id`, `project_ref`, `function_slug` | — | Yes |
| `supabase.function.delete` | Management | DESTRUCTIVE | `connection_id`, `project_ref`, `function_slug` | — | Yes |
| `supabase.database.read` | PostgreSQL | READ | `connection_id`, `project_ref`, `schema` | — | No |
| `supabase.database.write` | PostgreSQL | ELEVATED | `connection_id`, `project_ref`, `schema` | — | Yes |
| `supabase.database.destructive` | PostgreSQL | DESTRUCTIVE | `connection_id`, `project_ref`, `schema` | — | Yes |

The PostgreSQL connection defaults to `allow_writes=false`. Setting it to
`true` is only an operator opt-in: the agent still needs an exact mutation
grant, an applicable approval policy, and a live database role with the
necessary table privileges. Phase 9 deliberately exposes no agent DDL tool or
`allow_ddl` switch. Schema changes remain reviewed operator migrations.

For example, a deployment observer can receive only:

```json
{
  "capability": "vercel.deployment.read",
  "effect": "allow",
  "scope": {
    "connection_id": "<vercel-connection-id>",
    "project_id": "prj_customer_portal",
    "deployment_id": "dpl_preview_123"
  }
}
```

A database reader should be pinned independently:

```json
{
  "capability": "supabase.database.read",
  "effect": "allow",
  "scope": {
    "connection_id": "<postgres-connection-id>",
    "project_ref": "abcdefghijklmnopqrst",
    "schema": "agent_data"
  }
}
```

Balanced is the default policy: READ and WRITE run automatically after grant
checks, while ELEVATED and DESTRUCTIVE calls park for approval. Autonomous may
auto-run ELEVATED work such as preview creation, but DESTRUCTIVE calls still
park. Restricted forbids DESTRUCTIVE calls. Only an explicit custom `auto`
rule can make a DESTRUCTIVE tool automatic; no policy preset removes the need
for a matching grant.

An approval is not a frozen authorization snapshot. On resume, the gateway
revalidates the exact workspace, agent, run, task, tool definition, lossless
input, grant set, deny rules, approval policy, connection status, auth type,
public config, and credential fingerprint. Revocation, a new deny, credential
rotation, config drift, connection disablement/deletion, or tool-definition
drift prevents dispatch and produces a stable denial audit.

Each runtime invocation has a deterministic claim key. Jhin commits the claim
before an approval-capable mutation can produce an external effect, so racing
resolvers cannot both dispatch it. A retry of a terminal claim replays the
durable result. If a claimed mutation may have taken effect but its terminal
response cannot be proven, the call becomes `execution_unknown`; Jhin does not
retry the external mutation. An operator must reconcile the provider or
database state manually before deciding what to do next.

## Vercel webhooks

Vercel webhook availability and setup vary by provider plan and account type.
Jhin supports the integration where the selected Vercel plan exposes the
needed deployment webhooks; it does not imply that every plan supports account
webhooks.

Create the webhook in Vercel with the callback shown on the connection:

```text
https://<jhin-host>/api/v1/webhooks/vercel/<public-id>
```

Vercel generates the signing secret. Paste it into the connection's
write-only webhook-secret field; it cannot be read back. Jhin authenticates
the exact request bytes using the bare lowercase digest in
`x-vercel-signature`, computed as HMAC-SHA1 with that secret. Signature
verification happens before JSON parsing.

The accepted provider event allowlist is:

- `deployment.created`
- `deployment.ready`
- `deployment.succeeded`
- `deployment.error`
- `deployment.canceled`
- `deployment.promoted`

`deployment.ready` and `deployment.succeeded` both normalize to
`connector.vercel.deployment.ready`; the other accepted events map to their
corresponding bounded canonical deployment concepts. Unknown or malformed
events produce no canonical event. Provider metadata, environment values,
user/team objects, tokens, and arbitrary dashboard fields are not copied into
the canonical payload.

Webhook ingress accepts at most 1,048,576 bytes. The API enforces the cap while
streaming and rejects an oversized declared or actual body before parsing or
publishing it. A delivery is unique per connection and provider delivery ID;
canonical event IDs are derived deterministically. Provider retries and a
publish-before-commit retry therefore converge on one ingress identity, one
canonical deployment event, and at most one trigger invocation.

## Supabase database role

Use a dedicated login. Do not reuse `postgres`, a Supabase owner/service role,
Jhin's application-database role, or a role that inherits broad authority. A
minimal read-only pattern is:

```sql
CREATE ROLE jhin_agent_reader LOGIN PASSWORD '<generated-password>'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;

GRANT CONNECT ON DATABASE postgres TO jhin_agent_reader;
GRANT USAGE ON SCHEMA agent_data TO jhin_agent_reader;
GRANT SELECT ON agent_data.deployment_status TO jhin_agent_reader;
GRANT SET ON PARAMETER temp_file_limit TO jhin_agent_reader;
```

`agent_data.deployment_status` should be an ordinary, nonpartitioned base
table whose rows and columns contain only data intended for the agent. For a
writer, create a separate login with the same negative role attributes and
grant only the exact `SELECT`, `INSERT`, `UPDATE`, `DELETE`, or `TRUNCATE`
privileges needed on curated tables. Foreign-key peers may also require the
minimum `SELECT`/`MAINTAIN` privilege that Jhin's locked preflight verifies.
Do not grant `pg_read_all_data`, `pg_write_all_data`, schema `CREATE`, database
or schema ownership, table ownership, or membership in a role that has them.

For hosted Supabase, use an explicit TLS DSN:

```text
postgresql://jhin_agent_reader:<url-encoded-password>@db.<project-ref>.supabase.co:5432/postgres?sslmode=verify-full
```

The official direct host is exactly `db.<project_ref>.supabase.co:5432`.
Official pooler hosts must end in `.pooler.supabase.com`, use port 5432, and
have a username ending in `.<project_ref>`. Hosted targets require
`sslmode=require`, `verify-ca`, or `verify-full`; prefer `verify-full`.

Recommended connection settings retain the fail-closed defaults:

```json
{
  "project_ref": "abcdefghijklmnopqrst",
  "allowed_schemas": ["agent_data"],
  "allow_writes": false,
  "statement_timeout_ms": 5000,
  "lock_timeout_ms": 1000,
  "max_rows": 200,
  "max_cell_bytes": 4096,
  "max_result_bytes": 24000
}
```

The accepted bounds are 250–30,000 ms for statement timeout, 100–5,000 ms
for lock timeout, 1–1,000 rows, 256–8,000 bytes per cell, and 4,096–30,000
bytes per result. `max_cell_bytes` may not exceed `max_result_bytes`.

Every physical table reference in submitted SQL must be explicitly qualified
with the requested schema, such as `agent_data.deployment_status`. An
unqualified name is accepted only when it resolves lexically to a CTE in that
query. The transaction forces a `pg_catalog`-only search path, row security,
bounded work/temp memory, no parallel gather or JIT, and verified statement
and lock timeouts. Submitted functions, aggregates, window/table functions,
custom operators, explicit collations, and column/parameter/custom casts are
rejected. Only a narrow literal-to-built-in scalar cast is allowed.

Before executing, the same transaction checks the direct and inherited role
closure, then catalogs, deterministically locks, and rechecks every source,
target, and foreign-key peer. It rejects privileged roles, ownership or
schema-create authority, views/materialized views, partitions, foreign tables,
inheritance, RLS/policies, and unsafe custom types/operator classes, indexes,
foreign keys, or catalog shapes over fixed bounds. For mutation targets it
also rejects unsafe rules/triggers and hidden generated/default/CHECK/
exclusion code. UPDATE, DELETE, and TRUNCATE probe the target up to
`max_rows + 1` before dispatch, so an over-bound destructive target is
rejected with no submitted mutation.

### SQL decision table

| Tool class | Accepted shape | Representative rejections |
| --- | --- | --- |
| READ | One `SELECT`, including read-only CTEs, joins, subqueries, and set operations over explicitly qualified allowed base tables. | `SELECT INTO`, row locks, data-changing CTEs, root `VALUES`, `EXPLAIN`, session/transaction commands, catalog or cross-schema tables, unqualified physical tables, functions, unsupported operators/casts, and multiple statements. |
| WRITE | One bounded `INSERT ... VALUES` into one qualified target, using literals or contiguous `$1..$50` scalar parameters. | `INSERT ... SELECT`, `DEFAULT VALUES`, individual `DEFAULT`, `ON CONFLICT`, `OVERRIDING`, `RETURNING`, every UPDATE/DELETE/TRUNCATE/MERGE, and all DDL. |
| DESTRUCTIVE | One bounded UPDATE using the fixed assignment grammar, one DELETE, or one single-table `TRUNCATE` with default/`CONTINUE IDENTITY` and default/`RESTRICT` behavior. | Source-column assignments, `RETURNING`, MERGE, multi-table/`ONLY`/descendant-star TRUNCATE, `RESTART IDENTITY`, `CASCADE`, INSERT, and all DDL. |
| All | One parser-recognized statement with no semicolon token; physical relations exactly match `schema`; placeholders are contiguous and match the supplied scalar parameters. | Empty/comment-only input, parser fallback, gaps or unsupported placeholders, nested parameter values, unsupported AST nodes, system schemas, and any ambiguous or unprovable shape. |

Read output is positional (`columns` plus rows), so duplicate aliases cannot
overwrite cells. Fixed-width output types are limited to `bool`, `int2`,
`int4`, `int8`, `float4`, `float8`, `date`, `timestamp`, `timestamptz`, and
`uuid`. `text` and `varchar` are accepted only as directly attributable,
unchanged projections of externally stored ordinary-table columns. The trusted
wrapper byte-slices values before transfer, base64-encodes the bounded bytes,
decodes strict UTF-8, and applies row/cell/result limits. Compressed text and
all other output types—including `numeric`, `bpchar`, `json`, `jsonb`, and
`bytea`—are rejected.

These controls bound Jhin's wire copy and returned document; they are not a
claim that arbitrary hostile database contents can never consume PostgreSQL
backend memory. PostgreSQL also provides no safe namespace lock for an
operator concurrently renaming and recreating a schema. Jhin rejects owner or
`CREATE` authority from the connector role, while trusting database operators
not to perform concurrent namespace DDL during a call.

Schema allowlisting does not classify sensitive cells. Least-privilege table
and column grants, plus purpose-built ordinary tables containing only intended
data, are the primary confidentiality boundary. Views are intentionally
rejected in Phase 9 rather than recursively trusting stored view definitions
and owner authority.

## Connection access summary

The admin-only connection detail derives Agent Access from current workspace
agents and grants relevant to the selected connector. It reports each agent's
name, effective tool names, and the exact relevant grant capability, effect,
and scope. An allow is considered for authorization only when it names the
exact connection and includes every tool-required grant key. Applicable broad
or scoped denies use deny precedence.

Incomplete allows and deny rows remain visible with their eligibility reason
instead of being silently presented as authority. If a relevant persisted
scope cannot be modeled safely or the bounded scan cannot produce a reliable
answer, the summary fails closed. The summary deliberately does not load
credentials, public connection config, or agent approval-policy JSON.

This page is diagnostic, not an authorization token. A displayed grant never
bypasses the agent's current approval policy, live connection checks,
tool-specific project/deployment/database validation, or approval-resume
reauthorization.

## Endpoint policy

The official HTTP defaults are exact origins:

- Vercel: `https://api.vercel.com`
- Supabase Management API: `https://api.supabase.com`

An HTTP override is accepted only when its normalized origin appears exactly
in the comma-separated operator setting
`JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS`. Origins may contain no credentials,
path, query, or fragment. HTTP clients do not follow provider redirects; a 3xx
response is a stable failure rather than a chance to escape the approved
origin.

For a self-hosted or development PostgreSQL endpoint, the exact normalized
`host:port` must appear in `JHIN_CONNECTOR_ALLOWED_DB_HOSTS`. The DSN must
contain a password and one database name, and it may contain only an optional
`sslmode` query parameter. A DSN resolving to Jhin's own application database
identity is rejected.

Production supplies neither operator allowlist by default. Consequently
localhost, RFC1918/link-local addresses, cloud metadata endpoints, lookalike
provider domains, and arbitrary self-hosted targets fail closed unless an
operator deliberately adds their exact origin or host/port. Redirect rejection
keeps an approved public endpoint from bouncing a request to one of those
targets.

Supabase log reads use only the closed sources `edge_logs`, `postgres_logs`,
`auth_logs`, `storage_logs`, `realtime_logs`, `function_edge_logs`, and
`function_logs`. Do not configure or grant `logs.all`; Jhin does not use that
broader endpoint.

## Dev fakes

The development overlay provides deterministic local fixtures only:

| Service | Default host port | In-network target | Controls |
| --- | --- | --- | --- |
| Fake Vercel API | 8094 | `http://fake-vercel:8080` | `POST /_reset`, `GET /_state`, scenario/fault/webhook controls used by tests. |
| Fake Supabase Management API | 8095 | `http://fake-supabase:8080` | `POST /_reset`, `GET /_state`, mutation fault controls used by tests. |
| Supabase PostgreSQL fixture | 55433 | `fake-supabase-db:5432` | Database `supabase_fixture`; `jhin_reader` and `jhin_writer` fixture roles. |

The dev overlay allowlists those in-network endpoints explicitly. Its fixture
passwords and admin login are test data, not deployment examples. None of the
three services, their credentials, `supabase_fixture`, or the dev connector
allowlists exists in production `compose.yaml`.

## Verification

Verification date: 2026-08-18.

Fresh Phase 9 acceptance and focused boundary evidence:

- `uv run pytest -m integration tests/integration/test_phase9_exit.py -v` —
  **10 passed in 80.30s**.
- `uv run pytest packages/connectors/tests/vercel -q` — **114 passed**.
- `uv run pytest packages/connectors/tests/supabase/test_sql_policy.py -q` —
  **167 passed**.
- `uv run pytest services/event_worker/tests -q` — **18 passed**.
- `uv run pytest apps/api/tests/test_webhooks_unit.py packages/connectors/tests/vercel/test_webhook.py services/event_worker/tests/test_normalizer.py -q`
  — **61 passed**.
- `uv run pytest apps/api/tests/test_approvals_unit.py -k public -q` —
  **5 passed** for public approval, tool-call, and run-event payload projection.
- A hydrated browser inspection of `/connectors` rendered the Vercel and
  Supabase catalog entries, a live connection detail, Agent Access, and Recent
  Tool Usage. All 7 required strings were present and the forbidden
  secret/sentinel scan returned 0 hits.

The broad closure gates produced these fresh results:

- `uv run ruff check .` — **passed**.
- `uv run ruff format --check .` — **382 files already formatted**.
- `uv run mypy` — **no issues in 274 source files**.
- `uv run pytest -m "not integration"` — **1,414 passed, 132 deselected in
  90.94 seconds**.
- `pnpm --filter jhin-web lint` — **passed**.
- `pnpm --filter jhin-web typecheck` — **passed**.
- `pnpm --filter jhin-web test` — **78 tests across 12 files passed**.
- `pnpm --filter jhin-web exec next build --webpack` — **Next.js 16.3.1
  production build passed**.
- `uv run pytest -m integration tests/integration packages/connectors/tests/supabase/test_database_integration.py -v`
  — **131 passed in 254.27 seconds**.
- `docker compose -p jhin-phase9-production-check -f compose.yaml build --no-cache api agent-worker event-worker workflow-worker web`
  — **all five no-cache production images built successfully**. The final
  settled-tree refresh used the same build definition one target at a time
  after transient registry DNS and parallel host-resource contention.
- `uv run python scripts/assert_phase9_production_compose.py` — **passed**;
  production renders no dev fakes, fixture database credentials, or connector
  allowlist overrides.
- `git diff --check` — **clean**. `git status --short` accounts for all 46
  Task 8 paths in the plan's exact staging inventory; the unrelated untracked
  OrgForge reference remains excluded and untouched.
