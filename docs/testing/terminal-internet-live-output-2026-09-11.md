# Terminal internet and command visibility — September 11, 2026

## Reported behavior

The reported curl task, `01a08fc4-16a6-76f2-bc7b-c75b9980a335`, executed
`curl -I https://example.com` through `cli.test.run`. Its real exit code was
6 with a DNS error. That tool intentionally uses Docker networking `none`.
Bisby had no `cli.command.execute` grant. The reply also included a line
from an earlier command that was absent from this curl invocation's output.

Internet networking already existed on the dedicated sandbox bridge.
The missing pieces were an accessible per-agent permission control and
visibility of real execution records in the conversation. Runner logs were
captured after completion, and chat showed prose/activity without command
output cards.

## Resulting behavior

- **Agents → Tools & Access → Terminal Internet** selects an active CLI
  sandbox and enables a connection-pinned `cli.command.execute` grant with
  `network: internet`. Turning it off adds an explicit terminal network deny,
  including for calls that inherit the connection's network default.
- These are ordinary audited grants. The control manages only grants it
  created, preserves handmade grants and approval rules, and reports custom
  restrictions. Offline test jobs, Git operations, and app connections retain
  their separate permissions. No public domain or hosted OAuth service is
  needed for sandbox egress.
- Chat shows the actual command or file operation, execution state, separate
  stdout/stderr, exit code, and recorded errors. Output updates while the
  command runs and survives a refresh. Snapshots replace earlier snapshots;
  they are not appended as duplicate output.
- Live output is limited to command/test tools. Generated file-tool wrappers
  contain internal evidence trailers and use their public structured result
  instead. Chat displays up to 8,192 characters per stream and the newest
  100 terminal calls, with both limits stated in the interface.
- Platform prompt version 18 distinguishes offline tests from internet-enabled
  commands, container paths from host paths, missing programs from network
  restrictions, and current command output from older observations.

No migration or connection-default change is required.

## Live checks

Bisby's existing sandbox received one explicit internet command grant. Its
push approval policy and other agents' permissions were preserved.

- Task `01a08fca-3cd1-7dd0-b5cb-0a885b7cf98e` ran
  `curl -sS -I --max-time 20 https://example.com`, returning HTTP 200 and exit 0.
- The production internet control was exercised in a browser against the
  local authenticated API: On → Off (explicitly blocked) → On → reload.
  It remained On, preserved the original grant, and fit at 390 and 1440 pixels.
- Fresh conversation task `01a08fdf-ee52-73c1-a344-080c0fd4f8e8` was asked
  naturally whether it could run curl, without naming the tool or network
  argument. Bisby selected `cli.command.execute` with `network: internet`,
  ran `curl -i https://example.com`, and returned the actual headers/body,
  HTTP 200, and exit 0.
- Task `01a08fe4-7e88-7401-86fe-ce0888c2bf72` printed staged stdout and
  stderr, slept for 12 seconds, then fetched example.com. The production chat
  in a browser showed both streams and Running before the final marker existed.
  It then showed HTTP 200, Completed, and exit 0. The same output survived a
  page reload, and the terminal card fit at 390 and 1440 pixels without page
  overflow. The agent turn completed with the actual output.

Local test evidence is under `.tmp/live-agent-20260910/` and
`.tmp/terminal-progress-20260911/`. Verification scripts take an existing
credential through the environment; they do not save it. Browser fixtures
substitute router/workspace IDs and authentication while retaining the
production components, query hooks, and API responses.

## Validation and rollout

Backend regression coverage passed: 340 tests with two platform skips, plus
five shell checks with one POSIX-only skip using Git Bash. Prompt/context/persona
checks passed (44), as did OpenAPI compatibility checks (32). Frontend terminal
coverage passed across 16 suites (299 tests), with additional permission-control
and API scope checks. TypeScript, ESLint, Ruff, targeted mypy, and whitespace
checks passed. Independent lifecycle and secret-redaction reviews found no
remaining actionable issues after fixes for partial and overlapping secrets.

The local API, agent worker, tool worker, sandbox runner, and web services were
rebuilt and restarted. All five report healthy; the web returns HTTP 200.
Bisby's terminal internet permission remains enabled. The two temporary
verification chats were archived after their agent turns completed.
