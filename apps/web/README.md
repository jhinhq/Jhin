# jhin-web

Next.js (App Router) web UI for Jhin. Phase 1 ships a minimal shell that
renders live stack status from the API readiness endpoint
(`/api/v1/health/ready`): per-dependency health for PostgreSQL, NATS
JetStream, and Temporal, plus reachability of the API itself.

Configuration:

- `API_INTERNAL_URL` — base URL the web server uses to reach the API
  (defaults to `http://localhost:8000`).
- `APP_NAME` — display name fallback when the API is unreachable.

```bash
pnpm dev        # local dev server
pnpm lint       # eslint
pnpm typecheck  # tsc --noEmit
pnpm test       # vitest
pnpm build      # production build (standalone output)
```
