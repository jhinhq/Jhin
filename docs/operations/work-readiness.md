# Required inputs, schedules, and memory review

When an agent needs a prerequisite, its question remains open until someone answers or stops the task. Required questions support free text, including a confirmed destination URL, local time, or IANA timezone. The agent cannot continue dependent tool work while the prerequisite is missing. A delegated agent returns a blocked result and the missing inputs to its requester. An optional question may time out, but that does not authorize guessing a required value.

Ghost setup questions have fixed wording for the actual Admin URL and the agent allowed to review and publish drafts. Enter the exact requested URL or agent name/UUID. Sensitive answers are captured as opaque references; they are not reusable plaintext in chat. Existing pre-upgrade credentials are removed from model and public read projections without rewriting the old database records. Re-enter a legacy credential through sensitive input if it needs to become a configured connection.

A posting cadence is a preference, not permission to activate recurring work. Before asking an agent to automate, supply the standing brief, destination and audience when relevant, draft/review/publication authority, weekdays, local `HH:MM`, and an IANA timezone such as `America/Los_Angeles`. Clarify whether “PST” means fixed UTC−08:00 or Pacific local time with daylight saving.

Agent-created schedules start paused. Activation requires an owner/admin to review the exact saved brief and timing through the schedule confirmation question. Changing the saved work invalidates its earlier confirmation; agents cannot treat a conversational time answer or their own claim of permission as activation authority. Pausing and retiring remain immediate. Owners/admins can also manage schedules directly through the schedule UI. The confirmation does not provide app credentials or override connector permissions and editorial review requirements.

Agent tools accept named weekdays; the compatible API retains integers with Monday=0 through Sunday=6. Schedule output includes a calculated local next-run date and its weekday together. Each dispatched task retains the brief version it started with.

Keep the agent worker and Temporal running. Reconciliation checks saved schedules every 15 seconds; schedule workflows check their next occurrence at most 60 seconds apart. Restarting a worker reuses the recorded task instead of creating another occurrence. Runs that overlap are skipped and recorded. A spring-forward nonexistent time is skipped that day; a repeated fall-back time runs once at the first occurrence. Editing or pausing a schedule does not stop work already dispatched: use the task's Stop control for that. Retiring a schedule preserves execution history.

Memory summaries show up to 20 distinct supported current facts and link to their source records. Superseded and expired records are omitted. Agent claims about successful setup, shell logs, and temporary HTTP failures cannot establish durable facts. Older agent-created notes without supported sources remain visible to people with a **Source unverified** badge, but are excluded from automatic recall and summaries, including when pinned. Review the source and use **Review and save** to create a human-authored version, or forget an incorrect note. Saving unchanged reviewed wording is supported.

Current team membership controls team knowledge and relevant colleague discovery. A recorded departure removes access even if an older primary-team field still names that team. Cross-team delegation needs an explicit reason and existing permission. Repeating unchanged failed work twice blocks a third request, including switching to another colleague; provide corrected inputs before trying again.

## Upgrade and rollback

Apply the complete additive migration chain before starting upgraded workers. Migration 0049 adds required-question fields and durable schedules; 0051 pins a variable-backed Ghost connection to its full installation URL, including any subdirectory. Invalid or unmatched older URL bindings remain unusable until deliberately recreated. Existing explicit deny rules and custom capability scopes are preserved.

Back up PostgreSQL and the existing application file volume using the installation's backup procedure. Downgrades refuse to remove retained scoped variables, private secure captures, populated full-URL bindings, schedules or occurrence history, and required or structured question records. This includes retired schedules and answered questions. Roll back application images while retaining additive data, or plan an explicit export/removal before a schema downgrade; do not bypass these guards. Baseline grants added during upgrade remain recorded across downgrade and become inert where the older application has no matching capability.

For a repeatable migration check, create a separate **empty disposable database whose name contains `migration`**, set `MIGRATION_TEST_DATABASE_URL` to its asyncpg URL, and run `python scripts/verify_work_readiness_migrations.py` from the repository environment. The script refuses a nonempty database and never targets `agentic_test`. It performs upgrades and downgrades and leaves synthetic fixtures in that test database. Never point it at an installation database.

## Verification recorded on 2026-09-12

- 473 targeted tests passed across agent prompts, memory, required questions, collaboration, schedules, reasoning, API services, and the migration graph.
- 10 real Temporal tests passed for required-question waiting/cancellation and stable scheduled task dispatch.
- A real PostgreSQL concurrency test passed: duplicate creates and ticks produce one task, a restarted session reuses it, and a timezone edit changes future occurrences while retaining the active brief.
- A separate clean PostgreSQL database passed base→0051, downgrade→0047, and upgrade again, preserving deny/custom grants. Six Ghost URL fixtures verified subdirectory normalization and refusal of mismatched or malformed origins. Downgrades with populated bindings, retired schedules, completed occurrences, or answered required questions were refused; the schema revision and retained records remained unchanged.
- 194 worker invocation, reasoning-manifest, telemetry, and registration tests passed in a fresh process. Six focused rollback tests passed, including structured optional questions and compatibility with unchanged legacy optional questions.
- Actual chat-model and embedding-model request tests exclude recognizable legacy credentials, including one crossing a history truncation boundary, while preserving opaque references and the original stored records.

These tests complement the installation's live model, browser, and Ghost-server acceptance checks; they do not establish that a particular operator's provider credentials are configured.
