# Live agent acceptance — September 10, 2026

Status: verification complete for the requested real-work workflows. One
oversized repeated-text probe produced no usable Ollama response and is
recorded below as a limitation, not a passing check.

This records actual local Ollama runs through the running
Jhin API, Temporal workflows, tool worker, and sandbox runner. No fake model
markers or scripted model responses are used. The API credential is supplied
only through the acceptance driver's process environment and is not recorded
in this report or the driver.

## Evidence and acceptance requirements

Detailed request bodies, actual tool-call inputs/outputs, run IDs, and messages
are saved locally under `.tmp/live-agent-20260910/`. Those files contain local
workspace information and are not intended for publication. The driver is
`.tmp/live_agent_acceptance.py`; it observes existing task IDs rather than
restarting them when a polling window ends.

The saved evidence contains 19 real Ollama runs and 110 tool attempts,
including failed baselines and intentional validation failures. Thirty-three
terminal commands actually executed: 31 exited 0, one intentionally exited 7,
and one exited 1 while reproducing the original non-finite-input defect.
These totals are diagnostic coverage, not a claim that every attempt passed.
The final required workflows below passed after their fixes.

| Requirement | Evidence | Current result |
| --- | --- | --- |
| Execute terminal commands and change directory | Task `01a08d0f-a41d-7732-9dd4-92d5b7de8c05`: two real commands, `pwd`, create directory, generate UUID, read it in separate call | Passed; both commands exit 0 |
| Preserve files across runs | Task `01a08d10-99af-77c0-9a60-f145e306d349` reads the same generated UUID in a new run | Passed |
| Create and test a useful deliverable | Same task creates a Python CSV-to-JSON sales-report CLI, input, unittest suite, README, and report; later review adds non-finite validation | Passed; 5 final agent-written tests plus independent checks |
| Independently verify persisted bytes | Read-only export of the five generated files from Bisby's persistent Docker volume; `.tmp/check_sales_report_artifact.py` | Passed; all 14 final checks, including decimal precision, negative/empty/malformed/non-finite input, and actual CLI execution |
| Edit an existing file and verify the change | Task `01a08e1a-8d67-75b1-ad32-41049ea2bffc`: read README, edit with exact old text and expected match count, read back | Passed; actual edit input uses text matching, not a read token |
| Recover from an unsuccessful command | Same task observes exit 7 and `passed=false`, then runs a separate successful command returning 56088 | Passed; final answer reports both correctly |
| Fresh observations after a prior chat reply | Conversation `01a08e19-b33a-7610-afd5-e4c30ddd8151`; separate task adds a marker between requests; follow-up task `01a08e1b-ab8c-7563-8791-e61f3c9b4543` makes a new file-list call and reports that marker | Passed |
| Authenticated Supabase MCP and GitHub tools | Initial task `01a08d13-34da-70d3-9164-fb43a345152d`; fixed-count retest `01a08e22-9480-7d71-81e2-613b2ebb38ae` | Passed; actual GitHub count 21 with no truncation, successful Supabase response has `is_error=false` |
| Agent-to-agent work request and answer | Fixed-context CTO task `01a08e21-e332-7350-8dc8-3a3e8a2d2309`, QA task `01a08e22-3205-7453-8136-3fa93a804fb8` | Passed; QA ran a terminal calculation and CTO relayed the actual result; existing grants unchanged |
| Blocking review, fix, and follow-up verdict | Final Bisby task `01a08e30-9643-7792-8176-7ae675a34530`; QA children `01a08e31-70c1-7c43-b22b-2ccf50e9e57b` and `01a08e34-e344-7721-bca9-9c46137d78be` | Passed: fail verdict, real source/test edits, five passing tests and CLI execution, then independent QA pass; parent resumed and completed |
| Validation feedback and recovery | Task `01a08e49-14cc-7c80-b61f-162a86613a64` | Passed: timeout 0 rejected with `greater_than (gt=0)`; timeout 30 executed and printed the expected marker; internal transcript preserves argument keys and contains no audit wrapper |

The generated report totals are West 17.45, East 10.30, North 7.00,
grand total 34.75 over 5 rows. The exported `report.json` SHA-256 is
`5cd654fa188473e8fc0a683253dc9928b4c89263c3fa47e46bc95b37edbb9b11`,
matching the agent's actual terminal output. The export was read-only, with
networking disabled. Independent checks ran copies of the exported files.
The corrected source SHA-256 is
`ed6ff0a333a22bbfbc7c243ae158e37a22b6a8d8989710fe58afc70eb4ae5d10`,
matching the actual file-edit output. Final exported files and independent
results are saved under `artifact-final/` and `artifact-verification-final.json`.

## Regressions found

1. An earlier request to run `ls` completed with zero tool calls but claimed
   current directory contents. The prompt's duplicate-call warning lacked a
   request boundary. Preamble version 16 now requires fresh observations for
   new execution/current-state requests while preserving protection against
   replaying completed writes. It is deployed; a natural follow-up request made
   a new tool call and found a marker absent from the earlier chat reply.
   This freshness rule remains in prompt version 17. Final prompt and
   composition tests: 37 passed.
2. QA's unpinned CLI grants advertised terminal tools without any usable
   connection ID. It searched memory and guessed invalid IDs until reaching
   its step limit. The deployed fix resolves compatible active connections
   from valid same-workspace grants. The unchanged work request then completed:
   QA used the correct connection in one real terminal call. Sum of squares
   1–1000 was 333833500, and its independently checked SHA-256 result matched.
   Fixed MCP/Composio input scopes are also checked before advertising a
   connection. The final tool-worker version is deployed; 100 focused tests
   passed, and an independent review compared 96 generated-schema scope cases
   with the runtime authorization evaluator. Existing grants were unchanged.
3. GitHub returned 21 repositories, but the final answer claimed 18.
   `github.repository.list` now returns an authoritative `returned_count`
   after authorization filtering and output limits. GitHub tests: 77 passed;
   deployed and live retest correctly reported 21.
4. A blocking code review delivered the correct failing verdict, but Bisby
   ended its task with a promise to fix the finding instead of performing the
   already requested edits and verification. The child transport and parent
   resumption worked. Prompt version 17 preserves the original task after a
   colleague's result and allows a review of corrected work. On the unchanged
   retest, Bisby edited the source and test file, ran all five tests and the
   CLI, and requested a second blocking review. QA independently reran the
   five tests, checked all three non-finite inputs, and returned `pass`.
5. A rejected oversized command was replayed to the model as the internal
   `_raw_arguments` audit envelope. The model subsequently copied that
   envelope into another invalid call. Schema feedback also omitted the
   advertised numeric limit. The fix includes safe numeric constraints and
   reconstructs argument syntax only from complete, already sanitized audit
   data. Truncated or malformed input becomes `{}` with an explicit omission
   note and guidance to use the declared tool schema. Audit records and
   execution limits stay unchanged; manifests are not used to restore text
   that the tool worker may have redacted. Error locations are also redacted.
   Both workers are deployed. Final regression suites passed 203 tests:
   80 gateway, 59 local projection, and 64 adjacent history/manifest/review/
   approval tests. Two projection tests needing separate Temporal/Postgres
   test services were excluded from that local run. Ruff, formatting, mypy,
   compileall, and diff checks passed. The final live invalid-timeout request
   passed: the model received the numeric constraint, corrected its call, and
   reported actual stdout. A scoped read of the persisted internal transcript
   confirms the rejected call retained its real argument keys, with no
   `_raw_arguments` wrapper. Evidence: `schema-timeout-projection.json`.
6. That live retest (`01a08e41-e35b-7261-9f80-a047a01d8c88`) exposed an
   older empty-response fallback: the first model call received 44 tools, but
   an empty response triggered a second call with no tools and instructions
   forbidding their use. The final answer falsely reported missing terminal
   access. The deployed retry now retains the same advertised tools and
   continues the original task, while remaining bounded to one retry. All
   25 reasoning/manifest tests pass, including empty-to-tool-call recovery,
   empty-to-answer, two empty responses, summed usage, and committed replay.

The repeated 4,133-character command test is **not a passing live case**.
After the fallback fix, task `01a08e47-9186-7341-961c-27a4becb751c` received
two empty provider responses and executed no tool calls. Jhin recorded an
`empty_completion` note rather than inventing a missing-permission reply.
The reported zero provider tokens are not treated as evidence of execution.
Truncation recovery is covered by the projection regressions; the shorter
invalid-timeout test exercises live numeric feedback and argument replay.

All 11 local Compose services were healthy at the final check. No tasks were
running or queued. The corrected agent and tool workers are active; no
migration was needed for these runtime fixes.

The Windows checkout test harness also inherited a Bash PATH without `awk`
and `sed`. The harness now probes and runs with the same POSIX utilities and
without user startup scripts. All seven real branch/checkout/push tests pass;
the actual Git behavior assertions are unchanged.

The independent deliverable verifier was extended before the code-review
scenario to reject non-finite monetary values. The original utility correctly
rejects malformed amounts and infinity, but accepts NaN; that failing evidence
is stored in `artifact-nonfinite-before.json`. The live blocking review detected
the defect, Bisby fixed it through real file edits, and the follow-up review
passed. Independent verification of the exported correction also passes all
14 checks, including NaN and both signs of infinity.

Invalid-argument cases in the creation and connected-app tests were recoverable:
the agent corrected a file read-token error and provider/tool schema errors.
These are recorded as attempts rather than counted as successful calls. A
completed MCP invocation is only considered a successful provider operation
when its response also has `is_error=false`.

## Operational notes

- Runs use the configured Ollama `muse-glimmer:latest` profile. Verify each
  run's persisted model profile and token accounting; a completed task alone
  is not evidence that it called tools or produced the claimed result.
- Sandbox files persist per agent. Shell process state does not persist:
  each command must explicitly change directory when necessary.
- Work between agents is verified through the work-request/child-task records,
  the recipient's actual tool calls, and returned results. Directory lookups
  alone do not count as communication.
- Real external tests use existing authenticated read tools. Local test files
  in Bisby's sandbox are confined to `jhin_acceptance_20260910`. QA recreated
  the supplied source and fixtures in its separate sandbox workspace root.
  No external resources are created, deployed, deleted, or pushed by these
  agent acceptance cases.

## App setup and access verification

New app connections perform their initial provider check before creation
returns, recording the check time and its actual outcome. MCP setup also
discovers tools. Failed checks retain the saved connection with a retryable
error. The focused initial-check suite passes all 13 cases.

The app detail page provides **Give to agent**, with agent selection and
read-only or all-tool choices. Grants stay pinned to the selected connection;
partial failures can be retried. Prior browser verification passed 37 cases
(24 app-card layouts and 13 assignment cases).

The final live access read confirms Supabase is active, has a recorded check
time and no connection error, and Bisby has 18 valid connection-specific
read-only grants. His successful `list_projects` calls confirm usable provider
access, beyond merely displaying those grants in the UI.
