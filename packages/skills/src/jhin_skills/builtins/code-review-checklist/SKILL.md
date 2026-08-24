---
name: code-review-checklist
description: Review code changes methodically — correctness first, then safety, clarity, and tests. Use when reviewing a pull request, a diff, or another agent's code before it ships.
---

# Code review checklist

Review in passes, in this order. Stop and report immediately if a pass
finds a blocking problem — later passes are wasted effort until it is
fixed.

## Pass 1 — Correctness

- Does the change do what the task or issue asked, and only that?
- Trace the unhappy paths: empty inputs, missing records, timeouts,
  concurrent callers. Do failures surface, or vanish silently?
- Check boundaries: off-by-one, timezone handling, integer overflow,
  encoding, null versus empty.
- Look for state that can go stale: caches, memoized values, copies of
  data that the change now makes inconsistent.

## Pass 2 — Safety

- New inputs: is anything user- or model-controlled parsed, bounded, and
  validated before use?
- Secrets: no credentials, tokens, or connection strings in code, logs,
  errors, or test fixtures.
- Authorization: does every new read or write check the caller's access,
  or does it trust an identifier the caller supplied?
- Destructive operations: are deletes and overwrites guarded, audited,
  and reversible where the product promises reversibility?

## Pass 3 — Clarity and fit

- Would a newcomer understand each name and function without the diff's
  context? Flag names that describe implementation instead of intent.
- Does the change follow the codebase's existing patterns, or invent a
  parallel way to do something that already has a way?
- Is anything dead on arrival: unused parameters, unreachable branches,
  commented-out code?

## Pass 4 — Tests

- Is the bug or behavior in this change pinned by a test that fails
  without it?
- Do tests assert outcomes, not implementation details?
- Are the unhappy paths from Pass 1 covered, not just the happy path?

## Reporting

Report findings as: severity (blocking / should-fix / nit), location,
what is wrong, and why it matters. Suggest a fix when you can. Approve
explicitly when nothing blocks; never approve with unresolved blocking
findings.
