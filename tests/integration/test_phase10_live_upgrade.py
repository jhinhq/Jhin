"""True frozen Phase 9 to current Phase 10 in-flight worker swap."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from temporalio.client import Client as TemporalClient

from jhin_workflows import AGENT_TASK_QUEUE
from jhin_workflows.agent_task import AgentTaskInput
from jhin_workflows.triggered_task import TriggeredTaskInput

from . import conftest as integration_config
from . import test_phase7_exit as phase7
from .phase10_upgrade_harness import UpgradeHarness, activity_schedule_pairs
from .test_phase10_tool_worker_boundary import (
    _calls,
    _comment_count,
    _comment_marker,
    _csrf,
    _github_agent,
    _live_owner,
    _post,
    _task,
    _timeline,
    _wait_run_started,
    _wait_task,
)

pytestmark = pytest.mark.integration

_AGENT_QUEUE = "jhin-agent-queue"
_TOOL_QUEUE = "jhin-tool-queue"


@dataclass(frozen=True)
class UpgradeRun:
    scenario: str
    task_id: str
    workflow_id: str
    temporal_run_id: str
    domain_run_id: str
    handle: Any


async def _wait_frozen_phase9_arrivals(
    upgrade: UpgradeHarness,
    arrivals: Sequence[tuple[str, str]],
    *,
    probe_timeout: float,
) -> list[str]:
    results = await asyncio.gather(
        *(
            asyncio.to_thread(
                upgrade.wait_frozen_phase9_arrival,
                scenario,
                identity=identity,
                timeout=probe_timeout,
            )
            for scenario, identity in arrivals
        ),
        return_exceptions=True,
    )
    failures = [result for result in results if isinstance(result, BaseException)]
    if len(failures) == 1:
        raise failures[0]
    if failures:
        raise BaseExceptionGroup("frozen Phase 9 barrier probes failed", failures)
    if not all(isinstance(result, str) for result in results):
        raise TypeError("frozen Phase 9 barrier probe returned a non-string identity")
    return [result for result in results if isinstance(result, str)]


async def _start_direct_task(
    upgrade: UpgradeHarness,
    client: httpx.AsyncClient,
    workspace_id: str,
    *,
    scenario: str,
    agent_id: str,
    description: str,
) -> UpgradeRun:
    task = await _post(
        client,
        f"/api/v1/workspaces/{workspace_id}/tasks",
        {
            "title": f"Phase 10 upgrade {scenario} {uuid.uuid4().hex[:8]}",
            "description": description,
            "priority": "normal",
        },
    )
    workflow_id = f"phase10-upgrade-{scenario}-{uuid.uuid4().hex}"
    upgrade.bind_upgrade_task(
        task_id=str(task["id"]),
        agent_id=agent_id,
        workflow_id=workflow_id,
    )
    temporal = await TemporalClient.connect(
        integration_config.TEMPORAL_ADDRESS,
        namespace=upgrade.scenarios[scenario].namespace,
    )
    handle = await temporal.start_workflow(
        "AgentTaskWorkflow",
        AgentTaskInput(
            workspace_id=workspace_id,
            task_id=str(task["id"]),
            agent_id=agent_id,
            instruction=description,
        ),
        id=workflow_id,
        task_queue=AGENT_TASK_QUEUE,
    )
    await asyncio.to_thread(upgrade.run_phase9_snapshot_once, scenario)
    _detail, domain_run_id = await _wait_run_started(
        client, workspace_id, str(task["id"]), deadline_seconds=120.0
    )
    temporal_run_id = upgrade.authority.temporal_run_id(domain_run_id)
    assert temporal_run_id == handle.first_execution_run_id
    return UpgradeRun(
        scenario=scenario,
        task_id=str(task["id"]),
        workflow_id=workflow_id,
        temporal_run_id=temporal_run_id,
        domain_run_id=domain_run_id,
        handle=handle,
    )


async def _cleanup_agent(
    client: httpx.AsyncClient,
    workspace_id: str,
) -> tuple[dict[str, Any], str]:
    tag = uuid.uuid4().hex[:8]
    provider = await _post(
        client,
        f"/api/v1/workspaces/{workspace_id}/model-providers",
        {
            "type": "openai_compatible",
            "display_name": f"P10 upgrade cleanup provider {tag}",
            "base_url": "http://fake-provider:8080/v1",
        },
    )
    profile = await _post(
        client,
        f"/api/v1/workspaces/{workspace_id}/model-profiles",
        {
            "provider_id": provider["id"],
            "model_name": "fake-mini",
            "display_name": f"P10 upgrade cleanup profile {tag}",
        },
    )
    agent = await _post(
        client,
        f"/api/v1/workspaces/{workspace_id}/agents",
        {
            "name": f"P10 upgrade cleanup agent {tag}",
            "system_prompt": "Use the requested CLI tool exactly once.",
            "model_profile_id": profile["id"],
        },
    )
    cli = (
        await _post(
            client,
            f"/api/v1/workspaces/{workspace_id}/connections",
            {
                "connector_type": "cli",
                "name": f"P10 upgrade CLI {tag}",
                "auth_type": "none",
                "credentials": {},
                "config": {"default_network": "none"},
            },
        )
    )["connection"]
    await _post(
        client,
        f"/api/v1/workspaces/{workspace_id}/agents/{agent['id']}/grants",
        {
            "capability": "cli.command.execute",
            "scope": {"connection_id": cli["id"], "command": "python3 *"},
            "effect": "allow",
        },
    )
    arguments = json.dumps(
        {
            "connection_id": cli["id"],
            "command": "python3 -c \"print('phase10-upgrade-cleanup')\"",
            "network": "none",
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return agent, f"[[tool:cli.command.execute {arguments}]]"


async def _wait_pending_approval(
    client: httpx.AsyncClient,
    workspace_id: str,
    task_id: str,
) -> tuple[str, str]:
    deadline = time.monotonic() + 120.0
    while time.monotonic() < deadline:
        detail = await _task(client, workspace_id, task_id)
        if detail["runs"]:
            run_id = str(detail["runs"][0]["id"])
            calls = await _calls(client, workspace_id, run_id)
            parked = [call for call in calls if call["status"] == "pending_approval"]
            response = await client.get(
                f"/api/v1/workspaces/{workspace_id}/approvals",
                params={"status": "pending", "limit": 100},
            )
            assert response.status_code == 200, response.text
            approvals = [
                row for row in response.json()["items"] if str(row.get("task_id")) == task_id
            ]
            if len(parked) == len(approvals) == 1:
                return str(approvals[0]["id"]), str(parked[0]["id"])
        await asyncio.sleep(0.2)
    pytest.fail("frozen approval workflow did not park authoritatively")


async def _compatibility_history(
    namespace: str,
    workflow_id: str,
) -> list[tuple[str, str]]:
    client = await TemporalClient.connect(
        integration_config.TEMPORAL_ADDRESS,
        namespace=namespace,
    )
    unbound = client.get_workflow_handle(workflow_id)
    run_id = (await unbound.describe()).run_id
    history = await client.get_workflow_handle(workflow_id, run_id=run_id).fetch_history()
    return activity_schedule_pairs(history)


async def test_inflight_phase9_histories_finish_after_phase10_swap() -> None:
    authority = integration_config.compose_authority()
    upgrade = UpgradeHarness.from_authority(authority)
    upgrade.register_namespaces()
    async with _live_owner() as (client, workspace_id):
        normal_marker = f"p10-upgrade-normal-{uuid.uuid4().hex}"
        normal_connection, normal_agent = await _github_agent(
            client,
            workspace_id,
            tag=uuid.uuid4().hex[:8],
            preset="autonomous",
        )
        approval_marker = f"p10-upgrade-approval-{uuid.uuid4().hex}"
        approval_connection, approval_agent = await _github_agent(
            client,
            workspace_id,
            tag=uuid.uuid4().hex[:8],
            preset="restricted",
        )
        cleanup_agent, cleanup_description = await _cleanup_agent(client, workspace_id)

        normal = await _start_direct_task(
            upgrade,
            client,
            workspace_id,
            scenario="normal",
            agent_id=str(normal_agent["id"]),
            description=_comment_marker(normal_connection["id"], normal_marker),
        )
        approval = await _start_direct_task(
            upgrade,
            client,
            workspace_id,
            scenario="approval",
            agent_id=str(approval_agent["id"]),
            description=_comment_marker(approval_connection["id"], approval_marker),
        )
        cleanup = await _start_direct_task(
            upgrade,
            client,
            workspace_id,
            scenario="cleanup",
            agent_id=str(cleanup_agent["id"]),
            description=cleanup_description,
        )

        sync_agent = await phase7._make_agent(client, workspace_id, uuid.uuid4().hex[:8])
        linear, _secret = await phase7._linear_connection(
            client, workspace_id, uuid.uuid4().hex[:8]
        )
        sync_name = f"P10 upgrade sync {uuid.uuid4().hex[:8]}"
        trigger = await phase7._make_trigger(
            client,
            workspace_id,
            name=sync_name,
            connection_id=linear["id"],
            agent_id=sync_agent["id"],
            comment_back=True,
        )
        issue = await phase7._new_issue(sync_name, "Reply briefly without tools.")
        invocation_id = str(uuid.uuid4())
        event_id = str(uuid.uuid4())
        sync_workflow_id = f"phase10-upgrade-sync-{uuid.uuid4().hex}"
        upgrade.insert_trigger_invocation(
            invocation_id=invocation_id,
            workspace_id=workspace_id,
            trigger_id=str(trigger["id"]),
            event_id=event_id,
            workflow_id=sync_workflow_id,
        )
        sync_temporal = await TemporalClient.connect(
            integration_config.TEMPORAL_ADDRESS,
            namespace=upgrade.scenarios["sync"].namespace,
        )
        started_sync_handle = await sync_temporal.start_workflow(
            "TriggeredTaskWorkflow",
            TriggeredTaskInput(
                workspace_id=workspace_id,
                trigger_id=str(trigger["id"]),
                trigger_name=sync_name,
                invocation_id=invocation_id,
                connection_id=str(linear["id"]),
                event_id=event_id,
                event_type="linear.issue.updated",
                external_source="linear",
                external_id=issue,
                title=sync_name,
                description="Reply briefly without tools.",
                external_url=f"https://linear.app/issue/{issue}",
                agent_id=str(sync_agent["id"]),
                comment_back=True,
            ),
            id=sync_workflow_id,
            task_queue=AGENT_TASK_QUEUE,
        )
        await asyncio.to_thread(
            upgrade.run_phase9_snapshot_once,
            "sync",
            include_trigger_prepare=True,
        )
        invocations = await phase7._invocations(client, workspace_id, trigger["id"])
        invocation = next(row for row in invocations if str(row["id"]) == invocation_id)
        deadline = time.monotonic() + 60.0
        while invocation["task_id"] is None and time.monotonic() < deadline:
            await asyncio.sleep(0.2)
            invocation = (await phase7._invocations(client, workspace_id, trigger["id"]))[0]
        assert invocation["task_id"] and invocation["workflow_id"]
        _sync_detail, sync_domain_run_id = await _wait_run_started(
            client,
            workspace_id,
            str(invocation["task_id"]),
            deadline_seconds=120.0,
        )
        sync_run_id = started_sync_handle.first_execution_run_id
        assert sync_run_id
        sync_handle = sync_temporal.get_workflow_handle(
            str(invocation["workflow_id"]), run_id=sync_run_id
        )
        sync = UpgradeRun(
            scenario="sync",
            task_id=str(invocation["task_id"]),
            workflow_id=str(invocation["workflow_id"]),
            temporal_run_id=sync_run_id,
            domain_run_id=sync_domain_run_id,
            handle=sync_handle,
        )

        for parked in (normal, cleanup, sync):
            await asyncio.to_thread(
                upgrade.start_phase9_worker,
                parked.scenario,
                identity=parked.domain_run_id,
            )
        await asyncio.to_thread(upgrade.start_phase9_worker, "approval")

        arrivals = await _wait_frozen_phase9_arrivals(
            upgrade,
            tuple((parked.scenario, parked.domain_run_id) for parked in (normal, cleanup, sync)),
            probe_timeout=180.0,
        )
        assert arrivals == [
            normal.domain_run_id,
            cleanup.domain_run_id,
            sync.domain_run_id,
        ]
        approval_id, approval_call_id = await _wait_pending_approval(
            client, workspace_id, approval.task_id
        )
        assert await _comment_count(normal_marker) == 0
        assert await _comment_count(approval_marker) == 0
        normal_timeline = await _timeline(client, workspace_id, normal.domain_run_id)
        assert [row["event_type"] for row in normal_timeline].count("agent.step.tool_manifest") == 1
        assert [row["event_type"] for row in normal_timeline].count("agent.step.reasoning") == 0
        normal_manifest_before = authority.run_event_payload(
            normal.domain_run_id,
            event_type="agent.step.tool_manifest",
            step=0,
        )
        assert set(normal_manifest_before) == {"step", "manifest"}
        pre_sync_state = await phase7._fake_state()
        assert [
            comment
            for comment in pre_sync_state["comments"].get(issue, [])
            if sync_name in comment["body"]
        ] == []
        cleanup_volumes = authority._exact_label_ids(
            runner=integration_config.run_command,
            resource="volume",
            label=f"jhin.sandbox.workspace=run-{cleanup.domain_run_id}",
        )
        assert len(cleanup_volumes) == 1

        parked_topology = await asyncio.to_thread(
            upgrade.assert_stage_topology,
            "parked-phase9",
        )
        assert set(parked_topology) == authority.expected_services | {
            f"phase9-agent-worker-{scenario}"
            for scenario in ("normal", "approval", "sync", "cleanup")
        }

        for parked in (normal, approval, cleanup, sync):
            await asyncio.to_thread(upgrade.stop_phase9_worker, parked.scenario, kill=True)
        base_topology = await asyncio.to_thread(
            upgrade.assert_stage_topology,
            "base-only",
        )
        assert set(base_topology) == authority.expected_services
        for parked in (normal, cleanup, sync):
            upgrade.release(parked.scenario, parked.domain_run_id)
        current_workers = await asyncio.to_thread(upgrade.start_phase10_workers)
        assert set(current_workers) == {
            *(
                f"phase10-agent-worker-{scenario}"
                for scenario in ("normal", "approval", "sync", "cleanup")
            ),
            *(
                f"phase10-tool-worker-{scenario}"
                for scenario in ("normal", "approval", "sync", "cleanup")
            ),
        }
        current_images = {
            kind: {
                current_workers[f"phase10-{kind}-worker-{scenario}"]["Image"]
                for scenario in ("normal", "approval", "sync", "cleanup")
            }
            for kind in ("agent", "tool")
        }
        assert all(len(images) == 1 for images in current_images.values())
        assert upgrade.frozen.image_id not in current_images["agent"] | current_images["tool"]
        current_topology = await asyncio.to_thread(
            upgrade.assert_stage_topology,
            "current-phase10",
        )
        assert set(current_topology) == authority.expected_services | set(current_workers)

        decision = await client.post(
            f"/api/v1/workspaces/{workspace_id}/approvals/{approval_id}/approve",
            headers=_csrf(client),
        )
        assert decision.status_code == 409, decision.text
        assert decision.json() == {
            "detail": (
                "Decision 'approved' was recorded, but the task workflow could not "
                "be signaled (it may have already finished)"
            )
        }
        approved_response = await client.get(
            f"/api/v1/workspaces/{workspace_id}/approvals",
            params={"status": "approved", "limit": 100},
        )
        assert approved_response.status_code == 200, approved_response.text
        approved_rows = [
            row for row in approved_response.json()["items"] if str(row["id"]) == approval_id
        ]
        assert len(approved_rows) == 1
        assert approved_rows[0]["status"] == "approved"
        approval_temporal = await TemporalClient.connect(
            integration_config.TEMPORAL_ADDRESS,
            namespace=upgrade.scenarios["approval"].namespace,
        )
        await approval_temporal.get_workflow_handle(
            approval.workflow_id, run_id=approval.temporal_run_id
        ).signal("approval_decision", args=[approval_id, "approved"])

        await asyncio.gather(
            *(
                asyncio.wait_for(parked.handle.result(), timeout=900.0)
                for parked in (normal, approval, cleanup, sync)
            )
        )
        for parked in (normal, approval, cleanup, sync):
            detail = await _wait_task(client, workspace_id, parked.task_id, deadline_seconds=60.0)
            assert detail["task"]["state"] == "completed", detail

        assert await _comment_count(normal_marker) == 1
        assert await _comment_count(approval_marker) == 1
        approval_calls = await _calls(client, workspace_id, approval.domain_run_id)
        assert len(approval_calls) == 1
        assert str(approval_calls[0]["id"]) == approval_call_id
        assert approval_calls[0]["status"] == "completed"
        linear_state = await phase7._fake_state()
        matching_sync = [
            comment for comment in linear_state["comments"][issue] if sync_name in comment["body"]
        ]
        assert len(matching_sync) == 1
        assert (
            authority._exact_label_ids(
                runner=integration_config.run_command,
                resource="volume",
                label=f"jhin.sandbox.workspace=run-{cleanup.domain_run_id}",
            )
            == []
        )

        repaired = await _timeline(client, workspace_id, normal.domain_run_id)
        repaired_types = [row["event_type"] for row in repaired]
        assert repaired_types.count("agent.step.tool_manifest") == 1
        assert repaired_types.count("agent.step.reasoning") == 1
        assert repaired_types.count("run.completed") == 1
        normal_manifest_after = authority.run_event_payload(
            normal.domain_run_id,
            event_type="agent.step.tool_manifest",
            step=0,
        )
        assert normal_manifest_after == normal_manifest_before

        expected_outer_histories = {
            "normal": [
                ("resolve_snapshot", _AGENT_QUEUE),
                ("run_agent_step", _AGENT_QUEUE),
                ("run_agent_step", _AGENT_QUEUE),
                ("finalize_run", _AGENT_QUEUE),
            ],
            "approval": [
                ("resolve_snapshot", _AGENT_QUEUE),
                ("run_agent_step", _AGENT_QUEUE),
                ("resolve_approval", _AGENT_QUEUE),
                ("run_agent_step", _AGENT_QUEUE),
                ("finalize_run", _AGENT_QUEUE),
            ],
            "cleanup": [
                ("resolve_snapshot", _AGENT_QUEUE),
                ("run_agent_step", _AGENT_QUEUE),
                ("run_agent_step", _AGENT_QUEUE),
                ("finalize_run", _AGENT_QUEUE),
            ],
            "sync": [
                ("prepare_triggered_task", _AGENT_QUEUE),
                ("sync_external", _AGENT_QUEUE),
            ],
        }
        for parked in (normal, approval, cleanup, sync):
            assert (
                activity_schedule_pairs(await parked.handle.fetch_history())
                == expected_outer_histories[parked.scenario]
            )
        for parked, steps in ((normal, (0, 1)), (approval, (1,))):
            for step in steps:
                assert await _compatibility_history(
                    upgrade.scenarios[parked.scenario].namespace,
                    f"phase10-compat-advertised-{parked.domain_run_id}-{step}",
                ) == [("resolve_advertised_tools", _TOOL_QUEUE)]
        assert await _compatibility_history(
            upgrade.scenarios["normal"].namespace,
            f"phase10-compat-tool-step-{normal.domain_run_id}-0",
        ) == [("execute_bound_tool", _TOOL_QUEUE)]
        assert (
            await _compatibility_history(
                upgrade.scenarios["normal"].namespace,
                f"phase10-compat-tool-step-{normal.domain_run_id}-1",
            )
            == []
        )
        assert await _compatibility_history(
            upgrade.scenarios["approval"].namespace,
            f"phase10-compat-approval-{approval_id}",
        ) == [("resolve_bound_tool_approval", _TOOL_QUEUE)]
        assert (
            await _compatibility_history(
                upgrade.scenarios["approval"].namespace,
                f"phase10-compat-tool-step-{approval.domain_run_id}-1",
            )
            == []
        )
        assert await _compatibility_history(
            upgrade.scenarios["sync"].namespace,
            f"phase10-compat-sync-{sync.domain_run_id}",
        ) == [("sync_external_tool", _TOOL_QUEUE)]
        assert await _compatibility_history(
            upgrade.scenarios["cleanup"].namespace,
            f"phase10-compat-cleanup-{cleanup.domain_run_id}",
        ) == [("cleanup_run_workspace", _TOOL_QUEUE)]

        for scenario in ("normal", "approval", "sync", "cleanup"):
            service = f"phase10-agent-worker-{scenario}"
            current_agent = current_workers[service]
            agent_environment = current_agent["Config"].get("Env", [])
            assert not any(item.startswith("SANDBOX_RUNNER_") for item in agent_environment)
            assert f"{authority.project}_runner" not in current_agent["NetworkSettings"]["Networks"]
            assert authority.upgrade_agent_runner_probe(service) != 0
            import_probe = authority._run(
                authority.compose_command(
                    "--profile",
                    "phase10-upgrade",
                    "exec",
                    "-T",
                    service,
                    "python",
                    "-c",
                    "import jhin_connectors",
                    upgrade=True,
                ),
                runner=integration_config.run_command,
                timeout=30.0,
                check=False,
            )
            assert import_probe.returncode != 0
