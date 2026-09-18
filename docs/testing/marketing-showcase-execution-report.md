# Marketing showcase execution report

This report covers the September 15–16, 2026 implementation and test session. The requested real Mindy/Ashley showcase is **not yet accepted end to end**. Ollama inference has recovered and the real workflow resumed; live connector issues found against the large production archive are being repaired before drafting. Completed engineering, provider, database and browser evidence is separated from that unfinished live outcome below.

## Deployed work

The local Jhin installation was migrated from 0051 through 0057 and rebuilt with the changes. A database backup was retained before migration. The existing dirty checkout was preserved on `codex/marketing-showcase`; changes have not been committed.

| Area | Resulting behavior |
| --- | --- |
| Editorial assignment | Persisted, versioned brief and explicit draft-only release intent; writes are restricted to the assignment's own draft. |
| Publishing authority | The designated publisher's immutable agent ID controls approval/publication across connections to the same Ghost installation. A generic owner review, another agent named Ashley, or an author's handoff cannot substitute. |
| Review | Complete immutable article/metadata/source package, chunked read receipts, requested revisions, stale-review refusal, bounded revision rounds, and a browser preview of the full saved content. |
| Team handoffs | Durable review-result delivery, persisted continuation claims, restart recovery, and release of the waiting author's only execution slot. |
| Archive | Durable published-post inventory, body coverage, bounded reconciliation, progress/checkpoints, authorized search and full-body chunks. Search is explicitly lexical; semantic originality is not claimed. |
| Images | Native Unsplash integration with secure binding, human search/selection receipts, source/attribution handling, revocation checks and durable tracking reservation. Ambiguous effects are not blindly retried. |
| Memory | Personal/team/company boundaries plus prospective human-admin policies for specified classes and actors. Earlier private information and secrets cannot be promoted into shared memory through this policy. |
| Secrets | Encrypted scoped variables, opaque references, binding/origin checks, rotation/revocation checks and guarded native connector consumption. |

Runtime fixes found during execution included an Ollama nested-schema incompatibility, an Unsplash credential timestamp transaction deadlock, an archive-start projection mismatch, missing Ghost icon registration, missing runtime-gateway redaction/tracing, and credential fragments exposed in rejected memory-tag validation errors. Each received focused verification. Strict backend validation and publishing authorization were retained.

The broad test runs also found stale capability/service inventories and scripted provider fixtures. Those were repaired separately from product behavior. The disposable Docker daemon initially bypassed its standard startup initializer; restoring that initializer fixed nested constrained-sandbox startup without changing the production daemon.

## Real provider and agent outcome

The existing Mindy and Ashley retain their configured `qwen3.8:latest` profile and their original identities. No replacement model was substituted.

- Mindy's setup task completed. She securely stored the Ghost key at Marketing team scope, bound and verified the supplied Ghost URL, listed actual posts, and saved verified connection references in personal memory.
- Mindy asked about topic/delegated angles, audience/purpose, tone/length, SEO, sources and review preference. That turn omitted image preference, so complete brief-gathering acceptance is still pending.
- Her subsequent task securely stored the Unsplash key and fetched official Ghost/Unsplash documentation. Repeated malformed nested brief arguments were rejected before any assignment or draft write. The Ollama adapter now expands bounded local schema references; the real-model retest remains pending.
- The same task later failed after repeated inference transport timeouts. A pause request was accepted but could not interrupt the already-pending reasoning activity before it failed.
- Operator-only provider reads at `2026-09-16T05:39:28Z` returned **4,498 published Ghost posts, zero drafts and zero scheduled posts**, and an authenticated Unsplash metadata response. These are connectivity/count checks, not Mindy's completed full-archive research.
- Repeated tiny inference probes returned no response data within 45 seconds. A new probe after the user's Continue message, at `2026-09-16T17:47:23Z`, still found Ollama management API version 0.33.2 responsive and no resident model in `/api/ps`. The configured server address differs from this desktop's reported IPv4 addresses; its underlying problem is unconfirmed.
- At `2026-09-16T17:52:33Z`, the native Ollama chat route also returned no headers within 45 seconds, while an intentionally nonexistent model returned a prompt 404. Both inference interfaces are affected; the evidence does not identify the remote host's root cause. No further retries or remote changes were made.
- Subsequent user-provided diagnostics show `nvidia-smi` failing on both the Ubuntu LXC and the Proxmox host. The host still enumerates an RTX 5090 (GB202, `0000:01:00.0`) with NVIDIA Open Kernel Module **580.119.02** bound. The earliest supplied kernel errors are **Xid 79, GPU has fallen off the bus**, and **Xid 154, 0x2 Node Reboot Required**, both at **September 15 22:18:35 in the host journal's unverified timezone**. Later repeated GPU-control RPC failures return `NV_ERR_GPU_IS_LOST`. The recovery action calls for a planned Proxmox host OS reboot after preserving diagnostics and arranging guest shutdown/migration. The underlying driver/PCIe/power/hardware trigger and any causal connection to Jhin/Ollama remain unproven. No host reset, reboot or driver change was performed by this execution.

**No production article was created, published, scheduled, or emailed by this execution.** A real isolated Ghost 6 instance passed connector-level draft/revision/review/director-publication tests using synthetic content. That does not establish a real-model writing/review rehearsal.

After the user rebooted the Proxmox host, their September 16 15:13:57 `nvidia-smi` output showed the RTX 5090 accessible again: 0% utilization, 0 MiB allocated, 39°C and no running GPU processes. This verifies idle host recovery, not sustained inference stability. Follow-up connections from Jhin's desktop to `192.168.1.79:11434` were actively refused, so the planned single small Qwen readiness request was not submitted. Container GPU access and Ollama service readiness remain to be checked.

The next user-provided deployment log identifies Ollama as Docker-managed inside the LXC. Docker refuses startup because CDI's `/dev/nvidia-uvm` source is not a device node. Subsequent diagnostics confirm valid character devices on Proxmox (`508:0` and `508:1`) but zero-byte, mode-000 regular files at the same paths inside `proxmox-ai-lxc`; `findmnt` shows only the LXC's `/dev` tmpfs. This establishes a host-to-LXC device exposure failure. A clean shutdown/start of the affected LXC, now that host nodes exist, is the next recovery step; its outcome remains unverified. The actual CT configuration and installed Proxmox version are needed before selecting a durable startup-order/device-mapping fix. Changing file permissions cannot turn regular placeholders into GPU devices. The earlier suggestion to inspect a native `ollama.service` does not apply to this Docker deployment.

The user's next LXC check at September 16 22:42:44 showed working `nvidia-smi` with an idle RTX 5090, while both UVM paths remained zero-byte regular files. The provided output does not establish that the requested LXC shutdown/start occurred. GPU monitoring access is restored inside the LXC, but Docker's UVM device prerequisite remains broken. The user explicitly requests a persistent solution because prior repairs regress after machine restarts. Acceptance now includes correct host-device initialization, persistent LXC mappings, Docker CDI readiness, and a small inference check after a host reboot; none of those reboot-persistence checks has yet passed. Awaiting the actual CT configuration and installed package/service details before prescribing host changes.

The user supplied CT 101's configuration and package versions: `pve-container 6.0.12`, Ubuntu LXC `proxmox-ai-lxc`, NVIDIA Container Toolkit 1.18.1. Its five GPU mounts are optional raw binds, with hardcoded cgroup majors 195/236/508. The LXC CDI refresh additionally fails its host-kernel module-file ExecCondition and reports an unmet nvidia-smi path condition. A scoped persistent repair is prepared in `proxmox-101-gpu-recovery.md` and its host/LXC scripts: early host device preparation, native device passthrough, LXC-compatible CDI checks and Docker startup ordering. Both scripts pass Bash syntax checks and the migration passes ten local mocked checks. They have not been applied remotely; a host reboot followed by successful inference remains required for acceptance.

After the user reported applying both repair scripts, live checks at `2026-09-16T23:02:11Z` reached Ollama **0.34.1** (previously 0.33.2): version, model inventory and loaded-model endpoints all returned HTTP 200; initially no models were loaded. One bounded `qwen3.8:latest` generation with thinking disabled, a 2,048-token context and 16-token output limit returned `OK` in **75.013 seconds** (53.668 seconds loading, 20.169 seconds prompt evaluation). Ollama reported **17,267,614,022 bytes / approximately 16.1 GiB** allocated entirely in VRAM. A second warm request at `23:04:08Z` returned the correct answer `4` in **0.547 seconds**. Both completed with `done=true`, `done_reason=stop`, HTTP 200. Requests used a 30-second keep-alive, without changing the persisted agent profile. Evidence: `.tmp/ollama-post-repair-metadata.json`, `.tmp/ollama-post-repair-inference.json`, `.tmp/ollama-post-repair-warm-inference.json`. Immediate inference readiness is restored; sustained workload stability and a further full-host-reboot verification remain untested.

Crash attribution remains open: Mindy's recorded run spans `2026-09-16T05:15:11Z` through `05:33:45Z`. The host's unqualified September 15 22:18:35 crash timestamp would overlap if the host journal was displayed in America/Los_Angeles, but that timezone has not been verified. An explicit UTC journal excerpt is needed. Xid 79 establishes lost PCIe access, not that utilization itself or Jhin caused the failure; saved evidence does not provide the pre-crash GPU process/temperature/power trace needed for attribution.

Safe live references:

| Object | ID |
| --- | --- |
| Marketing conversation | `01a0a87f-184c-7fb0-b549-3a2ae2734fa4` |
| Completed setup task | `01a0a87f-186f-7ea2-938e-795c73f09cf7` |
| Failed research task | `01a0a8a3-f287-7611-ac18-1201d16cffd8` |
| Ghost connection | `01a0a887-4469-7e52-ac59-c4c69150d280` |
| Research web connection | `01a0a8a1-19a7-7641-99de-93e2104620fa` |

Credentials are intentionally absent from this report. Reuse encrypted Jhin bindings rather than asking for or copying the keys again.

## Verification

The [55-scenario acceptance matrix](marketing-showcase-acceptance-matrix.md) is the detailed source for exact test paths, receipts, coverage and remaining limits. Counts from overlapping runs must not be added together.

| Gate | Recorded result |
| --- | --- |
| Frontend | 120 files / 1,355 tests passed; TypeScript passed. |
| Static backend | Final Linux-platform mypy passed for 691 source files; repository Ruff lint passed; OpenAPI snapshot current; `git diff --check` passed. |
| Actual isolated Ghost | Draft/revision/approval, writer denial, draft-only denial, stale review and designated-director publication passed. |
| PostgreSQL marketing integration | Archive scale, review continuation, image credential/tracking races and editorial contention passed in the extended harness. |
| Publication races | Three additional real PostgreSQL cases passed: grant revocation, concurrent publication, and ambiguous provider response without redispatch. |
| Migration | Actual 0051-to-head PostgreSQL upgrade preserved historical published/uncertain rows and denied publication from old unbound reviews. |
| Browser | Team/company policy creation/revocation, persistence through test-stack restart, full 65,456-character preview, script isolation, refresh, keyboard and mobile/desktop checks passed. See [browser evidence](marketing-showcase-browser-evidence.md). |
| Broad Linux suite | Final full invocation: **8,109 passed, 3 failed, 37 skipped, 178 deselected**, 19m59s. All three failures were test-runner `python3` PATH errors; after activating the runner venv, both affected files passed (**11 tests**, 2.94s). No unresolved failure remains from that invocation; it was not a single clean full-suite run. |
| Extended integration manifest | **69/69 passed**, 165.79s, no skips/xfails/deselections, including all **14 marketing scenarios**. Real isolated stack and deterministic model/provider fixtures. |
| Supported integration manifest | **28/28 passed**, 200.55s, no skips/xfails/deselections, on the same deterministic-provider build as the final 69-node run. |
| Model fixture regression | **368 tests passed** after the later deterministic review-script isolation change, separately from the broad unit snapshot. |

Browser fixtures, deterministic model responses, real PostgreSQL and real Ghost are different evidence levels. A passed component check does not prove live agent judgment, article originality, substantive Ashley feedback or successful production drafting.

An additional formatter audit found 89 files needing normalization: 77 newline-only and 12 layout differences. This continuation's Phase 5/8 layout changes were formatted and linted. Broader formatting in the pre-existing dirty checkout was preserved; a whole-repository formatter pass is not claimed.

## Work still required for live acceptance

1. Small Qwen inference readiness has passed after recovery. Continue observing the real configured Mindy/Ashley workflow; no replacement model has been substituted.
2. Run the configured Mindy/Ashley workflow against the isolated Ghost archive: complete typed brief, actual source research, a substantive change request, revision, exact-version approval and a retained approved draft. Exercise publication separately only on that disposable instance.
3. Resume the production conversation with the existing encrypted connections. Resolve the human image-search/selection question; complete archive reconciliation for the actual published inventory; inspect the closest article bodies and record the distinct contribution.
4. Have Mindy create one assignment-owned Ghost draft and Ashley review it through the actual team handoff. Finish any requested revisions, verify the saved article, images and metadata, and leave it as a draft.
5. Verify appropriate memories in a new authorized conversation, including restart/retrieval behavior, and retain the final draft/research/review evidence. Complete any remaining acceptance-matrix clauses with their actual evidence.

The production deployment uses the normal Compose files and the default empty additional-HTTP-origin allowance. A local ignored override exists for an isolated Ghost rehearsal; it is not active. The two temporary UI forwarding containers were removed. The outer isolated Ghost and PostgreSQL containers were stopped with their data retained, and Ghost was detached from `jhin_edge`.

The supported leased cleanup removed the inner test containers and project networks, then refused its final clean-daemon check because 48 sandbox workspace volumes have no lease-project label. Those volumes and the valid lease record were retained; ownership checks were not weakened. Both test helpers and the dedicated Docker daemon are stopped. The daemon is not an empty reusable test environment: a future harness run needs a fresh dedicated daemon or explicit reconciliation of the retained fixture volumes. Test logs and the pre-migration database backup remain available locally.

Final operational verification at `2026-09-16T18:12:08Z` found all 12 ordinary services healthy on migration 0057. Mindy/Ashley retain their original identities, Qwen profiles and reporting relationship. The failed research run has no pending tool calls, Temporal activities or child workflows, and no assignment, archive sync or handoff awaiting execution. Its Temporal wrapper closed successfully while its business task correctly remains failed.

A post-cleanup check again confirmed all 12 ordinary Jhin services healthy and all five retained `jhin-showcase-*` containers stopped.

## Resumed live run after GPU recovery

At `2026-09-16T23:08:38Z`, the user's renewed instruction started task
`01a0ac7a-b5c8-72d2-a4ce-83fd91266ff8`, run
`01a0ac7a-b71a-7be3-b276-66f0efab8938`, in the original conversation. Mindy
created real assignment `01a0ac7b-8ed8-7330-9bdc-d58ce04da2dd` with
`release_intent=draft_only` and the original writer/publisher IDs. This is live
evidence that the previous nested-brief schema issue is resolved.

Two live defects were found before article creation:

- Ghost's valid full-post responses exceeded the shared 512 KiB transport cap,
  producing misleading `ghost_http_200` errors. Authenticated operator metadata
  checks confirmed 30 posts = 1,128,823 decoded bytes and 100 posts = 14,189,214
  bytes, both valid HTTP 200 JSON and a total of 4,498 posts. A Ghost-only 16 MiB
  bound with truthful error classification and smaller, HTML-only archive pages
  is being tested. The shared provider bound remains unchanged.
- Unsplash connection intent detection treats an unrelated negation elsewhere
  in a human message (such as "do not publish") as a denial of the affirmative
  connection request. A clause-aware fix is being tested while retaining the
  human-origin and current-authority checks.

Two attempted archive syncs failed before indexing any articles; no archive
coverage claim is valid yet. Mindy asked real required human question
`01a0ac7d-d2ff-7111-8618-d9fbfff31007` (`unsplash_search`). It has been relayed to
the user, and no answer has been fabricated. Production article creation and
the live Ashley review chain remain pending.

The fixes were deployed to the five Python application services using the
ordinary Compose files; all five passed their health checks. Verification:
108 Ghost/shared-HTTP/editorial/setup/corpus tests, 57 Unsplash tests, four
archive-worker tests and seven corpus/workflow tests passed (these sets overlap
and must not be added together). Focused Ruff/format and source mypy checks
passed. A real read through the patched Ghost client returned 20 published
posts, 639,066 full HTML characters, and the correct 4,498-post inventory total.
This is a successful page read, not a full archive coverage receipt.

Mindy's required `unsplash_search` question remains pending after the service
restart. Her run is `waiting_person`, with 20 of 24 steps consumed; a truthful
resume may be needed if it reaches the step budget. The repair instruction is
queued as a steering message. No native image answer or selection has been
invented, and no production draft has yet been created by this run.

### Delegated image answer and continuation defects

Following the user's instruction to handle the remaining work, the operator
selected the search phrase `creator workspace` and answered the persisted
`unsplash_search` question through the authenticated owner API at
`2026-09-17T00:22:15.887Z`. This was an operator choice under user delegation,
not a claim that the user personally selected a photo. The API accepted the
answer and resumed the existing run.

The next model step exhausted its context: 32,608 input tokens and 159 output
tokens against the Ollama runner's 32,768-token context. The provider returned
`finish_reason=length` and an incomplete sentence, but the task was incorrectly
classified as completed. No new connector work or production draft resulted.
Input budgeting and truthful truncation failure handling are being repaired.

Investigation also found that both ordinary successor turns and failed-turn
resume create new task records without preserving the editorial assignment
association. That prevents new research and image-answer receipts from being
attached to the existing assignment. A trusted, workspace/conversation/writer
scoped continuation fix is being tested; no task state or evidence has been
manually rewritten to bypass this boundary.
