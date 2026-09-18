# Scheduling and tool-input regressions — September 13, 2026

**Subsequent owner-requested reset:** The live chats, schedules, regression agent, memory, app connection, and runtime files described below were later deleted. Conversation links are historical identifiers and no longer open retained evidence. Ashley, Mindy, Marketing, and the saved model configuration were preserved. Verification after service restart confirmed all 45 reset data tables were empty, model credentials and agent configuration were unchanged, and the workspace's old Temporal histories and broker events were absent.

The owner reported that a posting preference created an enabled publishing schedule without a destination or complete brief, and that Monday was stored as Tuesday. The same conversation showed invalid directory inputs and rejected memory proposals.

## Production correction

The existing `Monday blog publish (9:00 AM PT)` schedule in Jhin HQ was paused and corrected to Monday (`weekdays=[0]`, version 2). It had zero occurrences. No existing chats, agent names, app connections, or model configurations were removed.

## Changes

- A cadence statement is treated as a preference. Agent-created schedules are paused proposals; an authenticated owner/admin must approve the exact saved revision before activation. The review includes the full brief, named weekdays, timezone, and computed local date. Human schedule API operations remain compatible.
- Editing a schedule invalidates earlier confirmation. The gateway checks the current revision, original and current human authority, explicit option, conversation, and agent. Locked reads refresh cached state before checking the revision.
- Agent weekday schemas use names; legacy numeric/API values still use Monday=0. Returned dates identify the actual weekday rather than requiring model calendar arithmetic.
- Directory tools accept a bounded 25 results and resolve workspace-local team names as well as UUIDs. Missing or ambiguous teams produce actionable errors.
- Rejected memory paraphrases offer an exact human excerpt with source-message provenance. They do not add a timezone interpretation, editorial workflow, or publishing authority.
- Live testing uncovered overly small question limits. Ordinary questions now allow 1,000 characters and supplementary context 2,000, without truncation. Reserved schedule reviews discard model-written question text before validation and render from the saved schedule. Reviewed details remain accessible after answering.

## Verification

447 targeted backend tests passed, covering prompts, directory and organization tools, memory, question validation and answers, schedule authority, revision conflicts, and Temporal scheduling. All 23 question-card component tests passed. Production web and Python images built successfully. No migration is required beyond the installed additive schema.

Live checks used Qwen3.8, the real PostgreSQL database, and the deployed Temporal/agent/tool workers. A hidden `Schedule Regression` agent copied the relevant role/model settings without app grants. The local operator invoked the same conversation/answer services used by the API; these were real model executions, not mocked tool transcripts. Browser sign-in was unavailable in the connected browser; UI verification here consists of component tests and the production build, not a claimed authenticated browser run.

The exact message `We post blogs on 9am PST on Mondays` asked for memory scope, saved the original wording privately after that answer, completed without tool errors, and created no schedule. [Retained conversation](http://localhost:3000/chats/01a098e0-0820-71d3-a8f2-a82a13e5d04b).

A fully specified Monday reminder stayed paused until its activation choice was answered, then activated for **Monday, September 14, 2026 at 09:00 PDT**, and was paused through a subsequent natural chat instruction. The first run exposed the question-length errors described above, which led to an additional fix and retest. [Initial diagnostic conversation](http://localhost:3000/chats/01a098e1-57b2-7d91-b3fd-ae4460471aa7).

The repeated reminder test completed with four successful calls (list, create paused, ask for activation, activate), zero invalid inputs, Monday stored as `0`, and the correct Monday date in the final reply. [Successful approval retest](http://localhost:3000/chats/01a098ee-54a9-7f62-8540-54d963a0ff34).

The natural directory query supplied `team_id="Marketing"` and returned Ashley and Mindy in one successful call. [Directory check](http://localhost:3000/chats/01a098eb-f12e-7620-b7c9-eecd1bd5965f).

The incomplete publishing request created no schedule. It asked for the timezone, then continued waiting for the actual work brief and Ghost URL after an explicit timezone answer. One later model call invented an unsupported `ask` field; the gateway correctly rejected it and the model corrected its call. Unknown fields remain invalid rather than being silently discarded. [Setup check](http://localhost:3000/chats/01a098e9-96a6-7ad2-adb2-ddd76abdf615).

This setup check also exposed invalid timezone-choice aliases. The subsequent tested fix uses valid typed option values for answers and canonical labels; invalid timezone/time/URL aliases fall back to free text. API tests cover selecting actual typed values, boundary validation, and retained answer labels. This final adjustment was deployed after the live scenarios.

All test schedules were paused before any occurrence. The regression agent was disabled and removed from Marketing; its chats were archived with evidence retained. Mindy and Ashley remain active, and the owner's original schedule remains paused on Monday.

Human review confirms the completeness of arbitrary standing briefs; it does not supply credentials or replace connector authorization and editorial restrictions. Incomplete setup must still be requested by the agent, and existing required-input checks block dependent work.
