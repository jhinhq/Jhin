# Local provider sign-in implementation plan

**Goal:** Make Jhin self-hosted first: prefer the provider's browser sign-in from a localhost installation without requiring Composio or a public domain.

**Architecture:** Reuse Jhin's direct OAuth discovery, PKCE, browser-session binding, encrypted client/token storage, refresh, and MCP tool permissions. Choose the native OAuth adapter where available, otherwise a verified official remote MCP service. Composio remains an explicit optional method on the same app card. Keep native and MCP tool families distinct; never reuse provider credentials or migrate grants across those families.

**Locality:** A loopback callback works when the browser accesses Jhin on the same machine, including through a private port-forward. A browser on another machine cannot reach a NAS through its own localhost without that forwarding. Device authorization is another option where the provider supports it and a client registration exists. Provider endpoints remain HTTPS; do not permit arbitrary plaintext LAN callbacks or disable security checks.

- [x] Prefer direct OAuth probes regardless of whether Composio is configured; expose native provider metadata and require explicit broker selection.
- [x] Negotiate dynamic-registration client authentication against real provider metadata, use native registration for loopback, and reject unusable responses.
- [x] Preserve original direct catalog endpoints/auth notes and make method choices available on one card, including OAuth discovery in app details.
- [x] Verify a localhost Supabase-style discovery/registration/authorization/callback/tool-discovery flow with no manually supplied key or public callback.
- [x] Document local and NAS access, provider limitations, and optional Composio; update API contract and run focused tests/type/lint/review.

**Evidence:** Supabase's public discovery advertises dynamic registration, S256 PKCE, and client_secret_basic/client_secret_post. Its docs confirm users need no manually created token or OAuth application for MCP. Automated tests can verify Jhin's flow; a real user must complete provider consent for live end-to-end proof.

**Validation:** Backend registration/probe/callback/managed-security checks: 145 passed. Additional OAuth, MCP authentication, catalog discovery/schema and managed connector checks: 394 passed. Frontend Apps, details, connection and reconnect checks: 135 passed, with TypeScript and ESLint clean. The added API integration test completed simulated provider consent on a production-mode localhost origin, checked PKCE and registration/user bindings, verified encrypted client/access/refresh credentials, and discovered tools over real MCP HTTP. Five direct catalog schema regressions passed. Targeted Python lint and source typing passed; OpenAPI snapshot is current. Independent review findings were fixed and rechecked. These checks do not constitute real-browser cookie testing, live Supabase consent, or deployment activation.
