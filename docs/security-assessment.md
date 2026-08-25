# Security assessment — Jhin as deployed

**Date:** 2026-08-25 · **Scope:** `main`-line source at the time of writing,
running under the source compose stack (`compose.yaml` + `compose.dev.yaml` +
`compose.desktop.yaml`) and the release stack (`deploy/compose.release.yaml`).

Jhin is self-hosted software that holds a company's credentials for external
systems and executes model-authored code. A compromised instance is a
compromised company. This document records what was examined, what was found,
what was changed, and what a self-hoster still has to do themselves.

---

## Methodology

Source review, not black-box scanning. Five areas were read end to end and
every claim below was confirmed against the code before being written down;
anything that could not be confirmed is labelled **unverified** and is not
counted as a finding.

| Area | What was read |
|---|---|
| Authentication | `apps/api/src/jhin_api/auth/{service,router,schemas}.py`, `security/{passwords,tokens,csrf,rate_limit}.py`, `deps.py`, `settings.py` |
| Cookie / transport | cookie flags and their `COOKIE_SECURE` wiring across `compose.yaml`, `compose.dev.yaml`, `deploy/compose.release.yaml`, `.env.example`; CORS and middleware in `main.py`; `apps/web/next.config.ts` |
| Authorization | every `apps/api/src/jhin_api/*/router.py` (21 routers, ~120 routes) traced to the service query behind it, checking for a `workspace_id` filter, CSRF coverage, and role choice |
| Secrets | `packages/secrets/` (envelope crypto, master key, redaction), the closed log-event registry in `packages/observability/`, every response schema that touches credential material, every `SecretStore.reveal` caller |
| Agent / tool surface | `validate_public_http_url` and every outbound-request path, untrusted-output labelling in `packages/agents/`, the capability registry in `packages/policy/`, `services/sandbox_runner/`, archive handling in `packages/skills/` and `packages/media/` |

Verification techniques used:

- **Executed the policy function** against 40+ SSRF candidate URLs and compared
  its verdict against what the host resolver actually does with the same
  string (this is how the packed-IPv4 bypass was confirmed rather than
  guessed).
- **Dependency audit**: `uv run pip-audit` — *no known vulnerabilities*;
  `pnpm audit` — *0 vulnerabilities across 546 dependencies*. No pinned
  version in either lockfile is currently known-vulnerable.
- **Live verification** against the running stack in the `QA Fresh` workspace:
  header presence, cookie flags, CSRF binding from a real browser (bound token,
  forged token, missing header, stale-cookie recovery), cross-workspace 404,
  negative pagination, login, chat, and a full agent run.

---

## Findings

Severity is impact on a self-hosted deployment holding real credentials.
"Fixed" means the change is in this branch with a regression test.

### Authentication and session management

| # | Sev | Component | Finding | Status |
|---|---|---|---|---|
| A1 | **High** | `security/rate_limit.py` | Lockout was keyed on the **pair** `email\|ip`, so it was neither per-account nor per-IP. Rotating source addresses defeated the account limit entirely (10 guesses per address, unbounded addresses); spraying many accounts from one address defeated the IP limit. | **Fixed** |
| A2 | **High** | `deps.py` | `client_ip` read `request.client.host` with no forwarded-header handling. Behind the Next.js rewrite proxy — the only supported topology — every request appears to come from the web container, so the per-IP dimension was inert, and any per-IP block would have locked out *every* user at once. | **Fixed** |
| A3 | Medium | `security/rate_limit.py` | Fixed-window counter with an unbounded `dict`. Guessing against endlessly many distinct emails grew the table without limit (memory exhaustion), and a hard window meant a sustained attack could hold an account blocked indefinitely with no unlock path. | **Fixed** |
| A4 | Medium | `deps.py` | Sessions had an absolute expiry only. No idle expiry, so an abandoned browser tab stayed a valid credential for the full 7 days. `last_used_at` was recorded but never enforced. | **Fixed** |
| A5 | Medium | `deps.py` | `ip_hash` and `user_agent` were stored on every session row and never checked. A copied cookie replayed from a completely different client was indistinguishable from the real one. | **Fixed** |
| A6 | Medium | `auth/` | No "log out everywhere", and no password-change endpoint at all — therefore no way to invalidate sessions after a suspected compromise, which is the single most important recovery action a user has. | **Fixed** |
| A7 | Medium | `auth/schemas.py` | Password minimum was 10 characters with no other policy: `password12` and `1234567890` were both accepted for the owner account. | **Fixed** |
| A8 | Low | `security/passwords.py` | No `check_needs_rehash`, so stored hashes could never be upgraded if argon2-cffi raised its defaults. | **Fixed** |
| A9 | — | `auth/service.py` | **Not a finding.** Session fixation was already handled: every authentication mints a fresh token, so a planted cookie is never the one the victim ends up holding. Now covered by a test so it stays that way. |  Verified |
| A10 | — | `security/passwords.py` | **Not a finding.** Argon2id parameters were verified against the installed library: `t=3, m=64 MiB, p=4`, 32-byte tag, 16-byte salt — well above the OWASP 2024 floor (`m=19 MiB, t=2, p=1`). Kept, now with a test that fails on a silent downgrade. | Verified |
| A11 | — | `auth/service.py` | **Not a finding.** Timing-safe user lookup was already correct: a dummy Argon2 hash is verified when the email is unknown, so response timing does not reveal account existence. Now covered by a test. | Verified |

### Cookies, transport, and headers

| # | Sev | Component | Finding | Status |
|---|---|---|---|---|
| T1 | **High** | `main.py`, `next.config.ts` | **No security headers anywhere.** No CSP, HSTS, `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`, or `Permissions-Policy` on either the API or the web app. The whole UI was framable, and API JSON was sniffable. | **Fixed** |
| T2 | **High** | `compose.yaml`, `settings.py` | The base compose stack defaults to `APP_ENV=production` but never passed `COOKIE_SECURE` through, and the setting defaults to `false`. A self-hoster putting TLS in front of the documented stack got session and CSRF cookies served **without the `Secure` flag**, silently. | **Fixed** |
| T3 | Medium | `security/csrf.py` | The CSRF token was a fresh random value with **no binding to the session**. An attacker able to write cookies for the app's host (a hostile sibling subdomain, or a network position on a plaintext deployment) could plant a value they knew and satisfy the double-submit check. The custom-header requirement was the only real barrier. | **Fixed** |
| T4 | Medium | `main.py` | Interactive API docs (`/docs`, `/redoc`, `/openapi.json`) were served unauthenticated in every environment, mapping the entire API surface for anyone who could reach the API. | **Fixed** |
| T5 | Low | `auth/router.py` | `delete_cookie` was called without the flags the cookies were set with. Browsers match on attributes, so logout could leave the cookie in place on some configurations. | **Fixed** |
| T6 | Low | API responses | No `Cache-Control` on authenticated JSON, so a shared cache in front of the API could retain another user's payload. | **Fixed** |
| T7 | — | CORS | **Not a finding as deployed.** `allow_origins=[settings.app_url]` with credentials, and browser traffic goes same-origin through the Next.js rewrite, so CORS is not load-bearing. Noted: setting `APP_URL=*` would make Starlette echo arbitrary origins with credentials — a misconfiguration, not a bug. | Accepted |
| T8 | — | `SameSite` | **Not a finding.** `SameSite=Lax` on both cookies is correct for a same-origin app, and `Lax` is what blocks the cross-site POST that CSRF depends on. Now configurable, and `SameSite=None` without `Secure` is rejected at startup. | Verified |

### Authorization

Every endpoint that takes a resource id was traced to the query behind it.
**No exploitable IDOR was found** — workspace scoping is genuinely applied
throughout, nested ids are re-checked against the parent, body-supplied
foreign ids are validated, and the 404-for-non-members / 403-for-wrong-role
discipline holds. The findings below are the exceptions.

| # | Sev | Component | Finding | Status |
|---|---|---|---|---|
| Z1 | Medium | `models/service.py` | Provider and profile deletion selected the `Workspace` row to mutate by **profile id alone**, with no workspace filter — a *write* reachable across the boundary — and named pinned agents in the 409 message from an unfiltered `Agent` query. Not exploitable through the API today (the profile id is proven in-workspace first), but the filter was genuinely absent. | **Fixed** |
| Z2 | Medium | `models/router.py` | `GET /model-providers/{id}/balance` was `ViewerCtx` while it decrypts the provider credential and makes a live authenticated billing call — the adjacent `/models` route doing the same class of thing was `AdminCtx`. | **Fixed** |
| Z3 | Low | `tasks/`, `skills/`, `triggers/`, `audit/`, `approvals/`, `conversations/`, `coordination/` | List endpoints clamped only the *upper* bound of `limit`, and `offset` not at all. `?limit=-1` reached Postgres as `LIMIT -1` and produced an unhandled 500 on seven services. | **Fixed** |
| Z4 | Low | `tasks/service.py` | Timeline endpoints (`/tasks/{id}/timeline`, `/runs/{id}/timeline`, `/runs/{id}/tool-calls`, task messages, task runs) fetched with **no `LIMIT` at all** — a long-running agent loop returns its entire event history in one response. | **Fixed** |
| Z5 | — | CSRF coverage | **Verified complete.** Every workspace-scoped mutating router carries `csrf_protect`; GET-only routers correctly omit it. | Verified |
| Z6 | Low | `POST /auth/login` | Login CSRF: no CSRF token on login, so an attacker could in principle force a victim's browser to sign into an attacker-controlled account. **Accepted** — not reachable in practice: the endpoint requires a JSON body, which an HTML form cannot produce, and a cross-site `fetch` with `content-type: application/json` requires a preflight the API refuses. | Accepted |
| Z7 | — | `POST /webhooks/...` | **Verified correct.** The one public write endpoint: unguessable `public_id`, mandatory HMAC verification *before* any parsing or state change, audited rejection, delivery-id dedupe, 1 MiB cap checked on both `Content-Length` and streamed chunks. | Verified |
| Z8 | Low | `approvals/router.py` | Approvals gate high-risk agent tool calls and can be granted by `MemberCtx`. Documented as intentional in the router. **Deferred** — a product decision about who may approve agent actions, not a scoping bug; flagged for the owner. | Deferred |
| Z9 | Low | `skills/service.py` | `install_from_browse` enables installed skills immediately, justified in its docstring by "every source here is a curated, hardcoded public library" — but the gate it uses also admits admin-added custom sources, so an admin can install *enabled* skills from an arbitrary repo, bypassing the `enabled=false` review queue the raw import path enforces. **Deferred**: admin-only, and correcting it is a product decision about the skills review flow. | Deferred |

### Secrets

The secret subsystem is the strongest part of this codebase and most of what
was examined needed no change.

**Verified correct, no action:** AES-256-GCM envelope encryption with a fresh
random DEK and a fresh 96-bit nonce per encryption (no nonce reuse is
possible); master key length enforced at exactly 32 bytes; HMAC-SHA256 secret
fingerprints rather than bare hashes, so a database dump cannot be brute-forced
offline; plaintext never persisted; **no API endpoint returns a decrypted
secret** — `SecretOut` has no ciphertext, DEK, fingerprint, or value field, and
`value` is write-only; connection config is re-derived through a fail-closed
serializer rather than echoing the stored JSON; the one-time webhook secret
display is the correct pattern. The log pipeline is stronger than a redaction
layer: `filter_log_event` is a **closed allowlist** with no free-text field
kind, so an arbitrary credential string cannot be emitted at all, and an
architecture test asserts the redactor is wired at all six service entry
points.

| # | Sev | Component | Finding | Status |
|---|---|---|---|---|
| S1 | Medium | `secrets/router.py` (via FastAPI default) | **A 422 echoed the submitted secret back to the client.** There was no `RequestValidationError` handler, so Pydantic's `input` key was serialised into the response — submit an over-long value to `POST /secrets` and the API key or database password came back in the error body, where it lands in the browser console, proxy logs, and error trackers. | **Fixed** |
| S2 | Low | `models/service.py` | An API key supplied *inline* to `verify-draft` never passed through `SecretStore`, so nothing registered it with the redactor, while the failure path quotes up to 500 characters of the upstream provider body (some providers echo part of the key on an auth failure). One branch returned `str(exc)` with no redaction at all. | **Fixed** |
| S3 | Medium | `packages/secrets/crypto.py` | No AAD binding: ciphertexts are not cryptographically bound to `workspace_id`/`secret_id`, so an attacker with **database write** could splice workspace A's credential into workspace B's row and decryption would succeed silently. **Deferred** — requires a schema migration and backfill of existing rows; the threat model already assumes DB write is a full compromise. Recommended as the next crypto change. | Deferred |
| S4 | Medium | `packages/secrets/crypto.py` | Master-key rotation is not implemented: `CURRENT_KEY_VERSION` is a constant, `load_master_key` never reads a version, and `decrypt` hard-fails on a mismatch. The `key_version` column is schema-only. **Deferred** — a rotation runbook is planned separately; per-secret rotation (`SecretStore.rotate`) does work. | Deferred |
| S5 | Low | `packages/secrets/crypto.py` | `load_master_key` does not check the key file's permissions. **Accepted, not fixed**: Docker mounts secrets `0444` by design, so a fail-closed `0600` check would break every supported deployment. `scripts/generate_master_key.py` already creates it `0600`; operator responsibility, see Residual risk. |  Accepted |
| S6 | Low–Med | `connections/service.py` | The process-global, cross-workspace redactor is applied to provider output that is then returned to the caller, giving a workspace admin an exact-value confirmation oracle for secrets belonging to *other* workspaces in the same process. **Deferred** — the fix is a per-workspace redactor (the sandbox runner already does this correctly per job); it is a plumbing change across the connection verify path. | Deferred |
| S7 | Low–Med | `packages/secrets/redaction.py` | The redactor registry never evicts and re-sorts its entries under a lock on every call — unbounded growth and O(N log N) per log record in a long-lived worker. **Deferred** — performance/DoS, no confidentiality impact. | Deferred |
| S8 | Low | `connections/service.py` | `GET /connections/{id}/metadata` decrypts a credential and makes an authenticated provider call with **no audit record**, unlike its siblings `verify_connection` and `list_connection_tools`. **Deferred** — audit completeness, admin-only. | Deferred |
| S9 | — | `health/service.py` | Readiness probes return `f"{type(exc).__name__}: {exc}"` truncated to 300 chars, and the DSNs involved embed passwords. **Unverified** — I could not confirm that any asyncpg or nats-py exception actually embeds the URL, so this is recorded as a shape worth watching rather than a finding. | Unverified |

### Agent and tool attack surface

| # | Sev | Component | Finding | Status |
|---|---|---|---|---|
| G1 | **High** | `connectors/endpoints.py` | **SSRF bypass, confirmed by execution.** Alternate IPv4 literal formats — `2130706433`, `0x7f000001`, `127.1`, `010.0.0.1`, `0300.0250.0.1` — are rejected by `ipaddress.ip_address`, so they fell through to the *hostname* branch, passed the label regex, and were classified as public. `getaddrinfo` resolves all of them to loopback or RFC1918. Reachable with an agent-supplied URL through `web.fetch`. | **Fixed** |
| G2 | **High** | `connectors/endpoints.py` | **No DNS resolution at all** — validation was purely lexical, so any registered public *name* pointing at `169.254.169.254` or an RFC1918 host passed. Confirmed: `https://internal.corp.example/admin` was allowed. | **Fixed** (validation-time resolution; see residual risk R6 for rebinding) |
| G3 | Low | `connectors/endpoints.py` | `is_global` alone classifies multicast (`224.0.0.0/4`, `ff02::/16`) as public, contradicting the function's own docstring. | **Fixed** |
| G4 | Medium | `packages/skills/bundle.py` | Zip decompression had **no member count cap and no aggregate output budget**. The 5 MB compressed cap says nothing about output: ~1000:1 deflate on repetitive data means a compliant archive could materialise gigabytes in the API process before any per-skill limit ran. Admin-triggered, not unauthenticated. | **Fixed** |
| G5 | Medium | `main.py` | **No global request body limit and no reverse proxy.** Bare uvicorn with no `--limit-*` flags and no nginx/Caddy anywhere in `deploy/`. Per-route limits exist where someone thought about it (webhooks 1 MiB, connection config 64 KiB, media 8 MiB, skills 5 MiB); every other JSON endpoint buffered an arbitrarily large body before Pydantic saw a `max_length`. | **Fixed** |
| G6 | Medium | `packages/models/` | Model provider `base_url` is not validated at all and the API key is posted to it — an admin-only SSRF that also fires on `verify-draft` before persistence. **Deferred**: admin-only, and the dev/QA stack points providers at fake in-network origins, so tightening it needs an allowlist design that does not break the supported dev topology. | Deferred |
| G7 | Medium | `packages/tools/cli/tools.py` | `cli.command.execute` declares scope keys but no `required_grant_scope_keys`, so an unscoped or `cli.*` grant authorizes an arbitrary command, an arbitrary container image, and internet egress at `WRITE` risk (auto-approved by default). **Deferred** — the fix is a one-line registry change but it changes grant semantics for existing installs; flagged to the owner as the highest-value follow-up in this area. | Deferred |
| G8 | Medium | `packages/agents/`, `packages/memory/` | Prompt injection can be *laundered into the system prompt* two ways: agent-private and team memory auto-activates without review and is appended to the system prompt; and `organization.update_agent_profile` writes `role_title`/`description` into **other agents'** prompts at `WRITE` risk (no approval by default). **Deferred** — architectural, and partially mitigated (credential screening on memory, an explicit "recalled information, not instructions" caveat, length bounds). | Deferred |
| G9 | Medium | `connectors/mcp/tools.py` | A hostile MCP server's self-reported tool descriptions become model-visible tool metadata **without** the `UNTRUSTED TOOL OUTPUT` framing applied to tool *results* — classic tool poisoning. Bounded to 1000 chars / 200 tools, and discovery only runs on admin verify paths. **Deferred**. | Deferred |
| G10 | — | Untrusted output labelling | **Verified present and consistent.** `UNTRUSTED_LABEL` is applied unconditionally to every tool-result turn, each connector embeds its own "treat as data, never as instructions" notice, and the platform preamble states the rule as non-overridable. It is a label, not structural separation — see residual risk. | Verified |
| G11 | — | Privilege escalation | **Verified: an agent cannot escalate.** `FORBIDDEN_CAPABILITY_PREFIXES` fails tool registration under `agent.grant`, `agent.policy`, `workspace.member`, `capability`, `policy`, `approval`, `secret`, `auth` at import time. No agent-callable tool grants capabilities, edits policy, reads secrets, or self-approves. Grants are reloaded live at every gateway resume point; new agents are created with zero grants; explicit deny beats allow; deny-by-default when no grant matches. | Verified |
| G12 | — | Sandbox | **Verified strong.** Fresh container per job, force-removed; `User: 1000:1000`; `CapDrop: ALL`; `no-new-privileges`; read-only rootfs; memory and swap both capped; PID and CPU limits; wall-clock kill; network `none` or a dedicated bridge with joining the runner/engine networks rejected; **no host bind mounts and no Docker socket**; `DOCKER_*` env and anything containing a socket path stripped from jobs; output redacted per job and byte-capped; orphan reaping at startup. Runner auth is constant-time and fails closed on empty config. Docker authority validation refuses uid 0 and has no default mode. | Verified |
| G13 | Low | `services/sandbox_runner/rootless_transport.py` | The rootless Docker-API proxy binds `0.0.0.0:2375` with no authentication. Contained: the `engine` network is `internal: true`, only two services attach, job containers never do. **Accepted** — one misconfiguration away from host compromise, so it is called out in Residual risk rather than treated as safe. | Accepted |
| G14 | Low | Sandbox egress | Jobs with `network_policy="internet"` have unrestricted outbound access and never pass through the URL policy — cloud metadata is reachable from agent-authored code. **Deferred**, documented in Residual risk. | Deferred |
| G15 | — | Redirects | **Verified correct.** `web.fetch` re-validates *every* redirect hop and additionally pins to the same origin; every other outbound client sets `follow_redirects=False` without exception. | Verified |
| G16 | — | Media / zip slip | **Verified correct.** Skill archives are never written to disk (entries go to memory then Postgres), absolute and `..` paths are dropped, per-file paths are regex-validated. Image import caps bytes, dimensions and pixels, and explicitly catches `DecompressionBombError`. | Verified |

### Dependencies

`uv run pip-audit`: **no known vulnerabilities**. `pnpm audit`: **0
vulnerabilities across 546 dependencies**. No action.

---

## What changed

### New security primitives

- **`security/rate_limit.py` — rewritten.** Two independent buckets (per
  account, per source address) with an exponentially **decaying** failure
  score instead of a fixed window, progressive backoff, and a hard cap on
  block length. Three properties together guarantee a victim can never be
  locked out permanently: the score halves every `LOGIN_WINDOW_SECONDS` with
  no operator action; the block is clamped at
  `LOGIN_ACCOUNT_MAX_BLOCK_SECONDS` (15 min); and **failures from an address
  that is already blocked stop counting against the account**, so an
  attacker's address trips the IP bucket and then stops poisoning the victim's
  account. The table is pruned and hard-capped at 20 000 keys.
  Unlock path: waiting. There is deliberately no admin unlock to build.
- **`security/tokens.py` — CSRF tokens are now session-bound.** The token is
  `HMAC-SHA256(key=session_token, "jhin-csrf-binding-v1")`. Stateless, needs no
  new configuration, rotates automatically with the session, and reading the
  JavaScript-readable cookie reveals nothing about the HttpOnly session token.
  `csrf_protect` now requires both the double-submit match *and* the binding.
- **`security/headers.py` — new.** `SecurityHeadersMiddleware` stamps
  `X-Content-Type-Options`, `X-Frame-Options: DENY`, `Referrer-Policy`,
  `Permissions-Policy`, `Cross-Origin-Opener-Policy`,
  `Cross-Origin-Resource-Policy`, a strict
  `default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'`
  CSP (the API only serves JSON and media), and `Cache-Control: no-store`
  unless the route set its own. HSTS only when `APP_ENV` is staging/production
  **and** `COOKIE_SECURE` is true.
- **`security/limits.py` — new.** Global 16 MiB body cap, above every
  legitimate upload so per-route errors stay specific. Checks the declared
  `Content-Length` up front *and* counts streamed chunks, so a chunked request
  that omits or lies about its length is still cut off.
- **`security/validation.py` — new.** Replaces FastAPI's default validation
  handler with one that keeps `type`/`loc`/`msg` and drops Pydantic's `input`
  key, so a rejected secret is never echoed back.
- **`security/common_passwords.py` — new.** A small embedded most-guessed list,
  including padded variants (`password1234`) that would otherwise clear a
  12-character minimum.

### Authentication

- Password policy: 12-character minimum, common-password check, and a refusal
  to embed the account's email. No composition rules — they trade real entropy
  for predictable substitutions (NIST SP 800-63B).
- Argon2 parameters kept (already above the OWASP floor) plus
  `check_needs_rehash`, so a stored hash is upgraded for free at next login.
- Session hardening: idle expiry (`SESSION_IDLE_TIMEOUT_HOURS`, default 72h)
  alongside the existing absolute expiry; an idle or client-mismatched session
  is **revoked**, not merely refused. User-Agent binding
  (`SESSION_BIND_USER_AGENT`, default on) catches a cookie replayed from a
  different client; the address is deliberately *not* part of the binding so
  mobile roaming does not log people out.
- `POST /auth/logout-all` — sign out of every browser everywhere.
- `POST /auth/password` — requires the current password, enforces the policy,
  revokes **every** session, and re-seats the caller in a fresh one.
- `GET /auth/me` re-issues the session-bound CSRF cookie, and the web client
  retries a 403 once after refreshing it, so a stale cookie is a hiccup rather
  than a dead end (logout is itself CSRF-protected, so without this a stale
  cookie would be unrecoverable).
- `client_ip` honours `X-Forwarded-For`, but **only** when the immediate peer
  is inside `TRUSTED_PROXY_CIDRS`, walking back to the first untrusted hop. An
  untrusted client cannot forge its own address.

### Fail-loud configuration

`Settings` now refuses to start rather than serving a production deployment
insecurely:

- `APP_ENV` staging/production + `https://` `APP_URL` + `COOKIE_SECURE=false`
  → **refuses to start**, naming the variable to set.
- `APP_ENV` staging/production + plaintext `APP_URL` on a non-loopback host
  → **refuses to start**. `http://localhost` still boots, so the documented
  single-machine quick start is unaffected.
- `SESSION_COOKIE_SAMESITE=none` without `COOKIE_SECURE` → refuses.
- Idle timeout longer than the absolute lifetime → refuses (it would never
  fire, and an operator would believe they had configured one).

`COOKIE_SECURE` and `TRUSTED_PROXY_CIDRS` are now passed through in
`compose.yaml` and `deploy/compose.release.yaml` and documented in both
`.env` examples.

### Frontend

`apps/web/next.config.ts` sets CSP, `X-Content-Type-Options`,
`X-Frame-Options: DENY`, `Referrer-Policy`, `Permissions-Policy`,
`Cross-Origin-Opener-Policy`, HSTS in production builds, and
`poweredByHeader: false`. The CSP is `default-src 'self'` with `object-src`
and `frame-ancestors` at `'none'`; fonts are self-hosted so no external origin
is permitted anywhere. **`script-src` retains `'unsafe-inline'`** — see
residual risk R1.

### Authorization and abuse

- Provider/profile deletion pinned to the acting workspace, for both the
  `Workspace` write and the agent-name lookup behind the 409 message.
- `GET /model-providers/{id}/balance` raised to `AdminCtx`.
- `limit` and `offset` clamped at both ends across seven services.
- Timeline endpoints capped at 2000 rows.
- Skill archives: member count and aggregate decompressed budget enforced
  inside the read loop, and members are read by `ZipInfo` rather than by name
  so a duplicate filename cannot decouple the bytes read from the size checked.

### SSRF

- Hostnames the resolver would reinterpret as a packed IPv4 address
  (all-numeric last label, `0x`-prefixed labels) are rejected outright.
- Address classification replaced with a predicate that also covers multicast,
  reserved and unspecified space, and unwraps IPv4-mapped, 6to4 and Teredo
  IPv6 addresses to judge the address they actually carry.
- Hostnames are resolved and every returned address is checked. Resolution
  *failure* deliberately does not block — a name that cannot be resolved here
  cannot be connected to either, and failing closed would break air-gapped
  installs. `JHIN_CONNECTOR_SKIP_DNS_CHECK=true` disables it.

---

## Tests added

| File | Covers |
|---|---|
| `apps/api/tests/test_security.py` | Argon2 parameter floor, rehash, the full password policy, CSRF derivation/binding/one-wayness, and 14 lockout properties — per-account across rotating addresses, per-address across accounts, progressive backoff, the block cap, decay clearing the lock, the blocked-address-stops-poisoning rule, and table pruning |
| `apps/api/tests/test_csrf.py` | Session binding: attacker-planted token that double-submits correctly is still rejected; a previous session's token is rejected |
| `apps/api/tests/test_security_headers.py` | Every header, HSTS gating, routes keeping their own cache policy, body limit with and without a declared length, and that a 422 never echoes the submitted value |
| `apps/api/tests/test_settings_transport.py` | Every refuse-to-start case, and that the localhost quick start still boots |
| `apps/api/tests/test_auth_sessions_unit.py` | Rotation/fixation, absolute and idle expiry, revoke-on-idle, client binding vs. roaming, logout scope, logout-everywhere, password change revoking all sessions, and lockout surfacing `Retry-After` |
| `apps/api/tests/test_workspace_scoping_unit.py` | Cross-workspace reads 404; deletion cannot clear another workspace's default or leak a foreign agent's name; in-workspace guards still fire |
| `packages/connectors/tests/test_ssrf_policy.py` | 50 cases: private/loopback/link-local/ULA/mapped/CGNAT/reserved/multicast, packed IPv4 literals, non-http schemes, credentials, the operator allowlist, DNS resolving into private space, mixed answers, and resolution failure |

---

## Residual risk — what a self-hoster must still do

Software cannot fix these. They are the operator's job.

**R1 — `script-src 'unsafe-inline'` in the web CSP.** The App Router streams
its payload through inline scripts whose content changes every build, and the
theme bootstrap must run before first paint. Hashing is not stable across
builds; nonces require middleware that forces every route to render
dynamically. The rest of the policy still blocks framing, plugin abuse and
`<base>` hijacking, but this weakens XSS containment. Moving to per-request
nonces is the right next step.

**R2 — TLS termination is yours.** Jhin does not terminate TLS and ships no
reverse proxy. Put one in front, set `APP_URL=https://...` and
`COOKIE_SECURE=true` (the API now refuses to start otherwise), and publish
**only** the web entry point.

**R3 — Master key custody.** Losing it makes every stored secret
unrecoverable; leaking it exposes every stored credential. Back it up
separately from database backups, restrict it to `0600` on the host — the
loader does not enforce this, because Docker mounts secrets `0444` and a
fail-closed check would break every supported deployment — and never commit
it. Master-key **rotation is not implemented** (S4): plan for that.

**R4 — Network isolation.** Keep the API, PostgreSQL, NATS, Temporal and
`sandbox-runner` off public interfaces. The base `compose.yaml` publishes the
API on port 8000 for convenience; the release compose does not, and you should
not. If the API is reachable from a network listed in `TRUSTED_PROXY_CIDRS`, a
client on that network can forge `X-Forwarded-For` and evade per-IP lockout —
narrow the list to your proxy's own network if you know it.

**R5 — Backups, including the audit trail.** The audit log is append-only in
code but not in the database. Back up PostgreSQL, and store the master key
somewhere a database restore alone cannot reach.

**R6 — DNS rebinding is not fully closed.** Hostnames are validated at
validation time, not pinned into the connection, so a hostile resolver can
answer differently for the request that follows. Closing this needs an
IP-pinned transport. Until then, run the stack where reaching internal
services requires more than one DNS answer, and keep
`JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS` empty unless you deliberately authorize
an origin.

**R7 — Sandbox egress is unrestricted for `network_policy="internet"`.** Jobs
on that policy reach anything the host can reach, including cloud metadata,
and never pass through the URL policy. If you run on a cloud instance with an
IMDS endpoint, enforce IMDSv2 or block `169.254.169.254` at the network layer.

**R8 — Use exactly one Docker-socket overlay** (`compose.rootful.yaml` *or*
`compose.rootless.yaml`), never both. The rootless transport is an
unauthenticated Docker API proxy contained only by an `internal` network:
attaching any other service to the `engine` network turns it into host-level
Docker compromise.

**R9 — Never run `compose.dev.yaml` in production.** It enables fake
providers, development credentials, and connector allowlists that
deliberately permit plaintext in-network origins.

**R10 — Prompt injection is bounded, not solved.** Untrusted tool output is
explicitly labelled and agents cannot escalate their own grants — but a
sufficiently capable injection can still mimic the label, and content can be
laundered into an agent's system prompt through memory (G8). Grant agents
least-privilege connector scopes, keep approval policies on for write
operations, and never grant an unscoped `cli.*` or `*` capability (G7).

**R11 — Login lockout is per-process.** It is in-memory, so replicas each keep
their own counters. Do not run multiple API replicas until it is backed by the
database or NATS.

---

## Verification

| Gate | Result |
|---|---|
| `uv run ruff check .` | pass |
| `uv run ruff format --check .` | pass for every file changed here |
| `uv run mypy` | pass (455 source files) |
| `uv run pytest -q` (API + connectors + packages) | pass |
| `pnpm typecheck && pnpm lint && pnpm test && pnpm build` | pass (360 web tests) |
| `uv run pip-audit` / `pnpm audit` | no known vulnerabilities |

Live, against the running stack in the `QA Fresh` workspace:

- All headers present on both the API (`http://localhost:8000`) and the web app.
- Login succeeds; `jhin_session` is `HttpOnly` and unreadable from JavaScript,
  `jhin_csrf` is readable, both `SameSite=Lax`.
- From a real browser: bound CSRF token → 200; forged token → 403; missing
  header → 403; a deliberately staled cookie → 403, then `GET /auth/me`
  re-issues it and the retry → 200.
- Chat and a full agent run complete end to end, from both the API and the
  browser.
- Cross-workspace request → 404. `?limit=-1&offset=-5` → 200 (was a 500).
  Unauthenticated `/me` → 401. `logout-all` revokes every session and the
  cookie is dead immediately afterwards.
