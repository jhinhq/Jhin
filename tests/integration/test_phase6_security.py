"""Phase 6 security tests (plan 32.5): sandbox isolation invariants, for real.

Two layers:

* Runner-level — jobs submitted straight to the sandbox runner's dev-only
  localhost binding (compose.dev publishes 127.0.0.1:8093). Each test runs a
  real container and asserts on its observed behaviour:
  no Docker socket, non-root uid + read-only rootfs, network `none` blocks
  all egress including control-plane DNS, `internet` reaches only the
  sandbox bridge, pids limit + timeout enforcement, secret redaction in
  captured output, container removal after completion/cancel, workspace
  volume lifecycle, and bearer-token auth on the API itself.

* Policy-level — cli.* capabilities are deny-by-default: an ungranted agent
  is denied outright and a granted agent is denied on a command outside its
  fnmatch scope (plan 48.2), both audited.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, Protocol, cast
from uuid import uuid4

import httpx
import pytest

from jhin_api.seed import DEV_OWNER_EMAIL, DEV_OWNER_PASSWORD

from .conftest import API_URL, REPO_ROOT, compose

pytestmark = pytest.mark.integration

# Dev-only localhost binding from compose.dev.yaml; inside compose the runner
# is only reachable on the `runner` network.
RUNNER_URL = os.environ.get("SANDBOX_RUNNER_DEV_URL", "http://localhost:8093")
RUNNER_TOKEN = os.environ.get("SANDBOX_RUNNER_TOKEN", "dev-sandbox-runner-token")
FAKE_PROVIDER_URL = "http://fake-provider:8080/v1"
JOB_WAIT_SECONDS = 90.0
TASK_TIMEOUT_SECONDS = 120.0
FORBIDDEN_JOB_SOCKET_PATHS = (
    "/var/run/docker.sock",
    "/run/jhin/docker.sock",
    "/run/host/docker.sock",
)
FORBIDDEN_JOB_DNS_NAMES = (
    "postgres",
    "api",
    "temporal",
    "nats",
    "sandbox-runner",
    "agent-worker",
    "tool-worker",
    "rootless-docker-transport",
)
JOB_CONTAINER_USER = "1000:1000"
SANDBOX_NETWORK = os.environ.get("SANDBOX_NETWORK", "jhin_sandbox")


class CancelResponse(Protocol):
    status_code: int
    text: str


def _job_id() -> str:
    return uuid4().hex


async def _submit(client: httpx.AsyncClient, body: dict[str, Any]) -> dict[str, Any]:
    response = await client.post(
        "/v1/jobs", json=body, headers={"Authorization": f"Bearer {RUNNER_TOKEN}"}
    )
    assert response.status_code == 202, f"submit: {response.status_code} {response.text}"
    payload: dict[str, Any] = response.json()
    return payload


async def _wait(client: httpx.AsyncClient, job_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + JOB_WAIT_SECONDS
    snapshot: dict[str, Any] = {}
    while time.monotonic() < deadline:
        response = await client.get(
            f"/v1/jobs/{job_id}", headers={"Authorization": f"Bearer {RUNNER_TOKEN}"}
        )
        assert response.status_code == 200, response.text
        snapshot = response.json()
        if snapshot["status"] != "running":
            return snapshot
        await asyncio.sleep(0.3)
    pytest.fail(f"job {job_id} still running after {JOB_WAIT_SECONDS}s: {snapshot}")


async def _run_job(client: httpx.AsyncClient, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"job_id": _job_id(), **overrides}
    await _submit(client, body)
    return await _wait(client, body["job_id"])


def _bash(script: str) -> list[str]:
    return ["bash", "-c", script]


def _docker(*args: str) -> str:
    result = subprocess.run(
        ["docker", *args], cwd=REPO_ROOT, capture_output=True, text=True, timeout=30, check=True
    )
    return result.stdout.strip()


def assert_job_container_boundary(
    inspected: object,
    *,
    job_id: str,
    network_policy: str,
    sandbox_network: str,
) -> None:
    """Fail closed on the host-observed Docker authority of one live job."""
    assert isinstance(inspected, dict), "container inspection must be an object"
    config = inspected.get("Config")
    host = inspected.get("HostConfig")
    mounts = inspected.get("Mounts")
    assert isinstance(config, dict), "container inspection Config must be an object"
    assert isinstance(host, dict), "container inspection HostConfig must be an object"
    assert isinstance(mounts, list), "container inspection Mounts must be a list"

    expected_network = "none" if network_policy == "none" else sandbox_network
    assert network_policy in {"none", "internet"}, "unexpected job network policy"
    assert host.get("NetworkMode") == expected_network, "job network authority drifted"
    assert (host.get("GroupAdd") or []) == [], "job supplemental group authority drifted"
    assert config.get("User") == JOB_CONTAINER_USER, "job user authority drifted"
    labels = config.get("Labels")
    assert isinstance(labels, dict), "job labels must be an object"
    assert labels.get("jhin.sandbox.job") == job_id, "job label identity drifted"

    environment = config.get("Env") or []
    assert isinstance(environment, list) and all(isinstance(item, str) for item in environment), (
        "job environment must be a string list"
    )
    for item in environment:
        name = item.partition("=")[0].upper()
        assert not name.startswith(("DOCKER_", "SANDBOX_DOCKER_")), (
            "job environment retained Docker authority"
        )
        assert "rootless-docker-transport" not in item.casefold(), (
            "job environment retained adapter authority"
        )
        assert not any(path in item for path in FORBIDDEN_JOB_SOCKET_PATHS), (
            "job environment retained socket authority"
        )

    binds = host.get("Binds") or []
    assert isinstance(binds, list) and all(isinstance(bind, str) for bind in binds), (
        "job bind authority must be a string list"
    )
    for bind in binds:
        assert not any(path in bind for path in FORBIDDEN_JOB_SOCKET_PATHS), (
            "job bind retained Docker authority"
        )
    for mount in mounts:
        assert isinstance(mount, dict), "job mount inspection must be an object"
        serialized = json.dumps(mount, sort_keys=True)
        assert not any(path in serialized for path in FORBIDDEN_JOB_SOCKET_PATHS), (
            "job mount retained Docker authority"
        )


async def _wait_for_job_container(job_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        identifiers = _docker(
            "ps", "-aq", "--filter", f"label=jhin.sandbox.job={job_id}"
        ).splitlines()
        assert len(identifiers) <= 1, f"job label matched multiple containers: {identifiers}"
        if identifiers:
            decoded: object = json.loads(_docker("inspect", identifiers[0]))
            assert isinstance(decoded, list) and len(decoded) == 1, (
                "docker inspect must return exactly one job container"
            )
            inspected = decoded[0]
            assert isinstance(inspected, dict), "docker inspect row must be an object"
            return inspected
        await asyncio.sleep(0.2)
    pytest.fail(f"job {job_id} never produced a labeled container")


async def _wait_for_job_container_removal(job_id: str) -> None:
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        if _docker("ps", "-aq", "--filter", f"label=jhin.sandbox.job={job_id}") == "":
            return
        await asyncio.sleep(0.2)
    identifiers = _docker("ps", "-aq", "--filter", f"label=jhin.sandbox.job={job_id}").splitlines()
    assert len(identifiers) <= 1, f"job label matched multiple containers: {identifiers}"
    if identifiers:
        _docker("rm", "-f", identifiers[0])
    remaining = _docker("ps", "-aq", "--filter", f"label=jhin.sandbox.job={job_id}")
    assert remaining == "", f"job {job_id} container survived forced cleanup: {remaining}"


async def cleanup_live_job(
    *,
    cancel: Callable[[], Awaitable[object]],
    wait_terminal: Callable[[], Awaitable[dict[str, Any]]],
    remove_container: Callable[[], Awaitable[None]],
) -> dict[str, Any]:
    """Attempt every cleanup step before surfacing cancellation or cleanup failures."""
    cancelled: object | None = None
    terminal: dict[str, Any] | None = None
    cleanup_errors: list[Exception] = []

    try:
        cancelled = await cancel()
    except Exception as exc:
        cleanup_errors.append(exc)
    try:
        terminal = await wait_terminal()
    except Exception as exc:
        cleanup_errors.append(exc)
    try:
        await remove_container()
    except Exception as exc:
        cleanup_errors.append(exc)

    if cleanup_errors:
        raise ExceptionGroup("live job cleanup failed", cleanup_errors)
    assert cancelled is not None
    cancellation_result = cast(CancelResponse, cancelled)
    assert cancellation_result.status_code == 200, (
        f"cancel: {cancellation_result.status_code} {cancellation_result.text}"
    )
    assert terminal is not None
    return terminal


async def _wait_for_boundary_probe(client: httpx.AsyncClient, job_id: str) -> None:
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        response = await client.get(
            f"/v1/jobs/{job_id}/logs",
            headers={"Authorization": f"Bearer {RUNNER_TOKEN}"},
        )
        assert response.status_code == 200, response.text
        if "boundary-probe-complete" in response.json()["stdout"]:
            return
        await asyncio.sleep(0.2)
    pytest.fail(f"job {job_id} boundary probe did not complete")


def assert_job_boundary_denials(output: str) -> None:
    """Assert a live job cannot discover any Docker-authority boundary."""
    for path in FORBIDDEN_JOB_SOCKET_PATHS:
        assert f"socket-denied:{path}" in output, output
    for name in FORBIDDEN_JOB_DNS_NAMES:
        assert f"dns-denied:{name}" in output, output
    assert "adapter-tcp-denied" in output, output
    assert "sandbox-docker-env-denied" in output, output
    assert "boundary-probe-complete" in output, output


def _job_boundary_probe() -> str:
    socket_paths = " ".join(FORBIDDEN_JOB_SOCKET_PATHS)
    dns_names = " ".join(FORBIDDEN_JOB_DNS_NAMES)
    return (
        f"for p in {socket_paths}; do "
        '  if test ! -e "$p" && ! grep -Fq "$p" /proc/self/mountinfo; then '
        "    echo socket-denied:$p; "
        "  fi; "
        "done; "
        f"for h in {dns_names}; do "
        '  getent hosts "$h" >/dev/null 2>&1 || echo dns-denied:$h; '
        "done; "
        "curl -fsS --max-time 3 http://rootless-docker-transport:2375/_ping "
        ">/dev/null 2>&1 || echo adapter-tcp-denied; "
        "if ! env | grep -q '^SANDBOX_DOCKER_'; then echo sandbox-docker-env-denied; fi; "
        "echo boundary-probe-complete"
    )


@pytest.fixture
async def runner() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(base_url=RUNNER_URL, timeout=30.0) as client:
        yield client


# --- API hardening -------------------------------------------------------------


async def test_runner_rejects_missing_or_wrong_token(runner: httpx.AsyncClient) -> None:
    body = {"job_id": _job_id(), "command": ["true"]}
    anonymous = await runner.post("/v1/jobs", json=body)
    assert anonymous.status_code == 401
    wrong = await runner.post(
        "/v1/jobs", json=body, headers={"Authorization": "Bearer wrong-token"}
    )
    assert wrong.status_code == 401
    status = await runner.get(f"/v1/jobs/{body['job_id']}")
    assert status.status_code == 401  # the unauthorized submit never created a job


# --- no Docker socket, non-root, read-only rootfs (plan 14.1, 48.7) ------------


@pytest.mark.parametrize("network_policy", ["none", "internet"])
async def test_live_job_has_no_docker_authority(
    runner: httpx.AsyncClient,
    network_policy: str,
) -> None:
    body: dict[str, Any] = {
        "job_id": _job_id(),
        "network_policy": network_policy,
        "command": _bash(f"{_job_boundary_probe()}; sleep 120"),
        "timeout_seconds": 180,
    }
    await _submit(runner, body)
    inspected: dict[str, Any] | None = None
    job: dict[str, Any] | None = None
    try:
        inspected = await _wait_for_job_container(body["job_id"])
        await _wait_for_boundary_probe(runner, body["job_id"])
    finally:
        job = await cleanup_live_job(
            cancel=lambda: runner.post(
                f"/v1/jobs/{body['job_id']}/cancel",
                headers={"Authorization": f"Bearer {RUNNER_TOKEN}"},
            ),
            wait_terminal=lambda: _wait(runner, body["job_id"]),
            remove_container=lambda: _wait_for_job_container_removal(body["job_id"]),
        )

    assert inspected is not None
    assert job is not None and job["status"] == "cancelled", job
    assert_job_container_boundary(
        inspected,
        job_id=body["job_id"],
        network_policy=network_policy,
        sandbox_network=SANDBOX_NETWORK,
    )
    assert_job_boundary_denials(job["stdout"])


async def test_job_is_non_root_with_readonly_rootfs(runner: httpx.AsyncClient) -> None:
    job = await _run_job(
        runner,
        command=_bash(
            "id -u; id -un; "
            "touch /etc/probe 2>&1; touch /usr/bin/probe 2>&1; touch /probe 2>&1; "
            "echo ok > /workspace/probe && echo workspace-writable; "
            "echo ok > /tmp/probe && echo tmp-writable"
        ),
    )
    assert job["status"] == "completed", job
    lines = job["stdout"].splitlines()
    assert lines[0] == "1000"
    assert lines[1] != "root"
    assert job["stdout"].count("Read-only file system") == 3
    assert "workspace-writable" in job["stdout"]
    assert "tmp-writable" in job["stdout"]


# --- network policy (plan 14.4) -------------------------------------------------


async def test_network_none_blocks_all_egress(runner: httpx.AsyncClient) -> None:
    job = await _run_job(
        runner,
        network_policy="none",
        command=_bash(
            "curl -s -m 4 http://fake-github:8080/_state >/dev/null 2>&1 "
            "|| echo fake-github-unreachable; "
            "curl -s -m 4 http://example.com >/dev/null 2>&1 || echo external-unreachable; "
            "for h in postgres api temporal nats sandbox-runner agent-worker tool-worker "
            "rootless-docker-transport; do "
            "  getent hosts $h >/dev/null 2>&1 || echo no-dns-$h; "
            "done; "
            "echo interfaces=$(ls /sys/class/net | sort | tr '\\n' ',')"
        ),
    )
    assert job["status"] == "completed", job
    out = job["stdout"]
    assert "fake-github-unreachable" in out
    assert "external-unreachable" in out
    # Control-plane service names must not even resolve (plan 14.4).
    for host in FORBIDDEN_JOB_DNS_NAMES:
        assert f"no-dns-{host}" in out, out
    # No veth into any bridge — only loopback and inert kernel tunnel stubs.
    interfaces_line = next(line for line in out.splitlines() if line.startswith("interfaces="))
    interfaces = set(interfaces_line.removeprefix("interfaces=").strip(",").split(","))
    assert "lo" in interfaces, out
    assert not any(name.startswith("eth") for name in interfaces), out


async def test_network_internet_reaches_sandbox_bridge_only(runner: httpx.AsyncClient) -> None:
    """`internet` jobs join the dedicated jhin_sandbox bridge. In dev/test the
    fake-github service is attached to that bridge (simulating a public
    endpoint), while control-plane services stay unreachable."""
    job = await _run_job(
        runner,
        network_policy="internet",
        command=_bash(
            "curl -s -m 5 http://fake-github:8080/_state >/dev/null && echo fake-github-ok; "
            "for h in postgres api temporal nats sandbox-runner agent-worker tool-worker "
            "rootless-docker-transport; do "
            "  getent hosts $h >/dev/null 2>&1 || echo no-dns-$h; "
            "done"
        ),
    )
    assert job["status"] == "completed", job
    out = job["stdout"]
    assert "fake-github-ok" in out
    for host in FORBIDDEN_JOB_DNS_NAMES:
        assert f"no-dns-{host}" in out, out


# --- resource limits + timeout (plan 14.3) --------------------------------------


_FORK_PROBE = """
import os, time
held, failed = [], 0
for _ in range(64):
    try:
        pid = os.fork()
    except OSError:
        failed += 1
        continue
    if pid == 0:
        time.sleep(30)
        os._exit(0)
    held.append(pid)
print(f"forked={len(held)} failed={failed}", flush=True)
"""


async def test_pids_limit_enforced(runner: httpx.AsyncClient) -> None:
    """A tiny pids cgroup cap stops a fork bomb: os.fork fails with EAGAIN
    once the cap is reached (children hold their slots by sleeping)."""
    job = await _run_job(
        runner,
        pids_limit=16,
        timeout_seconds=60,
        command=["bash", "-c", f"cat /sys/fs/cgroup/pids.max; python3 -c '{_FORK_PROBE}'"],
    )
    assert job["status"] == "completed", job
    lines = job["stdout"].splitlines()
    assert lines[0] == "16"
    result_line = next(line for line in lines if line.startswith("forked="))
    forked, failed = (int(part.split("=")[1]) for part in result_line.split())
    assert forked < 16, job["stdout"]  # cap includes bash + python themselves
    assert failed > 0 and forked + failed == 64, job["stdout"]


async def test_timeout_kills_long_job_and_removes_container(runner: httpx.AsyncClient) -> None:
    body: dict[str, Any] = {
        "job_id": _job_id(),
        "command": _bash("sleep 120"),
        "timeout_seconds": 3,
    }
    await _submit(runner, body)
    job = await _wait(runner, body["job_id"])
    assert job["status"] == "timeout", job
    assert _docker("ps", "-aq", "--filter", f"label=jhin.sandbox.job={body['job_id']}") == "", (
        "timed-out container must be force-removed"
    )


# --- secret redaction (plan 48.9) -----------------------------------------------


async def test_secret_env_never_appears_in_captured_output(runner: httpx.AsyncClient) -> None:
    secret_value = f"sbx-secret-{uuid4().hex}"
    body: dict[str, Any] = {
        "job_id": _job_id(),
        "secret_env": {"MY_TOKEN": secret_value},
        "command": _bash('echo "token=$MY_TOKEN"; echo "err=$MY_TOKEN" >&2; env | grep MY_TOKEN'),
    }
    await _submit(runner, body)
    job = await _wait(runner, body["job_id"])
    assert job["status"] == "completed", job
    dumped = json.dumps(job)
    assert secret_value not in dumped, "secret leaked into job status payload"
    assert "[REDACTED]" in job["stdout"]
    assert "[REDACTED]" in job["stderr"]
    # The logs endpoint is redacted the same way.
    logs = await runner.get(
        f"/v1/jobs/{body['job_id']}/logs", headers={"Authorization": f"Bearer {RUNNER_TOKEN}"}
    )
    assert logs.status_code == 200
    assert secret_value not in logs.text
    assert "[REDACTED]" in logs.json()["stdout"]


# --- container + workspace lifecycle (plan 14.3) --------------------------------


async def test_container_removed_after_completion_and_cancel(runner: httpx.AsyncClient) -> None:
    finished = await _run_job(runner, command=_bash("echo done"))
    assert finished["status"] == "completed"
    assert _docker("ps", "-aq", "--filter", f"label=jhin.sandbox.job={finished['job_id']}") == ""

    body: dict[str, Any] = {"job_id": _job_id(), "command": _bash("sleep 120")}
    await _submit(runner, body)
    await asyncio.sleep(1.0)
    cancelled = await runner.post(
        f"/v1/jobs/{body['job_id']}/cancel", headers={"Authorization": f"Bearer {RUNNER_TOKEN}"}
    )
    assert cancelled.status_code == 200
    job = await _wait(runner, body["job_id"])
    assert job["status"] == "cancelled", job
    assert _docker("ps", "-aq", "--filter", f"label=jhin.sandbox.job={body['job_id']}") == ""


async def test_workspace_volume_persists_between_jobs_and_deletes(
    runner: httpx.AsyncClient,
) -> None:
    key = f"sectest-{uuid4().hex[:12]}"
    marker = uuid4().hex
    first = await _run_job(
        runner, workspace_key=key, command=_bash(f"echo {marker} > /workspace/state.txt")
    )
    assert first["status"] == "completed", first
    second = await _run_job(runner, workspace_key=key, command=_bash("cat /workspace/state.txt"))
    assert second["status"] == "completed", second
    assert marker in second["stdout"]

    deleted = await runner.delete(
        f"/v1/workspaces/{key}", headers={"Authorization": f"Bearer {RUNNER_TOKEN}"}
    )
    assert deleted.status_code == 204
    volumes = _docker("volume", "ls", "-q", "--filter", f"label=jhin.sandbox.workspace={key}")
    assert volumes == "", "workspace volume must be gone after DELETE"


# --- deny-by-default policy for cli.* (plan 48.2) --------------------------------


@pytest.fixture
async def owner() -> AsyncIterator[tuple[httpx.AsyncClient, str]]:
    async with httpx.AsyncClient(base_url=API_URL, timeout=30.0) as client:
        credentials = {"email": DEV_OWNER_EMAIL, "password": DEV_OWNER_PASSWORD}
        login = await client.post("/api/v1/auth/login", json=credentials)
        if login.status_code != 200:
            compose("run", "--rm", "--no-deps", "api", "jhin-seed-dev")
            login = await client.post("/api/v1/auth/login", json=credentials)
        assert login.status_code == 200, login.text
        yield client, login.json()["memberships"][0]["workspace_id"]


def _csrf(client: httpx.AsyncClient) -> dict[str, str]:
    token = client.cookies.get("jhin_csrf")
    assert token, "no CSRF cookie after login"
    return {"x-csrf-token": token}


async def _post(
    client: httpx.AsyncClient, path: str, body: dict[str, Any], expect: int = 201
) -> dict[str, Any]:
    response = await client.post(path, json=body, headers=_csrf(client))
    assert response.status_code == expect, f"{path}: {response.status_code} {response.text}"
    payload: dict[str, Any] = response.json()
    return payload


async def _get(client: httpx.AsyncClient, path: str, **params: Any) -> Any:
    response = await client.get(path, params=params or None)
    assert response.status_code == 200, f"{path}: {response.status_code} {response.text}"
    return response.json()


async def _make_agent(client: httpx.AsyncClient, ws: str, tag: str, name: str) -> dict[str, Any]:
    provider = await _post(
        client,
        f"/api/v1/workspaces/{ws}/model-providers",
        {
            "type": "openai_compatible",
            "display_name": f"P6S provider {name} {tag}",
            "base_url": FAKE_PROVIDER_URL,
        },
    )
    profile = await _post(
        client,
        f"/api/v1/workspaces/{ws}/model-profiles",
        {
            "provider_id": provider["id"],
            "model_name": "fake-mini",
            "display_name": f"P6S profile {name} {tag}",
        },
    )
    return await _post(
        client,
        f"/api/v1/workspaces/{ws}/agents",
        {
            "name": f"P6S {name} {tag}",
            "system_prompt": "You complete tasks, using tools when instructed.",
            "model_profile_id": profile["id"],
        },
    )


async def _run_task(
    client: httpx.AsyncClient, ws: str, agent_id: str, title: str, description: str
) -> dict[str, Any]:
    task = await _post(
        client,
        f"/api/v1/workspaces/{ws}/agents/{agent_id}/assign-task",
        {"title": title, "description": description},
    )
    deadline = time.monotonic() + TASK_TIMEOUT_SECONDS
    detail: dict[str, Any] = {}
    while time.monotonic() < deadline:
        detail = await _get(client, f"/api/v1/workspaces/{ws}/tasks/{task['id']}")
        if detail["task"]["state"] in ("completed", "failed", "cancelled"):
            return detail
        await asyncio.sleep(0.5)
    pytest.fail(f"task {task['id']} did not finish in {TASK_TIMEOUT_SECONDS}s: {detail}")


async def test_cli_denied_without_grant_and_outside_command_scope(
    owner: tuple[httpx.AsyncClient, str],
) -> None:
    client, ws = owner
    tag = uuid4().hex[:8]
    created = await _post(
        client,
        f"/api/v1/workspaces/{ws}/connections",
        {
            "connector_type": "cli",
            "name": f"P6S CLI {tag}",
            "auth_type": "none",
            "credentials": {},
            "config": {},
        },
    )
    connection = created["connection"]
    marker = (
        f'[[tool:cli.command.execute {{"connection_id": "{connection["id"]}", '
        f'"command": "rm -rf /workspace"}}]]'
    )

    # Ungranted agent: denied outright (deny-by-default, plan 48.2).
    ungranted = await _make_agent(client, ws, tag, "ungranted")
    detail = await _run_task(client, ws, ungranted["id"], f"No grant {tag}", f"Clean up: {marker}")
    calls = await _get(client, f"/api/v1/workspaces/{ws}/runs/{detail['runs'][0]['id']}/tool-calls")
    assert len(calls) == 1
    assert calls[0]["status"] == "denied"
    assert calls[0]["error_code"] == "no_grant"

    # Agent granted `git *` commands only: `rm -rf` is a scope mismatch.
    scoped = await _make_agent(client, ws, tag, "scoped")
    await _post(
        client,
        f"/api/v1/workspaces/{ws}/agents/{scoped['id']}/grants",
        {
            "capability": "cli.command.execute",
            "scope": {"connection_id": connection["id"], "command": "git *"},
            "effect": "allow",
        },
    )
    detail = await _run_task(
        client, ws, scoped["id"], f"Scope violation {tag}", f"Clean up: {marker}"
    )
    calls = await _get(client, f"/api/v1/workspaces/{ws}/runs/{detail['runs'][0]['id']}/tool-calls")
    assert len(calls) == 1
    assert calls[0]["status"] == "denied"
    assert calls[0]["error_code"] == "scope_mismatch"

    # Both denials land in the append-only audit log.
    audit = await _get(
        client, f"/api/v1/workspaces/{ws}/audit-events", action="tool.call.denied", limit=200
    )
    denied_targets = {event["target_id"] for event in audit["events"]}
    assert calls[0]["id"] in denied_targets
