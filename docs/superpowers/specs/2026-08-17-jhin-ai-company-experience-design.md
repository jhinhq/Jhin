# Jhin AI Company Experience Design

**Status:** Approved by the product owner on 2026-08-17

## Summary

Jhin will evolve from an operations-first agent console into a chat-first AI
company platform. A user can create one independent agent, a flat group of
peers, several optional teams, or a nested company with managers and
departments. The same underlying task, workflow, policy, and audit systems
continue to provide durable execution and safety.

This design adds first-class conversations, curated long-term memory,
organization awareness, peer and cross-team collaboration, configurable
managerial review, agent avatars, and a complete responsive visual redesign.
It deliberately keeps reporting hierarchy optional and separates
relationships from authorization.

## Goals

- Let users have multiple named, persistent conversations with every agent.
- Let agents retain relevant knowledge across conversations without treating
  whole transcripts as memory.
- Let agents discover colleagues, understand roles and teams, and seek help
  from the right person.
- Support solo agents, peer groups, multiple team memberships, and optional
  reporting hierarchies using one model.
- Make close collaborator relationships such as Software Engineer and QA
  Engineer explicit and useful for routing.
- Let managers understand subordinate work and make decisions through concise,
  source-linked rollups and configurable exception review.
- Show human-agent and agent-agent communication in a clear, conversational
  activity stream without exposing hidden chain-of-thought.
- Give agents memorable identities with names, roles, descriptions, expertise,
  tools, memory, and safe generated or uploaded avatars.
- Present a friendly, nontechnical default experience while retaining the full
  operational surface in an Advanced area.
- Preserve Jhin's workspace isolation, deny-by-default tool policy, Temporal
  durability, audit trail, and current Phase 8 delegation capabilities.

## Non-goals

- Autonomous agent changes to grants, memberships, hierarchy, relationships,
  review policies, or workspace security settings.
- Continuous unsupervised agent chatter with no task or request boundary.
- Storing or displaying provider reasoning tokens, scratchpads, or hidden
  chain-of-thought.
- Using raw conversation transcripts as durable memory.
- Cross-workspace sharing.
- A mandatory external vector database or local GPU image-generation stack.
- General document RAG, knowledge-base ingestion, or document authoring.
- Photorealistic agent identities or any suggestion that an avatar represents
  a real person.

## Product principles

1. **Conversation first.** The primary action is to talk to an agent; durable
   work appears inside that conversation.
2. **Structure is optional.** Teams and managers enhance coordination but are
   never prerequisites.
3. **Awareness is not authority.** A colleague, team, manager, or collaborator
   relationship improves discovery and routing; it grants no data or tool
   access by itself.
4. **Relevant context, not maximal context.** The runtime receives a bounded,
   attributable selection of organization and memory context.
5. **Summaries with drill-down.** Default views explain outcomes and decisions;
   users can inspect source messages, tasks, events, and tool evidence.
6. **Safety stays visible.** Pause, stop, approval, access summaries, errors,
   and consequences are never hidden merely because they are technical.
7. **Progressive power.** The simple experience covers ordinary work. Advanced
   exposes models, runs, detailed policies, logs, and infrastructure.

## Existing foundation and Phase 8 closure

Phase 8 already supplies structured agent messages, delegation and result
tools, durable child workflows, delegation authorization, manager summaries,
task lineage, an engineering workflow, a QA fail/fix/retest loop, and
workspace/agent concurrency admission.

Before new product work:

- Replace the Phase 8 concurrency test's unsupported approval-gated
  `system.echo` fixture with an existing approval-capable inert tool.
- Re-run the five Phase 8 integration scenarios, then the complete unit,
  frontend, lint, type, build, and relevant integration suites.
- Update README status and add Phase 8 architecture documentation.
- Record unsupported later-phase items separately rather than presenting them
  as completed Phase 8 scope.

## Architecture boundaries

The implementation is divided into four bounded subsystems plus the redesign:

1. **Company topology and identity** owns teams, memberships, reporting,
   collaborator relationships, expertise, and avatars.
2. **Conversations and memory** owns named chats, participants, persistent
   messages, memory extraction, retrieval, and memory management.
3. **Coordination and oversight** owns peer requests, delegation presentation,
   manager context, review policies, reviews, and company activity.
4. **Experience shell** owns information architecture, simple/advanced
   presentation, accessibility, and responsive behavior.

All agent-initiated operations continue through the tool gateway. Temporal
owns long-running or durable work. PostgreSQL remains the system of record.
NATS transports events but never becomes canonical state.

## Company topology and identity

### Agent

The existing `agent` remains the central identity and configuration record.
It continues to own name, slug, role title, description, system prompt,
autonomy, model, run limits, budget, approval policy, and status. It gains:

- an optional active avatar asset;
- structured expertise tags and a short public purpose statement;
- discoverability and availability settings;
- compatibility links to the new membership and relationship records.

An agent with no team and no manager is valid and fully functional.

### Teams and memberships

`team` remains a nestable group with an optional manager. Add
`agent_team_membership` so an agent may belong to multiple teams while having
at most one primary team. Existing `agent.team_id` remains a compatibility
pointer during migration and is updated atomically with the primary
membership.

Membership represents working context, not permissions. A team may represent
a department, durable squad, or other stable group; V1 does not introduce
separate group kinds. Parent teams allow Engineering, Marketing, and Sales to
grow into nested departments without changing the agent model.

### Relationships

Add `agent_relationship` with workspace, source agent, target agent,
relationship kind, purpose, status, and timestamps.

- `close_collaborator` is symmetric and stored in canonical agent-ID order.
- `advisor` and `preferred_reviewer` are directed.
- `manager` continues to use the existing acyclic
  `agent.manager_agent_id` reporting line rather than duplicating authority in
  the relationship table.

Relationships influence colleague ranking, routing suggestions, and manager
context. They never bypass capability grants, visibility rules, or review
policy.

### Organization awareness

Every agent receives a compact roster containing itself, its manager and
reports, primary and secondary team members, and close collaborators. The
runtime also advertises a scoped `organization.directory.search` tool for
discovering other workspace agents by name, role, expertise, or team.

The directory exposes public identity fields only. It never exposes system
prompts, credentials, private memories, grants, or private conversations.

## Conversations and work episodes

### First-class conversations

Add `conversation` with workspace, editable title, status, creator, primary
agent, pin/archive state, and last-activity timestamp. Add
`conversation_participant` for human and agent participants with explicit
roles and join/leave timestamps.

V1 starts each user-created conversation with one primary agent. Additional
agents may participate through accepted help requests, review, or delegation;
joining a work episode does not silently make an agent a permanent participant.

### Preserve the task engine

Tasks remain the execution unit. Each user turn that starts agent work creates
a task linked to the conversation. This preserves `AgentTaskWorkflow`,
concurrency, approvals, tools, delegation, and recovery while preventing task
records from defining the user-facing chat model.

Add `conversation_id` to tasks and visible messages. Messages associated with
work may reference both conversation and task. Child-task handoffs are
projected into the root conversation as structured activity without copying
private or raw tool transcript data.

The default UI hides per-turn tasks. Advanced exposes each work episode, run,
lineage, token/cost totals, events, and sanitized tool evidence.

### Agent-to-agent communication

Keep delegation for authorized parent/child ownership transfer. Add a separate
`organization.request_work` capability and durable `work_request` record for
peer and cross-team help.

A work request supports `pending`, `clarification_requested`, `accepted`,
`declined`, `completed`, and `failed` states. Acceptance creates exactly one
linked task using an idempotency key. Decline creates no task. Bounded retries,
depth, concurrency, rate, and budget controls prevent duplicate work and
agent-to-agent ping-pong.

## Memory

### Memory scopes

Add immutable/versioned `memory_record` rows with:

- workspace and scope (`agent`, `team`, `workspace`, or explicit share);
- memory kind and concise content;
- source conversation/message/task/event references;
- visibility, sensitivity, confidence, importance, and tags;
- status (`proposed`, `active`, `contested`, `superseded`, `rejected`, or
  `forgotten`);
- validity and expiry timestamps;
- supersession link and optional embedding;
- creator type/ID and audit metadata.

Agent-private memory is available across all named chats with that agent. Team
memory is available to current authorized team members. Workspace knowledge is
explicitly company-wide. Collaborator links alone never reveal private or team
memory; explicit sharing is required.

### Extraction and promotion

After a visible conversation turn or completed task, an idempotent
`MemoryMaintenanceWorkflow` may ask a model for strict structured memory
candidates. Deterministic code then performs:

- secret and sensitive-data screening;
- source visibility and non-amplification checks;
- normalization and deduplication;
- contradiction detection;
- scope and promotion policy evaluation.

An explicit user instruction to remember something may activate it at the
same authorized scope. Ordinary agent-private facts can activate
automatically. Shared task decisions may activate as team memory when the
source was already team-visible and policy permits it. Model-proposed
workspace promotion or visibility broadening requires manager or human review.

Extraction failure never fails the originating chat or task.

### Retrieval

Before every run, live authorization filters eligible memories. Retrieval then
ranks by semantic relevance, lexical relevance, recency, confidence,
importance, and scope. Both record count and token count are capped.

The selected memory IDs, versions, policy result, and context hash are
recorded on the run timeline. Revoked, expired, superseded, rejected,
forgotten, or unauthorized records are never injected.

The default Compose environment uses pgvector inside PostgreSQL for semantic
retrieval. If an embedding profile or extension is unavailable, PostgreSQL
full-text search provides a visible degraded fallback; no second vector
database is introduced.

### User controls

Users can inspect, pin, edit, contest, or forget memories. Edits create new
versions. Forgetting immediately excludes retrieval and removes live content,
embeddings, and caches. The audit log retains only a content-free tombstone
with identifiers, actor, action, and timestamps.

## Management, review, and company activity

### Manager context

When a manager agent runs, context assembly includes an authorized structured
rollup of direct and indirect reports:

- active and recently completed work;
- blocked or failed work;
- pending reviews and approvals;
- outcomes, artifacts, risks, and requested decisions;
- workload and queue state.

The rollup is deterministically derived from source records. An optional model
narrative is labeled as derived and links to its source events. Manager status
does not reveal agent-private memory or unauthorized conversations.

### Review policies

Add `review_policy` scoped to workspace, team, agent, workflow, or task type.
Policies contain enabled state, review mode, conditions, and a reviewer
selector. Add `work_review` for each triggered review, its source evidence,
reviewer, status, verdict, feedback, and timestamps.

Supported modes are pre-action, before-close, post-action, and periodic
rollup. Default behavior is exception-based. Conditions may include:

- elevated or destructive external action;
- token, cost, or time threshold;
- tool, test, or workflow failure;
- approval or policy denial;
- blocked status or unresolved risk;
- low confidence;
- cross-team request;
- changed scope or explicit review request.

Routine low-risk work proceeds without a manager. Reviewer selection may use
the reporting manager, a named agent, a team role, or a human. If no manager
exists, policy may select another reviewer, escalate to a human, or skip. A
missing mandatory pre-action or before-close reviewer fails closed.

AI manager review is distinct from human security approval. An AI manager
cannot override tool policy or approve an action reserved for a human.

### Unified activity

Expose one query/read-model service that projects messages, task state,
reviews, approvals, and meaningful events into human-readable activity cards.
The underlying append-only records remain authoritative.

Default labels are:

- Started working
- Asked another agent
- Used an app
- Needs your review
- Finished
- Needs help
- Paused or stopped

The same cards appear within a conversation, agent profile, team page, and
company activity feed. Advanced reveals the original structured message,
event, sanitized payload, and identifiers.

## Authorization and safety

All agent-initiated reads and mutations use the existing policy/tool gateway.
Add narrow capabilities:

- `organization.directory.read`
- `organization.message.send`
- `organization.work.request`
- `organization.review.request`
- `memory.read`
- `memory.propose`

Agents never receive capabilities for grant modification, membership or
relationship modification, review-policy modification, audit deletion, or
unilateral shared-memory administration.

Every decision intersects:

1. workspace boundary and human RBAC;
2. structural invariants;
3. explicit deny;
4. matching allow grant and target scope;
5. resource visibility/non-amplification;
6. risk and review outcome.

No relationship is an implicit grant. Cross-workspace misses return 404.
Memory, messages, rollups, and avatar metadata are workspace-scoped.

## Agent avatars and media

Add a `MediaStore` boundary plus `media_asset` and `avatar_generation`
records. V1 stores normalized small avatars in PostgreSQL for backup-safe
single-node self-hosting while keeping an S3-compatible adapter boundary for
larger deployments.

Agent avatar choices are:

- generated editorial-style illustration derived only from public agent
  identity fields and an explicit user prompt;
- uploaded and cropped raster image;
- accessible initials or role icon.

Generation is asynchronous and non-blocking. Agent creation succeeds with
initials, and a previous valid avatar remains active until a replacement has
fully validated. The UI discloses external provider use and cost before
generation.

Uploads accept decoded PNG, JPEG, or WebP only. The service rejects SVG,
animation/video, MIME mismatch, oversized byte/pixel/frame counts, and
decompression bombs; strips metadata; and re-encodes to bounded WebP variants.
It never fetches arbitrary remote image URLs.

Generated avatars are deliberately stylized rather than photorealistic and
are never treated as proof of identity.

## Information architecture and primary flows

### Default navigation

- Chats
- Agents
- Company
- Automations
- Needs your attention

Workspace menu:

- Apps
- Members
- Settings

Advanced:

- Work queue and task lineage
- Runs, tokens, costs, and execution detail
- Models and providers
- Tools, grants, and review policies
- Detailed automation rules
- App and webhook diagnostics
- Audit and system health

Opening Jhin restores the last conversation or shows a Chats home with a large
composer and suggested agents. The generic infrastructure dashboard is no
longer the default destination.

### Primary flows

1. **First run:** name the workspace, connect an AI provider, describe the
   first needed agent, review its identity/access, then open the first chat.
   Teams and hierarchy are offered later.
2. **New chat:** choose an agent, send a request, and receive an editable
   suggested chat title.
3. **Collaborative work:** follow messages, compact work updates, handoff
   cards, artifacts, reviews, and inline approvals in one transcript.
4. **Find an agent:** search by name, role, expertise, team, or availability;
   Chat is the primary action.
5. **Create an agent:** describe the help needed, personalize identity/avatar,
   choose apps/review style/optional team, then Create and chat.
6. **Company:** view a flat directory, optional teams, or a nested org map
   without changing the core agent workflow.

### Simple versus Advanced

| System concept | Default experience | Advanced experience |
| --- | --- | --- |
| Tasks | Named chats and work updates | Work queue, priority, IDs, lineage |
| Runs | Working state and outcome | Run explorer, tokens, cost, steps |
| Timeline | Human-readable activity | Raw events, tool calls, sandbox output |
| Approvals | Inline cards and Attention inbox | Policy and payload inspection |
| Connectors | Apps | Credentials, scopes, webhook diagnostics |
| Triggers | Automation templates | Conditions, sample events, dry runs |
| Models | Guided AI setup | Providers, profiles, pricing, parameters |
| Audit/system | Relevant activity/errors | Audit log and infrastructure health |
| Agent settings | Purpose, apps, review style | Prompt, model, budgets, limits |

## Visual system

Use the supplied geometric J mark as the primary brand asset, preserving its
aspect ratio inside a square field. The mark is solid Jhin Iris on light
surfaces and a lighter iris tint on dark surfaces; restrained stepped geometry
may echo the mark in handoff and progress details.

Core palette:

- warm canvas `#F7F6F2`;
- surface `#FFFFFF`;
- primary ink `#242520`;
- muted ink `#62685F`;
- border `#DFDDD5`;
- Jhin Iris `#5B55C8` and tint `#EEECFF`;
- success/collaboration sage `#2E7558` and tint `#E7F4EC`;
- attention amber `#985B08` and tint `#FFF2D8`;
- danger coral `#B44351` and tint `#FCE9EB`;
- information sky `#316F98` and tint `#E8F4FA`;
- warm peach accent `#C96F43` and tint `#FBECE3`.

The experience is light-first with an optional warm-dark theme. Use self-hosted
Manrope with system fallbacks, 16px default body text, 13–14px minimum
metadata, readable line lengths, generous whitespace, 14–18px rounded cards,
quiet borders, restrained shadows, and monospace only in Advanced.

Motion is small and purposeful, used for handoffs and live state only. Honor
reduced-motion preferences. Team colors are decorative and never the sole
carrier of meaning.

## Responsive and accessibility behavior

Target WCAG 2.2 AA and use an internal 44px minimum interaction target.

- Wide chat layout: chat rail, conversation, optional context panel.
- Medium layout: chat rail plus conversation; context becomes a drawer.
- Small layout: one pane with bottom navigation for Chats, Agents, Company,
  and More; list/detail become separate screens.
- Keep the composer above safe areas and the onscreen keyboard.
- The organization map always has an equivalent semantic outline/tree and
  defaults to the outline on mobile.
- Operational tables become cards on narrow screens.
- Dialogs and drawers trap focus and restore it on close.
- Visible keyboard focus, text-plus-color statuses, live-region summaries,
  zoom/large-text support, and reduced motion are required.
- Chats do not force-scroll while a user reads history; a New updates control
  moves to the latest activity.
- Avatars always appear with visible agent names.

## Failure and degraded behavior

- Memory extraction failure queues a retry and does not fail chat or work.
- Embedding failure falls back to PostgreSQL full-text retrieval.
- A memory outage records `memory_unavailable`; non-memory-critical work may
  continue, while explicitly memory-critical work fails visibly.
- Conflicting memories become contested and are not silently selected.
- Mid-run revocation blocks the next read or action; already-sent model context
  cannot be recalled and is documented as such.
- A required unavailable reviewer fails closed or escalates according to
  policy; optional review never blocks routine work.
- Request retries are idempotent and cannot create duplicate tasks.
- Avatar-generation failure preserves initials or the previous valid avatar.
- Malformed media is rejected without changing the active avatar.
- Every user-facing failure states what failed, whether it will retry, what is
  affected, and a safe next action.

## Delivery sequence

### Release 0: Phase 8 closure

Repair the concurrency exit fixture, verify the Phase 8 suite, and close the
documentation gap.

### Release 1: Company topology and identity

Add multi-team membership, optional relationships, expertise, organization
directory context/search, media storage, and avatar upload/generation
contracts while preserving current agent/team APIs.

### Release 2: Conversations and memory

Add conversations, participants, task/message linking, persistent chat,
memory extraction/retrieval, memory management, and context provenance.

### Release 3: Coordination and oversight

Add peer/cross-team work requests, manager rollups, general review policies,
work reviews, and unified company activity.

### Release 4: Complete experience redesign

Replace the shell and primary flows with the new brand, information
architecture, responsive layouts, simple/advanced split, conversational work
cards, agent profiles, Company experience, onboarding, accessibility, and
warm-dark theme.

Each release remains backward compatible until the final route cutover. Data
migrations are additive first; old fields/routes are removed only after all
consumers have migrated and equivalent Advanced access exists.

## Testing strategy and acceptance criteria

### Phase 8

- All five Phase 8 integration scenarios pass, including worker restart while
  one run is parked and another is queued.
- Fresh unit, lint, format, type, frontend, and production build checks pass.

### Organization

- Managerless and teamless agents work.
- Multiple team memberships allow exactly one primary membership.
- Manager cycles and cross-workspace relationships fail.
- Close collaborator links are canonical, symmetric, and grant no authority.
- Directory context contains authorized public identity only.

### Conversations

- A user creates, names, renames, pins, archives, searches, and resumes chats.
- Multiple turns retain conversation context across distinct durable runs.
- A failed or restarted worker does not duplicate user or agent messages.
- Delegated work and accepted help requests appear in the originating chat.

### Memory

- Relevant active memory appears in later conversations.
- Irrelevant, unauthorized, expired, superseded, rejected, or forgotten memory
  does not appear.
- Secret and authorization-header candidates are rejected or redacted.
- Source visibility never broadens through memory promotion.
- Edits create versions and forget removes live content/embedding/cache while
  retaining a content-free audit tombstone.
- Retrieval provenance is recorded for every run.
- Full-text fallback works without embeddings.

### Coordination and review

- Cross-team request retries create at most one task; declines create none.
- Routine work bypasses exception review.
- Every configured exception opens exactly one review.
- Missing mandatory reviewers fail closed; managerless fallback policies work.
- Manager rollups are reproducible from authorized source records.
- AI review cannot bypass human approval or tool policy.

### Media

- Valid image upload produces bounded normalized variants.
- SVG, animation, MIME mismatch, oversized/decompression-bomb input, and
  metadata-bearing originals are rejected or sanitized.
- Failed generation preserves agent usability and the prior avatar.
- No private chat or memory content enters an avatar prompt.

### Experience

- Desktop, tablet, and mobile primary flows work with keyboard and touch.
- Default screens contain no raw event names, workflow IDs, or capability
  strings unless expanded or opened in Advanced.
- All legacy operational capabilities remain reachable in Advanced.
- Automated accessibility checks plus keyboard and screen-reader smoke tests
  cover chat, agent creation, approvals, memory controls, and org outline.
- Status never relies on color alone and reduced motion is honored.

## Decision log

- First-class conversations are separate from tasks; tasks remain hidden work
  episodes to preserve the proven Temporal engine.
- Multiple named conversations are supported per agent.
- Long-term memory is curated, scoped, versioned, attributable, and editable.
- PostgreSQL plus pgvector is the semantic store; full-text search is the
  degraded fallback; no second vector database is added.
- Teams, memberships, managers, and collaborator relationships are optional.
- Agents may join multiple teams with one primary team.
- Reporting, collaboration, and permissions are separate concepts.
- Peer/cross-team requests are distinct from delegation.
- Organization awareness uses a bounded local roster plus directory search.
- Manager reviews are configurable and exception-based by default.
- AI reviews never replace human security approval.
- Agent communication and decision summaries are visible; hidden reasoning is
  neither requested nor stored.
- Agent avatars support initials, safe upload, and optional asynchronous
  stylized generation.
- The J mark uses solid Iris as the primary brand treatment.
- The default product is light, conversational, responsive, and nontechnical;
  operational machinery remains in Advanced.

