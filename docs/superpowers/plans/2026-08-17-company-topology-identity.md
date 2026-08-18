# Company Topology and Identity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a workspace grow from one independent agent into optional multi-team, collaborative, nested-company structures with safe public identities and avatars.

**Architecture:** Extend the existing `Agent` and `Team` records additively. Membership, reporting, collaboration, expertise, and authorization remain separate concepts. Public organization context is assembled through a bounded roster plus a policy-gated directory search. Avatar bytes live behind a `MediaStore` boundary, with PostgreSQL as the default store and an asynchronous provider-neutral generation contract.

**Tech Stack:** Python 3.13, SQLAlchemy 2, Alembic, PostgreSQL, FastAPI/Pydantic, Pillow, Temporal, pytest/httpx.

**Spec:** `docs/superpowers/specs/2026-08-17-jhin-ai-company-experience-design.md`

## Global Constraints

- Agents with no manager and no team remain valid and fully functional.
- Membership, reporting, and relationships never grant capabilities or data access.
- An agent may have many active team memberships but exactly zero or one primary membership.
- `Agent.team_id` remains the compatibility pointer and changes atomically with primary membership.
- Reporting lines remain acyclic and workspace-local.
- `close_collaborator` is symmetric through canonical ID ordering; `advisor` and `preferred_reviewer` are directed.
- Directory results contain public identity only: never prompts, grants, credentials, private memory, or private conversations.
- Avatar processing never fetches remote URLs and never accepts SVG or animated input.
- Every new graph edge uses workspace-aware database constraints where PostgreSQL can enforce them; API checks are defense in depth.
- Every create/update/delete operation emits the existing audit envelope.

---

### Task 1: Add additive company identity tables and constraints

**Files:**
- Modify: `packages/db/src/jhin_db/models/org.py`
- Modify: `packages/db/src/jhin_db/models/__init__.py`
- Create: `packages/db/src/jhin_db/alembic/versions/20260817_0014_company_identity.py`
- Create: `packages/db/tests/test_company_identity_models.py`
- Create: `packages/db/tests/test_migration_graph.py`

**Interfaces:**
- `AgentTeamMembership(workspace_id, agent_id, team_id, is_primary, role_label, joined_at, left_at)`
- `AgentRelationship(workspace_id, source_agent_id, target_agent_id, kind, purpose, status, created_at, updated_at)`
- New `Agent` fields: `public_purpose`, `expertise_json`, `discoverability`, and `availability`.

- [ ] **Step 1: Write failing model and migration tests**

Cover teamless agents, multiple memberships, one-primary uniqueness, workspace-aware composite foreign keys, canonical collaborator ordering, a unique active relationship pair/kind, and relationship-kind checks. Run invalid cross-workspace inserts against PostgreSQL as well as portable model tests. Add a migration-graph test that loads Alembic's `ScriptDirectory`, asserts the exact `0013 -> 0014` chain, one head, and reachability.

Run:

```bash
uv run pytest packages/db/tests/test_company_identity_models.py -q
```

Expected: FAIL because the models and migration do not exist.

- [ ] **Step 2: Define models and database invariants**

Use UUIDv7-compatible IDs and existing timestamp/type helpers. Add composite unique `(workspace_id, id)` targets and composite foreign keys for workspace-owned edges. Add partial unique indexes for one active primary membership per agent, one active agent/team pair, and one active relationship pair/kind. Add a check requiring `source_agent_id < target_agent_id` for `close_collaborator`; directed relationships must reject self-links. Store public tags as bounded JSON arrays with application validation.

- [ ] **Step 3: Write the additive/backfill migration**

Create tables, columns, indexes, and foreign keys. Set the Alembic migration metadata explicitly to `revision="0014"` and `down_revision="0013"`. Backfill one primary membership for each existing non-null `Agent.team_id`. Do not make any new agent field non-null without a server default. The downgrade must remove only Release 1 additions.

- [ ] **Step 4: Verify migration shape and model tests**

Run:

```bash
uv run pytest packages/db/tests/test_company_identity_models.py -q
uv run pytest packages/db/tests/test_migration_graph.py -q
```

Expected: PASS and one migration head.

- [ ] **Step 5: Commit the schema slice**

```bash
git add packages/db/src/jhin_db/models packages/db/src/jhin_db/alembic/versions/20260817_0014_company_identity.py packages/db/tests/test_company_identity_models.py packages/db/tests/test_migration_graph.py
git commit -m "feat: add company identity data model"
```

### Task 2: Implement memberships, relationships, and compatible agent updates

**Files:**
- Modify: `apps/api/src/jhin_api/agents/schemas.py`
- Modify: `apps/api/src/jhin_api/agents/service.py`
- Modify: `apps/api/src/jhin_api/agents/router.py`
- Modify: `apps/api/src/jhin_api/teams/schemas.py`
- Modify: `apps/api/src/jhin_api/teams/service.py`
- Modify: `apps/api/src/jhin_api/teams/router.py`
- Modify: `apps/api/src/jhin_api/org/hierarchy.py`
- Modify: `apps/api/src/jhin_api/org/schemas.py`
- Modify: `apps/api/src/jhin_api/org/service.py`
- Modify: `apps/api/src/jhin_api/org/router.py`
- Create: `apps/api/tests/test_company_topology_unit.py`

**Interfaces:**
- `GET/PUT /api/v1/workspaces/{workspace_id}/agents/{agent_id}/memberships`
- `POST/DELETE /api/v1/workspaces/{workspace_id}/agents/{agent_id}/relationships`
- Topology reads use `ViewerCtx`; agent create/update plus membership and relationship mutations use `AdminCtx` and existing CSRF protection. Member/viewer roles never gain configuration authority from a team, manager, or collaborator edge.
- Agent create/update accepts public purpose, expertise, discoverability, availability, optional primary team, secondary teams, and optional manager.
- Team detail returns active memberships grouped by primary/secondary without duplicating agents.

- [ ] **Step 1: Write failing API service tests**

Test managerless/teamless creation, atomic primary changes, secondary membership, last-primary removal, duplicate membership, manager cycles, cross-workspace IDs, symmetric collaborator create/delete, directed advisor/reviewer links, relationship non-authority, ViewerCtx read access, viewer/member 403 for every topology mutation, admin success, CSRF enforcement, and cross-workspace 404 without existence disclosure.

Run:

```bash
uv run pytest apps/api/tests/test_company_topology_unit.py -q
```

Expected: FAIL on missing schemas/services.

- [ ] **Step 2: Add validated request/response schemas**

Bound names, purpose, expertise tags, and relationship purpose. Use literal enums for membership state, relationship kind/status, discoverability, and availability. Keep existing response fields for backward compatibility and add `memberships` and `relationships` as additive fields; avatar fields arrive atomically with the media schema in Task 5.

- [ ] **Step 3: Implement atomic topology services**

Lock the affected agent when replacing a primary membership. Update `Agent.team_id` in the same transaction. Reuse the existing hierarchy cycle checker for manager changes, validate all IDs against the route workspace, canonicalize collaborator pairs, and emit audit events after successful flush.

- [ ] **Step 4: Add routes and preserve legacy behavior**

Keep existing agent/team endpoints working. Define `PUT /agents/{agent_id}/memberships` as full replacement `{primary_team_id: UUID | null, secondary_team_ids: UUID[]}`; define relationship create as `{target_agent_id, kind, purpose}` and delete as `DELETE /agents/{agent_id}/relationships/{relationship_id}`. Audit as `agent.memberships.updated`, `agent.relationship.created`, and `agent.relationship.deleted`. Return 404 for cross-workspace resources, 409 for topology conflicts, and validation-safe 422 responses for malformed input.

- [ ] **Step 5: Run topology and hierarchy regressions**

```bash
uv run pytest apps/api/tests/test_company_topology_unit.py apps/api/tests/test_hierarchy.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit the topology API**

```bash
git add apps/api/src/jhin_api/agents apps/api/src/jhin_api/teams apps/api/src/jhin_api/org apps/api/tests/test_company_topology_unit.py
git commit -m "feat: support optional teams and agent relationships"
```

### Task 3: Add public organization directory and bounded runtime roster

**Files:**
- Create: `apps/api/src/jhin_api/directory/__init__.py`
- Create: `apps/api/src/jhin_api/directory/schemas.py`
- Create: `apps/api/src/jhin_api/directory/service.py`
- Create: `apps/api/src/jhin_api/directory/router.py`
- Modify: `apps/api/src/jhin_api/main.py`
- Modify: `packages/agents/src/jhin_agents/snapshot.py`
- Modify: `packages/agents/src/jhin_agents/context.py`
- Create: `packages/organization/pyproject.toml`
- Create: `packages/organization/src/jhin_organization/__init__.py`
- Create: `packages/organization/src/jhin_organization/directory.py`
- Create: `packages/organization/src/jhin_organization/invariants.py`
- Create: `packages/organization/src/jhin_organization/py.typed`
- Create: `packages/organization/tests/test_directory.py`
- Create: `packages/organization/tests/test_invariants.py`
- Modify: `pyproject.toml`
- Modify: `apps/api/pyproject.toml`
- Modify: `packages/agents/pyproject.toml`
- Modify: `packages/tools/pyproject.toml`
- Modify: `services/agent_worker/pyproject.toml`
- Modify: `docker/python.Dockerfile`
- Modify: `uv.lock`
- Create: `packages/agents/tests/test_directory_context.py`
- Create: `apps/api/tests/test_directory_unit.py`

**Interfaces:**
- `GET /api/v1/workspaces/{workspace_id}/directory?q=&team_id=&availability=&limit=`
- `DirectoryEntry`: agent ID, name, role, public purpose, expertise, availability, and primary/secondary team summaries. The optional public avatar reference is added in Task 5 after the media schema exists.
- `OrganizationRoster`: self, manager, reports, primary/secondary teammates, close collaborators, capped and deterministically ordered.

- [ ] **Step 1: Write failing directory and context tests**

Assert name/role/expertise/team search; exclusion of hidden agents; 404 isolation; response field allowlist; local roster ranking; deterministic caps; and absence of prompts, grants, model config, private metadata, and conversations.

Run:

```bash
uv run pytest packages/organization/tests apps/api/tests/test_directory_unit.py packages/agents/tests/test_directory_context.py -q
```

Expected: FAIL because directory and roster assembly are absent.

- [ ] **Step 2: Implement the public directory query**

Implement the DTO, query, ranking, and topology invariants in dependency-light `jhin-organization`; API, runtime, and tools import that package and never import each other. Use escaped, parameterized PostgreSQL matching over public fields and team membership. Default to available/discoverable active agents, cap results at 25, and return stable relevance/name ordering. Enforce the workspace at the first query predicate.

- [ ] **Step 3: Assemble the bounded local roster**

Rank self, direct manager/reports, primary teammates, close collaborators, then secondary teammates. Deduplicate by agent ID and cap both agents and rendered tokens. Include only fields from `DirectoryEntry`.

- [ ] **Step 4: Add roster context without changing authority**

Append a clearly delimited `Company directory` context section to the existing prompt builder. It is routing context only and must not alter effective capabilities.

- [ ] **Step 5: Wire the shared package into every consumer**

Add `packages/organization` to uv workspace, Ruff, mypy, and pytest paths; add `jhin-organization` plus workspace sources to API, agents, tools, and agent worker manifests; copy its manifest in the Docker dependency layer; refresh the lock. Verify `uv sync --frozen --all-packages` and clean imports from API/agent-worker package builds.

- [ ] **Step 6: Run tests and privacy scans**

```bash
uv run pytest packages/organization/tests apps/api/tests/test_directory_unit.py packages/agents/tests/test_directory_context.py packages/agents/tests/test_context.py -q
uv sync --frozen --all-packages
rg -n "system_prompt|credential|grant|private_memory" apps/api/src/jhin_api/directory packages/organization/src/jhin_organization
```

Expected: tests pass; any sensitive identifiers appear only in explicit exclusion assertions/comments.

- [ ] **Step 7: Commit directory context**

```bash
git add apps/api/src/jhin_api/directory apps/api/src/jhin_api/main.py apps/api/pyproject.toml packages/organization packages/agents packages/tools/pyproject.toml services/agent_worker/pyproject.toml docker/python.Dockerfile pyproject.toml uv.lock apps/api/tests/test_directory_unit.py
git commit -m "feat: add safe organization awareness"
```

### Task 4: Add policy-gated directory search for agents

**Files:**
- Modify: `packages/policy/src/jhin_policy/capabilities.py`
- Modify: `packages/tools/src/jhin_tools/organization.py`
- Modify: `packages/tools/src/jhin_tools/__init__.py`
- Modify: `services/agent_worker/src/jhin_agent_worker/activities.py`
- Create: `packages/tools/tests/test_directory_tool.py`
- Modify: `packages/policy/tests/test_capabilities.py`

**Interfaces:**
- Capability: `organization.directory.read`
- Tool: `organization.directory.search`
- Arguments: bounded `query`, optional `team_id`, optional `expertise`, `limit <= 10`.
- Result: the same public `DirectoryEntry` allowlist, plus `has_more`; no side effects.

- [ ] **Step 1: Write failing capability and tool tests**

Test deny-by-default, matching workspace grant, cross-workspace rejection, field allowlist, hidden-agent exclusion, malformed filters, and result cap.

```bash
uv run pytest packages/policy/tests/test_capabilities.py packages/tools/tests/test_directory_tool.py -q
```

Expected: FAIL on missing capability/tool.

- [ ] **Step 2: Register the narrow read capability**

Add the capability without making it part of unrelated delegation grants. Ensure explicit deny continues to win.

- [ ] **Step 3: Implement the tool through the existing gateway**

Resolve workspace/run identity from the gateway context, call `jhin_organization.directory.search_directory`, sanitize the result, and emit the normal tool audit events. Never import `jhin_api` from a package and never call the directory database directly from a model adapter.

- [ ] **Step 4: Run gateway regressions**

```bash
uv run pytest packages/policy/tests packages/tools/tests -q
```

Expected: PASS.

- [ ] **Step 5: Commit agent directory search**

```bash
git add packages/policy/src/jhin_policy/capabilities.py packages/tools/src/jhin_tools services/agent_worker/src/jhin_agent_worker/activities.py packages/policy/tests packages/tools/tests/test_directory_tool.py
git commit -m "feat: let authorized agents find colleagues"
```

### Task 5: Implement safe avatar upload and media delivery

**Files:**
- Create: `packages/media/pyproject.toml`
- Create: `packages/media/src/jhin_media/__init__.py`
- Create: `packages/media/src/jhin_media/base.py`
- Create: `packages/media/src/jhin_media/postgres.py`
- Create: `packages/media/src/jhin_media/images.py`
- Create: `packages/media/tests/test_images.py`
- Create: `packages/db/src/jhin_db/models/media.py`
- Modify: `packages/db/src/jhin_db/models/org.py`
- Modify: `packages/db/src/jhin_db/models/work.py`
- Modify: `packages/db/src/jhin_db/models/__init__.py`
- Create: `packages/db/src/jhin_db/workflow_commands.py`
- Create: `packages/db/src/jhin_db/alembic/versions/20260817_0015_media_assets.py`
- Modify: `packages/db/tests/test_migration_graph.py`
- Create: `packages/db/tests/test_workflow_commands.py`
- Create: `apps/api/src/jhin_api/media/__init__.py`
- Create: `apps/api/src/jhin_api/media/schemas.py`
- Create: `apps/api/src/jhin_api/media/service.py`
- Create: `apps/api/src/jhin_api/media/router.py`
- Modify: `apps/api/src/jhin_api/main.py`
- Modify: `pyproject.toml`
- Modify: `apps/api/pyproject.toml`
- Modify: `services/agent_worker/pyproject.toml`
- Modify: `services/event_worker/pyproject.toml`
- Modify: `docker/python.Dockerfile`
- Modify: `uv.lock`
- Create: `apps/api/tests/test_media_unit.py`
- Modify: `apps/api/src/jhin_api/agents/schemas.py`
- Modify: `packages/organization/src/jhin_organization/directory.py`
- Modify: `apps/api/src/jhin_api/directory/schemas.py`
- Modify: `apps/api/src/jhin_api/directory/service.py`

**Interfaces:**
- `MediaAsset(workspace_id, owner_type, owner_id, purpose, state, created_by_user_id, created_at, retired_at)` is metadata-only; it never owns normalized bytes or upload content.
- `MediaVariant(asset_id, workspace_id, name, media_type, byte_size, width, height, sha256, content)` with unique `(asset_id, name)` and workspace-aware ownership.
- `AvatarGeneration(workspace_id, agent_id, prompt, provider_profile_id, status, media_asset_id, error_code, cost_micros, created_by_user_id)`.
- `WorkflowCommand(workspace_id, command_id, target_workflow_id, command_kind, workflow_type, signal_name, payload_json, depends_on_command_id, delivery_status, attempts, backoff_seconds, next_attempt_at, last_error)` is a durable outbox row with unique `(workspace_id, command_id)`; `command_kind` is `start` or `signal`, and database checks require start commands to carry only `workflow_type` and signal commands to carry only `signal_name`. An optional workspace-local `depends_on_command_id` orders a signal behind the command that starts its target workflow.
- Shared lower-layer helpers live in `jhin_db.workflow_commands`: `enqueue_workflow_start(session, workspace_id, target_workflow_id, workflow_type, payload_json, command_id) -> WorkflowCommand`, `enqueue_workflow_signal(session, workspace_id, target_workflow_id, signal_name, payload_json, command_id, depends_on_command_id=None) -> WorkflowCommand`, and `claim_ready_workflow_commands(session, now, limit) -> list[WorkflowCommand]`. API and workers depend on this module; neither imports another service package. Enqueue helpers create or return an idempotent command in the caller's transaction; claiming uses PostgreSQL `FOR UPDATE SKIP LOCKED` and excludes a command until its optional workspace-local dependency is delivered.
- Nullable `Agent.active_avatar_asset_id`.
- Additive public `avatar` references on agent and directory responses, containing only authenticated media URL, dimensions, and accessible fallback initials.
- Async `MediaStore.put(session, workspace_id, owner, NormalizedAvatarSet)`, `get(session, workspace_id, asset_id, variant)`, and `delete(session, workspace_id, asset_id)`.
- `POST /api/v1/workspaces/{workspace_id}/agents/{agent_id}/avatar` accepts multipart PNG/JPEG/WebP.
- `GET /api/v1/workspaces/{workspace_id}/media/{asset_id}/{variant}` returns authenticated `image/webp`.
- `DELETE /api/v1/workspaces/{workspace_id}/agents/{agent_id}/avatar` returns the agent to initials.
- Authenticated media reads use `ViewerCtx` plus exact workspace scoping. Avatar upload/delete use `AdminCtx` and CSRF; viewer/member receive 403 and cross-workspace IDs return 404.

- [ ] **Step 1: Write failing image-safety tests**

Cover valid PNG/JPEG/WebP, EXIF stripping, deterministic square variants, SVG rejection, MIME mismatch, animation rejection, byte/pixel/frame limits, truncated files, decompression-bomb handling, and no active-avatar change on failure. Cover ViewerCtx authenticated delivery, viewer/member 403 for upload/delete, admin success, CSRF enforcement, and cross-workspace media/mutation 404. In the command tests, cover start/signal database checks, workspace isolation, idempotent helper retries, conflicting command reuse rejection, and a signal remaining undispatchable until its `depends_on_command_id` is delivered.

```bash
uv run pytest packages/media/tests/test_images.py packages/db/tests/test_workflow_commands.py apps/api/tests/test_media_unit.py -q
```

Expected: FAIL because the package/endpoints do not exist.

- [ ] **Step 2: Add the media schema and migration**

Define assets, variants, generation jobs, the generic workflow-command outbox, the dependency self-reference, and the nullable active-avatar pointer. Implement the idempotent enqueue helpers in the dependency-light `jhin_db.workflow_commands` module. Use composite workspace foreign keys/indexes, explicit state/format checks, and additive defaults. Create `20260817_0015_media_assets.py` with `revision="0015"` and `down_revision="0014"`; no image bytes are present during backfill. Extend the migration-graph test to assert `0013 -> 0014 -> 0015`.

- [ ] **Step 3: Implement the media boundary and normalizer**

Decode from bounded bytes with Pillow, verify the decoded format, reject animation and excess dimensions before resizing, apply EXIF orientation, convert to RGB/RGBA, strip metadata, center-crop, and encode bounded 64/128/256px WebP variants. Compute SHA-256 over normalized bytes.

- [ ] **Step 4: Implement PostgreSQL storage and authenticated delivery**

`MediaAsset` is metadata-only, and `MediaVariant` is the sole normalized-byte/content owner. Normalize the bounded upload into variants and persist only those variants; original uploads are never stored or retained. All reads include workspace ID and active/non-deleted state. Send content type, ETag, private cache headers, and nosniff. Do not accept a URL input.

- [ ] **Step 5: Make avatar activation atomic**

Create the asset and switch `active_avatar_asset_id` only after all variants validate and persist. Soft-retire the previous asset after activation; failed uploads leave it untouched.

- [ ] **Step 6: Wire media dependencies and multipart support**

Add `jhin-media` and `python-multipart` to API, `jhin-media` to agent worker, and workspace sources everywhere. Add the package to root uv/Ruff/mypy/pytest configuration and Docker manifest-copy layer, refresh the lock, and verify frozen sync plus clean API/agent-worker image import smoke tests.

- [ ] **Step 7: Run media/security tests**

```bash
uv run pytest packages/media/tests packages/db/tests/test_workflow_commands.py apps/api/tests/test_media_unit.py apps/api/tests/test_security.py -q
uv sync --frozen --all-packages
```

Expected: PASS.

- [ ] **Step 8: Commit avatar upload**

```bash
git add packages/media packages/db/src/jhin_db/models/media.py packages/db/src/jhin_db/models/org.py packages/db/src/jhin_db/models/work.py packages/db/src/jhin_db/models/__init__.py packages/db/src/jhin_db/workflow_commands.py packages/db/src/jhin_db/alembic/versions/20260817_0015_media_assets.py packages/db/tests/test_migration_graph.py packages/db/tests/test_workflow_commands.py packages/organization/src/jhin_organization/directory.py apps/api/src/jhin_api/media apps/api/src/jhin_api/agents/schemas.py apps/api/src/jhin_api/directory apps/api/src/jhin_api/main.py apps/api/tests/test_media_unit.py apps/api/pyproject.toml services/agent_worker/pyproject.toml services/event_worker/pyproject.toml docker/python.Dockerfile pyproject.toml uv.lock
git commit -m "feat: add safe agent avatar media"
```

### Task 6: Add asynchronous stylized avatar generation contract

**Files:**
- Modify: `packages/models/src/jhin_models/base.py`
- Modify: `packages/models/src/jhin_models/factory.py`
- Create: `packages/models/src/jhin_models/images.py`
- Create: `packages/models/src/jhin_models/providers/openai_images.py`
- Create: `packages/models/tests/test_image_generation.py`
- Create: `packages/workflows/src/jhin_workflows/avatar_generation/__init__.py`
- Create: `packages/workflows/src/jhin_workflows/avatar_generation/shared.py`
- Create: `packages/workflows/src/jhin_workflows/avatar_generation/workflows.py`
- Create: `packages/workflows/tests/test_avatar_generation_workflow.py`
- Create: `services/agent_worker/src/jhin_agent_worker/avatar_activities.py`
- Modify: `services/agent_worker/src/jhin_agent_worker/main.py`
- Create: `services/event_worker/src/jhin_event_worker/workflow_commands.py`
- Modify: `services/event_worker/src/jhin_event_worker/main.py`
- Create: `services/event_worker/tests/test_workflow_commands.py`
- Modify: `apps/api/src/jhin_api/media/schemas.py`
- Modify: `apps/api/src/jhin_api/media/service.py`
- Modify: `apps/api/src/jhin_api/media/router.py`
- Modify: `apps/api/src/jhin_api/agents/schemas.py`
- Create: `apps/api/tests/test_avatar_generation_unit.py`

**Interfaces:**
- `ImageGenerationProvider.generate(prompt, size, idempotency_key) -> GeneratedImage`.
- `POST /api/v1/workspaces/{workspace_id}/agents/{agent_id}/avatar-generations` accepts `{prompt, client_request_id, provider_profile_id}` and returns `202` with generation ID, disclosure, and estimated cost.
- `GET /api/v1/workspaces/{workspace_id}/avatar-generations/{id}` returns queued/running/succeeded/failed.
- Prompt input is limited to public agent identity plus explicit user text.
- Generation create/retry uses `AdminCtx` and CSRF; status reads use `ViewerCtx` with exact workspace scoping. Viewer/member cannot create or retry a generation, and cross-workspace IDs return 404.

- [ ] **Step 1: Write failing provider, workflow, and API tests**

Test unsupported provider, a real OpenAI Images HTTP request/response adapter with fake transport, profile capability/model/size/price validation, user disclosure/cost response, exact prompt allowlist, idempotent retry, safe image normalization, success activation, failure preserving prior avatar, and agent creation succeeding before generation completes. Cover ViewerCtx status, viewer/member 403 for create/retry, admin success, CSRF enforcement, cross-workspace 404, a commit-before-command-dispatch crash, start-command-succeeded/response-lost, signal delivery, duplicate command IDs, retry, and command reconciler delivery.

```bash
uv run pytest packages/models/tests/test_image_generation.py packages/workflows/tests/test_avatar_generation_workflow.py apps/api/tests/test_avatar_generation_unit.py -q
```

Expected: FAIL on missing generation contract.

- [ ] **Step 2: Add an optional provider capability**

Do not require image support from chat providers. Read `config_json.image_generation = {enabled, model, sizes, cost_micros_by_size}` from the selected workspace-local profile. Factory lookup returns `image_generation_unsupported` when absent. Implement OpenAI `/images/generations` behind `ImageGenerationProvider`; all tests use fake transports and never call an external service.

- [ ] **Step 3: Implement durable generation**

The API atomically commits a queued `AvatarGeneration` and a `WorkflowCommand` start row with a deterministic `command_id` and `target_workflow_id=avatar-generation-{id}`. It then makes a best-effort immediate dispatch; the event worker reconciles pending/retryable start and signal commands with compare-and-set claiming, exponential backoff, and persisted delivery errors. Temporal already-started counts as delivered, and a lost response is safe because the unique command ID makes retries idempotent. The activity reloads only public agent identity, constructs a stylized editorial illustration prompt with no private chat/memory/prompt fields, calls the provider, passes bytes through the same image normalizer, and atomically activates the asset.

- [ ] **Step 4: Register worker and expose status**

Generation must not block agent creation or editing. Duplicate `client_request_id` returns the same generation. Record cost and provider metadata without credentials. Surface a stable user-safe error code and retryability. A retry endpoint requeues only retryable failed workflow commands and preserves the same command, workflow, and generation identities. Signal commands use the same outbox, delivery status, attempt/backoff/error fields, and idempotent reconciliation contract as start commands.

- [ ] **Step 5: Run generation and worker regressions**

```bash
uv run pytest packages/models/tests packages/workflows/tests/test_avatar_generation_workflow.py apps/api/tests/test_avatar_generation_unit.py services/agent_worker/tests services/event_worker/tests/test_workflow_commands.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit generation support**

```bash
git add packages/models packages/workflows/src/jhin_workflows/avatar_generation packages/workflows/tests/test_avatar_generation_workflow.py services/agent_worker services/event_worker apps/api/src/jhin_api/media apps/api/tests/test_avatar_generation_unit.py
git commit -m "feat: generate stylized agent avatars asynchronously"
```

### Task 7: Prove Release 1 acceptance and compatibility

**Files:**
- Create: `tests/integration/test_company_identity_exit.py`
- Create: `tests/integration/test_release1_migrations.py`
- Modify: `docs/implementation-plan.md`
- Create: `docs/architecture/company-topology-and-identity.md`

**Interfaces:**
- Produces one end-to-end acceptance suite and architecture record for Release 1.

- [ ] **Step 1: Write the integration scenarios**

Exercise a solo agent; an agent in two teams with one primary; a rejected manager cycle; a canonical collaborator link that grants no authority; PostgreSQL rejection of cross-workspace edges; directory privacy/search; valid avatar upload; rejected malformed media; successful fake-provider generation; generation failure preserving initials/previous avatar; and workflow-command reconciliation for both start and signal commands after the API-to-Temporal failure windows.

Create disposable PostgreSQL databases with unique test names and prove fresh `head`, `0013 -> head`, `head -> 0013 -> head`, and one exact revision chain. Always drop only the explicitly created test database in fixture cleanup.

- [ ] **Step 2: Run the focused suite**

```bash
uv run pytest -m integration tests/integration/test_company_identity_exit.py -v
uv run pytest -m integration tests/integration/test_release1_migrations.py -v
```

Expected: PASS.

- [ ] **Step 3: Run schema, backend, and migration gates**

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest -m "not integration"
uv run pytest packages/db/tests/test_migration_graph.py -q
uv sync --frozen --all-packages
docker compose build api agent-worker event-worker
```

Expected: PASS.

- [ ] **Step 4: Document invariants and compatibility**

Document optional structure, multi-team primary compatibility, relationship non-authority, roster privacy, media safety, generation degradation, API routes, and fresh verification counts. Mark only completed Release 1 items in the implementation plan.

- [ ] **Step 5: Commit Release 1 evidence**

```bash
git add tests/integration/test_company_identity_exit.py tests/integration/test_release1_migrations.py docs/architecture/company-topology-and-identity.md docs/implementation-plan.md
git commit -m "docs: verify company identity release"
```
