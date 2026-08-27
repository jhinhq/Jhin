# API keys

An API key lets a script, a CI job, or another system act inside one workspace
without a browser session. Keys are **scoped** and **capped by the role of the
person who made them**, so a key is never a way to acquire authority its owner
does not have.

Companion documents: [Roles and permissions](rbac.md) for the ceiling a key
sits under, and [API versioning and compatibility](api-versioning.md) for what
`/api/v1` promises a key holder. The endpoint list itself is not written down
anywhere: signed-in users read it at `/api-docs`, generated from the install's
own OpenAPI document, with the required scope shown against every operation.

## Wire format

```
Authorization: Bearer jhin_<prefix>_<secret>
```

* `jhin_` — a fixed, greppable label. A key that leaks into a log, a repo, or a
  paste is findable with one search, and secret scanners can be taught one
  pattern.
* `<prefix>` — 8 hex characters, stored in clear. Public, unique, and shown in
  the UI so a key can be identified without revealing it.
* `<secret>` — 32 random bytes, URL-safe base64.

Only `sha256(secret)` is stored. Lookup is by prefix (one indexed equality),
then `hmac.compare_digest` against the stored hash.

**Why SHA-256 and not Argon2.** Password hashing is slow on purpose because
passwords are low-entropy and guessable. This secret is 256 bits of CSPRNG
output: there is no dictionary to search and no feasible guess, so a work
factor buys nothing and would add latency to every API call. This is the same
reasoning — and the same helper, `security/tokens.hash_token` — behind session
token storage.

Failed presentations are rate limited on the same decaying-backoff ladder as
logins, keyed by the key's prefix and the source address
(`API_KEY_MAX_ATTEMPTS`, `API_KEY_IP_MAX_ATTEMPTS`).

## Scope taxonomy

The canonical list lives in **one module**,
`packages/domain/src/jhin_domain/scopes.py`. The API validates against it and
serves it at `GET /api/v1/workspaces/{id}/api-keys/scopes`; the web client
renders its scope tree from that response, so the labels a person reads and the
strings the API accepts cannot drift apart.

A scope is `<category>:<action>`. `<category>:*` grants every action in that
category. There is deliberately **no global `*`**: "everything" should be
something you choose category by category.

Each scope declares the minimum role that may hold it:

| Scope | Min role | What it means |
|---|---|---|
| `workspace:read` | viewer | Workspace details, org chart, people directory |
| `workspace:settings` | admin | Rename, timezone, budgets, limits |
| `members:read` | viewer | Members and pending invitations |
| `members:write` | admin | Invite, change roles, revoke, remove |
| `agents:read` | viewer | Agents, avatars, reporting lines |
| `agents:write` | admin | Create, edit, pause, resume, delete |
| `agents:admin` | admin | Capability grants and autonomy policy |
| `teams:read` / `teams:write` | viewer / admin | Teams and their membership |
| `chats:read` | viewer | Conversations and messages |
| `chats:write` | member | Start chats and send messages (starts runs) |
| `tasks:read` | viewer | Tasks, trees, messages |
| `tasks:write` | member | Start, steer, pause, cancel, instruct |
| `runs:read` | viewer | Runs, timelines, tool calls |
| `apps:read` / `apps:write` | admin | Connections and the tools they expose |
| `automations:read` | viewer | Triggers and invocation history |
| `automations:write` | admin | Create, edit, test, enable, delete triggers |
| `skills:read` / `skills:write` | viewer / admin | Skill library |
| `memories:read` | viewer | Curated long-term memory |
| `memories:write` | member | Create, edit, pin, contest |
| `memories:admin` | admin | Approve, reject, forget, de-duplicate |
| `approvals:read` | viewer | Pending approvals |
| `approvals:decide` | member | Approve or reject a paused action |
| `reviews:read` | viewer | Work reviews, policies, work requests |
| `reviews:decide` | member | Submit a verdict; answer work requests |
| `reviews:write` | admin | Review policies |
| `models:read` / `models:write` | admin | Providers, profiles, pricing |
| `spend:read` | viewer | What the workspace has spent on model usage, and its budget |
| `audit:read` | admin | The audit log |
| `api_keys:read` / `api_keys:write` | viewer | Keys and their usage log |

### Endpoints no key can ever reach

Some routes are **sealed**: they have no scope at any level, and a key calling
them gets `403 This endpoint is not available to API keys`. These are the
credential surfaces — workspace secrets (`/secrets`, `/secrets/{id}/rotate`),
connection credential rotation and webhook signing secrets, and the draft
provider verification endpoint that takes a raw provider key in its body. They
are browser-session-only, forever.

One method is sealed on its own: `DELETE /api/v1/workspaces/{workspace_id}`.
The rest of that path is a normal scoped route — `workspace:read` to read it,
`workspace:settings` to rename it and set its budgets — but a scope offered as
"rename and budgets" must not also buy destroying the workspace and everything
in it, so a rule may seal `DELETE` while leaving its other writes alone.
Deleting a workspace stays an owner's browser-session act.

### The one route off the workspace prefix

Everything above hangs off `/api/v1/workspaces/{workspace_id}`, which leaves a
client holding only a key with no first call it can make: `GET /auth/me` needs
a browser session, so the key knows neither who it acts as nor the workspace id
that every other route is keyed by.

`GET /api/v1/auth/identity` is that first call, and it takes either credential
at any scope. A session gets every workspace the user belongs to and a null
`api_key`; a key gets exactly the one workspace it is bound to, plus its own
name, prefix, ceiling, and **effective** scopes. Because the scopes it reports
are already intersected — by the ceiling and by the creator's role today — a
client can grey out what the next call would refuse instead of discovering the
limit through a `403`.

It is the only operation outside the workspace prefix that a key may call, and
the exception is written down twice: `_DUAL_CREDENTIAL_OPERATIONS` in
`jhin_api.openapi` (which is what the published document declares) and the
tests in `apps/api/tests/test_access_control.py`. Widening that set is meant to
be a diff somebody reviews.

## The ceiling rule

> **effective permission = intersection(key scopes, scopes allowed for the key's role ceiling)**

`role_ceiling` is the creator's workspace role frozen at creation time, stored
on the key. A member's key can never carry an admin scope, no matter what was
requested.

This is computed by `jhin_domain.effective_scopes` and nowhere else. It applies
three times over, which is what makes it hard to get wrong:

1. **At creation.** Naming a scope above your role is a `422` that tells you
   which one. A wildcard is *capped* instead of refused — `memories:*` from a
   member means "everything in memories I'm allowed to delegate", so it stores
   `memories:read, memories:write` and not `memories:admin`. The stored row is
   therefore always already truthful about what the key can do.
2. **At authentication.** `effective_scopes(scopes_json, role_ceiling)` is
   applied again when the key is resolved, so a key written by an older or
   buggier code path still cannot exceed its ceiling.
3. **At every request.** The effective role is `min(the creator's role *today*,
   the key's ceiling)`. Demote the creator and their key loses the matching
   power on the next call — no revocation sweep required.

## Enforcement

Scope checking is central, not per-endpoint. Every workspace-scoped route
already resolves through `require_workspace_role`; that dependency looks the
current route up in `apps/api/src/jhin_api/access/route_scopes.py` and refuses
the request when the key's effective scopes do not cover it. Endpoints contain
no scope logic of their own, so an endpoint cannot forget to check.

The lookup is keyed by a *signature*: the route template with the
`/api/v1/workspaces/{workspace_id}` prefix and all path parameters removed, so
`/agents/{agent_id}/grants/{grant_id}` is `("agents", "grants")`. Read methods
take the rule's read scope, everything else the write scope.

It **fails closed**: a route with no entry is unreachable by any key.
`apps/api/tests/test_route_scopes.py` enumerates the live OpenAPI surface and
fails until every workspace route is classified, so an unclassified new route
is caught in CI rather than shipped.

Rejection responses are specific enough to debug and no more:

| Situation | Response |
|---|---|
| Malformed, unknown, revoked, or expired key | `401 Invalid or expired API key` |
| Key belongs to a different workspace | `404 Workspace not found` |
| Route is sealed | `403 This endpoint is not available to API keys` |
| Missing scope | `403 API key is missing the 'x:y' scope` |
| Effective role below the route's floor | `403 Requires workspace role 'admin' or higher` |

### CSRF

Bearer authentication is exempt from CSRF, but **only when no session cookie is
present**. CSRF exists because browsers attach cookies automatically; an
`Authorization` header is never attached automatically, so a purely
bearer-authenticated request has nothing to forge. The `no session cookie`
half is the load-bearing one: if a browser session is also present the request
is still a browser request, and adding an `Authorization` header must not
become a way to skip the check. See `apps/api/src/jhin_api/security/csrf.py`.

## Managing keys

| Endpoint | Who | Notes |
|---|---|---|
| `POST /api/v1/workspaces/{id}/api-keys` | viewer+ (browser session only) | Returns the full key **once**, capped at your role |
| `GET /api/v1/workspaces/{id}/api-keys` | viewer+ | Prefix, name, scopes, expiry, last used, creator — never the key |
| `DELETE /api/v1/workspaces/{id}/api-keys/{key}` | own key, or admin+ for anyone's | Revoke; the row survives so its usage log keeps its subject |
| `GET /api/v1/workspaces/{id}/api-keys/scopes` | viewer+ | The taxonomy, annotated with what your role may grant |
| `GET /api/v1/workspaces/{id}/api-keys/usage` | viewer+ | Paginated; visibility depends on your role |

A key cannot create another key, even holding `api_keys:write`: minting is a
human act, otherwise a leaked key could quietly mint its own long-lived
replacements. A workspace is capped at 100 live keys.

**Expiry** is chosen as an amount plus a unit — minutes, hours, days, or
`never`. `never` is the explicit absence of an expiry, not a very large number,
so "this key does not expire" is a visible decision rather than an accident.

## Usage log

Every API-key request writes one row to `api_key_usage`: timestamp, key,
acting user, method, **route template** (never the raw URL — query parameters
can carry filter values), status code, and a hash of the source address.

A dedicated table rather than `audit_event` rows: this is high-volume request
telemetry with its own retention and its own visibility rules, and mixing it
into the append-only audit log would drown the log it was meant to preserve.

Rows are written for **denied** calls too. The key is stashed on the request
the moment it authenticates, before the role and scope checks run, so a `403`
for a missing scope is recorded just as faithfully as a `200` — which is
exactly what you need when investigating what a key has been trying to do.

Retention is `API_KEY_USAGE_RETENTION_DAYS` (30 by default), enforced by a
sampled prune on write so the table cannot grow without bound on an instance
that never runs maintenance. Reads are paginated with a hard cap of 200.

### Visibility

| Reader | Sees |
|---|---|
| **Owner** | Every call in the workspace |
| **Admin** | Their own key's calls, plus every call by a member's or viewer's key |
| **Member / viewer** | Only their own |

An admin does not see another admin's or the owner's key usage. Admins are
peers, and peer surveillance is not part of the job; the owner is the one
accountable for the whole workspace, so the owner is the one who sees all of
it. The audit log — admin-readable, and where consequential *changes* are
recorded — is unaffected by this rule.

## Data model

`api_key` — `workspace_id`, `name`, `prefix` (unique), `key_hash`,
`created_by_user_id`, `role_ceiling`, `scopes_json`, `expires_at` (nullable =
never), `last_used_at`, `revoked_at`, timestamps.

`api_key_usage` — `workspace_id`, `api_key_id`, `acting_user_id`, `method`,
`path`, `status_code`, `ip_hash`, `created_at`.

Migration `0026`, `packages/db/src/jhin_db/models/access.py`.
