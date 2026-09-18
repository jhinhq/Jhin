# Scoped variables and secure input

Variables belong to a workspace and one immutable namespace: `agent` with an agent ID,
`team` with a team ID, or `company` with the workspace ID. Human management requires
the current workspace owner/admin role. Agents can read their own private variables,
current teams' variables, and company variables. Departed team membership defeats a
stale primary-team field. Disabled agents cannot read or consume variables.

All records expose `id`, `workspace_id`, `name`, `scope`, `scope_id`, `sensitive`,
`configured`, `version`, `description`, actor IDs/types and timestamps. Copies also
expose nullable `source_variable_id` and `source_version`. Ordinary variables have
`value`; sensitive variables have no value, hint, ciphertext, or backing-secret ID
in their public representation.

## Human API

Base: `/api/v1/workspaces/{workspace_id}/variables`.

| Operation | Request | Result |
|---|---|---|
| GET base | Optional `scope`, `scope_id`, `limit` (1–100), `offset` | `{items,total}`; database pagination and current audience filtering |
| GET `/{id}` | — | One public record |
| POST base | `name,scope,scope_id,value`, optional `description`; `sensitive:false` | 201, public record |
| POST `/secrets` | Same metadata, `value`, optional `sensitive:true` | 201, sensitive metadata; browser session only |
| PATCH `/{id}` | `expected_version`, optional `name,description,value` | Updated record; sensitive values cannot use this route |
| PUT `/{id}/secret` | `expected_version,value` | Sensitive replacement; browser session only |
| DELETE `/{id}` | Query `expected_version` | 204; atomically disables bound apps, removes bindings, and deletes the variable and its encrypted captured values |
| POST `/{id}/copy` | `expected_version,scope,scope_id`, optional `name` | 201; original retained, independently encrypted copy with provenance |

Sensitive values use a transient `SecretStr` request field. They are not mutation-cache
or optimistic-response data. Scope and sensitivity cannot be changed in place. Copying
the same source version to the same unchanged destination is idempotent; conflicting
names or changed versions return 409. A copied secret can be rotated or deleted
independently of its source. Reads use `Cache-Control: no-store`.

Names match `[A-Za-z_][A-Za-z0-9_.-]{0,119}`. Descriptions allow at most 2,000
characters. Values allow at most 8,192 UTF-8 bytes; invalid Unicode and NUL bytes are
refused before encryption or persistence. Sensitive values cannot be empty. Known
credential formats and explicit credential assignments are refused in ordinary
variable values, names, and descriptions. Use the sensitive form for these values.

## Agent tools and authority

`variables.list`, `variables.get`, `variables.set`, `variables.delete`, and
`variables.copy` are registered local tools. Read tools require `variables.read`;
writes require `variables.write`. Public outputs use the same metadata contract.
Set defaults to the calling agent's private namespace. Updates/deletes and copies
require the expected source version. Sensitive set accepts `secret_ref`, not plaintext.

`variables.list` also returns `accessible_scopes: [{scope,scope_id,name}]` and
`accessible_scopes_truncated: boolean`, including when `items` is empty or filtered.
Each scope entry contains only its namespace, exact UUID and current display name.
The response contains the caller's private agent scope, up to 100 current team
scopes ordered by name and ID, and the company scope whose ID is the workspace ID.
An explicit `scope: "team", scope_id: ...` filter narrows team discovery to that
accessible team. More than 100 teams matching the discovery query sets
`accessible_scopes_truncated` to true.
Discovery uses current membership, excludes departed and unrelated teams, filters
every team to the workspace, and refuses inactive or missing callers. Agents should
use these IDs for `variables.set` and `variables.copy`, without guessing IDs or
delegating their discovery. Listing a destination grants no write authority and
exposes no credential values.

Private writes stay within the caller's own agent namespace. Team/company writes
also require either an explicit scope request in the current task from a currently
authorized human admin or an existing grant matching both `scope` and `scope_id`.
The write validator resolves ID-based targets before applying scoped allow/deny
rules. Explicit denies win. These operations never create capability grants or
change membership. Copy requires source read access and destination write authority.

Human authority is stamped by authenticated ingress under `_human_authority`, retaining
the initiating user's effective role and, for API keys, the key ID and effective scopes.
`jhin_secrets.authority.human_message_authorized` checks that proof against current
active user/admin membership and current key issuer, workspace, expiry, revocation,
role ceiling and scopes. Shared variable writes require `variables:write`; Ghost setup
requires `apps:write`. A chat-only key does not borrow its owner's admin permissions.
It can still provide secure input and ask the agent to use its own private variables.
Untagged historical messages require a fresh authorized request for privileged setup.

Scope instructions must be positive, unquoted and unambiguous. A current prohibition
overrides earlier approval. Explicit creation requests such as “Create this setting
company-wide” and “Create this setting for Marketing” follow the same authority
checks as set/copy requests. Generic `team`, `team-wide` or `team wide` requests authorize
only the agent's current primary team; another team must be named explicitly. Queued
edits replace the initiating authority with that of the actual editor. Required answers
record equivalent proof in `task.metadata_json.resolved_input_authority[input_key]`;
privileged consumers check this before trusting the corresponding resolved input.

## Secure chat ingress

The authoritative detector is `jhin_secrets.intake.secret_spans`. It recognizes Ghost
Admin key envelopes, supported provider-key prefixes, JWTs, private PEM keys, Bearer
tokens, credentials embedded in URLs, and explicit API-key/token/password assignments.
The browser uses a conservative superset to withhold potentially sensitive drafts
from persistent storage. Unformatted secrets use transient `secure_inputs` fields.

Conversation create/send and queued edits accept up to ten `{name?,value}` secure
inputs. Combined automatic and explicit capture is bounded to twenty values per
submission. The server encrypts values before constructing persisted titles, messages,
tasks, journals, workflow input, instructions or public receipts. Visible text uses
`[secure_input:UUID]`; accompanying metadata is `[{secret_ref,name,kind}]`.
Retries deduplicate by keyed fingerprint, workspace, conversation, and human sender.
Unconsumed references expire after seven days. Presenting the actual value again
renews its intake expiry. Existing opaque markers are never captured as credentials.

Legacy agent-message/task/instruction routes use the same capture boundary. Assigned
credential-bearing tasks gain a conversation; unassigned tasks must select an agent
before accepting credentials. Free-text question answers are captured before required
input resolution and idempotency comparison. Structured fields such as URLs refuse
embedded credentials. Missing encryption fails closed without saving the secret.

References can be consumed only by the original human or the receiving agent in that
same conversation. A consumed reference identifies one variable; it cannot be stolen
into another namespace. Use `variables.copy` for explicitly authorized sharing without
asking the user to submit a stored secret again.

An isolated Ghost key adds the `ghost_admin_url` required-input blocker. Only an
explicit same-message Ghost Admin URL declaration suppresses this initial blocker;
earlier public website URLs do not count. Intake merges other blockers and references
instead of replacing them.
The shared `supplied_ghost_admin_urls` parser is used by intake and Ghost setup.
It recognizes direct `connect Ghost at …` and labeled Admin URL/origin statements,
including a bare URL wrapped in quotes or backticks. Natural rechecks such as
“reconnecting its existing Ghost connection at http://cms:2368” and “Verify the
existing Ghost connection at https://cms.example/blog” also supply that explicit
destination. Rechecking preserves the existing connection identity; it does not
add publisher authority. Quoted instructions, fenced examples, negative instructions
and ambiguous alternatives do not supply authority.

Benign status prose such as “your original private key is unchanged” or “the admin
key is intact” remains visible. This exemption applies only to recognized status
words after an unquoted `is` assignment for API, admin or private key labels.
The optional adverbs `still` and `currently` require a following recognized state,
such as `valid`, `working`, `active`, `inactive`, `verified` or the existing states;
“the stored admin key is still valid and working” remains visible. An adverb alone
or an unrecognized following value does not receive this exemption.
Quoted values, colon/equal assignments, password assignments, private PEM keys and
actual supplied key material remain captured or redacted.

Historical credentials remain unchanged in authoritative storage. Stateless
`redact_legacy_text` and `redact_legacy_payload` projections protect API messages,
titles, list previews, tasks, questions, tool/run payloads and conversation journals,
as well as model history. Redaction happens before truncation; oversized historical
strings are omitted whole. Existing opaque secure-input references are preserved.

## Trusted connector consumption

`jhin_secrets.variables.bind_internal(ctx, variable_id, connection_id=...,
credential_field=..., approved_origin=...)` persists a connection/field/URL binding
after the connector's explicit setup authorization. `resolve_internal` checks the
current caller audience, binding, enabled connection and unchanged full canonical Admin URL before
decrypting a value in the trusted worker. It is not a public tool or route. Connection
configuration and encrypted rows are refreshed to defeat stale ORM snapshots. The
installation path is part of the pin: changing to another site on the same host fails.
Equivalent default ports and `/ghost` or `/ghost/api/admin` suffixes normalize to the
same installation. Empty, malformed or mismatched pins fail closed.

The lower-level `VariableStore.resolve_bound(..., allow_disabled=True)` exists only
for the authenticated human **Test connection** path. Verification preserves disabled
status. The runtime wrapper cannot select this override. Secrets are never injected
into model context or CLI environments by variable storage.

Workspace writes serialize with PostgreSQL `NO KEY UPDATE`. Deletion acquires bound
connection locks in stable order before removing the variable. The gateway holds
connection/variable credential locks while dispatching authorized work. Audits record
scope, version and sensitivity without values.
Credential `Secret` rows are locked exclusively before decryption because consumption
updates `last_used_at`; this prevents concurrent shared-lock upgrades from deadlocking.

## Repeatable checks

Focused tests cover HTTP sealed writes and no readback, copy provenance/idempotency,
CAS, private/team/company access, departed memberships, disabled agents, scoped
denies, cross-chat/user reference theft, expiry, invalid bytes, rotation and full-URL
pins, all chat entry paths, question retries, legacy endpoints, API-key ceilings,
negative/quoted scope instructions and historical read redaction before truncation.
Tool tests also cover empty-list scope discovery, bounded active-team results,
discovery without write authority, and an encrypted private-to-team-to-company copy
chain using the returned IDs. Ghost setup tests cover natural reconnect/verify
wording, retained connection identity and drafts-only behavior; intake tests retain
the negative/quoted URL and credential-material checks alongside benign status prose.

`packages/secrets/tests/test_variables_postgres.py` additionally checks concurrent CAS,
duplicate capture and plaintext-free journal rows, revocation while gateway locks
are held, concurrent consumption without a lock-upgrade deadlock, and real Ghost
binding concurrent with deletion using the same workspace-before-connection lock
order. It requires an isolated, migrated `TEST_DATABASE_URL`; fixtures are synthetic
and clean up their own rows. Migration `0048` follows `0047` and refuses downgrade
while variable or intake records exist. Additive migration `0051` follows `0050` and
backfills full Admin URL pins only from valid matching connection configurations;
unmatched legacy bindings retain an empty, unusable pin.

`scripts/verify_agent_work_evidence.py` performs bounded, GET-only verification of
retained live samples. Supply `--url`, `--workspace`, and `--conversation` for a
completed editorial sample; add `--key-only-conversation` for the missing-Admin-URL
scenario. Optional `--writer` and `--director` pin expected actors. Authentication
comes only from `JHIN_LIVE_API_KEY` in the process environment. It checks the pinned
draft/review/post, publisher identity, single completed handoff, returned director
result, resumed writer reply, and absence of effects before the required URL answer.
Output contains status and evidence identifiers, never credentials, draft contents,
message text or tool arguments. It does not contact Ghost or change any state.
