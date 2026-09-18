# Agentic conversation timeline contract

All routes use `/api/v1/workspaces/{workspace_id}/conversations/{conversation_id}` and existing authenticated chat authorization.

- `GET /items?before=<sequence>&limit=50`: `{items, cursor, next_before, has_more, version:1}`. Items are oldest-first within each page of the most recent distinct items. `cursor` is the committed journal high-water mark, not a timestamp. `before` loads earlier items by stable item birth sequence; updating an old action does not move it between pages. `next_before` is a pagination position, distinct from an item revision.
- `GET /events?after=<sequence>`: authenticated SSE (`Last-Event-ID` accepted). Events `item` contain one item; ID is its ordered journal sequence. `snapshot_required` means reload `/items` and reconnect from its cursor. Heartbeat comments every 15 seconds. Reconnect is safe and clients upsert by item ID/revision, never concatenate snapshots.
- Each item: `{id, version:1, sequence, revision, kind, status, actor:{type,id}, task_id, run_id, created_at, data}`. Stable IDs use canonical prefixes (`message:`, `tool_call:`, `task:`, `approval:`, `user_question:`, `generation:`). Kinds: `message`, `action`, `task`, `approval`, `question`, `generation`, `delegation`, `file`. `data` is an allowlisted public canonical snapshot; tool calls retain their existing ToolCallOut field names. Generation snapshots contain complete sanitized draft text and attempt ID; replace, never append. A terminal generation remains inspectable but final messages are the authoritative answer.
- Existing `GET /tool-calls` now includes all tools and accepts `before` UUID plus bounded `limit`; response adds `next_before`. Existing CLI cards still receive unchanged data.
- `GET /tool-calls/{tool_call_id}/logs` downloads retained sanitized output. This is the retained 8192-character tail per stream, not an assertion of complete logs.
- Turn input adds `execution_mode: "ask"|"plan"|"act"` (omitted preserves active turn; new turns default act), `delivery: "auto"|"steer"|"queue"` (default auto), `model_profile_id?: UUID`, `attachment_ids: UUID[]`, `context_refs: object[]`. File worker validates and pins attachments. Text stays accepted. `auto` preserves existing active-run steering; `queue` creates a queued subsequent task. Per-turn mode/model pin applies only to new tasks; steering cannot silently alter active-task authority.
- `POST /control` with `{action:"pause"|"resume"|"stop"}` routes to active durable task. `PATCH /queued/{task_id}` with `{text}` edits only unstarted queued turns. `DELETE /queued/{task_id}` removes/cancels only unstarted queued turns. Conflicts return409.

The PostgreSQL journal is populated in the same transaction as canonical state changes. NATS is advisory; polling the committed sequence recovers missed notifications. Events expose only public projections, never raw run events, provider requests, credentials, or hidden reasoning.


## Branches and context

`POST /branches {message_id, checkpoint_id?, title?}` returns `{conversation_id, checkpoint_id, source_message_id}`. The branch stores ancestry and copies at most 1000 visible messages through the selected message. Messages carry no execution links and no task is dispatched. A selected checkpoint is cloned to destination-owned file revisions; inherited message attachments are cloned independently, so source deletion does not invalidate their bytes. A latest-message branch can capture an idle workspace; older points need a saved checkpoint at/before the point or an explicit selected checkpoint. The original remains intact.

`context_refs` accepts named file, artifact, agent, app, and project references. File revisions are pinned and validated at submit and provider input time. Named project context is copied to task metadata. Context never adds tool grants. A selected model profile and Ask/Plan/Act mode belong to the submitted task. Ask and Plan reject non-read tools at the gateway even when the agent has a broader grant; clarification questions remain allowed.

Queued tasks wait for their recorded predecessor. Editing/removing is refused once an AgentRun exists. Steering messages move from `pending` to `delivered`, then to `consumed` with the exact observed run/step; instructions arriving after prompt composition remain unconsumed until a later step. Stop stamps intent first, interrupts an idle or streaming provider request, and requests cancellation of the exact sandbox invocation through the scoped runtime gateway.

## Runtime reliability and retained output

Terminal and preview items expose actor, lifecycle, timestamps and the terminal command, with no capability hashes, scoped tickets, private configuration or source manifest. PTY input sequence namespaces are assigned by the gateway from each connection ticket, never browser input. A new reconnect ticket can start at sequence 1; duplicate input on the same ticket is not replayed. The runner retains at most 1024 distinct input namespaces and refuses excess instead of forgetting replay history.

Terminal replay uses sanitized output offsets and an authoritative retained tail of 131072 characters; reconnects do not append the same output twice. Prefixes of registered secrets are withheld across output chunks. Runner sessions expire at their scoped lifetime (at most 8 hours), with 30-second cleanup checks and one hour of in-memory ended-session logs. PostgreSQL retains the public session and bounded logs independently of a runner restart. Expired capability rows are removed after seven days; this cleanup never deletes conversation files. Human writes validate user role, current ownership generation/disk, and active or unconfirmed prior invocations at the gateway.

## Verification

- Clean PostgreSQL upgrade to 0047 and 0047→0045→0047 round trip; concurrent journal writes preserve commit order and rollback leaves no cursor gaps.
- API tests cover queue/edit guards, per-turn modes, typed input pinning, project context and branch history/file ownership without execution replay.
- Provider tests cover OpenAI-compatible and Anthropic fragmented tool calls, usage, citations, image blocks and exclusion of private reasoning.
- Generation tests cover split-secret draft redaction, distinct retry attempts, cancellation while a provider emits no output, and exact instruction-consumed receipts.
- Gateway/session tests cover one-shot capability claims after uncertain responses, revocation/expiry, stale disk fencing, reconnect input namespaces, bounded deduplication, retained output and session cleanup.
- The complete sandbox-runner + CLI test suites pass in a disposable Linux container: 638 passed, 5 skipped, 1 deselected. Temporal recorded-history/failure checks pass 54 tests; the full Temporal test-server environment suite was not completed in this run.
