"""What this process promises when it is told to stop.

Two promises, and they were both partly kept.

*Every* activity on the tool queue refuses new work once the drain begins.
Three of the six did. The one that mattered most did not: the trigger sync
claims a ``tool_call``, commits the claim, and then runs an executor that
posts a comment on somebody else's system — which is, in this module's own
words, the worst outcome available, with an external effect on the end of it.

And the budgets the shutdown spends fit inside the stop grace *with room*.
They used to sum to exactly it, with the Temporal worker's exit and the
telemetry flush still to come, which means any job that outlived the drain was
racing SIGKILL for the seconds it needed.
"""

from __future__ import annotations

import inspect
from typing import Any
from uuid import uuid4

import pytest
from temporalio.exceptions import ApplicationError

from jhin_connectors.cli.tools import _CANCEL_CLEANUP_SECONDS
from jhin_tool_worker.activities import ToolActivities
from jhin_tool_worker.cleanup_activities import CleanupActivities
from jhin_tool_worker.drain import (
    DRAIN_BUDGET_SECONDS,
    DRAINING_ERROR_TYPE,
    RUNTIME_SHUTDOWN_BUDGET_SECONDS,
    SHUTDOWN_MARGIN_SECONDS,
    STOP_GRACE_SECONDS,
    WorkerDrain,
)
from jhin_tool_worker.settings import ToolWorkerSettings
from jhin_tool_worker.trigger_activities import TriggerToolActivities
from jhin_tools import ToolCatalog
from jhin_workflows.agent_task.shared import (
    ACTIVITY_CLEANUP_RUN_WORKSPACE,
    ACTIVITY_EXECUTE_BOUND_TOOL,
    ACTIVITY_RESOLVE_ADVERTISED_TOOLS,
    ACTIVITY_RESOLVE_BOUND_TOOL_APPROVAL,
    ACTIVITY_RESOLVE_BOUND_TOOL_REVIEW,
    CleanupRunWorkspaceInput,
    ExecuteBoundToolInput,
    ResolveAdvertisedToolsInput,
    ResolveBoundToolApprovalInput,
    ResolveBoundToolReviewInput,
)
from jhin_workflows.tool_compat import SyncExternalToolInput
from jhin_workflows.triggered_task.shared import ACTIVITY_SYNC_EXTERNAL_TOOL

pytestmark = pytest.mark.anyio

_WORKSPACE = str(uuid4())
_TASK = str(uuid4())
_RUN = str(uuid4())
_AGENT = str(uuid4())


class _Resources:
    """Enough of ``ToolWorkerResources`` to construct the activity classes.

    Deliberately hostile past that point: a session factory that raises is how
    a test proves the refusal happened *before* anything was read, rather than
    proving it happened at all.
    """

    class _Runtime:
        tracer = None
        metrics = None

    runtime = _Runtime()
    crypto = None
    test_barrier = None

    def session_factory(self) -> Any:
        raise AssertionError("a drained activity must not reach the database")

    @property
    def publisher(self) -> Any:
        raise AssertionError("a drained activity must not reach the event bus")


def _activity_name(function: Any) -> str | None:
    definition = getattr(function, "__temporal_activity_definition", None)
    return None if definition is None else str(definition.name)


def _declared_activities(instance: object) -> dict[str, Any]:
    found: dict[str, Any] = {}
    for name, _member in inspect.getmembers(type(instance)):
        bound = getattr(instance, name)
        activity_name = _activity_name(bound)
        if activity_name is not None:
            found[activity_name] = bound
    return found


def _the_worker() -> tuple[WorkerDrain, dict[str, Any]]:
    """The three activity classes ``main`` registers, sharing one drain."""
    drain = WorkerDrain()
    resources: Any = _Resources()
    catalog = ToolCatalog()
    found: dict[str, Any] = {}
    for instance in (
        ToolActivities(resources, catalog, drain=drain),
        TriggerToolActivities(resources, catalog, drain=drain),
        CleanupActivities(resources, drain=drain),
    ):
        found.update(_declared_activities(instance))
    return drain, found


_PARAMS: dict[str, Any] = {
    ACTIVITY_RESOLVE_ADVERTISED_TOOLS: ResolveAdvertisedToolsInput(
        workspace_id=_WORKSPACE, agent_id=_AGENT, task_id=_TASK
    ),
    ACTIVITY_EXECUTE_BOUND_TOOL: ExecuteBoundToolInput(
        workspace_id=_WORKSPACE, run_id=_RUN, step_index=0, ordinal=0
    ),
    ACTIVITY_RESOLVE_BOUND_TOOL_APPROVAL: ResolveBoundToolApprovalInput(
        workspace_id=_WORKSPACE,
        task_id=_TASK,
        run_id=_RUN,
        agent_id=_AGENT,
        approval_id=str(uuid4()),
    ),
    ACTIVITY_RESOLVE_BOUND_TOOL_REVIEW: ResolveBoundToolReviewInput(
        workspace_id=_WORKSPACE,
        task_id=_TASK,
        run_id=_RUN,
        agent_id=_AGENT,
        review_id=str(uuid4()),
    ),
    ACTIVITY_SYNC_EXTERNAL_TOOL: SyncExternalToolInput(
        workspace_id=_WORKSPACE, task_id=_TASK, run_id=_RUN
    ),
    ACTIVITY_CLEANUP_RUN_WORKSPACE: CleanupRunWorkspaceInput(workspace_id=_WORKSPACE, run_id=_RUN),
}


def test_the_tool_queue_has_exactly_the_activities_this_file_drains() -> None:
    """An activity added to any of these three classes without a line in
    ``_PARAMS`` fails here rather than shipping undrained."""
    _drain, activities = _the_worker()
    assert set(activities) == set(_PARAMS)


@pytest.mark.parametrize("name", sorted(_PARAMS))
async def test_every_activity_on_the_queue_refuses_while_draining(name: str) -> None:
    drain, activities = _the_worker()
    drain.begin()

    with pytest.raises(ApplicationError) as raised:
        await activities[name](_PARAMS[name])

    assert raised.value.type == DRAINING_ERROR_TYPE
    assert raised.value.non_retryable is False
    # It says nothing started, because nothing did: the refusal is ahead of
    # every claim, every commit and every executor.
    assert "nothing ran" in str(raised.value)
    assert drain.in_flight == 0


async def test_the_refusal_asks_to_be_tried_again_after_the_restart() -> None:
    """Without this the refusal is instant and free, and a turn spends its
    whole retry budget inside one drain window — dying of a redeploy it was
    supposed to survive."""
    drain, activities = _the_worker()
    drain.begin()

    with pytest.raises(ApplicationError) as raised:
        await activities[ACTIVITY_EXECUTE_BOUND_TOOL](_PARAMS[ACTIVITY_EXECUTE_BOUND_TOOL])

    delay = raised.value.next_retry_delay
    assert delay is not None
    # Longer than the drain, so at most one attempt can land inside it; after
    # that the process is gone and nothing polls this queue at all.
    assert delay.total_seconds() > DRAIN_BUDGET_SECONDS


async def test_one_drain_covers_the_whole_process() -> None:
    """Three classes, one switch. A per-class drain would be three switches
    and a shutdown that flipped two of them."""
    drain, activities = _the_worker()
    owners = {activity.__self__._drain for activity in activities.values()}
    assert owners == {drain}


def test_the_shutdown_budgets_fit_inside_the_stop_grace_with_room() -> None:
    """The arithmetic, asserted rather than described.

    Three consecutive budgets share Docker's default stop grace: the drain,
    the per-job cancellation cleanup, and the telemetry flush. They used to
    sum to exactly the grace, which left nothing for ``worker.__aexit__``, the
    connection pool, or being wrong.
    """
    spent = DRAIN_BUDGET_SECONDS + _CANCEL_CLEANUP_SECONDS + RUNTIME_SHUTDOWN_BUDGET_SECONDS
    assert spent + SHUTDOWN_MARGIN_SECONDS <= STOP_GRACE_SECONDS
    assert SHUTDOWN_MARGIN_SECONDS > 0
    # And the setting a deployment actually reads is the budget above, so
    # nobody has to notice that two numbers exist.
    assert ToolWorkerSettings().tool_worker_drain_timeout_seconds == DRAIN_BUDGET_SECONDS
