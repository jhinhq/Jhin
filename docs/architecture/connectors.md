# Connector architecture

Connectors are how Jhin agents reach external systems (GitHub, Linear, …)
without ever holding credentials. This document describes the SDK in
`packages/connectors` (`jhin_connectors`), the runtime paths a connector
participates in, and — most importantly — how to contribute a new connector
without touching any other service (plan sections 11, 36.5).

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

**Tool execution.** The agent worker builds its catalog with
`build_default_catalog()` (built-ins + every registered connector's tools).
Every connector tool input carries a `connection_id`; the gateway checks the
grant (which may scope `connection_id`, `repository`, `branch` with glob
patterns) before the executor runs. The executor calls
`resolve_connection(...)`, which loads the connection **inside the caller's
workspace**, refuses disabled connections, decrypts the credential secret with
the worker's master key, and registers every credential value with the
`SecretRedactor` so tokens can never leak into sanitized outputs, errors, or
logs (plan 13.5, 48.1/48.9/48.11). Credentials exist in memory only for the
duration of the call.

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
The trigger engine that reacts to these events arrives in Phase 7.

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

The UI (gallery, connection create form, grant scoping) and the API
(connections CRUD, verify, webhook ingress) are entirely manifest-driven, so
a new connector appears there automatically.

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
