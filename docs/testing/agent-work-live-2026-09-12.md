# Agent work live acceptance — 2026-09-12

**Subsequent owner reset:** the owner later requested a clean Jhin HQ for personal testing. Its old chats, agents, teams, connections, variables, memories, schedules, files and execution histories were deleted, including these acceptance fixtures. The sample IDs below are historical and no longer resolve in the live installation. The workspace, login and saved model configurations were preserved; the owner is now Varand, with a fresh Marketing Director and Blogger using Qwen 3.8. This report records the tests performed before that reset.

This records the deployed local installation, using its existing configured model profile in place. Temporary Readiness Marketing agents use an isolated Ghost6 server. No production Ghost posts were created or published, and no provider credential was copied to another database.

## Verified workflows

| Scenario | Observed result |
| --- | --- |
| Ghost key without URL | Encrypted private variable created; a required actual-Admin-URL question appeared. No external tool call or delegation occurred before its answer. |
| Answer and verify | The supplied local URL was used by the native Ghost Admin client; verification and real post listing succeeded. No raw key appeared in public receipts. |
| Editorial work | Blogger created and corrected a draft. Only Readiness Director read, approved and published the exact reviewed revision. Blogger received the result and independently read the published post. |
| Invalid key | One real Ghost401 response; agent reported failure and stopped, without trying unrelated tools or colleagues. |
| Key with supplied URL | Final deployed intake accepted the explicit Admin URL without asking for it again. The model inferred an unconfirmed publisher; the server refused that assignment and requested clarification. After the owner selected Drafts only, verification produced one401 and the agent stopped. The existing working connection remained healthy. |
| Ordinary variables | Agent created, listed, updated with revision2, read back, and deleted its private setting. |
| Sensitive variables | Agent stored an opaque reference, replaced the value with a new secure input at revision2, and deleted it. No value was returned. Removing the bound value disabled its test connection. |
| Natural-language schedule | Agent created a daily02:21 America/Los_Angeles schedule, retaining the explicit read-only brief. |
| Schedule recovery | Worker restarted before the first occurrence. Exactly one task started at09:21:00 UTC and completed at09:21:08, reporting the current revision2 variable value. A later service replacement retained that same completed occurrence. |
| Schedule management | Schedule paused, changed to03:30 while paused, retired by the agent, and omitted from the active list. Its single completed occurrence remains queryable. |
| Memory | Verified URL and connection facts saved privately with source task/conversation links. Summary contained two supported current facts with a version and freshness timestamp. Unsupported compound claims remain rejected and now receive exact evidence-based recovery suggestions. |
| Existing connection identity | After the final image replacement, the live model rebound the private key, verified real Ghost access and retained connection `01a094e5-7a7d-74d3-927c-0acfd8fd4ab1` with its original display name. No publisher was added. The invalid test variable was deleted; no articles, delegation or memories were created. |
| Shared variables | With the replacement scoped API key, the model discovered the actual team/company IDs, copied the private secret to the team and then company, created ordinary settings, and bound both copied keys to real Ghost connections without asking for IDs or delegating. Copy receipts retain direct source IDs and versions. |
| Shared access and rotation | The Director read both shared ordinary values and used both shared connections. The agent replaced all four shared values at version 2; sensitive values remained absent. The original private key stayed at version 1 and still verified against Ghost. |
| Revocation and deletion | A Director request for Blogger's private value was refused once, with no retry. Removing Director's team membership removed the team values and scope from discovery while company access remained; membership was restored. The agent deleted all four shared values, disabling both bound connections. |
| Shared-key editorial work | Blogger drafted through the team-key connection; the designated Director read, approved and published the exact reviewed revision. Blogger read the published post. Seven completed tool calls, no errors or questions. |
| Explicit cross-team work | Blogger handed one bounded JavaScript question to Readiness Engineer in a separate Engineering team, with an explicit technical reason. The Engineer completed it, exactly one result returned, and Blogger replied afterward. No unrelated colleague, question, failed tool or external operation occurred. |
| Final reconnect and links | The exact natural wording “reconnecting its existing Ghost connection at…” verified in one bind without a URL question, preserving identity and private version 1. A fresh post read returned the exact public localhost URL rather than reconstructing the Admin hostname. |
| Final memory boundaries | Actual API POST and PATCH each rejected a synthetic credential in content, subject and tags without echoing it or its prefix. The original memory stayed unchanged; an ordinary edit created version 2. Both temporary probe records were forgotten. A real model saved and retrieved one exact human preference, and the current summary included its source evidence. |
| Status prose | After deployment, the real model's “The stored admin key is still valid and working.” reply remained readable. Explicit credential assignments, quoted values and known key formats retain redaction. |

The editorial sample contained one invalid list-limit request, rejected before provider access; the agent corrected it. It also corrected its draft HTML before requesting review. These are visible in retained execution history.

## Repeatable evidence verification

Set `JHIN_LIVE_API_KEY` in the process environment and run the read-only `scripts/verify_agent_work_evidence.py` with `--url`, `--workspace`, `--conversation`, and optionally `--key-only-conversation`, `--writer` and `--director`. It reads bounded, cursor-consistent history and verifies actor, exact revision, publication, parent resumption and the required-input gate. It prints status and identifiers, never credentials, message text or article HTML.

This installation's samples:

- Workspace: `01a050e9-bf9b-7501-89b7-acc85e4741a3`
- Credential-only conversation: `01a094e4-71c2-7043-9714-87d6f31105d5`
- Editorial conversation: `01a094e7-6d96-7951-8f16-9fb52388edcf`
- Writer: `01a094e4-4f84-7311-9e12-a58d7f9d64d0`
- Director: `01a094e4-4f66-7b32-9e06-235292c7a172`
- Editorial review: `01a094e7-ec21-7612-bfa1-a54517bd31dc`
- Retired schedule: `01a094e6-fe3c-7da0-a5a4-f1c660686303`
- Completed occurrence task: `01a094eb-8c8b-77a3-a7db-0fc3350e0e1e`
- Shared-credential editorial conversation: `01a09522-cf68-7c31-a595-93d3ddfbe16b`
- Shared-credential editorial review: `01a09523-0a1f-71b3-901d-4e7508338c03`
- Shared-credential published post: `6aa527acea30f00001966c11`
- Explicit Engineering conversation: `01a0953e-2fd5-7b22-ad11-4ed9bf5187b6`
- Engineering child task: `01a0953e-488b-7713-be3a-c4cc16e0137c`
- Final reconnect conversation: `01a09539-f4d4-79a0-94bf-dd26c166ab61`
- Public-link conversation: `01a0953a-350a-7891-9f9d-63bb48b1b97f`
- Human-preference conversation: `01a0953a-1171-7a62-960f-642f33060b74`
- Status-prose conversation: `01a0953e-d35e-71c1-90c2-24b4a2b14066`

## Additional verification

Real isolated browser/API scenarios passed three-scope variable CRUD, write-only replacement/copy, stale-write rejection, schedule controls/history, memory source/version review, responsive dialogs, and named Ghost publisher selection in setup and existing settings. The frontend suite passed1,339 tests and a production build. Backend regression groups include real PostgreSQL CAS, revocation, credential-use and approved Ghost-operation concurrency, clean and retained-data migration checks through0051, and real Temporal required-question/schedule recovery. Linux-only CLI/Git/recovery checks ran in Linux containers.

Connection identity uses the exact stored variable ID, preserving renamed and legacy display names. Same-named variables owned by different agents bind independently; pre-existing duplicate bindings return an explicit conflict before provider access and preserve both records.

Live acceptance exposed and corrected missing empty-list scope discovery, ordinary “create” wording for shared authority, natural reconnect wording, reconstructed result URLs and over-redacted status prose. A memory boundary review also corrected redaction after truncation, raw metadata copied into new versions/cards, and incomplete quoted-password redaction. Retrieval, embedding, extraction and adjudication now screen complete text before bounds or provider egress; retained legacy records and source IDs remain intact. No additional dependency or migration was needed for these corrections.

The final combined memory, intake, native Ghost setup and secure conversation ingress regression run passed **445 tests**. The separate OpenAPI compatibility/snapshot, evidence-verifier and agent-context group passed **50 tests**. Focused Ruff and strict mypy checks passed, and API/agent-worker/tool-worker images built successfully. These are targeted green groups; they do not claim that the earlier broad Windows repository run was entirely green.

After the final deployment, the retained editorial and missing-URL evidence verifier passed again, including after archiving the test chats. The retired schedule still has exactly one completed occurrence after the service replacements.

## Final installation state

**Inspection update:** after the cleanup described below, the operator asked to see the live chats. All 25 conversations were restored to the active chat list and verified visible through the API. Search Chats for **Readiness**. This changed visibility only; agents remain paused and no work was rerun.

The replacement API key supplied by the operator resolved the remaining shared-variable acceptance blocker. The original key's scopes were not changed. All required workflows above passed against the configured provider and local installation at migration **0051**; the final corrections are deployed.

The three temporary agents are paused, hidden and unavailable; four retained test connections are disabled; all **25** test conversations are archived, including the failed pre-fix case. The invalid and shared test variables were deleted by the agent. The encrypted private fixture, files, reviews, source memories and the single completed retired-schedule occurrence remain as evidence. Temporary acceptance containers are stopped with their data preserved. Normal API/tool-worker configuration is restored without the temporary Ghost HTTP-origin override. The sole sandbox runner was not replaced.
