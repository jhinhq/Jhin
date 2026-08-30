"""OAuthRefreshWorkflow stays deterministic, bounded, and knows when to stop.

Three properties a durable timer has to have and cannot be reasoned into:

* **deterministic.** No clock but ``workflow.now()``, no I/O, no randomness —
  a replay that diverges is a workflow that gets stuck.
* **bounded.** ``continue_as_new`` every 288 windows, so a workspace running
  for a year does not accumulate a year of history in one run.
* **it ends.** Three consecutive empty windows and the workflow exits, so an
  install that connects nothing runs nothing. Three, not one, because a
  workspace whose only OAuth connection is mid-re-authorization should keep
  its refresher rather than need one started again.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

from jhin_workflows.oauth_refresh import (
    ACTIVITY_REFRESH_DUE_CONNECTIONS,
    DEFAULT_OAUTH_REFRESH_INTERVAL_SECONDS,
    MIN_OAUTH_REFRESH_INTERVAL_SECONDS,
    OAUTH_REFRESH_IDLE_WINDOWS,
    OAUTH_REFRESH_WINDOWS_PER_RUN,
    OAUTH_REFRESH_WORKFLOW,
    SIGNAL_OAUTH_REFRESH_NOW,
    SIGNAL_OAUTH_REFRESH_STOP,
    OAuthRefreshInput,
    OAuthRefreshResult,
    OAuthRefreshSweep,
    OAuthRefreshWorkflow,
    oauth_refresh_workflow_id,
)

WORKFLOW_SOURCE = Path(inspect.getsourcefile(OAuthRefreshWorkflow) or "").read_text()

# Names that would make a workflow non-deterministic if called from one. A
# replay reads them again and gets a different answer, and the workflow wedges.
FORBIDDEN_CALLS = {
    "time",
    "sleep",
    "utcnow",
    "now",  # datetime.now; workflow.now() is the attribute access below
    "random",
    "uuid4",
    "open",
    "getenv",
}


def test_one_refresher_per_workspace_not_one_per_connection() -> None:
    """Thousands of durable timers to do what one bounded query does is not a design."""
    first = oauth_refresh_workflow_id("workspace-a")
    second = oauth_refresh_workflow_id("workspace-b")
    assert first != second
    assert first == oauth_refresh_workflow_id("workspace-a")
    assert "workspace-a" in first


def test_the_workflow_is_registered_under_the_name_the_starter_uses() -> None:
    """A mismatch here is a workflow that starts and is never picked up."""
    definition = getattr(OAuthRefreshWorkflow, "__temporal_workflow_definition", None)
    assert definition is not None
    assert definition.name == OAUTH_REFRESH_WORKFLOW


def test_both_signals_are_declared_under_their_shared_names() -> None:
    definition = getattr(OAuthRefreshWorkflow, "__temporal_workflow_definition", None)
    assert definition is not None
    assert set(definition.signals) >= {SIGNAL_OAUTH_REFRESH_STOP, SIGNAL_OAUTH_REFRESH_NOW}


def test_the_workflow_module_does_no_io_and_reads_no_wall_clock() -> None:
    """Determinism, checked mechanically rather than by reading.

    Every instant the workflow uses comes from ``workflow.now()``; anything
    else diverges on replay. Parsing the module is a blunt instrument, but it
    is the one that keeps working when somebody adds a helper in six months.
    """
    tree = ast.parse(WORKFLOW_SOURCE)
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        if isinstance(function, ast.Attribute):
            # ``workflow.now()`` and ``workflow.wait_condition()`` are the
            # deterministic replacements and are explicitly allowed.
            if isinstance(function.value, ast.Name) and function.value.id == "workflow":
                continue
            if function.attr in FORBIDDEN_CALLS:
                offenders.append(function.attr)
        elif isinstance(function, ast.Name) and function.id in FORBIDDEN_CALLS:
            offenders.append(function.id)
    assert offenders == [], f"non-deterministic calls in the workflow: {sorted(set(offenders))}"


def test_the_workflow_imports_nothing_that_touches_a_database_or_a_socket() -> None:
    """The activity does the work; the workflow only decides when."""
    tree = ast.parse(WORKFLOW_SOURCE)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported & {"sqlalchemy", "httpx", "jhin_db", "jhin_secrets", "jhin_oauth"} == set()


def test_a_run_is_bounded_at_288_windows() -> None:
    """A day per run at the default cadence keeps each history small."""
    assert OAUTH_REFRESH_WINDOWS_PER_RUN == 288
    assert "continue_as_new" in WORKFLOW_SOURCE
    assert f"% {OAUTH_REFRESH_WINDOWS_PER_RUN}" in WORKFLOW_SOURCE.replace(
        "OAUTH_REFRESH_WINDOWS_PER_RUN", str(OAUTH_REFRESH_WINDOWS_PER_RUN)
    )


def test_it_takes_three_empty_windows_to_stop() -> None:
    """One would drop the refresher mid-re-authorization."""
    assert OAUTH_REFRESH_IDLE_WINDOWS == 3


def test_the_interval_has_a_floor_the_input_cannot_undercut() -> None:
    """A one-second interval would sweep a workspace to death."""
    assert MIN_OAUTH_REFRESH_INTERVAL_SECONDS == 60
    assert DEFAULT_OAUTH_REFRESH_INTERVAL_SECONDS == 300
    assert "MIN_OAUTH_REFRESH_INTERVAL_SECONDS" in WORKFLOW_SOURCE


def test_the_input_carries_enough_to_continue_as_new_without_losing_its_place() -> None:
    params = OAuthRefreshInput(
        workspace_id="w", interval_seconds=300, windows_done=7, idle_windows=2
    )
    assert params.windows_done == 7
    assert params.idle_windows == 2


def test_an_empty_sweep_reports_zero_remaining_connections() -> None:
    """The single field the workflow's stop decision reads."""
    sweep = OAuthRefreshSweep()
    assert sweep.remaining_oauth_connections == 0
    assert sweep.refreshed == 0


def test_the_result_names_why_it_stopped() -> None:
    result = OAuthRefreshResult(workspace_id="w", windows_done=3, refreshed=1, reason="idle")
    assert result.reason in {"idle", "stopped"}


def test_the_activity_name_is_shared_with_the_worker() -> None:
    """The workflow references activities by name; a typo is a stuck workflow."""
    assert ACTIVITY_REFRESH_DUE_CONNECTIONS == "refresh_due_oauth_connections"
    assert "ACTIVITY_REFRESH_DUE_CONNECTIONS" in WORKFLOW_SOURCE


def test_stop_and_refresh_now_are_plain_state_flips() -> None:
    """Signal handlers must not await: a blocking handler blocks the workflow."""
    workflow_instance = OAuthRefreshWorkflow()
    assert not inspect.iscoroutinefunction(workflow_instance.stop)
    assert not inspect.iscoroutinefunction(workflow_instance.refresh_now)

    workflow_instance.refresh_now()
    workflow_instance.stop()
    assert workflow_instance._stop_requested() is True
