# Mindy research and streamed replies — September 13, 2026

The owner reported a blanket refusal of OnlyFans/adult-industry blog research and streamed responses disappearing. Work was investigated against the running Jhin HQ installation, with separate browser fixtures to avoid changing the owner's login or app permissions.

## Findings and changes

- The stored Mindy prompt was empty; no platform or persona rule prohibited non-graphic adult-industry business research. The live model repeatedly treated its earlier refusal as a standing instruction. Platform preamble version 21 now distinguishes the requested output from an industry label, asks agents to correct unsupported earlier refusals, and retains actual applicable restrictions.
- Default agents had no public-web reader after the reset. Jhin already supports keyless, read-only `web.fetch`; restoring production access is a permission change and remains pending owner approval. No production web grants or connections were changed.
- The first tool-free candidate was being streamed even though reasoning deliberately reviews it before committing an answer. That internal candidate now retains empty public text. Actual tool commentary and the selected answer continue to publish through the existing redactor.
- The UI discarded generation text as soon as generation completed, before the separate saved message arrived. Completed public text now stays in the ordered timeline until the authoritative message replaces it. Tool-step commentary remains beside its actions. Failed, cancelled, superseded and removed attempts are excluded.
- Removed plain-text agent messages retain an identity-only receipt in the journal. This suppresses the earlier generation on live updates and reload without retaining deleted message content.
- Real branch testing also found that file-free conversations were sent through a preview validator that required files. Empty branches now succeed after source authorization; empty previews and missing/foreign sources remain rejected.

## Automated verification

111 backend tests and 62 frontend tests passed across platform/context composition, generation/retry/cancellation/redaction, journal snapshots and replay, actual chat-page handoffs, polling reconciliation, and runtime branch controls. The new disappearance, candidate-publication and empty-branch regressions failed before their fixes. Production agent-worker, API, web and runtime-gateway images built successfully and were deployed. No migration was needed.

## Main-installation live checks

Using the deployed Qwen3.8 model and real Temporal workers:

- A branch containing the original refusal history accepted non-graphic business research, explicitly corrected the earlier refusal, and returned three topic ideas. Task `01a09e46-0034-73c2-828b-f1b3e4e85eeb`; saved reply `01a09e46-a454-7502-a934-d7be23a04dd6`.
- A fresh chat using the original OnlyFans/adult-content topic wording returned article angles and an informative introduction. Task `01a09e46-135a-7051-a853-1b4250372897`; saved reply `01a09e47-461d-7f10-b3c8-90a90d20572c`.
- Both superseded candidates retained zero public characters. Both selected replies were persisted. Neither test made tool calls, scheduled work, published content, saved memory, or contacted colleagues. Generated general-knowledge factual claims were not independently validated; these cases test refusal and response lifecycle, not publishable research accuracy.
- After the empty-branch correction, a real branch copied all 10 source messages successfully. The test branches/chats were archived; the owner's original conversation remains active.
- The final production audit confirmed all 12 main services healthy, the original agent/model configuration intact, all three regression chats archived, and no new live schedules, connections or public-web grants from testing.

## Authenticated browser checks

The production browser was at login. A separate installation at `127.0.0.1:3020` uses the same built web/API/agent images, its own database, NATS and Temporal namespace, an ordinary test-owner login, and the same local Qwen endpoint. Its test agent has only a blog-domain public reader. No additional sandbox runner or production login was created.

Both final browser scenarios passed using normal UI login, real model streaming, real workers and unmocked APIs:

| Scenario | Evidence | Result |
| --- | --- | --- |
| Non-graphic OnlyFans/adult-industry business writing | Chat `01a09e52-c7ad-7e12-bd86-b9c05e2e39ee`; 78 DOM samples; browser network disabled during streaming, then restored | Zero observed disappearance gaps, exactly one saved reply, identical reply after refresh |
| Read the public Fanclan blog, summarize published titles, suggest different topics | Chat `01a09e58-1ff2-7f31-a34b-77d367c2857c`; 47 DOM samples; actual `web.fetch` | Zero observed disappearance gaps, exactly one saved reply, identical reply after refresh; no blanket topic refusal |

Both replies were inspected at desktop size and at a 390 × 844 mobile viewport. The saved response was scrolled into view and showed no horizontal overflow. The public-blog reply identified the AdmireMe, Passes and Clips4Sale comparisons and acknowledged that a homepage read cannot establish uniqueness across the entire archive. This is not a full editorial accuracy or archive-deduplication acceptance test.

The public-blog task and run completed with exactly one action: a successful `web.fetch` of `https://blog.fanclan.io` in approximately 1.28 seconds. Its returned text contained all three comparison titles. No other actions ran.

The successful cases and screenshots are retained under `.tmp/mindy-browser-evidence/`; the repeatable driver is `.tmp/mindy-browser-check.cjs`. These test artifacts are local, not production workspace data.

Preliminary runs exposed three fixture/harness errors: an ambiguous response selector, a fixture-only 3,000-token output cap, and a directly seeded keyless connection missing the encrypted empty credential object that normal API creation provides. Each was corrected before the final scenarios above. The connection failure occurred before any network request; its original `execution_unknown` record was preserved, and a new read-only test turn was used. Preliminary runs are not counted as successful acceptance scenarios.

After verification, all seven isolated test services and the disposable test database were stopped; containers, volumes and evidence were retained. All 12 main services, including the sole sandbox runner, remained healthy.
