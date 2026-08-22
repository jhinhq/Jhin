"""TriggerMatcher: canonical events → trigger invocations (plan 10, 9.4).

For every ``connector.*`` event on the EVENTS stream, load the workspace's
enabled triggers (cached briefly), evaluate their filters with the pure DSL,
and for each match run the idempotent start sequence:

1. derive the deterministic idempotency key (trigger + connection + external
   entity + transition fingerprint + dedupe-window bucket);
2. insert a ``started`` invocation row — the partial unique index makes the
   database the first dedupe authority; losers record a ``duplicate`` row;
3. start TriggeredTaskWorkflow under a workflow id derived from the same
   key — Temporal's duplicate-start policy is the second defense, closing
   the race against a crash between commit and start (plan 48.6).

A Temporal outage marks the invocation ``failed`` (persisting only the
closed ``upstream_unavailable`` code) and raises, so the consumer naks for
redelivery: the failed row remains as history and the retry inserts a fresh
``started`` row.

A crash between the ``started`` commit and the Temporal start leaves an
authoritative started row with no linked task. A later delivery still
records its ``duplicate`` history row, then reconciles by reissuing the same
deterministic workflow id; Temporal's duplicate-start policy makes concurrent
reconcilers safe. A started row that already links a task is suppressed.

Telemetry (plan 10, Task 7) is diagnostic-only and records each
started/duplicate/failed transition once, only after its row commit. Labels
carry the central connector type and closed outcome/failure classes; never an
identifier, URL, event payload, or exception text.

The matcher knows nothing about Linear: normalized events follow the
connector-agnostic conventions ``data.external_id/title/description/url``
and ``data.changed_from`` (plan 52).
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import UUID

from opentelemetry.trace import SpanKind, Tracer
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.client import Client as TemporalClient
from temporalio.exceptions import WorkflowAlreadyStartedError

from jhin_db.models import Agent, AuditEvent, Team, Trigger, TriggerInvocation
from jhin_domain import AgentStatus, TriggerInvocationStatus, TriggerType
from jhin_events.envelope import EventEnvelope
from jhin_observability import (
    JhinMetrics,
    MetricName,
    SafeErrorCode,
    SpanName,
    get_logger,
    normalize_connector_type,
    record_span_error,
    safe_error,
    safe_span,
    set_span_attributes,
)
from jhin_triggers import (
    build_idempotency_key,
    evaluate_filter,
    transition_fingerprint,
    workflow_id_for_key,
)
from jhin_workflows import AGENT_TASK_QUEUE
from jhin_workflows.engineering_ticket import DEFAULT_MAX_RETEST_CYCLES, EngineeringTicketInput
from jhin_workflows.triggered_task import TriggeredTaskInput

logger = get_logger(__name__)

_CACHE_TTL_SECONDS = 5.0
_MAX_TITLE = 500
_MAX_DESCRIPTION = 10_000

_INVOCATIONS_METRIC: MetricName = "trigger_invocations_total"
_FAILURES_METRIC: MetricName = "trigger_failures_total"
_DISPATCH_SPAN_NAME: SpanName = "trigger.dispatch"
_FATAL_AUTHORITY_TYPES = (KeyboardInterrupt, SystemExit)

TriggerOutcome = Literal["started", "duplicate", "failed"]
TriggerFailureClass = Literal["target", "dispatch"]
PreDispatchBarrier = Callable[[UUID], Awaitable[None]]


def _run_trigger_diagnostic(operation: Callable[[], None], *, secondary: bool = False) -> None:
    """Telemetry is diagnostic-only: ordinary failures never escape.

    Fatal authority always escapes. Cancellation escapes only while it is the
    primary authority; while product work is already unwinding (``secondary``)
    a telemetry cancellation may not replace the active product exception.
    """
    try:
        operation()
    except BaseException as error:
        if isinstance(error, _FATAL_AUTHORITY_TYPES):
            raise
        if not secondary and not isinstance(error, Exception):
            raise


@dataclass(frozen=True)
class TriggerSpec:
    """Detached snapshot of one enabled trigger (safe to cache)."""

    id: UUID
    name: str
    connection_id: UUID | None
    event_type: str | None
    filter_json: dict[str, Any]
    target_agent_id: UUID | None
    target_team_id: UUID | None
    dedupe_window_seconds: int
    comment_back: bool
    # trigger.workflow_definition: selects a workflow template (plan 8.4).
    # Empty/absent = plain TriggeredTaskWorkflow (the default, never forced).
    workflow_definition: dict[str, Any] = field(default_factory=dict)


@dataclass
class _CacheEntry:
    loaded_at: float
    specs: list[TriggerSpec] = field(default_factory=list)


@dataclass(frozen=True)
class _DispatchAuthority:
    """The exact started row a dispatch is allowed to fail."""

    invocation_id: UUID
    workflow_id: str
    params: TriggeredTaskInput


class TriggerMatcher:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        temporal: TemporalClient,
        *,
        metrics: JhinMetrics,
        tracer: Tracer,
        cache_ttl_seconds: float = _CACHE_TTL_SECONDS,
        pre_dispatch_barrier: PreDispatchBarrier | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._temporal = temporal
        self._metrics = metrics
        self._tracer = tracer
        self._cache_ttl = cache_ttl_seconds
        self._pre_dispatch_barrier = pre_dispatch_barrier
        self._cache: dict[str, _CacheEntry] = {}

    def _record_invocation(
        self, connector_type: str, outcome: TriggerOutcome, *, secondary: bool = False
    ) -> None:
        _run_trigger_diagnostic(
            lambda: self._metrics.counter(_INVOCATIONS_METRIC).add(
                1,
                connector_type=normalize_connector_type(connector_type),
                outcome=outcome,
            ),
            secondary=secondary,
        )

    def _record_failure(
        self,
        connector_type: str,
        failure_class: TriggerFailureClass,
        *,
        secondary: bool = False,
    ) -> None:
        _run_trigger_diagnostic(
            lambda: self._metrics.counter(_FAILURES_METRIC).add(
                1,
                connector_type=normalize_connector_type(connector_type),
                failure_class=failure_class,
            ),
            secondary=secondary,
        )

    async def _commit_invocation_transition(
        self,
        session: AsyncSession,
        *,
        connector_type: str,
        outcome: TriggerOutcome,
        failure_class: TriggerFailureClass | None = None,
        secondary: bool = False,
    ) -> None:
        """Commit one durable transition, then count it exactly once.

        Durability precedes diagnostics: a crash after the commit may lose the
        metric, but a redelivery can never double count a committed row.
        """
        if failure_class is not None and outcome != "failed":
            raise ValueError("failure_class requires failed outcome")
        await session.commit()
        self._record_invocation(connector_type, outcome, secondary=secondary)
        if failure_class is not None:
            self._record_failure(connector_type, failure_class, secondary=secondary)

    async def handle_event(self, envelope: EventEnvelope) -> None:
        """Evaluate one canonical event against the workspace's triggers."""
        if not envelope.event_type.startswith("connector."):
            return
        specs = await self._triggers_for(envelope.workspace_id)
        event_view: dict[str, Any] = {"event_type": envelope.event_type, "data": envelope.data}
        for spec in specs:
            if spec.event_type and spec.event_type != envelope.event_type:
                continue
            if spec.connection_id is not None and spec.connection_id != (
                envelope.source.connection_id
            ):
                continue
            result = evaluate_filter(spec.filter_json, event_view)
            if not result.matched:
                continue
            await self._invoke(spec, envelope, result)

    async def _triggers_for(self, workspace_id: str) -> list[TriggerSpec]:
        now = time.monotonic()
        cached = self._cache.get(workspace_id)
        if cached is not None and now - cached.loaded_at < self._cache_ttl:
            return cached.specs
        async with self._session_factory() as session:
            rows = await session.scalars(
                select(Trigger).where(
                    Trigger.workspace_id == UUID(workspace_id),
                    Trigger.enabled.is_(True),
                    Trigger.trigger_type == TriggerType.CONNECTOR_EVENT.value,
                )
            )
            specs = [
                TriggerSpec(
                    id=row.id,
                    name=row.name,
                    connection_id=row.connection_id,
                    event_type=row.event_type,
                    filter_json=dict(row.filter_json),
                    target_agent_id=row.target_agent_id,
                    target_team_id=row.target_team_id,
                    dedupe_window_seconds=row.dedupe_window_seconds,
                    comment_back=bool(row.action_config_json.get("comment_back", False)),
                    workflow_definition=dict(row.workflow_definition or {}),
                )
                for row in rows
            ]
        self._cache[workspace_id] = _CacheEntry(loaded_at=now, specs=specs)
        return specs

    async def _invoke(self, spec: TriggerSpec, envelope: EventEnvelope, result: Any) -> None:
        workspace_id = UUID(envelope.workspace_id)
        external_id = str(envelope.data.get("external_id") or "") or str(envelope.event_id)
        key = build_idempotency_key(
            trigger_id=spec.id,
            connection_id=spec.connection_id,
            external_id=external_id,
            fingerprint=transition_fingerprint(result),
            dedupe_window_seconds=spec.dedupe_window_seconds,
            occurred_at=envelope.occurred_at,
        )
        workflow_id = workflow_id_for_key(key)
        connector_type = envelope.source.type

        async with self._session_factory() as session:
            invocation = await self._record_started(session, spec, envelope, key, workflow_id)
            if invocation is None:
                # Duplicate: recorded, audited, and counted inside. Reconcile an
                # authoritative started row that never reached Temporal.
                unfinished = await self._unfinished_started_authority(session, spec, key)
                if unfinished is None:
                    return
                agent_id = await self._resolve_agent(session, workspace_id, spec)
                if agent_id is None:
                    return
                authority = _DispatchAuthority(
                    invocation_id=unfinished.id,
                    workflow_id=unfinished.workflow_id or workflow_id,
                    params=self._workflow_input(
                        spec, envelope, unfinished.id, external_id, agent_id
                    ),
                )
            else:
                agent_id = await self._resolve_agent(session, workspace_id, spec)
                if agent_id is None:
                    invocation.status = TriggerInvocationStatus.FAILED.value
                    invocation.error = SafeErrorCode.INVALID_REQUEST.value
                    self._audit(session, spec, envelope, key, "failed", workflow_id)
                    await self._commit_invocation_transition(
                        session,
                        connector_type=connector_type,
                        outcome="failed",
                        failure_class="target",
                    )
                    logger.warning(
                        "trigger.no_agent",
                        connector_type=normalize_connector_type(connector_type),
                    )
                    return

                params = self._workflow_input(spec, envelope, invocation.id, external_id, agent_id)
                self._audit(session, spec, envelope, key, "started", workflow_id)
                await self._commit_invocation_transition(
                    session,
                    connector_type=connector_type,
                    outcome="started",
                )
                authority = _DispatchAuthority(
                    invocation_id=invocation.id,
                    workflow_id=workflow_id,
                    params=params,
                )

        if self._pre_dispatch_barrier is not None:
            await self._pre_dispatch_barrier(authority.invocation_id)
        await self._dispatch(spec, authority, connector_type)

    async def _unfinished_started_authority(
        self,
        session: AsyncSession,
        spec: TriggerSpec,
        key: str,
    ) -> TriggerInvocation | None:
        """The one started row for this key that has not yet linked a task."""
        started = await session.scalar(
            select(TriggerInvocation).where(
                TriggerInvocation.trigger_id == spec.id,
                TriggerInvocation.idempotency_key == key,
                TriggerInvocation.status == TriggerInvocationStatus.STARTED.value,
            )
        )
        if started is None or started.task_id is not None:
            return None
        return started

    async def _dispatch(
        self,
        spec: TriggerSpec,
        authority: _DispatchAuthority,
        connector_type: str,
    ) -> None:
        normalized_connector = normalize_connector_type(connector_type)
        workflow_name, workflow_params = self._select_workflow(spec, authority.params)
        with safe_span(
            _DISPATCH_SPAN_NAME,
            tracer=self._tracer,
            kind=SpanKind.CLIENT,
            attributes={"jhin.connector_type": normalized_connector},
        ) as span:
            try:
                await self._temporal.start_workflow(
                    workflow_name,
                    workflow_params,
                    id=authority.workflow_id,
                    task_queue=AGENT_TASK_QUEUE,
                )
            except WorkflowAlreadyStartedError:
                # The work already exists exactly once (e.g. a reconciled
                # delivery after a crash between commit and start). Not an
                # error.
                logger.info(
                    "trigger.workflow_already_started",
                    connector_type=normalized_connector,
                )
            except Exception as exc:
                failure = safe_error(exc, code=SafeErrorCode.UPSTREAM_UNAVAILABLE)
                _run_trigger_diagnostic(
                    lambda: set_span_attributes(span, {"jhin.outcome": "failed"}),
                    secondary=True,
                )
                _run_trigger_diagnostic(lambda: record_span_error(span, failure), secondary=True)
                await self._fail_started_authority(authority, connector_type, failure.code)
                raise  # nak -> redelivery retries the whole match
            _run_trigger_diagnostic(
                lambda: set_span_attributes(span, {"jhin.outcome": "started"}),
            )

        logger.info(
            "trigger.invoked",
            connector_type=normalized_connector,
            outcome="started",
        )

    async def _fail_started_authority(
        self,
        authority: _DispatchAuthority,
        connector_type: str,
        code: SafeErrorCode,
    ) -> None:
        """Fail only the exact started row; contain every secondary failure."""
        try:
            async with self._session_factory() as session:
                row = await session.get(TriggerInvocation, authority.invocation_id)
                if row is None or row.status != TriggerInvocationStatus.STARTED.value:
                    return
                row.status = TriggerInvocationStatus.FAILED.value
                row.error = code.value
                await self._commit_invocation_transition(
                    session,
                    connector_type=connector_type,
                    outcome="failed",
                    failure_class="dispatch",
                    secondary=True,
                )
        except Exception:
            # Secondary failure: the original Temporal error remains the
            # authority and the consumer's nak already reports the delivery.
            return

    async def _record_started(
        self,
        session: AsyncSession,
        spec: TriggerSpec,
        envelope: EventEnvelope,
        key: str,
        workflow_id: str,
    ) -> TriggerInvocation | None:
        """Insert the ``started`` row; on unique-index loss record a
        ``duplicate`` row and return None."""
        workspace_id = UUID(envelope.workspace_id)
        existing = await session.scalar(
            select(TriggerInvocation).where(
                TriggerInvocation.trigger_id == spec.id,
                TriggerInvocation.idempotency_key == key,
                TriggerInvocation.status == TriggerInvocationStatus.STARTED.value,
            )
        )
        if existing is None:
            invocation = TriggerInvocation(
                workspace_id=workspace_id,
                trigger_id=spec.id,
                idempotency_key=key,
                event_id=envelope.event_id,
                workflow_id=workflow_id,
                status=TriggerInvocationStatus.STARTED.value,
            )
            session.add(invocation)
            try:
                await session.flush()
            except IntegrityError:
                # Lost the insert race to a concurrent replica.
                await session.rollback()
            else:
                return invocation

        duplicate = TriggerInvocation(
            workspace_id=workspace_id,
            trigger_id=spec.id,
            idempotency_key=key,
            event_id=envelope.event_id,
            workflow_id=workflow_id,
            status=TriggerInvocationStatus.DUPLICATE.value,
        )
        session.add(duplicate)
        self._audit(session, spec, envelope, key, "duplicate", workflow_id)
        await self._commit_invocation_transition(
            session,
            connector_type=envelope.source.type,
            outcome="duplicate",
        )
        logger.info(
            "trigger.duplicate_suppressed",
            connector_type=normalize_connector_type(envelope.source.type),
        )
        return None

    async def _resolve_agent(
        self, session: AsyncSession, workspace_id: UUID, spec: TriggerSpec
    ) -> UUID | None:
        """Trigger target → concrete active agent (plan 8.1)."""
        if spec.target_agent_id is not None:
            agent = await session.scalar(
                select(Agent).where(
                    Agent.id == spec.target_agent_id,
                    Agent.workspace_id == workspace_id,
                    Agent.status == AgentStatus.ACTIVE.value,
                )
            )
            return agent.id if agent is not None else None
        if spec.target_team_id is not None:
            team = await session.get(Team, spec.target_team_id)
            if team is not None and team.manager_agent_id is not None:
                manager = await session.scalar(
                    select(Agent).where(
                        Agent.id == team.manager_agent_id,
                        Agent.workspace_id == workspace_id,
                        Agent.status == AgentStatus.ACTIVE.value,
                    )
                )
                if manager is not None:
                    return manager.id
            member = await session.scalar(
                select(Agent)
                .where(
                    Agent.team_id == spec.target_team_id,
                    Agent.workspace_id == workspace_id,
                    Agent.status == AgentStatus.ACTIVE.value,
                )
                .order_by(Agent.created_at)
            )
            return member.id if member is not None else None
        return None

    @staticmethod
    def _select_workflow(
        spec: TriggerSpec, params: TriggeredTaskInput
    ) -> tuple[str, TriggeredTaskInput | EngineeringTicketInput]:
        """Workflow-template selection (plan 8.4): the trigger's
        workflow_definition may pick a built-in template; plain
        TriggeredTaskWorkflow stays the default. Config values are parsed
        defensively — a malformed definition falls back to the default."""
        definition = spec.workflow_definition
        if definition.get("template") != "engineering_ticket":
            return "TriggeredTaskWorkflow", params
        try:
            max_cycles = int(definition.get("max_retest_cycles", DEFAULT_MAX_RETEST_CYCLES))
        except (TypeError, ValueError):
            max_cycles = DEFAULT_MAX_RETEST_CYCLES
        return "EngineeringTicketWorkflow", EngineeringTicketInput(
            base=params,
            implementer_agent_id=str(definition.get("implementer_agent_id", "") or ""),
            qa_agent_id=str(definition.get("qa_agent_id", "") or ""),
            manager_review=bool(definition.get("manager_review", False)),
            max_retest_cycles=max_cycles,
        )

    def _workflow_input(
        self,
        spec: TriggerSpec,
        envelope: EventEnvelope,
        invocation_id: UUID,
        external_id: str,
        agent_id: UUID,
    ) -> TriggeredTaskInput:
        data = envelope.data
        title = str(data.get("title") or "")[:_MAX_TITLE]
        description = str(data.get("description") or "")[:_MAX_DESCRIPTION]
        url = str(data.get("url") or "")
        if title and external_id:
            title = f"[{external_id}] {title}"[:_MAX_TITLE]
        if url:
            description = f"{description}\n\nSource: {url}".strip()
        return TriggeredTaskInput(
            workspace_id=envelope.workspace_id,
            trigger_id=str(spec.id),
            trigger_name=spec.name,
            invocation_id=str(invocation_id),
            connection_id=str(spec.connection_id or envelope.source.connection_id or ""),
            event_id=str(envelope.event_id),
            event_type=envelope.event_type,
            external_source=envelope.source.type,
            external_id=external_id,
            title=title,
            description=description,
            external_url=url,
            agent_id=str(agent_id),
            comment_back=spec.comment_back,
        )

    def _audit(
        self,
        session: AsyncSession,
        spec: TriggerSpec,
        envelope: EventEnvelope,
        key: str,
        status: str,
        workflow_id: str,
    ) -> None:
        session.add(
            AuditEvent(
                workspace_id=UUID(envelope.workspace_id),
                actor_type="system",
                actor_id=None,
                action="trigger.invoked",
                target_type="trigger",
                target_id=spec.id,
                metadata_json={
                    "status": status,
                    "event_id": str(envelope.event_id),
                    "event_type": envelope.event_type,
                    "idempotency_key": key,
                    "workflow_id": workflow_id,
                },
            )
        )
