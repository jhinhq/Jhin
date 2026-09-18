# Agentic chat workspace implementation

The user-approved plan in this conversation is the specification. All six milestones are required for the initial release; partial implementations are not a completed release.

## Deliverables

1. Durable versioned conversation items, ordered replay and SSE, typed provider streaming, all-tool visibility, reliable reconnect and bounded history/logs.
2. Persistent isolated chat workspaces, reusable saved projects, explicit legacy import, fenced writers, local managed file storage and immutable artifacts.
3. Upload/drop/paste and typed model input; Office/PDF/image extraction and previews; artifact publication and MCP resources; rich rendering, context chips and a responsive workspace pane.
4. Owner/admin PTY control with reconnect and confirmed handoff; cancellation, steering/queue receipts, and gateway-enforced per-turn Ask/Plan/Act.
5. Isolated versioned interactive HTML and registered development-server previews with HTTP/WebSocket routing and explicit lifecycle controls.
6. Revision-aware editing, real diffs, checkpoints/restore, conversation branches, annotations and visible delegated work.

## Constraints

- Self-hosted local volumes, no required public domain, model-provider neutral.
- Preserve current text clients, existing permissions, live data, uncertain-effect guards and secret redaction.
- General work plus coding; every chat owns persistent files, optional projects seed isolated working copies.
- Direct text/code editor and annotation-based Office revision; interactive terminal and app previews ship in the first release.
- Human terminal control is owner/admin only. No raw internal reasoning or fabricated activity.
- Existing shared dirty checkout is preserved. No reset, clean, destructive migration, commit or push is part of this task.

## Work allocation and integration

| Track | Owns | Integration |
|---|---|---|
| Files/backend | New chat-workspace DB/file models, migration 0045, general blob store, file/project/revision APIs | Publishes file API contract for frontend and runtime |
| Timeline/runtime | Conversation journal and streaming, typed provider messages, turn controls/modes, migration 0046 | Consumes managed file IDs; frontend consumes events and items |
| Frontend | Chat/rendering/composer/workspace UI and browser tests | Consumes the published additive APIs |
| Root/runtime sessions | Runner workspaces/PTY/previews, session transport, CLI file binding and integration, migration 0047 | Uses shared conversation workspace and file models |

API route registration, central route scopes, root dependency lockfiles, Compose wiring and final OpenAPI regeneration are root-owned unless explicitly reassigned. Workers must not revert others' changes.

## Release gates

Verify actual commands/actions; document upload-generation-revision-download; isolation across chats; PTY reconnect/input/resize/stop; Vite/Next/HTTP/WebSocket previews; refresh/retry/restart recovery; scoped access and hostile input; responsive keyboard-accessible UI. Run targeted regression tests and full integration checks before rollout. Back up persistent data before additive live migrations.
