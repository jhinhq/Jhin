"""Gateway pipeline: schema rejection, deny-by-default, execution,
approval staging, and approval resolution — all against in-memory SQLite."""

import asyncio
import json
import secrets as stdlib_secrets
from collections.abc import Iterator, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

import pytest
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jhin_db.models import (
    Agent,
    AgentCapabilityGrant,
    AgentRun,
    Approval,
    AuditEvent,
    Connection,
    Message,
    Secret,
    Task,
    ToolCall,
)
from jhin_domain import ApprovalStatus, SecretType, ToolCallStatus, new_uuid7
from jhin_policy import DecisionType, Grant, PolicyDecision, RiskLevel, ToolDefinition
from jhin_secrets import MasterKey, SecretCrypto, SecretStore, get_redactor
from jhin_tools.builtin import (
    ToolCatalog,
    ToolExecutionContext,
    ToolExecutor,
    ToolValidator,
    build_builtin_catalog,
)
from jhin_tools.errors import ToolExecutionError
from jhin_tools.gateway import GatewayOutcome, GatewayStateError, ToolGateway
from jhin_tools.sanitize import MAX_STRING_CHARS


@pytest.fixture(autouse=True)
def _reset_process_redactor() -> Iterator[None]:
    get_redactor().clear()
    yield
    get_redactor().clear()


class _WideApprovalInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(max_length=MAX_STRING_CHARS + 100)


class _ConnectionApprovalInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connection_id: str


class _ChangedRuntimeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    replacement: str


class _NumericInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: float


class _ApprovalOutput(BaseModel):
    executed: bool


async def _approval_executor(ctx: ToolExecutionContext, payload: BaseModel) -> _ApprovalOutput:
    return _ApprovalOutput(executed=True)


def _custom_approval_catalog(
    *,
    input_model: type[BaseModel] = _WideApprovalInput,
    validator: ToolValidator | None = None,
    executor: ToolExecutor = _approval_executor,
) -> ToolCatalog:
    catalog = ToolCatalog()
    scope_keys = ("connection_id",) if input_model is _ConnectionApprovalInput else ()
    catalog.register(
        ToolDefinition(
            name="test.approval_action",
            description="Approval test action",
            risk=RiskLevel.ELEVATED,
            input_model=input_model,
            output_model=_ApprovalOutput,
            required_capability="test.approval_action",
            supports_approval=True,
            scope_keys=scope_keys,
        ),
        executor,
        validator,
    )
    return catalog


async def _grant(
    session: AsyncSession, ctx: ToolExecutionContext, capability: str, effect: str = "allow"
) -> None:
    session.add(
        AgentCapabilityGrant(
            workspace_id=ctx.workspace_id,
            agent_id=ctx.agent_id,
            capability=capability,
            scope_json={},
            effect=effect,
        )
    )
    await session.flush()


async def _audit_actions(session: AsyncSession) -> list[str]:
    rows = await session.scalars(select(AuditEvent.action))
    return list(rows)


async def _persist_execution_context(session: AsyncSession, context: ToolExecutionContext) -> None:
    session.add(
        Task(
            id=context.task_id,
            workspace_id=context.workspace_id,
            title="Runtime invocation",
            assigned_agent_id=context.agent_id,
            correlation_id=new_uuid7(),
        )
    )
    session.add(
        AgentRun(
            id=context.run_id,
            workspace_id=context.workspace_id,
            task_id=context.task_id,
            agent_id=context.agent_id,
        )
    )
    await session.flush()


def _with_isolated_sessions(context: ToolExecutionContext) -> ToolExecutionContext:
    bind = context.session.bind
    assert bind is not None
    return replace(
        context,
        session_factory=async_sessionmaker(bind, expire_on_commit=False),
    )


async def _park_approved_destructive_call(
    gateway: ToolGateway, session: AsyncSession, context: ToolExecutionContext
) -> tuple[GatewayOutcome, Approval]:
    await _grant(session, context, "system.demo.destructive")
    parked = await gateway.request("system.demo.destructive", '{"label": "go"}')
    assert parked.approval_id is not None
    approval = await session.get(Approval, parked.approval_id)
    assert approval is not None
    approval.status = ApprovalStatus.APPROVED.value
    approval.decided_at = datetime.now(UTC)
    await session.flush()
    return parked, approval


async def _mark_approved(session: AsyncSession, outcome: GatewayOutcome) -> Approval:
    assert outcome.approval_id is not None
    approval = await session.get(Approval, outcome.approval_id)
    assert approval is not None
    approval.status = ApprovalStatus.APPROVED.value
    approval.decided_at = datetime.now(UTC)
    await session.flush()
    return approval


async def _make_connection(
    session: AsyncSession, context: ToolExecutionContext
) -> tuple[Connection, SecretStore, Secret]:
    crypto = SecretCrypto(MasterKey(key=stdlib_secrets.token_bytes(32)))
    store = SecretStore(session, crypto)
    secret = await store.create(
        workspace_id=context.workspace_id,
        name=f"approval-credentials-{new_uuid7().hex[:8]}",
        plaintext=json.dumps({"token": "approval-connection-token"}),
        secret_type=SecretType.CONNECTION_CREDENTIALS,
    )
    connection = Connection(
        workspace_id=context.workspace_id,
        connector_type="example",
        name=f"Approval connection {new_uuid7().hex[:8]}",
        auth_type="api_key",
        encrypted_secret_id=secret.id,
        config_json={"project": "safe-project"},
    )
    session.add(connection)
    await session.flush()
    return connection, store, secret


async def test_unknown_tool_is_recorded_denial(gateway: ToolGateway, session: AsyncSession) -> None:
    outcome = await gateway.request("system.nope", "{}")
    assert outcome.status == "denied"
    assert outcome.decision_code == "tool_not_found"
    row = await session.scalar(select(ToolCall))
    assert row is not None
    assert row.status == ToolCallStatus.DENIED.value
    actions = await _audit_actions(session)
    assert "tool.call.requested" in actions
    assert "tool.call.denied" in actions


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity", "1e999", "-1e999"])
async def test_nonstandard_json_number_is_denied_before_executor(
    session: AsyncSession,
    context: ToolExecutionContext,
    constant: str,
) -> None:
    executions = 0

    async def executor(_ctx: ToolExecutionContext, _payload: BaseModel) -> _ApprovalOutput:
        nonlocal executions
        executions += 1
        return _ApprovalOutput(executed=True)

    catalog = ToolCatalog()
    catalog.register(
        ToolDefinition(
            name="test.numeric_input",
            description="Strict JSON number test",
            risk=RiskLevel.READ,
            input_model=_NumericInput,
            output_model=_ApprovalOutput,
            required_capability="test.numeric_input",
        ),
        executor,
    )
    await _grant(session, context, "test.numeric_input")

    outcome = await ToolGateway(context, catalog).request(
        "test.numeric_input", f'{{"value":{constant}}}'
    )

    assert outcome.status == "denied"
    assert outcome.decision_code == "invalid_input"
    assert executions == 0


async def test_hostile_model_metadata_is_redacted_before_truncation(
    gateway: ToolGateway,
    session: AsyncSession,
    context: ToolExecutionContext,
) -> None:
    long_secret = "phase9-model-secret-" + ("x" * 1_500)
    get_redactor().register(long_secret)

    denied = await gateway.request(long_secret, long_secret)
    row = await session.get(ToolCall, denied.tool_call_id)
    assert row is not None
    persisted_denial = json.dumps(
        {
            "tool_name": row.tool_name,
            "input": row.sanitized_input_json,
            "reason": denied.decision_reason,
        }
    )
    assert long_secret not in persisted_denial
    assert ("x" * 100) not in persisted_denial
    assert "[REDACTED]" in persisted_denial

    await _grant(session, context, "test.approval_action")
    parked = await ToolGateway(context, _custom_approval_catalog()).request(
        "test.approval_action",
        '{"label":"safe"}',
        provider_call_id=long_secret,
    )
    assert parked.approval_id is not None
    approval = await session.get(Approval, parked.approval_id)
    assert approval is not None
    assert approval.action_payload_sanitized["provider_call_id"] == "[REDACTED]"


async def test_schema_violation_is_denied_before_policy(
    gateway: ToolGateway, session: AsyncSession, context: ToolExecutionContext
) -> None:
    await _grant(session, context, "system.echo")
    for bad in ('{"text": 42, "extra": true}', "not json", '["list"]'):
        outcome = await gateway.request("system.echo", bad)
        assert outcome.status == "denied"
        assert outcome.decision_code == "invalid_input"


async def test_deny_by_default_without_grant(gateway: ToolGateway, session: AsyncSession) -> None:
    outcome = await gateway.request("system.echo", '{"text": "hi"}')
    assert outcome.status == "denied"
    assert outcome.decision_code == "no_grant"
    row = await session.scalar(select(ToolCall))
    assert row is not None
    assert row.status == ToolCallStatus.DENIED.value
    assert row.error_code == "no_grant"


async def test_granted_read_tool_executes_and_persists(
    gateway: ToolGateway, session: AsyncSession, context: ToolExecutionContext
) -> None:
    await _grant(session, context, "system.echo")
    outcome = await gateway.request("system.echo", '{"text": "hello"}')
    assert outcome.status == "executed"
    assert outcome.sanitized_output == {"text": "hello"}
    assert json.loads(outcome.observation_json()) == {"text": "hello"}
    row = await session.scalar(select(ToolCall))
    assert row is not None
    assert row.status == ToolCallStatus.COMPLETED.value
    assert row.duration_ms is not None
    assert "tool.call.executed" in await _audit_actions(session)


async def test_deterministic_runtime_invocation_replays_auto_mutation_once(
    session: AsyncSession, context: ToolExecutionContext
) -> None:
    await _persist_execution_context(session, context)
    await _grant(session, context, "test.auto_mutation")
    agent = await session.get(Agent, context.agent_id)
    assert agent is not None
    agent.approval_policy_json = [{"capability": "test.auto_mutation", "action": "auto"}]
    await session.commit()
    context = _with_isolated_sessions(context)
    executions = 0

    async def executor(executor_ctx: ToolExecutionContext, payload: BaseModel) -> _ApprovalOutput:
        nonlocal executions
        executions += 1
        return _ApprovalOutput(executed=True)

    catalog = ToolCatalog()
    catalog.register(
        ToolDefinition(
            name="test.auto_mutation",
            description="External mutation used to verify durable invocation claims",
            risk=RiskLevel.ELEVATED,
            input_model=_WideApprovalInput,
            output_model=_ApprovalOutput,
            required_capability="test.auto_mutation",
            supports_approval=True,
        ),
        executor,
    )
    gateway = ToolGateway(context, catalog)
    invocation_id = new_uuid7()

    first = await gateway.request(
        "test.auto_mutation",
        '{"label": "charge-once"}',
        provider_call_id="provider-first",
        invocation_id=invocation_id,
    )
    await session.commit()
    replay = await gateway.request(
        "test.auto_mutation",
        '{"label": "charge-once"}',
        provider_call_id="provider-regenerated",
        invocation_id=invocation_id,
    )

    assert first.status == replay.status == "executed"
    assert first.tool_call_id == replay.tool_call_id == invocation_id
    assert replay.replayed is True
    assert executions == 1
    assert (await _audit_actions(session)).count("tool.call.claimed") == 1


async def test_uncertain_runtime_invocation_never_reexecutes_auto_mutation(
    session: AsyncSession, context: ToolExecutionContext
) -> None:
    await _persist_execution_context(session, context)
    await _grant(session, context, "test.auto_mutation")
    agent = await session.get(Agent, context.agent_id)
    assert agent is not None
    agent.approval_policy_json = [{"capability": "test.auto_mutation", "action": "auto"}]
    await session.commit()
    context = _with_isolated_sessions(context)
    executions = 0

    async def interrupted_executor(
        executor_ctx: ToolExecutionContext, payload: BaseModel
    ) -> _ApprovalOutput:
        nonlocal executions
        executions += 1
        raise asyncio.CancelledError

    catalog = ToolCatalog()
    catalog.register(
        ToolDefinition(
            name="test.auto_mutation",
            description="Interrupted external mutation",
            risk=RiskLevel.ELEVATED,
            input_model=_WideApprovalInput,
            output_model=_ApprovalOutput,
            required_capability="test.auto_mutation",
            supports_approval=True,
        ),
        interrupted_executor,
    )
    gateway = ToolGateway(context, catalog)
    invocation_id = new_uuid7()

    with pytest.raises(asyncio.CancelledError):
        await gateway.request(
            "test.auto_mutation",
            '{"label": "charge-once"}',
            invocation_id=invocation_id,
        )
    unknown = await gateway.request(
        "test.auto_mutation",
        '{"label": "charge-once"}',
        invocation_id=invocation_id,
    )

    assert unknown.status == "execution_unknown"
    assert unknown.replayed is True
    row = await session.get(ToolCall, invocation_id)
    assert row is not None
    assert row.status == ToolCallStatus.EXECUTION_UNKNOWN.value
    assert executions == 1


async def test_pre_effect_runtime_failure_persists_provider_code_and_replays(
    session: AsyncSession, context: ToolExecutionContext
) -> None:
    await _persist_execution_context(session, context)
    await _grant(session, context, "test.provider_read")
    await session.commit()
    context = _with_isolated_sessions(context)
    executions = 0
    exception_secret = "provider-exception-secret-must-not-persist"

    async def executor(executor_ctx: ToolExecutionContext, payload: BaseModel) -> _ApprovalOutput:
        nonlocal executions
        executions += 1
        raise ToolExecutionError(
            exception_secret,
            code="project_scope_mismatch",
            side_effect_possible=False,
        )

    catalog = ToolCatalog()
    catalog.register(
        ToolDefinition(
            name="test.provider_read",
            description="Provider validation failure",
            risk=RiskLevel.READ,
            input_model=_WideApprovalInput,
            output_model=_ApprovalOutput,
            required_capability="test.provider_read",
        ),
        executor,
    )
    invocation_id = new_uuid7()
    gateway = ToolGateway(context, catalog)

    first = await gateway.request(
        "test.provider_read",
        '{"label": "scope-check"}',
        invocation_id=invocation_id,
    )
    replay = await gateway.request(
        "test.provider_read",
        '{"label": "scope-check"}',
        invocation_id=invocation_id,
    )

    assert first.status == replay.status == "failed"
    assert first.error_code == replay.error_code == "project_scope_mismatch"
    assert first.sanitized_output == replay.sanitized_output == {"error": "project_scope_mismatch"}
    assert replay.replayed is True
    assert executions == 1
    row = await session.get(ToolCall, invocation_id)
    assert row is not None
    assert row.status == ToolCallStatus.FAILED.value
    assert row.error_code == "project_scope_mismatch"
    audit_rows = list(await session.scalars(select(AuditEvent)))
    persisted = json.dumps(
        {
            "outcomes": [first.model_dump(mode="json"), replay.model_dump(mode="json")],
            "tool_call": {
                "input": row.sanitized_input_json,
                "output": row.sanitized_output_json,
                "error_code": row.error_code,
            },
            "audits": [audit.metadata_json for audit in audit_rows],
        },
        default=str,
    )
    assert exception_secret not in persisted


@pytest.mark.parametrize(
    ("side_effect_possible", "raised_code", "expected_status", "expected_code"),
    [
        (False, "repository_scope_mismatch", "failed", "repository_scope_mismatch"),
        (True, "provider_transport_error", "execution_unknown", "execution_outcome_unknown"),
    ],
)
async def test_approved_typed_failure_preserves_only_proven_pre_effect_outcomes(
    session: AsyncSession,
    context: ToolExecutionContext,
    side_effect_possible: bool,
    raised_code: str,
    expected_status: str,
    expected_code: str,
) -> None:
    executions = 0

    async def executor(executor_ctx: ToolExecutionContext, payload: BaseModel) -> _ApprovalOutput:
        nonlocal executions
        executions += 1
        raise ToolExecutionError(
            "Bounded provider failure",
            code=raised_code,
            side_effect_possible=side_effect_possible,
        )

    catalog = _custom_approval_catalog(executor=executor)
    gateway = ToolGateway(context, catalog)
    await _grant(session, context, "test.approval_action")
    parked = await gateway.request("test.approval_action", '{"label": "validate"}')
    approval = await _mark_approved(session, parked)

    outcome = await gateway.resolve_approved(approval.id)

    assert outcome.status == expected_status
    assert outcome.error_code == expected_code
    assert executions == 1
    row = await session.get(ToolCall, parked.tool_call_id)
    assert row is not None
    assert row.status == (
        ToolCallStatus.FAILED.value
        if side_effect_possible is False
        else ToolCallStatus.EXECUTION_UNKNOWN.value
    )


@pytest.mark.parametrize("mismatch", ["tool", "input", "task"])
async def test_runtime_invocation_reuse_verifies_tool_input_and_context(
    session: AsyncSession,
    context: ToolExecutionContext,
    mismatch: str,
) -> None:
    await _persist_execution_context(session, context)
    await _grant(session, context, "system.*")
    await session.commit()
    context = _with_isolated_sessions(context)
    invocation_id = new_uuid7()
    gateway = ToolGateway(context, build_builtin_catalog())
    outcome = await gateway.request(
        "system.echo", '{"text": "original"}', invocation_id=invocation_id
    )
    assert outcome.status == "executed"
    await session.commit()

    if mismatch == "tool":
        tool_name, arguments, retry_context = "system.time", "{}", context
    elif mismatch == "input":
        tool_name, arguments, retry_context = (
            "system.echo",
            '{"text": "changed"}',
            context,
        )
    else:
        tool_name, arguments, retry_context = (
            "system.echo",
            '{"text": "original"}',
            replace(context, task_id=new_uuid7()),
        )

    mismatch_outcome = await ToolGateway(retry_context, build_builtin_catalog()).request(
        tool_name,
        arguments,
        invocation_id=invocation_id,
    )

    assert mismatch_outcome.status == "denied"
    assert mismatch_outcome.decision_code == "invocation_mismatch"
    failure = await session.scalar(
        select(AuditEvent).where(
            AuditEvent.action == "tool.call.denied",
            AuditEvent.target_id == invocation_id,
        )
    )
    assert failure is not None
    assert failure.metadata_json["code"] == "invocation_mismatch"


@pytest.mark.parametrize("drift", ["missing", "schema"])
async def test_terminal_runtime_invocation_repairs_after_catalog_drift(
    session: AsyncSession,
    context: ToolExecutionContext,
    drift: str,
) -> None:
    await _persist_execution_context(session, context)
    await _grant(session, context, "system.echo")
    await session.commit()
    context = _with_isolated_sessions(context)
    invocation_id = new_uuid7()
    first = await ToolGateway(context, build_builtin_catalog()).request(
        "system.echo",
        '{"text":"already-ran"}',
        invocation_id=invocation_id,
    )
    assert first.status == "executed"

    drifted = ToolCatalog()
    if drift == "schema":
        drifted.register(
            ToolDefinition(
                name="system.echo",
                description="Changed schema",
                risk=RiskLevel.READ,
                input_model=_ChangedRuntimeInput,
                output_model=_ApprovalOutput,
                required_capability="system.echo",
            ),
            _approval_executor,
        )
    replay = await ToolGateway(context, drifted).request(
        "system.echo",
        '{"text":"already-ran"}',
        invocation_id=invocation_id,
    )

    assert replay.status == "executed"
    assert replay.replayed is True
    assert replay.tool_call_id == invocation_id
    assert replay.sanitized_input == {"text": "already-ran"}

    mismatch = await ToolGateway(context, drifted).request(
        "system.echo",
        '{"text":"changed-after-drift"}',
        invocation_id=invocation_id,
    )
    assert mismatch.status == "denied"
    assert mismatch.decision_code == "invocation_mismatch"
    assert "tool.call.denied" in await _audit_actions(session)


async def test_pending_runtime_approval_with_missing_tool_fails_closed(
    session: AsyncSession,
    context: ToolExecutionContext,
) -> None:
    await _persist_execution_context(session, context)
    await _grant(session, context, "system.demo.destructive")
    await session.commit()
    context = _with_isolated_sessions(context)
    invocation_id = new_uuid7()
    parked = await ToolGateway(context, build_builtin_catalog()).request(
        "system.demo.destructive",
        '{"label":"parked"}',
        invocation_id=invocation_id,
    )
    assert parked.status == "needs_approval"

    denied = await ToolGateway(context, ToolCatalog()).request(
        "system.demo.destructive",
        '{"label":"parked"}',
        invocation_id=invocation_id,
    )

    assert denied.status == "denied"
    assert denied.decision_code == "invocation_mismatch"
    row = await session.get(ToolCall, invocation_id)
    assert row is not None
    assert row.status == ToolCallStatus.PENDING_APPROVAL.value


async def test_pending_approval_retry_reuses_deterministic_invocation(
    session: AsyncSession, context: ToolExecutionContext
) -> None:
    await _persist_execution_context(session, context)
    await _grant(session, context, "system.demo.destructive")
    await session.commit()
    context = _with_isolated_sessions(context)
    gateway = ToolGateway(context, build_builtin_catalog())
    invocation_id = new_uuid7()

    first = await gateway.request(
        "system.demo.destructive",
        '{"label": "review-once"}',
        provider_call_id="provider-first",
        invocation_id=invocation_id,
    )
    await session.commit()
    retry = await gateway.request(
        "system.demo.destructive",
        '{"label": "review-once"}',
        provider_call_id="provider-regenerated",
        invocation_id=invocation_id,
    )

    assert first.status == retry.status == "needs_approval"
    assert retry.replayed is True
    assert retry.tool_call_id == invocation_id
    assert retry.approval_id == first.approval_id
    approvals = (await session.scalars(select(Approval))).all()
    assert len(approvals) == 1
    assert approvals[0].action_payload_sanitized["invocation_format_version"] == 1
    assert approvals[0].action_payload_sanitized["invocation_id"] == str(invocation_id)


async def test_deterministic_runtime_invocation_rejects_lossy_input(
    session: AsyncSession, context: ToolExecutionContext
) -> None:
    await _persist_execution_context(session, context)
    await _grant(session, context, "system.echo")
    await session.commit()
    context = _with_isolated_sessions(context)
    secret_value = "runtime-input-secret"
    get_redactor().register(secret_value)

    outcome = await ToolGateway(context, build_builtin_catalog()).request(
        "system.echo",
        json.dumps({"text": secret_value}),
        invocation_id=new_uuid7(),
    )

    assert outcome.status == "denied"
    assert outcome.decision_code == "invocation_input_not_lossless"
    assert outcome.sanitized_input == {"text": "[REDACTED]"}


async def test_explicit_deny_beats_allow(
    gateway: ToolGateway, session: AsyncSession, context: ToolExecutionContext
) -> None:
    await _grant(session, context, "system.*")
    await _grant(session, context, "system.echo", effect="deny")
    outcome = await gateway.request("system.echo", '{"text": "hi"}')
    assert outcome.status == "denied"
    assert outcome.decision_code == "explicit_deny"
    # The wildcard allow still covers the other read tool.
    outcome_time = await gateway.request("system.time", "{}")
    assert outcome_time.status == "executed"


async def test_note_append_writes_visible_note(
    gateway: ToolGateway, session: AsyncSession, context: ToolExecutionContext
) -> None:
    await _grant(session, context, "system.note.append")
    outcome = await gateway.request("system.note.append", '{"text": "remember this"}')
    assert outcome.status == "executed"
    note = await session.scalar(select(Message))
    assert note is not None
    assert note.content_json == {"text": "remember this"}
    assert note.task_id == context.task_id


async def test_destructive_tool_stages_pending_approval(
    gateway: ToolGateway, session: AsyncSession, context: ToolExecutionContext
) -> None:
    await _grant(session, context, "system.demo.destructive")
    outcome = await gateway.request("system.demo.destructive", '{"label": "wipe"}')
    assert outcome.status == "needs_approval"
    assert outcome.approval_id is not None
    approval = await session.scalar(select(Approval))
    assert approval is not None
    assert approval.status == ApprovalStatus.PENDING.value
    payload = approval.action_payload_sanitized
    assert payload["approval_format_version"] == 2
    assert payload["workspace_id"] == str(context.workspace_id)
    assert payload["agent_id"] == str(context.agent_id)
    assert payload["run_id"] == str(context.run_id)
    assert payload["task_id"] == str(context.task_id)
    assert payload["tool_name"] == "system.demo.destructive"
    assert payload["capability"] == "system.demo.destructive"
    assert payload["risk"] == "destructive"
    assert payload["input"] == {"label": "wipe"}
    row = await session.scalar(select(ToolCall))
    assert row is not None
    assert row.status == ToolCallStatus.PENDING_APPROVAL.value
    assert row.approval_id == approval.id
    assert "approval.requested" in await _audit_actions(session)


async def test_resolve_approved_executes_the_parked_call(
    gateway: ToolGateway, session: AsyncSession, context: ToolExecutionContext
) -> None:
    await _grant(session, context, "system.demo.destructive")
    parked = await gateway.request("system.demo.destructive", '{"label": "go"}')
    assert parked.approval_id is not None

    approval = await session.get(Approval, parked.approval_id)
    assert approval is not None
    approval.status = ApprovalStatus.APPROVED.value
    approval.decided_at = datetime.now(UTC)
    await session.flush()

    outcome = await gateway.resolve_approved(parked.approval_id)
    assert outcome.status == "executed"
    assert outcome.sanitized_output is not None
    row = await session.get(ToolCall, parked.tool_call_id)
    assert row is not None
    assert row.status == ToolCallStatus.COMPLETED.value
    actions = await _audit_actions(session)
    assert "tool.call.claimed" in actions
    assert "tool.call.approved" in actions
    assert "demo.destructive.marker" in actions  # the inert destructive effect


async def test_terminal_approval_resolution_replays_without_executing_again(
    gateway: ToolGateway, session: AsyncSession, context: ToolExecutionContext
) -> None:
    parked, _ = await _park_approved_destructive_call(gateway, session, context)
    assert parked.approval_id is not None
    first = await gateway.resolve_approved(parked.approval_id)
    await session.commit()

    replay = await gateway.resolve_approved(parked.approval_id)

    assert first.status == replay.status == "executed"
    assert replay.replayed is True
    assert replay.sanitized_output == first.sanitized_output
    actions = await _audit_actions(session)
    assert actions.count("demo.destructive.marker") == 1
    assert actions.count("tool.call.claimed") == 1


@pytest.mark.parametrize(
    "uncertain_status",
    [ToolCallStatus.EXECUTING.value, ToolCallStatus.EXECUTION_UNKNOWN.value],
)
async def test_uncertain_approval_execution_never_invokes_the_executor_again(
    gateway: ToolGateway,
    session: AsyncSession,
    context: ToolExecutionContext,
    uncertain_status: str,
) -> None:
    parked, _ = await _park_approved_destructive_call(gateway, session, context)
    assert parked.approval_id is not None
    row = await session.get(ToolCall, parked.tool_call_id)
    assert row is not None
    row.status = uncertain_status
    await session.commit()

    unknown = await gateway.resolve_approved(parked.approval_id)

    assert unknown.status == "execution_unknown"
    await session.refresh(row)
    assert row.status == ToolCallStatus.EXECUTION_UNKNOWN.value
    assert "demo.destructive.marker" not in await _audit_actions(session)


async def test_legacy_approval_format_fails_closed_and_is_audited(
    gateway: ToolGateway, session: AsyncSession, context: ToolExecutionContext
) -> None:
    parked, approval = await _park_approved_destructive_call(gateway, session, context)
    payload = dict(approval.action_payload_sanitized)
    payload.pop("approval_format_version")
    approval.action_payload_sanitized = payload
    await session.flush()

    outcome = await gateway.resolve_approved(cast(UUID, parked.approval_id))

    assert outcome.status == "failed"
    assert outcome.decision_code == "approval_format_unsupported"
    assert "tool.call.failed" in await _audit_actions(session)
    assert "demo.destructive.marker" not in await _audit_actions(session)


@pytest.mark.parametrize(
    ("field", "tampered"),
    [
        ("workspace_id", "00000000-0000-0000-0000-000000000011"),
        ("agent_id", "00000000-0000-0000-0000-000000000012"),
        ("run_id", "00000000-0000-0000-0000-000000000013"),
        ("task_id", "00000000-0000-0000-0000-000000000014"),
        ("tool_name", "system.demo.changed"),
        ("input", {"label": "changed"}),
        ("tool_call_id", "00000000-0000-0000-0000-000000000015"),
        ("invocation_id", None),
        ("invocation_id", "00000000-0000-0000-0000-000000000001"),
        ("invocation_format_version", None),
        ("invocation_format_version", 999),
    ],
)
async def test_approval_runtime_invocation_binding_is_required(
    gateway: ToolGateway,
    session: AsyncSession,
    context: ToolExecutionContext,
    field: str,
    tampered: object,
) -> None:
    parked, approval = await _park_approved_destructive_call(gateway, session, context)
    payload = dict(approval.action_payload_sanitized)
    if tampered is None:
        payload.pop(field)
    else:
        payload[field] = tampered
    approval.action_payload_sanitized = payload
    await session.flush()

    outcome = await gateway.resolve_approved(cast(UUID, parked.approval_id))

    assert outcome.status == "failed"
    assert outcome.decision_code == "approval_binding_mismatch"
    assert "tool.call.failed" in await _audit_actions(session)
    assert "demo.destructive.marker" not in await _audit_actions(session)


@pytest.mark.parametrize(
    ("definition_change", "expected_code"),
    [
        ("capability", "approval_definition_changed"),
        ("risk", "approval_definition_changed"),
        ("input_schema", "invalid_input"),
    ],
)
async def test_definition_drift_fails_closed_before_executor(
    gateway: ToolGateway,
    session: AsyncSession,
    context: ToolExecutionContext,
    definition_change: str,
    expected_code: str,
) -> None:
    parked, _ = await _park_approved_destructive_call(gateway, session, context)
    original_entry = build_builtin_catalog().get("system.demo.destructive")
    assert original_entry is not None
    definition, executor = original_entry
    updates: dict[str, object]
    if definition_change == "capability":
        updates = {"required_capability": "system.demo.elevated"}
    elif definition_change == "risk":
        updates = {"risk": RiskLevel.ELEVATED}
    else:
        updates = {"input_model": _ChangedRuntimeInput}
    drifted_catalog = ToolCatalog()
    drifted_catalog.register(definition.model_copy(update=updates), executor)

    outcome = await ToolGateway(context, drifted_catalog).resolve_approved(
        cast(UUID, parked.approval_id)
    )

    assert outcome.status == "failed"
    assert outcome.decision_code == expected_code
    assert "tool.call.failed" in await _audit_actions(session)
    assert "demo.destructive.marker" not in await _audit_actions(session)


async def test_tool_validator_is_rerun_after_approval(
    session: AsyncSession, context: ToolExecutionContext
) -> None:
    state = {"deny": False, "executions": 0}

    async def validator(
        validator_ctx: ToolExecutionContext,
        payload: BaseModel,
        grants: Sequence[Grant],
    ) -> PolicyDecision | None:
        if state["deny"]:
            return PolicyDecision(
                decision=DecisionType.DENY,
                code="validator_changed",
                reason="validator state changed while parked",
            )
        return None

    async def executor(executor_ctx: ToolExecutionContext, payload: BaseModel) -> _ApprovalOutput:
        state["executions"] += 1
        return _ApprovalOutput(executed=True)

    catalog = _custom_approval_catalog(validator=validator, executor=executor)
    gateway = ToolGateway(context, catalog)
    await _grant(session, context, "test.approval_action")
    parked = await gateway.request("test.approval_action", '{"label": "review"}')
    await _mark_approved(session, parked)
    state["deny"] = True

    outcome = await gateway.resolve_approved(cast(UUID, parked.approval_id))

    assert outcome.status == "denied"
    assert outcome.decision_code == "validator_changed"
    assert state["executions"] == 0
    assert "tool.call.denied" in await _audit_actions(session)


@pytest.mark.parametrize("drift", ["credentials", "status", "config", "auth_type", "deleted"])
async def test_connection_authorization_drift_requires_a_new_approval(
    session: AsyncSession, context: ToolExecutionContext, drift: str
) -> None:
    executions = 0

    async def executor(executor_ctx: ToolExecutionContext, payload: BaseModel) -> _ApprovalOutput:
        nonlocal executions
        executions += 1
        return _ApprovalOutput(executed=True)

    connection, store, secret = await _make_connection(session, context)
    catalog = _custom_approval_catalog(input_model=_ConnectionApprovalInput, executor=executor)
    gateway = ToolGateway(context, catalog)
    await _grant(session, context, "test.approval_action")
    parked = await gateway.request(
        "test.approval_action", json.dumps({"connection_id": str(connection.id)})
    )
    assert parked.approval_id is not None
    approval = await session.get(Approval, parked.approval_id)
    assert approval is not None
    payload = approval.action_payload_sanitized
    digest = payload["connection_authorization_digest"]
    assert isinstance(digest, str) and len(digest) == 64
    serialized = json.dumps(payload)
    assert "approval-connection-token" not in serialized
    assert secret.secret_fingerprint not in serialized
    assert "safe-project" not in serialized

    if drift == "credentials":
        await store.rotate(
            context.workspace_id,
            secret.id,
            json.dumps({"token": "rotated-approval-token"}),
        )
    elif drift == "status":
        connection.status = "disabled"
    elif drift == "config":
        connection.config_json = {"project": "changed-project"}
    elif drift == "auth_type":
        connection.auth_type = "oauth"
    else:
        await session.delete(connection)
    await _mark_approved(session, parked)

    outcome = await gateway.resolve_approved(parked.approval_id)

    assert outcome.status == "denied"
    assert outcome.decision_code == "approval_connection_changed"
    assert executions == 0
    assert "tool.call.denied" in await _audit_actions(session)


async def test_approval_is_denied_if_grant_was_revoked_while_parked(
    gateway: ToolGateway, session: AsyncSession, context: ToolExecutionContext
) -> None:
    parked, _ = await _park_approved_destructive_call(gateway, session, context)
    grant = await session.scalar(select(AgentCapabilityGrant))
    assert grant is not None
    await session.delete(grant)
    await session.flush()

    outcome = await gateway.resolve_approved(parked.approval_id)  # type: ignore[arg-type]

    assert outcome.status == "denied"
    assert outcome.decision_code == "no_grant"
    row = await session.get(ToolCall, parked.tool_call_id)
    assert row is not None
    assert row.status == ToolCallStatus.DENIED.value
    assert row.error_code == "no_grant"
    actions = await _audit_actions(session)
    assert "tool.call.denied" in actions
    assert "demo.destructive.marker" not in actions


async def test_approval_is_denied_if_explicit_deny_was_added_while_parked(
    gateway: ToolGateway, session: AsyncSession, context: ToolExecutionContext
) -> None:
    parked, _ = await _park_approved_destructive_call(gateway, session, context)
    await _grant(session, context, "system.demo.destructive", effect="deny")

    outcome = await gateway.resolve_approved(parked.approval_id)  # type: ignore[arg-type]

    assert outcome.status == "denied"
    assert outcome.decision_code == "explicit_deny"
    assert "demo.destructive.marker" not in await _audit_actions(session)


async def test_approval_is_denied_if_policy_forbids_it_while_parked(
    gateway: ToolGateway, session: AsyncSession, context: ToolExecutionContext
) -> None:
    parked, _ = await _park_approved_destructive_call(gateway, session, context)
    agent = await session.get(Agent, context.agent_id)
    assert agent is not None
    agent.approval_policy_json = [{"capability": "system.demo.destructive", "action": "forbid"}]
    await session.flush()

    outcome = await gateway.resolve_approved(parked.approval_id)  # type: ignore[arg-type]

    assert outcome.status == "denied"
    assert outcome.decision_code == "forbidden_by_policy"
    assert "demo.destructive.marker" not in await _audit_actions(session)


@pytest.mark.parametrize("identity_field", ["agent_id", "run_id", "task_id"])
async def test_approval_resume_is_bound_to_original_execution_identity(
    gateway: ToolGateway,
    session: AsyncSession,
    context: ToolExecutionContext,
    identity_field: str,
) -> None:
    parked, _ = await _park_approved_destructive_call(gateway, session, context)
    if identity_field == "agent_id":
        mismatched = replace(context, agent_id=new_uuid7())
    elif identity_field == "run_id":
        mismatched = replace(context, run_id=new_uuid7())
    else:
        mismatched = replace(context, task_id=new_uuid7())
    other_gateway = ToolGateway(mismatched, build_builtin_catalog())

    with pytest.raises(GatewayStateError, match="does not belong to this execution context"):
        await other_gateway.resolve_approved(parked.approval_id)  # type: ignore[arg-type]

    assert "demo.destructive.marker" not in await _audit_actions(session)
    failure = await session.scalar(
        select(AuditEvent).where(
            AuditEvent.action == "tool.call.failed",
            AuditEvent.target_id == parked.approval_id,
        )
    )
    assert failure is not None
    assert failure.metadata_json["code"] == "approval_execution_context_mismatch"


async def test_approval_resume_from_wrong_workspace_is_hidden_and_has_no_effect(
    gateway: ToolGateway, session: AsyncSession, context: ToolExecutionContext
) -> None:
    parked, _ = await _park_approved_destructive_call(gateway, session, context)
    other_gateway = ToolGateway(replace(context, workspace_id=new_uuid7()), build_builtin_catalog())

    with pytest.raises(GatewayStateError, match="not found in workspace"):
        await other_gateway.resolve_approved(cast(UUID, parked.approval_id))

    assert "demo.destructive.marker" not in await _audit_actions(session)
    failure = await session.scalar(
        select(AuditEvent).where(
            AuditEvent.action == "tool.call.failed",
            AuditEvent.target_id == parked.approval_id,
        )
    )
    assert failure is not None
    assert failure.metadata_json["code"] == "approval_not_found"


async def test_approval_resume_rejects_a_mismatched_tool_call_row(
    gateway: ToolGateway, session: AsyncSession, context: ToolExecutionContext
) -> None:
    parked, _ = await _park_approved_destructive_call(gateway, session, context)
    row = await session.get(ToolCall, parked.tool_call_id)
    assert row is not None
    row.run_id = new_uuid7()
    await session.flush()

    with pytest.raises(GatewayStateError, match="does not match its approval"):
        await gateway.resolve_approved(parked.approval_id)  # type: ignore[arg-type]

    assert "demo.destructive.marker" not in await _audit_actions(session)
    failure = await session.scalar(
        select(AuditEvent).where(
            AuditEvent.action == "tool.call.failed",
            AuditEvent.target_id == parked.approval_id,
        )
    )
    assert failure is not None
    assert failure.metadata_json["code"] == "approval_tool_call_mismatch"


@pytest.mark.parametrize(
    ("pair_state", "expected_message", "expected_code"),
    [
        ("missing", "no tool_call row", "approval_tool_call_missing"),
        ("ambiguous", "multiple tool_call rows", "approval_tool_call_ambiguous"),
    ],
)
async def test_approval_resume_audits_missing_or_ambiguous_tool_call_pair(
    gateway: ToolGateway,
    session: AsyncSession,
    context: ToolExecutionContext,
    pair_state: str,
    expected_message: str,
    expected_code: str,
) -> None:
    parked, _approval = await _park_approved_destructive_call(gateway, session, context)
    row = await session.get(ToolCall, parked.tool_call_id)
    assert row is not None
    if pair_state == "missing":
        row.approval_id = None
    else:
        session.add(
            ToolCall(
                workspace_id=context.workspace_id,
                run_id=context.run_id,
                agent_id=context.agent_id,
                tool_name=row.tool_name,
                sanitized_input_json=dict(row.sanitized_input_json),
                sanitized_output_json={},
                status=ToolCallStatus.PENDING_APPROVAL.value,
                approval_id=parked.approval_id,
                started_at=datetime.now(UTC),
            )
        )
    await session.flush()

    with pytest.raises(GatewayStateError, match=expected_message):
        await gateway.resolve_approved(cast(UUID, parked.approval_id))

    failure = await session.scalar(
        select(AuditEvent).where(
            AuditEvent.action == "tool.call.failed",
            AuditEvent.target_id == parked.approval_id,
        )
    )
    assert failure is not None
    assert failure.metadata_json["code"] == expected_code
    assert "demo.destructive.marker" not in await _audit_actions(session)


async def test_malformed_stored_approval_input_fails_with_an_audit(
    gateway: ToolGateway, session: AsyncSession, context: ToolExecutionContext
) -> None:
    parked, approval = await _park_approved_destructive_call(gateway, session, context)
    row = await session.get(ToolCall, parked.tool_call_id)
    assert row is not None
    malformed = {"label": 42}
    row.sanitized_input_json = malformed
    payload = dict(approval.action_payload_sanitized)
    payload["input"] = malformed
    approval.action_payload_sanitized = payload
    await session.flush()

    outcome = await gateway.resolve_approved(cast(UUID, parked.approval_id))

    assert outcome.status == "failed"
    assert outcome.decision_code == "invalid_input"
    assert "tool.call.failed" in await _audit_actions(session)
    assert "demo.destructive.marker" not in await _audit_actions(session)


async def test_lossy_sanitization_cannot_create_a_replayable_approval(
    gateway: ToolGateway, session: AsyncSession, context: ToolExecutionContext
) -> None:
    from jhin_secrets.redaction import get_redactor

    secret_label = "approval-secret-value"
    await _grant(session, context, "system.demo.destructive")
    get_redactor().register(secret_label)
    try:
        outcome = await gateway.request(
            "system.demo.destructive", json.dumps({"label": secret_label})
        )
    finally:
        get_redactor().clear()

    assert outcome.status == "denied"
    assert outcome.decision_code == "approval_input_not_lossless"
    assert (await session.scalars(select(Approval))).all() == []
    row = await session.get(ToolCall, outcome.tool_call_id)
    assert row is not None
    assert row.status == ToolCallStatus.DENIED.value
    assert row.sanitized_input_json == {"label": "[REDACTED]"}


async def test_truncated_input_cannot_create_a_replayable_approval(
    session: AsyncSession, context: ToolExecutionContext
) -> None:
    await _grant(session, context, "system.demo.destructive")
    small_gateway = ToolGateway(context, build_builtin_catalog(), max_output_bytes=64)

    outcome = await small_gateway.request(
        "system.demo.destructive", json.dumps({"label": "x" * 200})
    )

    assert outcome.status == "denied"
    assert outcome.decision_code == "approval_input_not_lossless"
    assert (await session.scalars(select(Approval))).all() == []
    assert outcome.sanitized_input.get("truncated") is True


async def test_schema_valid_string_over_leaf_limit_cannot_be_approved(
    session: AsyncSession, context: ToolExecutionContext
) -> None:
    catalog = _custom_approval_catalog()
    gateway = ToolGateway(context, catalog)
    await _grant(session, context, "test.approval_action")

    outcome = await gateway.request(
        "test.approval_action", json.dumps({"label": "x" * (MAX_STRING_CHARS + 1)})
    )

    assert outcome.status == "denied"
    assert outcome.decision_code == "approval_input_not_lossless"
    assert (await session.scalars(select(Approval))).all() == []


async def test_resolve_rejected_records_denial(
    gateway: ToolGateway, session: AsyncSession, context: ToolExecutionContext
) -> None:
    await _grant(session, context, "system.demo.destructive")
    parked = await gateway.request("system.demo.destructive", '{"label": "no"}')
    assert parked.approval_id is not None

    approval = await session.get(Approval, parked.approval_id)
    assert approval is not None
    approval.status = ApprovalStatus.REJECTED.value
    await session.flush()

    outcome = await gateway.resolve_rejected(parked.approval_id)
    assert outcome.status == "rejected"
    assert outcome.error_code == "approval_rejected"
    row = await session.get(ToolCall, parked.tool_call_id)
    assert row is not None
    assert row.status == ToolCallStatus.REJECTED.value
    observation = json.loads(outcome.observation_json())
    assert observation["error"] == "approval_rejected"


@pytest.mark.parametrize(
    ("field", "tampered", "expected_code"),
    [
        ("approval_format_version", 999, "approval_format_unsupported"),
        ("workspace_id", "00000000-0000-0000-0000-000000000021", "approval_binding_mismatch"),
        ("input", {"label": "changed"}, "approval_binding_mismatch"),
        ("tool_call_id", "00000000-0000-0000-0000-000000000022", "approval_binding_mismatch"),
        ("invocation_id", "00000000-0000-0000-0000-000000000023", "approval_binding_mismatch"),
        ("capability", "system.demo.changed", "approval_definition_changed"),
        ("risk", RiskLevel.READ.value, "approval_definition_changed"),
        ("connection_authorization_digest", "unexpected", "approval_binding_mismatch"),
    ],
)
async def test_rejected_approval_still_requires_exact_payload_binding(
    gateway: ToolGateway,
    session: AsyncSession,
    context: ToolExecutionContext,
    field: str,
    tampered: object,
    expected_code: str,
) -> None:
    await _grant(session, context, "system.demo.destructive")
    parked = await gateway.request("system.demo.destructive", '{"label":"no"}')
    assert parked.approval_id is not None
    approval = await session.get(Approval, parked.approval_id)
    assert approval is not None
    approval.status = ApprovalStatus.REJECTED.value
    payload = dict(approval.action_payload_sanitized)
    payload[field] = tampered
    approval.action_payload_sanitized = payload
    await session.flush()

    outcome = await gateway.resolve_rejected(parked.approval_id)

    assert outcome.status == "failed"
    assert outcome.decision_code == expected_code
    assert "tool.call.failed" in await _audit_actions(session)
    assert "demo.destructive.marker" not in await _audit_actions(session)


async def test_output_is_redacted_and_size_capped(
    session: AsyncSession, context: ToolExecutionContext
) -> None:
    from jhin_secrets.redaction import get_redactor
    from jhin_tools.builtin import build_builtin_catalog

    get_redactor().register("sk-unit-test-secret")
    try:
        gateway = ToolGateway(context, build_builtin_catalog())
        await _grant(session, context, "system.echo")
        outcome = await gateway.request(
            "system.echo", json.dumps({"text": "key=sk-unit-test-secret " + "x" * 9_500})
        )
        assert outcome.status == "executed"
        assert outcome.sanitized_output is not None
        text = outcome.sanitized_output["text"]
        assert "sk-unit-test-secret" not in text
        assert "[REDACTED]" in text
        assert len(text) <= 8_192
    finally:
        get_redactor().clear()
