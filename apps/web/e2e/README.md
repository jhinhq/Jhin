# Browser end-to-end specs

Playwright specs for the chat experience, driven against a **running dev
stack** — the real API, workers, Temporal and the fake model provider.

They are deliberately **not** part of `pnpm test`. That gate is Vitest against
jsdom and stays fast and offline.

## Running them

```sh
# 1. the stack (once)
docker compose -f compose.yaml -f compose.dev.yaml up -d

# 2. the browser (once per machine — npm ships the driver, not Chromium)
pnpm --filter jhin-web exec playwright install chromium

# 3. the specs
make test-e2e                       # from the repo root, checks the browser first
pnpm --filter jhin-web test:e2e     # or directly
pnpm --filter jhin-web test:e2e -- --headed --project=chromium chat-send.spec.ts
```

`make test-e2e` fails with the `playwright install` line if Chromium is
missing, instead of a stack trace out of the first worker.

Environment overrides:

| Variable | Default | Purpose |
| --- | --- | --- |
| `JHIN_E2E_BASE_URL` | `http://localhost:3000` | the web app under test |
| `JHIN_E2E_FAKE_PROVIDER_URL` | `http://fake-provider:8080/v1` | how the **agent worker** reaches the fake provider (in-network) |
| `JHIN_E2E_FAKE_PROVIDER_PROBE_URL` | `http://127.0.0.1:8090/v1/chat/completions` | how **this process** reaches it, to measure its latency |
| `JHIN_E2E_WORKERS` | `2` | parallel specs; every worker is another real workflow on a shared stack |

## How a spec gets its data

Nothing depends on seed data or on another spec. `fixtures/test.ts` gives every
test a `workspace` built through the public API: a workspace, a provider
pointed at the fake provider, a model profile, and whatever agents the spec
asks for.

Two details are worth knowing:

- **Every workspace comes with a user of its own.** The app shell has no
  workspace switcher — it always opens `memberships[0]`, the account's
  *oldest* workspace. A workspace created a moment ago sorts last, so the only
  account that can see it in the browser is one that has no other. The fixture
  signs in as the dev seed owner (`owner@jhin.dev`, from
  `apps/api/src/jhin_api/seed.py`) only to create the workspace and invite a
  stranger into it; the browser session belongs to that stranger.
- **Sessions are injected, not typed.** The fixture establishes the session
  over the API and adds the cookies to the browser context. `login.spec.ts`
  covers the real sign-in form once, so the login page is still exercised.

Workspaces are **not** cleaned up afterwards. Deleting them would be the one
destructive act in an otherwise additive suite, on a database that is also
somebody's dev environment. They are named `E2E <spec title> <tag>`, which is
enough to recognise and sweep by hand.

## Keeping a run open

Stop, pause/resume, steering and reload-mid-run all need the agent to still be
working when the browser arrives, and a turn against the fake provider is over
in well under a second.

`FAKE_PROVIDER_LATENCY_MS` (compose.dev.yaml) is the documented way to slow one
down, and it works — but it is set on a container the whole machine shares, so
a suite that *depends* on it only runs after someone re-creates that container,
and it slows every other run on the stack while it is set.

So `fixtures/live-run.ts` builds the window out of **steps** instead. The fake
provider turns each `[[tool:…]]` marker in a message into a tool call, and the
agent loop checks for pause, cancellation and pending instructions *between*
steps — exactly where the controls need it to. `system.echo` is the cheapest
marker there is. The helper measures the provider's latency first and sizes the
script accordingly, so setting `FAKE_PROVIDER_LATENCY_MS` still works: it just
buys the same window with fewer steps.

## Known product race

`chat-live-controls.spec.ts` → *Stop ends a run in flight* is occasionally red
under parallel load, and it is the product rather than the spec. The "Stopped"
chip is derived from the task being cancelled, so it turns up in
`GET …/activity` at the same instant the conversation stops reporting an active
task — which is also the instant `live` goes false in the thread view and both
2-second pollers are switched off. Whether the chip is ever fetched comes down
to which poller ticks first; reloading always shows it. See the comment at the
assertion. The assertion is left as it is: a stop the reader took should leave a
record they can see.

## The oracle

`packages/models/src/jhin_models/testing/fake_openai.py` replies
`[{model}] Completed: {the last user message it saw}`. Whichever question comes
back is the one the model actually read last — which is what makes
`chat-turn-order.spec.ts` a real check on prompt ordering rather than on
plumbing.
