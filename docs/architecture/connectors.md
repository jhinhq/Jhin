# Connector architecture

Connectors are how Jhin agents reach external systems (GitHub, Linear, …)
without ever holding credentials. This document describes the SDK in
`packages/connectors` (`jhin_connectors`), the runtime paths a connector
participates in, and — most importantly — how to contribute a new connector
without touching any other service (plan sections 11, 36.5). Executable
connector ownership lives exclusively in `jhin-tool-worker`; the agent worker
holds model reasoning and sanitized projections but imports no connector
package.

## The pieces

```text
packages/connectors/src/jhin_connectors/
  base.py        Connector ABC + ConnectionHealth, VerifyContext,
                 RawWebhookEvent, NormalizedEvent, WebhookVerificationError
  manifest.py    ConnectorManifest + auth-scheme/config-field specs
  registry.py    ConnectorRegistry, DEFAULT_CONNECTORS, build_default_catalog()
  execution.py   resolve_connection() — runtime credential resolution
  example/       A complete minimal connector (copy this to start yours)
  github/        The real GitHub connector
  testing/       fake_github — in-stack GitHub REST double for tests/dev
```

GitHub signs in, in this order: the browser sign-in (no toggle on GitHub's
side), a sign-in code, app credentials, a personal access token — see
[oauth.md](oauth.md); a user-to-server token reaches only repositories the
app is installed on, and `verify` says when that is nowhere yet.

A **connector** is a class implementing the `Connector` ABC:

| Member | Purpose |
| --- | --- |
| `manifest` | Static metadata: display name, icon, auth schemes (with secret fields), public config fields, webhook events, capability names. Drives the UI gallery and the connection create form — no frontend changes needed per connector. |
| `tools()` | `ToolDefinition`s that register into the shared `jhin_policy` catalog and execute through the `jhin_tools` gateway like any built-in tool. |
| `verify_connection(ctx)` | Live health probe with decrypted credentials; result is persisted on the connection (`status`, `last_verified_at`, `last_error`). |
| `parse_webhook(headers, body, secret)` | Signature verification **first**, then event/delivery-id/payload extraction. Raise `WebhookVerificationError` to reject with 401. |
| `normalize_event(raw)` | Map one raw provider payload to zero or more canonical `connector.<type>.*` events. Unknown events return `[]`, never raise. |

A **connection** (table `connection`, plan 6.9) is one authenticated instance
of a connector inside a workspace: auth type, encrypted credential secret,
optional encrypted webhook secret, public non-secret `config_json` (e.g.
`base_url`), a random `public_id` used in the webhook URL, and health fields.

## Runtime paths

**Tool execution.** At startup, the tool worker is the sole runtime caller of
`build_default_catalog()` (built-ins plus every registered connector's tools).
Before a model step it filters live grants to advertised, dependency-light
schemas on `jhin-tool-queue`; the agent worker receives those schemas but no
executor or credential. The model's complete ordered call set is bound before
any effect, and the tool worker later reloads only one canonical bound call by
run, step, and ordinal.

Every connector tool input carries a `connection_id`; the gateway checks the
grant (which may scope `connection_id`, `repository`, and `branch` with glob
patterns) before the executor runs. The tool-worker executor calls
`resolve_connection(...)`, which loads the connection **inside the caller's
workspace**, refuses disabled connections, decrypts the credential secret with
the tool worker's master key, and registers every credential value with the
`SecretRedactor` so tokens cannot leak into sanitized outputs, errors, or logs
(plan 13.5, 48.1/48.9/48.11). Credentials exist in tool-worker memory only for
the duration of the call. The durable PostgreSQL `ToolCall`/`Approval` claim,
not a worker process or retry, is the effect authority.

**Webhooks.** `POST /api/v1/webhooks/{connector_type}/{public_id}` has no
session auth (plan 19). The API looks up the connection by `public_id`,
decrypts its webhook secret, and calls `parse_webhook` — HMAC verification
happens before the body is even JSON-parsed (plan 48.5). Verified deliveries
are recorded in `webhook_delivery` (unique per connection + delivery id, so
provider retries never duplicate events — plan 48.6) and published raw to the
NATS `INGRESS` stream (`jhin.v1.<ws>.ingress.<type>.<event>`). The event
worker's `IngressNormalizer` then calls `normalize_event` and publishes
canonical events (`jhin.v1.<ws>.connector.<type>.…`) to the `EVENTS` stream
with deterministic derived event ids, keeping the whole pipeline idempotent.
The trigger engine (see the Automations page) reacts to these canonical events.

## Adding a connector

Everything lives under one new package directory; no other service changes.

1. Copy `packages/connectors/src/jhin_connectors/example/` to
   `packages/connectors/src/jhin_connectors/<yourtype>/`. It contains the
   canonical file split:
   - `manifest.py` — `ConnectorManifest` (auth schemes, secret fields, config
     fields, webhook events, capabilities)
   - `schemas.py` — Pydantic input/output models; every tool input includes
     `connection_id` and whatever scope fields grants should match
   - `tools.py` — executors + `ToolDefinition`s with honest `RiskLevel`s and
     `scope_keys`; call `resolve_connection` for credentials, never cache them
   - `webhook.py` — signature verification + `normalize` (pure functions,
     easy to unit test with fixture payloads)
   - `connector.py` — the `Connector` subclass wiring the above together
2. Register it: add one entry to `DEFAULT_CONNECTORS` in
   `jhin_connectors/registry.py` (a lazy import, so optional dependencies
   stay optional).
3. Add tests under `packages/connectors/tests/<yourtype>/`. If the connector
   talks to an HTTP API, add a fake server under `jhin_connectors/testing/`
   and a dev-overlay compose service so integration tests need no real
   credentials (see `fake-github` in `compose.dev.yaml`).
4. Capability names follow `<type>.<resource>.<action>`
   (e.g. `github.pull_request.create`). Choose risk levels conservatively:
   destructive or hard-to-reverse actions are `elevated` and should set
   `supports_approval=True`.
5. Set `required_grant_scope_keys` on any tool that must never be authorized by
   an unscoped or wildcard grant. `cli.repository.checkout` and
   `cli.repository.push` require `connection_id` and `repository`, so no
   `cli.*` grant can reach a repository by accident.
6. Override `tool_validators()` when authorization depends on connection state
   a grant scope cannot express. It returns tool name → `ToolValidator`, and
   the gateway runs the validator after grants and rules at all three
   authorization points — policy decision, approval resume, and execution bind
   — so a narrowed connection invalidates an approval that is already parked.
   The CLI connector's per-connection repository allow-list is the shipped
   example. Connectors whose tools are discovered per connection cannot carry
   one: `ToolCatalog.for_workspace` registers dynamic-source tools without a
   validator, so their enforcement must live in scope keys.

Order `auth_schemes` by what an operator should reach for first — GitHub leads
with the fine-grained PAT, because it is the whole setup for the code-work path
and the only scheme whose blast radius the operator writes down themselves.
Removing a scheme later is a data migration, not a deletion:
`public_connection_config` re-runs `normalize_config` against the row's stored
`auth_type` and fails closed to `{}`.

The UI (gallery, connection create form, grant scoping) and the API
(connections CRUD, verify, webhook ingress) are entirely manifest-driven, so
a new connector appears there automatically.

Everything a person can do with a connection lives on the **Apps** page
(`apps/web/app/(app)/apps/page.tsx`, route `/apps`): the curated library and
the connected list at the top level, the service-type gallery behind a
"Connect by service type instead" disclosure, and per-connection verify,
credential rotation, webhook setup, discovered tools with risk overrides,
agent access, and enable/disable/delete in the connection drawer
(`apps/web/components/connection-detail.tsx`), whose operational controls sit
behind an "Advanced settings" disclosure. The former `/connectors` route is a
permanent redirect to `/apps`.

## MCP connections

The `mcp` connector (`jhin_connectors/mcp/`) is the one connector whose
tools are not static: it connects to any remote Model Context Protocol
server over Streamable HTTP (SSE fallback), discovers the server's tools at
verification time, and registers them **per connection** as
`mcp.<server_slug>.<tool>`. Three SDK hooks exist for this and default to
no-ops for every other connector:

| Hook | Purpose |
| --- | --- |
| `Connector.refresh_discovery(ctx)` | Called after a successful `verify_connection` (and by `GET …/connections/{id}/tools`); returns keys to merge into `config_json` (bounded, display-safe, never secrets). |
| `Connector.connection_tool_definitions(config)` | Definitions as seen through one stored connection; the access summary and the per-connection Tools tab use it. Pure, no I/O. |
| `ToolCatalog.add_dynamic_source(source)` / `for_workspace(session, workspace_id)` | `build_default_catalog()` registers `McpToolSource`; the tool worker materializes a per-workspace catalog before advertising or executing. |

Risk is derived from the server's annotations (`readOnlyHint` → read,
`destructiveHint` → destructive, otherwise write), admins can override it
per tool, and the executor refuses a tool whose live annotations now report
a higher risk than the reviewed one. Outputs are text-only projections with
binary blocks stripped and hard size caps. MCP connections have **no
webhooks**. The full security model — URL policy, discovery bounds,
approval binding, prompt-injection posture — is in
[mcp.md](mcp.md). The curated Apps library (`jhin_connectors/catalog.py`,
`catalog.json`, `GET /api/v1/connectors/catalog`) maps well-known apps to
either a native connector or an MCP endpoint; entries may pre-fill
non-secret connection config (`connector_config`).

## Web access

The `web` connector (`jhin_connectors/web/`) gives agents deny-by-default
internet access: `web.search` through a workspace-supplied Tavily/Brave/Exa
API key and `web.fetch` for bounded readable-text retrieval of public
pages, with domain scoping in grants and connections. Model-native provider
search is the separate opt-in path 2. Both are described in
[web.md](web.md).

## Security invariants (non-negotiable)

- Plaintext credentials are accepted exactly once (connection create /
  rotate), stored via `jhin_secrets` envelope encryption, and never returned
  by any endpoint.
- Webhook secrets are generated server-side, shown once in the create
  response, and stored encrypted.
- Signature verification precedes all payload processing; failures are
  audited (`webhook.rejected`) and answered with 401.
- Tools are deny-by-default: no grant → `no_grant`; grant scope mismatch
  (connection/repository/branch) → `scope_mismatch`. Both are persisted as
  denied `tool_call` rows and audited.
- Workspace isolation: `resolve_connection` filters by the executing
  workspace id; a connection id from another workspace behaves as not found.
- Worker isolation: connector executors, resolved credentials, sandbox runner
  endpoint/token, and executable catalogs stay out of the agent-worker
  distribution. Agent-side projections consume only durable IDs and sanitized
  rows after the tool-worker transaction commits.
