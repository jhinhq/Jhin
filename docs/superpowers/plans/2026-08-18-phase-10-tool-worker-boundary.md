# Phase 10 Deterministic Tool-Worker Boundary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a real `jhin-tool-worker` that exclusively owns deterministic tool, connector, trigger-sync, and sandbox effects while `agent-worker` owns model reasoning and sanitized persistence, with pre-Phase-10 Temporal histories remaining replayable and executable without local effects on the agent worker.

**Architecture:** `AgentTaskWorkflow` uses Temporal 1.31's `workflow.patched("phase10-tool-worker-boundary-v1")` once after run admission: new histories resolve advertised schemas and execute bound calls on `jhin-tool-queue`, while old histories retain their recorded activity names and agent-queue attributes and enter compatibility coordinator activities. The existing PostgreSQL `RunEvent(event_type="agent.step.tool_manifest")` remains the authoritative atomic ordered call manifest and contains only step identity plus canonical lossless calls; a separate fail-closed `RunEvent(event_type="agent.step.reasoning")` is the agent-only authority for bounded completion, usage, transitions, and provider call IDs. The two append-only events are inserted in one locked transaction for a new step, while legacy repair compares the frozen manifest exactly and inserts only the missing reasoning event before effect zero. Tool-worker selects only the requested manifest call's four JSON scalars, invokes the gateway with the existing deterministic run/step/ordinal UUID, and commits the existing `ToolCall`/`Approval` claims without materializing reasoning or unrelated history. Agent-side commit activities load the agent-only reasoning record, manifest, and durable rows by stable IDs, deriving projection-only status text rather than adding another outcome authority. Dedicated compatibility workflows accept stable IDs only, execute on the tool queue, and let legacy agent activities reuse the new reasoning and projection helpers without importing connectors or contacting sandbox-runner.

**Tech Stack:** Python 3.13, Temporal Python SDK 1.31.0 (`workflow.patched`, explicit activity `task_queue`, `Replayer`), SQLAlchemy 2.0.52, Alembic, PostgreSQL 17, Pydantic 2.13, NATS JetStream, Docker Compose, aiodocker, pytest, Ruff, mypy.

**Spec:** `docs/superpowers/specs/2026-08-18-phase-10-production-operations-design.md`, especially “Deterministic tool-worker boundary,” “Reverse proxy, TLS, and production Compose hardening,” sub-project 1, sequencing, and deployment/security acceptance.

## Global Constraints

- This plan implements only Phase 10 sub-project 1. It adds no telemetry, protected operations UI, DLQ/replay control, manual task retry, key rotation, or rate-limit feature.
- PostgreSQL remains the product source of truth, Temporal remains the durable workflow authority, NATS remains transport, and stable database invocation claims remain the at-most-once authority.
- Use Python `>=3.13`, Temporal SDK `>=1.31` with the lock remaining at 1.31.0, and SQLAlchemy `>=2.0.36`. This boundary needs no schema change: migration `0014` remains the single Alembic head.
- The new queue is exactly `TOOL_TASK_QUEUE = "jhin-tool-queue"`; the main workflow patch ID is exactly `phase10-tool-worker-boundary-v1`.
- `reason_agent_step` inserts the complete ordered manifest and separate agent-only reasoning event in one database transaction before any tool effect. `execute_bound_tool` accepts no tool name or arguments in its activity payload and SQL-selects only the requested entry's ordinal/lossless/name/arguments JSON scalars by workspace/run/step/ordinal.
- Tool-worker startup is the sole runtime caller of `build_default_catalog`; before every model step, `resolve_advertised_tools` on the tool queue is the sole consumer that applies live grant-to-schema filtering. Agent code only converts dependency-light schema DTOs to `jhin_models.ToolSchema`.
- Approval waits stay in `AgentTaskWorkflow`. Tool-worker resolves the decided approval against current PostgreSQL authority; agent-worker only commits the sanitized transcript/timeline projection.
- Ordinary calls, approval resolution, trigger comment-back, and sandbox workspace cleanup all cross `jhin-tool-queue`. Agent-worker has no `jhin-connectors` dependency, runner URL/token/default-image environment, or `runner` network.
- Tool-worker has PostgreSQL, NATS, Temporal, master-key, connector, and runner access, but no `jhin-agents`, `jhin-models`, model-provider settings, prompt, completion, private reasoning, agent reasoning DTO, whole `RunEvent.payload_json`, or unrelated history access.
- Pre-patch histories retain `run_agent_step`, `resolve_approval`, `finalize_run`, and `sync_external` schedule names and recorded queue attributes. Compatibility handlers coordinate deterministic tool-queue workflows and never execute connectors or call sandbox-runner locally.
- Do not call `workflow.deprecate_patch`; do not remove a legacy handler or compatibility workflow until every pre-patch history has closed and can no longer be queried.
- `execution_unknown` is persisted and projected before the outer workflow stops non-retryably; no automatic retry may repeat an ambiguous effect.
- Test crash barriers cover the exact reasoning/tool matrix: agent pre-bind and post-bind, plus tool pre-claim, post-claim/pre-effect, and post-effect/pre-commit. They are selected by exact versioned names and stable UUIDs, persist fsynced arrival/release markers on a test-only mount, are no-ops when unconfigured, and make agent/tool startup fail in production if any barrier setting is present.
- Sandbox-runner runs with a nonzero UID/GID. Docker access is either a rootless socket owned by that UID or a rootful socket whose exact numeric group is the only supplemental Compose group. There is no root, privileged, sudo, chmod, socket-ownership mutation, or fallback path. Job containers remain `1000:1000` and receive neither socket nor supplemental group.
- Every task follows RED → focused GREEN → affected regression → scoped commit. Never use `git add .`, and never edit, stage, rename, delete, or commit the user-owned `orgforge-production-implementation-plan.md`.

## Complete File Map

The task-local `Files` blocks are the staging authority; this index is the complete cross-task map (a repeated path is intentionally modified in a later task).

- **Crash controls:** `packages/tools/src/jhin_tools/test_barriers.py`, `packages/tools/src/jhin_tools/builtin.py`, `packages/tools/src/jhin_tools/gateway.py`, `packages/tools/src/jhin_tools/__init__.py`, `packages/tools/tests/test_crash_barriers.py`, `packages/tools/tests/test_gateway.py`, `services/agent_worker/src/jhin_agent_worker/settings.py`, `services/agent_worker/src/jhin_agent_worker/resources.py`, `services/agent_worker/src/jhin_agent_worker/activities.py`, `services/agent_worker/src/jhin_agent_worker/trigger_activities.py`, `services/agent_worker/tests/test_upgrade_crash_barriers.py`.
- **Frozen-history generator/contracts:** `scripts/capture_phase9_temporal_histories.py`, `tests/test_capture_phase9_temporal_histories.py`, `packages/workflows/tests/fixtures/phase9_temporal/agent-tool-step.json`, `packages/workflows/tests/fixtures/phase9_temporal/agent-post-bind-pre-effect.json`, `packages/workflows/tests/fixtures/phase9_temporal/agent-parked-approval.json`, `packages/workflows/tests/fixtures/phase9_temporal/agent-finalization.json`, `packages/workflows/tests/fixtures/phase9_temporal/triggered-sync.json`, `packages/workflows/tests/fixtures/phase9_temporal/engineering-sync.json`, `packages/workflows/tests/fixtures/phase9_temporal/README.md`, `packages/workflows/tests/fixtures/phase9_temporal/phase9-ref.txt`, `packages/workflows/tests/test_phase10_history_replay.py`, `packages/workflows/src/jhin_workflows/task_queues.py`, `packages/workflows/src/jhin_workflows/__init__.py`, `packages/workflows/src/jhin_workflows/agent_task/shared.py`, `apps/api/src/jhin_api/public_payloads.py`, `apps/api/tests/test_approvals_unit.py`.
- **Reasoning/projections:** `services/agent_worker/src/jhin_agent_worker/reasoning.py`, `services/agent_worker/src/jhin_agent_worker/projections.py`, `services/agent_worker/src/jhin_agent_worker/activities.py`, `services/agent_worker/tests/test_reasoning_manifest.py`, `services/agent_worker/tests/test_step_projection.py`, `services/agent_worker/tests/test_legacy_manifest_sidecar.py`, `services/agent_worker/tests/test_phase9_invocation_activity.py`, `services/agent_worker/tests/test_approval_activity.py`.
- **Catalog/tool-worker/workspace registration:** `services/tool_worker/pyproject.toml`, `services/tool_worker/src/jhin_tool_worker/__init__.py`, `services/tool_worker/src/jhin_tool_worker/settings.py`, `services/tool_worker/src/jhin_tool_worker/resources.py`, `services/tool_worker/src/jhin_tool_worker/activities.py`, `services/tool_worker/src/jhin_tool_worker/main.py`, `services/tool_worker/tests/test_advertised_tools.py`, `services/tool_worker/tests/test_bound_tool_execution.py`, `services/tool_worker/tests/test_bound_approval.py`, `packages/tools/src/jhin_tools/builtin.py`, `packages/tools/src/jhin_tools/__init__.py`, `packages/connectors/src/jhin_connectors/base.py`, `packages/connectors/src/jhin_connectors/registry.py`, `packages/connectors/src/jhin_connectors/__init__.py`, `packages/connectors/src/jhin_connectors/github/connector.py`, `packages/connectors/src/jhin_connectors/cli/connector.py`, `packages/connectors/src/jhin_connectors/linear/connector.py`, `packages/connectors/src/jhin_connectors/vercel/connector.py`, `packages/connectors/src/jhin_connectors/supabase/connector.py`, `packages/connectors/src/jhin_connectors/example/connector.py`, `packages/connectors/tests/test_manifest_registry.py`, `apps/api/src/jhin_api/policy/router.py`, `apps/api/tests/test_policy_unit.py`, `pyproject.toml`, `uv.lock`.
- **Patched orchestration:** `packages/workflows/src/jhin_workflows/agent_task/workflows.py`, `packages/workflows/tests/test_agent_task_tool_routing.py`, `packages/workflows/tests/test_agent_task_delegation.py`, `packages/workflows/tests/test_phase10_history_replay.py`.
- **Compatibility/sync/cleanup:** `packages/workflows/src/jhin_workflows/tool_compat/__init__.py`, `packages/workflows/src/jhin_workflows/tool_compat/shared.py`, `packages/workflows/src/jhin_workflows/tool_compat/workflows.py`, `services/agent_worker/src/jhin_agent_worker/compatibility.py`, `services/agent_worker/src/jhin_agent_worker/trigger_activities.py`, `services/tool_worker/src/jhin_tool_worker/trigger_activities.py`, `services/tool_worker/src/jhin_tool_worker/cleanup_activities.py`, `packages/workflows/tests/test_tool_compat_workflows.py`, `services/agent_worker/tests/test_compatibility_coordinators.py`, `services/tool_worker/tests/test_trigger_sync_and_cleanup.py`, `packages/tools/src/jhin_tools/invocation.py`, `packages/tools/src/jhin_tools/__init__.py`, `packages/tools/tests/test_invocation.py`, `packages/workflows/src/jhin_workflows/triggered_task/shared.py`, `packages/workflows/src/jhin_workflows/triggered_task/workflows.py`, `packages/workflows/tests/test_triggered_task_workflow.py`, `packages/workflows/src/jhin_workflows/engineering_ticket/shared.py`, `packages/workflows/src/jhin_workflows/engineering_ticket/workflows.py`, `packages/workflows/tests/test_engineering_ticket_workflow.py`.
- **Worker distributions/poller:** `services/tool_worker/src/jhin_tool_worker/main.py`, `services/tool_worker/tests/test_worker_registration.py`, `services/agent_worker/src/jhin_agent_worker/main.py`, `services/agent_worker/pyproject.toml`, `services/tool_worker/pyproject.toml`, `packages/workflows/pyproject.toml`, `packages/workflows/src/jhin_workflows/poller_health.py`, `packages/workflows/tests/test_poller_health.py`, `docker/python.Dockerfile`, `tests/test_worker_dependency_boundaries.py`, `tests/test_executable_catalog_boundary.py`.
- **Sandbox socket boundary:** `services/sandbox_runner/src/jhin_sandbox_runner/docker_socket.py`, `services/sandbox_runner/src/jhin_sandbox_runner/settings.py`, `services/sandbox_runner/src/jhin_sandbox_runner/jobs.py`, `services/sandbox_runner/src/jhin_sandbox_runner/main.py`, `services/sandbox_runner/tests/test_docker_socket.py`, `services/sandbox_runner/tests/test_job_config.py`, `services/sandbox_runner/tests/test_job_lifecycle.py`.
- **Compose topology:** `compose.yaml`, `compose.dev.yaml`, `compose.rootless.yaml`, `compose.rootful.yaml`, `.env.example`, `scripts/assert_phase10_tool_worker_compose.py`, `tests/test_phase10_tool_worker_compose.py`, `tests/test_compose_connector_allowlist.py`, `tests/test_compose_phase9_dev_fakes.py`, `tests/test_compose_supabase_db_fixture.py`, `tests/integration/conftest.py`, `tests/integration/test_stack_health.py`, `tests/integration/test_phase6_security.py`.
- **Documentation:** `docs/architecture/tool-worker-boundary.md`, `docs/architecture/connectors.md`, `docs/architecture/sandboxing.md`, `README.md`, `.env.example`, `tests/test_tool_worker_docs.py`.
- **Live/upgrade acceptance:** `tests/integration/test_phase10_tool_worker_boundary.py`, `tests/integration/test_phase10_sandbox_socket_modes.py`, `tests/integration/phase10_upgrade_harness.py`, `tests/integration/test_phase10_live_upgrade.py`, `tests/integration/compose.phase10-upgrade.yaml`, `tests/integration/test_phase3_exit.py`, `tests/integration/test_phase6_exit.py`, `tests/integration/test_phase7_exit.py`, `tests/integration/test_phase9_exit.py`, `Makefile`, `.github/workflows/ci.yml`.

---

### Task 0: Add disabled-by-default durable crash barriers to the Phase 9 baseline

**Files:**
- Create: `packages/tools/src/jhin_tools/test_barriers.py`
- Modify: `packages/tools/src/jhin_tools/builtin.py`
- Modify: `packages/tools/src/jhin_tools/gateway.py`
- Modify: `packages/tools/src/jhin_tools/__init__.py`
- Create: `packages/tools/tests/test_crash_barriers.py`
- Modify: `packages/tools/tests/test_gateway.py`
- Modify: `services/agent_worker/src/jhin_agent_worker/settings.py`
- Modify: `services/agent_worker/src/jhin_agent_worker/resources.py`
- Modify: `services/agent_worker/src/jhin_agent_worker/activities.py`
- Modify: `services/agent_worker/src/jhin_agent_worker/trigger_activities.py`
- Create: `services/agent_worker/tests/test_upgrade_crash_barriers.py`

**Interfaces:**
- Consumes: Phase 9's existing manifest bind, gateway claim/execute/terminal commits, trigger sync, final cleanup, and Pydantic settings.
- Produces: `CrashBarrierConfig(root: Path | None, selected: CrashBarrierName | None, match_identity: UUID | None)`; `CrashBarrier.arrive_and_wait(name: CrashBarrierName, identity: UUID) -> None`; seven exact failpoint names (the five required reasoning/tool crash boundaries plus Phase 9 sync/cleanup upgrade controls); durable host-visible arrival/release files; production configuration rejection. With no explicit test configuration every call is a no-op.

- [ ] **Step 1: Write the failing barrier filesystem and settings tests**

```python
async def test_barrier_fsyncs_arrival_and_waits_for_release(tmp_path: Path) -> None:
    barrier = CrashBarrier(CrashBarrierConfig(root=tmp_path, selected=TOOL_AFTER_CLAIM))
    identity = UUID("018f4d52-8b93-7d41-8ac7-7f190f091111")
    waiting = asyncio.create_task(barrier.arrive_and_wait(TOOL_AFTER_CLAIM, identity))
    arrived = tmp_path / TOOL_AFTER_CLAIM / f"{identity}.arrived"
    release = tmp_path / TOOL_AFTER_CLAIM / f"{identity}.release"
    await wait_until(arrived.exists)
    assert not waiting.done()
    release_barrier(tmp_path, TOOL_AFTER_CLAIM, identity)
    await asyncio.wait_for(waiting, timeout=1)


@pytest.mark.parametrize("app_env", ["production", "prod"])
@pytest.mark.parametrize(
    ("setting", "value"),
    [
        ("JHIN_TEST_CRASH_BARRIER_DIR", "/run/jhin/test-barriers"),
        ("JHIN_TEST_CRASH_BARRIER_NAME", PHASE9_AFTER_MANIFEST),
        ("JHIN_TEST_CRASH_BARRIER_MATCH", "018f4d52-8b93-7d41-8ac7-7f190f091111"),
    ],
)
def test_agent_rejects_test_barrier_configuration_in_production(
    app_env: str, setting: str, value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APP_ENV", app_env)
    monkeypatch.setenv(setting, value)
    with pytest.raises(ValidationError, match="test crash barriers are forbidden"):
        Settings()
```

```python
async def test_gateway_barriers_bracket_only_the_external_effect(gateway_world: GatewayWorld) -> None:
    gateway_world.barrier.record_into(gateway_world.order)
    await gateway_world.gateway.request(
        "test.mutate", '{"value":"once"}', invocation_id=gateway_world.invocation_id
    )
    assert gateway_world.order == [
        TOOL_BEFORE_CLAIM,
        "claim_committed",
        TOOL_AFTER_CLAIM,
        "executor",
        TOOL_AFTER_EFFECT,
        "terminal_committed",
    ]


@pytest.mark.parametrize(
    "name",
    [PHASE9_AFTER_MANIFEST, PHASE9_SYNC_BEFORE_EFFECT, PHASE9_CLEANUP_BEFORE_EFFECT],
)
async def test_phase9_barrier_arrives_before_effect(
    phase9_world: Phase9BarrierWorld, name: CrashBarrierName
) -> None:
    waiting = asyncio.create_task(phase9_world.invoke(name))
    await phase9_world.wait_arrived(name)
    assert phase9_world.effect_count(name) == 0
    phase9_world.release(name)
    await waiting
    assert phase9_world.effect_count(name) == 1


async def test_agent_bind_barriers_bracket_only_the_manifest_commit(
    phase9_world: Phase9BarrierWorld,
) -> None:
    await phase9_world.invoke_reasoning_with_recording_barrier()
    assert phase9_world.order == [
        "model_returned",
        AGENT_BEFORE_BIND,
        "manifest_committed",
        PHASE9_AFTER_MANIFEST,
        "activity_returned",
    ]
```

- [ ] **Step 2: Run RED and confirm the missing symbols**

```bash
uv run pytest packages/tools/tests/test_crash_barriers.py packages/tools/tests/test_gateway.py services/agent_worker/tests/test_upgrade_crash_barriers.py -q
```

Expected: FAIL on importing `CrashBarrier`, `AGENT_BEFORE_BIND`, `TOOL_BEFORE_CLAIM`, and the new agent settings fields; no production implementation exists yet.

- [ ] **Step 3: Implement exact failpoint names and durable controls**

```python
CrashBarrierName = Literal[
    "phase10.agent.before_manifest_bind.v1",
    "phase9.agent.after_manifest.before_effect.v1",
    "phase9.agent.sync.before_effect.v1",
    "phase9.agent.cleanup.before_effect.v1",
    "phase10.tool.before_claim.v1",
    "phase10.tool.after_claim.before_effect.v1",
    "phase10.tool.after_effect.before_terminal_commit.v1",
]
AGENT_BEFORE_BIND: CrashBarrierName = "phase10.agent.before_manifest_bind.v1"
PHASE9_AFTER_MANIFEST: CrashBarrierName = "phase9.agent.after_manifest.before_effect.v1"
PHASE9_SYNC_BEFORE_EFFECT: CrashBarrierName = "phase9.agent.sync.before_effect.v1"
PHASE9_CLEANUP_BEFORE_EFFECT: CrashBarrierName = "phase9.agent.cleanup.before_effect.v1"
TOOL_BEFORE_CLAIM: CrashBarrierName = "phase10.tool.before_claim.v1"
TOOL_AFTER_CLAIM: CrashBarrierName = "phase10.tool.after_claim.before_effect.v1"
TOOL_AFTER_EFFECT: CrashBarrierName = "phase10.tool.after_effect.before_terminal_commit.v1"

@dataclass(frozen=True)
class CrashBarrierConfig:
    root: Path | None = None
    selected: CrashBarrierName | None = None
    match_identity: UUID | None = None

class CrashBarrier:
    def __init__(self, config: CrashBarrierConfig) -> None:
        self._config = config

    async def arrive_and_wait(self, name: CrashBarrierName, identity: UUID) -> None:
        if self._config.root is None or self._config.selected != name:
            return
        if self._config.match_identity is not None and identity != self._config.match_identity:
            return
        directory = self._config.root / name
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        arrived = directory / f"{identity}.arrived"
        release = directory / f"{identity}.release"
        try:
            fd = os.open(arrived, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            try:
                os.write(fd, b"arrived\n")
                os.fsync(fd)
            finally:
                os.close(fd)
            directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        while not release.is_file():
            await asyncio.sleep(0.05)

def release_barrier(root: Path, name: CrashBarrierName, identity: UUID) -> None:
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    marker = directory / f"{identity}.release"
    fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.write(fd, b"release\n")
        os.fsync(fd)
    finally:
        os.close(fd)
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)

class BarrierSettingsMixin(BaseModel):
    app_env: str = Field(default="development", validation_alias="APP_ENV")
    test_crash_barrier_dir: Path | None = Field(
        default=None, validation_alias="JHIN_TEST_CRASH_BARRIER_DIR"
    )
    test_crash_barrier_name: CrashBarrierName | None = Field(
        default=None, validation_alias="JHIN_TEST_CRASH_BARRIER_NAME"
    )
    test_crash_barrier_match: UUID | None = Field(
        default=None, validation_alias="JHIN_TEST_CRASH_BARRIER_MATCH"
    )

    @field_validator(
        "test_crash_barrier_dir", "test_crash_barrier_name", "test_crash_barrier_match",
        mode="before",
    )
    @classmethod
    def empty_barrier_value_is_none(cls, value: object) -> object:
        return None if value == "" else value

    @model_validator(mode="after")
    def reject_production_barrier(self) -> Self:
        configured = any((self.test_crash_barrier_dir, self.test_crash_barrier_name,
                          self.test_crash_barrier_match))
        if self.app_env.lower() in {"prod", "production"} and configured:
            raise ValueError("test crash barriers are forbidden in production")
        return self
```

Add optional `test_barrier: CrashBarrier | None = None` to `ToolExecutionContext`. Call `TOOL_BEFORE_CLAIM` with the stable invocation UUID after entering the invocation lifecycle lock but before reading or inserting a `ToolCall`; call `TOOL_AFTER_CLAIM` immediately after the deterministic claim commit and before executor dispatch; call `TOOL_AFTER_EFFECT` after sanitized output/status are assigned but immediately before the terminal commit. After a model response but before opening the locked manifest-bind transaction, the agent calls `AGENT_BEFORE_BIND` with `run_id`; immediately after the manifest commit and before activity return or effect delegation it calls the existing versioned `PHASE9_AFTER_MANIFEST` with `run_id`. Preserve that post-bind name through the Phase 10 reasoning split so frozen Phase 9 capture and the live Phase 10 crash test exercise the same boundary. The Phase 9 agent also calls its sync and cleanup barriers before their effects. Settings map `APP_ENV`, `JHIN_TEST_CRASH_BARRIER_DIR`, `JHIN_TEST_CRASH_BARRIER_NAME`, and `JHIN_TEST_CRASH_BARRIER_MATCH`; a model validator rejects each individual barrier control when normalized `APP_ENV` is production/prod. `Resources.create` constructs `CrashBarrier(CrashBarrierConfig(root=settings.test_crash_barrier_dir, selected=settings.test_crash_barrier_name, match_identity=settings.test_crash_barrier_match))`, and every `ToolExecutionContext` receives it. Never expose a barrier HTTP endpoint and never unlink arrival/release files in runtime code.

- [ ] **Step 4: Run focused GREEN and the Phase 9 regression**

```bash
uv run pytest packages/tools/tests/test_crash_barriers.py packages/tools/tests/test_gateway.py packages/tools/tests/test_gateway_concurrency.py services/agent_worker/tests/test_upgrade_crash_barriers.py services/agent_worker/tests/test_phase9_invocation_activity.py -q
uv run ruff check packages/tools services/agent_worker
uv run mypy packages/tools/src services/agent_worker/src
```

Expected: PASS; disabled barriers leave existing effect counts and gateway behavior unchanged.

- [ ] **Step 5: Commit the test control baseline**

```bash
git add packages/tools/src/jhin_tools/test_barriers.py packages/tools/src/jhin_tools/builtin.py packages/tools/src/jhin_tools/gateway.py packages/tools/src/jhin_tools/__init__.py packages/tools/tests/test_crash_barriers.py packages/tools/tests/test_gateway.py services/agent_worker/src/jhin_agent_worker/settings.py services/agent_worker/src/jhin_agent_worker/resources.py services/agent_worker/src/jhin_agent_worker/activities.py services/agent_worker/src/jhin_agent_worker/trigger_activities.py services/agent_worker/tests/test_upgrade_crash_barriers.py
git commit -m "test: add durable worker crash barriers"
```

### Task 1: Freeze Phase 9 histories and define queue/activity contracts

**Files:**
- Create: `scripts/capture_phase9_temporal_histories.py`
- Create: `tests/test_capture_phase9_temporal_histories.py`
- Create: `packages/workflows/tests/fixtures/phase9_temporal/agent-tool-step.json`
- Create: `packages/workflows/tests/fixtures/phase9_temporal/agent-post-bind-pre-effect.json`
- Create: `packages/workflows/tests/fixtures/phase9_temporal/agent-parked-approval.json`
- Create: `packages/workflows/tests/fixtures/phase9_temporal/agent-finalization.json`
- Create: `packages/workflows/tests/fixtures/phase9_temporal/triggered-sync.json`
- Create: `packages/workflows/tests/fixtures/phase9_temporal/engineering-sync.json`
- Create: `packages/workflows/tests/fixtures/phase9_temporal/README.md`
- Create: `packages/workflows/tests/fixtures/phase9_temporal/phase9-ref.txt`
- Create: `packages/workflows/tests/test_phase10_history_replay.py`
- Modify: `packages/workflows/src/jhin_workflows/task_queues.py`
- Modify: `packages/workflows/src/jhin_workflows/__init__.py`
- Modify: `packages/workflows/src/jhin_workflows/agent_task/shared.py`
- Modify: `apps/api/src/jhin_api/public_payloads.py`
- Modify: `apps/api/tests/test_approvals_unit.py`

**Interfaces:**
- Consumes: the current pre-patch workflows, existing `RunEvent(event_type="agent.step.tool_manifest")`, `stable_tool_invocation_id(run_id, step_index, ordinal)`, and `public_run_event_payload`.
- Produces: six immutable pre-patch histories plus the exact Phase 9 barrier-enabled Git ref; `TOOL_TASK_QUEUE`; dependency-light DTOs; a generator that preserves real `HistoryEvent` values in the Temporal SDK JSON shape and reconstructs with an explicit caller-supplied workflow ID; fail-closed public projections proving canonical arguments and every future `agent.step.reasoning` field remain unavailable through API models.

- [ ] **Step 1: Write failing generator, queue/DTO, and public-projection tests**

```python
async def test_save_history_uses_real_event_sdk_json_and_caller_workflow_id(
    tmp_path: Path,
) -> None:
    event = HistoryEvent(
        event_id=1,
        event_type=EventType.EVENT_TYPE_WORKFLOW_EXECUTION_STARTED,
    )
    event.workflow_execution_started_event_attributes.workflow_type.name = "AgentTaskWorkflow"
    event.workflow_execution_started_event_attributes.task_queue.name = "jhin-agent-queue"
    fetched = WorkflowHistory("server-returned-id", [event])
    handle = SimpleNamespace(fetch_history=AsyncMock(return_value=fetched))
    destination = tmp_path / "agent-tool-step.json"

    await capture.save_history(
        handle,
        destination,
        workflow_id="caller-supplied-workflow-id",
    )

    raw = json.loads(destination.read_text(encoding="utf-8"))
    assert set(raw) == {"events"}
    assert raw["events"] == [{
        "eventId": "1",
        "eventType": "EVENT_TYPE_WORKFLOW_EXECUTION_STARTED",
        "workflowExecutionStartedEventAttributes": {
            "workflowType": {"name": "AgentTaskWorkflow"},
            "taskQueue": {"name": "jhin-agent-queue"},
        },
    }]
    reconstructed = WorkflowHistory.from_json("caller-supplied-workflow-id", raw)
    assert reconstructed.workflow_id == "caller-supplied-workflow-id"
    assert reconstructed.events[0] == event


def test_capture_defaults_to_the_development_database_port() -> None:
    assert capture.DEFAULT_CAPTURE_DATABASE_URL == (
        "postgresql+asyncpg://jhin:jhin@127.0.0.1:55432/jhin"
    )


async def test_generate_writes_exact_ref_and_reconstructs_each_caller_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = HistoryEvent(
        event_id=1,
        event_type=EventType.EVENT_TYPE_WORKFLOW_EXECUTION_STARTED,
    )
    captures = {
        scenario: capture.CapturedWorkflow(
            workflow_id=f"phase9-{scenario}-workflow",
            handle=SimpleNamespace(
                fetch_history=AsyncMock(
                    return_value=WorkflowHistory("server-id", [event])
                )
            ),
        )
        for scenario in capture.SCENARIOS
    }
    monkeypatch.setattr(capture, "capture_scenarios", AsyncMock(return_value=captures))
    source_ref = "0123456789abcdef0123456789abcdef01234567"
    await capture.generate(tmp_path, source_ref=source_ref)
    assert (tmp_path / "phase9-ref.txt").read_text(encoding="utf-8") == f"{source_ref}\n"
    for scenario, captured in captures.items():
        restored = WorkflowHistory.from_json(
            captured.workflow_id,
            (tmp_path / f"{scenario}.json").read_text(encoding="utf-8"),
        )
        assert restored.workflow_id == captured.workflow_id
        assert restored.events == [event]
```

```python
def test_tool_queue_name_is_stable() -> None:
    assert TOOL_TASK_QUEUE == "jhin-tool-queue"


def test_public_manifest_projection_never_exposes_bound_arguments() -> None:
    private = {
        "step": 2,
        "manifest": {"count": 1, "calls": [{
            "ordinal": 0, "lossless": True, "tool_name": "linear.issue.get",
            "arguments_json": '{"connection_id":"secret-ref","issue":"ENG-7"}',
        }]},
    }
    public = public_run_event_payload("agent.step.tool_manifest", private)
    assert public == {
        "step": 2,
        "manifest": {"count": 1, "calls": [
            {"ordinal": 0, "lossless": True, "tool_name": "linear.issue.get"}
        ]},
    }
    assert "arguments_json" not in json.dumps(public)


def test_public_reasoning_projection_is_always_empty() -> None:
    private = {
        "format_version": 1,
        "step": 2,
        "completion": "agent-only completion",
        "provider_call_ids": ["provider-secret-id"],
        "transitions": [{"node": "execute_tool"}],
        "usage": {"input_tokens": 7, "output_tokens": 3},
        "unexpected_future_private_field": {"secret": "must fail closed"},
    }
    event = RunEventOut.model_validate({
        "id": new_uuid7(),
        "run_id": new_uuid7(),
        "seq": 3,
        "event_type": "agent.step.reasoning",
        "payload_json": private,
        "created_at": datetime.now(UTC),
    }).model_dump(mode="json")
    assert event["payload_json"] == {}
    assert "must fail closed" not in json.dumps(event)
```

- [ ] **Step 2: Run RED and verify missing generator/contracts**

```bash
uv run pytest tests/test_capture_phase9_temporal_histories.py packages/workflows/tests/test_phase10_history_replay.py apps/api/tests/test_approvals_unit.py -q
```

Expected: FAIL because the capture module, tool queue, DTOs, replay fixtures, and fail-closed reasoning-event projection do not exist; the existing public manifest projection assertion itself passes.

- [ ] **Step 3: Implement the generator before changing workflow code**

`scripts/capture_phase9_temporal_histories.py` must refuse a dirty workflow source or any source containing `PHASE10_TOOL_WORKER_PATCH`, require `git rev-parse HEAD` to equal the Task 0 commit, apply the real current Alembic head to `PHASE9_CAPTURE_DATABASE_URL`, and run the Phase 9 workers against one Temporal test environment. Use these executable entry points:

```python
SCENARIOS = (
    "agent-tool-step",
    "agent-post-bind-pre-effect",
    "agent-parked-approval",
    "agent-finalization",
    "triggered-sync",
    "engineering-sync",
)

DEFAULT_CAPTURE_DATABASE_URL = (
    "postgresql+asyncpg://jhin:jhin@127.0.0.1:55432/jhin"
)

@dataclass(frozen=True)
class CapturedWorkflow:
    workflow_id: str
    handle: Any

async def save_history(handle: Any, destination: Path, *, workflow_id: str) -> None:
    fetched = await handle.fetch_history()
    reconstructed = WorkflowHistory(workflow_id, fetched.events)
    destination.write_text(reconstructed.to_json() + "\n", encoding="utf-8")

async def generate(destination: Path, *, source_ref: str) -> None:
    captures: dict[str, CapturedWorkflow] = await capture_scenarios()
    if tuple(captures) != SCENARIOS:
        raise RuntimeError("Phase 9 capture scenarios are incomplete or reordered")
    for scenario, captured in captures.items():
        await save_history(
            captured.handle,
            destination / f"{scenario}.json",
            workflow_id=captured.workflow_id,
        )
    (destination / "phase9-ref.txt").write_text(f"{source_ref}\n")
```

Parse `PHASE9_CAPTURE_DATABASE_URL` with `DEFAULT_CAPTURE_DATABASE_URL` as its default. `WorkflowHistory.to_json()` is the serialization authority: do not hand-build JSON or add a non-SDK `workflowId` member. `WorkflowHistory.from_json(captured.workflow_id, ...)` supplies the workflow ID during verification because the SDK JSON document itself contains only the `events` array.

For `agent-post-bind-pre-effect`, configure `PHASE9_AFTER_MANIFEST` for the exact `run_id`, start the real Phase 9 `run_agent_step`, wait for its fsynced `.arrived` file, verify the manifest exists and no `ToolCall` does, then fetch the still-open history. Other scenarios capture a completed normal step, open approval, finalization pending at cleanup, trigger sync pending before connector dispatch, and engineering sync pending before connector dispatch. Fetch every handle before closing the environment. The README records the source ref, Temporal SDK 1.31.0, workflow type, database state at capture, and expected old names.

- [ ] **Step 4: Capture the immutable histories while workflow source still matches the Phase 9 ref**

```bash
PHASE9_CAPTURE_DATABASE_URL=postgresql+asyncpg://jhin:jhin@127.0.0.1:55432/jhin uv run python scripts/capture_phase9_temporal_histories.py
test "$(cat packages/workflows/tests/fixtures/phase9_temporal/phase9-ref.txt)" = "$(git rev-parse HEAD)"
```

Expected: six SDK JSON histories plus README/ref are written; the post-bind scenario has a manifest, no separate reasoning event, and no tool effect. Do this before editing any `packages/workflows/src` file.

- [ ] **Step 5: Add exact stdlib-only workflow contracts and fail-closed public projections**

Preserve legacy `RunStepInput`, `ResolveApprovalInput`, and old activity constants. Add the exact contracts below, plus the queue-name and projection tests:

```python
EXPECTED_OLD_ACTIVITIES = {
    "agent-tool-step.json": {"resolve_snapshot", "run_agent_step", "finalize_run"},
    "agent-post-bind-pre-effect.json": {"resolve_snapshot", "run_agent_step"},
    "agent-parked-approval.json": {"resolve_snapshot", "run_agent_step"},
    "agent-finalization.json": {"resolve_snapshot", "run_agent_step", "finalize_run"},
    "triggered-sync.json": {"prepare_triggered_task", "sync_external"},
    "engineering-sync.json": {"prepare_triggered_task", "sync_external"},
}

def test_frozen_histories_have_only_phase9_commands() -> None:
    for filename, names in EXPECTED_OLD_ACTIVITIES.items():
        text = (FIXTURE_ROOT / filename).read_text(encoding="utf-8")
        recorded = set(re.findall(r'"activityType"\s*:\s*\{"name"\s*:\s*"([^"]+)"', text))
        assert names.issubset(recorded)
        assert "phase10-tool-worker-boundary-v1" not in text
```

```python
PHASE10_TOOL_WORKER_PATCH = "phase10-tool-worker-boundary-v1"
ACTIVITY_RESOLVE_ADVERTISED_TOOLS = "resolve_advertised_tools"
ACTIVITY_REASON_AGENT_STEP = "reason_agent_step"
ACTIVITY_EXECUTE_BOUND_TOOL = "execute_bound_tool"
ACTIVITY_COMMIT_AGENT_STEP = "commit_agent_step"
ACTIVITY_RESOLVE_BOUND_TOOL_APPROVAL = "resolve_bound_tool_approval"
ACTIVITY_COMMIT_APPROVAL_PROJECTION = "commit_approval_projection"
ACTIVITY_CLEANUP_RUN_WORKSPACE = "cleanup_run_workspace"
ACTIVITY_FINALIZE_RUN_PROJECTION = "finalize_run_projection"

@dataclass(frozen=True)
class AdvertisedTool:
    name: str
    description: str
    parameters: dict[str, Any]

@dataclass
class ResolveAdvertisedToolsInput:
    workspace_id: str
    agent_id: str

@dataclass
class ReasonAgentStepInput(RunStepInput):
    advertised_tools: list[AdvertisedTool] = field(default_factory=list)

@dataclass
class ReasonAgentStepResult:
    call_count: int

@dataclass
class ExecuteBoundToolInput:
    workspace_id: str
    run_id: str
    step_index: int
    ordinal: int

@dataclass
class BoundToolResult:
    tool_call_id: str
    status: str
    approval_id: str | None = None
    stop_reason: str | None = None

@dataclass
class CommitAgentStepInput:
    workspace_id: str
    task_id: str
    run_id: str
    agent_id: str
    step_index: int
    gateway_tool_call_ids: list[str] = field(default_factory=list)

@dataclass
class ResolveBoundToolApprovalInput:
    workspace_id: str
    task_id: str
    run_id: str
    agent_id: str
    approval_id: str

@dataclass
class CommitApprovalProjectionInput(ResolveBoundToolApprovalInput):
    tool_call_id: str = ""

@dataclass
class CleanupRunWorkspaceInput:
    workspace_id: str
    run_id: str

@dataclass
class CleanupRunWorkspaceResult:
    deleted: bool
```

Keep `agent.step.tool_manifest` on its existing allowlisted projection and make the new agent-only event fail closed before any generic deep copy:

```python
_AGENT_ONLY_REASONING_EVENT = "agent.step.reasoning"

if event_type == _AGENT_ONLY_REASONING_EVENT:
    return {}
```

Insert that branch as the first branch inside the existing `public_run_event_payload`, before its generic deep-copy branch, and leave the existing manifest allowlist body intact. The test above is the forward-compatibility gate: an unknown key added later to `agent.step.reasoning` must still serialize as `{}`.

- [ ] **Step 6: Run focused GREEN and frozen-history replay**

```bash
uv run pytest tests/test_capture_phase9_temporal_histories.py apps/api/tests/test_approvals_unit.py -q
uv run pytest packages/workflows/tests/test_phase10_history_replay.py -q
uv run ruff check scripts/capture_phase9_temporal_histories.py tests/test_capture_phase9_temporal_histories.py packages/workflows apps/api/src/jhin_api/public_payloads.py apps/api/tests/test_approvals_unit.py
uv run mypy scripts/capture_phase9_temporal_histories.py tests/test_capture_phase9_temporal_histories.py packages/workflows/src
```

Expected: PASS; the generator unit test uses a real SDK history object and the projection still strips arguments/reasoning.

- [ ] **Step 7: Inspect and commit immutable Phase 9 evidence and contracts**

```bash
git add scripts/capture_phase9_temporal_histories.py tests/test_capture_phase9_temporal_histories.py packages/workflows/tests/fixtures/phase9_temporal packages/workflows/tests/test_phase10_history_replay.py packages/workflows/src/jhin_workflows/task_queues.py packages/workflows/src/jhin_workflows/__init__.py packages/workflows/src/jhin_workflows/agent_task/shared.py apps/api/src/jhin_api/public_payloads.py apps/api/tests/test_approvals_unit.py
git diff --cached --check
git commit -m "test: freeze Phase 9 histories and tool contracts"
```

Expected: all six fixtures parse and replay; every parsed history is reconstructed with its caller-supplied workflow ID; `phase9-ref.txt` is exactly the Task 0 commit; post-bind state has one lossless manifest, no `agent.step.reasoning` event, no `ToolCall`, and zero effects. Never regenerate these files after workflow patching begins.

### Task 2: Split model reasoning and agent-side projections

**Files:**
- Create: `services/agent_worker/src/jhin_agent_worker/reasoning.py`
- Create: `services/agent_worker/src/jhin_agent_worker/projections.py`
- Modify: `services/agent_worker/src/jhin_agent_worker/activities.py`
- Create: `services/agent_worker/tests/test_reasoning_manifest.py`
- Create: `services/agent_worker/tests/test_step_projection.py`
- Create: `services/agent_worker/tests/test_legacy_manifest_sidecar.py`
- Modify: `services/agent_worker/tests/test_phase9_invocation_activity.py`
- Modify: `services/agent_worker/tests/test_approval_activity.py`

**Interfaces:**
- Consumes: `AdvertisedTool`, existing `agent.step.tool_manifest` `RunEvent` (including Phase 9 manifests with no matching reasoning event), existing `ToolCall`/`Approval` rows, existing snapshot/history/model logic, and current transcript/run-event behavior.
- Produces: agent-internal `AgentStepReasoningRecord.from_payload(payload: dict[str, Any], *, expected_step: int, expected_call_count: int) -> AgentStepReasoningRecord`; `AgentReasoningActivities.reason_agent_step_activity(params: ReasonAgentStepInput) -> ReasonAgentStepResult`; `reason_agent_step(params, *, legacy_sidecar_repair: bool = False) -> ReasonAgentStepResult`; `AgentProjectionActivities.commit_agent_step_activity(params: CommitAgentStepInput) -> StepResult`; `commit_approval_projection_activity(params: CommitApprovalProjectionInput) -> StepResult`; `finalize_run_projection_activity(params: FinalizeInput) -> None`. `AgentStepReasoningRecord` and event type `agent.step.reasoning` never leave `jhin_agent_worker`.

- [ ] **Step 1: Write failing reasoning and projection tests**

```python
async def test_reasoning_returns_count_after_atomic_lossless_bind(world: ReasoningWorld) -> None:
    world.model.responses.append(two_call_response())
    result = await world.reasoning.reason_agent_step_activity(world.params)
    assert result == ReasonAgentStepResult(call_count=2)
    assert not hasattr(result, "tool_calls")
    assert not hasattr(result, "text")
    manifest = await world.load_manifest(step=0)
    reasoning = await world.load_reasoning(step=0)
    assert set(manifest.payload_json) == {"step", "manifest"}
    assert [call["arguments_json"] for call in manifest.payload_json["manifest"]["calls"]] == [
        '{"value":"first"}',
        '{"value":"second"}',
    ]
    assert reasoning.payload_json["provider_call_ids"] == ["provider-call-1", "provider-call-2"]
    assert await world.count_events("agent.step.tool_manifest") == 1
    assert await world.count_events("agent.step.reasoning") == 1
    assert world.effect.count == 0


async def test_new_reasoning_bind_rolls_back_manifest_and_reasoning_together(
    world: ReasoningWorld,
) -> None:
    world.model.responses.append(two_call_response())
    world.fail_next_commit(RuntimeError("injected commit failure"))
    with pytest.raises(RuntimeError, match="injected commit failure"):
        await world.reasoning.reason_agent_step_activity(world.params)
    assert await world.count_events("agent.step.tool_manifest") == 0
    assert await world.count_events("agent.step.reasoning") == 0
    assert await world.tool_call_count() == 0
    assert world.effect.count == 0


async def test_projection_is_idempotent_and_unknown_is_durable(world: ProjectionWorld) -> None:
    await world.seed_manifest_and_tool_call(status="execution_unknown")
    with pytest.raises(ApplicationError) as first:
        await world.projections.commit_agent_step_activity(world.commit_params())
    assert first.value.type == "tool_execution_unknown"
    with pytest.raises(ApplicationError):
        await world.projections.commit_agent_step_activity(world.commit_params())
    assert await world.count_events("agent.step.committed") == 1
    assert await world.count_projection_messages() == 2
    assert (await world.load_run()).error_code == "tool_execution_unknown"
```

```python
@pytest.mark.parametrize(
    ("case", "error_type"),
    [("nonlossless", "tool_step_manifest_not_lossless"), ("drift", "tool_step_manifest_drift")],
)
async def test_reasoning_failures_precede_effects(
    world: ReasoningWorld, case: str, error_type: str
) -> None:
    await world.arrange_reasoning_case(case)
    with pytest.raises(ApplicationError) as error:
        await world.reasoning.reason_agent_step_activity(world.params)
    assert error.value.type == error_type
    assert await world.tool_call_count() == 0
    assert world.effect.count == 0
```

The legacy-sidecar test uses the exact database shape documented by `agent-post-bind-pre-effect.json`:

```python
async def test_phase9_manifest_without_reasoning_is_rebound_before_any_effect(world: World) -> None:
    await world.seed_manifest(step=0, calls=[("system.echo", '{"value":"same"}')])
    world.model.responses.append(model_call("replacement-provider-id", "system.echo", {"value": "same"}))
    result = await world.reasoning.reason_agent_step(
        world.params,
        legacy_sidecar_repair=True,
    )
    manifest = await world.load_manifest(step=0)
    reasoning = await world.load_reasoning(step=0)
    assert result.call_count == 1
    assert set(manifest.payload_json) == {"step", "manifest"}
    assert reasoning.payload_json["provider_call_ids"] == ["replacement-provider-id"]
    assert await world.count_events("agent.step.reasoning") == 1
    assert world.effect.count == 0


async def test_phase9_sidecar_repair_rejects_canonical_drift_before_effect(world: World) -> None:
    await world.seed_manifest(step=0, calls=[("system.echo", '{"value":"bound"}')])
    world.model.responses.append(model_call("retry", "system.echo", {"value": "changed"}))
    with pytest.raises(ApplicationError) as error:
        await world.reasoning.reason_agent_step(world.params, legacy_sidecar_repair=True)
    assert error.value.type == "tool_step_manifest_drift"
    assert world.effect.count == 0
    assert await world.count_events("agent.step.reasoning") == 0
```

- [ ] **Step 2: Run RED**

```bash
uv run pytest services/agent_worker/tests/test_reasoning_manifest.py services/agent_worker/tests/test_legacy_manifest_sidecar.py services/agent_worker/tests/test_step_projection.py services/agent_worker/tests/test_phase9_invocation_activity.py services/agent_worker/tests/test_approval_activity.py -q
```

Expected: FAIL because the split activities, legacy-sidecar repair flag, separate agent-only reasoning authority, and manifest-backed projection do not exist.

- [ ] **Step 3: Implement atomic reasoning bind and provider-schema adapter**

Move `_load_history`, provider construction, `execute_step`, cost calculation, and manifest canonicalization into `reasoning.py`. The only adapter from tool DTO to provider DTO is:

```python
def to_model_tool_schemas(tools: list[AdvertisedTool]) -> tuple[ToolSchema, ...]:
    return tuple(
        ToolSchema(name=tool.name, description=tool.description, parameters=tool.parameters)
        for tool in tools
    )
```

Before the model call, load the `agent.step.tool_manifest` and `agent.step.reasoning` events for exactly `(workspace_id, task_id, run_id, step_index)`. Return the manifest count without a model call only when `AgentStepReasoningRecord.from_payload` validates the matching event and its provider-ID count. A manifest without that separate event is legal only when `legacy_sidecar_repair=True`; otherwise raise non-retryable `reasoning_sidecar_missing` because new code always inserts both events atomically.

After the call, reuse `_step_tool_manifest`: strict JSON object parsing, sorted compact canonical JSON, the existing 8,192-character cap, name/provider-ID caps of 200, and value/structural redaction equality. Define the agent-only DTO and its exact storage shape in `reasoning.py`:

```python
BoundedProviderText = Annotated[str, StringConstraints(max_length=200)]

class AgentStepUsage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cached_tokens: int = Field(ge=0)
    cost_micros: int = Field(ge=0)

class AgentStepReasoningRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    format_version: Literal[1] = 1
    step: int = Field(ge=0)
    completion_sanitized: str = Field(max_length=_MAX_MODEL_TEXT_CHARS)
    model: BoundedProviderText
    finish_reason: BoundedProviderText
    provider_request_id: BoundedProviderText
    provider_call_ids: tuple[BoundedProviderText, ...]
    transitions: tuple[dict[str, Any], ...] = Field(max_length=128)
    done: bool
    usage: AgentStepUsage
    latency_ms: int = Field(ge=0)

    def to_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    @classmethod
    def from_payload(
        cls,
        payload: dict[str, Any],
        *,
        expected_step: int,
        expected_call_count: int,
    ) -> AgentStepReasoningRecord:
        try:
            record = cls.model_validate(payload)
        except ValidationError as error:
            raise ApplicationError(
                "agent step reasoning is malformed",
                type="reasoning_sidecar_invalid",
                non_retryable=True,
            ) from error
        if record.step != expected_step or len(record.provider_call_ids) != expected_call_count:
            raise ApplicationError(
                "agent step reasoning does not match its manifest",
                type="reasoning_sidecar_invalid",
                non_retryable=True,
            )
        return record
```

Construct `AgentStepReasoningRecord` with `redact_text` and the exact existing bounds: completion at `_MAX_MODEL_TEXT_CHARS`; model, finish reason, request ID, and every provider call ID at 200; transitions through `sanitize_transition`; nonnegative integer usage/cost/latency fields. After the model returns, call `AGENT_BEFORE_BIND` with `run_id`; then, under the existing `AgentRun FOR UPDATE` lock, reload both event types and the committed marker. For a new step, append consecutive `agent.step.tool_manifest` and `agent.step.reasoning` rows and commit once. Immediately after that commit and before returning, call `PHASE9_AFTER_MANIFEST` with `run_id`. Never put completion, provider IDs, transitions, usage, or latency in the manifest. If either new event already exists without the other, reject `reasoning_bind_incomplete` rather than guessing. A retry that finds both complete events skips the model and bind, but still passes the activity return path; the live crash matrix in Task 10 proves the pre-bind retry calls the fake model twice while the post-bind retry calls it once.

For a Phase 9 manifest without the separate event, rerun model reasoning, require exact equality between `_step_tool_manifest(outcome.tool_calls)` and the already committed canonical manifest, then append only the bounded `agent.step.reasoning` row and commit under the same lock before starting a compatibility tool workflow. Provider IDs may differ and come from this repair response; they are transcript metadata, never manifest identity. If another repair wins the lock, validate and reuse its complete event. Canonical mismatch raises retryable `tool_step_manifest_drift`, leaves all rows unchanged, and executes nothing. Do not create `ToolCall`, approval, connector client, or sandbox client here.

- [ ] **Step 4: Implement ID-loaded projections**

Move transcript/timeline/cost helpers into `projections.py`. Reconstruct each projection from the immutable manifest call entry, its deterministic `ToolCall`, its matching `AgentStepReasoningRecord`, and its optional `Approval`; never accept sanitized output, policy reason, tool name, risk, provider ID, or arguments from `CommitAgentStepInput`. Derive expected IDs with `stable_tool_invocation_id(run_id, step_index, ordinal)` and require the supplied IDs to equal the executed prefix. Permit a shorter prefix only when the final durable row is pending approval, blocking delegation, or execution unknown. Adapt rows to transcript/event fields deterministically: status/error/duration/input/output come from `ToolCall`; approval reason/risk metadata come from the matching `Approval.action_payload_sanitized`; provider call ID comes from the agent-only reasoning record at the same ordinal. The only compatibility fallback is a Phase 9 already-pending approval with no reasoning event, for which `Approval.action_payload_sanitized["provider_call_id"]` is used after bounded string validation; new rows may not use that fallback. Decision code is `approval_required`, `granted`, or the persisted error/status code; non-approval decision reason is a fixed status-to-safe-text mapping, never a fresh policy evaluation. Add one `agent.step.committed` marker in the same transaction as messages, run totals/status, and public timeline events.

`finalize_run_projection_activity` keeps run/task finalization and queue kicks but contains no `delete_sandbox_workspace` import or call. Approval projection loads the decided `Approval`, exact `ToolCall`, matching manifest entry, and separate reasoning record, repairs a missing outer bundle idempotently, and preserves `execution_unknown`.

- [ ] **Step 5: Run GREEN and commit**

```bash
uv run pytest services/agent_worker/tests/test_reasoning_manifest.py services/agent_worker/tests/test_legacy_manifest_sidecar.py services/agent_worker/tests/test_step_projection.py services/agent_worker/tests/test_phase9_invocation_activity.py services/agent_worker/tests/test_approval_activity.py services/agent_worker/tests/test_delegation_activities.py -q
uv run ruff check services/agent_worker
uv run mypy services/agent_worker/src services/agent_worker/tests
git add services/agent_worker/src/jhin_agent_worker/reasoning.py services/agent_worker/src/jhin_agent_worker/projections.py services/agent_worker/src/jhin_agent_worker/activities.py services/agent_worker/tests/test_reasoning_manifest.py services/agent_worker/tests/test_legacy_manifest_sidecar.py services/agent_worker/tests/test_step_projection.py services/agent_worker/tests/test_phase9_invocation_activity.py services/agent_worker/tests/test_approval_activity.py
git commit -m "refactor: split agent reasoning from tool projection"
```

### Task 3: Build tool-worker catalog, ordinary execution, and approval resolution

**Files:**
- Create: `services/tool_worker/pyproject.toml`
- Create: `services/tool_worker/src/jhin_tool_worker/__init__.py`
- Create: `services/tool_worker/src/jhin_tool_worker/settings.py`
- Create: `services/tool_worker/src/jhin_tool_worker/resources.py`
- Create: `services/tool_worker/src/jhin_tool_worker/activities.py`
- Create: `services/tool_worker/src/jhin_tool_worker/main.py`
- Create: `services/tool_worker/tests/test_advertised_tools.py`
- Create: `services/tool_worker/tests/test_bound_tool_execution.py`
- Create: `services/tool_worker/tests/test_bound_approval.py`
- Modify: `packages/tools/src/jhin_tools/builtin.py`
- Modify: `packages/tools/src/jhin_tools/__init__.py`
- Modify: `packages/connectors/src/jhin_connectors/base.py`
- Modify: `packages/connectors/src/jhin_connectors/registry.py`
- Modify: `packages/connectors/src/jhin_connectors/__init__.py`
- Modify: `packages/connectors/src/jhin_connectors/github/connector.py`
- Modify: `packages/connectors/src/jhin_connectors/cli/connector.py`
- Modify: `packages/connectors/src/jhin_connectors/linear/connector.py`
- Modify: `packages/connectors/src/jhin_connectors/vercel/connector.py`
- Modify: `packages/connectors/src/jhin_connectors/supabase/connector.py`
- Modify: `packages/connectors/src/jhin_connectors/example/connector.py`
- Modify: `packages/connectors/tests/test_manifest_registry.py`
- Modify: `apps/api/src/jhin_api/policy/router.py`
- Modify: `apps/api/tests/test_policy_unit.py`
- Modify: `pyproject.toml`
- Modify: `uv.lock`

**Interfaces:**
- Consumes: `build_default_catalog()`, `allowed_tool_definitions`, live `AgentCapabilityGrant`, scalar JSON paths from existing manifest `RunEvent` entries, `ToolGateway`, existing `ToolCall`/`Approval` claims, and current approval authority. It never consumes `agent.step.reasoning` or the whole `RunEvent.payload_json` column.
- Produces: `ToolDefinitionCatalog`; `build_default_definition_catalog() -> ToolDefinitionCatalog`; internal scalar-only `BoundManifestEntry`; `bound_manifest_entry_statement(params: ExecuteBoundToolInput) -> Select[tuple[int | None, bool | None, str | None, str | None]]`; `ToolActivities.resolve_advertised_tools_activity(params: ResolveAdvertisedToolsInput) -> list[AdvertisedTool]`; `execute_bound_tool_activity(params: ExecuteBoundToolInput) -> BoundToolResult`; `resolve_bound_tool_approval_activity(params: ResolveBoundToolApprovalInput) -> BoundToolResult`; existing deterministic `ToolCall`/`Approval` rows as the only gateway outcome authority; a workspace-registered `jhin-tool-worker` package.

- [ ] **Step 1: Write failing catalog and ordinary-call tests**

```python
async def test_advertise_then_execute_only_the_bound_ordinal(world: ToolWorld) -> None:
    advertised = await world.activities.resolve_advertised_tools_activity(
        ResolveAdvertisedToolsInput(
            workspace_id=str(world.workspace.id), agent_id=str(world.agent.id)
        )
    )
    assert [tool.name for tool in advertised] == ["system.echo", "linear.issue.get"]
    assert advertised[0].parameters["type"] == "object"
    params = ExecuteBoundToolInput(
        workspace_id=str(world.workspace.id),
        run_id=str(world.run.id),
        step_index=2,
        ordinal=0,
    )
    assert set(vars(params)) == {"workspace_id", "run_id", "step_index", "ordinal"}
    result = await world.activities.execute_bound_tool_activity(params)
    assert result.tool_call_id == str(stable_tool_invocation_id(world.run.id, 2, 0))
    assert world.effect.count == 1


@pytest.mark.parametrize(
    "case",
    ["revoked_grant", "disabled_connection", "unknown_tool", "invalid_arguments",
     "wrong_workspace", "wrong_ordinal"],
)
async def test_invalid_live_or_bound_state_stops_before_effect(
    world: ToolWorld, case: str
) -> None:
    await world.arrange_invalid_case(case)
    with pytest.raises(ApplicationError):
        await world.activities.execute_bound_tool_activity(world.execute_params())
    assert world.effect.count == 0
```

```python
async def test_two_bound_calls_execute_in_manifest_order(world: ToolWorld) -> None:
    await world.seed_two_call_manifest(values=["first", "second"])
    results = [
        await world.activities.execute_bound_tool_activity(world.execute_params(ordinal=ordinal))
        for ordinal in (0, 1)
    ]
    assert [result.tool_call_id for result in results] == [
        str(stable_tool_invocation_id(world.run.id, 2, ordinal)) for ordinal in (0, 1)
    ]
    assert world.effect.values == ["first", "second"]


def test_manifest_statement_projects_only_requested_call_scalars(world: ToolWorld) -> None:
    statement = bound_manifest_entry_statement(world.execute_params(ordinal=1))
    assert tuple(column.key for column in statement.selected_columns) == (
        "ordinal",
        "lossless",
        "tool_name",
        "arguments_json",
    )
    assert all(column is not RunEvent.payload_json for column in statement.selected_columns)


async def test_execution_never_reads_agent_reasoning_event(world: ToolWorld) -> None:
    await world.seed_manifest(step=2, ordinal=0, value="ordinary")
    await world.seed_private_reasoning_event({
        "completion_sanitized": "must-not-enter-tool-process",
        "provider_call_ids": ["must-not-enter-tool-process"],
        "transitions": [{"private": "must-not-enter-tool-process"}],
        "usage": {"input_tokens": 99},
    })
    await world.activities.execute_bound_tool_activity(world.execute_params())
    assert world.selected_run_event_types == ["agent.step.tool_manifest"]
    assert world.selected_run_event_columns == [
        "ordinal", "lossless", "tool_name", "arguments_json"
    ]
    assert "must-not-enter-tool-process" not in world.process_observations
```

Use the Task 0 crash-barrier integration for pre-claim, post-claim/pre-effect, and post-effect/pre-commit gaps rather than a sleep-based unit test.

Add an API/catalog ownership test with an executable-builder bomb:

```python
async def test_tools_endpoint_uses_definition_only_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        jhin_connectors.registry,
        "build_default_catalog",
        lambda: pytest.fail("API attempted executable catalog construction"),
    )
    tools = await list_tools(ctx=None)  # type: ignore[arg-type]
    assert {tool.name for tool in tools} >= {"system.echo", "linear.issue.get"}


@pytest.mark.parametrize(
    ("setting", "value"),
    [
        ("JHIN_TEST_CRASH_BARRIER_DIR", "/run/jhin/test-barriers"),
        ("JHIN_TEST_CRASH_BARRIER_NAME", TOOL_AFTER_CLAIM),
        ("JHIN_TEST_CRASH_BARRIER_MATCH", "018f4d52-8b93-7d41-8ac7-7f190f091111"),
    ],
)
def test_tool_worker_rejects_crash_barrier_in_production(
    setting: str, value: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv(setting, value)
    with pytest.raises(ValidationError, match="test crash barriers are forbidden"):
        ToolWorkerSettings()
```

- [ ] **Step 2: Write failing approval-boundary tests**

```python
async def test_approval_resolution_reloads_database_authority_and_identity(
    world: ToolWorld,
) -> None:
    parked = await world.activities.execute_bound_tool_activity(world.execute_params())
    await world.approve_in_database(parked.approval_id)
    await world.rotate_connection()
    result = await world.activities.resolve_bound_tool_approval_activity(
        world.approval_params(parked.approval_id)
    )
    assert result.tool_call_id == parked.tool_call_id
    assert result.status == "denied"
    assert world.effect.count == 0


async def test_ambiguous_approved_effect_returns_durable_unknown(world: ToolWorld) -> None:
    parked = await world.park_and_approve()
    await world.seed_executing_claim(parked.tool_call_id)
    first = await world.activities.resolve_bound_tool_approval_activity(
        world.approval_params(parked.approval_id)
    )
    second = await world.activities.resolve_bound_tool_approval_activity(
        world.approval_params(parked.approval_id)
    )
    assert first == second == BoundToolResult(
        tool_call_id=parked.tool_call_id,
        status="execution_unknown",
        approval_id=parked.approval_id,
        stop_reason="execution_unknown",
    )
    assert world.effect.count == 0
```

- [ ] **Step 3: Run RED**

```bash
uv run pytest services/tool_worker/tests packages/connectors/tests/test_manifest_registry.py apps/api/tests/test_policy_unit.py -q
```

Expected: FAIL because `jhin-tool-worker`, `ToolDefinitionCatalog`, and the definition-only API path do not exist.

- [ ] **Step 4: Implement catalog ownership and durable-row loading**

Separate definitions from executor registration:

```python
class ToolDefinitionCatalog:
    def __init__(self) -> None:
        self._registry = CapabilityRegistry()

    def register(self, definition: ToolDefinition) -> None:
        self._registry.register(definition)

    def definitions(self) -> tuple[ToolDefinition, ...]:
        return tuple(self._registry)

def build_default_definition_catalog(
    registry: ConnectorRegistry | None = None,
) -> ToolDefinitionCatalog:
    catalog = ToolDefinitionCatalog()
    for definition in builtin_tool_definitions():
        catalog.register(definition)
    for connector in registry if registry is not None else default_registry():
        for definition in connector.tool_definitions():
            catalog.register(definition)
    return catalog

def builtin_tool_definitions() -> tuple[ToolDefinition, ...]:
    from jhin_tools.organization import ORGANIZATION_TOOLS
    return tuple(definition for definition, _executor in BUILTIN_TOOLS) + tuple(
        definition for definition, _executor, _validator in ORGANIZATION_TOOLS
    )
```

Add `Connector.tool_definitions() -> tuple[ToolDefinition, ...]` and implement it for every connector class listed in this task without returning executor callables. `build_default_catalog` remains the executable pairing path; new tool-worker constructs it once in `jhin_tool_worker.main` and injects it into `ToolActivities`. API `/tools` calls `build_default_definition_catalog`. Task 5 removes the temporary Phase 9 legacy agent call when its compatibility coordinator lands, and Task 6's AST gate then proves tool-worker is the only runtime caller.

- [ ] **Step 5: Implement manifest-loaded ordinary and approval activities**

Create `ToolActivities(resources, catalog)` and build `catalog = build_default_catalog()` only in tool-worker startup/tests. `resolve_advertised_tools` reads live grants and returns ordered `AdvertisedTool` values. `execute_bound_tool` performs this exact scalar-only lookup before creating a gateway. It must not call `session.get` with `RunEvent`, select the `RunEvent` ORM entity, select bare `RunEvent.payload_json`, scan all events for a run, mention `agent.step.reasoning`, or import any agent reasoning DTO:

```python
@dataclass(frozen=True)
class BoundManifestEntry:
    ordinal: int
    lossless: bool
    tool_name: str
    arguments_json: str

def bound_manifest_entry_statement(
    params: ExecuteBoundToolInput,
) -> Select[tuple[int | None, bool | None, str | None, str | None]]:
    requested = RunEvent.payload_json["manifest"]["calls"][params.ordinal]
    return select(
        requested["ordinal"].as_integer().label("ordinal"),
        requested["lossless"].as_boolean().label("lossless"),
        requested["tool_name"].as_string().label("tool_name"),
        requested["arguments_json"].as_string().label("arguments_json"),
    ).where(
        RunEvent.workspace_id == UUID(params.workspace_id),
        RunEvent.run_id == UUID(params.run_id),
        RunEvent.event_type == "agent.step.tool_manifest",
        RunEvent.payload_json["step"].as_integer() == params.step_index,
    ).limit(2)

async def _load_bound_call(
    session: AsyncSession,
    params: ExecuteBoundToolInput,
) -> BoundManifestEntry:
    rows = (await session.execute(bound_manifest_entry_statement(params))).tuples().all()
    if len(rows) != 1:
        raise ApplicationError(
            "bound tool manifest entry not found",
            type="bound_tool_not_found",
            non_retryable=True,
        )
    ordinal, lossless, tool_name, arguments_json = rows[0]
    if (
        ordinal != params.ordinal
        or lossless is not True
        or not isinstance(tool_name, str)
        or len(tool_name) > 200
        or not isinstance(arguments_json, str)
        or len(arguments_json) > 8_192
    ):
        raise ApplicationError(
            "bound tool manifest entry is malformed",
            type="bound_tool_invalid",
            non_retryable=True,
        )
    return BoundManifestEntry(ordinal, lossless, tool_name, arguments_json)

entry = await _load_bound_call(session, params)
invocation_id = stable_tool_invocation_id(UUID(params.run_id), params.step_index, params.ordinal)
result = await ToolGateway(context, self._catalog).request(
    entry.tool_name,
    entry.arguments_json,
    invocation_id=invocation_id,
)
```

Do not pass `provider_call_id` into `ToolGateway.request` from Phase 10. New transcript stitching gets that value from the separate agent-only reasoning event during agent projection, and approval execution does not need it. Existing Phase 9 approval payloads retain their bounded provider ID for the compatibility-only fallback defined in Task 2.

After `ToolGateway.request` returns (including an existing-invocation replay), commit the gateway session and return only the durable tool-call ID plus bounded status/approval/stop routing. Do not add an outcome table, outcome `RunEvent`, or duplicate policy columns: `ToolCall` already holds sanitized input/output, status, duration, error, and approval binding; `Approval` holds its sanitized decision context. Map blocking `organization.delegate_task` output to `stop_reason="blocking_delegation"`; map approval and unknown states similarly. The activity verifies `result.tool_call_id == invocation_id` before returning and turns `invocation_mismatch` into the current non-retryable run-stop behavior.

`resolve_bound_tool_approval` accepts IDs only, reloads approval/tool/manifest/context, calls `resolve_approved` or `resolve_rejected`, commits the existing rows, and returns a `BoundToolResult`. It does not write transcript or run events.

- [ ] **Step 6: Join the uv workspace and lock before running service tests**

Add `services/tool_worker` to all four root registrations—`tool.uv.workspace.members`, `tool.ruff.src`, `tool.mypy.files`, and `tool.pytest.ini_options.testpaths`. Use this service package contract:

```toml
[project]
name = "jhin-tool-worker"
version = "0.1.0"
requires-python = ">=3.13"
dependencies = [
  "temporalio>=1.31",
  "pydantic>=2.10",
  "pydantic-settings>=2.7",
  "nats-py>=2.13",
  "jhin-connectors",
  "jhin-db",
  "jhin-domain",
  "jhin-events",
  "jhin-observability",
  "jhin-secrets",
  "jhin-tools",
  "jhin-workflows",
]

[tool.uv.sources]
jhin-connectors = { workspace = true }
jhin-db = { workspace = true }
jhin-domain = { workspace = true }
jhin-events = { workspace = true }
jhin-observability = { workspace = true }
jhin-secrets = { workspace = true }
jhin-tools = { workspace = true }
jhin-workflows = { workspace = true }

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/jhin_tool_worker"]
```

Tool settings include `app_env: str = Field(default="development", validation_alias="APP_ENV")` and the three Task 0 barrier fields and reject each individual control in production with the same validator. Then run `uv lock`; do not defer membership or lock changes to worker registration.
`ToolWorkerResources.create` builds the Task 0 `CrashBarrier` from those settings, and `ToolActivities` passes it into every gateway context; this is how the integration harness reaches both exact tool failpoints without a production endpoint.

- [ ] **Step 7: Run GREEN and commit every Task 3 path**

```bash
uv lock
uv run pytest services/tool_worker/tests packages/tools/tests/test_gateway.py packages/tools/tests/test_gateway_concurrency.py -q
uv run pytest packages/connectors/tests/test_manifest_registry.py apps/api/tests/test_policy_unit.py -q
uv run ruff check services/tool_worker packages/tools/src/jhin_tools/builtin.py packages/connectors apps/api/src/jhin_api/policy/router.py apps/api/tests/test_policy_unit.py
uv run mypy services/tool_worker/src packages/connectors/src apps/api/src/jhin_api/policy/router.py
git add services/tool_worker packages/tools/src/jhin_tools/builtin.py packages/tools/src/jhin_tools/__init__.py packages/connectors/src/jhin_connectors/base.py packages/connectors/src/jhin_connectors/registry.py packages/connectors/src/jhin_connectors/__init__.py packages/connectors/src/jhin_connectors/github/connector.py packages/connectors/src/jhin_connectors/cli/connector.py packages/connectors/src/jhin_connectors/linear/connector.py packages/connectors/src/jhin_connectors/vercel/connector.py packages/connectors/src/jhin_connectors/supabase/connector.py packages/connectors/src/jhin_connectors/example/connector.py packages/connectors/tests/test_manifest_registry.py apps/api/src/jhin_api/policy/router.py apps/api/tests/test_policy_unit.py pyproject.toml uv.lock
git commit -m "feat: add deterministic tool execution worker"
```

### Task 4: Patch new workflow routing and preserve old history commands

**Files:**
- Modify: `packages/workflows/src/jhin_workflows/agent_task/workflows.py`
- Create: `packages/workflows/tests/test_agent_task_tool_routing.py`
- Modify: `packages/workflows/tests/test_agent_task_delegation.py`
- Modify: `packages/workflows/tests/test_phase10_history_replay.py`

**Interfaces:**
- Consumes: all Task 1 DTOs and queues plus Task 2/3 activity contracts.
- Produces: patched resolve → reason → ordered execute → commit orchestration, workflow-owned approval wait, explicit cleanup-before-finalize routing, and unchanged old commands on replay.

- [ ] **Step 1: Write failing two-queue orchestration tests**

Run one `Worker` on an agent test queue and one on `TOOL_TASK_QUEUE`. Capture activity info and assert the exact sequence and queue ownership:

```python
async def test_new_history_routes_each_boundary_to_its_owner(two_queue_world: World) -> None:
    await two_queue_world.run_one_step()
    calls = two_queue_world.activity_calls
    assert calls == [
        ("resolve_snapshot", two_queue_world.agent_queue),
        ("resolve_advertised_tools", TOOL_TASK_QUEUE),
        ("reason_agent_step", two_queue_world.agent_queue),
        ("execute_bound_tool", TOOL_TASK_QUEUE),
        ("commit_agent_step", two_queue_world.agent_queue),
        ("cleanup_run_workspace", TOOL_TASK_QUEUE),
        ("finalize_run_projection", two_queue_world.agent_queue),
    ]
```

```python
@pytest.mark.parametrize(
    ("scenario", "last_effect_activity"),
    [("zero_calls", None), ("approval", "resolve_bound_tool_approval"),
     ("blocking_delegation", "execute_bound_tool"),
     ("execution_unknown", "execute_bound_tool"),
     ("cancellation", "cleanup_run_workspace"),
     ("cleanup_failure", "cleanup_run_workspace")],
)
async def test_stop_scenarios_never_schedule_a_later_ordinal(
    two_queue_world: World, scenario: str, last_effect_activity: str | None
) -> None:
    await two_queue_world.run_scenario(scenario)
    assert two_queue_world.executed_ordinals == two_queue_world.expected_prefix(scenario)
    if last_effect_activity is not None:
        assert two_queue_world.effect_activity_names[-1] == last_effect_activity
    assert all(queue == TOOL_TASK_QUEUE for _, queue in two_queue_world.effect_calls)
```

- [ ] **Step 2: Run RED and verify the old single-activity route**

```bash
uv run pytest packages/workflows/tests/test_agent_task_tool_routing.py packages/workflows/tests/test_agent_task_delegation.py packages/workflows/tests/test_phase10_history_replay.py -q
```

Expected: FAIL because new histories still schedule `run_agent_step` on the agent queue and the patch constant/new activity route is unused; frozen Phase 9 replay remains green.

- [ ] **Step 3: Implement the Temporal 1.31-compatible patch branch**

Evaluate the patch once after snapshot admission; this placement preserves already-recorded snapshot commands. Never use a version API not present in 1.31.

```python
use_tool_worker = workflow.patched(PHASE10_TOOL_WORKER_PATCH)

if use_tool_worker:
    advertised = await workflow.execute_activity(
        ACTIVITY_RESOLVE_ADVERTISED_TOOLS,
        ResolveAdvertisedToolsInput(params.workspace_id, params.agent_id),
        result_type=list[AdvertisedTool],
        task_queue=TOOL_TASK_QUEUE,
        start_to_close_timeout=timedelta(seconds=30),
        retry_policy=_STEP_RETRY,
    )
    reasoned = await workflow.execute_activity(
        ACTIVITY_REASON_AGENT_STEP,
        ReasonAgentStepInput(
            workspace_id=params.workspace_id,
            task_id=params.task_id,
            run_id=snapshot.run_id,
            agent_id=params.agent_id,
            snapshot_json=snapshot.snapshot_json,
            step_index=self._steps_used,
            instruction=params.instruction,
            user_instructions=instructions,
            advertised_tools=advertised,
        ),
        result_type=ReasonAgentStepResult,
        task_queue=AGENT_TASK_QUEUE,
        start_to_close_timeout=timedelta(minutes=10),
        retry_policy=_STEP_RETRY,
    )
    tool_ids: list[str] = []
    for ordinal in range(reasoned.call_count):
        bound = await workflow.execute_activity(
            ACTIVITY_EXECUTE_BOUND_TOOL,
            ExecuteBoundToolInput(params.workspace_id, snapshot.run_id, self._steps_used, ordinal),
            result_type=BoundToolResult,
            task_queue=TOOL_TASK_QUEUE,
            start_to_close_timeout=timedelta(minutes=10),
            retry_policy=_STEP_RETRY,
        )
        tool_ids.append(bound.tool_call_id)
        if bound.stop_reason is not None:
            break
    step = await workflow.execute_activity(
        ACTIVITY_COMMIT_AGENT_STEP,
        CommitAgentStepInput(
            params.workspace_id,
            params.task_id,
            snapshot.run_id,
            params.agent_id,
            self._steps_used,
            tool_ids,
        ),
        result_type=StepResult,
        task_queue=AGENT_TASK_QUEUE,
        start_to_close_timeout=timedelta(seconds=30),
        retry_policy=_FINALIZE_RETRY,
    )
else:
    step = await workflow.execute_activity(
        ACTIVITY_RUN_AGENT_STEP,
        RunStepInput(
            workspace_id=params.workspace_id,
            task_id=params.task_id,
            run_id=snapshot.run_id,
            agent_id=params.agent_id,
            snapshot_json=snapshot.snapshot_json,
            step_index=self._steps_used,
            instruction=params.instruction,
            user_instructions=instructions,
        ),
        result_type=StepResult,
        start_to_close_timeout=timedelta(minutes=10),
        retry_policy=_STEP_RETRY,
    )
```

After an approval signal, the new branch executes `resolve_bound_tool_approval` on `TOOL_TASK_QUEUE`, then `commit_approval_projection` on `AGENT_TASK_QUEUE`; the old branch schedules the exact old `resolve_approval` command. Before new finalization, best-effort cleanup runs on the tool queue, then `finalize_run_projection` runs on the agent queue. Old histories schedule only old `finalize_run`.

- [ ] **Step 4: Replay frozen histories and run GREEN**

```python
@pytest.mark.parametrize("fixture", sorted(FIXTURE_ROOT.glob("*.json")))
async def test_phase9_history_replays_with_phase10_workflows(fixture: Path) -> None:
    history = WorkflowHistory.from_json(fixture.stem, fixture.read_text(encoding="utf-8"))
    await Replayer(
        workflows=[AgentTaskWorkflow, TriggeredTaskWorkflow, EngineeringTicketWorkflow]
    ).replay_workflow(history)
```

```bash
uv run pytest packages/workflows/tests/test_agent_task_tool_routing.py packages/workflows/tests/test_agent_task_delegation.py packages/workflows/tests/test_phase10_history_replay.py -q
git add packages/workflows/src/jhin_workflows/agent_task/workflows.py packages/workflows/tests/test_agent_task_tool_routing.py packages/workflows/tests/test_agent_task_delegation.py packages/workflows/tests/test_phase10_history_replay.py
git commit -m "feat: route agent tools through dedicated queue"
```

### Task 5: Add stable-ID compatibility coordinators, trigger sync, and cleanup

**Files:**
- Create: `packages/workflows/src/jhin_workflows/tool_compat/__init__.py`
- Create: `packages/workflows/src/jhin_workflows/tool_compat/shared.py`
- Create: `packages/workflows/src/jhin_workflows/tool_compat/workflows.py`
- Create: `services/agent_worker/src/jhin_agent_worker/compatibility.py`
- Modify: `services/agent_worker/src/jhin_agent_worker/trigger_activities.py`
- Create: `services/tool_worker/src/jhin_tool_worker/trigger_activities.py`
- Create: `services/tool_worker/src/jhin_tool_worker/cleanup_activities.py`
- Create: `packages/workflows/tests/test_tool_compat_workflows.py`
- Create: `services/agent_worker/tests/test_compatibility_coordinators.py`
- Create: `services/tool_worker/tests/test_trigger_sync_and_cleanup.py`
- Modify: `packages/tools/src/jhin_tools/invocation.py`
- Modify: `packages/tools/src/jhin_tools/__init__.py`
- Modify: `packages/tools/tests/test_invocation.py`
- Modify: `packages/workflows/src/jhin_workflows/triggered_task/shared.py`
- Modify: `packages/workflows/src/jhin_workflows/triggered_task/workflows.py`
- Modify: `packages/workflows/tests/test_triggered_task_workflow.py`
- Modify: `packages/workflows/src/jhin_workflows/engineering_ticket/shared.py`
- Modify: `packages/workflows/src/jhin_workflows/engineering_ticket/workflows.py`
- Modify: `packages/workflows/tests/test_engineering_ticket_workflow.py`

**Interfaces:**
- Consumes: old activity payloads on agent-worker; new reason/projection helpers; current trigger standing authority; sandbox delete endpoint.
- Produces: stable-ID compatibility workflows and IDs; `stable_sync_invocation_id`; `sync_external_tool`; `cleanup_run_workspace`; separate trigger/engineering patch routes.

- [ ] **Step 1: Write failing compatibility and deterministic-ID tests**

```python
def test_compatibility_ids_are_exact(run_id: str, approval_id: str) -> None:
    assert compatibility_workflow_id("advertised", run_id, step_index=4) == f"phase10-compat-advertised-{run_id}-4"
    assert compatibility_workflow_id("tool-step", run_id, step_index=4) == f"phase10-compat-tool-step-{run_id}-4"
    assert compatibility_workflow_id("approval", approval_id) == f"phase10-compat-approval-{approval_id}"
    assert compatibility_workflow_id("sync", run_id) == f"phase10-compat-sync-{run_id}"
    assert compatibility_workflow_id("cleanup", run_id) == f"phase10-compat-cleanup-{run_id}"


async def test_legacy_post_bind_activity_adds_reasoning_event_then_uses_tool_queue(
    compatibility_world: CompatibilityWorld,
) -> None:
    await compatibility_world.seed_phase9_manifest_without_reasoning_event()
    await compatibility_world.run_legacy_step_handler()
    assert await compatibility_world.count_events("agent.step.reasoning") == 1
    assert set((await compatibility_world.load_manifest()).payload_json) == {"step", "manifest"}
    assert compatibility_world.agent_effect_count == 0
    assert compatibility_world.tool_effect_count == 1
```

```python
async def test_legacy_retry_reattaches_without_local_effect(
    compatibility_world: CompatibilityWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("jhin_connectors.build_default_catalog", local_effect_forbidden)
    monkeypatch.setattr("jhin_connectors.cli.runner_client.delete_workspace", local_effect_forbidden)
    compatibility_world.client.start_workflow.side_effect = WorkflowAlreadyStartedError()
    compatibility_world.client.get_workflow_handle.return_value.result = AsyncMock(
        return_value=[compatibility_world.tool_call_id]
    )
    result = await compatibility_world.run_legacy_step_handler()
    compatibility_world.client.get_workflow_handle.assert_called_once_with(
        compatibility_workflow_id("tool-step", compatibility_world.run_id, step_index=0)
    )
    assert result.done is False
    assert await compatibility_world.committed_tool_call_ids() == [
        compatibility_world.tool_call_id
    ]
    assert compatibility_world.agent_effect_count == 0


async def test_sync_reloads_authority_and_replays_one_claim(sync_world: SyncWorld) -> None:
    first = await sync_world.activities.sync_external_tool_activity(sync_world.ids_only_params)
    second = await sync_world.activities.sync_external_tool_activity(sync_world.ids_only_params)
    assert first == second == SyncExternalResult(synced=True, detail=sync_world.comment_url)
    assert sync_world.comment_count == 1
    assert await sync_world.tool_call_id() == stable_sync_invocation_id(sync_world.run_id)


async def test_cleanup_uses_run_workspace_name_once(cleanup_world: CleanupWorld) -> None:
    first = await cleanup_world.activities.cleanup_run_workspace_activity(cleanup_world.params)
    second = await cleanup_world.activities.cleanup_run_workspace_activity(cleanup_world.params)
    assert first == CleanupRunWorkspaceResult(deleted=True)
    assert second == CleanupRunWorkspaceResult(deleted=False)
    assert cleanup_world.deleted_names == [f"run-{cleanup_world.run_id}"]
```

- [ ] **Step 2: Run RED and verify compatibility symbols are absent**

```bash
uv run pytest packages/workflows/tests/test_tool_compat_workflows.py services/agent_worker/tests/test_compatibility_coordinators.py services/tool_worker/tests/test_trigger_sync_and_cleanup.py -q
```

Expected: FAIL because compatibility workflow IDs/classes, legacy reasoning-event coordination, tool sync, and cleanup activities do not exist.

- [ ] **Step 3: Define dependency-light compatibility workflows**

Use stdlib-only dataclasses and one exact ID formatter:

```python
CompatibilityKind = Literal["advertised", "tool-step", "approval", "sync", "cleanup"]

@dataclass
class AdvertisedCompatibilityInput:
    workspace_id: str
    agent_id: str

@dataclass
class ToolStepCompatibilityInput:
    workspace_id: str
    run_id: str
    step_index: int
    call_count: int

@dataclass
class ApprovalCompatibilityInput(ResolveBoundToolApprovalInput):
    pass

@dataclass
class SyncExternalToolInput:
    workspace_id: str
    task_id: str
    run_id: str

def compatibility_workflow_id(
    kind: CompatibilityKind,
    identity: str,
    *,
    step_index: int | None = None,
) -> str:
    suffix = f"-{step_index}" if step_index is not None else ""
    return f"phase10-compat-{kind}-{UUID(identity)}{suffix}"
```

`AdvertisedToolsCompatibilityWorkflow` calls only `resolve_advertised_tools`; `ToolStepCompatibilityWorkflow` loops `execute_bound_tool`; `ApprovalCompatibilityWorkflow` calls `resolve_bound_tool_approval`; `SyncExternalCompatibilityWorkflow` calls `sync_external_tool`; `CleanupCompatibilityWorkflow` calls `cleanup_run_workspace`. Every workflow runs on the tool queue and receives only UUID strings plus step/ordinal/count integers. The agent legacy `run_agent_step` coordinator first obtains advertised DTOs through the advertised compatibility workflow, calls `reason_agent_step(..., legacy_sidecar_repair=True)`, then starts tool-step compatibility with run ID, step index, and committed call count, and finally calls the local projection helper with the resulting deterministic tool-call IDs. This is the only caller allowed to add a missing `agent.step.reasoning` record for a Phase 9 manifest; the manifest itself remains unchanged.

The old `resolve_approval` handler validates the legacy IDs, reattaches `ApprovalCompatibilityWorkflow`, then calls `commit_approval_projection`; old `sync_external` validates workspace/task/run and reattaches `SyncExternalCompatibilityWorkflow`; old `finalize_run` reattaches `CleanupCompatibilityWorkflow` before calling `finalize_run_projection`. None imports an executor or runner client.

Use this reattach helper in an activity, where Temporal client I/O is allowed:

```python
async def compatibility_result(
    client: Client,
    workflow: Callable[..., Any],
    arg: Any,
    *,
    workflow_id: str,
) -> Any:
    try:
        handle = await client.start_workflow(
            workflow,
            arg,
            id=workflow_id,
            task_queue=TOOL_TASK_QUEUE,
        )
    except WorkflowAlreadyStartedError:
        handle = client.get_workflow_handle(workflow_id)
    return await handle.result()
```

- [ ] **Step 4: Move trigger sync and cleanup effects**

Define the sync invocation key alongside the existing tool key, without changing the tool-call format or schema:

```python
SYNC_INVOCATION_FORMAT_VERSION = 1
SYNC_INVOCATION_NAMESPACE = UUID("3dc26b04-1af9-5ec5-a0ea-d7d95c3a393b")

def stable_sync_invocation_id(run_id: UUID) -> UUID:
    return uuid5(SYNC_INVOCATION_NAMESPACE, f"v1:{run_id.hex}:trigger-sync")
```

Tool-worker reloads `Task.trigger_id`, `Trigger`, enabled `comment_back`, connection status/credentials, run status, external source/ID, and agent from PostgreSQL. Claim an existing `ToolCall` row under `stable_sync_invocation_id(UUID(run_id))` with tool name `system.trigger.sync_external` before the comment effect; a prior terminal claim replays, and a prior `executing` claim becomes `execution_unknown` without reposting. Commit safe timeline/audit/NATS projection with the terminal claim. Do not use the agent's capability grants: the trigger's audited `comment_back` configuration remains the standing authority.

Define `cleanup_run_workspace(CleanupRunWorkspaceInput)` to call `delete_workspace(f"run-{run_id}")`; deletion is idempotent/best-effort and returns `CleanupRunWorkspaceResult`. No agent module imports the runner client.

- [ ] **Step 5: Patch trigger and engineering sync independently**

Use exact stable IDs:

```python
PHASE10_TRIGGER_SYNC_PATCH = "phase10-trigger-sync-tool-routing-v1"
PHASE10_ENGINEERING_SYNC_PATCH = "phase10-engineering-sync-tool-routing-v1"
```

At each existing sync call site, use `workflow.patched` around only that command. New histories schedule `sync_external_tool` with stable IDs and `task_queue=TOOL_TASK_QUEUE`; old histories schedule the recorded `sync_external` name with its old payload/queue. The old agent handler ignores payload arguments as authority, validates their IDs, and coordinates `SyncExternalCompatibilityWorkflow`.

- [ ] **Step 6: Run replay/runtime GREEN and commit**

```bash
uv run pytest packages/workflows/tests/test_tool_compat_workflows.py packages/workflows/tests/test_triggered_task_workflow.py packages/workflows/tests/test_engineering_ticket_workflow.py packages/workflows/tests/test_phase10_history_replay.py services/agent_worker/tests/test_compatibility_coordinators.py services/tool_worker/tests/test_trigger_sync_and_cleanup.py -q
git add packages/workflows/src/jhin_workflows/tool_compat packages/workflows/src/jhin_workflows/triggered_task packages/workflows/src/jhin_workflows/engineering_ticket packages/workflows/tests/test_tool_compat_workflows.py packages/workflows/tests/test_triggered_task_workflow.py packages/workflows/tests/test_engineering_ticket_workflow.py packages/workflows/tests/test_phase10_history_replay.py packages/tools/src/jhin_tools/invocation.py packages/tools/src/jhin_tools/__init__.py packages/tools/tests/test_invocation.py services/agent_worker/src/jhin_agent_worker/compatibility.py services/agent_worker/src/jhin_agent_worker/trigger_activities.py services/agent_worker/tests/test_compatibility_coordinators.py services/tool_worker/src/jhin_tool_worker/trigger_activities.py services/tool_worker/src/jhin_tool_worker/cleanup_activities.py services/tool_worker/tests/test_trigger_sync_and_cleanup.py
git commit -m "feat: preserve tool effects across workflow upgrades"
```

### Task 6: Register workers and enforce distribution dependency boundaries

**Files:**
- Modify: `services/tool_worker/src/jhin_tool_worker/main.py`
- Create: `services/tool_worker/tests/test_worker_registration.py`
- Modify: `services/agent_worker/src/jhin_agent_worker/main.py`
- Modify: `services/agent_worker/pyproject.toml`
- Modify: `services/tool_worker/pyproject.toml`
- Modify: `packages/workflows/pyproject.toml`
- Create: `packages/workflows/src/jhin_workflows/poller_health.py`
- Create: `packages/workflows/tests/test_poller_health.py`
- Modify: `docker/python.Dockerfile`
- Create: `tests/test_worker_dependency_boundaries.py`
- Create: `tests/test_executable_catalog_boundary.py`

**Interfaces:**
- Consumes: all activity/workflow classes from Tasks 2–5.
- Produces: `jhin-tool-worker` console script; agent/tool registration sets; Temporal poller health CLI; package-level negative dependency gates.

- [ ] **Step 1: Write failing registration and import-boundary tests**

Assert agent registration includes reasoning, commit, and all legacy names but excludes tool activities; tool registration includes catalog/execution/approval/sync/cleanup and all compatibility workflows. Parse project metadata and AST imports:

```python
TOOL_ACTIVITY_NAMES = {
    "resolve_advertised_tools",
    "execute_bound_tool",
    "resolve_bound_tool_approval",
    "sync_external_tool",
    "cleanup_run_workspace",
}

def test_distribution_dependencies_and_imports_are_one_way() -> None:
    assert "jhin-connectors" not in agent_dependencies
    assert "jhin-agents" not in tool_dependencies
    assert "jhin-models" not in tool_dependencies
    assert not imports_under("services/agent_worker/src", "jhin_connectors")
    assert not imports_under("services/agent_worker/src", "jhin_connectors.cli.runner_client")
    assert not imports_under("services/tool_worker/src", "jhin_agents")
    assert not imports_under("services/tool_worker/src", "jhin_models")


def test_tool_worker_never_imports_or_queries_agent_reasoning_authority() -> None:
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in Path("services/tool_worker/src").rglob("*.py")
    )
    for forbidden in (
        "AgentStepReasoningRecord",
        "agent.step.reasoning",
        "completion_sanitized",
        "provider_call_ids",
        "transitions",
        "input_tokens",
        "output_tokens",
        "cached_tokens",
    ):
        assert forbidden not in source


def test_worker_registration_sets_are_exact() -> None:
    assert set(agent_activity_names()) >= {
        "reason_agent_step", "commit_agent_step", "run_agent_step", "resolve_approval",
        "finalize_run", "sync_external",
    }
    assert set(agent_activity_names()).isdisjoint(TOOL_ACTIVITY_NAMES)
    assert set(tool_activity_names()) == TOOL_ACTIVITY_NAMES


async def test_poller_health_requires_a_workflow_poller(temporal: TemporalEnvironment) -> None:
    assert await queue_has_workflow_poller(temporal.address, "default", "empty-queue") is False
    async with temporal.worker(task_queue="live-queue", workflows=[NoopWorkflow]):
        assert await queue_has_workflow_poller(
            temporal.address, "default", "live-queue"
        ) is True


def find_python_calls(root: Path, *, imported_name: str) -> set[Path]:
    callers: set[Path] = set()
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if any(
            isinstance(node, ast.Call)
            and (
                (isinstance(node.func, ast.Name) and node.func.id == imported_name)
                or (isinstance(node.func, ast.Attribute) and node.func.attr == imported_name)
            )
            for node in ast.walk(tree)
        ):
            callers.add(path.relative_to(root))
    return callers


def test_executable_catalog_builder_has_one_runtime_caller() -> None:
    callers = find_python_calls(REPO_ROOT, imported_name="build_default_catalog")
    runtime_callers = {
        path for path in callers
        if "tests" not in path.parts
        and path != Path("packages/connectors/src/jhin_connectors/registry.py")
    }
    assert runtime_callers == {
        Path("services/tool_worker/src/jhin_tool_worker/main.py")
    }
```

- [ ] **Step 2: Run RED against old dependencies and registration**

```bash
uv lock --check
uv run pytest services/tool_worker/tests/test_worker_registration.py packages/workflows/tests/test_poller_health.py tests/test_worker_dependency_boundaries.py tests/test_executable_catalog_boundary.py -q
```

Expected: FAIL because agent-worker still depends on/imports connectors, tool-worker has no complete worker registration, and the poller CLI is missing.

- [ ] **Step 3: Implement exact registration sets**

Agent-worker registers `AgentTaskWorkflow`, `TriggeredTaskWorkflow`, `DelegatedTaskWorkflow`, and `EngineeringTicketWorkflow`; agent-side snapshot/reason/commit/finalize/delegation/engineering activities; and legacy `run_agent_step`, `resolve_approval`, `finalize_run`, `sync_external` coordinator names. It does not instantiate a catalog.

Tool-worker registers all five compatibility workflows and only these effect activities: `resolve_advertised_tools`, `execute_bound_tool`, `resolve_bound_tool_approval`, `sync_external_tool`, and `cleanup_run_workspace`. Its settings have database/NATS/Temporal/log values and no model-provider fields.

Add exactly:

```toml
[project.scripts]
jhin-tool-worker = "jhin_tool_worker.main:run"
```

Keep registration inspectable as explicit lists (using the existing activity method objects for unchanged snapshot/delegation/engineering entries):

```python
agent_workflows = [
    AgentTaskWorkflow,
    TriggeredTaskWorkflow,
    DelegatedTaskWorkflow,
    EngineeringTicketWorkflow,
]
tool_workflows = [
    AdvertisedToolsCompatibilityWorkflow,
    ToolStepCompatibilityWorkflow,
    ApprovalCompatibilityWorkflow,
    SyncExternalCompatibilityWorkflow,
    CleanupCompatibilityWorkflow,
]
tool_activities = [
    tools.resolve_advertised_tools_activity,
    tools.execute_bound_tool_activity,
    tools.resolve_bound_tool_approval_activity,
    triggers.sync_external_tool_activity,
    cleanup.cleanup_run_workspace_activity,
]
```

- [ ] **Step 4: Add a real poller health command**

Expose `jhin-temporal-poller-check = "jhin_workflows.poller_health:run"` and implement Temporal 1.31 raw service inspection:

```python
async def queue_has_workflow_poller(address: str, namespace: str, queue: str) -> bool:
    client = await Client.connect(address, namespace=namespace)
    response = await client.workflow_service.describe_task_queue(
        DescribeTaskQueueRequest(
            namespace=namespace,
            task_queue=TaskQueue(name=queue),
            task_queue_type=TaskQueueType.TASK_QUEUE_TYPE_WORKFLOW,
        )
    )
    return bool(response.pollers)
```

The CLI reads `TEMPORAL_ADDRESS`, `TEMPORAL_NAMESPACE`, and one positional queue, exits 0 only with a poller, and prints no server addresses or exception text on failure.

- [ ] **Step 5: Run GREEN and commit**

```bash
uv lock --check
uv run pytest services/tool_worker/tests/test_worker_registration.py packages/workflows/tests/test_poller_health.py tests/test_worker_dependency_boundaries.py tests/test_executable_catalog_boundary.py -q
uv run ruff check services/agent_worker services/tool_worker packages/workflows tests/test_worker_dependency_boundaries.py tests/test_executable_catalog_boundary.py
uv run mypy services/agent_worker/src services/tool_worker/src packages/workflows/src
git add services/agent_worker/src/jhin_agent_worker/main.py services/agent_worker/pyproject.toml services/tool_worker/src/jhin_tool_worker/main.py services/tool_worker/tests/test_worker_registration.py services/tool_worker/pyproject.toml packages/workflows/pyproject.toml packages/workflows/src/jhin_workflows/poller_health.py packages/workflows/tests/test_poller_health.py docker/python.Dockerfile tests/test_worker_dependency_boundaries.py tests/test_executable_catalog_boundary.py
git commit -m "build: register isolated agent and tool workers"
```

### Task 7: Make sandbox-runner non-root with exact socket identity validation

**Files:**
- Create: `services/sandbox_runner/src/jhin_sandbox_runner/docker_socket.py`
- Modify: `services/sandbox_runner/src/jhin_sandbox_runner/settings.py`
- Modify: `services/sandbox_runner/src/jhin_sandbox_runner/jobs.py`
- Modify: `services/sandbox_runner/src/jhin_sandbox_runner/main.py`
- Create: `services/sandbox_runner/tests/test_docker_socket.py`
- Modify: `services/sandbox_runner/tests/test_job_config.py`
- Modify: `services/sandbox_runner/tests/test_job_lifecycle.py`

**Interfaces:**
- Consumes: mounted Unix socket, process effective UID/groups, documented `rootless|rootful` mode.
- Produces: `validate_docker_socket(path: Path, *, mode: DockerSocketMode, configured_gid: int | None, effective_uid: int, supplemental_groups: set[int]) -> str`; fatal startup on mismatch; explicit aiodocker URL; unchanged job-container isolation.

- [ ] **Step 1: Write failing socket and job-boundary tests**

Create real temporary Unix sockets and use these concrete identity/job tests:

```python
def test_rootless_socket_requires_process_ownership(unix_socket: Path) -> None:
    assert validate_docker_socket(
        unix_socket,
        mode="rootless",
        configured_gid=None,
        effective_uid=unix_socket.stat().st_uid,
        supplemental_groups=set(),
    ) == f"unix://{unix_socket}"
    with pytest.raises(DockerSocketConfigurationError, match="owner"):
        validate_docker_socket(
            unix_socket,
            mode="rootless",
            configured_gid=None,
            effective_uid=unix_socket.stat().st_uid + 1,
            supplemental_groups=set(),
        )


def test_job_never_inherits_runner_socket_identity(request: JobRequest, settings: Settings) -> None:
    config = build_container_config(
        request, settings, image="job", cpu_limit=1, memory_mb=256, pids_limit=32
    )
    assert config["User"] == "1000:1000"
    assert "GroupAdd" not in config["HostConfig"]
    assert all("docker.sock" not in bind for bind in config["HostConfig"].get("Binds", []))


@pytest.mark.parametrize(
    ("configured_gid", "groups", "matches"),
    [(12001, {12001}, True), (12002, {12001}, False), (12001, set(), False)],
)
def test_rootful_requires_exact_socket_gid_and_membership(
    unix_socket: Path, configured_gid: int, groups: set[int], matches: bool
) -> None:
    socket_gid = unix_socket.stat().st_gid
    supplied = socket_gid if configured_gid == 12001 else socket_gid + 1
    member_groups = {socket_gid} if groups else set()
    if matches:
        assert validate_docker_socket(
            unix_socket, mode="rootful", configured_gid=supplied,
            effective_uid=os.geteuid(), supplemental_groups=member_groups,
        ).startswith("unix://")
    else:
        with pytest.raises(DockerSocketConfigurationError):
            validate_docker_socket(
                unix_socket, mode="rootful", configured_gid=supplied,
                effective_uid=os.geteuid(), supplemental_groups=member_groups,
            )


def test_socket_boundary_contains_no_identity_or_permission_mutation() -> None:
    tree = ast.parse(
        Path(cast(str, DOCKER_SOCKET_MODULE.__file__)).read_text(encoding="utf-8")
    )
    forbidden = {"chmod", "chown", "setuid", "setgid"}
    called = {
        node.func.attr for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert called.isdisjoint(forbidden)
```

- [ ] **Step 2: Run RED before socket validation exists**

```bash
uv run pytest services/sandbox_runner/tests/test_docker_socket.py services/sandbox_runner/tests/test_job_config.py services/sandbox_runner/tests/test_job_lifecycle.py -q
```

Expected: FAIL importing `validate_docker_socket`; current runner still starts as root in Compose and creates a Docker client without identity validation.

- [ ] **Step 3: Implement fail-closed validation before Docker client creation**

```python
DockerSocketMode = Literal["rootless", "rootful"]

def validate_docker_socket(
    path: Path,
    *,
    mode: DockerSocketMode,
    configured_gid: int | None,
    effective_uid: int,
    supplemental_groups: set[int],
) -> str:
    info = path.stat()
    if not stat.S_ISSOCK(info.st_mode):
        raise DockerSocketConfigurationError("configured Docker endpoint is not a Unix socket")
    if effective_uid == 0:
        raise DockerSocketConfigurationError("sandbox runner must not run as root")
    if mode == "rootless" and info.st_uid != effective_uid:
        raise DockerSocketConfigurationError("rootless Docker socket owner does not match runner UID")
    if mode == "rootful":
        if configured_gid is None or info.st_gid != configured_gid:
            raise DockerSocketConfigurationError("Docker socket group does not match SANDBOX_DOCKER_GID")
        if configured_gid not in supplemental_groups:
            raise DockerSocketConfigurationError("runner does not hold the configured Docker socket group")
    if not os.access(path, os.R_OK | os.W_OK):
        raise DockerSocketConfigurationError("Docker socket is not readable and writable by the runner")
    return f"unix://{path}"
```

Settings add `sandbox_docker_mode`, `sandbox_docker_socket`, and optional validated positive `sandbox_docker_gid`. `JobManager.start()` validates and then constructs `aiodocker.Docker(url=validated_url)`, calls `version()`, and aborts startup on failure. It never catches the configuration error to continue degraded.

- [ ] **Step 4: Run GREEN and commit**

```bash
uv run pytest services/sandbox_runner/tests -q
uv run ruff check services/sandbox_runner
uv run mypy services/sandbox_runner/src
git add services/sandbox_runner/src/jhin_sandbox_runner/docker_socket.py services/sandbox_runner/src/jhin_sandbox_runner/settings.py services/sandbox_runner/src/jhin_sandbox_runner/jobs.py services/sandbox_runner/src/jhin_sandbox_runner/main.py services/sandbox_runner/tests/test_docker_socket.py services/sandbox_runner/tests/test_job_config.py services/sandbox_runner/tests/test_job_lifecycle.py
git commit -m "fix: run sandbox socket boundary without root"
```

### Task 8: Wire and assert the final Compose topology

**Files:**
- Modify: `compose.yaml`
- Modify: `compose.dev.yaml`
- Create: `compose.rootless.yaml`
- Create: `compose.rootful.yaml`
- Modify: `.env.example`
- Create: `scripts/assert_phase10_tool_worker_compose.py`
- Create: `tests/test_phase10_tool_worker_compose.py`
- Modify: `tests/test_compose_connector_allowlist.py`
- Modify: `tests/test_compose_phase9_dev_fakes.py`
- Modify: `tests/test_compose_supabase_db_fixture.py`
- Modify: `tests/integration/conftest.py`
- Modify: `tests/integration/test_stack_health.py`
- Modify: `tests/integration/test_phase6_security.py`

**Interfaces:**
- Consumes: `jhin-tool-worker`, queue poller CLI, non-root socket settings.
- Produces: rendered service/network/secret/env/user/group/port invariants and stack readiness for both queues.

- [ ] **Step 1: Write failing rendered-Compose assertions**

Assert all of the following from `docker compose config --format json`:

```python
def render_compose(
    *files: str,
    env: dict[str, str] | None = None,
    env_without: Collection[str] = (),
) -> dict[str, Any]:
    process_env = os.environ.copy()
    for key in env_without:
        process_env.pop(key, None)
    process_env.update(env or {})
    command = ["docker", "compose"]
    for filename in files:
        command.extend(("-f", filename))
    command.extend(("config", "--format", "json"))
    completed = subprocess.run(command, env=process_env, check=True, text=True, capture_output=True)
    return cast(dict[str, Any], json.loads(completed.stdout))


def test_rootful_render_has_exact_service_boundary() -> None:
    services = render_compose(
        "compose.yaml", "compose.rootful.yaml",
        env={"APP_ENV": "production", "SANDBOX_DOCKER_GID": "10001"},
    )["services"]
    assert set(services["agent-worker"]["networks"]) == {"control", "data"}
    assert set(services["tool-worker"]["networks"]) == {"control", "data", "runner"}
    assert set(services["sandbox-runner"]["networks"]) == {"runner"}
    assert "SANDBOX_RUNNER_URL" not in services["agent-worker"]["environment"]
    assert "SANDBOX_RUNNER_TOKEN" not in services["agent-worker"]["environment"]
    assert services["tool-worker"]["environment"]["SANDBOX_RUNNER_URL"] == "http://sandbox-runner:8085"
    assert services["sandbox-runner"]["user"] != "0:0"
    assert services["sandbox-runner"]["privileged"] is False
    assert services["sandbox-runner"].get("group_add", []) == ["10001"]
    assert "ports" not in services["tool-worker"]
    assert services["agent-worker"]["environment"]["APP_ENV"] == "production"
    assert services["tool-worker"]["environment"]["APP_ENV"] == "production"
    assert all(
        not key.startswith("JHIN_TEST_CRASH_BARRIER_")
        for key in services["tool-worker"]["environment"]
    )


def test_rootless_render_never_interpolates_gid() -> None:
    services = render_compose(
        "compose.yaml", "compose.rootless.yaml", env_without={"SANDBOX_DOCKER_GID"}
    )["services"]
    runner = services["sandbox-runner"]
    assert runner.get("group_add", []) == []
    assert runner["environment"]["SANDBOX_DOCKER_MODE"] == "rootless"
    assert "SANDBOX_DOCKER_GID" not in runner["environment"]
```

```python
def test_dev_fakes_and_healthchecks_follow_worker_ownership() -> None:
    services = render_compose(
        "compose.yaml", "compose.dev.yaml", "compose.rootless.yaml",
        env_without={"APP_ENV", "SANDBOX_DOCKER_GID"},
    )["services"]
    for key in (
        "JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS",
        "JHIN_CONNECTOR_ALLOWED_DB_HOSTS",
    ):
        assert key in services["tool-worker"]["environment"]
        assert key not in services["agent-worker"]["environment"]
    assert services["agent-worker"]["environment"]["APP_ENV"] == "dev"
    assert services["tool-worker"]["environment"]["APP_ENV"] == "dev"
    assert services["fake-linear"]["build"]["args"]["SERVICE_PACKAGE"] == "jhin-tool-worker"
    assert services["fake-provider"]["build"]["args"]["SERVICE_PACKAGE"] == "jhin-agent-worker"
    assert "jhin-agent-queue" in " ".join(services["agent-worker"]["healthcheck"]["test"])
    assert "jhin-tool-queue" in " ".join(services["tool-worker"]["healthcheck"]["test"])


def test_dev_overlay_propagates_explicit_test_app_env() -> None:
    services = render_compose(
        "compose.yaml", "compose.dev.yaml", "compose.rootless.yaml",
        env={"APP_ENV": "test"},
        env_without={"SANDBOX_DOCKER_GID"},
    )["services"]
    assert services["agent-worker"]["environment"]["APP_ENV"] == "test"
    assert services["tool-worker"]["environment"]["APP_ENV"] == "test"
```

Update the three existing rendered-Compose contracts with these exact ownership assertions, rather than leaving stale Phase 9 agent ownership:

```python
# tests/test_compose_connector_allowlist.py
def test_connector_origin_allowlist_is_exact_and_dev_only() -> None:
    development = _render_compose("compose.yaml", "compose.dev.yaml")
    production = _render_compose("compose.yaml")
    recipients = {
        name for name, service in development["services"].items()
        if "JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS" in service.get("environment", {})
    }
    assert recipients == {"api", "tool-worker"}
    for name in recipients:
        assert development["services"][name]["environment"][
            "JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS"
        ] == DEV_CONNECTOR_ORIGINS
    assert "JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS" not in json.dumps(production)


# tests/test_compose_phase9_dev_fakes.py
EXPECTED_CONNECTOR_FAKE_BUILD = {
    "context": str(ROOT),
    "dockerfile": "docker/python.Dockerfile",
    "args": {"SERVICE_PACKAGE": "jhin-tool-worker"},
}

def test_connector_fakes_use_tool_worker_image_and_provider_uses_agent_image() -> None:
    services = _render_compose("compose.yaml", "compose.dev.yaml")["services"]
    for name in ("fake-github", "fake-linear", "fake-vercel", "fake-supabase"):
        assert services[name]["build"]["args"]["SERVICE_PACKAGE"] == "jhin-tool-worker"
    assert services["fake-provider"]["build"]["args"]["SERVICE_PACKAGE"] == (
        "jhin-agent-worker"
    )

def test_phase9_http_origins_extend_existing_dev_allowlist_only() -> None:
    development = _render_compose("compose.yaml", "compose.dev.yaml")
    production = _render_compose("compose.yaml")
    for name in ("api", "tool-worker"):
        assert development["services"][name]["environment"][
            "JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS"
        ] == DEV_HTTP_ORIGINS
        assert "JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS" not in production[
            "services"
        ][name]["environment"]
    assert "JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS" not in development[
        "services"
    ]["agent-worker"]["environment"]


# tests/test_compose_supabase_db_fixture.py
def test_only_database_callers_receive_the_dev_fixture_allowlist() -> None:
    development = _render_compose("compose.yaml", "compose.dev.yaml")
    recipients = {
        name for name, service in development["services"].items()
        if "JHIN_CONNECTOR_ALLOWED_DB_HOSTS" in service.get("environment", {})
    }
    assert recipients == {"api", "tool-worker"}
    for name in recipients:
        assert development["services"][name]["environment"][
            "JHIN_CONNECTOR_ALLOWED_DB_HOSTS"
        ] == FIXTURE_HOST
```

In the existing `test_phase9_http_fakes_are_dev_only_healthy_and_loopback_bound` loop, replace its build assertion with `assert service["build"] == EXPECTED_CONNECTOR_FAKE_BUILD`; keep its command, network, loopback port, and healthcheck assertions unchanged.

- [ ] **Step 2: Run RED against all three rendered modes**

```bash
env -u SANDBOX_DOCKER_GID docker compose -f compose.yaml -f compose.rootless.yaml config --format json
SANDBOX_DOCKER_GID=10001 docker compose -f compose.yaml -f compose.rootful.yaml config --format json
uv run pytest tests/test_phase10_tool_worker_compose.py tests/test_compose_connector_allowlist.py tests/test_compose_phase9_dev_fakes.py tests/test_compose_supabase_db_fixture.py -q
```

Expected: FAIL because tool-worker, the mode overlays, propagated test environment, and connector fake/allowlist ownership do not exist; importantly the first command must not fail at interpolation before the test runs.

- [ ] **Step 3: Implement services and mode-specific non-root runner shape**

Add base `tool-worker` with command `jhin-tool-worker`, `APP_ENV: ${APP_ENV:-production}`, database/NATS/Temporal/master-key/runner variables, networks `[control, data, runner]`, and no port/model variables. Add the same `APP_ENV: ${APP_ENV:-production}` mapping to agent-worker. Remove connector/runner variables and `runner` network from agent-worker. In `compose.dev.yaml`, use `APP_ENV: ${APP_ENV:-dev}` on both workers so an explicit test value is not overwritten, and move exactly `JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS` and `JHIN_CONNECTOR_ALLOWED_DB_HOSTS` from agent-worker to tool-worker while retaining their API entries. Build `fake-github`, `fake-linear`, `fake-vercel`, and `fake-supabase` from `SERVICE_PACKAGE: jhin-tool-worker`; only `fake-provider` continues to use `jhin-agent-worker`. Use:

```yaml
sandbox-runner:
  user: "10001:10001"
  privileged: false
  environment:
    SANDBOX_DOCKER_MODE: rootless
    SANDBOX_DOCKER_SOCKET: /run/jhin/docker.sock
  volumes:
    - ${SANDBOX_DOCKER_SOCKET_HOST:-/run/user/10001/docker.sock}:/run/jhin/docker.sock
```

Keep `compose.rootless.yaml` explicit but free of every GID interpolation:

```yaml
# compose.rootless.yaml
services:
  sandbox-runner:
    environment:
      SANDBOX_DOCKER_MODE: rootless
```

Put the entire rootful group contract in the rootful overlay only:

```yaml
# compose.rootful.yaml
services:
  sandbox-runner:
    group_add:
      - "${SANDBOX_DOCKER_GID:?set SANDBOX_DOCKER_GID to the mounted socket numeric group}"
    environment:
      SANDBOX_DOCKER_MODE: rootful
      SANDBOX_DOCKER_GID: "${SANDBOX_DOCKER_GID:?set SANDBOX_DOCKER_GID}"
    volumes:
      - ${SANDBOX_DOCKER_SOCKET_HOST:-/var/run/docker.sock}:/run/jhin/docker.sock
```

The mounted rootless socket owner must be UID 10001. In rootful mode only, `SANDBOX_DOCKER_GID` must equal the socket's exact nonzero group. `compose.yaml`, `compose.dev.yaml`, and `compose.rootless.yaml` must contain neither `${SANDBOX_DOCKER_GID...}` nor a `group_add` key. Do not default to group 0.

Only `compose.dev.yaml` exposes disabled-by-default crash-barrier controls, to both workers for the required live matrix; empty names normalize to `None`:

```yaml
agent-worker:
  environment:
    APP_ENV: ${APP_ENV:-dev}
    JHIN_TEST_CRASH_BARRIER_DIR: ${JHIN_TEST_CRASH_BARRIER_DIR:-}
    JHIN_TEST_CRASH_BARRIER_NAME: ${JHIN_TEST_CRASH_BARRIER_NAME:-}
    JHIN_TEST_CRASH_BARRIER_MATCH: ${JHIN_TEST_CRASH_BARRIER_MATCH:-}
  volumes:
    - ${JHIN_TEST_CRASH_BARRIER_HOST_DIR:-/tmp/jhin-disabled-barriers}:/run/jhin/test-barriers

tool-worker:
  environment:
    APP_ENV: ${APP_ENV:-dev}
    JHIN_TEST_CRASH_BARRIER_DIR: ${JHIN_TEST_CRASH_BARRIER_DIR:-}
    JHIN_TEST_CRASH_BARRIER_NAME: ${JHIN_TEST_CRASH_BARRIER_NAME:-}
    JHIN_TEST_CRASH_BARRIER_MATCH: ${JHIN_TEST_CRASH_BARRIER_MATCH:-}
  volumes:
    - ${JHIN_TEST_CRASH_BARRIER_HOST_DIR:-/tmp/jhin-disabled-barriers}:/run/jhin/test-barriers
```

`JHIN_TEST_CRASH_BARRIER_HOST_DIR` is only the host mount source; it is never passed to worker settings. `JHIN_TEST_CRASH_BARRIER_DIR`, when enabled by a test, is always the distinct container path `/run/jhin/test-barriers`. The production/base render contains none of these keys or mounts. The upgrade-only Compose file supplies the same mount to its Phase 9 agent container. Both upgrade generations receive `APP_ENV: test`.

- [ ] **Step 4: Run unit/render gates and commit**

```bash
env -u SANDBOX_DOCKER_GID docker compose -f compose.yaml -f compose.rootless.yaml config --format json >/tmp/jhin-phase10-rootless-compose.json
SANDBOX_DOCKER_GID=10001 docker compose -f compose.yaml -f compose.rootful.yaml config --format json >/tmp/jhin-phase10-rootful-compose.json
env -u SANDBOX_DOCKER_GID uv run python scripts/assert_phase10_tool_worker_compose.py --mode rootless
SANDBOX_DOCKER_GID=10001 uv run python scripts/assert_phase10_tool_worker_compose.py --mode rootful
uv run pytest tests/test_phase10_tool_worker_compose.py tests/test_phase9_production_compose.py tests/test_compose_connector_allowlist.py tests/test_compose_phase9_dev_fakes.py tests/test_compose_supabase_db_fixture.py services/agent_worker/tests/test_upgrade_crash_barriers.py services/tool_worker/tests/test_bound_tool_execution.py -q
git add compose.yaml compose.dev.yaml compose.rootless.yaml compose.rootful.yaml .env.example scripts/assert_phase10_tool_worker_compose.py tests/test_phase10_tool_worker_compose.py tests/test_compose_connector_allowlist.py tests/test_compose_phase9_dev_fakes.py tests/test_compose_supabase_db_fixture.py tests/integration/conftest.py tests/integration/test_stack_health.py tests/integration/test_phase6_security.py
git commit -m "build: wire isolated tool-worker topology"
```

### Task 9: Document ownership, compatibility lifetime, and socket modes

**Files:**
- Create: `docs/architecture/tool-worker-boundary.md`
- Modify: `docs/architecture/connectors.md`
- Modify: `docs/architecture/sandboxing.md`
- Modify: `README.md`
- Modify: `.env.example`
- Create: `tests/test_tool_worker_docs.py`

**Interfaces:**
- Consumes: the implemented queue, manifests, patches, compatibility workflows, packages, Compose topology, and socket validation.
- Produces: operator/developer runbook with no hidden setup assumptions.

- [ ] **Step 1: Write the failing documentation contract test**

```python
def fenced_command_after(document: str, heading: str) -> str:
    tail = document.split(heading, maxsplit=1)[1]
    return tail.split("```bash", maxsplit=1)[1].split("```", maxsplit=1)[0]


def test_documented_socket_commands_keep_gid_mode_specific() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")
    rootless = fenced_command_after(readme, "Rootless Docker socket")
    rootful = fenced_command_after(readme, "Rootful Docker socket")
    assert "compose.rootless.yaml" in rootless
    assert "SANDBOX_DOCKER_GID" not in rootless
    assert "compose.rootful.yaml" in rootful
    assert "SANDBOX_DOCKER_GID" in rootful
    assert "phase10-tool-worker-boundary-v1" in Path(
        "docs/architecture/tool-worker-boundary.md"
    ).read_text(encoding="utf-8")
```

- [ ] **Step 2: Run RED against the stale documentation**

```bash
uv run pytest tests/test_tool_worker_docs.py -q
```

Expected: FAIL because the ownership document and mode-specific commands do not exist.

- [ ] **Step 3: Write the exact architecture and operator contract**

Document the sequence `resolve advertised tools → reason/bind → ordered execute → commit`, stable IDs and transaction authority, ordinary/approval/sync/cleanup ownership, the three patch IDs, each compatibility workflow ID formula, and the removal gate: query all open histories and retain handlers until none predate the patch and no closed prepatch history is queryable under the retention policy. State that `agent.step.tool_manifest` contains only the canonical call set; `agent.step.reasoning` is a separate agent-only append-only record whose API payload is always `{}`; and tool-worker selects only one call's four JSON scalars and never loads that reasoning record, completion, usage, transitions, or other step history. List the exact test-only crash matrix and outcomes: agent pre-bind reruns the model without a tool effect, agent post-bind reuses the committed pair, tool pre-claim executes once after recovery, and the two post-claim boundaries become `execution_unknown`. Explicitly state `workflow.deprecate_patch` is not allowed in Phase 10 subproject 1.

Update sandboxing to show `tool-worker → sandbox-runner`, not agent-worker; rootless socket owner UID 10001; rootful numeric GID discovery/configuration; fatal wrong-GID behavior; runner UID 10001; job UID 1000; and no socket/group propagation. README gives a rootless command with no GID variable and a separate rootful command that requires the discovered exact `SANDBOX_DOCKER_GID`; neither prints secrets.

- [ ] **Step 4: Run documentation GREEN, boundary scans, and commit**

```bash
uv run pytest tests/test_tool_worker_docs.py -q
rg -n "agent.worker.*sandbox.runner|agent-worker.*runner network|Runs as root|user:.*0:0" README.md docs/architecture .env.example
rg -n "phase10-tool-worker-boundary-v1|phase10-trigger-sync-tool-routing-v1|phase10-engineering-sync-tool-routing-v1|jhin-tool-queue" docs/architecture/tool-worker-boundary.md README.md
git diff --check
git add docs/architecture/tool-worker-boundary.md docs/architecture/connectors.md docs/architecture/sandboxing.md README.md .env.example tests/test_tool_worker_docs.py
git commit -m "docs: explain deterministic tool-worker boundary"
```

Expected: the first search returns no stale ownership/root claims; the second finds all stable IDs and queue documentation.

### Task 10: Prove live queue ownership, crash gaps, compatibility, and final staging

**Files:**
- Create: `tests/integration/test_phase10_tool_worker_boundary.py`
- Create: `tests/integration/test_phase10_sandbox_socket_modes.py`
- Create: `tests/integration/phase10_upgrade_harness.py`
- Create: `tests/integration/test_phase10_live_upgrade.py`
- Create: `tests/integration/compose.phase10-upgrade.yaml`
- Modify: `tests/integration/test_phase3_exit.py`
- Modify: `tests/integration/test_phase6_exit.py`
- Modify: `tests/integration/test_phase7_exit.py`
- Modify: `tests/integration/test_phase9_exit.py`
- Modify: `Makefile`
- Modify: `.github/workflows/ci.yml`

**Interfaces:**
- Consumes: the complete Task 1–9 system.
- Produces: live evidence for ordinary tools, advertised filtering, approvals, trigger sync, cleanup, queue loss/recovery, non-root socket access, dependency/network separation, agent pre-bind/post-bind hard kills, tool pre-claim/post-claim/post-effect hard kills, and true Phase 9→10 in-flight history completion in one PostgreSQL/Temporal environment.

- [ ] **Step 1: Write failing end-to-end assertions**

Use fake providers and exact IDs. The ordinary/approval/sync/cleanup test body is:

```python
async def test_all_effect_classes_cross_tool_queue_once(stack: ComposeStack) -> None:
    ordinary = await stack.run_ordinary_tool()
    approval = await stack.park_restart_and_approve()
    sync = await stack.run_linear_trigger()
    cleanup = await stack.run_cli_and_finalize()
    assert ordinary.effect_count == approval.effect_count == sync.effect_count == 1
    assert cleanup.delete_count == 1
    for execution in (ordinary, approval, sync, cleanup):
        assert await stack.activity_queues(execution.workflow_ids) == {"jhin-tool-queue"}
    assert approval.before_restart_invocation_id == approval.after_restart_invocation_id
```

Fetch workflow history and inspect `activity_task_scheduled_event_attributes.task_queue.name` rather than trusting service logs.

Use the Task 0 files as hard barriers, never timing sleeps. Because schemas are resolved on the tool queue before agent reasoning, `definition_only_resolver()` is a test-process Temporal poller that registers the real `resolve_advertised_tools` activity plus an `execute_bound_tool` stub that raises retryable `integration_effect_worker_absent` without opening a gateway or database session. It lets the agent bind the manifest while the real effect-capable `tool-worker` container is stopped; the production-path test above separately proves the real resolver/executor sequence.

```python
@asynccontextmanager
async def definition_only_resolver(stack: ComposeStack) -> AsyncIterator[None]:
    execute_seen = asyncio.Event()

    @activity.defn(name="execute_bound_tool")
    async def reject_effect_activity(params: ExecuteBoundToolInput) -> NoReturn:
        execute_seen.set()
        raise ApplicationError(
            "effect-capable tool worker is intentionally absent",
            type="integration_effect_worker_absent",
            next_retry_delay=timedelta(milliseconds=250),
        )

    async with Worker(
        stack.temporal_client,
        task_queue=TOOL_TASK_QUEUE,
        activities=[stack.resolve_advertised_tools_activity, reject_effect_activity],
    ):
        yield
        await asyncio.wait_for(execute_seen.wait(), timeout=10)


def container_env(stack: ComposeStack, service: str, key: str) -> str | None:
    values = stack.inspect(service)["Config"].get("Env", [])
    environment = dict(item.split("=", 1) for item in values if "=" in item)
    return environment.get(key)


def barrier_environment(
    host_root: Path,
    failpoint: CrashBarrierName,
    identity: UUID,
) -> dict[str, str]:
    return {
        "APP_ENV": "test",
        "JHIN_TEST_CRASH_BARRIER_HOST_DIR": str(host_root),
        "JHIN_TEST_CRASH_BARRIER_DIR": "/run/jhin/test-barriers",
        "JHIN_TEST_CRASH_BARRIER_NAME": failpoint,
        "JHIN_TEST_CRASH_BARRIER_MATCH": str(identity),
    }


async def hard_kill_at(
    stack: ComposeStack,
    *,
    failpoint: CrashBarrierName,
    run_id: UUID,
) -> UUID:
    await stack.wait_manifest(run_id, step_index=0)
    ordinal = await stack.first_manifest_ordinal(run_id, step_index=0)
    invocation_id = stable_tool_invocation_id(run_id, 0, ordinal)
    host_root = stack.new_host_barrier_root(failpoint, invocation_id)
    await stack.recreate_tool_worker(barrier_environment(host_root, failpoint, invocation_id))
    assert container_env(stack, "tool-worker", "APP_ENV") == "test"
    await stack.wait_for_barrier(
        host_root, failpoint, invocation_id, suffix="arrived", timeout=10
    )
    if failpoint == TOOL_BEFORE_CLAIM:
        assert await stack.tool_call_count(invocation_id) == 0
        assert await stack.effect_count(invocation_id) == 0
    elif failpoint == TOOL_AFTER_CLAIM:
        assert await stack.tool_status(invocation_id) == "executing"
        assert await stack.effect_count(invocation_id) == 0
    else:
        assert failpoint == TOOL_AFTER_EFFECT
        assert await stack.tool_status(invocation_id) == "executing"
        assert await stack.effect_count(invocation_id) == 1
    stack.kill("tool-worker", signal="SIGKILL")
    stack.release_barrier(host_root, failpoint, invocation_id)
    await stack.recreate_tool_worker({"APP_ENV": "test"})
    return invocation_id


@pytest.mark.parametrize(
    ("failpoint", "expected_effects", "expected_status"),
    [
        (TOOL_BEFORE_CLAIM, 1, "completed"),
        (TOOL_AFTER_CLAIM, 0, "execution_unknown"),
        (TOOL_AFTER_EFFECT, 1, "execution_unknown"),
    ],
)
async def test_tool_crash_matrix_preserves_claim_and_ambiguity_contract(
    stack: ComposeStack,
    failpoint: CrashBarrierName,
    expected_effects: int,
    expected_status: str,
) -> None:
    stack.stop("tool-worker")
    async with definition_only_resolver(stack):
        run_id = await stack.start_mutating_tool_run()
        await stack.wait_manifest(run_id, step_index=0)
    invocation_id = await hard_kill_at(stack, failpoint=failpoint, run_id=run_id)
    await stack.wait_workflow_closed(run_id)
    assert await stack.effect_count(invocation_id) == expected_effects
    assert await stack.tool_status(invocation_id) == expected_status
    assert await stack.tool_call_count(invocation_id) == 1
    assert await stack.terminal_tool_call_count(invocation_id) == 1


@pytest.mark.parametrize(
    ("failpoint", "expected_model_calls", "events_at_barrier"),
    [
        (AGENT_BEFORE_BIND, 2, (0, 0)),
        (PHASE9_AFTER_MANIFEST, 1, (1, 1)),
    ],
)
async def test_agent_crash_matrix_retries_without_tool_effect_duplication(
    stack: ComposeStack,
    failpoint: CrashBarrierName,
    expected_model_calls: int,
    events_at_barrier: tuple[int, int],
) -> None:
    await stack.configure_repeated_canonical_model_response(
        tool_name="test.mutate", arguments={"value": "once"}
    )
    stack.stop("agent-worker")
    run_id = await stack.start_mutating_tool_run()
    host_root = stack.new_host_barrier_root(failpoint, run_id)
    await stack.recreate_agent_worker(barrier_environment(host_root, failpoint, run_id))
    assert container_env(stack, "agent-worker", "APP_ENV") == "test"
    await stack.wait_for_barrier(
        host_root, failpoint, run_id, suffix="arrived", timeout=10
    )
    assert await stack.event_count(run_id, "agent.step.tool_manifest") == events_at_barrier[0]
    assert await stack.event_count(run_id, "agent.step.reasoning") == events_at_barrier[1]
    assert await stack.model_call_count(run_id) == 1
    assert await stack.effect_count_for_run(run_id) == 0
    assert await stack.public_run_status(run_id) in {"running", "retrying"}

    stack.kill("agent-worker", signal="SIGKILL")
    assert await stack.public_run_status(run_id) in {"running", "retrying"}
    stack.release_barrier(host_root, failpoint, run_id)
    await stack.recreate_agent_worker({"APP_ENV": "test"})
    assert container_env(stack, "agent-worker", "APP_ENV") == "test"
    await stack.wait_workflow_closed(run_id)

    invocation_id = stable_tool_invocation_id(run_id, 0, 0)
    assert await stack.model_call_count(run_id) == expected_model_calls
    assert await stack.event_count(run_id, "agent.step.tool_manifest") == 1
    assert await stack.event_count(run_id, "agent.step.reasoning") == 1
    assert await stack.effect_count(invocation_id) == 1
    assert await stack.tool_call_count(invocation_id) == 1
    assert await stack.terminal_run_event_count(run_id) == 1
    assert await stack.public_run_status(run_id) == "completed"
```

`new_host_barrier_root` creates a unique host directory beneath the pytest temporary root. `recreate_agent_worker` runs `docker compose -f compose.yaml -f compose.dev.yaml -f compose.rootful.yaml up -d --no-deps --force-recreate agent-worker`; `recreate_tool_worker` runs the same command ending in `tool-worker`. Each uses the supplied process environment and removes all `JHIN_TEST_CRASH_BARRIER_*` keys not explicitly supplied; therefore the barrier recreation receives the exact container root/name/match and the post-kill recreation is unconfigured. Both helpers inspect the recreated container and fail unless Compose propagated `APP_ENV=test`. Never pass the host path as `JHIN_TEST_CRASH_BARRIER_DIR`, never mutate environment in an already-running container, and never reuse the disabled default mount directory across tests. At `TOOL_BEFORE_CLAIM`, no `ToolCall` exists, so the retry safely creates one terminal claim and executes once. At the two later tool boundaries, the durable executing claim makes absence/presence of the external effect unprovable, so recovery writes nonretryable `execution_unknown` and never invokes the executor again.

Exercise both startup validators in real service images as well as their Task 0/3 unit tests:

```python
@pytest.mark.parametrize("service", ["agent-worker", "tool-worker"])
def test_worker_image_rejects_live_barrier_controls_in_production(
    stack: ComposeStack, service: str,
) -> None:
    result = stack.run_service_once(
        service,
        environment={
            "APP_ENV": "production",
            "JHIN_TEST_CRASH_BARRIER_DIR": "/run/jhin/test-barriers",
            "JHIN_TEST_CRASH_BARRIER_NAME": TOOL_AFTER_CLAIM,
            "JHIN_TEST_CRASH_BARRIER_MATCH": "018f4d52-8b93-7d41-8ac7-7f190f091111",
        },
        timeout=10,
    )
    assert result.returncode != 0
    assert "test crash barriers are forbidden in production" in result.stderr
```

The queue-loss and live container test is:

```python
async def test_tool_queue_loss_blocks_effect_and_live_networks_are_isolated(
    stack: ComposeStack, expected_gid: str
) -> None:
    stack.stop("tool-worker")
    async with definition_only_resolver(stack):
        run_id = await stack.start_ordinary_tool_run()
        await stack.wait_manifest(run_id, step_index=0)
    assert await stack.effect_count_for_run(run_id) == 0
    await stack.recreate_tool_worker({"APP_ENV": "test"})
    await stack.wait_workflow_closed(run_id)
    assert await stack.effect_count_for_run(run_id) == 1
    runner = stack.inspect("sandbox-runner")
    assert runner["Config"]["User"] != "0:0"
    assert runner["HostConfig"]["Privileged"] is False
    assert runner["HostConfig"]["GroupAdd"] == [expected_gid]
    assert stack.agent_runner_dns_probe().returncode != 0
    assert stack.tool_runner_health_probe().returncode == 0
    job = stack.inspect_last_job()
    assert job["Config"]["User"] == "1000:1000"
    assert job["HostConfig"]["Privileged"] is False
    assert job["HostConfig"].get("GroupAdd", []) == []
    assert all("docker.sock" not in bind for bind in job["HostConfig"].get("Binds", []))
```

In `test_phase10_sandbox_socket_modes.py`, parameterize the live probe from `PHASE10_SOCKET_MODE`. Rootful asserts the configured numeric GID is the container's sole supplemental group and a runner `/health` plus one no-op job succeeds. Rootless asserts the mounted socket owner equals the runner's effective UID, `GroupAdd == []`, and the same health/job probe succeeds. A `wrong-gid` case starts only sandbox-runner with a known-incorrect nonzero GID, asserts startup exits nonzero with the bounded configuration error, and asserts socket mode/UID/GID are unchanged before and after. Do not skip a requested mode; skip only when `PHASE10_SOCKET_MODE` is absent from an unrelated integration invocation.

- [ ] **Step 2: Write the failing true-upgrade test and harness contract**

`phase10_upgrade_harness.py` reads `source_ref` from `phase9-ref.txt`, builds `f"jhin-phase9-agent-worker:{source_ref[:12]}"` from `git archive source_ref` (never the current tree), and starts that image with the Task 0 barrier directory against the same database, NATS, Temporal namespace, fake model, fake connector, and sandbox-runner later used by Phase 10. `compose.phase10-upgrade.yaml` publishes no extra port, sets `APP_ENV: test` on the Phase 9 agent and both Phase 10 worker services, and mounts each host barrier source at exactly `/run/jhin/test-barriers` only in test workers. Every worker setting uses the container path; the harness retains the distinct host source for arrival/release operations.

```python
def build_phase9_agent_image(repo: Path, source_ref: str) -> str:
    image = f"jhin-phase9-agent-worker:{source_ref[:12]}"
    archive = subprocess.Popen(
        ["git", "archive", "--format=tar", source_ref],
        cwd=repo,
        stdout=subprocess.PIPE,
    )
    assert archive.stdout is not None
    built = subprocess.run(
        ["docker", "build", "-f", "docker/python.Dockerfile",
         "--build-arg", "SERVICE_PACKAGE=jhin-agent-worker", "-t", image, "-"],
        cwd=repo,
        stdin=archive.stdout,
        check=False,
        text=False,
    )
    archive.stdout.close()
    archive_status = archive.wait()
    if archive_status != 0 or built.returncode != 0:
        raise RuntimeError("failed to build the frozen Phase 9 agent image")
    return image


async def test_inflight_phase9_histories_finish_after_phase10_swap(
    upgrade: UpgradeHarness,
) -> None:
    await upgrade.start_phase9_workers()
    parked = await upgrade.park_phase9_boundaries(
        normal=PHASE9_AFTER_MANIFEST,
        approval=True,
        sync=PHASE9_SYNC_BEFORE_EFFECT,
        finalize=PHASE9_CLEANUP_BEFORE_EFFECT,
    )
    assert await upgrade.count_events(
        parked.normal.run_id, event_type="agent.step.reasoning", step=0
    ) == 0
    assert await upgrade.effect_count(parked.normal.invocation_id) == 0

    await upgrade.kill_phase9_workers(signal="SIGKILL")
    await upgrade.release_all_arrived_barriers()
    await upgrade.start_phase10_workers()
    await upgrade.decide_approval(parked.approval.approval_id, "approved")
    results = await upgrade.wait_all_closed(parked.workflow_ids)

    assert all(result.status == "completed" for result in results)
    assert await upgrade.count_events(
        parked.normal.run_id, event_type="agent.step.reasoning", step=0
    ) == 1
    assert set((await upgrade.load_manifest(parked.normal.run_id, step=0)).payload_json) == {
        "step", "manifest"
    }
    assert await upgrade.effect_count(parked.normal.invocation_id) == 1
    assert await upgrade.comment_count(parked.sync.external_id) == 1
    assert await upgrade.cleanup_count(parked.finalize.run_id) == 1
    assert await upgrade.compatibility_activity_queues() == {"jhin-tool-queue"}
    assert await upgrade.phase10_agent_effect_attempts() == 0
```

The harness must fetch and retain the original outer workflow IDs/runs before the swap, then inspect those same histories plus deterministic compatibility workflow histories afterward. `park_phase9_boundaries` waits on fsynced arrival files or the authoritative pending `Approval` row—never elapsed time. The normal case proves legacy repair adds the separate agent-only reasoning event for the `agent-post-bind-pre-effect` fixture shape without mutating its manifest. The sync and finalize cases prove the old names resume through tool-queue compatibility workflows. The agent-effect assertion combines a zero fake-effect ledger for agent-worker identity with its absent connector/runner configuration and failed runner DNS probe.

- [ ] **Step 3: Run RED before adding integration helpers**

```bash
uv run pytest --collect-only tests/integration/test_phase10_tool_worker_boundary.py tests/integration/test_phase10_sandbox_socket_modes.py tests/integration/test_phase10_live_upgrade.py -q
```

Expected: FAIL because the three integration files, `UpgradeHarness`, and crash-barrier Compose controls do not exist.

- [ ] **Step 4: Implement integration helpers and focused/full Make targets**

Add `test-tool-worker-boundary` for unit/replay/render gates, `test-tool-worker-boundary-integration` for the live boundary/crash files, `test-tool-worker-live-upgrade` for the true image swap, and explicit `test-sandbox-socket-rootful`, `test-sandbox-socket-rootless`, and `test-sandbox-socket-wrong-gid` targets. The rootless target requires `PHASE10_ROOTLESS_DOCKER_SOCKET` to name an already-running UID-10001 rootless daemon socket; no target infers or changes socket permissions. Extend the Docker build matrix with `tool-worker` and `sandbox-runner`, make the Python CI job run `test-tool-worker-boundary`, and set `actions/checkout` `fetch-depth: 0` in the upgrade job so the exact SHA in `phase9-ref.txt` is available to `git archive`.

```make
test-tool-worker-live-upgrade:
	uv run pytest -m integration tests/integration/test_phase10_live_upgrade.py -v

test-sandbox-socket-rootless:
	test -S "$(PHASE10_ROOTLESS_DOCKER_SOCKET)"
	env -u SANDBOX_DOCKER_GID SANDBOX_DOCKER_SOCKET_HOST="$(PHASE10_ROOTLESS_DOCKER_SOCKET)" PHASE10_SOCKET_MODE=rootless uv run pytest -m integration tests/integration/test_phase10_sandbox_socket_modes.py -v

test-sandbox-socket-rootful:
	test -n "$(SANDBOX_DOCKER_GID)"
	PHASE10_SOCKET_MODE=rootful uv run pytest -m integration tests/integration/test_phase10_sandbox_socket_modes.py -v

test-sandbox-socket-wrong-gid:
	test -n "$(SANDBOX_DOCKER_GID)"
	PHASE10_SOCKET_MODE=wrong-gid uv run pytest -m integration tests/integration/test_phase10_sandbox_socket_modes.py -v
```

```yaml
phase10-live-upgrade:
  runs-on: ubuntu-latest
  steps:
    - uses: actions/checkout@v4
      with:
        fetch-depth: 0
    - uses: astral-sh/setup-uv@v5
    - run: uv sync --frozen --all-packages
    - name: Export rootful socket group
      run: |
        socket_gid="$(stat -c %g /var/run/docker.sock)"
        test "$socket_gid" -gt 0
        echo "SANDBOX_DOCKER_GID=$socket_gid" >> "$GITHUB_ENV"
    - name: Phase 9 to Phase 10 in-flight upgrade
      run: make test-tool-worker-live-upgrade
```

- [ ] **Step 5: Run the focused acceptance gate**

```bash
uv run pytest packages/workflows/tests/test_agent_task_tool_routing.py packages/workflows/tests/test_tool_compat_workflows.py packages/workflows/tests/test_phase10_history_replay.py services/agent_worker/tests/test_reasoning_manifest.py services/agent_worker/tests/test_legacy_manifest_sidecar.py services/agent_worker/tests/test_step_projection.py services/agent_worker/tests/test_compatibility_coordinators.py services/tool_worker/tests services/sandbox_runner/tests tests/test_worker_dependency_boundaries.py tests/test_executable_catalog_boundary.py tests/test_phase10_tool_worker_compose.py -q
env -u SANDBOX_DOCKER_GID uv run python scripts/assert_phase10_tool_worker_compose.py --mode rootless
SANDBOX_DOCKER_GID=10001 uv run python scripts/assert_phase10_tool_worker_compose.py --mode rootful
uv run ruff check .
uv run ruff format --check .
uv run mypy
```

Expected: PASS; all six frozen Phase 9 histories replay, package boundaries hold, and rendered topology is exact.

- [ ] **Step 6: Rebuild and run live integration and the true upgrade**

```bash
SANDBOX_DOCKER_GID=10001 docker compose -f compose.yaml -f compose.dev.yaml -f compose.rootful.yaml up -d --build
uv run pytest -m integration tests/integration/test_phase10_tool_worker_boundary.py tests/integration/test_stack_health.py tests/integration/test_phase3_exit.py tests/integration/test_phase6_security.py tests/integration/test_phase6_exit.py tests/integration/test_phase7_exit.py tests/integration/test_phase9_exit.py -v
uv run pytest -m integration tests/integration/test_phase10_live_upgrade.py -v
```

Expected: PASS; agent and tool queues have pollers, ordinary/approved/sync/cleanup effects cross the tool queue, agent pre-bind/post-bind recovery has one manifest and terminal run, tool pre-claim recovery executes exactly once, both post-claim boundaries stop as `execution_unknown` without retrying an effect, wrong-GID startup was separately proven fatal, and existing Phase 3/6/7/9 flows remain green.

Run both documented socket modes and the negative boundary on a Linux acceptance host that provides the two sockets:

```bash
SANDBOX_DOCKER_GID="$(stat -c %g /var/run/docker.sock)" PHASE10_SOCKET_MODE=rootful uv run pytest -m integration tests/integration/test_phase10_sandbox_socket_modes.py -v
PHASE10_ROOTLESS_DOCKER_SOCKET=/run/user/10001/docker.sock PHASE10_SOCKET_MODE=rootless uv run pytest -m integration tests/integration/test_phase10_sandbox_socket_modes.py -v
SANDBOX_DOCKER_GID="$(stat -c %g /var/run/docker.sock)" PHASE10_SOCKET_MODE=wrong-gid uv run pytest -m integration tests/integration/test_phase10_sandbox_socket_modes.py -v
```

Expected: both connection/job probes PASS and the deliberate wrong-GID runner fails closed without changing the socket.

- [ ] **Step 7: Run the full repository gate**

```bash
uv run pytest
pnpm test
pnpm lint
pnpm typecheck
pnpm build
env -u SANDBOX_DOCKER_GID docker compose -f compose.yaml build agent-worker tool-worker sandbox-runner
git diff --check
```

Expected: PASS with fresh results. Do not mark later Phase 10 subprojects complete.

- [ ] **Step 8: Perform exact final staging and commit**

```bash
git add .github/workflows/ci.yml Makefile tests/integration/test_phase10_tool_worker_boundary.py tests/integration/test_phase10_sandbox_socket_modes.py tests/integration/phase10_upgrade_harness.py tests/integration/test_phase10_live_upgrade.py tests/integration/compose.phase10-upgrade.yaml tests/integration/test_phase3_exit.py tests/integration/test_phase6_exit.py tests/integration/test_phase7_exit.py tests/integration/test_phase9_exit.py
git diff --cached --name-only
git diff --cached --check
test "$(git status --short -- orgforge-production-implementation-plan.md)" = "?? orgforge-production-implementation-plan.md"
git commit -m "test: verify Phase 10 tool-worker boundary"
git status --short
```

Expected before commit: the cached-name output contains exactly the eleven listed paths. Expected after commit: no implementation path is modified or untracked; the user-owned `orgforge-production-implementation-plan.md` and any user-owned untracked specs remain untouched and uncommitted.

## Execution Notes

- Compatibility is a release invariant, not temporary test scaffolding. Keep the frozen histories, old activity registrations, compatibility workflows, and stable patch IDs together.
- If a focused task reveals an unrelated pre-existing failure, record the exact command/output and keep it outside the scoped commit; do not absorb unrelated working-tree changes.
- Any runtime attempt to import `jhin_connectors` from agent-worker, import `jhin_agents`/`jhin_models` from tool-worker, accept tool arguments in an execution activity payload, or run sandbox-runner as root is a release blocker.
