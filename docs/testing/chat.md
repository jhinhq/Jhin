# Chat: what must work, and what proves it

Every behaviour the chat experience depends on, each mapped to the test that
would fail if it broke. Where nothing automated covers a row, it says so
plainly — an unmarked gap is how the ordering bug below shipped.

## Why this document exists

Agents were answering the **previous** question in a chat. Turn 2 replied with
turn 1's context; turn 3 repeated turn 2's answer word for word.

`build_messages` put the current question at index 1 and appended the
conversation history *after* it, while the worker deleted the current task's
seed user message because it matched the task description. Neither half was
wrong on its own, and each was tested on its own. Together, the newest user
message reaching the provider was the previous turn's question.

Nothing asserted the message list handed to the model. That is the gap this
suite closes first.

## The invariant

> The newest user turn is the **last `user`-role message the provider sees**,
> and on a tool-using step it is the last user turn before that step's tool
> transcript.

A chat turn composes no `Task: …` brief — the seed message already carries the
question, in its chronological place. A work task (assigned, trigger-started,
delegated, work request) still composes its brief **first**, because there the
description frames the run rather than being the latest thing somebody said.
The shape is chosen on `task.metadata_json["origin"]`, never on
`conversation_id`. See `docs/architecture/conversations.md`.

## Running

| | |
| --- | --- |
| Fast gate (offline, seconds) | `uv run pytest -q` and `pnpm test` |
| Browser specs (needs the dev stack) | `make test-e2e` |
| Live stack scenario | `make test-integration PHASE10_MODE=desktop` on macOS |

`make test-e2e` needs browsers once: `pnpm --filter jhin-web exec playwright
install chromium`. It checks and tells you rather than failing obscurely.

## The prompt handed to the model

| Behaviour | Proof |
| --- | --- |
| Newest user turn is last, across several turns | `services/agent_worker/tests/test_prompt_message_sequence.py::test_three_turn_chat_hands_the_newest_question_last` — runs the real activity on real rows and asserts the verbatim sequence |
| Same, through the whole running stack | `apps/web/e2e/chat-turn-order.spec.ts`, `tests/integration/test_conversation_turns.py` |
| A chat turn composes no brief | `test_prompt_message_sequence.py`, `packages/agents/tests/test_context.py::test_a_chat_turn_ends_on_the_newest_user_message` |
| A work task keeps its brief first | `test_an_assigned_work_task_keeps_its_brief_first`, `test_work_task_brief_precedes_history` |
| The question precedes this step's tool transcript | `test_chat_turn_question_precedes_this_steps_tool_transcript` |
| A work request inside a chat keeps its brief | `services/agent_worker/tests/test_conversation_history.py::test_a_work_request_inside_a_chat_keeps_its_brief` |
| A missing seed row falls back to the brief | `test_a_chat_task_whose_seed_row_is_missing_keeps_its_brief` |
| Seed message matches the description byte for byte | `apps/api/tests/test_conversations_unit.py::test_seed_message_carries_the_request_verbatim` |
| Earlier turns are capped, with the omission marked | `test_conversation_history.py` message and char cap tests |
| A failed earlier turn is marked unanswered | `test_a_turn_whose_run_failed_is_marked_unanswered` |
| A mid-run instruction reads as plain language, once | `test_a_mid_run_instruction_reads_as_plain_language`, `test_a_live_instruction_is_composed_once` |
| The agent knows who it is talking to, and the date | `test_the_agent_is_told_who_it_is_speaking_to_and_when`, `services/agent_worker/tests/test_situation_context.py` |

**The oracle.** The fake provider replies with whichever user message it saw
*last* (`packages/models/src/jhin_models/testing/fake_openai.py`). So a live
test asserts what the model received, not what the app believes it sent.

**Two limits of that oracle, stated so nobody trusts it further than it goes:**
it only ever exposes the last user message, so a brief reintroduced *earlier*
in the list leaves the browser specs green — that half is held by
`test_prompt_message_sequence.py`. And once any tool result is present the fake
stops echoing, so the tool-step half of the invariant has no browser oracle at
all.

## Turn handling

| Behaviour | Proof |
| --- | --- |
| A turn with no active task starts a new one | `test_conversations_unit.py::test_second_turn_after_completion_starts_a_new_task` |
| A turn during a live run is delivered as an instruction | `test_second_turn_while_task_active_is_an_instruction`, `apps/web/e2e/chat-live-controls.spec.ts` |
| A turn to a paused task is still an instruction | `test_a_turn_to_a_paused_task_is_still_an_instruction` |
| Replaying a `client_turn_id` creates nothing | `test_client_turn_id_is_idempotent` |
| `newTurn()` mints a fresh id per call | `apps/web/tests/chat-turn.test.ts` |
| A turn after a failed turn starts a fresh task | `test_a_turn_after_a_failed_turn_starts_a_new_task` |
| Archived conversation and paused agent are refused | `test_archived_conversation_and_paused_agent_are_409` |

## Controls while a run is live

| Behaviour | Proof |
| --- | --- |
| Stop ends the run and says so in the transcript | `apps/web/e2e/chat-live-controls.spec.ts` |
| Pause records the paused state, so Resume appears | `apps/api/tests/test_task_pause_state.py`, `apps/web/tests/chat-context-panel.test.tsx` |
| A finished run keeps its outcome against a racing signal | `test_state_is_only_written_from_the_state_it_was_read_in` |
| Reloading mid-run recovers from the server | `apps/web/e2e/chat-live-controls.spec.ts` |
| Steering mid-run creates no second task | same |

`FAKE_PROVIDER_LATENCY_MS` (see `compose.dev.yaml`) holds a run open long enough
to exercise these by hand; without it runs finish faster than a person can click.

## What the reader sees

| Behaviour | Proof |
| --- | --- |
| Markdown renders; user text stays literal | `apps/web/tests/chat-components.test.tsx`, `apps/web/e2e/chat-markdown.spec.ts` |
| `javascript:` links are refused | `chat-components.test.tsx` |
| An agent-to-agent exchange stays folded | `apps/web/tests/chat-work-request.test.tsx`, `apps/web/e2e/chat-exchange.spec.ts` |
| The agent that asked a colleague answers with what it heard | `packages/workflows/tests/test_work_request_task_workflow.py::test_the_requester_waits_for_the_colleagues_answer` (it waits), `services/agent_worker/tests/test_conversation_history.py::test_a_colleagues_answer_reaches_the_requesters_next_step` (it can read the answer) |
| A colleague who does not answer in time is reported, not invented | `test_a_colleague_who_does_not_answer_in_time_is_reported_not_invented`, `services/agent_worker/tests/test_coordination_activities.py::test_an_unanswered_request_leaves_a_mark_the_requester_can_read` |
| The primary agent's own turns stay in the dialogue | `chat-work-request.test.tsx` |
| Queued instructions show as queued, then delivered | `chat-helpers.test.ts`, `chat-components.test.tsx` |
| An empty completion never becomes an empty bubble | `services/agent_worker/tests/test_step_projection.py` |
| Title, rename, pin and archive | `apps/web/tests/chat-header.test.tsx` |
| A draft survives the new-chat redirect | `apps/web/tests/chat-turn.test.ts` |
| A picker chip for an unusable agent is refused | `apps/web/tests/chat-agent-picker.test.tsx` |
| The header stays readable at 375px | `apps/web/e2e/chat-mobile.spec.ts` |

## Not covered by automation

Honest list. These need a person, or a model with judgement.

- **Whether the answers are any good.** The fake provider echoes; it cannot
  reason. Conversation quality, tone, and whether an agent uses the right tool
  are only observable against a real model.
- **Whether an agent knows its team and its manager.** The roster reaches the
  prompt and that is tested, but whether the agent *uses* it needs a real model.
- **Memory quality** — that the right things are remembered, that near-duplicates
  do not accumulate, that team memory is shared and personal memory is not.
- **Tool-step prompt ordering in a browser** — no oracle, as above.
- **Attachments in chat.** Not built.

## Known flakiness

- `apps/web/e2e/fixtures/live-run.ts` sizes its window from one startup latency
  probe plus a fixed per-step constant. The reply budget is now 90s against an
  ~18s window, so a loaded machine has room; if the probe's assumptions drift
  far enough, a slow run could still overrun it.
- The Next.js rewrite turns an upstream `ECONNRESET` into a `500` the API never
  saw. Provisioning writes retry twice on a 5xx or a dropped connection, which
  covers the observed hiccup; a sustained proxy failure still fails the spec.
- `tests/integration/test_phase10_tool_worker_boundary.py` fails if a Docker
  image build runs concurrently. It passes in isolation.
