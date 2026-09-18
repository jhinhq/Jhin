# Marketing showcase execution baseline

Plan: `docs/superpowers/plans/2026-09-15-mindy-ashley-marketing-showcase.md`.

The owner authorized comprehensive execution and fixes, supplying Jhin, Ghost, and Unsplash credentials. Production writes are restricted to the new assignment's draft. Publication is tested only against isolated Ghost.

## Current execution

- Branch: `codex/marketing-showcase`, created from the existing dirty checkout. Existing changes are preserved; no reset, reseed, or broad staging is permitted.
- Running installation: local Jhin Compose project using `compose.yaml` and `compose.desktop.yaml`; web port 3000, API port 8000.
- Initial migration head: 0051. Deployed additions: 0052 editorial authority, 0053 prospective memory authority, 0054 durable review continuation, 0055 archive corpus, 0056 image receipts, 0057 bounded capability grants.
- Existing Ghost client/editorial/setup/API baseline: **72 passed**. Command: `.venv/Scripts/python.exe -m pytest packages/connectors/tests/test_ghost_client.py packages/connectors/tests/test_ghost_editorial.py packages/connectors/tests/test_ghost_setup.py apps/api/tests/test_ghost_api.py -q`.
- A local pytest cache permission warning occurred. Use a per-task cache or disable cache provider for subsequent concurrent runs.
- `uv` global cache is inaccessible inside the sandbox; use the existing workspace Python directly or a task-local cache. Docker inspections require the authorized escalated execution path.
- API-key discovery uses `/api/v1/auth/identity`; `/api/v1/workspaces` is session-only and returned 401. This was corrected at the client; no authentication policy was weakened.

## Ownership

| Work | Owner | State |
|---|---|---|
| Assignment/draft/review/publish authority and complete provider metadata | editorial_guards | In progress |
| Durable result continuation and evidence verifier | durable_reviews | In progress |
| Prospective memory capture policy and scope/evidence enforcement | memory_capture | In progress |
| Live discovery, credentials, Unsplash, corpus, registries, UI integration, full acceptance | root | In progress |

## Execution rulings

- Preserve the dirty checkout on a new branch instead of creating a worktree that would omit the current uncommitted Ghost/memory implementation.
- Treat the supplied `ghost url` as the owner's explicit target, normalized to `https://blog.fanclan.io`; never guess a different Admin origin or forward credentials on redirects.
- Reuse current model profiles and persistent agent IDs. Do not infer identities from historical test IDs.
- Keep secure inputs transient until the existing encrypted Jhin intake accepts them; never copy credentials into this report, source, fixtures, or a model prompt.
- Real images follow the plan's permitted selection mode; human selection is required unless provider authorization for autonomous selection is established.

This is a live execution ledger, not an acceptance pass. Remaining plan tasks and scenarios are pending until fresh evidence is recorded.

## Live run evidence, September 16 UTC

- Workspace Jhin HQ `01a050e9-bf9b-7501-89b7-acc85e4741a3`; Marketing `01a09869-9da5-7a90-8da9-0c47bf35df42`.
- Existing Mindy `01a09869-9ddb-7340-8f6a-396b48eec877`; Ashley `01a09869-9dbc-7832-950e-f50ff5a42d4a`.
- Both retain the existing Qwen 3.8 profile (`qwen3.8:latest`, profile `01a064bf-d387-70e1-818a-b1c24c4fd466`).
- Live conversation `01a0a87f-184c-7fb0-b549-3a2ae2734fa4`; initial setup task `01a0a87f-186f-7ea2-938e-795c73f09cf7` completed. Mindy stored the Ghost key encrypted at Marketing team scope, bound and verified the supplied URL, listed real posts, and asked editorial questions. No article mutation in that task.
- Ghost connection `01a0a887-4469-7e52-ac59-c4c69150d280`; research web connection `01a0a8a1-19a7-7641-99de-93e2104620fa`, with connection-scoped web.fetch grants for both agents.
- Research task `01a0a8a3-f287-7611-ac18-1201d16cffd8` started after deployment: secure Unsplash setup, full archive, delegated non-explicit creator topic, 900–1200-word draft, human cover-image choice, draft_only. This is pending, not a completed article.
- Owner input remains pending for optional topic choice, image search/selection, and browser sign-in. Capture-policy controls intentionally require a signed-in admin session; API-key restrictions were not relaxed to pass a test.
- Local Jhin services were rebuilt and restarted after an encrypted-database backup under ignored `.tmp/marketing-db-before-0057.dump`. API, agent worker, tool worker, workflow worker and web reported healthy. An initial credential-free connection creation timed out and was reconciled as no row; the same operation succeeded after deployment.

## Fresh verification so far

- Existing Ghost baseline: 72 passed; expanded owned Ghost/client/setup/editorial/assignment/API checks: 95 passed.
- Memory backend/capture/worker/API: 333 passed; memory frontend: 38 passed; focused types/lint clean.
- Review frontend: 3 passed and TypeScript clean.
- Durable continuation focused checks: 78 passed; Temporal and frozen history replay: 57 passed, one PostgreSQL test separately executed later.
- Isolated Ghost 6 fixture: real draft, requested revisions, new approval, draft-only publication denial, writer denial, outside metadata-change invalidation, rereview, and director publication: 1 integration scenario passed. This is connector-level real-provider evidence; it is not yet the configured-model rehearsal.
- Fresh PostgreSQL migration through 0057 and eight continuation/Ghost/scoped-variable race tests passed. Two further real PostgreSQL authority tests passed. The first migration attempt caught an overlong PostgreSQL constraint name; fixed before deployment.
- Three real PostgreSQL Unsplash reservation/revocation/uncertain-effect tests passed after fixing a credential timestamp self-deadlock. These use provider transport doubles.
- Full archive fixture covers 4,500 posts, two agreeing scans, missing body/incomplete coverage, long article chunks and restart checkpoints. Search is explicitly lexical; there is no claimed semantic embedding index.
- Central platform prompt/registry/default grants/route scopes: 82 passed. Projections plus migration graph: 88 passed.
- Release preflight passed. The OpenAPI snapshot was regenerated.
- Full Windows test collection encountered Linux-only `fcntl`; an independent Windows-compatible slice and a supported Linux run are in progress. Full mypy exposed existing and newly imported test typing issues; remediation is in progress. Do not claim either gate passed yet.
- The ordinary integration harness correctly refused the occupied Docker daemon. A separate disposable daemon is being prepared without changing existing Jhin or unrelated containers.

## Runtime findings and second deployment

- The research run submitted `brief` as quoted JSON repeatedly. Strict validation denied every malformed call; no assignment or draft was created by those calls. Diagnosis traced the lost shape to Ollama's typed tool decoder dropping `$ref`. The Ollama adapter now expands bounded local schema references while retaining strict server validation; it rejects cyclic/external/broken references. Other provider schemas are unchanged. Models/editorial-wire/gateway verification: **440 passed**.
- The same run then encountered repeated `ollama: stream transport failed` while reasoning. It ended failed after ten reasoning steps. Its pause request was accepted, but could not interrupt the pending model activity. This is recorded as a live acceptance failure, not completed research.
- A second build/redeployment of API, agent worker, tool worker and workflow worker includes the Ollama compatibility, Unsplash transaction, and archive projection fixes. Schema migration remains 0057.
- An ignored Compose override temporarily permits exactly `http://jhin-showcase-ghost:2368` for the disposable Ghost rehearsal. The fixture is attached to `jhin_edge`; Fanclan remains the separate HTTPS production connection. Remove the override and private network attachment when the rehearsal is finished.
- Three clearly labelled fixture articles were seeded in the disposable Ghost archive. They are controlled inputs, not claimed agent-generated articles. Configured-agent writing/review/publication remains pending.
- Full frontend verification: **120 files, 1,355 tests passed**. Full repository Ruff passed and Linux-platform mypy passed for **689 files** before the later Ollama fix; its touched files also passed focused checks.
- The dedicated Linux harness reached its real stack and exposed outdated agent-name fixtures and a fake-provider stream failure. Remediation is in progress. Full Python suites are still running; no overall pass is claimed.
- Operator-only provider readiness at `2026-09-16T05:39:28Z`: trusted HTTPS Ghost Admin reads returned **4,498 published, 0 draft, 0 scheduled** posts. Unsplash authenticated metadata validation returned HTTP 200 with one photo; no selection/download-tracking call occurred. This is credential/readiness evidence, not agent-generated research or a completed full-body corpus.
- Ollama management API version 0.33.2 remained responsive, but a single tiny inference returned no headers/data within 45 seconds and `/api/ps` showed no resident model. Remote cause remains unknown. Owner was asked to check/restart that host; no model profile, timeout, or remote system settings were changed.

## Broader verification and latest deployment

- Initial full Linux snapshot: **7,949 passed, 60 failed, 36 skipped, 178 deselected** in 28m28s. Initial Windows run: **7,538 passed, 137 failed, 42 errors**, including unsupported Unix socket/UID/resource tests. These are baseline results, not a green full-suite claim. Failed Linux nodes are being rerun after fixes; the first rerun had 84 passed and 10 remaining Compose failures including added acceptance cases.
- The full run found outdated registry/worker/default-grant assertions plus real missing Ghost icon registration, missing runtime-gateway secret-log processing and DB tracing, and a credential-fragment disclosure through rejected memory tags. These were fixed. The memory fix passed a 356-test slice with 23 actual-store/gateway boundary tests; observability/runtime-gateway fixes passed 215 tests. The IPC audit exception is limited to exact reviewed subprocess JSON responses and has negative tests.
- Ten new editorial adversarial cases passed locally and on Linux. They cover generic owner review override, renamed-Ashley impersonation, metadata/source/intent drift, missing cover evidence, and three unresolved revision rounds.
- A real PostgreSQL upgrade fixture passed: 0051 historical published/uncertain records are retained verbatim, pending/approved unbound reviews become stale, and old reviews cannot publish. The test uses and drops its own unique database, never the shared fixture or production database.
- A second tiny Qwen probe again returned no headers/data within 45 seconds. Real configured-agent Tier B/C work remains blocked on inference. No workaround model was substituted.
- Latest build includes API, agent worker, tool worker, workflow worker, runtime gateway and web. It deploys the memory-validation privacy fix, runtime observability fixes, and Ghost icon. Deployment uses only the normal Compose files, restoring the default empty HTTP-origin allowance. The ignored isolated-origin override remains as a future rehearsal helper but is no longer active.
- The supported Linux integration harness runs inside its own Docker-in-Docker daemon. Streaming and duplicate-script fixture defects were found and fixed; final regression/extended results and browser checks remain pending. See the acceptance matrix for exact distinctions between mocks, PostgreSQL, real isolated Ghost, real model and browser evidence.

## Continuation on September 16

- Current results and blockers are consolidated in `marketing-showcase-execution-report.md`; browser receipts are in `marketing-showcase-browser-evidence.md`.
- The real browser verified team/company memory-policy creation and revocation, persistence through isolated stack restart, a complete 65,456-character editorial review, mobile/desktop layout, script isolation, refresh and keyboard controls. Fixtures were explicitly synthetic and both temporary browser tabs were closed after resetting the viewport.
- Added real PostgreSQL publication tests passed: revoked publisher grant prevents dispatch, concurrent requests serialize to one publish call, and an ambiguous response remains uncertain without automatic redispatch.
- Supported regression results improved to 26/28; both remaining Phase 3 fixtures now pass focused verification. Extended results improved to 64/69 with all 14 marketing nodes passing; Phase 4/5 focused reruns now pass and the final combined runs are underway.
- A subsequent full Linux run reached 8,102 passes and ten failures. The failures were isolated to test-runner lease/PID1 behavior and one service-count assertion; all ten passed after corrections. A clean full unit run is underway in a separate snapshot with an init process.
- The user's Continue message did not indicate that Ollama was restored. A new probe at 17:47 UTC again timed out after 45 seconds without headers; management endpoints responded and no model was resident. A question is pending about using an existing model profile for the live rehearsal. No profile has been switched.
- The native Ollama chat route also timed out at 17:52 UTC; a nonexistent model was correctly rejected immediately. This narrows the readiness failure to inference with the configured model, without establishing a host-level cause. Repeated probing stopped.
- Delegation fixtures now use read-before-edit and the native credential-handling push tool with approvals limited to their own exact task trees, connection, repository and branch. A scripted QA review no longer executes the author's embedded repair instructions. Each retest uses a task-specific checkout and asserts its head equals the author's pushed SHA. The failure/fix/retest scenario passed in 31.98 seconds; the complete manifests are rerunning against the same fixture build.
- Final read-only static verification passed repository Ruff lint, Linux-platform mypy for 691 source files, and OpenAPI snapshot consistency. The optional formatter audit found 89 files (77 newline-only, 12 layout differences); this continuation's Phase 5/8 layout differences were formatted and linted. Broader baseline formatting was preserved.
- Final ordinary-installation verification at 18:12 UTC: all 12 services healthy, schema 0057, empty extra HTTP origins, original Mindy/Ashley Qwen profiles and reporting relationship retained. The failed research task is terminal with no pending tool calls or Temporal activities/children, and no showcase assignment, archive sync or handoff pending.
- Final extended integration manifest: **69/69 passed in 165.79s**, including all 14 marketing cases, with no skips, xfails or deselections. Complete evidence is `.tmp/marketing-extended-phase8-verified.log`.
- Final broad Linux invocation: **8,109 passed, 3 failed, 37 skipped, 178 deselected in 19m59s**. All three failures were `python3` absent from the test-runner PATH (exit 127). With the runner venv on PATH, both affected files passed (**11 tests in 2.94s**). This closes the observed failures without claiming a single green full-suite invocation. All 368 model tests separately passed after the later deterministic-provider fixture update.
- Browser forwarding containers were removed; outer disposable Ghost and PostgreSQL were stopped with data retained, and Ghost was detached from the ordinary `jhin_edge` network. Inner harness cleanup follows its final same-build supported-regression run.
- Final supported regression manifest: **28/28 passed in 200.55s** on the same deterministic-provider build as the 69-node run, without skips, xfails or deselections. Evidence: `.tmp/marketing-regressions-verified-current.log`. The supported leased cleanup command is removing only its owned inner project before stopping the dedicated test helpers/daemon.
- Cleanup removed the inner test containers/project networks, then correctly refused the clean-daemon invariant with 48 sandbox workspace volumes lacking a lease-project ownership label. Volumes and valid lease evidence were preserved; both helpers and the dedicated daemon were stopped. All five retained `jhin-showcase-*` containers are stopped, while all 12 ordinary Jhin services remain healthy. A new harness run requires a fresh daemon or explicit reconciliation of the retained fixture volumes.
