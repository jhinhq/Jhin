---
name: release-notes
description: Write user-facing release notes from a list of changes, commits, or merged pull requests. Use when announcing a release, a deploy, or a changelog entry.
---

# Release notes

Release notes are for users, not for the team that built the release.
Translate implementation into outcomes: what can the reader do now, what
should they do differently, what got fixed.

## Process

1. Gather the raw changes (commits, merged PRs, closed issues).
2. Discard internal-only work: refactors, CI, dependency bumps with no
   user-visible effect. If the release is *only* internal work, say so in
   one line rather than dressing it up.
3. Group what remains into: **New**, **Improved**, **Fixed**, and (when
   applicable) **Breaking changes** and **Security**.
4. Rewrite each item from the user's point of view, present tense:
   "You can now export tasks as CSV" — not "Added CSV export endpoint".
5. Order items by impact on the reader, not by merge date.

See `template.md` in this skill's files for the exact output skeleton.

## Rules

- Breaking changes come first, with the migration step in the same bullet.
  Never make the reader hunt for what they must do.
- Security fixes state the impact and the affected versions without a
  recipe for exploitation.
- One line per item; link the fuller reference (PR, doc) instead of
  explaining internals.
- Keep the tone plain. No exclamation marks, no "we're excited".
- Version and date go in the title. If versioning follows semver, the
  changes must justify the bump — flag it if they do not.
