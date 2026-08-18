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
- Vercel and Supabase tools require connection-scoped grants. A production-impacting Vercel action and every Supabase DML/DDL mutation use `RiskLevel.ELEVATED` or `RiskLevel.DESTRUCTIVE` and set `supports_approval=True`. Balanced is the default and requires approval for both; Autonomous may auto-run the elevated preview action but still parks destructive actions; only an explicit custom `AUTO` rule may auto-run a destructive action. `supports_approval` is not itself a non-overridable approval floor.
- A human approval is permission to retry authorization, not a frozen authorization result: resume must bind to the original workspace/agent/run/task/tool definition/connection credential revision and re-evaluate current grants, required scopes, policy rules, connection state, and tool-specific validators.
- Every structured tool call receives a versioned deterministic internal invocation UUID derived from its durable run ID, durable step index, and zero-based tool-call ordinal—never from a retry-variant model/provider call ID, arguments, credentials, or secret material. A bounded provider call ID may be integrity-bound to the persisted request, but it is not retry identity; persisted transcript tool-call/result pairing uses the canonical internal UUID. Regardless of whether policy returns `ALLOW` immediately or resumes an approval, an atomically committed `executing` claim precedes external side effects; terminal outcomes replay on activity retry, and an ambiguous post-claim failure becomes `execution_unknown` and is never automatically executed again. Transmit the internal UUID as a provider idempotency key only on an endpoint whose current official contract explicitly supports idempotency.
- Vercel webhook secrets use the provider's value and HMAC-SHA1 protocol. GitHub and Linear retain generated secrets and their existing algorithms.
- Webhook bodies are capped at `1_048_576` bytes before parsing or provider verification. Dotted provider event names are represented as individually validated NATS subject tokens.
- HTTP endpoints default to the official HTTPS origins. Non-official origins and database hosts work only when an operator explicitly adds their exact origin/host to the documented dev/self-host allowlists; localhost, link-local, metadata, private-IP, and redirect-based bypasses otherwise fail closed.
- A Supabase database connection uses a custom low-privilege login, never `postgres`, a superuser, or a `BYPASSRLS` role. Hosted connections require TLS. Jhin's application database is never accepted as the target.
- Supabase SQL accepts exactly one PostgreSQL statement, validates the entire SQLGlot AST against the fixed-risk tool selected by the model, executes the original parameterized SQL, and rejects unknown syntax rather than guessing its risk.
- Database reads use a read-only transaction, `statement_cache_size=0`, quoted `search_path`, server-side cursor fetch of `max_rows + 1`, server and client timeouts, and per-cell plus total-result byte caps.
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
- Modify: `packages/workflows/src/jhin_workflows/agent_task/shared.py`
- Modify: `packages/workflows/src/jhin_workflows/agent_task/workflows.py`
- Modify: `packages/workflows/src/jhin_workflows/delegated_task/shared.py`
- Modify: `services/agent_worker/src/jhin_agent_worker/activities.py`
- Modify: `apps/api/src/jhin_api/connections/service.py`
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
- Test: `packages/workflows/tests/test_agent_task_delegation.py`
- Test: `services/agent_worker/tests/test_activities.py`
- Test: `services/agent_worker/tests/test_approval_activity.py`
- Test: `services/agent_worker/tests/test_delegation_activities.py`
- Test: `apps/api/tests/test_connections_unit.py`
- Test: `apps/api/tests/test_policy_unit.py`
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

Implement `test_approval_staging_rejects_non_lossless_sanitized_input` twice through the existing gateway fixture: once with a schema-valid string of `MAX_STRING_CHARS + 1` characters, and once with a schema-valid string containing a value pre-registered in `get_redactor()`. In both cases assert `outcome.status == "denied"`, `outcome.decision_code == "approval_input_not_lossless"`, the executor effect list is empty, and a query for pending `Approval` rows returns zero.

Add an API service test in `apps/api/tests/test_policy_unit.py` that creates two `allow` grants for the same capability/effect with distinct `scope_json`, then proves an exact duplicate still returns `409`. Use two PostgreSQL sessions to prove the target-agent row lock serializes racing exact duplicates.

- [ ] **Step 5: Run the authorization tests to verify RED**

Run:

```bash
uv run pytest packages/tools/tests/test_invocation.py packages/tools/tests/test_gateway.py packages/tools/tests/test_gateway_concurrency.py services/agent_worker/tests/test_activities.py apps/api/tests/test_policy_unit.py -q
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

For every call, first look up/lock the deterministic row. Require the existing workspace/run/agent/tool/connection and exact validated JSON input to match; otherwise audit and return `invocation_mismatch`. Replay terminal rows and the same pending approval without creating a row or side effect. For a newly immediate `ALLOW`, atomically insert the row as `executing`, add the requested/claimed audits, and commit before calling the executor. Approval staging inserts the same deterministic row as `pending_approval`; approval resolution atomically changes it to `executing` and commits before execution. A duplicate observer of `executing` must not invoke the executor; after a retry/crash it records or returns `execution_unknown`. Commit a definitive terminal result before returning it to the activity. A failure proven to occur before any external dispatch may be `failed`; a timeout, connection loss, process crash, or database-commit ambiguity after dispatch is `execution_unknown`. Neither state is automatically executed again.

After the gateway returns, the activity writes the canonical transcript/run-event bundle under the internal invocation UUID and checks for an existing bundle first. Thus a crash after the gateway's terminal commit but before the outer bundle commit is repaired on activity retry without a second executor call or duplicate messages/events. Extend `GatewayStatus`/`ToolCallStatus` and observation handling for `executing` and `execution_unknown`; an unknown result is committed and then fails the step non-retryably so the workflow cannot advance to a new model step and repeat the uncertain mutation. Carry the canonical invocation ID through approval and delegated-result stitching. Do not add a migration because the existing `ToolCall.id` UUID and string status columns carry the new protocol.

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

- [ ] **Step 7: Run focused regressions and commit**

Run:

```bash
uv run pytest packages/policy/tests packages/tools/tests packages/secrets/tests packages/connectors/tests/test_execution.py packages/workflows/tests apps/api/tests/test_connections_unit.py apps/api/tests/test_policy_unit.py services/agent_worker/tests -q
uv run pytest -m integration tests/integration/test_phase9_authorization.py -v
uv run ruff check packages/domain packages/policy packages/tools packages/secrets packages/connectors packages/workflows apps/api services/agent_worker
uv run mypy
git diff --check
```

Expected: PASS, including immediate-policy crash/race/replay/unknown behavior, revoke-after-approval, wrong-context, definition/credential drift, lossless-input, required-scope, longest-first leaf redaction, shared atomic claims, canonical transcript repair, and serialized scoped-grant cases.

Commit:

```bash
git add packages/domain/src/jhin_domain/enums.py packages/policy/src/jhin_policy/capabilities.py packages/policy/src/jhin_policy/evaluator.py packages/policy/tests/test_evaluator.py packages/tools/src/jhin_tools/__init__.py packages/tools/src/jhin_tools/builtin.py packages/tools/src/jhin_tools/invocation.py packages/tools/src/jhin_tools/gateway.py packages/tools/tests/test_invocation.py packages/tools/tests/test_gateway.py packages/tools/tests/test_gateway_concurrency.py packages/secrets/src/jhin_secrets/__init__.py packages/secrets/src/jhin_secrets/material.py packages/secrets/src/jhin_secrets/store.py packages/secrets/src/jhin_secrets/redaction.py packages/secrets/tests/test_store.py packages/secrets/tests/test_redaction.py packages/connectors/src/jhin_connectors/execution.py packages/connectors/tests/test_execution.py packages/workflows/src/jhin_workflows/agent_task/shared.py packages/workflows/src/jhin_workflows/agent_task/workflows.py packages/workflows/src/jhin_workflows/delegated_task/shared.py packages/workflows/tests/test_agent_task_delegation.py apps/api/src/jhin_api/connections/service.py apps/api/src/jhin_api/policy/schemas.py apps/api/src/jhin_api/policy/router.py apps/api/src/jhin_api/policy/service.py apps/api/tests/test_connections_unit.py apps/api/tests/test_policy_unit.py services/agent_worker/src/jhin_agent_worker/activities.py services/agent_worker/tests/test_activities.py services/agent_worker/tests/test_approval_activity.py services/agent_worker/tests/test_delegation_activities.py tests/integration/test_phase9_authorization.py docs/superpowers/plans/2026-08-17-phase-9-vercel-supabase.md
git commit -m "fix: harden scoped tool approval authorization"
```

### Task 2: Add typed connector settings, provider-supplied webhooks, body caps, and endpoint policy

**Files:**
- Create: `packages/connectors/src/jhin_connectors/endpoints.py`
- Create: `packages/connectors/src/jhin_connectors/http_client.py`
- Modify: `packages/connectors/src/jhin_connectors/manifest.py`
- Modify: `packages/connectors/src/jhin_connectors/base.py`
- Modify: `packages/connectors/src/jhin_connectors/github/manifest.py`
- Modify: `packages/connectors/src/jhin_connectors/linear/manifest.py`
- Modify: `packages/connectors/src/jhin_connectors/__init__.py`
- Modify: `packages/events/src/jhin_events/subjects.py`
- Modify: `apps/api/src/jhin_api/connections/schemas.py`
- Modify: `apps/api/src/jhin_api/connections/service.py`
- Modify: `apps/api/src/jhin_api/connections/router.py`
- Modify: `apps/api/src/jhin_api/webhooks/router.py`
- Modify: `apps/api/src/jhin_api/webhooks/service.py`
- Test: `packages/connectors/tests/test_manifest_registry.py`
- Test: `packages/connectors/tests/test_endpoints.py`
- Test: `packages/connectors/tests/test_http_client.py`
- Test: `packages/events/tests/test_subjects.py`
- Test: `apps/api/tests/test_connections_unit.py`
- Test: `apps/api/tests/test_webhooks_unit.py`

**Interfaces:**
- Consumes: `ConnectorManifest`, `ConfigFieldSpec`, `Connector.verify_connection`, generic connection create/rotate, and generic webhook ingress.
- Produces: `ConfigFieldKind = Literal["text", "integer", "boolean", "string_list"]`; auth-specific fields/defaults/bounds; `WebhookSecretMode = Literal["none", "generated", "provider_supplied"]`; normalized settings; provider-secret write-only endpoint; webhook configured status; exact outbound target validation; shared redirect-free streaming provider HTTP with a 512 KiB cap; dotted ingress events; deterministic ingress event IDs; bounded webhook body reader.

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
uv run pytest packages/connectors/tests/test_manifest_registry.py packages/connectors/tests/test_endpoints.py packages/connectors/tests/test_http_client.py packages/events/tests/test_subjects.py apps/api/tests/test_connections_unit.py apps/api/tests/test_webhooks_unit.py -q
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
uv run pytest packages/connectors/tests/test_manifest_registry.py packages/connectors/tests/test_endpoints.py packages/connectors/tests/test_http_client.py packages/events/tests/test_subjects.py apps/api/tests/test_connections_unit.py apps/api/tests/test_webhooks_unit.py -q
uv run ruff check packages/connectors packages/events apps/api
uv run mypy
git diff --check
```

Expected: PASS with GitHub/Linear behavior preserved, every provider response bounded while streaming, redirects disabled, and crash/retry ingress IDs stable.

Commit:

```bash
git add packages/connectors packages/events/src/jhin_events/subjects.py packages/events/tests/test_subjects.py apps/api/src/jhin_api/connections apps/api/src/jhin_api/webhooks apps/api/tests/test_connections_unit.py apps/api/tests/test_webhooks_unit.py
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
- Create: `packages/connectors/src/jhin_connectors/vercel/webhook.py`
- Modify: `packages/connectors/src/jhin_connectors/vercel/manifest.py`
- Modify: `packages/connectors/src/jhin_connectors/vercel/connector.py`
- Modify: `packages/connectors/src/jhin_connectors/testing/fake_vercel.py`
- Test: `packages/connectors/tests/vercel/test_webhook.py`
- Test: `apps/api/tests/test_webhooks_unit.py`
- Test: `services/event_worker/tests/test_normalizer.py`

**Interfaces:**
- Consumes: the Vercel connector from Task 3, provider-supplied webhook secret storage, bounded raw-body ingress, Task 2's deterministic ingress UUID, dotted subjects, `RawWebhookEvent`, `NormalizedEvent`, and the event worker's generic deterministic normalizer.
- Produces: constant-time Vercel signature verification and fixed-field, retry-stable canonical deployment events for `deployment.created`, `deployment.ready`, `deployment.error`, `deployment.canceled`, and `deployment.promoted`.

- [ ] **Step 1: Write failing signature and normalization tests**

First assert the completed manifest changes from `none` to `provider_supplied`, declares `sha1`, advertises the five provider/canonical event names, and returns setup metadata with no Jhin-generated secret. Then use raw JSON bytes, a known provider secret, and `hmac.new(secret, body, hashlib.sha1).hexdigest()`. Assert missing/malformed/wrong `x-vercel-signature` returns `WebhookVerificationError`; correct signature yields root-level `id` as `delivery_id` and root-level `type` as `event`. Reject missing/oversized IDs before payload normalization.

For every supported event assert one canonical event:

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
uv run pytest packages/connectors/tests/vercel/test_webhook.py apps/api/tests/test_webhooks_unit.py services/event_worker/tests/test_normalizer.py -q
```

Expected: FAIL because Vercel has no parser/normalizer.

- [ ] **Step 3: Implement HMAC-SHA1 parsing and fixed-field normalization**

Verify the exact raw bytes before `json.loads`, compare lowercase hex digests with `hmac.compare_digest`, require an object root, and accept only bounded string `id`/`type`. Do not derive a delivery identifier from timestamps or mutable deployment fields.

Normalize through small extraction helpers that tolerate provider shape changes while emitting only the ten allowed fields shown in Step 1. Do not copy `data` wholesale. Wire `parse_webhook` and `normalize_event` through `VercelConnector` and set `WEBHOOK_EVENTS`/canonical events in the manifest. Preserve Task 2's deterministic ingress ID unchanged; the existing event worker continues deriving canonical UUIDv5 IDs from that ingress ID, so publish/commit crash recovery remains idempotent across both streams.

Extend the fake with an admin webhook emitter that signs the exact bytes using a supplied test secret, posts to a caller-provided Jhin callback URL only in dev tests, and reports the provider response without logging the secret.

- [ ] **Step 4: Run focused tests and commit**

Run:

```bash
uv run pytest packages/connectors/tests/vercel/test_webhook.py apps/api/tests/test_webhooks_unit.py services/event_worker/tests/test_normalizer.py packages/events/tests/test_subjects.py -q
uv run ruff check packages/connectors apps/api services/event_worker packages/events
uv run mypy
git diff --check
```

Expected: PASS for valid, invalid, duplicate, oversized, dotted, unknown, normalized, and publish-before-commit crash/retry webhook cases, with exactly one canonical event and trigger.

Commit:

```bash
git add packages/connectors/src/jhin_connectors/vercel packages/connectors/src/jhin_connectors/testing/fake_vercel.py packages/connectors/tests/vercel/test_webhook.py apps/api/tests/test_webhooks_unit.py services/event_worker/tests/test_normalizer.py
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
- Modify: `packages/connectors/pyproject.toml`
- Modify: `uv.lock`
- Modify: `packages/connectors/src/jhin_connectors/registry.py`
- Modify: `packages/connectors/src/jhin_connectors/testing/__init__.py`
- Test: `packages/connectors/tests/supabase/test_manifest.py`
- Test: `packages/connectors/tests/supabase/test_management_tools.py`
- Test: `packages/connectors/tests/supabase/test_database_verify.py`
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

The `management_token` settings are `project_ref` (required text) and `base_url` (text, default `https://api.supabase.com`). The `postgres` settings are `project_ref`, `allowed_schemas` (string list, default `public`), `allow_writes` (boolean, default false), `allow_ddl` (boolean, default false), `statement_timeout_ms` (integer, default 5000, 250..30000), `lock_timeout_ms` (integer, default 1000, 100..5000), `max_rows` (integer, default 200, 1..1000), `max_cell_bytes` (integer, default 16384, 256..65536), and `max_result_bytes` (integer, default 262144, 4096..1048576).

Tests must prove every Management API executor rejects a `postgres` connection before network I/O, every future database executor rejects `management_token`, fields for the other auth type are rejected rather than silently stored, empty/duplicate/system schemas fail validation, and `allow_ddl=True` requires `allow_writes=True`.

Add `asyncpg>=0.31,<1` as a direct connector dependency and refresh `uv.lock`. Add a database-verification protocol test proving the `postgres` auth path validates the target, connects with `timeout=5` and `statement_cache_size=0`, checks `current_user`/superuser/`BYPASSRLS`, rejects unsafe roles without returning the DSN, and closes in `finally`. This makes the connector complete and healthy before Task 6 adds any SQL execution tools.

Run after editing the dependency:

```bash
uv lock
uv sync --frozen
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

Add tests for project-ref binding, auth failure, bounded project/function output, log time range at most 24 hours, log `limit <= 200`, a fixed source enum, a fixed projected-field query, no caller-provided log SQL, no `logs.all`, fixed `DESTRUCTIVE` risk plus approval support for both function mutations, shared streaming-response cap/redirect behavior, no undocumented idempotency header/field, deterministic one-shot `/_fault` behavior after a deploy/delete side effect, and zero side effects on validation failure.

Function deployment input is an in-memory list of at most eight `{path, content}` files. Paths are POSIX-relative, reject absolute paths, `.`/`..`, backslashes, control characters, and duplicates. Each content string is at most 6 KiB and total serialized source is at most 24 KiB, keeping approval persistence lossless under gateway limits. Require a bounded slug, `entrypoint_path` that names one supplied file, and explicit `verify_jwt`. Deletion accepts only the slug.

- [ ] **Step 3: Run Supabase Management tests to verify RED**

Run:

```bash
uv run pytest packages/connectors/tests/supabase/test_manifest.py packages/connectors/tests/supabase/test_management_tools.py packages/connectors/tests/supabase/test_database_verify.py packages/connectors/tests/test_manifest_registry.py -q
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

The client uses bearer auth and the validated official/allowlisted origin, constructs requests with a 5-second connect/20-second total timeout, and routes every response through Task 2's redirect-free streaming 512 KiB helper. Build the unified log ClickHouse query internally from a closed source enum, validated ISO start/end, an optional 500-character text filter escaped by a dedicated ClickHouse string-literal function, and `limit`; select only timestamp, source, event message, path, status code, and method. Snapshot the exact generated query in unit tests, including quotes/backslashes in the text filter. Never accept arbitrary log SQL.

Deploy with official multipart `POST /v1/projects/{ref}/functions/deploy?slug={function_slug}`, JSON metadata, and in-memory files. Delete with the official slug path. Return only id, slug, name, status, version, timestamps, `verify_jwt`, and entrypoint path. These selected Management API endpoints do not currently document an idempotency header or request field, so retain the tool-call ID as Jhin's internal invocation ID and do not invent an on-wire key; a post-side-effect transport ambiguity becomes `execution_unknown` under Task 1 and is not automatically retried.

`SupabaseConnector.verify_connection` switches strictly on auth type: `management_token` calls project metadata; `postgres` calls `verify_database_connection` in `database_client.py`. The verifier applies `validate_postgres_target(..., app_database_url=os.getenv("DATABASE_URL"))`, connects without a statement cache, reads `current_user` and the current row's `rolsuper`, `rolbypassrls`, `rolcreatedb`, `rolcreaterole`, and `rolreplication` flags from `pg_catalog.pg_roles`, rejects `postgres` or any privileged flag, and always closes. Add the factory to `DEFAULT_CONNECTORS` now; its database tool tuple stays empty until Task 6.

- [ ] **Step 5: Run focused tests and commit**

Run:

```bash
uv run pytest packages/connectors/tests/supabase/test_manifest.py packages/connectors/tests/supabase/test_management_tools.py packages/connectors/tests/supabase/test_database_verify.py packages/connectors/tests/test_manifest_registry.py packages/policy/tests/test_evaluator.py -q
uv run ruff check packages/connectors
uv run mypy
git diff --check
```

Expected: PASS, including plane separation, endpoint/ref binding, current logs endpoint, source-path safety, destructive function-mutation risks, scoped approvals, shared HTTP bounds, and output allowlists.

Commit:

```bash
git add packages/connectors/pyproject.toml packages/connectors/src/jhin_connectors/supabase packages/connectors/src/jhin_connectors/testing/fake_supabase.py packages/connectors/src/jhin_connectors/testing/__init__.py packages/connectors/src/jhin_connectors/registry.py packages/connectors/tests/supabase packages/connectors/tests/test_manifest_registry.py uv.lock
git commit -m "feat: add Supabase management plane tools"
```

### Task 6: Implement fail-closed Supabase SQL policy and bounded database execution

**Files:**
- Create: `packages/connectors/src/jhin_connectors/supabase/sql_policy.py`
- Modify: `packages/connectors/src/jhin_connectors/supabase/database_client.py`
- Create: `packages/connectors/src/jhin_connectors/supabase/database_tools.py`
- Modify: `packages/connectors/src/jhin_connectors/supabase/schemas.py`
- Modify: `packages/connectors/src/jhin_connectors/supabase/connector.py`
- Modify: `packages/connectors/pyproject.toml`
- Modify: `uv.lock`
- Create: `tests/fixtures/supabase/init.sql`
- Modify: `compose.dev.yaml`
- Modify: `.env.example`
- Test: `packages/connectors/tests/supabase/test_sql_policy.py`
- Test: `packages/connectors/tests/supabase/test_database_tools.py`
- Test: `packages/connectors/tests/supabase/test_database_integration.py`

**Interfaces:**
- Consumes: the latest resolved encrypted `database_url` and public config on every invocation, typed Postgres settings, endpoint policy, fixed tool risks, required grant scopes, SQLGlot PostgreSQL ASTs, asyncpg, and Task 1's mutation claim lifecycle.
- Produces: `classify_and_validate_sql(sql, *, expected, requested_schema) -> ValidatedSql`; per-execution target/TLS/project/role verification; bounded parameterized execution; separate read/write/destructive/DDL tools; an isolated real PostgreSQL fixture and integration gate.

- [ ] **Step 1: Add the pinned SQL parser dependency**

Keep Task 5's direct asyncpg dependency and add the SQL parser dependency, then refresh the lock:

```toml
"sqlglot>=30.13,<31",
```

Run:

```bash
uv lock
uv sync --frozen
```

Expected: lock succeeds with SQLGlot 30.x and asyncpg remains a direct connector dependency. Do not add `pglast` or another GPL-licensed parser.

- [ ] **Step 2: Write the SQL policy decision table as failing parameterized tests**

Define `SqlClass = Literal["read", "write", "destructive", "ddl"]`. Cover at least this matrix:

```text
read allow:        SELECT 1, SELECT FROM public.widgets,
                   WITH named_cte AS (SELECT FROM public.widgets)
                   SELECT FROM named_cte, qualified UNION branches
read deny:         SELECT INTO, FOR UPDATE/SHARE, data-changing CTE,
                   EXPLAIN ANALYZE, COPY, CALL, DO, PREPARE, EXECUTE,
                   SET/RESET, transaction/session commands
write allow:       INSERT INTO public.widgets, UPDATE public.widgets with WHERE
write deny:        UPDATE without WHERE, DELETE, TRUNCATE, any DDL, RETURNING
destructive allow: DELETE FROM public.widgets,
                   UPDATE public.widgets without WHERE, TRUNCATE public.widgets
destructive deny:  safe UPDATE, INSERT, any DDL, RETURNING
ddl allow:         one narrow CREATE or DROP TABLE, INDEX, or VIEW only
ddl deny:          every ALTER plus schema/database/role/function/trigger/
                   extension/policy/type DDL; every DROP ... CASCADE;
                   unsupported CREATE properties/forms
all deny:          empty SQL, comments only, two statements, unknown command,
                   cross-schema references, system catalogs, disallowed schema,
                   every function call, AST whose actual class differs from
                   the selected tool
```

Test quoted identifiers, mixed case, nested subqueries, lexical CTE aliases, `INSERT INTO public.widgets SELECT 1`, comments containing semicolons, and bypass attempts hidden below the root node. Every physical table/view/source/target reference must be explicitly qualified with exactly `requested_schema`; `SELECT * FROM widgets`, `INSERT INTO widgets`, and an unqualified physical name hidden in a subquery or DDL source all fail. An unqualified `named_cte` is accepted only when it resolves to a CTE alias in that query scope. `pg_catalog`, `information_schema`, and every other schema are denied to agent SQL even though the executor's internal role-verification query uses `pg_catalog`.

The Phase 9 CREATE allowlist is deliberately small: ordinary permanent `CREATE TABLE requested_schema.name (...)`, `CREATE [UNIQUE] INDEX name ON requested_schema.table (simple_columns)`, and `CREATE VIEW requested_schema.name WITH (security_invoker=true) AS <validated read query>`. PostgreSQL requires a new index name to be unqualified and creates it in its explicitly qualified parent table's schema; treat that declaration name as part of CREATE syntax, not as an unqualified lookup. Deny `TEMP`/`TEMPORARY`, `UNLOGGED`, `FOREIGN`, `MATERIALIZED`, `OR REPLACE`, `CONCURRENTLY`, `TABLESPACE`, `LIKE`, `INHERITS`, partition clauses, CTAS, expression/partial indexes, custom access methods/operator classes, unrecognized properties, and any other CREATE form. DROP accepts one explicitly qualified TABLE/INDEX/VIEW with default/`RESTRICT` behavior; every `CASCADE` and multi-object drop fails. Every `ALTER` fails.

Phase 9 defaults every SQL function/aggregate call to deny; it does not try to infer volatility from a name, resolve overloads, or trust a database catalog that a privileged operator could change. Add explicit bypass tests for `pg_read_file`, `dblink`, `setval`, `nextval`, `pg_sleep`, an unqualified custom function, a `requested_schema.custom_function`, and a callable `SECURITY DEFINER` function. Basic projections, predicates, arithmetic, the restricted literal casts below, CASE, and CTEs remain available without functions. A later phase may introduce a versioned exact built-in function allowlist only with parser and real-PostgreSQL tests.

Deny PostgreSQL's explicitly schema-qualified `OPERATOR(schema.operator)` syntax and every custom/operator-class reference. Explicit casts are accepted only when the operand is a scalar literal (or another already-validated literal cast) and both the inferred source and exact unqualified target are in a fixed built-in whitelist: `boolean`, `smallint`, `integer`, `bigint`, `numeric`, `real`, `double precision`, `text`, `varchar`, `date`, `timestamp`, `timestamptz`, `uuid`, `json`, and `jsonb`. Reject casts of columns/parameters, schema-qualified/custom/domain/array types, and CREATE columns outside the same whitelist. Add bypass tests for `OPERATOR(public.+)`, a custom operator, `literal::public.custom_type`, a custom/domain cast, and a column cast that could dispatch through a user-defined cast function.

- [ ] **Step 3: Run SQL policy tests to verify RED**

Run:

```bash
uv run pytest packages/connectors/tests/supabase/test_sql_policy.py -q
```

Expected: FAIL because `sql_policy.py` is absent.

- [ ] **Step 4: Implement entire-AST validation**

Parse with `sqlglot.parse(sql, read="postgres")`, require exactly one non-empty expression, classify by concrete root and nested nodes, and walk the entire AST. Do not authorize by the first keyword or a regex. Treat parser fallback `Command`, unrecognized nodes, and unsupported syntax as `SqlPolicyError("unsupported SQL")`.

Return immutable metadata:

```python
@dataclass(frozen=True)
class ValidatedSql:
    sql_class: SqlClass
    schemas: frozenset[str]
    statement_type: str
```

Build a lexical set of CTE aliases while walking each query scope; only a matching unqualified CTE reference is exempt from physical-relation qualification. Validate every source and target node, including nested queries and DDL subtrees. Reject every SQLGlot function/aggregate/anonymous-function node before execution, including qualified and nested calls; do not consult `pg_proc` to widen access. Reject schema-qualified operator nodes, nonliteral casts, and any cast/CREATE column type outside Step 2's fixed built-in whitelist. Reject any CREATE property/node not in Step 2's fixed allowlist and every DROP with `CASCADE`. Execute the original SQL string with the original validated positional-parameter sequence, never string interpolation and never SQLGlot's re-rendered text. Reject `RETURNING` for all mutation tools so mutation output is only an affected-row count and cannot bypass result caps. Operators use reviewed migrations outside the agent SQL path for every unsupported DDL operation.

- [ ] **Step 5: Write failing database connection/executor tests**

Use an asyncpg protocol fake for unit-level call ordering and assert:

```python
asyncpg.connect(
    dsn=database_url,
    timeout=5,
    statement_cache_size=0,
    ssl=hosted_ssl_context,
)
```

The verifier and every executor query the same connection for `current_user`, `rolsuper`, `rolbypassrls`, `rolcreatedb`, `rolcreaterole`, and `rolreplication`, rejecting `postgres`, a missing role row, or any privileged flag. In the same round trip/transaction, walk the transitive membership closure through `pg_catalog.pg_auth_members` and `pg_catalog.pg_roles` with a cycle-safe recursive CTE. Reject membership in any built-in `pg_%` role—including `pg_read_all_data`, `pg_write_all_data`, `pg_read_server_files`, `pg_write_server_files`, `pg_execute_server_program`, `pg_signal_backend`, and `pg_monitor`—and any custom ancestor carrying superuser, BYPASSRLS, CREATEDB, CREATEROLE, or REPLICATION. Return/log only a generic credential-safe `database_role_not_least_privilege` code; never expose the login or inherited role name.

Execution begins a transaction only after this check; reads set it read-only. Every call issues transaction-local statement timeout, lock timeout, and a safely double-quoted search path with `pg_catalog` first and only the requested schema second. The search path is defense in depth for built-in function/operator/type resolution, never permission to accept an unqualified physical relation. Read execution uses a prepared statement plus server cursor and fetches at most `max_rows + 1`.

Cover server timeout, client `asyncio.timeout`, cancellation, connection failure, rollback, close-in-finally, oversized cells, oversized total result, and credential-safe errors. Add call-order tests proving every invocation resolves the latest connection, re-runs `validate_postgres_target` with the current `database_url`, `project_ref`, and process `DATABASE_URL`, builds current hosted TLS settings, connects, and re-reads current direct flags plus recursive memberships before any user SQL. Rotate the credential after successful verification, change public config (`project_ref`, `allowed_schemas`, write/DDL flags, timeout bounds), change the returned role to each privileged case, and add/remove a dangerous direct or transitive membership; old assumptions must fail closed with zero SQL side effects. Assert no DSN/password/login/member-role name appears in returned errors or captured logs.

For every `write`/`destructive` statement, add a live mutation-target preflight on the same checked connection and inside the execution transaction. Acquire a safely quoted `ROW EXCLUSIVE` table lock first so an ALTER/TRIGGER race cannot invalidate the check. Query catalogs with relation names as parameters and accept only one ordinary, non-partition table (`relkind='r'`, `relispartition=false`) in exactly `requested_schema`. Reject views/materialized views, partitioned tables/partitions, foreign tables, inheritance parents/children, user-defined triggers, nontrivial rewrite rules, and any inbound foreign key whose UPDATE/DELETE action is CASCADE, SET NULL, or SET DEFAULT (reject all such actions, necessarily including any crossing the allowed schema). Unit protocol tests must prove the catalog checks and lock occur before user SQL and a failed preflight rolls back/closes without invoking the prepared mutation.

- [ ] **Step 6: Run database tests to verify RED**

Run:

```bash
uv run pytest packages/connectors/tests/supabase/test_database_tools.py -q
```

Expected: FAIL because the database client/executors and live revalidation are absent.

- [ ] **Step 7: Add the isolated real PostgreSQL fixture service**

Create `tests/fixtures/supabase/init.sql` with fixture-only credentials and least-privilege roles:

```sql
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
CREATE ROLE jhin_reader LOGIN PASSWORD 'reader-pass' NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
CREATE ROLE jhin_writer LOGIN PASSWORD 'writer-pass' NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
CREATE TABLE public.widgets (id integer PRIMARY KEY, name text NOT NULL);
INSERT INTO public.widgets VALUES (1, 'alpha'), (2, 'beta'), (3, repeat('x', 20000));
CREATE SCHEMA private;
REVOKE ALL ON SCHEMA private FROM PUBLIC;
CREATE TABLE private.secrets (id integer PRIMARY KEY, value text NOT NULL);
INSERT INTO private.secrets VALUES (1, 'must-never-be-readable');
GRANT CONNECT ON DATABASE supabase_fixture TO jhin_reader, jhin_writer;
GRANT USAGE ON SCHEMA public TO jhin_reader, jhin_writer;
GRANT CREATE ON SCHEMA public TO jhin_writer;
GRANT SELECT ON public.widgets TO jhin_reader;
GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON public.widgets TO jhin_writer;
```

Define `fake-supabase-db` only in `compose.dev.yaml`:

```yaml
services:
  fake-supabase-db:
    image: postgres:17-alpine
    environment:
      POSTGRES_USER: postgres
      POSTGRES_PASSWORD: ${PHASE9_SUPABASE_FIXTURE_ADMIN_PASSWORD:-phase9-fixture-admin-only}
      POSTGRES_DB: supabase_fixture
    ports:
      - "127.0.0.1:${FAKE_SUPABASE_DB_DEV_PORT:-55433}:5432"
    volumes:
      - fake_supabase_data:/var/lib/postgresql/data
      - ./tests/fixtures/supabase/init.sql:/docker-entrypoint-initdb.d/10-init.sql:ro
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres -d supabase_fixture"]
    networks: [data]

volumes:
  fake_supabase_data:
```

Add exact `fake-supabase-db:5432` to `JHIN_CONNECTOR_ALLOWED_DB_HOSTS` for both `api` verification and `agent-worker` execution in the dev override only. Document `FAKE_SUPABASE_DB_DEV_PORT` and the fixture-only role names in `.env.example`; do not put this service, its volume, passwords, or the permissive host entry in `compose.yaml`.

- [ ] **Step 8: Write failing real-PostgreSQL integration tests**

Mark `test_database_integration.py` with `pytest.mark.integration` and read three explicit fixture DSNs from environment: reader, writer, and fixture admin. Exercise the real asyncpg connection/transaction/cursor path, not the protocol fake. Prove qualified reads and CTE aliases work; unqualified/cross-schema/catalog queries fail; row/cell/document caps truncate; one held lock with `lock_timeout_ms` lower than `statement_timeout_ms` raises the lock timeout and another with the bounds reversed raises the statement timeout; a failing mutation rolls back; a scoped approved write changes exactly one row; narrow CREATE/DROP TABLE, INDEX, and `security_invoker` VIEW work only through the DDL tool; and unsupported CREATE, every ALTER, and DROP CASCADE leave state unchanged. Create harmless custom and `SECURITY DEFINER` functions, a custom type/cast, and a custom operator as fixture admin, then prove those and `pg_read_file`/`dblink`/`setval` are rejected by policy before asyncpg sends user SQL. Assert the executor sets `search_path` to `pg_catalog` before the requested schema.

After a successful verification, rotate to the fixture `postgres` DSN and separately change `project_ref`, `allowed_schemas`, `allow_writes`, and `allow_ddl` before execution. Each invocation must use the new state, reject stale scope or the live privileged role, and cause zero SQL side effects. Also pass the fixture target as `app_database_url` once and prove the Jhin-database collision check rejects it without leaking either password.

Using the fixture-admin connection and restoring state in `finally`, grant `pg_read_all_data` directly to each low-privilege login, then test a deep transitive custom parent-role chain whose ancestor carries `CREATEDB`; execution must fail before user SQL even though the login's direct flags did not change. The recursive query remains cycle-safe even though PostgreSQL prevents creating a cyclic role grant through supported DDL. Revoke/drop each fixture role after the assertion and prove the returned error/logs contain only `database_role_not_least_privilege`, never any login or member-role name. Rotate membership after connection verification to prove the check is live on every execution, not cached.

Create isolated ordinary-table fixtures whose DML would otherwise have indirect effects: a user trigger calling a `SECURITY DEFINER` function that writes `private.secrets`, a nontrivial rewrite rule that writes a second table, and a cross-schema inbound foreign key with `ON UPDATE CASCADE` or `ON DELETE SET NULL`. Also create partitioned/partition, foreign-table, view/materialized-view, and inheritance parent/child targets. For each, invoke the matching write/destructive tool and assert preflight rejection before user SQL, unchanged target rows, and zero rows added/changed in every private or secondary relation. Include one plain `public` table without those features to prove the preflight still permits an approved bounded mutation.

- [ ] **Step 9: Start a fresh fixture volume and verify the real tests are RED**

Run against a dedicated Compose project so deleting its volume cannot affect a developer's normal stack:

```bash
export FAKE_SUPABASE_DB_DEV_PORT=65434
export JHIN_CONNECTOR_ALLOWED_DB_HOSTS=127.0.0.1:65434
export JHIN_PHASE9_DB_READER_DSN=postgresql://jhin_reader:reader-pass@127.0.0.1:65434/supabase_fixture
export JHIN_PHASE9_DB_WRITER_DSN=postgresql://jhin_writer:writer-pass@127.0.0.1:65434/supabase_fixture
export JHIN_PHASE9_DB_ADMIN_DSN=postgresql://postgres:phase9-fixture-admin-only@127.0.0.1:65434/supabase_fixture
docker compose -p jhin-phase9-dbtest -f compose.yaml -f compose.dev.yaml down --volumes --remove-orphans
docker compose -p jhin-phase9-dbtest -f compose.yaml -f compose.dev.yaml up -d --force-recreate fake-supabase-db
docker compose -p jhin-phase9-dbtest -f compose.yaml -f compose.dev.yaml ps fake-supabase-db
uv run pytest -m integration packages/connectors/tests/supabase/test_database_integration.py -q
```

Expected: the named fixture service is healthy with a newly initialized `fake_supabase_data` volume, then tests FAIL because the database tools are not implemented. Never run the `down --volumes` command without the exact `jhin-phase9-dbtest` project name.

- [ ] **Step 10: Implement four fixed-risk database tools with live checks**

Register:

```text
supabase.database.read          READ
supabase.database.write         ELEVATED + approval
supabase.database.destructive   DESTRUCTIVE + approval
supabase.database.ddl           DESTRUCTIVE + approval
```

Every input contains `connection_id`, `project_ref`, `schema`, `sql` capped at 7,000 characters, and at most 50 JSON-scalar positional parameters. Every definition requires grant scopes `connection_id`, `project_ref`, and `schema`. The executor requires configured `project_ref` equality and membership of `schema` in `allowed_schemas`; write/destructive additionally require `allow_writes=True`; DDL requires both `allow_writes=True` and `allow_ddl=True`.

On every invocation—including calls allowed immediately by Autonomous/custom policy and resumed Balanced approvals—resolve the connection again and perform this exact order before user SQL: require `auth_type="postgres"`; validate current input scope against current config; re-run `validate_postgres_target` on the current credential with current `project_ref`, current process `DATABASE_URL`, and hosted TLS rules; connect using current SSL settings; query and reject current privileged direct role flags plus the cycle-safe transitive membership closure on that same connection; then begin the bounded transaction. For every write/destructive DML call, acquire the target lock and complete the live ordinary-table/trigger/rule/partition/inheritance/foreign-key preflight inside that transaction before preparing or dispatching user SQL. Verification performed when the connection was created never substitutes for these live checks. Task 1's authorization digest rejects a parked approval after credential/config changes, but these executor checks remain mandatory defense in depth and protect non-parked calls too.

For reads, create a read-only transaction, prepare the original SQL, open a server-side cursor, fetch `max_rows + 1`, and return column names, at most `max_rows` rows, `row_count`, and `truncated`. Cap each encoded cell at `max_cell_bytes` with an explicit marker and stop the document at `max_result_bytes`; never fetch the whole result to compute truncation. For mutations, execute the original positional-parameter SQL, parse the command tag into `affected_rows`, and return no row data.

Use server settings:

```sql
SET LOCAL statement_timeout = '<validated integer>ms';
SET LOCAL lock_timeout = '<validated integer>ms';
SET LOCAL search_path TO pg_catalog, "<escaped schema>";
```

Identifiers are quoted by doubling `"`; config bounds make timeout literals numeric. Wrap connection plus transaction work in a client timeout slightly above `statement_timeout_ms`; rollback on every exception and close the connection in `finally`.

- [ ] **Step 11: Run unit, real-PostgreSQL, Compose, and quality gates; then commit**

Run:

```bash
uv run pytest packages/connectors/tests/supabase packages/policy/tests/test_evaluator.py packages/tools/tests/test_gateway.py -q
export FAKE_SUPABASE_DB_DEV_PORT=65434
export JHIN_CONNECTOR_ALLOWED_DB_HOSTS=127.0.0.1:65434
export JHIN_PHASE9_DB_READER_DSN=postgresql://jhin_reader:reader-pass@127.0.0.1:65434/supabase_fixture
export JHIN_PHASE9_DB_WRITER_DSN=postgresql://jhin_writer:writer-pass@127.0.0.1:65434/supabase_fixture
export JHIN_PHASE9_DB_ADMIN_DSN=postgresql://postgres:phase9-fixture-admin-only@127.0.0.1:65434/supabase_fixture
uv run pytest -m integration packages/connectors/tests/supabase/test_database_integration.py -q
docker compose -f compose.yaml -f compose.dev.yaml config --quiet
uv run ruff check packages/connectors
uv run ruff format --check packages/connectors
uv run mypy
git diff --check
docker compose -p jhin-phase9-dbtest -f compose.yaml -f compose.dev.yaml down --volumes --remove-orphans
```

Expected: PASS across the SQL matrix, explicit physical-relation qualification, CTE handling, fixed risks, opt-in writes/DDL, per-call endpoint/config/direct-and-inherited-role checks, mutation-target indirect-effect preflights, real transactions, cursor limits, timeouts, output caps, and safe failures; only the literal `jhin-phase9-dbtest` fixture project and its volumes are then removed.

Commit:

```bash
git add packages/connectors/pyproject.toml packages/connectors/src/jhin_connectors/supabase packages/connectors/tests/supabase uv.lock tests/fixtures/supabase/init.sql compose.dev.yaml .env.example
git commit -m "feat: add bounded Supabase SQL execution"
```

### Task 7: Make connector setup and grant scopes data-driven; wire dev fakes and clean image builds

**Files:**
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
- Modify: `apps/web/tests/connectors-gallery.test.tsx`
- Modify: `apps/web/tests/wizard.test.ts`
- Modify: `apps/web/tests/org-tree-render.test.tsx`
- Modify: `docker/python.Dockerfile`
- Modify: `compose.dev.yaml`
- Modify: `.env.example`

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

Seed multiple workspaces, two agents, exact `connection_id` allow/deny grants, an unscoped grant, a grant for another connection, a wildcard capability, and a grant missing another required tool scope. The response includes only agents/grants relevant to this connection and installed connector capabilities, preserves each exact scope/effect, marks missing-required-scope grants ineligible, applies deny precedence when deriving `authorized_tool_names`, sorts deterministically, and never returns policy JSON, config secrets, credential IDs, or ciphertext. An admin receives `200`; a viewer receives `403`; a cross-workspace connection ID receives `404` with no agent/grant existence leak.

Assert:

```text
- Linear, Vercel, and Supabase appear exactly once as live connectors.
- Vercel provider-supplied setup shows the callback URL, SHA1 header guidance,
  and a password field/button to store the provider-generated secret.
- It never labels a Vercel secret as Jhin-generated or displays a stored value.
- GitHub/Linear retain the one-time generated-secret dialog.
- Connection detail shows "Webhook secret configured" from the boolean only.
- Supabase management-token and database forms show only their own settings.
- allow_writes and allow_ddl are explicit advanced toggles defaulting off.
- Connection detail lists authorized agent names and each exact relevant
  capability/effect/scope, labels incomplete or denied grants, shows a clear
  empty state, and never implies that a grant bypasses current approval policy.
```

- [ ] **Step 3: Run web tests to verify RED**

Run:

```bash
uv run pytest apps/api/tests/test_connections_unit.py -q
pnpm --filter jhin-web test -- connectors.test.ts connectors-gallery.test.tsx wizard.test.ts scope-editor.test.tsx connection-access-summary.test.tsx org-tree-render.test.tsx
```

Expected: FAIL because the access-summary route/component, scopes, config controls, and webhook setup do not exist or remain hard-coded.

- [ ] **Step 4: Implement the data-driven UI**

Mirror the new API fields in `ToolInfo`, `ConfigFieldSpec`, `ConnectorInfo`, `ConnectionInfo`, and `WebhookSetup`. `ScopeEditor` receives one `ToolInfo`, matching connections, values, and `onChange`; it renders labels for known keys (`connection_id`, `project_id`, `deployment_id`, `environment`, `project_ref`, `schema`, `function_slug`, repository/branch/CLI keys) and a safe text input for any future declared key. Required keys get visible “Required for this tool” copy and block submission.

Use `ScopeEditor` in both `ToolsAccessTab` and the agent wizard. Key picker options by `tool.name` and display both tool name and required capability; never merge different tools' scope keys into one grant editor. Keep per-tool scope state in the wizard, submit the selected tool's `required_capability`, and rely on Task 1's distinct-scope grants when two tools share a capability.

Render config controls by manifest kind: text/password as appropriate, bounded integer input, checkbox for booleans, and newline-separated editor normalized to a string list. Filter by `auth_types`; submit defaults so the operator sees the effective safety posture. Put `allow_writes` and `allow_ddl` under a clearly labeled Advanced database access disclosure.

For `provider_supplied`, show setup metadata after create and in connection detail, accept the provider-generated secret through the new `PUT /webhook-secret` route, then discard the local form state. Remove Linear/Vercel/Supabase from `UPCOMING_CONNECTORS`; leave HTTP as future work without assigning it to completed Phase 9.

Implement the access summary with one workspace-filtered query over `AgentCapabilityGrant` joined to `Agent`, then evaluate only the selected connector's registered tool definitions. A grant is relevant only when its exact scope contains this `connection_id` and its capability/pattern can match one of those tools; never treat an unscoped grant as connection authorization. For each matching tool, require every `required_grant_scope_key` to exist and apply existing capability/scope deny precedence before adding the tool name to `authorized_tool_names`. Return agents with relevant rows even when all rows are denied/incomplete so administrators can diagnose access, but set `authorized=True` only when at least one tool remains eligible. Render authorized agents first and place exact grants/scopes in an Advanced disclosure on the connection detail page.

- [ ] **Step 5: Add the two dev HTTP fakes and clean-build coverage**

Keep Task 6's `fake-supabase-db`, named volume, init mount, fixture SQL, DB allowlist, and port documentation unchanged. This task adds only these healthy HTTP services to `compose.dev.yaml`:

```text
fake-vercel       module jhin_connectors.testing.fake_vercel, host port 8094
fake-supabase     module jhin_connectors.testing.fake_supabase, host port 8095
```

Build each from the agent-worker image, run its module on container port 8080, bind only `127.0.0.1:${FAKE_VERCEL_DEV_PORT:-8094}` or `127.0.0.1:${FAKE_SUPABASE_DEV_PORT:-8095}`, give `/_state` health checks, and attach only to the dev `data` network. Set `JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS` for both API and agent-worker to exactly `http://fake-vercel:8080,http://fake-supabase:8080`; production `compose.yaml` receives neither service nor this allowlist. Document the two override ports in `.env.example` without real credentials.

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
pnpm --filter jhin-web lint
pnpm --filter jhin-web typecheck
pnpm --filter jhin-web test
docker compose -f compose.yaml -f compose.dev.yaml config --quiet
docker compose -f compose.yaml -f compose.dev.yaml build --no-cache api agent-worker workflow-worker event-worker web
docker compose -f compose.yaml -f compose.dev.yaml up -d fake-vercel fake-supabase fake-supabase-db
docker compose -f compose.yaml -f compose.dev.yaml ps fake-vercel fake-supabase fake-supabase-db
git diff --check
```

Expected: connection access-summary RBAC/query tests and web gates pass; Compose validates; clean images build without relying on stale layers; both new HTTP fakes and Task 6's existing fixture database become healthy.

- [ ] **Step 7: Commit UI and dev infrastructure**

```bash
git add apps/api/src/jhin_api/connections apps/api/tests/test_connections_unit.py apps/web docker/python.Dockerfile compose.dev.yaml .env.example
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
   and rejects private/cross-schema/unqualified/catalog/multi-statement/lock/
   session/data-changing SQL plus function, custom operator, and custom cast
   bypasses before execution.
8. Supabase database mutation/live recheck: read-only config, missing required
   scope, wrong SQL-risk tool, missing Balanced approval, and allow_writes/
   allow_ddl=false leave state unchanged; exact approved write and narrow DDL
   behave as declared. After verification, rotate the credential and separately
   change project/schema/write/DDL config or current role to postgres/superuser/
   BYPASSRLS/CREATEDB/CREATEROLE/REPLICATION, grant a dangerous built-in role,
   or add a privileged transitive membership; every execution rechecks live
   state and causes zero unauthorized SQL effects. Trigger/rule, partition/
   foreign/view/inheritance, and cascading-FK targets fail the same-transaction
   preflight with zero changes to private or secondary relations.
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
docker compose -p jhin-phase9-acceptance -f compose.yaml -f compose.dev.yaml up -d --force-recreate
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
List every tool, risk, required scope key, write/DDL opt-in, approval resume
reauthorization, Balanced/Autonomous/custom behavior, deterministic invocation
claims, execution_unknown reconciliation, and example least-privilege grants.

## Vercel webhooks
Document manual/provider-plan availability, callback URL, provider-generated
secret entry, x-vercel-signature HMAC-SHA1, event allowlist, body cap, and
deduplication. Do not imply every Vercel plan supports account webhooks.

## Supabase database role
Show creation of a custom NOSUPERUSER NOBYPASSRLS login, grants limited to
curated schemas/views, TLS DSN setup, allowlist settings, timeouts, row/cell/
result caps, explicit physical relation qualification, pg_catalog-first search
path, function/operator/cast denials, direct plus inherited role rejection,
mutation-target indirect-effect preflight, and the SQL decision table. State that
schema allowlisting does not classify sensitive cells; low-privilege roles and
curated views are the primary confidentiality boundary.

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
- [ ] Confirm SQL validation walks the complete AST, executes the original SQL, requires explicit schema qualification for every physical relation, permits lexical CTE aliases, denies all functions and custom operator/cast paths, rejects every ALTER/unsupported CREATE/DROP CASCADE, and uses `pg_catalog` first in search path.
- [ ] Confirm every write/destructive DML call locks and preflights its live target as one ordinary requested-schema table, rejecting trigger/rule, partition/foreign/view/inheritance, and cascading-FK indirect effects with zero changes outside the target.
- [ ] Confirm no approval can execute from a different agent/run/task or after a grant/policy/connection change forbids it.
- [ ] Confirm webhook ingress UUID derives from connector/connection/delivery and a post-publish/precommit retry creates one delivery, canonical event, and trigger.
- [ ] Confirm webhook readers test exact cap, cap+1, and one huge chunk before copy; provider clients stream up to 512 KiB before buffering and never follow redirects.
- [ ] Confirm response/row/cell/source/time caps are constants with boundary tests and statement-timeout/max-row behavior is proven against real PostgreSQL.
- [ ] Confirm provider endpoints cannot redirect or target an unapproved private/metadata/Jhin database address.
- [ ] Confirm credentials and their individual JSON leaves are registered with redaction in API and worker processes.
- [ ] Confirm the connection detail/API names authorized agents and shows exact relevant capability/effect/scope with admin-only RBAC and workspace isolation.
- [ ] Confirm the acceptance stack starts from literal project `jhin-phase9-acceptance` with fresh named volumes and is torn down by the same literal scoped target.
- [ ] Confirm the automated rendered-production-Compose check rejects every fake provider, fixture database/credential, and dev endpoint allowlist.
- [ ] Confirm `docker/python.Dockerfile` cleanly resolves every workspace manifest consumed by service packages.
- [ ] Confirm commit `d8d1055` migrated the tracked canonical plan and the repository-wide `git grep` negative assertion finds no legacy branding in any tracked file.
- [ ] Confirm Task 0B committed this plan before Task 1 implementation commits.
- [ ] Confirm the separately assembled untracked production-plan reference path remains untouched and unstaged.
