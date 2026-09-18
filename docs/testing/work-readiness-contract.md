# Work readiness, questions, and schedules

Schedule routes use `/api/v1/workspaces/{workspace_id}/schedules`.

- `GET /schedules?agent_id=&limit=50&offset=0` → `{items: Schedule[], total}`.
- `POST /schedules` → Schedule (201). Input: `{name, agent_id, brief, local_time:"09:00", timezone:"America/Los_Angeles", weekdays:[0,1,2,3,4,5,6], enabled:true, idempotency_key}`. Monday is 0. Timezone is required. `overlap_policy` is `"skip"`.
- `GET /schedules/{id}` → Schedule.
- `PATCH /schedules/{id}` → Schedule. Input: `{expected_version, name?, brief?, local_time?, timezone?, weekdays?, enabled?}`. Use enabled false/true for pause/resume. Agent is immutable.
- `DELETE /schedules/{id}?expected_version=N` → 204. Retires the schedule; occurrence history remains.
- `GET /schedules/{id}/occurrences?limit=50&offset=0` → `{items: Occurrence[], total}`.

Schedule fields: `id,workspace_id,name,agent_id,brief,local_time,timezone,weekdays,enabled,deleted_at,overlap_policy,version,next_run_at,last_run_at,last_status,created_at,updated_at`. Times are ISO UTC. Gaps in local daylight-saving time skip that date; repeated local times run once at the first occurrence. Pause or edit never cancels a task already dispatched. Missed occurrences while a preceding task runs are recorded as skipped overlap. Scheduling uses current agent grants and preserves the brief in each task.

Occurrence fields: `id,schedule_id,scheduled_for,status,task_id,started_at,finished_at,error_code`. Statuses: `running,completed,failed,cancelled,skipped_overlap,skipped_unavailable`. Task links use the existing task routes. Mutation errors: 409 stale/idempotency mismatch, 422 invalid timezone/time/brief, 404 unavailable row, 403 denied.

Question API and message projections gain `required:boolean,input_key:string,value_type:"text"|"url"|"timezone"|"time"`. `options` may be empty for free text, with `allow_other:true`. Required unanswered questions remain pending across the old 30-minute timeout. Stopping a run still cancels its questions. Optional unanswered questions do not authorize an external action or substitute for a missing prerequisite.

Task intake may set `metadata_json.required_inputs=[{key,label,value_type,reason}]`. Entries contain public descriptions only. The gateway permits clarification and bounded internal context/setup actions until a valid answer resolves each key. `ghost_admin_url` requires an explicitly supplied HTTP(S) URL without embedded credentials. The URL is never inferred from a publication hostname.

Reserved setup questions use platform-owned wording, empty options, no model-authored context, and required free text: `ghost_admin_url` asks for the actual Ghost Admin URL (`value_type:url`); `ghost_publisher_agent_id` asks which agent may review and publish Ghost drafts (`value_type:text`, exact name or UUID). An answer to an unrelated/optional question cannot satisfy a reserved setup key. Accepted answers are stored in task `resolved_inputs`; sensitive answer intake runs before persistence and uses opaque references.

`organization.report_result` accepts `missing_inputs:[{key,label,value_type}]` with `status:blocked`. A blocked child returns those fields and propagates them to its requester. It cannot report completion while required inputs remain. Both delegation tools require `cross_team_reason` outside current teams and stop a third unchanged failed handoff even when its target changes. Current team membership excludes departed rows, including a stale legacy primary-team pointer.

Agent schedule tools are `schedules.create`, `schedules.list`, `schedules.update`, `schedules.delete`, and `schedules.history`, limited to the calling agent. `schedules.read` and `schedules.manage` grants remain authoritative. Temporal reconciliation starts stable per-schedule workflows; each occurrence claims one task under a PostgreSQL row lock and uniqueness constraint. Repeated ticks and restarted sessions reuse that exact task. Local-time edits only change future work; a running task retains its original brief. An outage produces at most one late occurrence rather than a catch-up burst.

Memory summary routes are `GET /memories/summary?scope=agent|team|workspace&scope_id=UUID` and `POST /memories/summary/rebuild` with the same parameters. Response: `{scope,scope_id,version,summary,items:[{id,version,content,source_conversation_id,source_message_id,source_task_id}],coverage_count,source_count,generated_at,stale:false}`. Summaries select at most 20 distinct supported facts from 500 current records, excluding superseded/expired records; rebuilding reads current authoritative records and does not create another memory store.

Memory list/detail outputs add `evidence_status:supported|unsupported`. Unsupported legacy agent notes remain available to people but are excluded from automatic search/recall and summaries, even when pinned. Human-authored, explicitly human-approved, validated human-source excerpts, and verified native-tool facts remain eligible. Reviewing and saving through the existing PATCH endpoint creates a human-authored version. Tool-generated memory cannot establish successful setup from an assistant claim, shell output, or an HTTP failure.

Legacy credential projection removes recognizable secret spans before chat-model and embedding-model inputs, including before history truncation. It preserves stored records and opaque secure references, and does not create a usable credential from a legacy paste. Oversized projection values are omitted whole to avoid leaking a credential prefix at a truncation boundary.
