---
name: meeting-notes-summary
description: Distill a meeting transcript or raw notes into decisions, action items, and open questions. Use when summarizing a discussion, a call transcript, or a long chat thread.
---

# Meeting notes summary

The summary exists for two readers: the person who missed the meeting and
the person who must act on it. Serve both in under a page.

## Output format

Produce exactly these sections, in this order, skipping any that are
genuinely empty:

1. **Decisions** — what was settled, one bullet each, phrased as the
   decision itself: "Ship the pricing change behind a flag on Monday."
   Include who decided when there was a clear owner.
2. **Action items** — `owner — action — due` on each line. An action
   without an owner is not an action item; move it to open questions.
3. **Open questions** — what was raised but not resolved, and who is
   expected to resolve it.
4. **Context** (optional, three sentences max) — only what a reader needs
   to make sense of the above.

## Rules

- Report only what was actually said or decided. Never infer a decision
  from the direction of the discussion — that goes under open questions.
- Attribute sparingly: name people for decisions and ownership, not for
  every remark.
- Collapse repetition. Ten minutes of circling one topic is one bullet.
- Preserve exact figures, dates, and names; paraphrase everything else.
- If the source is partial (cut transcript, fragmentary notes), say so at
  the top: "Summary of a partial transcript."

## Quality check

Before finishing, verify: every action item has an owner; every decision
would be recognized as such by the people in the room; nothing in the
summary is your own opinion.
