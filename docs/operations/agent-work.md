# Agent work: variables, Ghost, schedules and memory

Jhin stores this state locally in PostgreSQL and uses its existing encryption master key. Ghost integration, variables and schedules do not require Composio or a public Jhin callback domain. Keep the PostgreSQL backup and the installation's master key together in your normal protected backup process; neither alone restores encrypted values.

## Upgrade

Apply additive migrations 0048 through 0051 using the normal Jhin migration command before replacing API and worker containers. Build and update the API, agent worker, tool worker and web together. Migration 0050 adds bounded default capabilities only when an agent has no existing grant for that capability; existing explicit grants and denies remain authoritative. Migration 0051 pins each variable-backed Ghost key to its full Admin installation URL, including a subdirectory. Unverifiable existing pins fail closed and need explicit reconnection.

Take a database backup before upgrading. Downgrades that would discard populated variable/editorial state are refused; restore a matching backup and compatible application version when rolling back. Do not run two sandbox runners against the same Docker socket.

## Connect Ghost in chat

Provide an Admin API integration key, not a Content API key. A recognizable pasted key or a **Send a secret** input is encrypted before the chat message, task, journal or model input is saved. The agent receives an opaque reference. A missing Admin URL produces a required question and blocks dependent external work.

Use the actual installation address, for example `https://cms.example.com`, `https://example.com/blog`, or its `/ghost/` Admin page. Jhin does not infer it from the publication's public hostname. Explicit setup pins the full normalized URL before testing authentication. Production HTTPS endpoints follow the connector's existing network checks. For a deliberately private or HTTP Ghost installation, an operator must add that exact origin to `JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS` in both the API and tool-worker environment. This does not require making Jhin public.

You can ask to store a key for one agent, its team, or the company. If both Blogger and the Marketing Director need it, use their shared team scope. Existing private secrets can be copied to an authorized scope without displaying or pasting them again. The original remains unless explicitly deleted; future rotation is independent for each copy.

Shared variable changes require an explicit current admin request. Chat submitted through an API key retains that key's original role and scopes: `variables:write` is required for shared variable changes, and `apps:write` for Ghost setup. Revoked or limited keys cannot borrow the owner's broader permissions. Existing API keys do not automatically gain the new variable scopes; an owner can create a replacement under **Advanced → API keys**, selecting the needed scopes. Browser-only secret entry remains browser-only.

Specify who may publish, for example: “Only Marketing Director may publish.” The Apps connection form also offers a named publishing-agent selector and **Drafts only**. Only that current designated agent can approve and publish a revision created by another agent. Blogger's draft tools cannot set a published status. A content change requires a new review. A publish whose outcome is uncertain consumes its review reservation and must be reconciled before any new attempt; approving a generic work review does not bypass this rule.

## Manage values and schedules

Open an agent's **Variables** tab, or **Variables & memory** in Company or a team's context dialog. Ordinary values are readable and editable. Sensitive values expose metadata, replacement and deletion, with no reveal action. Edits use versions to reject stale writes. Removing a bound variable disables its connection and revokes its binding. Removing an agent from a team revokes use of that team's keys even if an old primary-team pointer remains.

Scheduled requests become durable schedule rows and Temporal workflows. The schedule records the brief, IANA timezone, local time, weekdays, next occurrence and execution history. The agent and Automations views support pause, edit and deletion. Pausing affects future occurrences; it does not stop a task already dispatched. A repeated local time runs once, a nonexistent local time is skipped, and overlapping occurrences are recorded as skipped. Each dispatched task keeps the brief from its own occurrence. API and tool-worker restarts do not create a second occurrence.

## Memory and previous chats

Agent-written memories need a supporting human statement or a verified native operation. Summaries expose source links, version and freshness, and exclude unsupported legacy agent notes from automatic retrieval. Those notes remain available for human review. Recognizable credentials in older chat text are redacted at model and public read boundaries. This read-time protection does not rewrite historical backups or provider-side logs; use the normal credential-rotation process for a key that was previously exposed.

Required questions remain unanswered until a valid reply or cancellation. An elapsed timeout is not permission to guess. Repeated unchanged failures are bounded, and routine delegation favors relevant teammates; an intentional cross-team request needs a reason.

## Verification

The release checks include real PostgreSQL migration and concurrent-write tests, real Temporal required-question/schedule tests, and isolated Ghost Admin API draft/review/publication scenarios through the actual tool gateway. Browser acceptance covers the live API's scope-aware values, secret replacement/copy, version conflicts, schedules, memory sources and mobile dialogs. See the implementation plan and test contracts for current acceptance status; a passing unit suite alone is not completion of model-driven acceptance.
