# Real workspace browser acceptance

This harness uses real APIs and containers. It is disabled unless explicitly enabled. It creates a dedicated acceptance chat and keeps its records/files so an operator can inspect recovery after container restart. It never creates credentials, changes agent grants, or mocks routes.

Use an existing owner/admin test account session exported by the operator as Playwright storageState. Keep that file outside version control. Set `JHIN_LIVE_AUTH_STATE`, `JHIN_LIVE_URL`, `JHIN_LIVE_WORKSPACE_ID`, `JHIN_LIVE_AGENT_ID`, and `JHIN_LIVE_WORKSPACE_ACCEPTANCE=1`. The session's first workspace must match the selected workspace, matching the application shell. Do not supply an API key in place of the session: PTY and preview tickets deliberately require session authentication.

Run from `apps/web`:

```text
node node_modules/@playwright/test/cli.js test --config browser-live/playwright.config.ts
```

Set `JHIN_LIVE_AGENT_GENERATION=1` to include the separately gated real model turn, which calculates a CSV total and publishes an editable report using the configured agent's existing tools. This uses model quota.

The files scenario checks uploads, revision saves, directory browsing, actual PTY input and reconnect, interruption, retained output, and opaque-origin interactive HTML previews. Three additional fixtures exercise Vite/React and Next.js hydration, a Next POST route, and HTTP POST/WebSocket round trips through the scoped browser gateway. Vite and Next install pinned dependencies inside their disposable preview containers. No model is required for these four cases.

JSON test attachments identify the dedicated chat/files without recording session credentials. Each case stops only its own sessions and returns workspace ownership after completion or failure. After restarting workers and the runtime container, reopen the files scenario chat, open `sales.csv`, and confirm version 2 still contains `North,50`; retained immutable content is stored separately from containers. Dismiss a new test owner's Getting started tour through its normal Skip for now control before saving storageState.

For an operator's already authenticated browser, perform these same steps directly. No cookie export or new credentials are required for manual acceptance. Screenshot and JSON artifacts are evidence from a run only; a skipped harness is not an acceptance pass.
