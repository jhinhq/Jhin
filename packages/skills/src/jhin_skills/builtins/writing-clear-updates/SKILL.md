---
name: writing-clear-updates
description: Write concise, skimmable status updates — for standups, task handoffs, and progress reports. Use whenever you report progress to a person or another agent.
---

# Writing clear updates

A good update lets the reader decide something in under thirty seconds.
Optimize for their next action, not for showing your work.

## Structure

Lead with the state, then the substance, then what you need:

1. **Headline** — one sentence: what happened and whether it is on track.
   "Login bug fixed and deployed to staging; on track for Friday."
2. **What changed** — two to five bullets. Facts, not narration. Include
   numbers where they exist (tests passing, items processed, time spent).
3. **Blockers or risks** — name the blocker, who or what unblocks it, and
   what happens if it stays blocked. No blockers? Say "No blockers."
4. **Next** — the single next step you will take, with a time expectation.

## Rules

- Never bury bad news. If something failed or slipped, it goes in the
  headline, with the recovery plan in the body.
- Write "done", "in progress", or "blocked" — never "mostly done" or
  "almost there". Percentages invite false precision; states invite action.
- One update, one topic. If two workstreams need reporting, write two
  short sections with their own headlines.
- Link or reference the artifact (task, pull request, document) instead of
  restating its content.
- Cut every sentence that does not change what the reader knows or does.

## Anti-patterns

- Chronological diaries ("First I looked at... then I tried...").
- Hedged states ("should probably be working now").
- Asking a question in the middle of a paragraph where it will be missed —
  questions and asks belong in the blockers section, explicitly addressed.
