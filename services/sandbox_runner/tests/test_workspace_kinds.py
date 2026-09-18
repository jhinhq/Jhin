"""Durable workspaces, from the runner's side of the wall.

The runner knows nothing about agents, leases or eviction policy — that all
lives in the control plane, which is the only place that knows when a workspace
was last *used*. What the runner owns is three things, and each is proven here:

* a job gets exactly one mount, and it is the volume its key names — the
  mechanical half of the isolation argument, the other half being that two
  agents can never derive one key;
* an agent-kind volume is never reaped by age, because creation age is not use
  age and an age sweep would wipe a healthy agent's disk every 24 hours, which
  is the bug durable workspaces exist to fix;
* the size the init container reports is parsed strictly, or not at all;
* the delete route answers exactly what the manager said — 204 for a volume
  that is gone, 409 for one Docker refused to remove.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from aiodocker.exceptions import DockerError
from fastapi import FastAPI

from jhin_connectors.cli.workspace import agent_workspace_key, run_workspace_key
from jhin_sandbox_runner.jobs import (
    WORKSPACE_KIND_AGENT,
    WORKSPACE_KIND_LABEL,
    WORKSPACE_KIND_RUN,
    WORKSPACE_LABEL,
    JobManager,
    build_container_config,
    resolve_limits,
    workspace_volume_name,
)
from jhin_sandbox_runner.main import install_existing_runner_routes
from jhin_sandbox_runner.schemas import SandboxJobRequest
from jhin_sandbox_runner.settings import Settings

SETTINGS = Settings(
    sandbox_runner_token="test-token",
    sandbox_default_image="jhin-sandbox:test",
    sandbox_network="jhin_sandbox_test",
    sandbox_docker_mode="rootless",
    sandbox_docker_transport_url="http://rootless-docker-transport:2375",
)


def _config(workspace_key: str) -> dict[str, object]:
    request = SandboxJobRequest.model_validate(
        {
            "job_id": "0123456789abcdef",
            "command": ["bash", "-lc", "true"],
            "workspace_key": workspace_key,
        }
    )
    cpu, memory, pids, _ = resolve_limits(request, SETTINGS)
    return build_container_config(
        request,
        SETTINGS,
        image=SETTINGS.sandbox_default_image,
        cpu_limit=cpu,
        memory_mb=memory,
        pids_limit=pids,
    )


class TestOneJobOneVolume:
    def test_an_agent_key_mounts_exactly_that_agents_volume_and_nothing_else(self) -> None:
        key = agent_workspace_key(uuid4(), uuid4())
        host = _config(key)["HostConfig"]
        assert host["Mounts"] == [  # type: ignore[index]
            {
                "Type": "volume",
                "Source": workspace_volume_name(key),
                "Target": "/workspace",
                "ReadOnly": False,
                "VolumeOptions": {"NoCopy": True},
            }
        ]
        assert "Binds" not in host  # type: ignore[operator]

    def test_two_agents_keys_produce_two_different_volumes(self) -> None:
        tenant = uuid4()
        first = _config(agent_workspace_key(tenant, uuid4()))["HostConfig"]["Mounts"]  # type: ignore[index]
        second = _config(agent_workspace_key(tenant, uuid4()))["HostConfig"]["Mounts"]  # type: ignore[index]
        assert first[0]["Source"] != second[0]["Source"]  # type: ignore[index]

    def test_an_agent_key_is_accepted_by_the_job_schema(self) -> None:
        """71 characters against an 81-character limit: the derivation cannot
        produce a key the runner would refuse."""
        key = agent_workspace_key(uuid4(), uuid4())
        request = SandboxJobRequest.model_validate(
            {"job_id": "0123456789abcdef", "command": ["true"], "workspace_key": key}
        )
        assert request.workspace_key == key


class TestReapingIsScopedToRunVolumes:
    @pytest.mark.parametrize(
        ("labels", "expected"),
        [
            (
                {WORKSPACE_LABEL: "agent-x", WORKSPACE_KIND_LABEL: WORKSPACE_KIND_AGENT},
                WORKSPACE_KIND_AGENT,
            ),
            (
                {WORKSPACE_LABEL: "run-x", WORKSPACE_KIND_LABEL: WORKSPACE_KIND_RUN},
                WORKSPACE_KIND_RUN,
            ),
        ],
    )
    def test_the_label_decides_when_it_is_there(
        self, labels: dict[str, str], expected: str
    ) -> None:
        assert JobManager.workspace_kind({"Labels": labels}) == expected

    def test_a_volume_from_before_the_label_is_read_from_its_key(self) -> None:
        """Every workspace volume that predates this label is run-scoped, since
        that was the only kind there was — so the fallback is right for all of
        them, and keeps the behaviour that already shipped."""
        run_key = run_workspace_key(uuid4())
        assert JobManager.workspace_kind({"Labels": {WORKSPACE_LABEL: run_key}}) == (
            WORKSPACE_KIND_RUN
        )
        agent_key = agent_workspace_key(uuid4(), uuid4())
        assert JobManager.workspace_kind({"Labels": {WORKSPACE_LABEL: agent_key}}) == (
            WORKSPACE_KIND_AGENT
        )

    @pytest.mark.parametrize("entry", [{}, {"Labels": None}, {"Labels": {}}, {"Labels": "junk"}])
    def test_an_unreadable_label_set_falls_back_to_run(self, entry: dict[str, object]) -> None:
        """Reaping a stale run volume is what already happened; retaining disk
        forever is the new risk. A guess falls on the side of the old
        behaviour."""
        assert JobManager.workspace_kind(entry) == WORKSPACE_KIND_RUN

    def test_a_forged_kind_value_is_not_trusted(self) -> None:
        assert (
            JobManager.workspace_kind(
                {"Labels": {WORKSPACE_LABEL: "run-x", WORKSPACE_KIND_LABEL: "keep-forever"}}
            )
            == WORKSPACE_KIND_RUN
        )


class TestTheMeasurementIsParsedStrictly:
    def test_one_line_is_read(self) -> None:
        assert JobManager._parse_measurement("JHIN_WS_BYTES=4096\nJHIN_WS_PARTIAL=0\n") == (
            4096,
            False,
        )

    def test_a_partial_walk_is_flagged(self) -> None:
        assert JobManager._parse_measurement("JHIN_WS_BYTES=10\nJHIN_WS_PARTIAL=1\n") == (
            10,
            True,
        )

    def test_two_lines_are_no_measurement_at_all(self) -> None:
        """A stream carrying two is ambiguous, and resolving it by "last one
        wins" hands the decision to whoever printed last."""
        assert JobManager._parse_measurement("JHIN_WS_BYTES=1\nJHIN_WS_BYTES=999999\n") == (
            None,
            False,
        )

    @pytest.mark.parametrize(
        "logs",
        [
            "",
            "JHIN_WS_BYTES=\n",
            "JHIN_WS_BYTES=-1\n",
            "JHIN_WS_BYTES=abc\n",
            "prefix JHIN_WS_BYTES=10\n",
            "JHIN_WS_BYTES=10 suffix\n",
            "JHIN_WS_BYTES=123456789012345678901\n",
        ],
    )
    def test_anything_that_is_not_exactly_the_line_is_ignored(self, logs: str) -> None:
        assert JobManager._parse_measurement(logs) == (None, False)


class TestEveryJobMeasures:
    """No throttle, on either kind, and no cached "measured recently".

    The throttle was what made the cap unenforceable. A durable workspace has
    no filesystem quota behind it — the stored number *is* the cap — so a
    ten-minute interval was a ten-minute window in which an agent could write
    as much as the disk allowed. And a run-scoped workspace is not exempt
    because it dies with its run: its bytes are on the same host and count
    against the same tenant budget while it lives, so a stale number for it is
    a stale number for the budget.
    """

    def test_the_manager_keeps_no_measured_recently_state(self) -> None:
        manager = JobManager(SETTINGS)
        assert not hasattr(manager, "_should_measure")
        assert not hasattr(manager, "_measured_at")

    def test_a_floor_is_reported_as_a_floor(self) -> None:
        """The wire's whole vocabulary for "this is not a size"."""
        logs = "JHIN_WS_BYTES=21520384\nJHIN_WS_PARTIAL=1\n"
        assert JobManager._parse_measurement(logs) == (
            21520384,
            True,
        )


class _Volume:
    def __init__(self, error: DockerError | None) -> None:
        self._error = error

    async def delete(self) -> None:
        if self._error is not None:
            raise self._error


class _Volumes:
    def __init__(self, *, get_error: DockerError | None, delete_error: DockerError | None) -> None:
        self._get_error = get_error
        self._delete_error = delete_error
        self.asked: list[str] = []

    async def get(self, name: str) -> _Volume:
        self.asked.append(name)
        if self._get_error is not None:
            raise self._get_error
        return _Volume(self._delete_error)


class TestDeletingAWorkspaceTellsTheTruth:
    """True means the volume is gone. Nothing else may mean it.

    Docker refuses to remove a volume a container still has mounted, and that
    refusal used to be collapsed into the same ``False`` as "no such volume"
    and then discarded entirely by the route, which answered 204 regardless.
    Everything downstream rested on that answer: an operator's reset was
    cleared for nothing, the audit trail's ``deleted`` field was untrue, and a
    workspace that was still full came back into service recorded as empty.
    """

    def _manager(self, **errors: DockerError | None) -> tuple[JobManager, _Volumes]:
        manager = JobManager(SETTINGS)
        volumes = _Volumes(
            get_error=errors.get("get_error"), delete_error=errors.get("delete_error")
        )
        manager._docker = SimpleNamespace(volumes=volumes)  # type: ignore[assignment]
        return manager, volumes

    async def test_a_removed_volume_is_gone(self) -> None:
        manager, volumes = self._manager()
        assert await manager.delete_workspace("run-abc") is True
        assert volumes.asked == [workspace_volume_name("run-abc")]

    async def test_a_volume_that_was_never_there_is_also_gone(self) -> None:
        manager, _ = self._manager(get_error=DockerError(404, {"message": "no such volume"}))
        assert await manager.delete_workspace("run-abc") is True

    async def test_a_volume_docker_refuses_to_remove_is_not_gone(self) -> None:
        manager, _ = self._manager(delete_error=DockerError(409, {"message": "volume is in use"}))
        assert await manager.delete_workspace("run-abc") is False

    async def test_an_unreachable_daemon_is_not_gone(self) -> None:
        manager, _ = self._manager(get_error=DockerError(500, {"message": "server error"}))
        assert await manager.delete_workspace("run-abc") is False


class TestTheDeleteRouteSaysWhatTheManagerSaid:
    """``manager.delete_workspace`` returning False has to reach the caller.

    The manager telling the truth is only half of it: the route used to
    discard the boolean and answer 204 either way, so a volume Docker refused
    to remove was reported to the control plane as destroyed — an operator's
    reset cleared for nothing, ``deleted: true`` in an audit row that was not,
    and a still-full disk back in service recorded as empty and therefore
    invisible to the cap and to every future sweep. Nothing tested the
    refusal, which is why it survived the fix to the manager underneath it.
    """

    def _client(self, deleted: bool) -> tuple[httpx.AsyncClient, list[str]]:
        asked: list[str] = []

        async def delete_workspace(workspace_key: str) -> bool:
            asked.append(workspace_key)
            return deleted

        app = FastAPI()
        install_existing_runner_routes(
            app, SETTINGS, SimpleNamespace(delete_workspace=delete_workspace)
        )
        transport = httpx.ASGITransport(app=app)
        return (
            httpx.AsyncClient(transport=transport, base_url="http://runner"),
            asked,
        )

    async def _delete(self, client: httpx.AsyncClient, key: str) -> httpx.Response:
        return await client.delete(
            f"/v1/workspaces/{key}",
            headers={"Authorization": f"Bearer {SETTINGS.sandbox_runner_token}"},
        )

    async def test_a_volume_that_is_gone_is_204(self) -> None:
        client, asked = self._client(deleted=True)
        async with client:
            response = await self._delete(client, "run-abc")
        assert response.status_code == 204
        assert asked == ["run-abc"]

    async def test_a_volume_docker_refused_to_remove_is_409(self) -> None:
        """The whole point: a refusal must not read as a deletion.

        ``jhin_connectors.cli.runner_client.delete_workspace`` accepts 204 and
        404 and nothing else, so this status is what stops the control plane
        recording a full disk as empty.
        """
        client, asked = self._client(deleted=False)
        async with client:
            response = await self._delete(client, "agent-abc")
        assert response.status_code == 409
        assert asked == ["agent-abc"]

    @pytest.mark.parametrize("key", ["../etc", "has space", "a" * 82, "-leading"])
    async def test_a_key_the_job_schema_would_refuse_never_reaches_the_manager(
        self, key: str
    ) -> None:
        client, asked = self._client(deleted=True)
        async with client:
            response = await self._delete(client, key)
        assert response.status_code in (404, 422), key
        assert asked == []
