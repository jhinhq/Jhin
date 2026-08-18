# Phase 9 Vercel + Supabase Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship production-useful Vercel and Supabase integrations that let scoped agents inspect deployments and permitted database data while every production mutation remains bounded, auditable, and governed by the user's explicit approval policy; the default Balanced policy parks production changes for human approval.

**Architecture:** Extend the existing connector registry and tool gateway rather than adding provider-specific authorization paths. Vercel uses one access-token connection and a provider-supplied webhook secret; Supabase uses one connector manifest but two independent connection rows—one Management API token and one direct PostgreSQL DSN—so management-plane and database-plane authority never mix. Shared policy, secret, webhook, endpoint, and UI contracts are hardened before either provider is registered; deterministic dev-only fakes prove the complete flow without touching a real account or Jhin's own database.

**Tech Stack:** Python 3.13, FastAPI, Pydantic v2, SQLAlchemy 2, httpx, asyncpg 0.31+, SQLGlot `>=30.13,<31`, PostgreSQL 17, NATS JetStream, Temporal, pytest, Next.js, React, TypeScript, Vitest, Docker Compose.

**Spec:** `docs/implementation-plan.md` sections 11.1, 11.4, 11.5, 12, 13.5, 17.9, 21, 42, 48, and Phase 9.

## Global Constraints

- The product, tracked canonical plan, paths/examples, and every user-facing label remain **Jhin**. The separate untracked production-plan reference supplied by the user is preserved exactly as verified in Task 0A and must not be edited, staged, renamed, or deleted.
- PostgreSQL remains Jhin's source of truth, Temporal remains the durable workflow authority, and all agent tool calls continue through `ToolGateway`.
- No Phase 9 database migration is required; migration `0014` remains the single Alembic head.
- A provider credential, Vercel environment-variable value, PostgreSQL DSN, SQL cell, function source, raw webhook body, or unsanitized tool input must never enter a prompt, API response, audit record, exception, log, or persisted tool-call field outside the bounded outputs specified here.
- Vercel and Supabase tools require connection-scoped grants. A production-impacting Vercel action and every exposed Supabase database mutation use `RiskLevel.ELEVATED` or `RiskLevel.DESTRUCTIVE` and set `supports_approval=True`. Balanced is the default and requires approval for both; Autonomous may auto-run the elevated preview action but still parks destructive actions; only an explicit custom `AUTO` rule may auto-run a destructive action. `supports_approval` is not itself a non-overridable approval floor. Phase 9 exposes no agent DDL capability; schema changes remain reviewed operator migrations.
- A human approval is permission to retry authorization, not a frozen authorization result: resume must bind to the original workspace/agent/run/task/tool definition/connection credential revision and re-evaluate current grants, required scopes, policy rules, connection state, and tool-specific validators.
- Every structured tool call receives a versioned deterministic internal invocation UUID derived from its durable run ID, durable step index, and zero-based tool-call ordinal—never from a retry-variant model/provider call ID, arguments, credentials, or secret material. A bounded provider call ID may be integrity-bound to the persisted request, but it is not retry identity; persisted transcript tool-call/result pairing uses the canonical internal UUID. Regardless of whether policy returns `ALLOW` immediately or resumes an approval, an atomically committed `executing` claim precedes external side effects; terminal outcomes replay on activity retry, and an ambiguous post-claim failure becomes `execution_unknown` and is never automatically executed again. Transmit the internal UUID as a provider idempotency key only on an endpoint whose current official contract explicitly supports idempotency.
- Vercel webhook secrets use the provider's value and HMAC-SHA1 protocol. GitHub and Linear retain generated secrets and their existing algorithms.
- Webhook bodies are capped at `1_048_576` bytes before parsing or provider verification. Dotted provider event names are represented as individually validated NATS subject tokens.
- HTTP endpoints default to the official HTTPS origins. Non-official origins and database hosts work only when an operator explicitly adds their exact origin/host to the documented dev/self-host allowlists; localhost, link-local, metadata, private-IP, and redirect-based bypasses otherwise fail closed.
- A Supabase database connection uses a custom low-privilege login, never `postgres`, a superuser, or a `BYPASSRLS` role. Hosted connections require TLS. Jhin's application database is never accepted as the target.
- Supabase SQL accepts exactly one PostgreSQL statement, validates the entire SQLGlot AST against the fixed-risk tool selected by the model, executes the original parameterized SQL, and rejects unknown syntax rather than guessing its risk.
- Database reads use a read-only transaction, `statement_cache_size=0`, `search_path=pg_catalog`, deterministic relation locks and live catalog revalidation, a type-specific trusted wrapper that accepts only fixed-width outputs or safely sliceable direct `text`/`varchar` columns, one-row cursor fetch, server and client timeouts, and per-cell plus total-result byte caps. Unsupported or compressed variable-width outputs fail closed.
- Dev fakes live only in `compose.dev.yaml`, expose health checks, record side effects for assertions, and never appear in production-shaped `compose.yaml`.
- Use current official Vercel and Supabase API documentation at implementation time. The Supabase logs tool targets `/v1/projects/{ref}/analytics/endpoints/logs`, not the deprecated `logs.all` endpoint scheduled for removal on 2026-09-23.
- Each task is implemented test-first, receives a requirements review and code-quality review, and ends in the scoped commit shown. Do not carry unrelated working-tree changes into a task commit.
- Do not mark Phase 9 complete until the focused acceptance suite, all Python gates, all web gates, a clean production image build, and the full integration suite are freshly green.

## File Map

The following boundaries are intentional; do not collapse provider clients, schemas, policy, and executors into one module.

```text
packages/policy/src/jhin_policy/capabilities.py        required scoped-grant contract
packages/policy/src/jhin_policy/evaluator.py           pure grant/scope/policy decision
packages/tools/src/jhin_tools/gateway.py               live approval reauthorization
packages/tools/src/jhin_tools/invocation.py            stable run/step/ordinal invocation identity
packages/secrets/src/jhin_secrets/material.py          credential decode + leaf/URL-part registration
packages/secrets/src/jhin_secrets/redaction.py         longest-match secret scrubbing
packages/connectors/src/jhin_connectors/endpoints.py   outbound HTTP/Postgres target policy
packages/connectors/src/jhin_connectors/http_client.py streaming bounded provider HTTP responses
packages/connectors/src/jhin_connectors/manifest.py    typed/auth-specific config + webhook mode
packages/connectors/src/jhin_connectors/vercel/        Vercel client, DTOs, tools, webhook
packages/connectors/src/jhin_connectors/supabase/      Management API, SQL policy, DB executor
packages/connectors/src/jhin_connectors/testing/       dev-only fake provider applications
apps/api/src/jhin_api/connections/                     connection validation + webhook secret setup
apps/api/src/jhin_api/webhooks/                        bounded raw-body ingress
apps/web/components/scope-editor.tsx                   data-driven grant scope fields
tests/fixtures/supabase/init.sql                       isolated low-privilege fixture database
tests/integration/test_phase9_authorization.py         real-Postgres invocation/approval races
tests/integration/test_phase9_exit.py                  Phase 9 end-to-end acceptance
```

---

### Task 0A: Verify the completed canonical Jhin branding migration

**Files:**
- Modify: `docs/implementation-plan.md`

**Interfaces:**
- Consumes: the former working-name prose, package/path examples, event subjects, secret paths, and commands in the tracked canonical plan.
- Produces: one canonical Jhin name and `jhin` identifier prefix throughout tracked repository content, with the user-owned untracked reference preserved.

Commit `d8d1055` (`docs: standardize product name as Jhin`) already performed the scoped mechanical migration before Phase 9 implementation work. It replaced the legacy display name with `Jhin` and its lowercase repository/package/example prefix with `jhin` in `docs/implementation-plan.md`; it did not edit or stage the user's untracked production-plan reference.

- [x] **Step 1: Verify the migration commit and repository-wide tracked branding gate**

Run:

```bash
git show --stat --oneline d8d1055
if git grep -nEi '[o]rg[f]orge' -- .; then
  echo "Legacy product branding remains in tracked content"
  exit 1
fi
untracked_reference='org''forge-production-implementation-plan.md'
test "$(git status --short -- "$untracked_reference")" = "?? $untracked_reference"
```

Expected: commit `d8d1055` changes only `docs/implementation-plan.md`; the repository-wide tracked-content assertion returns no matches; the separately assembled exact reference path remains untracked. `git grep` searches tracked files only, so this gate neither reads nor creates an exception for that user-owned file.

### Task 0B: Check in the reviewed Phase 9 execution plan

**Files:**
- Create: `docs/superpowers/plans/2026-08-17-phase-9-vercel-supabase.md`

**Interfaces:**
- Consumes: the reviewed Phase 9 design and this task sequence.
- Produces: a tracked, immutable execution baseline before Task 1 changes continue.

- [ ] **Step 1: Stage only this plan and verify the index**

Run:

```bash
git add docs/superpowers/plans/2026-08-17-phase-9-vercel-supabase.md
git diff --cached --name-only
git diff --cached --check
```

Expected: the staged-name output contains exactly `docs/superpowers/plans/2026-08-17-phase-9-vercel-supabase.md`. If any unrelated path is already staged, stop and separate it before committing; do not sweep existing Task 1 work or the user-owned untracked production-plan reference into this commit.

- [ ] **Step 2: Commit the execution baseline**

```bash
git commit -m "docs: add Phase 9 Vercel and Supabase plan"
```

Expected: the plan is tracked before the first implementation commit, so later task reviews compare code against a stable plan rather than an untracked working file.

### Task 1: Harden scoped grants, approval resume, and credential redaction

**Files:**
- Modify: `docs/superpowers/plans/2026-08-17-phase-9-vercel-supabase.md`
- Create: `packages/secrets/src/jhin_secrets/material.py`
- Modify: `packages/secrets/src/jhin_secrets/__init__.py`
- Modify: `packages/secrets/src/jhin_secrets/store.py`
- Modify: `packages/secrets/src/jhin_secrets/redaction.py`
- Modify: `packages/connectors/src/jhin_connectors/execution.py`
- Modify: `packages/domain/src/jhin_domain/enums.py`
- Modify: `packages/policy/src/jhin_policy/capabilities.py`
- Modify: `packages/policy/src/jhin_policy/evaluator.py`
- Create: `packages/tools/src/jhin_tools/invocation.py`
- Modify: `packages/tools/src/jhin_tools/__init__.py`
- Modify: `packages/tools/src/jhin_tools/builtin.py`
- Modify: `packages/tools/src/jhin_tools/gateway.py`
- Modify: `packages/tools/src/jhin_tools/sanitize.py`
- Modify: `packages/observability/src/jhin_observability/logging.py`
- Modify: `packages/workflows/src/jhin_workflows/agent_task/shared.py`
- Modify: `packages/workflows/src/jhin_workflows/agent_task/workflows.py`
- Modify: `packages/workflows/src/jhin_workflows/delegated_task/shared.py`
- Modify: `services/agent_worker/src/jhin_agent_worker/activities.py`
- Modify: `apps/api/src/jhin_api/connections/service.py`
- Modify: `apps/api/src/jhin_api/approvals/service.py`
- Modify: `apps/api/src/jhin_api/policy/schemas.py`
- Modify: `apps/api/src/jhin_api/policy/router.py`
- Modify: `apps/api/src/jhin_api/policy/service.py`
- Test: `packages/secrets/tests/test_store.py`
- Test: `packages/secrets/tests/test_redaction.py`
- Test: `packages/connectors/tests/test_execution.py`
- Test: `packages/policy/tests/test_evaluator.py`
- Test: `packages/tools/tests/test_invocation.py`
- Test: `packages/tools/tests/test_gateway.py`
- Test: `packages/tools/tests/test_gateway_concurrency.py`
- Test: `packages/tools/tests/test_sanitize.py`
- Test: `packages/observability/tests/test_logging.py`
- Test: `packages/workflows/tests/test_agent_task_delegation.py`
- Test: `packages/workflows/tests/test_delegated_task_workflow.py`
- Test: `services/agent_worker/tests/test_approval_activity.py`
- Test: `services/agent_worker/tests/test_delegation_activities.py`
- Test: `services/agent_worker/tests/test_phase9_invocation_activity.py`
- Test: `apps/api/tests/test_connections_unit.py`
- Test: `apps/api/tests/test_approvals_unit.py`
- Test: `apps/api/tests/test_policy_unit.py`
- Test: `apps/api/tests/test_policy_rbac.py`
- Test: `tests/integration/test_phase9_authorization.py`

**Interfaces:**
- Consumes: existing `ToolDefinition.scope_keys`, `evaluate(...)`, `sanitize_payload(...)`, `Approval`, `ToolCall`, `AgentCapabilityGrant`, and encrypted JSON credential blobs.
- Produces: `ToolDefinition.required_grant_scope_keys: tuple[str, ...]`; `decode_string_secret_map(plaintext: str) -> dict[str, str]`; bounded `register_secret_material(plaintext: str) -> None`; longest-first redaction; live approval reauthorization; `stable_tool_invocation_id(run_id, step_index, tool_call_ordinal) -> UUID`; a durable claim/replay/`execution_unknown` lifecycle for both immediate and approved calls; `ToolOut.scope_keys`; `ToolOut.required_grant_scope_keys`; multiple same-capability grants when their scope differs.

- [ ] **Step 1: Write failing pure policy and secret-material tests**

Add explicit tests with these assertions:

```python
def test_required_grant_scope_keys_reject_unscoped_allow() -> None:
    tool = scoped_tool(required_grant_scope_keys=("connection_id", "project_id"))
    decision = evaluate(
        tool,
        grants=[Grant(capability=tool.required_capability, scope={})],
        rules=[],
        requested_scope={"connection_id": "c1", "project_id": "p1"},
    )
    assert decision.code == "required_scope_missing"

def test_required_grant_scope_keys_accept_matching_scoped_allow() -> None:
    tool = scoped_tool(required_grant_scope_keys=("connection_id", "project_id"))
    decision = evaluate(
        tool,
        grants=[Grant(
            capability=tool.required_capability,
            scope={"connection_id": "c1", "project_id": "p1"},
        )],
        rules=[],
        requested_scope={"connection_id": "c1", "project_id": "p1"},
    )
    assert decision.code == "granted"

def test_required_grant_scope_keys_must_be_declared_scope_keys() -> None:
    with pytest.raises(ValidationError, match="required grant scope"):
        scoped_tool(
            scope_keys=("connection_id",),
            required_grant_scope_keys=("connection_id", "project_id"),
        )

def test_decode_secret_map_registers_full_json_and_each_string_leaf() -> None:
    plaintext = (
        '{"access_token":"token-six-plus",'
        '"database_url":"postgresql://db_user:password-six-plus@db.test/app?sslkey=query-secret"}'
    )
    assert decode_string_secret_map(plaintext)["access_token"] == "token-six-plus"
    register_secret_material(plaintext)
    assert redact_text("Bearer token-six-plus") == "Bearer [REDACTED]"
    assert redact_text("password-six-plus") == "[REDACTED]"
    assert redact_text("query-secret") == "[REDACTED]"

def test_decode_secret_map_rejects_non_string_leaf() -> None:
    with pytest.raises(SecretMaterialError):
        decode_string_secret_map('{"token": 7}')
```

Also cover malformed JSON, non-object JSON, nested string leaves for redaction, percent-encoded URL credentials, repeated/generic query parameters, overlapping values, rotation, reveal, maximum plaintext bytes, traversal depth, fragment count, and query-field count. Keep credential decoding strict: connection credential maps remain top-level `dict[str, str]`; recursive traversal exists only to register every string leaf defensively. For each string leaf, register the whole value and, when it parses as a URL/DSN, the decoded username, password, and every nonempty decoded query-string value so an embedded database password is redacted even when it appears outside the full DSN. Exceeding a bound fails secret creation/rotation safely rather than silently omitting a fragment. Redaction always replaces longest registered values first so overlapping credentials cannot partially leak.

- [ ] **Step 2: Run the pure tests to verify RED**

Run:

```bash
uv run pytest packages/policy/tests/test_evaluator.py packages/secrets/tests/test_store.py packages/connectors/tests/test_execution.py -q
```

Expected: FAIL because `required_grant_scope_keys`, `material.py`, and leaf registration do not exist.

- [ ] **Step 3: Implement required grant scopes and shared credential material handling**

Add this frozen field to `ToolDefinition`, add a model validator requiring `set(required_grant_scope_keys).issubset(scope_keys)`, forbid combining nonempty required keys with `defers_scope=True`, and export both tuples through the API tool catalog:

```python
required_grant_scope_keys: tuple[str, ...] = ()
```

In `evaluate`, an allow grant for a non-deferred tool is eligible only when it both matches the requested scope and contains every required key:

```python
def _allow_covers(tool: ToolDefinition, grant: Grant, requested: Mapping[str, Any]) -> bool:
    required = set(tool.required_grant_scope_keys)
    return required.issubset(grant.scope) and scope_matches(grant.scope, requested)
```

Return code `required_scope_missing` when a matching capability exists but no allow grant carries every mandatory scope dimension; retain `scope_mismatch` when keys exist but values do not match. Deferred-scope tools keep their current validator-owned behavior and cannot declare this generic required-scope contract.

Implement `material.py` with hard byte/depth/fragment/query-field limits so `register_secret_material` registers the full plaintext, recursively registers bounded string leaves from valid JSON, and extracts all decoded URL credential/query parts. `decode_string_secret_map` returns only a strict top-level string map and raises `SecretMaterialError` with a credential-safe message. Call registration from `SecretStore.create`, `reveal`, and `rotate`; replace ad-hoc JSON decoders in both `resolve_connection` and API verification/metadata with the shared decoder. Sort registered values longest-first in `SecretRedactor.redact_text`.

- [ ] **Step 4: Write failing durable-invocation, approval-resume, and duplicate-scope tests**

Add gateway tests proving all of these transitions without executing the fake executor:

```python
@pytest.mark.parametrize("change", ["grant_revoked", "explicit_deny", "policy_forbid"])
async def test_approved_call_reauthorizes_live_state(change: str, gateway_fixture) -> None:
    approval_id, effects = await gateway_fixture.park_scoped_call()
    await gateway_fixture.apply(change)
    outcome = await gateway_fixture.gateway.resolve_approved(approval_id)
    assert outcome.status == "denied"
    assert effects == []

async def test_approved_call_is_bound_to_original_agent_run_and_task(gateway_fixture) -> None:
    approval_id, effects = await gateway_fixture.park_scoped_call()
    other_context = gateway_fixture.context_for_other_agent_run_task()
    with pytest.raises(GatewayStateError, match="does not belong"):
        await ToolGateway(other_context, gateway_fixture.catalog).resolve_approved(approval_id)
    assert effects == []

```

Also add tests for wrong workspace; a tool-specific validator changing to DENY while parked; capability/risk/input/approval-format drift under the same tool name; connection credential rotation, public-config change, disablement, and deletion; every fail-closed branch emitting an audit event; single-use resolution; terminal outcome replay; and an `executing`/`execution_unknown` row never invoking the executor.

In `test_invocation.py`, add pure derivation tests proving invocation format version 1 uses exactly `(run_id, durable_step_index, zero_based_tool_call_ordinal)`: the same tuple returns the same UUID across sessions and different provider call IDs, while changing any tuple member changes the UUID. It must not hash or serialize tool arguments, credentials, connection secrets, or the provider call ID into retry identity. Add collision tests proving a retry at the same tuple with a different tool name or validated input fails closed and audits `invocation_mismatch` rather than executing either version.

In `test_gateway_concurrency.py` and `test_phase9_authorization.py`, cover immediate-policy execution as rigorously as approval execution:

```text
- Autonomous ELEVATED call: two PostgreSQL sessions race the same invocation;
  one claims/executes and the other replays, with one external effect.
- Explicit custom AUTO DESTRUCTIVE call: same race and one effect (the
  Autonomous preset itself must still return REQUIRE_APPROVAL for destructive).
- AUTO call whose fake executor applies its external effect and then raises a
  transport error: status becomes execution_unknown; an activity retry and a
  second gateway request return the same row and never invoke it again.
- AUTO call that crashes after the committed executing claim but before the
  executor: retry converts/remains execution_unknown with zero effects.
- AUTO terminal result committed before transcript persistence: run-step retry
  reconstructs the tool_call/tool_result bundle with the canonical internal
  UUID and creates no duplicate message, run event, or provider side effect.
```

Assert the run-step activity passes stable `step_index` and call ordinal, enumerated in the provider-returned order, to the gateway; it must not use `provider_call_id` as the database key. Persisted transcript pairing uses the internal invocation UUID, so a retried LLM response that chooses a different provider call ID cannot create a new execution identity.

Before effect zero, add activity tests for a durable, ordered, provider-independent whole-step manifest. Prove dropped, appended, reordered, renamed, and changed-input calls on a retry cannot execute a new effect; JSON whitespace/key order and regenerated provider IDs remain equivalent. The persisted manifest may contain only redact-then-bounded tool names and canonical strict-JSON arguments for which `sanitize_payload` is structurally lossless—never raw/unsalted hashes, provider IDs, credentials, or any argument changed by sanitization. A first non-lossless response stops the run before any claim/effect; a differing overlapping retry is retryable and must not poison the canonical attempt. Reject `NaN`, positive/negative `Infinity`, exponent-overflow numbers that decode non-finite, duplicate JSON keys, invalid JSON, and non-object arguments before the manifest or gateway can dispatch an executor.

Add approval-decision service tests proving a row-locked pending transition has one durable winner, an opposite concurrent decision receives `409`, the decision audit is single-copy, and only the winning decision is signaled. A same-decision retry must re-signal without adding another audit so a commit-to-Temporal-signal failure is repairable. Add delegated-result stitching tests proving the canonical gateway UUID is forwarded, validated, and used for an idempotent message/event bundle; retain an explicitly audited provider-ID fallback only for already-running pre-Phase-9 workflows whose serialized request lacks the new field.

Implement `test_approval_staging_rejects_non_lossless_sanitized_input` twice through the existing gateway fixture: once with a schema-valid string of `MAX_STRING_CHARS + 1` characters, and once with a schema-valid string containing a value pre-registered in `get_redactor()`. In both cases assert `outcome.status == "denied"`, `outcome.decision_code == "approval_input_not_lossless"`, the executor effect list is empty, and a query for pending `Approval` rows returns zero.

Add an API service test in `apps/api/tests/test_policy_unit.py` that creates two `allow` grants for the same capability/effect with distinct `scope_json`, then proves an exact duplicate still returns `409`. Use two PostgreSQL sessions to prove the target-agent row lock serializes racing exact duplicates.

- [ ] **Step 5: Run the authorization tests to verify RED**

Run:

```bash
uv run pytest packages/tools/tests/test_invocation.py packages/tools/tests/test_gateway.py packages/tools/tests/test_gateway_concurrency.py packages/tools/tests/test_sanitize.py services/agent_worker/tests/test_phase9_invocation_activity.py apps/api/tests/test_policy_unit.py apps/api/tests/test_approvals_unit.py -q
```

Expected: FAIL because immediate calls have no stable durable claim/replay path, approval resume is workspace-only, sanitizer changes can be staged, and duplicate lookup ignores scope.

- [ ] **Step 6: Implement stable claims, lossless staging, binding, and live reauthorization**

In `jhin_tools/invocation.py`, define a checked-in namespace UUID and `TOOL_INVOCATION_FORMAT_VERSION = 1`; keep the separate `APPROVAL_FORMAT_VERSION = 2` with approval/gateway code so the two formats cannot be confused. Implement and export:

```python
def stable_tool_invocation_id(
    run_id: UUID,
    step_index: int,
    tool_call_ordinal: int,
) -> UUID:
    require non-negative bounded integers
    return uuid.uuid5(
        TOOL_INVOCATION_NAMESPACE,
        f"v1:{run_id.hex}:{step_index}:{tool_call_ordinal}",
    )
```

`run_agent_step` enumerates model tool calls in their returned order and passes the durable step index plus ordinal to `ToolGateway.request`; the gateway derives and uses that UUID as `ToolCall.id`. The provider call ID may be checked/bound as bounded transcript metadata, but never changes this identity. Use the canonical UUID string for persisted `tool_call`/`tool_result` message pairing. Run gateway authorization/execution in a fresh database session, separate from the activity's pending transcript/run-event transaction, so committing a pre-effect claim cannot accidentally commit half of an outer activity bundle.

For every call, first look up/lock the deterministic row. Require the existing workspace/run/agent/tool/connection and exact validated JSON input to match; otherwise audit and return `invocation_mismatch`. Replay terminal rows and the same pending approval without creating a row or side effect. For a newly immediate `ALLOW`, atomically insert the row as `executing`, add the requested/claimed audits, and commit before calling the executor. Approval staging inserts the same deterministic row as `pending_approval`; approval resolution atomically changes it to `executing` and commits before execution. Hold a PostgreSQL session-level advisory lifecycle lock keyed from the invocation UUID across claim commit, dispatch, and terminal commit (with an in-process keyed fallback only for portable SQLite tests). A live duplicate waits and then replays; only a duplicate that acquires the released lock and finds an orphan `executing` claim may mark it `execution_unknown`. Commit a definitive terminal result before returning it to the activity. A failure proven to occur before any external dispatch may be `failed`; a timeout, connection loss, process crash, or database-commit ambiguity after dispatch is `execution_unknown`. Neither state is automatically executed again.

After the gateway returns, the activity writes the canonical transcript/run-event bundle under the internal invocation UUID and checks for an existing bundle first. Thus a crash after the gateway's terminal commit but before the outer bundle commit is repaired on activity retry without a second executor call or duplicate messages/events. Extend `GatewayStatus`/`ToolCallStatus` and observation handling for `executing` and `execution_unknown`; an unknown result is committed and then fails the step non-retryably so the workflow cannot advance to a new model step and repeat the uncertain mutation. Carry the canonical invocation ID through approval and delegated-result stitching. Do not add a migration because the existing `ToolCall.id` UUID and string status columns carry the new protocol.

Bind the complete ordered call set before effect zero under an `AgentRun FOR UPDATE` lock. Store only canonical strict-JSON objects and tool names that remain byte-for-byte equivalent after recursive redaction and configured bounds; exclude provider IDs and never persist a hash of raw arguments. A first non-lossless response durably stops before a tool claim, while a different overlapping retry rolls back and raises retryably so it cannot poison a canonical attempt already in flight. Persist the full step result as an `agent.step.committed` marker in the same transaction as counters/transcript/events; activity replay returns that marker without calling the model again. Canonical delegated-result delivery similarly locks the parent run and deduplicates the message/event bundle by gateway UUID; an empty UUID is accepted only as an audited compatibility path for pre-upgrade workflows and falls back to a redacted, bounded provider ID.

Before creating an `Approval`, require structural equality between the JSON-mode validated input and `sanitized_input`. If redaction or truncation changed it, record a denied tool call with `approval_input_not_lossless`; do not persist an approval. Persist `approval_format_version=2`, the tool capability/risk, and—when `connection_id` is present—a one-way authorization digest over connection ID, auth type, status, canonical public config, and current secret fingerprint/key version. Never persist raw fingerprint or credential material. Bound approved/rejected resolution to all of:

```python
approval.workspace_id == ctx.workspace_id
approval.requested_by_agent_id == ctx.agent_id
approval.run_id == ctx.run_id
approval.task_id == ctx.task_id
tool_call.workspace_id == ctx.workspace_id
tool_call.agent_id == ctx.agent_id
tool_call.run_id == ctx.run_id
tool_call.approval_id == approval.id
approval.action_type == tool_call.tool_name
approval.action_payload_sanitized["input"] == tool_call.sanitized_input_json
approval.action_payload_sanitized["capability"] == current_definition.required_capability
approval.action_payload_sanitized["risk"] == current_definition.risk.value
approval.action_payload_sanitized["approval_format_version"] == 2
approval.action_payload_sanitized["invocation_format_version"] == 1
approval.action_payload_sanitized["invocation_id"] == str(tool_call.id)
approval.action_payload_sanitized["connection_authorization_digest"] == current_digest
```

After re-validating the losslessly stored input, rebuild `requested_scope`, reload current grants and policy rules, call `evaluate`, and re-run the tool-specific validator. A live `DENY` marks the same tool-call row denied and audits the reason. `REQUIRE_APPROVAL` is satisfied by the exact approved row; `ALLOW` may proceed. Re-check the resolved connection during executor execution as it does for a normal call.

The same claim helper serves immediate and approved calls; do not maintain an approval-only execution path. Only the claimant may call the executor. Provider/tool clients receive the deterministic `tool_call_id` as the stable internal invocation ID, but may put it on the wire only through an endpoint-specific idempotency field documented by the provider. Add real PostgreSQL two-session races for Autonomous ELEVATED, custom-AUTO DESTRUCTIVE, and approved calls, plus retry-after-terminal-commit and post-dispatch-unknown tests.

In `create_grant`, lock the target agent row before checking/inserting and include exact JSON scope equality in the duplicate predicate so only `(agent, capability, effect, scope)` duplicates conflict under concurrent requests.

In the approval API, lock the pending approval row before deciding. Opposing decisions have exactly one durable winner and one audit/signal; same-decision retries re-signal the already-durable decision without duplicating its transition. Run finalization takes the same approval row lock and cancels only an approval still pending plus a tool-call row still `pending_approval`, so it cannot overwrite an approved/rejected decision or an executing/terminal call. Redact and bound every provider-controlled value before persistence or response, including mapping keys and rendered exception tracebacks. Run traceback redaction after `dict_tracebacks`, safely stringify unknown log objects before rendering, and raise sanitized provider activity/API errors `from None` so raw credential-reflecting causes are not retained.

- [ ] **Step 7: Run focused regressions and commit**

Run:

```bash
uv run pytest packages/policy/tests packages/tools/tests packages/secrets/tests packages/connectors/tests/test_execution.py packages/observability/tests/test_logging.py packages/workflows/tests apps/api/tests/test_approvals_unit.py apps/api/tests/test_connections_unit.py apps/api/tests/test_policy_unit.py apps/api/tests/test_policy_rbac.py services/agent_worker/tests -q
uv run pytest -m integration tests/integration/test_phase9_authorization.py -v
uv run ruff check packages/domain packages/policy packages/tools packages/secrets packages/connectors packages/observability packages/workflows apps/api services/agent_worker tests/integration/test_phase9_authorization.py
uv run ruff format --check packages/domain packages/policy packages/tools packages/secrets packages/connectors packages/observability packages/workflows apps/api services/agent_worker tests/integration/test_phase9_authorization.py
uv run mypy
git diff --check
```

Expected: PASS, including immediate-policy crash/race/replay/unknown behavior, revoke-after-approval, wrong-context, definition/credential drift, lossless-input, required-scope, longest-first leaf redaction, shared atomic claims, canonical transcript repair, and serialized scoped-grant cases.

Commit:

```bash
git add packages/domain/src/jhin_domain/enums.py packages/policy/src/jhin_policy/capabilities.py packages/policy/src/jhin_policy/evaluator.py packages/policy/tests/test_evaluator.py packages/tools/src/jhin_tools/__init__.py packages/tools/src/jhin_tools/builtin.py packages/tools/src/jhin_tools/invocation.py packages/tools/src/jhin_tools/gateway.py packages/tools/src/jhin_tools/sanitize.py packages/tools/tests/test_invocation.py packages/tools/tests/test_gateway.py packages/tools/tests/test_gateway_concurrency.py packages/tools/tests/test_sanitize.py packages/secrets/src/jhin_secrets/__init__.py packages/secrets/src/jhin_secrets/material.py packages/secrets/src/jhin_secrets/store.py packages/secrets/src/jhin_secrets/redaction.py packages/secrets/tests/test_store.py packages/secrets/tests/test_redaction.py packages/connectors/src/jhin_connectors/execution.py packages/connectors/tests/test_execution.py packages/observability/src/jhin_observability/logging.py packages/observability/tests/test_logging.py packages/workflows/src/jhin_workflows/agent_task/shared.py packages/workflows/src/jhin_workflows/agent_task/workflows.py packages/workflows/src/jhin_workflows/delegated_task/shared.py packages/workflows/tests/test_agent_task_delegation.py packages/workflows/tests/test_delegated_task_workflow.py apps/api/src/jhin_api/approvals/service.py apps/api/src/jhin_api/connections/service.py apps/api/src/jhin_api/policy/schemas.py apps/api/src/jhin_api/policy/router.py apps/api/src/jhin_api/policy/service.py apps/api/tests/test_approvals_unit.py apps/api/tests/test_connections_unit.py apps/api/tests/test_policy_unit.py apps/api/tests/test_policy_rbac.py services/agent_worker/src/jhin_agent_worker/activities.py services/agent_worker/tests/test_approval_activity.py services/agent_worker/tests/test_delegation_activities.py services/agent_worker/tests/test_phase9_invocation_activity.py tests/integration/test_phase9_authorization.py docs/superpowers/plans/2026-08-17-phase-9-vercel-supabase.md
git commit -m "fix: harden scoped tool approval authorization"
```

### Task 2: Add typed connector settings, provider-supplied webhooks, body caps, and endpoint policy

**Files:**
- Create: `packages/connectors/src/jhin_connectors/endpoints.py`
- Create: `packages/connectors/src/jhin_connectors/http_client.py`
- Modify: `packages/connectors/src/jhin_connectors/manifest.py`
- Modify: `packages/connectors/src/jhin_connectors/base.py`
- Modify: `packages/connectors/src/jhin_connectors/example/manifest.py`
- Modify: `packages/connectors/src/jhin_connectors/github/manifest.py`
- Modify: `packages/connectors/src/jhin_connectors/github/connector.py`
- Modify: `packages/connectors/src/jhin_connectors/github/client.py`
- Modify: `packages/connectors/src/jhin_connectors/github/auth.py`
- Modify: `packages/connectors/src/jhin_connectors/linear/manifest.py`
- Modify: `packages/connectors/src/jhin_connectors/linear/connector.py`
- Modify: `packages/connectors/src/jhin_connectors/linear/client.py`
- Modify: `packages/connectors/src/jhin_connectors/cli/tools.py`
- Modify: `packages/connectors/src/jhin_connectors/__init__.py`
- Modify: `packages/events/src/jhin_events/subjects.py`
- Modify: `.env.example`
- Modify: `compose.dev.yaml`
- Modify: `docs/superpowers/plans/2026-08-17-phase-9-vercel-supabase.md`
- Modify: `apps/api/src/jhin_api/connections/schemas.py`
- Modify: `apps/api/src/jhin_api/connections/service.py`
- Modify: `apps/api/src/jhin_api/connections/router.py`
- Modify: `apps/api/src/jhin_api/webhooks/router.py`
- Modify: `apps/api/src/jhin_api/webhooks/service.py`
- Test: `packages/connectors/tests/test_manifest_registry.py`
- Test: `packages/connectors/tests/test_endpoints.py`
- Test: `packages/connectors/tests/test_http_client.py`
- Test: `packages/connectors/tests/test_client_endpoint_security.py`
- Test: `packages/connectors/tests/github/test_tools_against_fake.py`
- Test: `packages/connectors/tests/linear/test_linear_tools_against_fake.py`
- Test: `packages/connectors/tests/cli/test_executors.py`
- Test: `packages/events/tests/test_subjects.py`
- Test: `apps/api/tests/test_connections_unit.py`
- Test: `apps/api/tests/test_webhooks_unit.py`
- Test: `tests/test_compose_connector_allowlist.py`

**Interfaces:**
- Consumes: `ConnectorManifest`, `ConfigFieldSpec`, `Connector.verify_connection`, generic connection create/rotate, and generic webhook ingress.
- Produces: `ConfigFieldKind = Literal["text", "integer", "boolean", "string_list"]`; auth-specific fields/defaults/bounds; `WebhookSecretMode = Literal["none", "generated", "provider_supplied"]`; normalized settings; provider-secret write-only endpoint; webhook configured status; credential-safe bounded parsing for every connection secret write; safe filtering of legacy public config; exact outbound target validation; shared redirect-free streaming provider HTTP with a 512 KiB cap and optional exact-success-status contract; runtime endpoint enforcement for both new and legacy GitHub/Linear connection rows; dotted ingress events; deterministic ingress event IDs; bounded webhook body reader.

- [ ] **Step 1: Write failing manifest, endpoint-policy, and shared HTTP tests**

Cover these contracts explicitly:

```python
def test_config_fields_filter_by_auth_and_apply_typed_defaults() -> None:
    normalized = normalize_config(
        manifest,
        "postgres",
        {"allowed_schemas": ["public"], "statement_timeout_ms": "5000"},
    )
    assert normalized["statement_timeout_ms"] == 5000
    assert normalized["allow_writes"] is False
    assert "management_base_url" not in normalized

@pytest.mark.parametrize("url", [
    "http://169.254.169.254/latest/meta-data",
    "http://127.0.0.1:9000",
    "https://user:pass@example.com",
])
def test_unapproved_http_target_is_rejected(url: str) -> None:
    with pytest.raises(EndpointPolicyError):
        validate_http_origin(url, official_origins=("https://api.vercel.com",))
```

Add three named endpoint tests alongside it: `test_exact_operator_allowlisted_origin_is_accepted` sets `JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS` to one exact origin and asserts its normalized return; `test_hosted_supabase_dsn_requires_tls_and_expected_host_shape` rejects an official-host DSN with `sslmode=disable`; and `test_jhin_database_target_is_rejected` passes the application and target DSNs with the same host/database and asserts `EndpointPolicyError` without either password in `str(exc)`.

In `test_http_client.py`, test the exact shared primitive:

```python
MAX_PROVIDER_RESPONSE_BYTES = 524_288

async def send_bounded_json(
    client: httpx.AsyncClient,
    request: httpx.Request,
    *,
    max_response_bytes: int = MAX_PROVIDER_RESPONSE_BYTES,
) -> Any: ...
```

Name the tests `test_redirect_response_is_rejected_without_following`, `test_content_length_over_512_kib_is_rejected_before_read`, `test_chunked_response_stops_before_buffering_over_512_kib`, `test_exact_512_kib_json_response_is_accepted`, and `test_provider_error_is_credential_safe`. The chunked case must instrument the response iterator: once the next chunk would exceed the cap, the helper raises without appending that chunk and closes the response. Assert no second redirect request, no call to `Response.aread()`, no buffered object larger than `524_288` bytes, and no bearer token or URL credential in the error or captured logs.

`validate_http_origin` must compare normalized scheme/host/port exactly and reject URL credentials/fragments. `validate_postgres_target` must accept an official `db.<ref>.supabase.co` host only when `<ref>` equals configured `project_ref`; a `*.pooler.supabase.com` DSN must carry a username ending in `.<project_ref>`. Hosted targets require TLS. An exact `JHIN_CONNECTOR_ALLOWED_DB_HOSTS` entry may opt a self-host/dev target in without the hosted naming/TLS rule; the validator must still reject the configured Jhin application database host/database pair and all other local/private targets. Both API verification and agent-worker execution pass their process `DATABASE_URL` as `app_database_url`; normalize `postgresql+asyncpg` and `postgresql` schemes before comparing host, port, and database name, and never compare or emit passwords.

- [ ] **Step 2: Write failing connection/webhook/subject tests**

Add tests named `test_generated_connector_returns_one_time_secret`, `test_provider_supplied_connector_returns_url_without_generated_secret`, `test_admin_can_store_and_rotate_provider_webhook_secret_without_readback`, `test_viewer_cannot_store_provider_webhook_secret`, `test_webhook_secret_write_requires_csrf`, and `test_connection_output_only_exposes_webhook_secret_configured_boolean`. The first two assert the mode-specific `WebhookSetupOut`; the next three assert `PUT /webhook-secret` returns `204`, rejects viewer/CSRF calls, rotates ciphertext, and never returns plaintext; the last recursively scans serialized connection responses for the submitted secret and asserts only `webhook_secret_configured` changes from false to true.

Keep the subject assertion literal:

```python
def test_dotted_ingress_event_becomes_individual_subject_tokens() -> None:
    assert ingress_subject("w1", "vercel", "deployment.ready") == (
        "jhin.v1.w1.ingress.vercel.deployment.ready"
    )
```

Implement `test_webhook_body_exactly_one_mib_is_accepted`, `test_webhook_body_one_byte_over_cap_is_rejected_before_parse_or_publish`, `test_one_huge_asgi_chunk_is_rejected_before_copy`, and `test_content_length_over_cap_rejects_before_stream_iteration`. For the `1_048_577`-byte and one-huge-chunk cases, assert `413`, no extension of the accumulator with the offending chunk, no `WebhookDelivery`, no NATS publish, and no provider parser invocation. A declared `Content-Length > 1_048_576` may reject early, but missing/malformed/false-small headers still use the streaming backstop. Add a direct `process_delivery` over-limit test so service callers cannot bypass the route cap.

Add `test_ingress_event_id_is_stable_for_connector_connection_and_delivery` and `test_retry_after_publish_before_commit_reuses_event_id`. Derive the UUID from exactly `(connector_type, connection.id, raw.delivery_id)` with a fixed checked-in UUIDv5 namespace and an unambiguous canonical tuple encoding. In the crash test, make JetStream publish succeed, inject a failure immediately before the database commit, assert the transaction rolls back and the provider receives a retryable `503`, then retry through a fresh session. Both publishes must carry the same `Nats-Msg-Id` and envelope `event_id`; the retry leaves exactly one `WebhookDelivery` row. Task 4 proves that the derived canonical Vercel event and trigger are also emitted once.

- [ ] **Step 3: Run focused tests to verify RED**

Run:

```bash
uv run pytest packages/connectors/tests/test_manifest_registry.py packages/connectors/tests/test_endpoints.py packages/connectors/tests/test_http_client.py packages/connectors/tests/test_client_endpoint_security.py packages/connectors/tests/github/test_tools_against_fake.py packages/connectors/tests/linear/test_linear_tools_against_fake.py packages/connectors/tests/cli/test_executors.py packages/events/tests/test_subjects.py apps/api/tests/test_connections_unit.py apps/api/tests/test_webhooks_unit.py tests/test_compose_connector_allowlist.py -q
```

Expected: FAIL because typed/auth-specific fields, endpoint policy, bounded shared HTTP, `provider_supplied`, deterministic ingress IDs, dotted event handling, and body caps do not exist.

- [ ] **Step 4: Implement the connector contract and target validators**

Extend `ConfigFieldSpec` with:

```python
kind: Literal["text", "integer", "boolean", "string_list"] = "text"
auth_types: tuple[str, ...] = ()
default: Any | None = None
minimum: int | None = None
maximum: int | None = None
```

Add `normalize_config(manifest, auth_type, submitted) -> dict[str, Any]` that rejects fields not declared for that auth type, coerces only the four declared kinds, applies defaults, and enforces required/min/max. `ConfigFieldSpec` remains public-only and has no way to mark a config value secret; secret input remains exclusive to `AuthSchemeSpec.secret_fields`. Add `Connector.validate_settings(auth_type, config) -> dict[str, Any]` as an overridable post-normalization hook; the default returns a copy.

Add `ConnectorManifest.webhook_secret_mode`, `webhook_signature_algorithm`, and `webhook_setup_help`. Validate that connectors with events use either `generated` or `provider_supplied`; set GitHub/Linear to `generated` with their real algorithm. Retain `supports_webhooks` in the serialized API for compatibility, derived/validated against mode rather than acting as the source of truth.

Implement `endpoints.py` with the exact interfaces `validate_http_origin(raw: str, *, official_origins: tuple[str, ...], allowlist_env: str = "JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS") -> str` and `validate_postgres_target(dsn: str, *, project_ref: str, app_database_url: str | None, allowlist_env: str = "JHIN_CONNECTOR_ALLOWED_DB_HOSTS") -> str`.

Return normalized safe endpoints; never return or interpolate a DSN into an exception.

Implement `http_client.py` around `AsyncClient.send(request, stream=True, follow_redirects=False)`. Reject every `3xx` without resolving or following `Location`; reject an oversized numeric `Content-Length` before iterating; otherwise consume `aiter_bytes()` and check `len(body) + len(chunk)` before extending the `bytearray`. Close the response in all paths, parse JSON only after the bounded byte body is complete, reject malformed/non-JSON provider responses with a stable safe error, and redact every exception before it crosses the connector boundary. Provider-specific clients in Tasks 3 and 5 must build an `httpx.Request` and use this helper rather than calling `response.json()`, `response.aread()`, or maintaining another cap.

Retrofit the existing GitHub REST/GitHub App token and Linear GraphQL clients in this task as well. Validate the exact official or operator-allowlisted origin at every credentialed outbound boundary, including execution from legacy stored connection rows, before building or sending a request. Apply the same validation before deriving the Git clone URL or resolving a PAT for `cli.repository.checkout`, so an old `base_url` cannot send a token into an attacker-controlled sandbox clone. Route every provider response through `send_bounded_json`; preserve GitHub App token creation's exact `201` requirement through the helper's optional expected-status argument. Convert transport, request-build, response-close, and provider-shape failures into stable provider-specific errors `from None`, never provider response text, credentials, arbitrary URLs, or raw exception strings. Fake-provider tests must opt their ephemeral origin in explicitly rather than weakening production policy.

Set the GitHub and Linear `base_url` config fields to typed text defaults for their official origins. Override each connector's `validate_settings` hook so creation validates and stores only the normalized official or exact operator-allowlisted origin; reject URL credentials, paths, queries, fragments, and unapproved origins before any secret or connection row is created. Keep the outbound revalidation above for legacy rows. Document both operator-only allowlists in `.env.example` as empty-by-default comma-separated exact entries with warnings; do not wire either variable into production compose.

- [ ] **Step 5: Implement provider-supplied webhook setup and body limits**

Make connection creation mode-specific:

```python
if mode == "generated":
    create encrypted random secret and return it once
elif mode == "provider_supplied":
    create no secret and return setup metadata with secret=None
else:
    return no webhook setup
```

Add `WebhookSecretWrite(secret: str = Field(min_length=16, max_length=4096))` and admin/CSRF-protected `PUT /api/v1/workspaces/{workspace_id}/connections/{connection_id}/webhook-secret`. It accepts only `provider_supplied` connectors, creates or rotates the encrypted `WEBHOOK_SECRET`, audits `connection.webhook_secret_configured` or `.rotated`, and returns `204`. `ConnectionOut` exposes only `webhook_secret_configured: bool`. `WebhookSetupOut` exposes `url_path`, nullable `secret`, `secret_mode`, `signature_algorithm`, and `help`; it never exposes a stored provider secret.

Treat connection creation, credential rotation, and provider webhook-secret writes as one credential boundary. Read each request incrementally under a 65,536-byte pre-copy cap, strict-decode JSON (including duplicate-key/non-finite rejection), and manually validate the extra-forbidden Pydantic model so FastAPI cannot echo plaintext through its default validation response. Return only stable credential-free `413`/`422` details, while preserving the three request schemas explicitly in OpenAPI. Credential-field validation errors must never interpolate submitted map keys or values. When serializing `ConnectionOut`, filter `config_json` to manifest-declared fields for the stored auth type and re-run generic plus connector-specific validation; invalid or unknown legacy values are omitted with no raw fallback, while runtime clients still revalidate stored endpoints before use.

Read the webhook stream incrementally:

```python
MAX_WEBHOOK_BODY_BYTES = 1_048_576

async def read_bounded_body(request: Request) -> bytes:
    content_length = parse_optional_nonnegative_content_length(request.headers)
    if content_length is not None and content_length > MAX_WEBHOOK_BODY_BYTES:
        raise HTTPException(status_code=413, detail="Webhook body is too large")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_WEBHOOK_BODY_BYTES:
            raise HTTPException(status_code=413, detail="Webhook body is too large")
        body.extend(chunk)
    return bytes(body)
```

Check the same cap at the start of `process_delivery`. Change `ingress_subject` to split `event` on `.`, reject empty segments, validate every segment, and join them; do not relax token validation for workspace or connector names.

Replace random webhook ingress IDs with `ingress_event_id(connector_type, connection_id, delivery_id) -> UUID`, using the fixed UUIDv5 namespace tested in Step 2. Use the result for `WebhookDelivery.event_id`, `EventEnvelope.event_id`, and `Nats-Msg-Id`. If publish succeeds but commit raises, roll back and return `503`; a provider retry regenerates the same ID, allowing JetStream and downstream derived IDs to deduplicate the publish even though the first database insert rolled back.

- [ ] **Step 6: Run focused regressions and commit**

Run:

```bash
uv run pytest packages/connectors/tests/test_manifest_registry.py packages/connectors/tests/test_endpoints.py packages/connectors/tests/test_http_client.py packages/connectors/tests/test_client_endpoint_security.py packages/connectors/tests/github/test_tools_against_fake.py packages/connectors/tests/linear/test_linear_tools_against_fake.py packages/connectors/tests/cli/test_executors.py packages/events/tests/test_subjects.py apps/api/tests/test_connections_unit.py apps/api/tests/test_webhooks_unit.py tests/test_compose_connector_allowlist.py -q
uv run ruff check packages/connectors packages/events apps/api tests/test_compose_connector_allowlist.py
uv run ruff format --check packages/connectors packages/events apps/api tests/test_compose_connector_allowlist.py
uv run mypy
docker compose -f compose.yaml -f compose.dev.yaml config --format json >/dev/null
docker compose -f compose.yaml config --format json >/dev/null
git diff --check
```

Expected: PASS with GitHub/Linear behavior preserved, every provider response bounded while streaming, redirects disabled, and crash/retry ingress IDs stable.

Commit:

```bash
git add packages/connectors packages/events/src/jhin_events/subjects.py packages/events/tests/test_subjects.py apps/api/src/jhin_api/connections apps/api/src/jhin_api/webhooks apps/api/tests/test_connections_unit.py apps/api/tests/test_webhooks_unit.py .env.example compose.dev.yaml tests/test_compose_connector_allowlist.py docs/superpowers/plans/2026-08-17-phase-9-vercel-supabase.md
git commit -m "feat: add secure connector setup contracts"
```

### Task 3: Implement the scoped Vercel client and tools

**Files:**
- Create: `packages/connectors/src/jhin_connectors/vercel/__init__.py`
- Create: `packages/connectors/src/jhin_connectors/vercel/manifest.py`
- Create: `packages/connectors/src/jhin_connectors/vercel/schemas.py`
- Create: `packages/connectors/src/jhin_connectors/vercel/client.py`
- Create: `packages/connectors/src/jhin_connectors/vercel/tools.py`
- Create: `packages/connectors/src/jhin_connectors/vercel/connector.py`
- Create: `packages/connectors/src/jhin_connectors/testing/fake_vercel.py`
- Modify: `packages/connectors/src/jhin_connectors/registry.py`
- Modify: `packages/connectors/src/jhin_connectors/testing/__init__.py`
- Test: `packages/connectors/tests/vercel/test_manifest.py`
- Test: `packages/connectors/tests/vercel/test_tools_against_fake.py`
- Test: `packages/connectors/tests/test_manifest_registry.py`

**Interfaces:**
- Consumes: `Connector`, typed manifest settings, `resolve_connection`, endpoint validation, `send_bounded_json`, `ToolDefinition.required_grant_scope_keys`, the stable internal tool-call ID, and the existing tool catalog.
- Produces: a registered `vercel` connector; bounded project/deployment/log/environment inspection; preview-only Git deployment creation; fixed-risk approval-capable preview/redeploy/promote/alias tools; a deterministic fake with side-effect counters. Task 4 separately enables webhook support only after its parser is present.

- [ ] **Step 1: Write failing manifest and registry tests**

Assert the manifest declares access-token auth (`token` secret), optional `team_id`, optional `base_url` defaulting to `https://api.vercel.com`, and exactly these tools/capabilities:

```text
vercel.project.list
vercel.project.read
vercel.deployment.list
vercel.deployment.read
vercel.deployment.logs.read
vercel.environment_metadata.read
vercel.deployment.preview.create
vercel.deployment.redeploy
vercel.deployment.promote
vercel.deployment.alias.assign
```

For every Vercel tool assert `connection_id` is both a request scope and a required grant scope. Project-specific tools also require `project_id`; deployment-specific tools also require `deployment_id`. Every mutation requires `environment`; preview creation additionally requires `repository_id`, while alias assignment additionally requires `alias`. `ref` remains an optional glob scope on preview creation. `preview.create` is `ELEVATED`; `redeploy`, `promote`, and `alias.assign` are `DESTRUCTIVE`; all four set `supports_approval=True`. Balanced parks all four, Autonomous may auto-run only the elevated preview action and still parks the three destructive actions, and only an explicit custom `AUTO` rule can auto-run a destructive action. `preview.create` has input `environment: Literal["preview"] = "preview"`, so its required scope is present in validated input and it cannot create a production deployment. Keep webhook mode `none` and events empty in this task so no connection advertises an ingress protocol before Task 4 implements it.

- [ ] **Step 2: Run manifest tests to verify RED**

Run:

```bash
uv run pytest packages/connectors/tests/vercel/test_manifest.py packages/connectors/tests/test_manifest_registry.py -q
```

Expected: FAIL because `VercelConnector` is absent from the default registry.

- [ ] **Step 3: Define strict Vercel DTOs and the provider client**

Use `extra="forbid"` input models and bounded strings/lists. The mutation contracts are:

```python
class PreviewCreateInput(ScopedProjectInput):
    environment: Literal["preview"] = "preview"
    git_provider: Literal["github", "gitlab", "bitbucket"]
    repository_id: str = Field(min_length=1, max_length=200)
    ref: str = Field(min_length=1, max_length=250)

class RedeployInput(ScopedDeploymentInput):
    environment: Literal["preview", "production"]

class PromoteInput(ScopedDeploymentInput):
    environment: Literal["production"] = "production"

class AliasAssignInput(ScopedDeploymentInput):
    environment: Literal["production"] = "production"
    alias: str = Field(pattern=r"^[a-z0-9.-]+$", max_length=253)
```

The client constructs an `httpx.AsyncClient` with a 5-second connect timeout and 20-second total timeout, bearer token, and optional `teamId` query, but every response flows through Task 2's shared redirect-free streaming 512 KiB helper. Implement these official calls:

```text
GET  /v2/user
GET  /v9/projects
GET  /v9/projects/{project_id}
GET  /v6/deployments
GET  /v13/deployments/{deployment_id}
GET  /v3/deployments/{deployment_id}/events   # bounded deployment build-log events
GET  /v9/projects/{project_id}/env
POST /v13/deployments
POST /v13/deployments?forceNew=1
POST /v10/projects/{project_id}/promote/{deployment_id}
POST /v2/deployments/{deployment_id}/aliases
```

For `GET /v6/deployments`, always send the exact input `projectId`; never use an account-wide request followed by local filtering. Request pages of at most 100, follow `pagination.next`/`until` for at most five pages with repeated-cursor rejection, and cap the returned list at 200. Before emitting anything from a page, validate every returned row's `projectId` (or documented nested project ID) against the requested project; one malformed or mismatched row fails the entire call.

Redeploy sends `{deploymentId, name, target, meta: {action: "redeploy"}}`. Preview creation sends provider `target="preview"`, the fetched project name/id, and the provider-specific bounded Git source. Promote sends an empty JSON body. The currently selected Vercel endpoints do not document an idempotency header or request field, so retain `tool_call_id` only as Jhin's stable internal invocation ID and do not invent an on-wire header/query/body field; if the official endpoint contract adds one before implementation, add only that documented field and a contract test. Filter every output into typed display-safe fields.

- [ ] **Step 4: Build the fake and write failing tool behavior tests**

The fake serves seeded projects with provider-specific Git links, preview/production deployments, deployment events, and environment records containing a deliberate `value="must-never-leak"`. Its `/_state` response exposes counters and last sanitized request for preview create, redeploy, promote, and alias; `/_reset` clears mutations. A test-only `POST /_fault` arms exactly one named mutation to record its side effect and then terminate/raise during response streaming, simulating a post-effect transport ambiguity; the arm clears after one request. Add named tests `test_environment_metadata_never_returns_provider_value`, `test_logs_are_time_and_count_bounded`, `test_preview_create_cannot_send_target_production`, `test_preview_create_rejects_unlinked_or_mismatched_git_repository_before_side_effect`, `test_redeploy_payload_matches_current_vercel_contract`, `test_deployment_list_always_sends_project_id_and_has_bounded_pagination`, `test_deployment_list_rejects_any_mixed_project_page_without_returning_rows`, `test_deployment_project_mismatch_has_zero_side_effects`, `test_promote_and_alias_verify_project_ownership_first`, `test_mutation_risks_and_approval_support_are_fixed`, `test_team_id_is_sent_without_entering_outputs`, `test_no_undocumented_idempotency_field_is_sent`, `test_post_effect_fault_is_one_shot_and_visible_in_state`, and `test_provider_redirect_to_unapproved_origin_is_rejected`. Each mutation denial asserts both the gateway/provider exception code and the unchanged matching counter from `/_state`.

Seed enough build-log events to prove the deployment logs executor stops at `limit <= 200`, a time window no wider than 24 hours, and an output byte cap. UI/API copy must call these deployment build logs, not project activity logs.

- [ ] **Step 5: Run provider tests to verify RED**

Run:

```bash
uv run pytest packages/connectors/tests/vercel -q
```

Expected: FAIL because the executors and fake routes are not implemented.

- [ ] **Step 6: Implement Vercel verification and tools**

`verify_connection` accepts only `auth_type="access_token"`, validates the base origin, calls `/v2/user`, and returns display-safe username/email/team facts. Each executor resolves a `vercel` connection, asserts the auth type, validates the origin, and uses only its configured token/team through `send_bounded_json`.

Before deployment read/log/mutation output, fetch `/v13/deployments/{deployment_id}` and require its `projectId`/`project.id` to equal the input `project_id`. A redeploy also requires the fetched deployment target to equal the scoped `environment`; it cannot label a production deployment as preview. Before every project action, fetch `/v9/projects/{project_id}` and require its returned ID to equal the input. Deployment listing uses the bounded, project-filtered, every-row validation contract from Step 3.

Before preview creation, normalize the fetched project's documented Git `link` shape into `(provider, repository_identifier)`. Reject an absent/unknown link, a provider mismatch, or a repository identifier mismatch before the POST; never trust the model-supplied `git_provider`/`repository_id` merely because they match its grant. Serialize the provider-specific `gitSource` only after this equality check and map validated `environment="preview"` to provider `target="preview"`. Environment metadata outputs only key/name, target, type, created/updated timestamps, and git-branch scope; explicitly discard `value`, `encryptedValue`, `internalContent`, and unknown keys.

Register all definitions through `VercelConnector.tools()` and add the connector factory to `DEFAULT_CONNECTORS`.

- [ ] **Step 7: Run focused tests and commit**

Run:

```bash
uv run pytest packages/connectors/tests/vercel packages/connectors/tests/test_manifest_registry.py packages/policy/tests/test_evaluator.py -q
uv run ruff check packages/connectors
uv run mypy
git diff --check
```

Expected: PASS, including fixed mutation risks, bounded project-filtered pagination, every-row ownership, fetched Git-link binding, output allowlists, preview-only behavior, and zero-side-effect denials.

Commit:

```bash
git add packages/connectors/src/jhin_connectors/vercel packages/connectors/src/jhin_connectors/testing packages/connectors/src/jhin_connectors/registry.py packages/connectors/tests/vercel packages/connectors/tests/test_manifest_registry.py
git commit -m "feat: add scoped Vercel deployment tools"
```

### Task 4: Implement Vercel deployment webhook ingestion and normalization

**Files:**
- Modify: `docs/superpowers/plans/2026-08-17-phase-9-vercel-supabase.md`
- Create: `packages/connectors/src/jhin_connectors/vercel/webhook.py`
- Modify: `packages/connectors/src/jhin_connectors/vercel/manifest.py`
- Modify: `packages/connectors/src/jhin_connectors/vercel/connector.py`
- Modify: `packages/connectors/src/jhin_connectors/testing/fake_vercel.py`
- Test: `packages/connectors/tests/vercel/test_webhook.py`
- Test: `packages/connectors/tests/vercel/test_manifest.py`
- Test: `packages/connectors/tests/test_manifest_registry.py`
- Test: `apps/api/tests/test_webhooks_unit.py`
- Test: `services/event_worker/tests/test_normalizer.py`
- Test: `services/event_worker/tests/test_matcher.py`

**Interfaces:**
- Consumes: the Vercel connector from Task 3, provider-supplied webhook secret storage, bounded raw-body ingress, Task 2's deterministic ingress UUID, dotted subjects, `RawWebhookEvent`, `NormalizedEvent`, and the event worker's generic deterministic normalizer.
- Produces: constant-time Vercel signature verification and fixed-field, retry-stable canonical deployment events for `deployment.created`, `deployment.ready`, `deployment.error`, `deployment.canceled`, and `deployment.promoted`. The current account-webhook success event `deployment.succeeded` is also accepted and maps to the existing canonical `deployment.ready` concept because Vercel still emits `deployment.ready` for integrations/checks; this avoids splitting one successful-deployment automation concept into two Jhin events.

- [ ] **Step 1: Write failing signature and normalization tests**

First assert the completed manifest changes from `none` to `provider_supplied`, declares `sha1`, advertises six provider event names but five canonical event names, and returns setup metadata with no Jhin-generated secret. The provider list contains both `deployment.ready` and current account-webhook `deployment.succeeded`; both normalize to `connector.vercel.deployment.ready`. Then use raw JSON bytes, a known provider secret, and `hmac.new(secret, body, hashlib.sha1).hexdigest()`. Assert missing/malformed/wrong `x-vercel-signature` returns `WebhookVerificationError`; correct signature yields root-level `id` as `delivery_id` and root-level `type` as `event`. Reject missing/oversized IDs before payload normalization.

For every supported provider event assert one canonical event, with an explicit compatibility test proving `deployment.ready` and `deployment.succeeded` produce the same canonical event type and neither copies provider-only fields:

```python
assert normalized.event_type == "connector.vercel.deployment.ready"
assert normalized.data == {
    "deployment_id": "dpl_123",
    "project_id": "prj_123",
    "project_name": "storefront",
    "url": "storefront-abc.vercel.app",
    "target": "preview",
    "state": "READY",
    "created_at": 1700000000000,
    "git_ref": "agent/fix",
    "git_sha": "abc123",
}
```

Provider-only fields and environment values must not survive. Unknown, malformed, or unsupported signed events normalize to `[]`. Add an event-worker test proving `ingress.vercel.deployment.ready` is consumed and published under the canonical connector subject.

Add `test_vercel_post_publish_precommit_retry_keeps_one_canonical_event`. Pass the same correctly signed Vercel body twice through the API ingress service, inject a failure after the first JetStream publish and before its database commit, then retry in a fresh database session. Assert both ingress attempts derive the same UUID from `(vercel, connection_id, delivery_id)`, the normalizer derives the same canonical UUID from that ingress UUID, JetStream observes one canonical event, the trigger matcher starts at most one task, and the database eventually contains one `WebhookDelivery`. This test must fail if ingress returns to random UUIDv7 generation.

- [ ] **Step 2: Run webhook tests to verify RED**

Run:

```bash
uv run pytest packages/connectors/tests/vercel/test_webhook.py packages/connectors/tests/vercel/test_manifest.py packages/connectors/tests/test_manifest_registry.py apps/api/tests/test_webhooks_unit.py services/event_worker/tests/test_normalizer.py services/event_worker/tests/test_matcher.py -q
```

Expected: FAIL because Vercel has no parser/normalizer.

- [ ] **Step 3: Implement HMAC-SHA1 parsing and fixed-field normalization**

Verify the exact raw bytes before `json.loads`, compare lowercase hex digests with `hmac.compare_digest`, require an object root, and accept only bounded string `id`/`type`. Do not derive a delivery identifier from timestamps or mutable deployment fields.

Normalize through small extraction helpers that tolerate provider shape changes while emitting only the allowed fields shown in Step 1. Do not copy `data` wholesale. Wire `parse_webhook` and `normalize_event` through `VercelConnector` and set the six provider `WEBHOOK_EVENTS` plus five canonical events in the manifest. Preserve Task 2's deterministic ingress ID unchanged; the existing event worker continues deriving canonical UUIDv5 IDs from that ingress ID, so publish/commit crash recovery remains idempotent across both streams. Do not attempt cross-delivery semantic deduplication: provider delivery identity remains the safe idempotency boundary.

Extend the fake with an admin webhook emitter that signs the exact bytes using a supplied test secret, posts to a caller-provided Jhin callback URL only in dev tests, and reports the provider response without logging the secret.

- [ ] **Step 4: Run focused tests and commit**

Run:

```bash
uv run pytest packages/connectors/tests/vercel/test_webhook.py packages/connectors/tests/vercel/test_manifest.py packages/connectors/tests/test_manifest_registry.py apps/api/tests/test_webhooks_unit.py services/event_worker/tests/test_normalizer.py services/event_worker/tests/test_matcher.py packages/events/tests/test_subjects.py -q
uv run ruff check packages/connectors apps/api services/event_worker packages/events
uv run mypy
git diff --check
```

Expected: PASS for valid, invalid, duplicate, oversized, dotted, unknown, normalized, and publish-before-commit crash/retry webhook cases, with exactly one canonical event and trigger.

Commit:

```bash
git add docs/superpowers/plans/2026-08-17-phase-9-vercel-supabase.md packages/connectors/src/jhin_connectors/vercel packages/connectors/src/jhin_connectors/testing/fake_vercel.py packages/connectors/tests/vercel/test_webhook.py packages/connectors/tests/vercel/test_manifest.py packages/connectors/tests/test_manifest_registry.py apps/api/tests/test_webhooks_unit.py services/event_worker/tests/test_normalizer.py services/event_worker/tests/test_matcher.py
git commit -m "feat: ingest signed Vercel deployment events"
```

### Task 5: Implement the Supabase Management API plane

**Files:**
- Create: `packages/connectors/src/jhin_connectors/supabase/__init__.py`
- Create: `packages/connectors/src/jhin_connectors/supabase/manifest.py`
- Create: `packages/connectors/src/jhin_connectors/supabase/schemas.py`
- Create: `packages/connectors/src/jhin_connectors/supabase/management_client.py`
- Create: `packages/connectors/src/jhin_connectors/supabase/management_tools.py`
- Create: `packages/connectors/src/jhin_connectors/supabase/database_client.py`
- Create: `packages/connectors/src/jhin_connectors/supabase/connector.py`
- Create: `packages/connectors/src/jhin_connectors/testing/fake_supabase.py`
- Modify: `packages/connectors/src/jhin_connectors/endpoints.py`
- Modify: `packages/connectors/pyproject.toml`
- Modify: `uv.lock`
- Modify: `packages/connectors/src/jhin_connectors/registry.py`
- Modify: `packages/connectors/src/jhin_connectors/testing/__init__.py`
- Modify: `docs/superpowers/plans/2026-08-17-phase-9-vercel-supabase.md`
- Test: `packages/connectors/tests/supabase/test_manifest.py`
- Test: `packages/connectors/tests/supabase/test_management_tools.py`
- Test: `packages/connectors/tests/supabase/test_database_verify.py`
- Test: `packages/connectors/tests/test_endpoints.py`
- Test: `packages/connectors/tests/test_manifest_registry.py`

**Interfaces:**
- Consumes: typed auth-specific connection settings, endpoint validation, `send_bounded_json`, `resolve_connection`, required grant scopes, the stable internal tool-call ID, and approval-safe bounded input.
- Produces: one registered `supabase` connector with independent `management_token` and `postgres` connection modes; project/log/function inspection; approval-gated function deployment/deletion; live low-privilege database verification; a deterministic fake Management API.

- [ ] **Step 1: Write failing manifest/plane-separation tests**

Declare two auth schemes on one manifest:

```text
management_token -> secret access_token
postgres         -> secret database_url
```

The `management_token` settings are `project_ref` (required text) and `base_url` (text, default `https://api.supabase.com`). The `postgres` settings are `project_ref`, `allowed_schemas` (string list, default `public`), `allow_writes` (boolean, default false), `statement_timeout_ms` (integer, default 5000, 250..30000), `lock_timeout_ms` (integer, default 1000, 100..5000), `max_rows` (integer, default 200, 1..1000), `max_cell_bytes` (integer, default 4096, 256..8000), and `max_result_bytes` (integer, default 24000, 4096..30000). The effective cell budget cannot exceed the effective result budget. The result ceiling stays below `ToolGateway`'s fixed 32,768-byte document cap, and the cell ceiling stays below its 8,192-character string cap. Phase 9 deliberately exposes no agent DDL setting or capability.

Tests must prove every Management API executor rejects a `postgres` connection before network I/O, every future database executor rejects `management_token`, fields for the other auth type (and removed `allow_ddl`) are rejected rather than silently stored, and empty/duplicate/system schemas fail validation.

Add `asyncpg>=0.31,<1` as a direct connector dependency and refresh `uv.lock`. Add a database-verification protocol test proving the `postgres` auth path validates the target, requires an explicit nonempty DSN password, permits at most one case-insensitive `sslmode` query key and rejects every other key before connect, normalizes an accepted `postgresql+asyncpg://` scheme case-insensitively to the driver-supported `postgresql://` prefix without changing the remaining credential bytes, connects with `timeout=5` and `statement_cache_size=0`, checks equal `session_user`/`current_user` plus direct superuser/`BYPASSRLS`/`CREATEDB`/`CREATEROLE`/replication flags, and rejects a login that owns `current_database()` or any configured `allowed_schemas`. Every unsafe-role/ownership/target failure is credential-safe. Bound the whole connect-and-query verification and the final close with client-side timeouts, preserve external cancellation, and always close in `finally`. Official Supabase direct and session-mode pooler connections use port 5432; reject the official transaction-pooler port 6543 because Task 6 requires prepared statements and server cursors with session semantics. Evaluate official Supabase hostname/ref/port/TLS rules before the operator allowlist so an allowlist entry cannot downgrade an official host; keep arbitrary ports available only for exact non-official operator-allowlisted dev hosts. This makes the connector complete and healthy before Task 6 adds any SQL execution tools.

Run after editing the dependency:

```bash
uv lock
uv sync --frozen --all-packages
```

Expected: asyncpg is recorded as a direct `jhin-connectors` dependency and the frozen environment syncs.

- [ ] **Step 2: Write failing Management API/fake tests**

The fake implements:

```text
GET    /v1/projects/{ref}
GET    /v1/projects/{ref}/analytics/endpoints/logs
GET    /v1/projects/{ref}/functions
POST   /v1/projects/{ref}/functions/deploy
DELETE /v1/projects/{ref}/functions/{function_slug}
GET    /_state
POST   /_reset
POST   /_fault   # test-only one-shot post-side-effect transport failure
```

Add tests for project-ref binding, auth failure, bounded project/function output, log time range at most 24 hours, log `limit <= 200`, a fixed source enum, a fixed projected-field query, no caller-provided log SQL, no `logs.all`, fixed `DESTRUCTIVE` risk plus approval support for both function mutations, shared streaming-response cap/redirect behavior, an outer total wall-clock timeout with cancellation preserved, no undocumented idempotency header/field, deterministic one-shot `/_fault` behavior after a deploy/delete side effect, and zero side effects on validation failure. Projected log and function-list documents use deterministic row/byte cutoffs below the gateway's 32,768-byte whole-document cap, calculate with the gateway's spaced UTF-8 JSON serialization rather than Pydantic's compact JSON, account for expansion by the active secret redactor through `sanitize_payload`, survive sanitization without whole-document replacement, and set `truncated=True` whenever a row, requested limit, or byte budget cuts the provider result. Every projected provider string must be strict UTF-8 and reject Unicode `C*` categories; a log event message may preserve ordinary newline/tab only, and input log filters must be strict UTF-8 before network I/O. Prove unsafe project/function/log strings fail with stable provider errors, including an unsafe deploy response after its side effect.

Function deployment input is an in-memory list of at most eight `{path, content}` files. Paths are POSIX-relative, reject absolute paths, `.`/`..`, backslashes, duplicates, and every Unicode control/format/surrogate/unassigned/private-use category (`C*`), including C1 controls and bidirectional format characters; ordinary Unicode letters remain valid. Each content string is at most 6 KiB and total serialized source is at most 24 KiB, keeping approval persistence lossless under gateway limits. Require a bounded slug, `entrypoint_path` that names one supplied file, and explicit `verify_jwt`. Deletion accepts only the slug.

- [ ] **Step 3: Run Supabase Management tests to verify RED**

Run:

```bash
uv run pytest packages/connectors/tests/supabase/test_manifest.py packages/connectors/tests/supabase/test_management_tools.py packages/connectors/tests/supabase/test_database_verify.py packages/connectors/tests/test_endpoints.py packages/connectors/tests/test_manifest_registry.py -q
```

Expected: FAIL because the Supabase manifest, client, tools, and fake do not exist.

- [ ] **Step 4: Implement the Management API client and tools**

Implement exact tools:

```text
supabase.project.read       READ
supabase.logs.read          READ
supabase.function.list      READ
supabase.function.deploy    DESTRUCTIVE + approval
supabase.function.delete    DESTRUCTIVE + approval
```

All tools require grant scopes `connection_id` and `project_ref`; function mutations additionally require `function_slug`. The configured `project_ref`, not model output, is authoritative: require input scope to equal config before the request.

The client uses bearer auth and the validated official/allowlisted origin, constructs requests with a 5-second connect timeout, wraps request construction, send, bounded streaming read, and client close in a 20-second total wall-clock timeout, and routes every response through Task 2's redirect-free streaming 512 KiB helper. Preserve caller cancellation rather than converting it to a provider error. Build the unified log ClickHouse query internally from a closed source enum, validated ISO start/end, an optional 500-character text filter escaped by a dedicated ClickHouse string-literal function, and `limit`; select only timestamp, source, event message, path, status code, and method. Snapshot the exact generated query in unit tests, including quote, backslash, NUL, newline, carriage return, and tab escaping. Never accept arbitrary log SQL. Treat a 200 response with a nonempty provider `error` as a stable credential-free failure rather than returning `result`.

Deploy with official multipart `POST /v1/projects/{ref}/functions/deploy?slug={function_slug}`, JSON metadata, and in-memory files. Delete with the official slug path. Return only id, slug, name, status, version, timestamps, `verify_jwt`, and entrypoint path. These selected Management API endpoints do not currently document an idempotency header or request field, so retain the tool-call ID as Jhin's internal invocation ID and do not invent an on-wire key; a post-side-effect transport ambiguity becomes `execution_unknown` under Task 1 and is not automatically retried.

`SupabaseConnector.verify_connection` switches strictly on auth type: `management_token` calls project metadata; `postgres` calls `verify_database_connection` in `database_client.py`. The verifier applies `validate_postgres_target(..., app_database_url=os.getenv("DATABASE_URL"))`, normalizes only an accepted SQLAlchemy asyncpg scheme prefix for driver compatibility, connects without a statement cache, reads equal `session_user` and `current_user` plus the current row's `rolsuper`, `rolbypassrls`, `rolcreatedb`, `rolcreaterole`, and `rolreplication` flags from `pg_catalog.pg_roles`, and checks ownership of `current_database()` plus every configured allowed schema from `pg_catalog`. Reject `postgres`, a session/current-user mismatch, any privileged direct flag, current-database ownership, or allowed-schema ownership with one credential-safe least-privilege error. Bound the entire verification query and close lifecycle. Add the factory to `DEFAULT_CONNECTORS` now; its database tool tuple stays empty until Task 6.

- [ ] **Step 5: Run focused tests and commit**

Run:

```bash
uv run pytest packages/connectors/tests/supabase/test_manifest.py packages/connectors/tests/supabase/test_management_tools.py packages/connectors/tests/supabase/test_database_verify.py packages/connectors/tests/test_endpoints.py packages/connectors/tests/test_manifest_registry.py packages/policy/tests/test_evaluator.py -q
uv run ruff check packages/connectors
uv run ruff format --check packages/connectors
uv run mypy
git diff --check
```

Expected: PASS, including plane separation, endpoint/ref binding, current logs endpoint, source-path safety, destructive function-mutation risks, scoped approvals, shared HTTP bounds, and output allowlists.

Commit:

```bash
git add docs/superpowers/plans/2026-08-17-phase-9-vercel-supabase.md packages/connectors/pyproject.toml packages/connectors/src/jhin_connectors/endpoints.py packages/connectors/src/jhin_connectors/supabase packages/connectors/src/jhin_connectors/testing/fake_supabase.py packages/connectors/src/jhin_connectors/testing/__init__.py packages/connectors/src/jhin_connectors/registry.py packages/connectors/tests/supabase packages/connectors/tests/test_endpoints.py packages/connectors/tests/test_manifest_registry.py uv.lock
git commit -m "feat: add Supabase management plane tools"
```

### Task 6: Implement fail-closed Supabase SQL policy and bounded database execution

**Files:**
- Modify: `docs/superpowers/plans/2026-08-17-phase-9-vercel-supabase.md`
- Create: `packages/connectors/src/jhin_connectors/supabase/sql_policy.py`
- Modify: `packages/connectors/src/jhin_connectors/supabase/database_client.py`
- Create: `packages/connectors/src/jhin_connectors/supabase/database_preflight.py`
- Create: `packages/connectors/src/jhin_connectors/supabase/database_tools.py`
- Modify: `packages/connectors/src/jhin_connectors/supabase/schemas.py`
- Modify: `packages/connectors/src/jhin_connectors/supabase/manifest.py`
- Modify: `packages/connectors/src/jhin_connectors/supabase/connector.py`
- Modify: `packages/connectors/pyproject.toml`
- Modify: `uv.lock`
- Create: `tests/fixtures/supabase/init.sql`
- Modify: `compose.dev.yaml`
- Modify: `.env.example`
- Test: `packages/connectors/tests/supabase/test_manifest.py`
- Test: `packages/connectors/tests/supabase/test_database_verify.py`
- Test: `packages/connectors/tests/supabase/test_database_gateway.py`
- Test: `packages/connectors/tests/supabase/test_database_preflight.py`
- Test: `packages/connectors/tests/supabase/test_sql_policy.py`
- Test: `packages/connectors/tests/supabase/test_database_tools.py`
- Test: `packages/connectors/tests/supabase/test_database_integration.py`
- Test: `packages/connectors/tests/test_manifest_registry.py`
- Test: `tests/test_compose_supabase_db_fixture.py`

**Interfaces:**
- Consumes: Task 5's strict `validate_postgres_target` and `verify_database_connection(database_url, *, project_ref, allowed_schemas, app_database_url)` boundary; the latest resolved encrypted `database_url` and normalized public config on every invocation; fixed tool risks and grant scopes; SQLGlot 30.x PostgreSQL ASTs; asyncpg 0.31 session semantics; Task 1's mutation-claim lifecycle.
- Produces: `classify_and_validate_sql(sql, *, expected, requested_schema) -> ValidatedSql`; immutable physical-relation and placeholder metadata; same-connection direct/inherited role and ownership verification; locked catalog preflight; bounded parameterized execution; three database tools (`read`, `write`, `destructive`); an isolated real PostgreSQL fixture and integration gate.

Phase 9 exposes no agent DDL tool or setting. Schema changes remain reviewed operator migrations until a later dedicated migration workflow can give them a separate sandbox, plan/diff artifact, and approval contract. Remove Task 5's temporary `allow_ddl` manifest/config field if it remains when this task starts, and reject legacy/new submitted `allow_ddl` rather than silently storing it.

- [ ] **Step 1: Add SQLGlot and write failing manifest/schema/tool-contract tests**

Keep Task 5's direct asyncpg dependency and add:

```toml
"sqlglot>=30.13,<31",
```

`uv.lock` is the exact deployed parser resolution; do not add `pglast` or another GPL-licensed parser. Refresh and sync all workspace packages:

```bash
uv lock
uv sync --frozen --all-packages
```

In `test_manifest.py` and `test_manifest_registry.py`, first assert the final exact Supabase capability set is Task 5's five Management API tools plus:

```text
supabase.database.read          READ, no approval support
supabase.database.write         ELEVATED, approval support
supabase.database.destructive   DESTRUCTIVE, approval support
```

All three database tools require grant scopes `connection_id`, `project_ref`, and `schema`. Assert there is no `supabase.database.ddl` capability/tool and no `allow_ddl` config field. Assert the Postgres config bounds are exactly `max_cell_bytes=4096` with range `256..8000` and `max_result_bytes=24000` with range `4096..30000`, below the gateway's 8,192-character leaf and 32,768-byte document limits.

Add strict database request/output models. Every input has `connection_id`, `project_ref`, `schema`, `sql`, and `params`. Cap SQL at 7,000 strict UTF-8 bytes, reject surrogates/control characters other than ordinary SQL whitespace, and accept at most 50 positional JSON scalars. Parameters are only `None`, strict booleans, signed 64-bit integers, finite floats, or strict UTF-8 strings; cap one encoded string at 8,192 bytes and compact-JSON encoding of the full parameter list at 16,000 bytes. No coercion, nested list/object, non-finite number, or unsupported Python value reaches asyncpg. Add boundary tests through the real `ToolGateway` proving the complete SQL/scopes/params input is preserved byte-for-byte by active sanitization for approval digest/replay; a redaction hit still fails lossless admission before a tool claim. Read output is positional—bounded column names plus `list[list[str | None]]`, `row_count`, and `truncated`—so duplicate SQL aliases cannot overwrite cells. Mutation output contains only `affected_rows`.

Run:

```bash
uv run pytest packages/connectors/tests/supabase/test_manifest.py packages/connectors/tests/test_manifest_registry.py -q
```

Expected: FAIL because the database tools/models/capabilities do not exist and the temporary Task 5 bounds/config still differ.

- [ ] **Step 2: Write the SQL policy decision table as failing parameterized tests**

Define `SqlClass = Literal["read", "write", "destructive"]`. Test this exact matrix:

```text
read allow:         SELECT 1; SELECT id FROM public.widgets;
                    WITH named_cte AS (SELECT id FROM public.widgets)
                    SELECT id FROM named_cte; qualified joins/subqueries/UNION branches
read deny:          SELECT INTO; FOR UPDATE/NO KEY UPDATE/SHARE/KEY SHARE;
                    data-changing CTE; EXPLAIN/ANALYZE; VALUES as the root;
                    COPY/CALL/DO/PREPARE/EXECUTE/DEALLOCATE; DECLARE/FETCH;
                    LOCK; LISTEN/NOTIFY; SET/RESET/DISCARD; transaction commands
write allow:        INSERT INTO public.widgets (...) VALUES (...)
write deny:         every UPDATE/DELETE/TRUNCATE/MERGE; INSERT DEFAULT VALUES;
                    INSERT ... SELECT; ON CONFLICT (including DO UPDATE);
                    OVERRIDING; RETURNING; DEFAULT in an individual value
destructive allow:  UPDATE with the fixed assignment grammar (with/without
                    WHERE/FROM), DELETE FROM public.widgets,
                    one `TRUNCATE public.widgets` using default/CONTINUE IDENTITY
                    and default/RESTRICT behavior
destructive deny:   INSERT; multi-table TRUNCATE; ONLY/descendant `*` TRUNCATE;
                    RESTART IDENTITY; CASCADE; RETURNING; MERGE; every DDL
all deny:           empty/comments-only SQL; any semicolon token (even a trailing
                    terminator); two statements; parser fallback/unknown command;
                    CREATE/ALTER/DROP/GRANT/REVOKE and every other DDL;
                    wrong concrete root for the selected tool; catalog/system/
                    cross-schema/unqualified physical relations; every function,
                    aggregate, window/table function, explicit COLLATE, custom
                    operator/type/cast, or unsupported AST node
```

Test quoted identifiers, mixed case, nested and recursive CTE scopes, CTE shadowing, aliases that collide with table names, `UPDATE ... FROM`, `DELETE ... USING`, rejected `INSERT ... SELECT`, rejected source-column UPDATE assignment, comments/dollar-quoted strings containing semicolons, and bypasses below the root. Every physical source and target table is explicitly qualified with exactly `requested_schema`; an unqualified table name is accepted only when it resolves lexically to a CTE in that query scope. Column references may be unqualified (`id`), qualified by an alias/table (`w.id`), or quoted in either form (`"MixedCase"`, `w."MixedCase"`); do not confuse column qualification with the mandatory schema qualification of physical tables. Add positive mixed-case/quoted table, alias, and column combinations plus stable rejection of an ambiguous unqualified join column. `pg_catalog`, `information_schema`, `pg_toast`, and every other schema are denied to submitted SQL even though trusted executor queries use `pg_catalog`.

Add fixed resource-abuse boundaries and their exact boundary/over-boundary cases:

```python
MAX_SQL_TOKENS = 1_024
MAX_SQL_AST_NODES = 512
MAX_SQL_AST_DEPTH = 64
```

Tokenize with the SQLGlot PostgreSQL dialect first so parentheses inside strings/comments do not count as nesting. Parse using `error_message_context=0`, `max_errors=1`, and `max_nodes=MAX_SQL_AST_NODES`; then perform an independent iterative whole-tree node/depth walk because SQLGlot's parser-side node counter does not cover every expression form. Catch tokenizer/parser errors and `RecursionError`, raise only `SqlPolicyError("unsupported SQL") from None`, and never log the exception, SQL, or parameters. A `caplog` regression places a secret marker in invalid syntax and `Command` fallback and proves it appears nowhere in logs/errors.

Only PostgreSQL `$1` through `$50` parameter nodes are accepted. Reject `?`, `:name`, `$0`, gaps, indexes above 50, and a placeholder set that is not exactly contiguous `1..len(params)` at execution. Placeholders inside comments/string/dollar-quoted literals do not count.

Every SQLGlot `exp.Func` is denied except the deliberately narrow cast case. Since SQLGlot's `Cast` subclasses `Func`, validate an `exp.Cast` before the blanket function rejection. Accept only a scalar literal (or nested already-valid literal cast) to a semantic, unqualified built-in scalar type in this fixed set: `boolean`, `smallint`, `integer`, `bigint`, `numeric`, `real`, `double precision`, `text`, `varchar`, `date`, `timestamp`, `timestamptz`, `uuid`, `json`, `jsonb`. SQLGlot normalizes aliases such as `int` and loses quote spelling, so enforce the semantic `DataType` plus absence of user-defined/schema/array structure rather than claiming to recover original spelling. Reject parameter/column casts, `TRY_CAST`, domains/custom/schema-qualified/array/composite types, and every other type. Deny explicit `OPERATOR(schema.op)` and `COLLATE`; add cases for `pg_read_file`, `dblink`, `setval`, `nextval`, `pg_sleep`, `current_user`, `session_user`, unquoted `user`/`current_role`/`system_user`, a qualified/unqualified custom function, a SECURITY DEFINER function, `OPERATOR(public.+)`, a domain/custom cast, and a column cast.

The expression allowlist contains only literals/parameters, qualified or unqualified column references, aliases, `CASE`, boolean `AND`/`OR`/`NOT`, `IS [NOT] NULL`, `IN`, `BETWEEN`, `LIKE`/`ILIKE`, comparisons (`=`, `<>`, `<`, `<=`, `>`, `>=`), unary sign, arithmetic (`+`, `-`, `*`, `/`, `%`), and the structural SELECT/CTE/join/subquery/set-operation/filter/group/order/distinct/limit/offset nodes required by the allowed matrix. Reject every generic/custom binary node and every PostgreSQL operator spelling not explicitly mapped to one of those concrete SQLGlot nodes. Live preflight limits all operands to the fixed built-in column types below, and `search_path=pg_catalog` limits unqualified symbolic resolution to built-in operators.

- [ ] **Step 3: Run SQL policy tests to verify RED**

Run:

```bash
uv run pytest packages/connectors/tests/supabase/test_sql_policy.py -q
```

Expected: FAIL because `sql_policy.py` does not exist.

- [ ] **Step 4: Implement fail-closed entire-AST validation**

Return immutable metadata without re-rendering SQL:

```python
@dataclass(frozen=True, order=True)
class RelationRef:
    schema: str
    name: str
    access: Literal["source", "target"]

@dataclass(frozen=True)
class MutationValueRef:
    parameter_index: int | None
    literal_bytes: int | None

@dataclass(frozen=True)
class ValidatedSql:
    sql_class: SqlClass
    statement_type: str
    relations: tuple[RelationRef, ...]
    mutation_target: RelationRef | None
    parameter_indexes: tuple[int, ...]
    mutation_values: tuple[MutationValueRef, ...]
    insert_row_count: int | None
```

Build a concrete root map and an explicit supported-node allowlist; a node not required by the Step 2 grammar is denied. Walk every child iteratively. Track CTE aliases per lexical query scope rather than globally, distinguish CTE references from physical tables, preserve quoted identifier values for catalog lookup, and deterministically de-duplicate/sort physical metadata. `parameter_indexes` is the sorted unique set used for contiguity; `mutation_values` preserves every assignment/VALUES occurrence so repeated placeholders cannot evade expansion accounting, with exactly one field populated per item. Never authorize by a first keyword, regex, SQLGlot pretty-print, or inferred parser warning. The original SQL bytes and original validated positional parameters are the only user values later submitted to PostgreSQL.

Classify every `UPDATE` as destructive regardless of whether a `WHERE` node exists—`WHERE TRUE`, `id IS NOT NULL`, and equivalent tautologies are not bounded writes. Treat `OnConflict(action=DO UPDATE)` as unsupported rather than hiding an update below an `Insert`. Require one exact target for every mutation and one exact table for TRUNCATE. INSERT accepts only an explicit column list and one `VALUES` matrix whose every cell is a bounded literal, parameter, or already-allowed literal cast; it never accepts a query source. Every UPDATE assignment is likewise only a bounded literal, parameter, or allowed literal cast—never a source/target column, operator expression, subquery, row expression, or `DEFAULT`. Predicates and qualified `UPDATE ... FROM`/`DELETE ... USING` sources still follow the read expression/relation rules.

Define `MAX_MUTATION_VALUE_BYTES = 1_048_576`. Before dispatch, resolve every literal/parameter value to its strict UTF-8/compact-JSON byte contribution. INSERT sums each occurrence across all VALUES rows (a repeated `$1` counts each time), requires the static VALUES row count to be at most `max_rows`, and rejects when the sum exceeds the fixed budget. After the locked target pre-probe below, UPDATE sums its assignment values once, multiplies by the exact bounded target row count, and rejects when that product exceeds the budget. DELETE/TRUNCATE contribute zero new value bytes. Add exact budget/cap+1 cases, repeated-parameter amplification, many-column assignment, and huge-source copy attempts; none may reach asyncpg when rejected.

Run `uv run pytest packages/connectors/tests/supabase/test_sql_policy.py -q` now. Expected: PASS, including fixed token/node/depth boundaries, parser-error secrecy, cast-before-function handling, the explicit operator grammar, CTE scoping, placeholder validation, and exact TRUNCATE forms, before executor work starts.

- [ ] **Step 5: Write failing same-connection role, preflight, timeout, and bounded-output protocol tests**

Use an asyncpg protocol fake to assert Task 5's strict connection call remains:

```python
asyncpg.connect(
    dsn=driver_normalized_database_url,
    timeout=5,
    statement_cache_size=0,
    ssl=hosted_verify_full_context,
)
```

Rerun Task 5 endpoint/verifier regressions proving an explicit password, a sole optional `sslmode` query key, official direct/session `:5432`, rejected official transaction-pooler `:6543`, rejected startup/file query keys, and exact operator-allowlisted dev hosts. Task 6 does not add a second looser DSN path.

Start the execution transaction immediately after connect so all following database work is bounded. Before catalog inspection or planning submitted SQL, issue transaction-local settings with trusted constants/validated decimal timeout literals:

```sql
SET LOCAL statement_timeout = '<validated-ms>ms';
SET LOCAL lock_timeout = '<validated-ms>ms';
SET LOCAL idle_in_transaction_session_timeout = '<bounded-total-ms>ms';
SET LOCAL search_path TO pg_catalog;
SET LOCAL standard_conforming_strings = on;
SET LOCAL row_security = on;
SET LOCAL work_mem = '1MB';
SET LOCAL hash_mem_multiplier = 1.0;
SET LOCAL temp_file_limit = '16MB';
SET LOCAL max_parallel_workers_per_gather = 0;
SET LOCAL jit = off;
SET LOCAL enable_seqscan = on;
SET LOCAL enable_indexscan = off;
SET LOCAL enable_indexonlyscan = off;
SET LOCAL enable_bitmapscan = off;
```

Apply `SET LOCAL search_path TO pg_catalog` before the first discovery query so even its equality operators cannot resolve through a role-controlled initial namespace; later apply/read back the full setting set as an independent fail-closed check. Discover supported resource GUCs through `pg_catalog.pg_settings`, using only `pg_catalog`-qualified callables before that readback. Set every present setting, and fail closed if a present setting cannot be applied. An absent feature-control setting is skipped only when the same trusted capability/version query proves that feature itself is unavailable (for example, a build without JIT); absence never masks a permission/value failure or leaves a supported resource path unbounded. The fixture grants the low-privilege roles `SET` on `temp_file_limit`. Add unit/real-PostgreSQL tests for exact ordering and exact `current_setting` values, including `standard_conforming_strings=on`, `enable_indexscan=off`, `enable_indexonlyscan=off`, and `enable_bitmapscan=off`; also cover `temp_file_limit` permission failure, JIT-unavailable fallback, no parallel workers, forced sequential planning even when role defaults prefer indexes, a sort that crosses `work_mem` but stays under the temp limit, and a query canceled at the temp-file limit without leaking provider text. Add a protocol assertion that the namespace is hardened before discovery and that every callable used before the hardened namespace readback is explicitly `pg_catalog`-qualified.

On that same connection and transaction, run one cycle-safe recursive role query starting from `session_user` and following every direct/transitive `pg_auth_members` edge. Require a nonempty role row, `session_user = current_user`, UTF-8 server encoding, `current_setting('session_replication_role') = 'origin'`, and existence of every configured allowed schema. Reject `replica`/`local` before relation inspection, and add protocol plus real-PostgreSQL controls proving the origin setting is checked rather than silently overwritten. Reject the login or any ancestor that is named `postgres`, starts `pg_`, carries SUPERUSER/BYPASSRLS/CREATEDB/CREATEROLE/REPLICATION, owns `current_database()`, owns any allowed schema, or has `CREATE` on any allowed schema. Later relation preflight also rejects login/ancestor ownership of every accessed or FK-peer relation. The connection-verification path in `database_client.py` reuses this live role query only after issuing its own trusted `SET search_path TO pg_catalog`; verification must not evaluate it under a role-controlled initial namespace. Return/log only `database_role_not_least_privilege`; never include a DSN, password, login, ancestor, relation, schema, SQL, parameter, or raw asyncpg/PostgreSQL error.

Test separate deadlines: 5 seconds for connect; one transaction-work deadline bounded by `statement_timeout_ms + lock_timeout_ms + 10_000ms` and capped at 45 seconds; and 2 seconds each for rollback/close cleanup. Server timeouts still apply per statement. Preserve an external `CancelledError`, shield cleanup only within those deadlines, never retry internally, and raise stable errors `from None`. A rollback/close coroutine that delays or suppresses cancellation must not make the caller wait without a deadline: cancel it after the bounded attempt, then either observe completion within a separately bounded grace period or detach it with exception consumption—never perform an unbounded await. Cover cancellation-resistant rollback and close, including the successful post-commit close path, plus connect/role/catalog/prepare/cursor/execute/commit/rollback/close failures. A post-commit ambiguity is surfaced so Task 1 records `execution_unknown` and never automatically repeats.

For every AST physical relation, first resolve `(schema, name)` to an OID using parameterized `pg_catalog` queries; for a mutation target, also discover a cycle-safe same-schema foreign-key peer closure capped with all AST relations at `MAX_PREFLIGHT_RELATIONS = 32`. Acquire internal `LOCK TABLE ONLY` statements in deterministic quoted `(schema, name)` order, doubling identifier quotes: `ACCESS SHARE` for source-only relations, `SHARE ROW EXCLUSIVE` for a non-TRUNCATE mutation target and every FK peer, and `ACCESS EXCLUSIVE` for the TRUNCATE target. Use the strongest required mode once per relation. The operator must grant the login `MAINTAIN`, `UPDATE`, `DELETE`, or `TRUNCATE` on each FK peer so PostgreSQL permits the stronger lock; recommend table-scoped `MAINTAIN` when the role should not receive peer DML, and fail closed on insufficient lock privilege. Re-resolve OID/name and rerun every catalog assertion after all locks; a pre-lock lookup never authorizes execution. Protocol tests simulate drop/recreate/rename, trigger/index/FK changes between discovery and lock, over-cap/cyclic FK graphs, insufficient lock privilege, and reversed input relation order. A failed resolve, lock, or post-lock recheck rolls back without prepare/execute of submitted SQL.

Every accessed source/target/FK peer must be a permanent PostgreSQL heap base table (`relkind='r'`, `relpersistence='p'`, `relispartition=false`, heap table AM) in exactly the requested schema. Reject views/materialized views, foreign/partitioned/partition tables, inheritance parents/children, relation ownership by the login/any ancestor, RLS flags or policies, custom/domain/array/composite column types, non-`pg_catalog` column collations, expression/partial/invalid/unready/dead indexes, custom index access methods or operator classes, and INCLUDE columns. Cap each relation at `MAX_RELATION_COLUMNS = 128` live columns and `MAX_RELATION_INDEXES = 16` indexes; every catalog list query requests only its cap plus one, and existence-only unsafe-feature checks use `LIMIT 1`. Permit simple column-only `pg_catalog` B-tree indexes, whether primary-key, unique, or nonunique, when every key/opclass/collation is built-in and no expression/predicate/custom option exists. The exact allowed relation column types are `bool`, `int2`, `int4`, `int8`, `float4`, `float8`, `numeric`, `text`, `varchar`, `bpchar`, `date`, `timestamp`, `timestamptz`, `uuid`, `json`, `jsonb`, and `bytea`; the narrower read-output set is defined below. Reject every other type OID/name pair. Bound constraint/FK catalog rows at 64 per relation and reject cap+1 before extending the peer graph. Disable index, index-only, and bitmap scans before planning as defense in depth for the weaker source `ACCESS SHARE` locks: AccessExclusive schema/type/owner/RLS-enable/rule changes are blocked, a policy created while RLS is off remains inert, triggers/defaults/checks do not execute on a source that is not mutated, and compatible concurrent index creation cannot enter the submitted plan. The mutation target/FK-peer stronger locks block trigger/index/policy DDL, and the mutation target additionally has no column defaults/identity, generated expressions, CHECK/exclusion constraints, noninternal triggers, or rewrite rules. Internal FK triggers are allowed only for an accepted FK.

An FK is accepted only when every peer is inside the bounded locked closure, in the same requested schema, passes the applicable ordinary-table/type/index/ownership checks, is nondeferrable and initially immediate, uses only simple built-in key columns, uses `pg_catalog` equality operators and B-tree operator families, and has NO ACTION or RESTRICT for both update and delete. Reject SET NULL/SET DEFAULT/CASCADE, cross-schema, expression/custom-opclass, or dangling peers. After the target and peers are locked and revalidated, UPDATE and DELETE under `SHARE ROW EXCLUSIVE` and TRUNCATE under `ACCESS EXCLUSIVE` run a trusted prepared `SELECT 1 FROM ONLY <quoted target> LIMIT <max_rows + 1>` probe and fully consume its already server-bounded result so no live portal blocks the following mutation. Reject target row `max_rows + 1` before submitted SQL, never use unbounded `count(*)`, and retain the bounded exact row count for UPDATE expansion accounting and mutation output. INSERT remains VALUES-only and is pre-bounded by its static row count. TRUNCATE also rejects every external inbound FK. Thus every mutation is bounded before dispatch; strict command-tag `affected_rows <= max_rows` remains a post-execution invariant, not the primary cap.

Add exact protocol regressions for hidden code in default/generated/CHECK/exclusion/RLS policy/expression or partial index/custom opclass/custom type; stored view/rule; trigger; unsafe FK; and an ancestor-owned relation. Prove catalog checks and locks precede the first prepare/execute of submitted SQL.

For reads, lock/preflight first and then `prepare(original_sql)` exactly once only to inspect attributes. Reject zero or more than 64 columns, a column name over 256 strict UTF-8 bytes, and any Unicode general-category `C*` code point in a column name. The only fixed-width result OID/name pairs are `bool`, `int2`, `int4`, `int8`, `float4`, `float8`, `date`, `timestamp`, `timestamptz`, and `uuid`; their built-in text outputs have fixed small maxima, so the trusted wrapper may cast them to `pg_catalog.text` before byte slicing. Reject `numeric`, `bpchar`, `json`, `jsonb`, `bytea`, and every other output type, plus a projection cast to any rejected type, because safe Phase 9 execution cannot prove their complete text/binary representation is bounded without whole-value materialization.

`text` and `varchar` are the sole variable-width output exception. Each such result must be an explicitly projected direct physical column (optionally table/alias-qualified and output-aliased) whose live `pg_attribute.attstorage='e'`; reject `*`, CTE/set-operation propagation, a literal/parameter/cast/expression result, and any use of that column in `WHERE`, `JOIN`/`ON`, `GROUP BY`, `ORDER BY`, `DISTINCT`, a set operation, operator, function, or cast. Resolve unqualified columns against the locked live column catalogs and reject ambiguity. This keeps the submitted SELECT itself from comparing, sorting, transforming, or otherwise detoasting the wide datum before the wrapper.

Never reference a user alias in generated SQL. Replace output names with generated `__jhin_c0..N` in a derived-table alias list. For each accepted direct text/varchar result, project a compression flag and use this exact ordering in the trusted wrapper around the byte-for-byte original SELECT:

```sql
CASE
  WHEN pg_catalog.pg_column_compression(__jhin_row.__jhin_c0) IS NULL THEN
    pg_catalog.encode(
      pg_catalog.substr(
        pg_catalog.convert_to(
          pg_catalog.substr(
            __jhin_row.__jhin_c0::pg_catalog.text,
            1,
            <effective_cell_bytes + 1>
          ),
          'UTF8'
        ),
        1,
        <effective_cell_bytes + 1>
      ),
      'base64'
    )
  ELSE NULL
END AS __jhin_c0,
(pg_catalog.pg_column_compression(__jhin_row.__jhin_c0) IS NOT NULL)
  AS __jhin_c0_compressed
-- fixed-width columns use only their bounded ::pg_catalog.text form plus
-- convert_to/substr/encode; repeat trusted projections for all attributes
FROM (
<exact original SELECT, followed by a newline>
) AS __jhin_row(__jhin_c0 /*, generated aliases */)
LIMIT <max_rows + 1>
```

The no-semicolon policy makes the nested original unambiguous; the inserted newline safely ends a trailing `--` comment. All wrapper identifiers, functions, types, encodings, and numeric limits are trusted constants/generated names—not model text—and every function/type is explicitly `pg_catalog`. For UTF-8 text/varchar, character `substring` occurs before `convert_to`; PostgreSQL 17's text substring uses the TOAST slicing interface, fetching at most four times the character limit for an uncompressed external value, and the following bytea `substr` enforces the exact byte cap. `attstorage='e'` prevents future compression, while the per-row `pg_column_compression` flag rejects legacy compressed values without returning the cell. If any flag is true, discard the whole result and raise stable `database_output_not_safely_sliceable` from none. Tests set storage to EXTERNAL before insertion, and separately insert a compressed value before changing only catalog storage to EXTERNAL to prove that the per-value check—not `attstorage` alone—is authoritative.

This is an asyncpg/wire-copy bound, not a claim that arbitrary hostile database contents can never consume backend memory. Phase 9 rejects every output form known to require whole-value conversion and prevents accepted wide text from participating in submitted expressions, but PostgreSQL storage internals and already-running privileged database changes remain inside the explicitly trusted database-operator boundary. PostgreSQL does not expose a safe namespace lock to this low-privilege executor: a privileged operator can rename an allowed schema and recreate its old name between catalog revalidation and preparation, rebinding byte-identical qualified SQL. The executor therefore rejects ownership and `CREATE` privilege for the login and all reachable roles, while explicitly trusting database operators not to perform concurrent namespace DDL. Tests assert bounded wire fields and rejection behavior; they must not describe this as a universal PostgreSQL backend-memory or malicious-administrator sandbox.

Compute `effective_cell_bytes` as the minimum of configured `max_cell_bytes` and the remaining configured result budget divided by the validated column count after exact header/marker overhead; fail safely if even headers cannot fit. Base64-decode in Python, use the extra byte to detect truncation, back off an incomplete UTF-8 suffix, append the fixed marker, and keep the final cell within its byte cap. Values are display strings or null; original column names remain separately bounded metadata.

Prepare the wrapper and obtain a manual cursor by awaiting `wrapped_statement.cursor(*params)` with no prefetch; `CursorFactory(prefetch=...)` cannot be awaited for manual one-row reads. Call `fetchrow()` one row at a time, at most `max_rows + 1`. The executor imports and uses the production defaults `sanitize_payload`, `MAX_STRING_CHARS=8192`, and `MAX_DOCUMENT_BYTES=32768` directly; it does not add sanitizer fields to `ToolExecutionContext` or accept per-call cap/redactor overrides. Before appending each row, call `sanitize_payload(candidate)` with its default process redactor and fixed caps, including redaction-marker expansion, then measure the exact final spaced UTF-8 JSON serialization. Retain the sanitized candidate, or stop before the row and set `truncated=True` when it would exceed configured `max_result_bytes`; never rely on raw pre-sanitization JSON sizing. Detect `sanitize_payload`'s whole-document replacement shape and stop rather than returning it as a database result; if redaction expansion alone causes a leaf truncation, retain the safe marked leaf and set the model's `truncated=True`. Boundary tests register a secret whose replacement expands the cell and invoke a real `ToolGateway` constructed with its production-default output limit—no custom context/gateway sanitizer option—proving the returned value is redacted and the result is neither gateway-truncated nor replaced wholesale.

For UPDATE/DELETE, run the bounded target probe first, execute the exact original SQL only when it returned at most `max_rows`, and parse only the expected strict asyncpg command tag. INSERT is already statically row-bounded and follows the same strict tag check. Treat any impossible `affected_rows > max_rows` as an invariant failure inside the transaction and roll back. Return no row data. TRUNCATE accepts exactly one table with default/CONTINUE IDENTITY and default/RESTRICT, dispatches only after the same bounded probe under `ACCESS EXCLUSIVE`, and returns the pre-probed row count as `affected_rows`; RESTART IDENTITY/CASCADE/ONLY/descendants/multiple tables never dispatch.

- [ ] **Step 6: Run executor protocol tests to verify RED**

Run:

```bash
uv run pytest packages/connectors/tests/supabase/test_database_preflight.py packages/connectors/tests/supabase/test_database_tools.py packages/connectors/tests/supabase/test_database_gateway.py -q
```

Expected: FAIL because live database executors, locked preflight, and bounded wrapper are absent.

- [ ] **Step 7: Write the isolated PostgreSQL fixture contract test and verify RED**

Create `tests/test_compose_supabase_db_fixture.py` first. Render the production Compose file alone and the production-plus-dev files as JSON. Assert `fake-supabase-db`, its named volume, fixture credential, fixture port, and database allowlist are absent from production; the dev service uses `postgres:17-alpine`, exact environment `POSTGRES_DB=supabase_fixture`, `POSTGRES_USER=postgres`, and `POSTGRES_PASSWORD=phase9-fixture-admin-only`, a read-only init mount, a localhost-only configurable port, a project-scoped named volume, and a sentinel-backed health check; and only the dev API/agent-worker receive `fake-supabase-db:5432` in their database-host allowlist.

Run:

```bash
uv run pytest tests/test_compose_supabase_db_fixture.py -q
```

Expected: FAIL because the dev fixture service, volume, mount, health contract, and environment documentation do not exist.

- [ ] **Step 8: Add the isolated real PostgreSQL 17 fixture, make its contract green, and write failing execution/security tests**

Create `tests/fixtures/supabase/init.sql` with fixture-only credentials and put a health sentinel last:

```sql
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
CREATE ROLE jhin_reader LOGIN PASSWORD 'reader-pass'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
CREATE ROLE jhin_writer LOGIN PASSWORD 'writer-pass'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
GRANT SET ON PARAMETER temp_file_limit TO jhin_reader, jhin_writer;
CREATE TABLE public.widget_groups (id integer PRIMARY KEY, label text NOT NULL);
ALTER TABLE public.widget_groups ALTER COLUMN label SET STORAGE EXTERNAL;
CREATE TABLE public.widgets (
  id integer PRIMARY KEY,
  group_id integer NOT NULL REFERENCES public.widget_groups(id)
    ON UPDATE NO ACTION ON DELETE NO ACTION NOT DEFERRABLE,
  name text NOT NULL
);
ALTER TABLE public.widgets ALTER COLUMN name SET STORAGE EXTERNAL;
CREATE INDEX widgets_group_id_idx ON public.widgets (group_id);
INSERT INTO public.widget_groups VALUES (1, 'primary');
INSERT INTO public.widgets VALUES
  (1, 1, 'alpha'), (2, 1, 'beta'), (3, 1, repeat('x', 20000));
CREATE TABLE public.fixture_ready (ready boolean PRIMARY KEY);
INSERT INTO public.fixture_ready VALUES (true);
CREATE SCHEMA private;
REVOKE ALL ON SCHEMA private FROM PUBLIC;
CREATE TABLE private.side_effects (id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, source text NOT NULL);
GRANT CONNECT ON DATABASE supabase_fixture TO jhin_reader, jhin_writer;
GRANT USAGE ON SCHEMA public TO jhin_reader, jhin_writer;
GRANT SELECT ON public.widgets, public.widget_groups TO jhin_reader;
GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON public.widgets TO jhin_writer;
GRANT SELECT ON public.widget_groups TO jhin_writer;
GRANT MAINTAIN ON public.widget_groups TO jhin_writer;
```

The fixture-only identity/default lives in `private.side_effects`, which agent SQL can never name; `public.widgets` remains a safe mutation target. Do not grant `CREATE` on `public` to either low-privilege role now that agent DDL is absent.

Define `fake-supabase-db` only in `compose.dev.yaml`, using `postgres:17-alpine`, exact environment `POSTGRES_DB=supabase_fixture`, `POSTGRES_USER=postgres`, and `POSTGRES_PASSWORD=phase9-fixture-admin-only`, a project-scoped named volume, the read-only init mount, and a localhost-only `${FAKE_SUPABASE_DB_DEV_PORT:-55433}` binding. Its health check must connect to `supabase_fixture` as `postgres` and query `public.fixture_ready`, not merely call `pg_isready`, because the official image starts a temporary server before init scripts finish. Add exactly `fake-supabase-db:5432` to `JHIN_CONNECTOR_ALLOWED_DB_HOSTS` for API verification and agent-worker execution in the dev override only. Document the fixture port and role names in `.env.example`; no fixture service, volume, password, or DB allowlist enters production-shaped `compose.yaml`.

Rerun `uv run pytest tests/test_compose_supabase_db_fixture.py -q`. Expected: PASS before the integration suite relies on the fixture.

Mark `test_database_integration.py` with `pytest.mark.integration` and require explicit reader, writer, and fixture-admin DSNs from environment. Use real asyncpg transactions/prepared statements/manual cursors; do not substitute the protocol fake.

Cover qualified and unqualified column reads, nested/recursive CTE aliases for fixed-width outputs, joins/set operations, bound parameters, exact/over 8,192-byte parameter leaves and 16,000-byte compact parameter payloads through approval/replay, row/cell/document boundaries, 64/65 columns, Unicode `C*` output aliases, multibyte truncation, duplicate/hostile aliases, redaction-expanding cells, exact 30,000-byte executor output passing through a real production-default `ToolGateway`, statement versus lock timeout ordering, resource/planner GUCs, `session_replication_role`, temp spill cancellation, cancellation cleanup, and transaction rollback. Verify the generated wrapper contains the exact original SQL and generated aliases only, retains bound `$N` values, uses only the fully qualified trusted functions shown above, limits to `max_rows + 1`, and copies at most one bounded encoded row per cursor fetch.

For extraction, store a large incompressible UTF-8 value after setting its physical text column to `STORAGE EXTERNAL`; prove direct `text` and `varchar` projections slice by characters before conversion, then by exact bytes, and transfer only the bounded base64 prefix. Insert a highly compressible value while the column is EXTENDED, change only its catalog storage to EXTERNAL without rewriting it, and prove `pg_column_compression` causes stable rejection with no cell bytes copied. Reject numeric/bpchar/json/jsonb/bytea results and casts, text/varchar through a CTE/star/expression/cast/operator/DISTINCT/set operation, and any wide text/varchar used in WHERE/JOIN/GROUP/ORDER. Accept a direct aliased text/varchar projection while predicates/order use fixed-width columns. Include mixed-case quoted/unquoted column controls and document that the test proves asyncpg/wire bounds, not a universal backend-memory sandbox over a malicious database administrator.

Create fixture-admin attack objects with cleanup in `finally` and prove rejection before submitted SQL executes:

- a view and materialized view over a SECURITY DEFINER function/private table;
- partitioned/partition, foreign, and inheritance parent/child relations;
- custom/domain/array columns and a custom collation;
- default, generated, CHECK, and RLS-policy expressions backed by SECURITY DEFINER functions that would append to `private.side_effects` (use a deliberately misdeclared immutable sequence helper where PostgreSQL requires immutable generated/index expressions);
- user trigger and rewrite rule side effects;
- expression/partial/custom-opclass/INCLUDE/invalid indexes, plus a safe ordinary
  nonunique column-only `pg_catalog` B-tree index that remains usable;
- exclusion constraints;
- unsafe cross-schema or CASCADE/SET NULL/SET DEFAULT/deferrable FKs;
- a safe same-schema nondeferrable NO ACTION/RESTRICT FK graph and a graph over the 32-relation cap.

For every unsafe case, assert unchanged target/peer/private rows or sequence counters. Include a plain table with both safe nonunique and unique B-tree indexes and the safe FK graph to prove approved bounded mutations still work under deterministic `SHARE ROW EXCLUSIVE` peer locks; remove the peer `MAINTAIN` grant in one test and prove failure before SQL. Tables at 128 columns/16 indexes and their 129/17 cases prove exact catalog boundaries without unbounded fetches. Exact/cap+1 mutation-byte tests repeat one large parameter across VALUES rows and UPDATE columns; `INSERT ... SELECT` and `UPDATE ... SET value = source.huge_value` against a huge stored source are rejected before submitted SQL. UPDATE and DELETE against a target containing `max_rows + 1` rows are rejected by a spy-confirmed cap+1 probe before the submitted command; INSERT is rejected statically at `max_rows + 1` VALUES rows. A single default/CONTINUE+RESTRICT TRUNCATE at or below the cap succeeds; spy on the trusted probe to prove it reads no more than `max_rows + 1` rows, while multi-table/RESTART/CASCADE/over-cap/external-inbound-FK forms leave every row and sequence unchanged.

Create a custom composite type plus symbolic operator in `public` backed by a SECURITY DEFINER counter function. As fixture admin, prove the control resolves/fires with `search_path=public,pg_catalog`, then prove `search_path=pg_catalog` cannot resolve it. The executor must also reject custom relation/output types before prepare and explicit `OPERATOR(public.op)` in static policy. This covers unqualified arithmetic/predicate operator resolution without putting an untrusted schema on the executor path.

After successful connection verification, change each live fact independently and restore it in `finally`: rotate the credential; change project/schema/write config; use the fixture admin to alter the test login to each privileged flag; add direct/deep inherited `pg_read_all_data` or privileged ancestors; make a deep ancestor own the current database, an allowed schema, or an accessed/FK-peer relation; and simulate a session/current-user mismatch. Every invocation re-resolves current state and fails before submitted SQL with only `database_role_not_least_privilege` or another stable credential-free code. Assert captured logs/errors contain no DSN password, SQL marker, parameter marker, login, ancestor, schema, or relation name.

Add race coverage. Unit tests mutate catalog answers between initial discovery and the post-lock check. Real PostgreSQL tests hold/attempt conflicting ALTER/owner/RLS/rule operations against a source and ALTER/TRIGGER/index/policy operations against a mutation target, proving the deterministic relation locks either serialize them or fail under `lock_timeout`. A separate source/peer concurrent-index control proves index scans remain disabled and no index support function fires. No pre-lock observation alone permits SQL. Also prove the fixture login and every reachable ancestor neither owns nor has `CREATE` on the allowed schema. State explicitly in the test name and documentation that a privileged operator can terminate or tamper with the executor, including the namespace rename/recreate race described above—this contract closes ordinary effect-bearing relation DDL races under the declared lock/plan rules, not the database-administrator trust boundary.

- [ ] **Step 9: Start a fresh isolated fixture and verify the integration tests are RED**

Use one literal Compose project. Run this entire fenced block as one Bash process—never as separately spawned lines—so the installed cleanup trap remains active through startup and pytest and removes only this task's volume on success, failure, or interruption:

```bash
set -euo pipefail
phase9_db_cleanup() {
  docker compose -p jhin-phase9-dbtest -f compose.yaml -f compose.dev.yaml down --volumes --remove-orphans
}
trap phase9_db_cleanup EXIT INT TERM
phase9_db_cleanup
export FAKE_SUPABASE_DB_DEV_PORT=65434
export POSTGRES_DB=supabase_fixture
export POSTGRES_USER=postgres
export POSTGRES_PASSWORD=phase9-fixture-admin-only
export JHIN_CONNECTOR_ALLOWED_DB_HOSTS=127.0.0.1:65434
export JHIN_PHASE9_DB_READER_DSN=postgresql://jhin_reader:reader-pass@127.0.0.1:65434/supabase_fixture
export JHIN_PHASE9_DB_WRITER_DSN=postgresql://jhin_writer:writer-pass@127.0.0.1:65434/supabase_fixture
export JHIN_PHASE9_DB_ADMIN_DSN=postgresql://postgres:phase9-fixture-admin-only@127.0.0.1:65434/supabase_fixture
docker compose -p jhin-phase9-dbtest -f compose.yaml -f compose.dev.yaml up -d --force-recreate --wait --wait-timeout 60 fake-supabase-db
docker compose -p jhin-phase9-dbtest -f compose.yaml -f compose.dev.yaml ps fake-supabase-db
uv run pytest -m integration packages/connectors/tests/supabase/test_database_integration.py -q
```

Expected: Compose itself waits for the sentinel-backed health check on a new project-scoped volume, then tests FAIL because the executors are absent, and the still-active same-shell trap tears the fixture down. Never parameterize or omit the literal `jhin-phase9-dbtest` destructive target.

- [ ] **Step 10: Implement three fixed-risk tools and the ordered execution boundary**

Register the three exact tools from Step 1. `SupabaseConnector.tools()` returns the Management tuple followed by the database tuple, and the manifest advertises the same exact names once. Every executor first calls `resolve_connection`, requires `auth_type="postgres"`, binds the submitted project/schema to current normalized config, requires `allow_writes=True` for write/destructive, validates the current DSN against the current project ref/process `DATABASE_URL`, creates current hosted TLS state, and only then connects.

Within the connection, preserve this exact order:

```text
begin transaction (read-only for read tool)
set time/namespace/resource GUCs
recheck replication mode, session/direct/transitive role, DB/schema ownership, and server encoding
resolve relation OIDs and bounded FK closure
lock every relation in deterministic order
re-resolve and revalidate every relation/FK/owner fact
read: prepare original for attributes -> enforce fixed/direct-text output matrix -> build/prepare trusted wrapper -> bounded cursor
update/delete/truncate: bounded target probe -> execute original once -> verify command tag
insert: static VALUES row/byte caps -> execute original once -> verify command tag
commit; on every failure rollback; always bounded-close
```

Verification at connection creation never substitutes for these per-call checks. The authorization digest handles parked approval drift, while the executor independently protects immediate calls. Do not retry a database statement or commit internally. All asyncpg/SQLGlot errors become stable credential-free connector errors `from None`; caller cancellation propagates after cleanup.

- [ ] **Step 11: Run unit, real-PostgreSQL, Compose, and quality gates; then commit**

Run this entire fenced block as one Bash process. It creates a fresh fixture for the GREEN gate and keeps the literal-project cleanup trap active through integration and every later check:

```bash
set -euo pipefail
uv run pytest packages/connectors/tests/supabase packages/connectors/tests/test_endpoints.py packages/connectors/tests/test_manifest_registry.py packages/policy/tests/test_evaluator.py packages/tools/tests/test_gateway.py tests/test_compose_supabase_db_fixture.py -q
phase9_db_cleanup() {
  docker compose -p jhin-phase9-dbtest -f compose.yaml -f compose.dev.yaml down --volumes --remove-orphans
}
trap phase9_db_cleanup EXIT INT TERM
phase9_db_cleanup
export FAKE_SUPABASE_DB_DEV_PORT=65434
export POSTGRES_DB=supabase_fixture
export POSTGRES_USER=postgres
export POSTGRES_PASSWORD=phase9-fixture-admin-only
export JHIN_CONNECTOR_ALLOWED_DB_HOSTS=127.0.0.1:65434
export JHIN_PHASE9_DB_READER_DSN=postgresql://jhin_reader:reader-pass@127.0.0.1:65434/supabase_fixture
export JHIN_PHASE9_DB_WRITER_DSN=postgresql://jhin_writer:writer-pass@127.0.0.1:65434/supabase_fixture
export JHIN_PHASE9_DB_ADMIN_DSN=postgresql://postgres:phase9-fixture-admin-only@127.0.0.1:65434/supabase_fixture
docker compose -p jhin-phase9-dbtest -f compose.yaml -f compose.dev.yaml up -d --force-recreate --wait --wait-timeout 60 fake-supabase-db
uv run pytest -m integration packages/connectors/tests/supabase/test_database_integration.py -q
docker compose -f compose.yaml -f compose.dev.yaml config --quiet
uv run ruff check packages/connectors tests/test_compose_supabase_db_fixture.py
uv run ruff format --check packages/connectors tests/test_compose_supabase_db_fixture.py
uv run mypy
git diff --check
phase9_db_cleanup
trap - EXIT INT TERM
```

Expected: PASS across parser secrecy/resource boundaries, three exact fixed risks, no DDL surface, current credential/config/role/ownership checks, deterministic locked relation/FK preflight, hidden-code fixtures, bounded mutation rollback, type-specific fixed/direct-text output extraction with unsupported/compressed values rejected, real cursor/time/resource limits, plane isolation, Compose isolation, and static gates. Task 7 still owns adding the connector workspace manifest to the Docker dependency-cache stage before clean production image builds.

Commit:

```bash
git add docs/superpowers/plans/2026-08-17-phase-9-vercel-supabase.md packages/connectors/pyproject.toml packages/connectors/src/jhin_connectors/supabase packages/connectors/tests/supabase packages/connectors/tests/test_manifest_registry.py tests/test_compose_supabase_db_fixture.py uv.lock tests/fixtures/supabase/init.sql compose.dev.yaml .env.example
git commit -m "feat: add bounded Supabase SQL execution"
```

### Task 7: Make connector setup and grant scopes data-driven; wire dev fakes and clean image builds

**Files:**
- Modify: `docs/superpowers/plans/2026-08-17-phase-9-vercel-supabase.md`
- Create: `apps/web/components/scope-editor.tsx`
- Create: `apps/web/tests/scope-editor.test.tsx`
- Create: `apps/web/components/connection-access-summary.tsx`
- Create: `apps/web/tests/connection-access-summary.test.tsx`
- Modify: `apps/api/src/jhin_api/connections/schemas.py`
- Modify: `apps/api/src/jhin_api/connections/service.py`
- Modify: `apps/api/src/jhin_api/connections/router.py`
- Test: `apps/api/tests/test_connections_unit.py`
- Modify: `apps/web/lib/types.ts`
- Modify: `apps/web/lib/hooks.ts`
- Modify: `apps/web/lib/connectors.ts`
- Modify: `apps/web/lib/wizard.ts`
- Modify: `apps/web/components/connectors-gallery.tsx`
- Modify: `apps/web/components/org/tools-access-tab.tsx`
- Modify: `apps/web/app/(app)/agents/new/page.tsx`
- Modify: `apps/web/app/(app)/connectors/page.tsx`
- Modify: `apps/web/tests/connectors.test.ts`
- Test: `apps/web/tests/connectors-page.test.tsx`
- Modify: `apps/web/tests/connectors-gallery.test.tsx`
- Modify: `apps/web/tests/wizard.test.ts`
- Modify: `apps/web/tests/org-tree-render.test.tsx`
- Modify: `docker/python.Dockerfile`
- Modify: `compose.dev.yaml`
- Modify: `.env.example`
- Test: `tests/test_compose_connector_allowlist.py`
- Test: `tests/test_compose_phase9_dev_fakes.py`

**Interfaces:**
- Consumes: API `ConfigFieldOut` auth types/kinds/defaults/bounds, `ToolOut.scope_keys`, `ToolOut.required_grant_scope_keys`, webhook mode/setup/configured status, fake provider modules, Supabase DB settings, and Task 6's existing `fake-supabase-db` service/fixture.
- Produces: reusable scope editing for every connector tool; per-tool wizard grant scopes; typed connection forms; provider-supplied webhook setup; a workspace-safe connection access summary with authorized agent names and exact relevant grant scopes; live gallery cards; dev-only fake Vercel and Supabase Management HTTP services; reproducible clean Python images.

- [ ] **Step 1: Write failing pure UI tests for typed config and data-driven scopes**

Replace the hard-coded connector scope helper with:

```typescript
export type ToolScopeValues = Record<string, string>;

export function buildToolScope(
  tool: ToolInfo,
  values: ToolScopeValues,
): Record<string, string>;

export function missingRequiredScopeKeys(
  tool: ToolInfo,
  values: ToolScopeValues,
): string[];

export function configFieldsForAuth(
  connector: ConnectorInfo,
  authType: string,
): ConfigFieldSpec[];

export function coerceConnectorConfig(
  fields: ConfigFieldSpec[],
  raw: Record<string, string | boolean>,
): Record<string, string | number | boolean | string[]>;
```

Tests prove Vercel renders connection/project/deployment/environment keys only when declared; Supabase renders connection/project/schema; required keys cannot be submitted empty; CLI and delegation retain their special semantics; integer/boolean/string-list config values serialize with correct types; changing auth clears fields not valid for the new scheme.

Update `WizardState` to use `grantToolNames: string[]` plus `grantScopes: Record<string, ToolScopeValues>`, both keyed by `ToolInfo.name`, and delete the single shared `grantConnectionId`/`grantRepository`. At submission, resolve each selected tool name to its `required_capability`; collapse only exact `(capability, scope)` duplicates. Tests prove two selected tools—even two tools sharing a capability—persist independent scopes and emit distinct grant payloads when their scopes differ.

- [ ] **Step 2: Write failing access-summary API and connection/webhook UI tests**

Add admin-only `GET /api/v1/workspaces/{workspace_id}/connections/{connection_id}/access-summary` schemas and failing service/router tests for:

```python
class ConnectionGrantSummaryOut(BaseModel):
    grant_id: UUID
    capability: str
    effect: Literal["allow", "deny"]
    scope: dict[str, str]
    eligible_tool_names: list[str]
    eligibility_reason: str | None

class ConnectionAgentAccessOut(BaseModel):
    agent_id: UUID
    agent_name: str
    authorized: bool
    authorized_tool_names: list[str]
    grants: list[ConnectionGrantSummaryOut]

class ConnectionAccessSummaryOut(BaseModel):
    connection_id: UUID
    agents: list[ConnectionAgentAccessOut]
```

Seed multiple workspaces, two agents, exact `connection_id` allow/deny grants, an unscoped allow, an unscoped/broad deny, a grant for another connection, a wildcard capability, and a grant missing another required tool scope. The response includes only agents/grants relevant to this connection and installed connector capabilities, preserves each exact scope/effect, marks missing-required-scope allows ineligible, applies runtime-compatible deny precedence when deriving `authorized_tool_names`, sorts deterministically, and never returns policy JSON, config secrets, credential IDs, or ciphertext. An admin receives `200`; a viewer receives `403`; a cross-workspace connection ID receives `404` with no agent/grant existence leak.

Assert:

```text
- Linear, Vercel, and Supabase appear exactly once as live connectors.
- Vercel provider-supplied setup shows the callback URL, SHA1 header guidance,
  and a password field/button to store the provider-generated secret.
- It never labels a Vercel secret as Jhin-generated or displays a stored value.
- GitHub/Linear retain the one-time generated-secret dialog.
- Connection detail shows "Webhook secret configured" from the boolean only.
- Supabase management-token and database forms show only their own settings.
- allow_writes is an explicit advanced toggle defaulting off; no agent DDL
  toggle or capability is exposed in Phase 9.
- Connection detail lists authorized agent names and each exact relevant
  capability/effect/scope, labels incomplete or denied grants, shows a clear
  empty state, and never implies that a grant bypasses current approval policy.
```

- [ ] **Step 3: Run web tests to verify RED**

Run:

```bash
uv run pytest apps/api/tests/test_connections_unit.py -q
pnpm --filter jhin-web test -- connectors.test.ts connectors-page.test.tsx connectors-gallery.test.tsx wizard.test.ts scope-editor.test.tsx connection-access-summary.test.tsx org-tree-render.test.tsx
```

Expected: FAIL because the access-summary route/component, scopes, config controls, and webhook setup do not exist or remain hard-coded.

- [ ] **Step 4: Implement the data-driven UI**

Mirror the new API fields in `ToolInfo`, `ConfigFieldSpec`, `ConnectorInfo`, `ConnectionInfo`, and `WebhookSetup`. `ScopeEditor` receives one `ToolInfo`, matching connections, values, and `onChange`; it renders labels for known keys (`connection_id`, `project_id`, `deployment_id`, `environment`, `project_ref`, `schema`, `function_slug`, repository/branch/CLI keys) and a safe text input for any future declared key. Required keys get visible “Required for this tool” copy and block submission.

Use `ScopeEditor` in both `ToolsAccessTab` and the agent wizard. Key picker options by `tool.name` and display both tool name and required capability; never merge different tools' scope keys into one grant editor. Keep per-tool scope state in the wizard, submit the selected tool's `required_capability`, and rely on Task 1's distinct-scope grants when two tools share a capability.

Render config controls by manifest kind: text/password as appropriate, bounded integer input, checkbox for booleans, and newline-separated editor normalized to a string list. Filter by `auth_types`; submit defaults so the operator sees the effective safety posture. Put `allow_writes` under a clearly labeled Advanced database access disclosure, and do not render or submit an `allow_ddl` field.

For `provider_supplied`, show setup metadata after create and in connection detail, accept the provider-generated secret through the new `PUT /webhook-secret` route, then discard the local form state. Remove Linear/Vercel/Supabase from `UPCOMING_CONNECTORS`; leave HTTP as future work without assigning it to completed Phase 9.

Implement the access summary with one streamed workspace-filtered query over `AgentCapabilityGrant` joined to `Agent`, filtered in SQL to the exact/prefix-wildcard/universal capability patterns that can match the selected connector's registered tools. Fetch in bounded driver batches, apply exact connection relevance before counting rows, retain at most 256 relevant rows, and return a stable generic `503` on the 257th; thousands of nonmatching glob denies must not consume the relevant-row cap. An allow is relevant only when its exact scope contains this `connection_id`; never treat an unscoped/broad allow as connection authorization. A deny is relevant when its exact or fnmatch-style `connection_id` scope covers this connection, including an unscoped connector deny. Required grant-scope keys constrain allows only; connection-only or broad denies still affect every matching tool. For each matching tool, apply existing capability/scope deny precedence before adding the tool name to `authorized_tool_names`; when two non-connection glob scope languages may overlap, conservatively report the tool as denied rather than claim authorization that a runtime invocation could lose. Return agents with relevant rows even when all rows are denied/incomplete so administrators can diagnose access, but set `authorized=True` only when at least one tool remains eligible. Render authorized agents first and place exact grants/scopes in an Advanced disclosure on the connection detail page.

- [ ] **Step 5: Add the two dev HTTP fakes and clean-build coverage**

Keep Task 6's `fake-supabase-db`, named volume, init mount, fixture SQL, DB allowlist, and port documentation unchanged. This task adds only these healthy HTTP services to `compose.dev.yaml`:

```text
fake-vercel       module jhin_connectors.testing.fake_vercel, host port 8094
fake-supabase     module jhin_connectors.testing.fake_supabase, host port 8095
```

Build each from the agent-worker image, run its module on container port 8080, bind only `127.0.0.1:${FAKE_VERCEL_DEV_PORT:-8094}` or `127.0.0.1:${FAKE_SUPABASE_DEV_PORT:-8095}`, give `/_state` health checks, and attach only to the dev `data` network. Preserve Task 2's GitHub/Linear development endpoints and extend `JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS` for both API and agent-worker to exactly `http://fake-github:8080,http://fake-linear:8080,http://fake-vercel:8080,http://fake-supabase:8080` in that stable order. Replacing the prior origins would regress existing connector verification and worker tools. Production `compose.yaml` receives neither service nor this allowlist. Document the two override ports in `.env.example` without real credentials.

In the dependency-cache stage of `docker/python.Dockerfile`, add the missing workspace manifests:

```dockerfile
COPY packages/policy/pyproject.toml packages/policy/
COPY packages/tools/pyproject.toml packages/tools/
COPY packages/connectors/pyproject.toml packages/connectors/
```

- [ ] **Step 6: Run web, Compose, and clean-image gates**

Run:

```bash
uv run pytest apps/api/tests/test_connections_unit.py -q
uv run pytest tests/test_compose_phase9_dev_fakes.py tests/test_compose_connector_allowlist.py tests/test_compose_supabase_db_fixture.py -q
pnpm --filter jhin-web lint
pnpm --filter jhin-web typecheck
pnpm --filter jhin-web test
docker compose -f compose.yaml -f compose.dev.yaml config --quiet
docker compose -f compose.yaml -f compose.dev.yaml build --no-cache api agent-worker workflow-worker event-worker web
docker compose -f compose.yaml -f compose.dev.yaml up -d --wait --wait-timeout 60 fake-vercel fake-supabase fake-supabase-db
docker compose -f compose.yaml -f compose.dev.yaml ps fake-vercel fake-supabase fake-supabase-db
git diff --check
```

Expected: connection access-summary RBAC/query tests and web gates pass; Compose validates; clean images build without relying on stale layers; both new HTTP fakes and Task 6's existing fixture database become healthy.

- [ ] **Step 7: Commit UI and dev infrastructure**

```bash
git add docs/superpowers/plans/2026-08-17-phase-9-vercel-supabase.md apps/api/src/jhin_api/connections apps/api/tests/test_connections_unit.py apps/web docker/python.Dockerfile compose.dev.yaml .env.example tests/test_compose_connector_allowlist.py tests/test_compose_phase9_dev_fakes.py
git commit -m "feat: add scoped production connector setup"
```

### Task 8: Prove the Phase 9 exit test and close documentation

**Files:**
- Create: `tests/integration/test_phase9_exit.py`
- Modify: `tests/integration/conftest.py`
- Modify: `tests/integration/test_phase5_exit.py`
- Modify: `tests/integration/test_phase6_exit.py`
- Modify: `tests/integration/test_phase7_exit.py`
- Modify: `tests/integration/test_phase8_exit.py`
- Create: `scripts/assert_phase9_production_compose.py`
- Test: `tests/test_phase9_production_compose.py`
- Create: `docs/architecture/vercel-and-supabase.md`
- Modify: `README.md`
- Modify: `docs/implementation-plan.md`
- Modify only defects reproduced by this task's verification; do not weaken an assertion to make a gate green.

**Interfaces:**
- Consumes: Tasks 1–7, the real API/agent/event-worker/gateway path, explicit policy preset routes, dev fakes and fault controls, approval routes, NATS normalization, access summaries, and the isolated fixture database.
- Produces: deterministic Phase 9 acceptance evidence from a freshly named Compose project/volumes, an automated production-Compose negative assertion, operator architecture guidance, and checklist closure for exactly Phase 9.

- [ ] **Step 1: Write the failing Phase 9 integration scenarios**

Implement independent workspace-isolated tests with unique names, deterministic fake/fixture resets, and side-effect counts. Every normal scenario creates its agent, sets `PUT .../policy` to `{"preset":"balanced"}`, then reads the policy back and asserts `preset == "balanced"` before invoking a tool. Only the isolated AUTO subcases in scenario 4 may select another policy, and they must assert exactly what was selected.

```text
1. Vercel inspect: a scoped agent lists/reads its project and deployment,
   proves /v6/deployments always sends projectId, rejects a mixed-project
   page, follows only bounded pagination, reads bounded build logs and
   environment metadata, and never receives the fake environment value.
2. Vercel production guard: no grant, unscoped grant, wrong project scope,
   wrong deployment ownership, mismatched project Git link, wrong repository,
   and missing Balanced approval each produce zero preview/redeploy/promote/
   alias side effects; exact approved scoped actions execute once with the
   declared ELEVATED/DESTRUCTIVE risks.
3. Approval liveness: park provider and SQL mutations, then independently
   revoke a grant, add a deny/forbid rule, rotate credentials, change public
   config, disable/delete the connection, or simulate tool-definition drift.
   Approval never executes stale authority and emits the exact denial audit.
4. Invocation race/crash: race two Balanced approval resolvers for one Vercel
   mutation and one Supabase function mutation; each has one side effect and
   one replay. An Autonomous agent auto-runs ELEVATED preview create but still
   parks DESTRUCTIVE; a separate explicit custom-AUTO agent exercises a
   destructive one-shot post-effect transport fault. Vercel redeploy and
   Supabase function deploy each become execution_unknown, and activity/gateway
   retries cause no duplicate side effect, message, or run event. Repeat a
   same-invocation database write race and observe one row change.
5. Vercel events: store a provider-supplied secret, accept a correctly signed
   deployment.ready body at exactly 1 MiB, reject a bad signature and cap+1
   body, and replay once. Inject publish success followed by precommit failure,
   retry the same delivery, and observe the same ingress/canonical UUID, one
   WebhookDelivery, one canonical deployment event, and at most one trigger.
6. Supabase management: a management-token connection reads bounded project
   metadata, bounded projected logs, and function metadata. Wrong project/
   function scope and missing approval cause zero effects; separately approved
   DESTRUCTIVE deploy and delete succeed once, with source/env/credential data
   absent from every output. Post-effect retry behavior is covered in group 4.
7. Supabase database read: a postgres connection using jhin_reader reads
   public.widgets with explicit qualification and a CTE alias, demonstrates
   max_rows, max_cell_bytes, max_result_bytes, and a real statement timeout,
   and rejects private/cross-schema/unqualified-table/catalog-schema/multi-statement/lock/
   session/data-changing SQL plus function, custom operator, and custom cast
   bypasses before execution.
8. Supabase database mutation/live recheck: read-only config, missing required
   scope, wrong SQL-risk tool, missing Balanced approval, and
   `allow_writes=false` leave state unchanged; exact approved INSERT,
   UPDATE/DELETE, and bounded single-table TRUNCATE behave as declared, while
   every DDL statement remains unavailable and rejected. After verification,
   rotate the credential and separately change project/schema/write config or
   current role to postgres/superuser/BYPASSRLS/CREATEDB/CREATEROLE/REPLICATION,
   grant a dangerous built-in role or ancestor ownership of the database,
   allowed schema, accessed table, or FK-peer table, or add a privileged deep
   transitive membership; every execution rechecks live state and causes zero
   unauthorized SQL effects. Trigger/rule, partition/foreign/view/inheritance,
   RLS/policy, custom-type/opclass, default/generated/CHECK/exclusion,
   expression/partial-index, over-column/index/constraint caps, unsafe-FK,
   INSERT-SELECT/source-assignment, and mutation-byte-expansion paths fail the
   same-transaction locked preflight with zero changes to private or secondary
   relations. The
   real-PostgreSQL path also proves approval-lossless SQL/parameter bounds,
   resource GUCs, `pg_catalog`-only search path, allowed ordinary nonunique
   B-tree indexes, bounded UPDATE/DELETE/TRUNCATE target probing, Unicode-safe
   output names, production-default sanitizer/redaction budgeting, the
   fixed/direct-text type-specific base64 wrapper with unsupported or compressed
   variable-width results rejected, one-row cursor, and row/cell/result caps.
9. Plane/workspace isolation: management tools cannot use the postgres
   connection, database tools cannot use the management token, and another
   workspace cannot reference either connection.
10. Control-plane security and access visibility: viewers cannot create/rotate
    connections, store webhook secrets, or read access summaries; admin
    mutations require CSRF. An admin access summary names only agents with
    relevant exact connection grants, shows capability/effect/scope and denied/
    incomplete rows accurately, and leaks no token, DSN password, webhook
    secret, environment value, source content, or private fixture value through
    connection/tool/audit/API/UI output.
```

Use the normal agent/gateway path for authorization and approval assertions, provider HTTP state only for side-effect observation, and the fixture database only to verify data effects. The webhook crash subcase may call the ingress service with the real Postgres/JetStream resources to place the fault precisely after publish and before commit. Use controlled test-only database updates only to simulate config/role drift that has no public update route. Never connect a Supabase tool to Jhin's `postgres` service/database.

Make existing integration endpoint constants environment-driven in `conftest.py` and Phases 5–8 (`JHIN_API_URL`, `JHIN_WEB_URL`, `JHIN_NATS_URL`, `JHIN_TEMPORAL_ADDRESS`, `JHIN_FAKE_GITHUB_URL`, and `JHIN_FAKE_LINEAR_URL`) so the full suite can target the dedicated Phase 9 stack without conflicting with a developer's normal stack. `compose()` adds literal project `-p` from validated `JHIN_TEST_COMPOSE_PROJECT`, whose acceptance value is fixed to `jhin-phase9-acceptance` in Step 3.

Create `assert_phase9_production_compose.py` with a pure `assert_production_config(config: dict[str, Any])` plus a CLI that runs `docker compose -f compose.yaml config --format json`. Fail if any service name starts with `fake-`, or if the rendered JSON contains `fake-supabase-db`, `supabase_fixture`, `reader-pass`, `writer-pass`, `phase9-fixture-admin-only`, `JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS`, or `JHIN_CONNECTOR_ALLOWED_DB_HOSTS`. Unit tests feed safe and individually poisoned configs; this is an executable negative assertion, not a prose inspection.

- [ ] **Step 2: Run acceptance tests to verify RED or expose missing behavior**

Run:

```bash
uv run pytest -m integration tests/integration/test_phase9_exit.py -v
```

Expected before all implementation is integrated: FAIL at the first missing Phase 9 behavior. Diagnose failures with the systematic-debugging process; preserve every security assertion.

- [ ] **Step 3: Establish a fresh complete dev stack**

Use literal Compose project `jhin-phase9-acceptance` and non-default host ports so an existing `jhin` project is untouched. The first command deletes volumes belonging only to that exact acceptance project, guaranteeing both Jhin Postgres and `fake_supabase_data` rerun initialization:

```bash
export JHIN_TEST_COMPOSE_PROJECT=jhin-phase9-acceptance
export WEB_PORT=13000
export API_PORT=18000
export APP_URL=http://127.0.0.1:13000
export POSTGRES_DEV_PORT=65432
export NATS_DEV_PORT=14222
export NATS_MONITOR_DEV_PORT=18222
export TEMPORAL_DEV_PORT=17233
export TEMPORAL_UI_DEV_PORT=18233
export FAKE_PROVIDER_DEV_PORT=18090
export FAKE_GITHUB_DEV_PORT=18091
export FAKE_LINEAR_DEV_PORT=18092
export SANDBOX_RUNNER_DEV_PORT=18093
export FAKE_VERCEL_DEV_PORT=18094
export FAKE_SUPABASE_DEV_PORT=18095
export FAKE_SUPABASE_DB_DEV_PORT=65433
export SANDBOX_NETWORK=jhin_phase9_acceptance_sandbox
export SANDBOX_DEFAULT_IMAGE=jhin-phase9-sandbox:acceptance
export JHIN_API_URL=http://127.0.0.1:18000
export JHIN_WEB_URL=http://127.0.0.1:13000
export JHIN_NATS_URL=nats://127.0.0.1:14222
export JHIN_TEMPORAL_ADDRESS=127.0.0.1:17233
export JHIN_FAKE_GITHUB_URL=http://127.0.0.1:18091
export JHIN_FAKE_LINEAR_URL=http://127.0.0.1:18092
export JHIN_FAKE_VERCEL_URL=http://127.0.0.1:18094
export JHIN_FAKE_SUPABASE_URL=http://127.0.0.1:18095
export SANDBOX_RUNNER_DEV_URL=http://127.0.0.1:18093
export JHIN_CONNECTOR_ALLOWED_DB_HOSTS=127.0.0.1:65433
export JHIN_PHASE9_DB_READER_DSN=postgresql://jhin_reader:reader-pass@127.0.0.1:65433/supabase_fixture
export JHIN_PHASE9_DB_WRITER_DSN=postgresql://jhin_writer:writer-pass@127.0.0.1:65433/supabase_fixture
export JHIN_PHASE9_DB_ADMIN_DSN=postgresql://postgres:phase9-fixture-admin-only@127.0.0.1:65433/supabase_fixture
docker compose -p jhin-phase9-acceptance -f compose.yaml -f compose.dev.yaml down --volumes --remove-orphans
docker compose -p jhin-phase9-acceptance -f compose.yaml -f compose.dev.yaml --profile build build sandbox-image
docker compose -p jhin-phase9-acceptance -f compose.yaml -f compose.dev.yaml build --no-cache api agent-worker workflow-worker event-worker web fake-vercel fake-supabase
docker compose -p jhin-phase9-acceptance -f compose.yaml -f compose.dev.yaml up -d --force-recreate --wait --wait-timeout 120
docker compose -p jhin-phase9-acceptance -f compose.yaml -f compose.dev.yaml ps
docker compose -p jhin-phase9-acceptance -f compose.yaml -f compose.dev.yaml exec -T api jhin-db-migrate
uv run alembic -c packages/db/alembic.ini heads
```

Expected: every required service/fake is healthy under the dedicated project; Docker names fresh project-scoped `postgres_data`, `nats_data`, and `fake_supabase_data` volumes; Alembic reports exactly one head and no Phase 9 migration. Never replace the literal project name on either `down --volumes` command with an unchecked variable.

- [ ] **Step 4: Make the acceptance suite green without weakening boundaries**

For each failure, reproduce the narrow case, fix the source boundary, run its focused unit file, then rerun:

```bash
uv run pytest -m integration tests/integration/test_phase9_exit.py -v
```

Expected: all ten scenario groups pass, with zero unauthorized or duplicate fake/database side effects and explicit Balanced-policy evidence for every normal agent.

- [ ] **Step 5: Write the architecture and operator guidance**

Create `docs/architecture/vercel-and-supabase.md` with these concrete sections:

```markdown
# Vercel and Supabase Connectors

## Authority planes
Explain Vercel access-token authority and the two independent Supabase
connection rows. State that one credential never crosses planes.

## Grants and approvals
List every tool, risk, required scope key, write opt-in, absence of agent DDL,
approval resume
reauthorization, Balanced/Autonomous/custom behavior, deterministic invocation
claims, execution_unknown reconciliation, and example least-privilege grants.

## Vercel webhooks
Document manual/provider-plan availability, callback URL, provider-generated
secret entry, x-vercel-signature HMAC-SHA1, event allowlist, body cap, and
deduplication. Do not imply every Vercel plan supports account webhooks.

## Supabase database role
Show creation of a custom NOSUPERUSER NOBYPASSRLS login, grants limited to
curated ordinary nonpartition base tables, TLS DSN setup, allowlist settings,
timeouts, row/cell/result caps, explicit physical relation qualification,
`pg_catalog`-only search path, function/operator/cast denials, direct plus
inherited role and ownership rejection, deterministic relation/FK-peer locking,
hidden-code preflight, pre-dispatch target bounds for every destructive mutation,
the fixed/direct-text output-type matrix and its trusted-database residual, and
the SQL decision table. State that schema allowlisting
does not classify sensitive cells; least-privilege table/column grants and
purpose-built ordinary tables containing only intended data are the primary
confidentiality boundary. Views are intentionally rejected in Phase 9 rather
than recursively trusting stored view definitions and owner authority.

## Connection access summary
Explain how authorized agent names and exact relevant grant capability/effect/
scope are derived, why incomplete/deny rows are shown, and why grants do not
bypass current policy or live connection checks.

## Endpoint policy
Document official defaults, exact operator allowlists for self-hosted/dev,
redirect rejection, and why private/metadata targets fail closed.

## Dev fakes
Document ports 8094, 8095, and 55433, fixture roles, reset/state endpoints,
and that none exists in production Compose.

## Verification
Record the exact fresh commands and counts from Steps 4, 6, and 7.
```

- [ ] **Step 6: Run Python and web quality gates**

Run:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest -m "not integration"
pnpm --filter jhin-web lint
pnpm --filter jhin-web typecheck
pnpm --filter jhin-web test
pnpm --filter jhin-web exec next build --webpack
```

Expected: every command passes. Record actual counts, not estimates.

- [ ] **Step 7: Run the full integration and production-build gates**

Run:

```bash
uv run pytest -m integration tests/integration packages/connectors/tests/supabase/test_database_integration.py -v
docker compose -p jhin-phase9-production-check -f compose.yaml build --no-cache api agent-worker event-worker workflow-worker web
uv run python scripts/assert_phase9_production_compose.py
git diff --check
git status --short
```

Expected: the full integration suite and real-PostgreSQL gate pass; clean production images build; the production-config assertion proves no fake service, fixture database/credential, or dev connector allowlist is rendered. Inspect status only to ensure Task 8's own paths are accounted for; preserve every unrelated user-owned change, including the untracked production-plan reference from Task 0A.

- [ ] **Step 8: Tear down only the dedicated acceptance project**

After preserving test output/counts, run:

```bash
docker compose -p jhin-phase9-acceptance -f compose.yaml -f compose.dev.yaml down --volumes --remove-orphans
docker ps --filter label=com.docker.compose.project=jhin-phase9-acceptance --format '{{.Names}}'
docker volume ls --filter label=com.docker.compose.project=jhin-phase9-acceptance --format '{{.Name}}'
```

Expected: both filtered outputs are empty. This removes only the literal Phase 9 acceptance containers/networks/volumes and leaves the normal `jhin` project plus every other Docker project untouched.

- [ ] **Step 9: Close Phase 9 only after fresh evidence**

Update README status to Phase 9 and add a short least-privilege Vercel/Supabase walkthrough linking the architecture document. In `docs/implementation-plan.md`, mark exactly the ten Phase 9 checklist lines `[x]`; leave Phases 10–11 and later Jhin company/chat/memory/redesign work unchanged. Record verification date, exact commands, and actual counts in the architecture document. Do not claim the broader product redesign is complete.

Reconfirm the completed canonical migration from commit `d8d1055` with a repository-wide negative assertion over tracked content. There is no tracked canonical-plan exception. Preserve the user's separate untracked production-plan reference by checking its exact assembled path without staging or editing it.

```bash
git show --stat --oneline d8d1055
if git grep -nEi '[o]rg[f]orge' -- .; then
  echo "Legacy product branding remains in tracked content"
  exit 1
fi
untracked_reference='org''forge-production-implementation-plan.md'
test "$(git status --short -- "$untracked_reference")" = "?? $untracked_reference"
rg -n "Phase 9|Vercel|Supabase|provider_supplied|logs\.all" README.md docs/architecture/vercel-and-supabase.md docs/implementation-plan.md
git diff --check
```

Expected: commit `d8d1055` is the scoped canonical branding migration; no tracked file contains legacy product branding; the user-owned reference remains untracked; `logs.all` appears only in an explicit warning that it is not used; exactly Phase 9 is newly checked.

- [ ] **Step 10: Commit Phase 9 acceptance and closure**

```bash
git add tests/integration/test_phase9_exit.py tests/integration/conftest.py tests/integration/test_phase5_exit.py tests/integration/test_phase6_exit.py tests/integration/test_phase7_exit.py tests/integration/test_phase8_exit.py scripts/assert_phase9_production_compose.py tests/test_phase9_production_compose.py docs/architecture/vercel-and-supabase.md README.md docs/implementation-plan.md
git commit -m "docs: close Phase 9 production integrations"
```

## Final Review Checklist

- [ ] Compare every Phase 9 checklist item in `docs/implementation-plan.md` to a focused test and an end-to-end assertion in Task 8.
- [ ] Confirm every new tool declares fixed risk, capability, `scope_keys`, `required_grant_scope_keys`, strict input/output models, and approval support where required.
- [ ] Confirm Balanced parks every Phase 9 ELEVATED/DESTRUCTIVE mutation, Autonomous auto-runs ELEVATED preview but still parks DESTRUCTIVE, and only an explicit custom AUTO rule bypasses destructive parking.
- [ ] Confirm deterministic invocation identity uses run/step/ordinal rather than provider call ID and that immediate, approved, racing, retried, and post-effect-ambiguous calls share one CAS/replay/unknown path.
- [ ] Confirm every provider/SQL mutation test checks both the returned denial/status and the fake/database side-effect count, including `execution_unknown` no-retry cases.
- [ ] Confirm Vercel deployment list always sends `projectId`, bounds pagination, validates every row, and verifies project/deployment ownership plus fetched Git provider/repository before logs or mutations; environment values are never modeled in output.
- [ ] Confirm Supabase management/database auth types cannot substitute for each other and `project_ref` comes from connection config.
- [ ] Confirm Supabase Management project/log/function reads and approved function deploy/delete pass end to end with destructive risks and no source/value leak.
- [ ] Confirm every database execution re-resolves current credential/config, rechecks endpoint/TLS/project, direct role flags, and the transitive dangerous-role membership closure, then runs bounded SQL on the same checked connection without exposing role names.
- [ ] Confirm SQL validation enforces token/node/depth limits, catches parser recursion without logging raw SQL, walks the complete AST, handles casts before the blanket function denial, executes the original SQL, requires explicit schema qualification for every physical relation while permitting qualified/unqualified and quoted/mixed-case column references, permits lexical CTE aliases, denies all functions and custom operator/cast paths, rejects every DDL statement, accepts TRUNCATE only for one table with default/CONTINUE IDENTITY and default/RESTRICT behavior, bounds every UPDATE/DELETE/TRUNCATE target with a locked `max_rows + 1` pre-probe before dispatch, and uses a `pg_catalog`-only search path.
- [ ] Confirm every accessed source, mutation target, and bounded same-schema FK peer is deterministically locked and then revalidated as an ordinary nonpartition base table; relation column/index/constraint catalogs use cap+1 reads; current/ancestor ownership and trigger/rule, view/foreign/partition/inheritance, RLS/policy, custom-type/opclass, default/generated/CHECK/exclusion, expression/partial-index, and unsafe-FK indirect-effect paths are rejected with zero unauthorized changes.
- [ ] Confirm no approval can execute from a different agent/run/task or after a grant/policy/connection change forbids it.
- [ ] Confirm webhook ingress UUID derives from connector/connection/delivery and a post-publish/precommit retry creates one delivery, canonical event, and trigger.
- [ ] Confirm webhook readers test exact cap, cap+1, and one huge chunk before copy; provider clients stream up to 512 KiB before buffering and never follow redirects.
- [ ] Confirm SQL/parameter input caps preserve the complete approval digest through production-default gateway sanitization, and response/row/cell/source/time/AST/mutation-byte caps are constants with boundary tests; INSERT-SELECT/source-column assignments are rejected, repeated literal/parameter expansion is pre-bounded, resource GUC ordering (including string, replication, and index-plan controls), statement/lock/client timeout behavior, `max_rows` mutation rollback, Unicode-safe output names, production-default sanitizer-aware redaction/result budgeting, and the trusted fixed/direct-text type-specific base64 wrapper with unsupported/compressed variable-width rejection and one-row cursor fetch are proven through real PostgreSQL and a real default `ToolGateway` while remaining below its leaf/document limits.
- [ ] Confirm provider endpoints cannot redirect or target an unapproved private/metadata/Jhin database address.
- [ ] Confirm credentials and their individual JSON leaves are registered with redaction in API and worker processes.
- [ ] Confirm the connection detail/API names authorized agents and shows exact relevant capability/effect/scope with admin-only RBAC and workspace isolation.
- [ ] Confirm the acceptance stack starts from literal project `jhin-phase9-acceptance` with fresh named volumes and is torn down by the same literal scoped target.
- [ ] Confirm the automated rendered-production-Compose check rejects every fake provider, fixture database/credential, and dev endpoint allowlist.
- [ ] Confirm `docker/python.Dockerfile` cleanly resolves every workspace manifest consumed by service packages.
- [ ] Confirm commit `d8d1055` migrated the tracked canonical plan and the repository-wide `git grep` negative assertion finds no legacy branding in any tracked file.
- [ ] Confirm Task 0B committed this plan before Task 1 implementation commits.
- [ ] Confirm the separately assembled untracked production-plan reference path remains untouched and unstaged.
