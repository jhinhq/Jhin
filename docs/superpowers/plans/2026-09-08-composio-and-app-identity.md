# Composio adoption and app identity implementation plan

> For agentic workers: execute independent responsibilities in parallel, preserve existing edits, and verify each deliverable before integration.

**Goal:** Connect supported apps through Composio's hosted authentication and display one catalog entry per app, preferring Jhin's built-ins.

**Architecture:** Keep Jhin's native connector types, tool contracts, grants, and approvals. Composio owns managed account authentication and refresh. An encrypted, server-created account binding identifies the Composio account; native executors resolve fresh credentials through a shared bounded client. New authorization flows reuse Jhin's expiring state and browser-session checks. Catalog deduplication runs before filtering, counts, facets, and pagination without deleting installed connections or changing their trust.

- [x] Add bounded Composio REST client, account ownership verification, native credential mapping and execution resolver with tests.
- [x] Add API configuration, managed availability, hosted sign-in initiation and verified callback, reconnect, verification, and disconnect lifecycle with tests.
- [x] Add browser connection flow and operator configuration status, retaining explicit manual connection methods, with tests.
- [x] Suppress built-in/community and equivalent community duplicates consistently across catalog queries; preserve legacy details and installation risk, with regression tests.
- [x] Integrate additional managed toolkit execution using Jhin's existing dynamic tool permissions where supported.
- [x] Document configuration, regenerate API contract, run focused backend/frontend checks, and review security boundaries and user flows.

**Verification:** Test callback expiry/replay, wrong session/workspace/account/toolkit, pending and revoked accounts, reconnect identity, provider target binding, secret redaction, required public configuration, missing operator setup, duplicate counts/search/pagination, and unchanged legacy connections. Live authentication requires an operator-provided Composio project API key and the user's provider consent.

## Verification results

- 600 backend/catalog/connector tests and 80 migration, API-contract, and tool-advertisement tests passed.
- 111 frontend auth, app identity, reconnect, and connection-tool tests passed.
- Strict mypy passed for 43 source files across API, catalog, secrets, and connectors; TypeScript and targeted Ruff/ESLint checks passed.
- Six Compose renders using synthetic environment values passed; no project key was read or exposed.
- Independent reviews covered callback identity, account binding, namespace ownership, and the web cookie/redirect path. Findings were fixed and regression tested.
- The Linux integration harness cannot collect on Windows because it imports fcntl. Unrelated CLI sandbox tests also require Linux.
- Live provider sign-in and deployment remain unverified: this workspace has no configured Composio project key, and the mandatory public HTTPS project callback verifier must be configured by the operator.
