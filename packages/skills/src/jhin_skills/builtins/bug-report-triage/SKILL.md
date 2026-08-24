---
name: bug-report-triage
description: Turn a vague bug report into an actionable, prioritized ticket — reproduction, impact, severity, and routing. Use when handling incoming bug reports or triaging an issue queue.
---

# Bug report triage

Triage answers four questions: what breaks, for whom, how badly, and who
should fix it. A triaged bug either becomes actionable or gets closed with
a reason — nothing stays vague.

## 1. Reproduce or bound it

- Extract the exact steps, inputs, and environment from the report. If a
  step is missing, state your best-guess reproduction and mark it as such.
- Try to reproduce if you have the means. Record the result either way:
  "reproduced on staging", "could not reproduce with the given steps".
- Capture the evidence: error message, request id, timestamp, screenshot
  reference. Evidence beats description.

## 2. Establish impact

- Who hits this: everyone, one role, one configuration, one user?
- What is lost: data, money, access, time, or polish?
- Is there a workaround? A one-line workaround can drop urgency a level;
  data loss with no workaround raises it.

## 3. Assign severity

- **S1 — critical**: data loss or corruption, security exposure, or the
  product unusable for many users. Escalate immediately; do not queue.
- **S2 — major**: a core flow broken with no reasonable workaround.
- **S3 — minor**: broken but avoidable, or a degraded experience.
- **S4 — cosmetic**: visual or wording issues with no functional impact.

Severity is about impact, not about who reported it or how loudly.

## 4. Route and write it up

Produce a ticket with: a one-line title stating the symptom ("Export
button returns 500 for workspaces with no members"), reproduction steps,
expected versus actual behavior, evidence, severity with one sentence of
justification, and the component or team best placed to fix it.

## Closing without fixing

Close as duplicate (link the original), cannot-reproduce (show what you
tried), or working-as-intended (explain the intent). Always tell the
reporter which of these happened and why.
