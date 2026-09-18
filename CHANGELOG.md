# Changelog

All notable changes to Jhin are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Major version zero means public interfaces may still change between minor
versions.

## [Unreleased]

### Added

- **An agent keeps its disk between turns.** Every `cli.*` job of one agent
  now runs on a named volume derived from that agent's identity, mounted at
  `/workspace`, and it outlives the run that made it — a checkout, a
  dependency install and a build cache are still there on the agent's next
  message instead of being cloned from scratch and deleted every turn. A
  second concurrent run of the same agent gets a private `run-<run_id>` disk
  rather than sharing the tree. The work carries over too, not just the
  caches: a checkout onto a branch that already exists continues it — from the
  remote's copy, or from unpushed commits still on the disk — so a second turn
  builds on the first instead of starting again (see **Fixed**). `sandbox_workspace` (migration `0040`) is the
  control plane's account of who owns which volume, when it was last used and
  which run holds it now; `size_state` (migration `0042`) records whether a
  stored size is the disk's usage or only a floor, and a workspace whose
  measurement did not finish refuses new calls rather than reading as small
  and being evicted with an agent's unpushed work on it.
- **An agent can be given a name and keep it.**
  `organization.identity.set_name` writes `agent.name`; until now the only
  writer was an admin-gated `PATCH /agents/{id}`, so an agent told "your name
  is Bisby" could do no more than agree to answer to it for one chat. The
  tool's input names no target, so renaming a colleague is not expressible
  rather than merely refused; a name must be conferred by a *person* in the
  run (`jhin_tools.naming_authority`), so "order a colleague to rename
  itself" — one hop to the same outcome, and how both reported cases actually
  happened — is refused, with the delegation chain recorded; `agent.slug`
  never moves, so links, references and handles survive a rename; and every
  rename leaves a visible receipt in the conversation beside its
  `agent.renamed` audit row. Migration `0041` backfills the capability to
  agents that already exist, and `0043` makes one name per workspace a
  database fact, resolving duplicates an install already holds (the oldest
  row keeps the name; later ones take the smallest free numbered suffix and
  an audit row saying so).
- **How long an agent has been working, rather than how long you have been
  waiting.** A turn's `started_at` is stamped once and is not re-stamped when
  an approval is decided or a question answered, so elapsed time was the
  agent's thinking *plus* every minute a person took to decide — one live run
  showed twenty minutes of "thinking" over fifty seconds of thought.
  Conversations now carry `active_run_working_seconds` and
  `active_run_working_since` beside `active_run_started_at`, derived from the
  approval, question and review rows that are themselves the waits, so the
  number is retroactive and no existing column had to change meaning. The
  transcript, the chat header pill and the conversation rail count from it;
  while a run is parked on somebody there is no clock rather than a wrong one.
- **A failed turn says what happened, and offers the way out.** Run failures
  reached the chat as their own debugging note ("tool call a34dd1dc-…
  execution outcome is unknown; manual reconciliation is required"). The API
  now sends a failure notice beside the raw text: one sentence per failure
  class in the product's voice, the failure's own words underneath where they
  add something, and the identifier kept for support but never leading. It is
  rendered on the server, so the chat, the activity feed and the attention
  inbox cannot drift apart. A new endpoint,
  `POST /api/v1/workspaces/{workspace_id}/conversations/{conversation_id}/resume`,
  picks the failed turn back up without retyping it; it is safe to press
  twice, and it refuses (`409`) when the last turn did not fail, when the chat
  or the agent cannot take work, or when a call from that turn was never
  accounted for and repeating it could repeat whatever it did.
- **Composer controls.** Which model the agent runs on, how cautious it is
  before acting, what it can reach, and what the chat has cost so far, on the
  composer's own row — where a person already is when the answer makes them
  want to change one of those things. Readable by anyone who can open the
  chat; the writes stay admin-only, enforced by the API.
- `github.repository.list` — an agent can find a repository instead of asking
  a person for its `owner/name`. Every other GitHub tool takes an
  `owner/name` it must already know, so an agent asked about "the Password1
  repo" had nothing to call. The new tool lists the repositories the
  connection's token can reach (user tokens and GitHub App installation
  tokens alike, whether or not the app is installed anywhere), in name order,
  with an optional substring `query` and `owner` filter, a per-call limit, a
  cap on pages walked, and a `truncated` flag that says when either cut the
  answer short. It is in the GitHub (read) and Code editing bundles, so
  re-applying a bundle an agent already holds adds exactly this one row.
- Grant scopes now bound the *rows* a listing returns, not just the calls that
  name a resource. A tool may declare `result_scope_keys` for a dimension its
  call spans rather than names; the evaluator matches such a grant on the
  dimensions the call does name, and the gateway hands the executor the allow
  grants that authorized that very call so it can drop everything outside
  them. An agent granted `octo/*` lists `octo` repositories and learns nothing
  about the rest of the token's reach. The gateway still decides every call:
  this only narrows a READ result.
- Two one-shot commands on the `tool-worker` image, both idempotent and both
  safe to run against a live stack: `jhin-sandbox-reconcile` closes
  `sandbox_job` rows whose runner can be shown to have let go of the job, from
  the runner's own account of it, and `jhin-tool-calls-rollback` is the manual
  step in rolling this release back (see **Upgrade notes**). Each takes
  `--dry-run`.

### Changed

- **One sandbox job per tool call, decided where both attempts are visible.**
  A tool call whose worker dies is re-dispatched with a fresh job id, so
  nothing on the wire related the two attempts and a `cli.file.edit` could be
  applied twice. Every job now carries the invocation it belongs to, and the
  sandbox runner answers a second dispatch of one invocation with the first
  dispatch's job — running, or finished with its outcome intact — instead of a
  container of its own. Guards over file contents cannot settle this and have
  been removed: a file records an *effect*, and the question is about an
  *event*. Across a runner restart its ledger is empty by construction, so the
  tool worker sends `prior_dispatch_at`, the moment the earliest dispatch of
  that call began, read from its own `sandbox_job` rows; a dispatch the runner
  cannot vouch for is refused (`redispatch_unprovable`) rather than run a
  second time on top of however far the first got. Both ends fail closed.
- `sandbox_job` rows are committed on their own connection before the job is
  submitted, instead of in the tool call's transaction. The record of a job
  used to be conditional on the call *succeeding*: a push that came back with
  exit 128 raised, the gateway rolled its transaction back to persist the
  outcome, and the row describing the container went with it — leaving an
  operator the single word "unknown" behind two real pushes.
- `tool_call.status` gained `claimed`: the durable half-step that proves an
  executor was never entered, so a call found there on re-entry is
  re-authorized and dispatched once rather than reported as an unknown
  outcome. The move from `claimed` to `executing` is a compare-and-set, which
  is the at-most-once guarantee itself and not merely a record of one. No
  migration — the column is a `varchar` — but rolling back has a step; see
  **Upgrade notes**.
- `tool-worker` now drains on SIGTERM: it stops accepting new calls, lets
  in-flight ones finish inside a budget, and closes what it can. A worker
  cancelled underneath a running sandbox job leaves the container alone and
  records that it walked away, rather than killing the one thing that still
  knows what the call did.
- The internal runner API gained `GET /v1/runner/memory`: when this runner
  process began serving, and how long it keeps a finished job's record. It is
  behind the same bearer token as every other job endpoint and carries no job
  data. The tool worker's reconciliation sweep is the only caller, and it is
  what lets a 404 for a job mean something exact — see **Fixed**.
- The internal runner API (`POST /v1/jobs`) requires `prior_dispatch_at`
  whenever `invocation_id` is set. An omitted field is indistinguishable on
  the wire from "I established there was no earlier dispatch", which is the
  answer that starts a container, so it is refused (`422`) rather than read as
  one. A caller that offers no invocation is unaffected, which is what keeps
  an older `tool-worker` working against a newer runner.

### Fixed

- **A second run of a task can push again.** On a workspace it already had,
  the checkout ran `git checkout -B <branch> FETCH_HEAD` — force-moving the
  working branch back onto the base ref — so a second run began by rewinding
  past the commit the first one had pushed, did its work on top of the base,
  and had its push rejected as a non-fast-forward with everything it had done
  stranded in the sandbox. A branch that already exists is now **continued**:
  from the remote's copy when the remote has it (so an evicted, purged or
  re-cloned workspace resumes the same branch), otherwise from this disk's
  copy when that copy already contains everything the remote does, so commits
  a run made and never pushed are kept rather than thrown away. The base ref
  decides where a *new* branch starts and nothing else. `started_from`
  (`base`, `remote_branch`, `workspace_branch`) says which happened, in the
  result and in the audit record; a local branch that has diverged from the
  published one loses to it and the head it abandoned is recorded as
  `discarded_head`.
- **Two tasks on one repository no longer take the same branch.** The default
  name was `agent/<first 8 characters of the task id>-<repo>`, and those eight
  hex characters are the top 32 bits of a uuid7's 48-bit millisecond
  timestamp — they advance once every 65.536 seconds. Two different tasks
  started in the same minute got byte-identical names, and the second one's
  checkout resumed the first one's branch. It is now
  `agent/<repo>-<whole task id>`, which is collision-free and is also the
  handle to paste back into Jhin to find the task that made the branch. The
  name is computed at each checkout, so a task that was already working under
  the old name starts a branch under the new one on its next turn, cut from
  the base; the old branch and any pull request open from it are left exactly
  where they are, for a person to finish or close.
- **A re-dispatched sandbox tool can read the answer it is given.** The runner
  answers a second dispatch of one tool call with the first dispatch's job and
  its output, which is what stops a container running twice — but the trailer
  sentinel Jhin parses that output with was drawn per *dispatch*, so the
  replayed stdout carried the first attempt's nonce, no trailer was found, and
  every value fell back to a default: an empty `read_token`, an empty
  `pushed_sha`, a completed checkout refusing itself as
  `checkout_unrecordable`. The sentinel is now derived per tool call (HMAC over
  the tool call id, keyed on the runner token), so every dispatch of one call
  asks for the same trailer. It stays unforgeable and unpredictable from inside
  a container, which never sees the key.
- **A tool worker that was down longer than the runner's memory no longer
  records completed jobs as failures.** The reconciliation sweep read a 404
  from the runner as "nothing will ever report an outcome", which is true of a
  job whose runner restarted and false of one the runner finished and then
  dropped — and the retention window that kept the two apart was sized against
  the sweep's grace and interval, not against the sweep being *absent*. A
  worker that crash-looped or stayed down for an afternoon came back and wrote
  `runner_gone` over jobs that had completed. The runner now publishes what its
  memory covers (`GET /v1/runner/memory`), and the sweep closes a row on a 404
  only where that is proof: `runner_gone` when the runner started after the job
  did, or when it has been serving throughout and would still be holding the
  record of a job it had run. Otherwise the row is closed as
  `outcome_forgotten` — the job is over, and what it did is no longer
  recoverable — and a runner that will not say what it remembers leaves 404
  rows untouched.
- A sandbox tool call now refuses (`redispatch_uncheckable`) instead of
  dispatching whenever the database cannot answer, or cannot be made to
  record, the question the re-dispatch interlock rests on — a failed lookup of
  this call's earlier dispatches, or a `sandbox_job` insert that fails *for any
  reason at all*, constraint refusals included. Both used to proceed as though
  there had been no earlier dispatch, which is exactly how an edit, a checkout
  or a push gets applied a second time. The refusal names the database error,
  ran nothing itself, and reports `side_effect_possible` for the *earlier*
  attempt only: an unaccounted-for listing is a plain retry, an
  unaccounted-for edit stops for a person. It appears while the database is
  unreachable, which is a state in which nothing else is working either.

### Security

- `DELETE /api/v1/workspaces/{workspace_id}` no longer accepts an API key at
  any scope. The route table keyed rules by path alone, so `workspace:settings`
  — offered as renaming the workspace and changing its budgets — also bought
  destroying the workspace and everything in it. Deleting a workspace is now a
  browser-session, owner-only action. Existing keys keep every other workspace
  write, including `PATCH` on the same path.

### Upgrade notes

- **Roll `sandbox-runner` out before `tool-worker`, and `web` last.** Compose
  cannot enforce the first for you — `tool-worker` reaches the runner over
  HTTP and has no `depends_on` edge to it — so take that one on its own and
  let the rest follow. A new `tool-worker` against an old `sandbox-runner`
  does not degrade: the runner rejects fields it does not know, so every
  sandbox job is refused `422` until it catches up. The reverse pairing is
  safe. Full sequence in `docs/deployment.md`. The order matters for one more
  thing now: a `tool-worker` at this version asks the runner what its memory
  covers before it closes an overdue `sandbox_job` row, and an older runner
  cannot answer — so until the runner catches up, the sweep leaves those rows
  open instead of guessing at them.
- **Rolling back has one manual step, and it goes first.** `tool_call.status`
  gained `claimed`, and the previous release raises on a status it has never
  heard of, failing the run that owns the row. The command that clears it
  ships in *this* release's `tool-worker` image and nowhere else — the version
  you are going back to has no such module and no such entry point — so run it
  **before** you revert any image, while `tool-worker` still resolves to this
  version:

  ```
  docker compose run --rm --no-deps tool-worker jhin-tool-calls-rollback
  ```

  If you have already reverted, it is still one command, just one that has to
  name the release you left: `JHIN_VERSION=<this release> docker compose run
  --rm --no-deps tool-worker jhin-tool-calls-rollback` for a tag-based deploy,
  or the same `docker compose run` from that release's bundle directory or
  source checkout. The ordered procedure is in `docs/deployment.md`
  (**Rolling back**).

  It closes every `claimed` row as a *failed* call carrying
  `rolled_back_before_dispatch`, which the old release replays as an ordinary
  failure. Failed rather than `executing` on purpose: `claimed` proves the
  executor was never entered, and writing `executing` would throw that proof
  away and hand an operator a pile of unknowns to reconcile for calls that
  provably did nothing. It is idempotent, guarded on `claimed`, and safe to
  race with a live worker. Nothing else needs undoing; see
  `docs/architecture/tool-worker-boundary.md`.
- **Run `tool-worker` and `sandbox-runner` against the same clock.** The
  re-dispatch interlock compares a timestamp written by one against a
  watermark held by the other, with a five-second margin that is for ordering,
  not for skew. The shipped topology gives them one clock — both are
  containers on a single Docker host — and nothing here changes for such an
  install. If you split them across hosts, keep those hosts NTP-synchronised
  to well inside five seconds: a `tool-worker` whose clock runs ahead is the
  case that produces a second container rather than a refusal. Details in
  `docs/architecture/sandboxing.md`.

## [0.1.0] - Unreleased

First public, self-hostable release candidate. Everything below was built
across implementation phases 1-10 of `docs/implementation-plan.md`; Phase 11
adds the open-source release artifacts.

### Added

- **Platform core (phases 1-3):** Docker Compose stack with PostgreSQL as
  system of record, NATS JetStream as event transport, and Temporal as the
  durable workflow authority; FastAPI control plane with cookie sessions,
  CSRF protection, workspaces, roles, and audit trail; first-run `/setup`
  onboarding; envelope-encrypted secret store (AES-256-GCM, per-secret DEKs,
  file-backed master key) with log redaction; model providers and priced
  model profiles; durable `AgentTaskWorkflow` agent runs with token and cost
  accounting.
- **Tool gateway and approvals (phase 4):** capability registry, tool
  definitions, per-agent grants with scoped dimensions, deny-by-default
  policy evaluation, approval policies, an approvals inbox, sanitized and
  audited tool calls.
- **Connectors (phases 5, 6, 9):** connector SDK (manifest, tools, schemas,
  webhooks) with GitHub, Linear, Vercel, Supabase (Management API and
  bounded SQL), and a CLI connector that runs jobs in ephemeral,
  non-root, read-only sandbox containers through the internal
  `sandbox-runner`; outbound endpoint policy with operator allowlists;
  fake GitHub, Linear, Vercel, Supabase, and OpenAI-compatible services for
  credential-free development.
- **Triggers and events (phase 7):** signed webhook ingestion with delivery
  dedupe, canonical event normalization on the event worker, a WHEN/IF/THEN
  trigger builder with filter DSL, dry-run explanations, and
  `TriggeredTaskWorkflow` task creation.
- **Delegation and teams (phase 8):** hierarchical organizations, manager
  relationships, grant-scoped `organization.delegate_task`, the
  `engineering_ticket` workflow template with implementer/QA routing and
  bounded fix-retest loops, per-agent concurrency limits, and queued-state
  visibility.
- **Production operations (phase 10):** a dedicated deterministic
  `tool-worker` on its own Temporal task queue that owns authorization,
  secret resolution, connector effects, and audit; the agent worker owns
  model reasoning only; rootful and rootless Docker-socket modes for
  `sandbox-runner` with fail-closed identity checks; protected health
  endpoints; structured JSON logging, OpenTelemetry traces and metrics;
  in-flight Phase 9 to Phase 10 workflow upgrade compatibility; the Phase 10
  live harness and regression suites.
- **Chat-first experience:** persistent, named conversations with every
  agent (`/chats`), a company activity feed (`/activity`), an Attention
  inbox (`/attention`), Agents and Company directories with profiles and an
  org map (`/agents`, `/company`), Automations and Apps views over triggers
  and connectors (`/automations`, `/apps`), and an Advanced area keeping
  every operational screen (`/advanced`).
- **Memory:** curated long-term memory per agent with candidate extraction,
  policy-based redaction of secrets, and review before promotion.
- **Avatars and media:** agent avatars with an image pipeline and
  multipart upload.
- **Coordination and oversight:** review policies, handoff/review/approval
  cards inline in chats, escalation visibility, and task lineage trees.
- **People and permissions:** single-use, expiring invitation links (no
  email dependency — the link is revealed once to share out of band), a
  documented four-role matrix enforced in one dependency, last-owner
  protection, and admin/owner authority rules
  (`docs/architecture/rbac.md`).
- **Scoped API keys:** `jhin_`-prefixed bearer keys with a granular scope
  taxonomy, a hard ceiling at the creating user's role, central per-route
  scope enforcement that fails closed, sealed credential endpoints, and a
  usage log with role-scoped visibility (`docs/architecture/api-keys.md`).
- **API reference and a versioned contract:** an in-app reference at
  `/api-docs`, rendered from the install's own OpenAPI document (served to
  signed-in users at `GET /api/v1/openapi.json`, so it is available in
  production where the anonymous `/docs` is not) and linked from the API keys
  page alongside the base URL, bearer header, and a runnable curl example;
  enriched OpenAPI metadata with described tags, both security schemes, and
  the scope every operation requires read out of the same table the API
  enforces; `api_version` on `GET /api/v1/health`; and a committed snapshot
  (`docs/api/openapi.v1.json`) diffed on every test run so a backwards-
  incompatible change to `/api/v1` fails the build
  (`docs/architecture/api-versioning.md`).
- **Open-source release (phase 11):** Apache-2.0 license metadata,
  community files (`CONTRIBUTING.md`, `SECURITY.md`, `CODE_OF_CONDUCT.md`,
  `SUPPORT.md`), issue and pull-request templates, CODEOWNERS, Dependabot,
  CI/E2E/Security/Release workflows, multi-arch GHCR image matrix with SBOM,
  provenance, Cosign signing, and Trivy scanning, the release Compose bundle
  under `deploy/`, the documentation set under `docs/`, and
  `scripts/release_preflight.py`.

### Security

- Secrets are never returned after creation, never logged, and never stored
  in `.env`; the master key is file-mounted only into services that decrypt.
- Only `sandbox-runner` can reach the Docker socket, always as a non-root
  identity; job containers never receive it.
- Production Compose publishes only the web and API ports; PostgreSQL,
  NATS, Temporal, and `sandbox-runner` stay on internal networks.

[Unreleased]: https://github.com/jhinhq/Jhin/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/jhinhq/Jhin/releases/tag/v0.1.0
