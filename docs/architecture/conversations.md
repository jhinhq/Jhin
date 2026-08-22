# Conversations and Company Activity

Conversations make Jhin chat-first without replacing the task engine. A
conversation is a named, persistent thread between a human and one primary
agent. Every user turn that needs agent work becomes a task linked to the
conversation, so `AgentTaskWorkflow`, concurrency admission, approvals,
tools, delegation, and recovery all keep working unchanged. The company
activity feed projects agent-to-agent traffic (delegations, reviews, results,
escalations) and task lifecycle into human-readable cards.

This document is the implementation contract for the API, the worker, and the
web app.

## Data model (migration `0015`, down revision `0014`)

### `conversation`

| column | type | notes |
| --- | --- | --- |
| `id` | uuid pk | UUIDv7 |
| `workspace_id` | uuid fk workspace (cascade) | indexed |
| `title` | varchar(200) | editable; defaults to the first line of the first turn (≤120 chars) |
| `status` | varchar(16) | `active` or `archived` |
| `pinned` | bool | default false |
| `primary_agent_id` | uuid fk agent (set null) | the agent the user is talking to |
| `created_by_user_id` | uuid fk user (set null) | |
| `last_activity_at` | timestamptz | bumped on every visible message |
| `created_at`, `updated_at` | timestamptz | |

Indexes: `(workspace_id, last_activity_at desc)`, `(workspace_id, primary_agent_id)`.

### Additive columns

- `task.conversation_id` uuid nullable fk conversation (set null), indexed.
- `message.conversation_id` uuid nullable fk conversation (set null), indexed.

### Backfill

For every existing task with `metadata_json.origin == "message"` (the legacy
"Message an agent" flow), create one conversation titled from the task title,
`primary_agent_id = task.assigned_agent_id`, `last_activity_at = task.updated_at`,
and set `conversation_id` on that task and on all of its messages. The
downgrade drops the columns and the table.

## Domain vocabulary

`packages/domain`:

- `ConversationStatus`: `active`, `archived`.
- `ActivityKind` (feed cards): `started`, `asked_agent`, `reported`,
  `escalated`, `status_update`, `needs_review`, `finished`, `failed`,
  `paused`, `stopped`, `queued`.
- `ACTIVITY_LABELS`: the default human label per kind —
  `started` → "Started working", `asked_agent` → "Asked another agent",
  `reported` → "Reported back", `escalated` → "Needs help",
  `status_update` → "Shared an update", `needs_review` → "Needs your review",
  `finished` → "Finished", `failed` → "Ran into a problem",
  `paused` → "Paused", `stopped` → "Stopped", `queued` → "Waiting for a free slot".

## API (`/api/v1/workspaces/{workspace_id}/conversations`)

All routes are workspace-scoped (404 for non-members, 403 for insufficient
role), CSRF-protected at the router level, and never return `INTERNAL`
messages or raw tool payloads that are not already public through
`public_payloads`.

| method | path | role | body → response |
| --- | --- | --- | --- |
| GET | `` | viewer | query `q`, `agent_id`, `status` (`active`/`archived`, default active), `pinned`, `limit` (≤100), `offset` → `ConversationListOut` |
| POST | `` | member | `ConversationCreate` → `ConversationDetailOut` (201) |
| GET | `/{conversation_id}` | viewer | → `ConversationDetailOut` |
| PATCH | `/{conversation_id}` | member | `ConversationUpdate` → `ConversationOut` |
| DELETE | `/{conversation_id}` | admin | 204; tasks keep their rows, `conversation_id` becomes null |
| GET | `/{conversation_id}/messages` | viewer | query `after` (message id) → `list[ConversationMessageOut]` |
| POST | `/{conversation_id}/turns` | member | `TurnIn` → `TurnOut` |
| GET | `/{conversation_id}/activity` | viewer | → `ActivityListOut` (cards scoped to this conversation, see below) |

Additional:

| method | path | role | response |
| --- | --- | --- | --- |
| GET | `/workspaces/{workspace_id}/activity` | viewer | query `agent_id`, `team_id`, `conversation_id`, `kinds` (comma list), `before` (ISO datetime cursor), `limit` (≤100) → `ActivityListOut` |
| GET | `/workspaces/{workspace_id}/attention` | viewer | → `AttentionOut` (pending approvals, failed tasks in the last 7 days, conversations waiting on the user) |

`POST /agents/{agent_id}/message` (legacy) keeps its response shape but now
also creates a conversation and links the task to it.

### Schemas

```text
ConversationOut
  id, workspace_id, title, status, pinned, primary_agent_id,
  created_by_user_id, last_activity_at, created_at, updated_at,
  active_task_id: uuid|null      # most recent task in the conversation that is
                                 # queued/running/paused
  active_task_state: str|null    # its task state
  active_run_status: str|null    # its latest run status (e.g. waiting_approval)
  last_message_preview: str|null # ≤160 chars of the latest visible text/summary
  last_message_sender_type: str|null
  agent_name: str|null
  agent_role_title: str|null
  task_count: int

ConversationListOut { items: list[ConversationOut], total: int }

ConversationCreate
  agent_id: uuid
  title: str|null (≤200)
  text: str|null (1..20000)       # when present, the first turn is sent immediately
  client_turn_id: str|null (≤64)

ConversationUpdate { title?: str, pinned?: bool, status?: ConversationStatus }

TurnIn { text: str (1..20000), client_turn_id: str|null (≤64) }

TurnOut
  conversation: ConversationOut
  message: ConversationMessageOut
  task_id: uuid
  mode: "new_task" | "instruction"

ConversationMessageOut = MessageOut +
  conversation_id: uuid|null
  sender_name: str|null          # agent name, user display name, or "System"
  agent_id: uuid|null            # when sender_type == agent

ConversationDetailOut
  conversation: ConversationOut
  agent: { id, name, role_title, status, availability, public_purpose } | null
  tasks: list[TaskOut]           # newest first
  total_input_tokens, total_output_tokens, total_cost_micros: int
  pending_approvals: list[ApprovalOut]  # pending approvals for any task here

ActivityCardOut
  id: str                        # stable: "msg:<uuid>" | "task:<uuid>:<state>" | "approval:<uuid>"
  kind: ActivityKind
  label: str                     # ACTIVITY_LABELS[kind]
  actor_type: "agent"|"user"|"system"
  actor_agent_id, actor_agent_name: uuid|null, str|null
  target_agent_id, target_agent_name: uuid|null, str|null
  task_id, task_title: uuid|null, str|null
  root_task_id: uuid|null
  conversation_id: uuid|null
  approval_id: uuid|null
  summary: str                   # one or two sentences, plain language, ≤400 chars
  detail_json: dict              # sanitized structured content / public payload for Advanced
  created_at: datetime

ActivityListOut { items: list[ActivityCardOut], next_before: datetime|null }

AttentionOut
  pending_approvals: list[ApprovalOut]
  failed_tasks: list[TaskOut]
  waiting_conversations: list[ConversationOut]   # active task waiting_approval
  counts: { approvals: int, failures: int, total: int }
```

### Turn semantics (`POST /{conversation_id}/turns`)

1. Load the conversation (404 cross-workspace), require `status == active`,
   require the primary agent to be `ACTIVE` (409 otherwise with a plain
   message such as "This agent is paused").
2. Idempotency: if `client_turn_id` is provided and a message with
   `content_json.client_turn_id == client_turn_id` already exists in this
   conversation, return the existing message and its task with the original
   `mode`, without creating anything.
3. If the conversation has an active task (`queued`/`running`/`paused`):
   persist a `Message(message_type="instruction", conversation_id, task_id)`
   and signal `user_instruction` exactly as `send_instruction` does.
   `mode = "instruction"`.
4. Otherwise create a new `Task` with `conversation_id`, title from the
   conversation title, `description = text`, `assigned_agent_id = primary
   agent`, `metadata_json = {"origin": "conversation", "conversation_id":…}`;
   persist the seed user `Message(message_type="text", conversation_id,
   task_id)`; commit; then start `AgentTaskWorkflow` the same way
   `message_agent` does (Temporal failure → task `failed`, 503).
   `mode = "new_task"`.
5. Bump `last_activity_at`, audit `conversation.turn`.

### Message listing

Returns visible messages (`visibility == VISIBLE`) whose `conversation_id`
matches **or** whose `task_id` belongs to a task in the conversation, ordered
by `(created_at, id)`. Delegated child tasks are not part of the
conversation's message list; their outcomes reach the parent as structured
`result`/`review_result` messages already, and the activity endpoint shows
the handoffs.

### Activity projection

Sources, all workspace-scoped and filtered before projection:

- Structured messages (`message_type` in `AGENT_MESSAGE_TYPES` minus
  `instruction`, sender agent): `delegation`/`review_request`/`question` →
  `asked_agent` (target = recipient agent or the child task's agent);
  `result`/`review_result` → `reported`; `escalation` → `escalated`;
  `status` → `status_update`. `summary` comes from `content_json.summary`
  (fallback `text`), truncated.
- Tasks with an assigned agent: one `started` card at `created_at`
  (`queued` when `metadata_json.queue` is set and the task is still queued),
  plus one terminal/lifecycle card at `updated_at` when the state is
  `completed` (`finished`), `failed`, `paused`, or `cancelled` (`stopped`).
- Pending approvals: `needs_review` at `requested_at`, actor = requesting
  agent, `summary` = reason.

`root_task_id` follows `parent_task_id` to the top of the delegation chain
(max depth 20). Filtering by `conversation_id` includes every task whose
root task belongs to the conversation, so delegated work shows up in the
chat's activity. Filtering by `agent_id` matches actor or target. Cards are
sorted by `created_at` descending with a `before` cursor.

`detail_json` must pass through `public_payloads` helpers when it carries
tool data; structured message content is already sanitized at write time.

## Worker: conversation-aware history

`AgentActivities._load_history` gains conversation context. When
`task.conversation_id` is set, the history is:

1. Visible messages from **earlier tasks in the same conversation** (any task
   with the same `conversation_id` and `created_at < task.created_at`),
   rendered as plain `user`/`agent` turns — never their internal
   `tool_call`/`tool_result` rows — capped to the most recent
   `CONVERSATION_HISTORY_MAX_MESSAGES = 40` messages and
   `CONVERSATION_HISTORY_MAX_CHARS = 24_000` characters (oldest dropped
   first; a single `[earlier messages omitted]` system-style turn is
   prepended when truncation happened);
2. followed by the current task's own history exactly as today.

The seed user message of the current task is still deduplicated against
`task.description`. Memory, retrieval, and provenance are out of scope here.

## Events

Publish `conversation.created` and `conversation.turn` on the existing
event backbone (`jhin.v1.<workspace>.conversation.*`) with ids only. Audit
actions: `conversation.created`, `conversation.updated`,
`conversation.deleted`, `conversation.turn`.

## Web

The web app calls the endpoints above through `lib/hooks.ts`
(`useConversations`, `useConversation`, `useConversationMessages`,
`useConversationActivity`, `useActivity`, `useAttention`) and renders them in
`/chats`, `/activity`, `/attention`, and agent profiles. Advanced views keep
linking to `/tasks/{id}` for the underlying work episode.
