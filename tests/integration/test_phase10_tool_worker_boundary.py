"""Phase 10 tool-worker boundary acceptance and harness contract tests."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
import yaml  # type: ignore[import-untyped]
from temporalio.api.common.v1 import ActivityType
from temporalio.api.history.v1 import ActivityTaskScheduledEventAttributes, HistoryEvent
from temporalio.api.taskqueue.v1 import TaskQueue
from temporalio.client import Client as TemporalClient

from jhin_api.seed import DEV_OWNER_EMAIL, DEV_OWNER_PASSWORD
from jhin_tools import (
    AGENT_BEFORE_BIND,
    PHASE9_AFTER_MANIFEST,
    TOOL_AFTER_CLAIM,
    TOOL_AFTER_EFFECT,
    TOOL_BEFORE_CLAIM,
    stable_tool_invocation_id,
)

from . import conftest as integration_config
from . import phase10_upgrade_harness as lifecycle
from .phase10_upgrade_harness import (
    EXPECTED_ROOTFUL_SERVICES,
    EXPECTED_ROOTLESS_SERVICES,
    LIVE_SCENARIOS,
    PUBLISHED_ENDPOINTS,
    ComposeAuthority,
    ComposePsError,
    FrozenPhase9Image,
    LiveScenario,
    SandboxArtifact,
    SocketMetadata,
    UpgradeHarness,
    activity_schedule_pairs,
    activity_start_count,
    build_child_environment,
    build_live_pytest_command,
    compose_files_for,
    create_barrier_root,
    execute_one_shot,
    lease_path_for,
    parse_compose_port,
    parse_compose_ps,
    read_authority_lease,
    read_phase9_source_ref,
    sanitized_external_environment,
    select_live_authority,
    validate_daemon_info,
    validate_socket_metadata,
    write_authority_lease,
)


def _scheduled(
    event_id: int,
    activity_name: str,
    queue: str,
) -> SimpleNamespace:
    return SimpleNamespace(
        event_id=event_id,
        activity_task_scheduled_event_attributes=SimpleNamespace(
            activity_type=SimpleNamespace(name=activity_name),
            task_queue=SimpleNamespace(name=queue),
        ),
        activity_task_started_event_attributes=None,
    )


def _started(event_id: int, scheduled_event_id: int) -> SimpleNamespace:
    return SimpleNamespace(
        event_id=event_id,
        activity_task_scheduled_event_attributes=None,
        activity_task_started_event_attributes=SimpleNamespace(
            scheduled_event_id=scheduled_event_id,
        ),
    )


def test_compose_vectors_select_exactly_one_mode_and_upgrade_overlay() -> None:
    assert compose_files_for("rootful") == (
        "compose.yaml",
        "compose.dev.yaml",
        "compose.rootful.yaml",
    )
    assert compose_files_for("rootless", upgrade=True) == (
        "compose.yaml",
        "compose.dev.yaml",
        "compose.rootless.yaml",
        "tests/integration/compose.phase10-upgrade.yaml",
    )
    with pytest.raises(ValueError, match="rootful or rootless"):
        compose_files_for("wrong-gid")


def test_upgrade_overlay_has_four_distinct_frozen_and_current_worker_pairs() -> None:
    overlay = Path("tests/integration/compose.phase10-upgrade.yaml")
    payload = yaml.safe_load(overlay.read_text(encoding="utf-8"))
    services = payload["services"]
    scenarios = {"normal", "approval", "sync", "cleanup"}
    assert set(services) == {
        *(f"phase9-agent-worker-{scenario}" for scenario in scenarios),
        *(f"phase10-agent-worker-{scenario}" for scenario in scenarios),
        *(f"phase10-tool-worker-{scenario}" for scenario in scenarios),
    }
    for scenario in scenarios:
        old = services[f"phase9-agent-worker-{scenario}"]
        assert old["image"] == "${PHASE9_AGENT_IMAGE:?required}"
        assert old["pull_policy"] == "never"
        assert "build" not in old and "ports" not in old
        assert old["environment"]["APP_ENV"] == "test"
        assert old["environment"]["TEMPORAL_NAMESPACE"] == (
            f"${{PHASE10_UPGRADE_NAMESPACE_{scenario.upper()}:?required}}"
        )
        for generation in ("agent", "tool"):
            current = services[f"phase10-{generation}-worker-{scenario}"]
            assert current["build"]["context"] == "."
            assert current["environment"]["APP_ENV"] == "test"
            assert "ports" not in current
        for worker in (
            old,
            services[f"phase10-agent-worker-{scenario}"],
            services[f"phase10-tool-worker-{scenario}"],
        ):
            assert worker["profiles"] == ["phase10-upgrade"]
            assert worker["volumes"][0].endswith(":/run/jhin/test-barriers")


def test_external_environment_scrubs_poison_and_preserves_registry_auth(tmp_path: Path) -> None:
    poisoned = {
        "PATH": os.environ.get("PATH", ""),
        "DOCKER_CONFIG": str(tmp_path / "docker-config"),
        "APP_ENV": "production",
        "COMPOSE_FILE": "foreign.yaml",
        "COMPOSE_PROFILES": "foreign",
        "COMPOSE_PROJECT_NAME": "jhin",
        "COMPOSE_ENV_FILES": "/tmp/hostile.env",
        "DOCKER_CONTEXT": "remote",
        "DOCKER_HOST": "tcp://attacker.invalid:2375",
        "DOCKER_TLS_VERIFY": "1",
        "BUILDX_BUILDER": "cloud",
        "BUILDKIT_HOST": "tcp://builder.invalid:1234",
        "PHASE10_SOCKET_MODE": "rootless",
        "PHASE10_ROOTLESS_DOCKER_SOCKET": "/foreign.sock",
        "SANDBOX_NETWORK": "jhin_sandbox",
        "SANDBOX_DOCKER_GID": "999",
        "JHIN_TEST_CRASH_BARRIER_NAME": "foreign",
        "UNRELATED_SECRET": "must-not-cross-docker-boundary",
    }
    clean = sanitized_external_environment(
        poisoned,
        docker_host="unix:///var/run/docker.sock",
        mode="rootful",
        values={
            "COMPOSE_PROJECT_NAME": "jhin-p10-a1b2c3d4",
            "SANDBOX_NETWORK": "jhin_p10_sandbox_a1b2c3d4",
            "SANDBOX_DOCKER_GID": "123",
        },
    )
    assert clean["DOCKER_CONFIG"] == poisoned["DOCKER_CONFIG"]
    assert clean["DOCKER_HOST"] == "unix:///var/run/docker.sock"
    assert clean["BUILDX_BUILDER"] == "default"
    assert clean["COMPOSE_DISABLE_ENV_FILE"] == "1"
    assert clean["PHASE10_SOCKET_MODE"] == "rootful"
    assert clean["COMPOSE_PROJECT_NAME"] == "jhin-p10-a1b2c3d4"
    assert clean["SANDBOX_NETWORK"] == "jhin_p10_sandbox_a1b2c3d4"
    for forbidden in (
        "APP_ENV",
        "COMPOSE_FILE",
        "COMPOSE_PROFILES",
        "COMPOSE_ENV_FILES",
        "DOCKER_CONTEXT",
        "DOCKER_TLS_VERIFY",
        "BUILDKIT_HOST",
        "PHASE10_ROOTLESS_DOCKER_SOCKET",
        "JHIN_TEST_CRASH_BARRIER_NAME",
    ):
        assert forbidden not in clean
    assert "UNRELATED_SECRET" not in clean


def test_compose_ps_parser_accepts_array_object_and_ndjson() -> None:
    rows = [
        {"Service": name, "State": "running", "Health": "healthy"}
        for name in sorted(EXPECTED_ROOTFUL_SERVICES)
    ]
    for payload in (
        json.dumps(rows),
        "\n".join(json.dumps(row) for row in rows),
    ):
        parsed = parse_compose_ps(payload, EXPECTED_ROOTFUL_SERVICES)
        assert set(parsed) == EXPECTED_ROOTFUL_SERVICES
    parsed_one = parse_compose_ps(
        json.dumps({"Service": "api", "State": "running", "Health": "healthy"}),
        {"api"},
    )
    assert set(parsed_one) == {"api"}
    assert len(EXPECTED_ROOTFUL_SERVICES) == 17
    assert EXPECTED_ROOTFUL_SERVICES | {"rootless-docker-transport"} == EXPECTED_ROOTLESS_SERVICES


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ("", "blank"),
        ("not-json", "malformed"),
        ('{"Service":"api","State":"exited","Health":""}', "not running"),
        ('{"Service":"api","State":"running","Health":""}', "unhealthy"),
        ('{"Service":"api","State":"running","Health":"unhealthy"}', "unhealthy"),
        (
            '{"Service":"api","State":"running","Health":"healthy"}\n'
            '{"Service":"api","State":"running","Health":"healthy"}',
            "duplicate",
        ),
    ],
)
def test_compose_ps_parser_fails_closed(payload: str, message: str) -> None:
    with pytest.raises(ComposePsError, match=message):
        parse_compose_ps(payload, {"api"})


def test_compose_ps_parser_rejects_missing_and_unexpected_services() -> None:
    api = '{"Service":"api","State":"running","Health":"healthy"}'
    with pytest.raises(ComposePsError, match="missing"):
        parse_compose_ps(api, {"api", "web"})
    with pytest.raises(ComposePsError, match="unexpected"):
        parse_compose_ps(api, set())


@pytest.mark.parametrize(
    ("output", "port"),
    [
        ("127.0.0.1:49152\n", 49152),
        ("0.0.0.0:49153\n", 49153),
        ("[::1]:49154\n", 49154),
    ],
)
def test_compose_port_parser_returns_docker_allocated_port(output: str, port: int) -> None:
    assert parse_compose_port(output) == port


@pytest.mark.parametrize("output", ["", "127.0.0.1:0", "49152", "a:1\nb:2\n"])
def test_compose_port_parser_rejects_ambiguous_or_unallocated_output(output: str) -> None:
    with pytest.raises(ValueError, match="published port"):
        parse_compose_port(output)


def test_barrier_root_is_directly_under_tmp_and_cross_uid_writable() -> None:
    barrier = create_barrier_root("phase10.tool.after_claim.before_effect.v1")
    try:
        assert barrier.root.parent == Path("/tmp")
        assert stat.S_IMODE(barrier.root.lstat().st_mode) == 0o711
        assert stat.S_IMODE(barrier.selected_dir.lstat().st_mode) == 0o1777
        release = barrier.release("018f4d52-8b93-7d41-8ac7-7f190f091111")
        assert release.read_text(encoding="utf-8") == "release\n"
        assert stat.S_IMODE(release.lstat().st_mode) == 0o666
        assert list(barrier.root.iterdir()) == [barrier.selected_dir]
    finally:
        barrier.cleanup()
    assert not barrier.root.exists()


def test_child_barrier_intent_is_fsynced_before_the_first_mkdir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = _authority_for_recorder().with_child_barrier_journal()
    journal = authority.child_barrier_journal_path
    failpoint = "phase10.tool.after_claim.before_effect.v1"
    observed: list[dict[str, str]] = []
    real_mkdir = Path.mkdir
    monkeypatch.setenv("JHIN_PHASE10_CHILD_BARRIER_JOURNAL", str(journal))

    def inspect_before_mkdir(
        path: Path,
        mode: int = 0o777,
        parents: bool = False,
        exist_ok: bool = False,
    ) -> None:
        if path.parent == Path("/tmp") and path.name.startswith("jhin-p10-barrier-"):
            lines = journal.read_text(encoding="utf-8").splitlines()
            assert len(lines) == 1
            record = cast(dict[str, str], json.loads(lines[0]))
            assert record == {"failpoint": failpoint, "root": str(path)}
            observed.append(record)
        real_mkdir(path, mode=mode, parents=parents, exist_ok=exist_ok)

    monkeypatch.setattr(Path, "mkdir", inspect_before_mkdir)
    barrier: Any = None
    try:
        barrier = create_barrier_root(failpoint)
        assert observed == [{"failpoint": failpoint, "root": str(barrier.root)}]
        authority.remove_recovery_paths()
        assert not barrier.root.exists()
    finally:
        if barrier is not None and barrier.root.exists():
            barrier.cleanup()
        if authority.runtime_dir.exists() or authority.barrier_root.exists():
            authority.remove_runtime_paths()


def test_child_barrier_cleanup_reads_every_record_after_a_legal_short_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = _authority_for_recorder().with_child_barrier_journal()
    journal = authority.child_barrier_journal_path
    monkeypatch.setenv("JHIN_PHASE10_CHILD_BARRIER_JOURNAL", str(journal))
    barriers = [
        create_barrier_root("phase10.tool.before_claim.v1"),
        create_barrier_root("phase10.tool.after_effect.before_commit.v1"),
    ]
    journal_identity = journal.lstat().st_dev, journal.lstat().st_ino
    first_record_size = len(journal.read_bytes().splitlines(keepends=True)[0])
    real_read = os.read
    journal_reads = 0

    def short_first_record(descriptor: int, size: int) -> bytes:
        nonlocal journal_reads
        metadata = os.fstat(descriptor)
        if (metadata.st_dev, metadata.st_ino) == journal_identity:
            journal_reads += 1
            if journal_reads == 1:
                return real_read(descriptor, min(size, first_record_size))
        return real_read(descriptor, size)

    monkeypatch.setattr(os, "read", short_first_record)
    try:
        authority.cleanup_child_barriers()
        assert journal_reads >= 3
        assert all(not barrier.root.exists() for barrier in barriers)
    finally:
        for barrier in barriers:
            if barrier.root.exists():
                barrier.cleanup()
        if authority.runtime_dir.exists() or authority.barrier_root.exists():
            authority.remove_runtime_paths()


def test_barrier_arrival_wait_requires_one_fsynced_uuid_marker() -> None:
    barrier = create_barrier_root("phase10.tool.before_claim.v1")
    identity = "018f4d52-8b93-7d41-8ac7-7f190f091111"
    try:
        arrived = barrier.selected_dir / f"{identity}.arrived"
        arrived.write_text("arrived\n", encoding="utf-8")
        arrived.chmod(0o644)
        assert barrier.wait_arrival(timeout=0.1) == identity
        with pytest.raises(RuntimeError, match="unexpected barrier marker"):
            (barrier.selected_dir / "not-an-identity.arrived").write_text(
                "arrived\n", encoding="utf-8"
            )
            barrier.wait_arrival(timeout=0.1)
    finally:
        barrier.cleanup()


def test_barrier_arrival_waits_for_cross_uid_mode_publication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    barrier = create_barrier_root("phase10.tool.before_claim.v1")
    identity = "018f4d52-8b93-7d41-8ac7-7f190f091111"
    arrived = barrier.selected_dir / f"{identity}.arrived"
    arrived.write_bytes(b"arrived\n")
    arrived.chmod(0o600)
    sleeps = 0

    def publish(_duration: float) -> None:
        nonlocal sleeps
        sleeps += 1
        arrived.chmod(0o644)

    monkeypatch.setattr(time, "sleep", publish)
    try:
        assert barrier.wait_arrival(timeout=0.1) == identity
        assert sleeps == 1
    finally:
        barrier.cleanup()


def test_history_parser_preserves_order_and_correlates_retried_starts() -> None:
    events = [
        _scheduled(1, "reason_agent_step", "jhin-agent-queue"),
        _started(2, 1),
        _started(3, 1),
        _scheduled(4, "resolve_advertised_tools", "jhin-tool-queue"),
        _started(5, 4),
        _scheduled(6, "execute_bound_tool", "jhin-tool-queue"),
        _started(7, 6),
    ]
    assert activity_schedule_pairs(events) == [
        ("reason_agent_step", "jhin-agent-queue"),
        ("resolve_advertised_tools", "jhin-tool-queue"),
        ("execute_bound_tool", "jhin-tool-queue"),
    ]
    assert activity_start_count(events, "reason_agent_step") == 2
    assert activity_start_count(events, "execute_bound_tool") == 1


def test_history_parser_ignores_unset_protobuf_activity_attributes() -> None:
    scheduled = HistoryEvent(
        event_id=2,
        activity_task_scheduled_event_attributes=ActivityTaskScheduledEventAttributes(
            activity_type=ActivityType(name="execute_bound_tool"),
            task_queue=TaskQueue(name="jhin-tool-queue"),
        ),
    )

    assert activity_schedule_pairs([HistoryEvent(event_id=1), scheduled]) == [
        ("execute_bound_tool", "jhin-tool-queue")
    ]


def test_phase9_source_ref_is_exact_committed_ancestor() -> None:
    repo = Path(__file__).resolve().parents[2]
    source_ref = read_phase9_source_ref(repo)
    assert source_ref == "6318781b57692bf39f37cd428d73de115d7458e2"


def test_phase9_source_ref_validation_uses_injected_owned_runner() -> None:
    repo = Path(__file__).resolve().parents[2]
    environment = {"PATH": "/usr/bin", "BOUNDARY": "owned"}
    calls: list[tuple[tuple[str, ...], dict[str, Any]]] = []

    def runner(command: tuple[str, ...], **kwargs: Any) -> Any:
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, "", "")

    source_ref = read_phase9_source_ref(
        repo,
        runner=runner,
        environment=environment,
    )
    assert source_ref == "6318781b57692bf39f37cd428d73de115d7458e2"
    assert calls == [
        (
            (
                "git",
                "cat-file",
                "-e",
                "6318781b57692bf39f37cd428d73de115d7458e2^{commit}",
            ),
            {
                "env": environment,
                "cwd": repo,
                "timeout": 30.0,
                "check": False,
                "input_bytes": None,
            },
        ),
        (
            (
                "git",
                "merge-base",
                "--is-ancestor",
                "6318781b57692bf39f37cd428d73de115d7458e2",
                "HEAD",
            ),
            {
                "env": environment,
                "cwd": repo,
                "timeout": 30.0,
                "check": False,
                "input_bytes": None,
            },
        ),
    ]


def test_frozen_phase9_build_streams_exact_archive_to_selected_daemon() -> None:
    authority = _authority_for_recorder()
    source_ref = "6318781b57692bf39f37cd428d73de115d7458e2"
    try:
        tag = authority.phase9_image_tag(source_ref)
        assert tag == f"jhin-phase9-agent-worker:{source_ref[:12]}-{authority.token}"
        assert authority.phase9_archive_command(source_ref) == (
            "git",
            "archive",
            "--format=tar",
            source_ref,
        )
        build = authority.phase9_build_command(source_ref)
        assert build[:3] == authority.docker_command()[:3]
        assert build[-1] == "-"
        assert (build[build.index("--build-arg") : build.index("--build-arg") + 2]) == (
            "--build-arg",
            "SERVICE_PACKAGE=jhin-agent-worker",
        )
        assert build[build.index("-t") + 1] == tag
    finally:
        authority.remove_runtime_paths()


def test_frozen_phase9_archive_and_selected_daemon_build_are_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = _authority_for_recorder()
    source_ref = "6318781b57692bf39f37cd428d73de115d7458e2"
    image_id = "sha256:" + "b" * 64
    archive_bytes = b"phase9-tar-stream"
    runner_calls: list[tuple[tuple[str, ...], float, bytes | None]] = []

    def forbidden_direct_subprocess(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        raise AssertionError("frozen archive escaped the injected runner")

    def bounded_runner(
        command: tuple[str, ...],
        *,
        env: dict[str, str],
        cwd: Path,
        timeout: float,
        check: bool,
        input_bytes: bytes | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        del check
        assert env == authority.environment and cwd == authority.repo
        runner_calls.append((command, timeout, input_bytes))
        if command == authority.phase9_archive_command(source_ref):
            stdout = archive_bytes
        else:
            stdout = image_id.encode() + b"\n" if "inspect" in command else b""
        return subprocess.CompletedProcess(command, 0, stdout, b"")

    monkeypatch.setattr(subprocess, "run", forbidden_direct_subprocess)
    try:
        frozen = authority.build_phase9_agent_image(source_ref, runner=bounded_runner)
        assert frozen.image_id == image_id
        assert runner_calls[0] == (
            authority.phase9_archive_command(source_ref),
            120.0,
            b"",
        )
        build_call = next(call for call in runner_calls if "build" in call[0])
        assert build_call[1:] == (1200.0, archive_bytes)
    finally:
        authority.remove_runtime_paths()


def test_frozen_phase9_build_timeout_attempts_only_exact_tag_cleanup() -> None:
    authority = _authority_for_recorder()
    source_ref = "6318781b57692bf39f37cd428d73de115d7458e2"
    tag = authority.phase9_image_tag(source_ref)
    calls: list[tuple[str, ...]] = []
    removed = False

    def timing_out_runner(
        command: tuple[str, ...],
        **kwargs: Any,
    ) -> subprocess.CompletedProcess[bytes]:
        nonlocal removed
        calls.append(command)
        if command == authority.phase9_archive_command(source_ref):
            return subprocess.CompletedProcess(command, 0, b"phase9-tar-stream", b"")
        if "build" in command:
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        if command[-3:] == ("image", "inspect", tag):
            return subprocess.CompletedProcess(command, 1 if removed else 0, b"[]", b"")
        if command[-3:] == ("image", "rm", tag):
            removed = True
        return subprocess.CompletedProcess(command, 0, b"", b"")

    try:
        with pytest.raises(subprocess.TimeoutExpired):
            authority.build_phase9_agent_image(source_ref, runner=timing_out_runner)
        assert authority.docker_command("image", "rm", tag) in calls
        assert calls.count(authority.docker_command("image", "inspect", tag)) == 2
        assert not any("compose" in call for call in calls)
    finally:
        authority.remove_runtime_paths()


def test_frozen_phase9_build_group_survivor_starts_no_cleanup_command() -> None:
    authority = _authority_for_recorder()
    source_ref = "6318781b57692bf39f37cd428d73de115d7458e2"
    calls: list[tuple[str, ...]] = []

    def surviving_build_runner(
        command: tuple[str, ...],
        **kwargs: Any,
    ) -> subprocess.CompletedProcess[bytes]:
        del kwargs
        calls.append(command)
        if command == authority.phase9_archive_command(source_ref):
            return subprocess.CompletedProcess(command, 0, b"phase9-tar-stream", b"")
        if command == authority.phase9_build_command(source_ref):
            raise lifecycle._OwnedProcessGroupSurvived(4747)
        return subprocess.CompletedProcess(command, 1, b"", b"not found")

    try:
        with pytest.raises(RuntimeError, match=r"4747.*survived"):
            authority.build_phase9_agent_image(source_ref, runner=surviving_build_runner)
        assert calls == [
            authority.phase9_archive_command(source_ref),
            authority.phase9_build_command(source_ref),
        ]
    finally:
        authority.remove_runtime_paths()


def test_frozen_phase9_build_socket_mismatch_makes_zero_daemon_calls_and_reports_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_ref = "6318781b57692bf39f37cd428d73de115d7458e2"
    authority = replace(
        _authority_for_recorder(),
        socket_snapshot=SocketMetadata(
            path=Path("/var/run/docker.sock"),
            inode=111,
            mode=stat.S_IFSOCK | 0o660,
            uid=0,
            gid=123,
        ),
    )
    monkeypatch.setattr(
        SocketMetadata,
        "capture",
        classmethod(
            lambda cls, path: SocketMetadata(
                path=path,
                inode=222,
                mode=stat.S_IFSOCK | 0o660,
                uid=0,
                gid=123,
            )
        ),
    )
    daemon_calls: list[tuple[str, ...]] = []

    def forbidden_runner(command: tuple[str, ...], **kwargs: Any) -> Any:
        del kwargs
        if command == authority.phase9_archive_command(source_ref):
            return subprocess.CompletedProcess(command, 0, b"phase9-tar-stream", b"")
        daemon_calls.append(command)
        raise AssertionError("changed socket reached Docker")

    try:
        with pytest.raises(BaseExceptionGroup, match="unknown"):
            authority.build_phase9_agent_image(source_ref, runner=forbidden_runner)
        assert daemon_calls == []
    finally:
        authority.remove_runtime_paths()


def test_frozen_phase9_failed_build_does_not_cleanup_through_a_replaced_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_ref = "6318781b57692bf39f37cd428d73de115d7458e2"
    original = SocketMetadata(
        path=Path("/var/run/docker.sock"),
        inode=111,
        mode=stat.S_IFSOCK | 0o660,
        uid=0,
        gid=123,
    )
    authority = replace(_authority_for_recorder(), socket_snapshot=original)
    replaced = False

    def capture(cls: type[SocketMetadata], path: Path) -> SocketMetadata:
        del cls
        return replace(original, path=path, inode=222 if replaced else 111)

    monkeypatch.setattr(SocketMetadata, "capture", classmethod(capture))
    daemon_calls: list[tuple[str, ...]] = []

    def failing_build(command: tuple[str, ...], **kwargs: Any) -> Any:
        nonlocal replaced
        if command == authority.phase9_archive_command(source_ref):
            return subprocess.CompletedProcess(command, 0, b"phase9-tar-stream", b"")
        daemon_calls.append(command)
        if "build" in command:
            replaced = True
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        raise AssertionError("changed socket reached cleanup")

    try:
        with pytest.raises(BaseExceptionGroup, match="unknown"):
            authority.build_phase9_agent_image(source_ref, runner=failing_build)
        assert len(daemon_calls) == 1
        assert "build" in daemon_calls[0]
        assert not any("inspect" in command or "rm" in command for command in daemon_calls)
    finally:
        authority.remove_runtime_paths()


def test_upgrade_runtime_owns_four_namespaces_and_direct_tmp_barrier_roots() -> None:
    authority = _authority_for_recorder()
    frozen = FrozenPhase9Image(
        source_ref="6318781b57692bf39f37cd428d73de115d7458e2",
        tag=f"jhin-phase9-agent-worker:6318781b5769-{authority.token}",
        image_id="sha256:" + "a" * 64,
    )
    upgraded = authority.with_upgrade_runtime(frozen)
    try:
        environment = upgraded.environment
        namespaces = {
            environment[f"PHASE10_UPGRADE_NAMESPACE_{scenario.upper()}"]
            for scenario in ("normal", "approval", "sync", "cleanup")
        }
        assert len(namespaces) == 4
        assert all(namespace.startswith(f"jhin-p10-{authority.token}-") for namespace in namespaces)
        assert environment["PHASE9_AGENT_IMAGE"] == frozen.image_id
        assert environment["PHASE10_UPGRADE_PHASE9_TAG"] == frozen.tag
        for scenario in ("normal", "approval", "sync", "cleanup"):
            root = Path(environment[f"PHASE10_UPGRADE_BARRIER_{scenario.upper()}_HOST"])
            assert root.parent == Path("/tmp")
            assert root.is_dir() and not root.is_symlink()
    finally:
        upgraded.remove_runtime_paths()


def test_upgrade_harness_uses_profiled_distinct_services_and_verified_image() -> None:
    authority = _authority_for_recorder()
    frozen = FrozenPhase9Image(
        source_ref="6318781b57692bf39f37cd428d73de115d7458e2",
        tag=f"jhin-phase9-agent-worker:6318781b5769-{authority.token}",
        image_id="sha256:" + "a" * 64,
    )
    upgraded = authority.with_upgrade_runtime(frozen)
    try:
        harness = UpgradeHarness.from_authority(upgraded)
        assert set(harness.scenarios) == {"normal", "approval", "sync", "cleanup"}
        assert len({scenario.namespace for scenario in harness.scenarios.values()}) == 4
        old = harness.worker_up_command("phase9-agent-worker-normal", build=False)
        current = harness.worker_up_command("phase10-tool-worker-normal", build=True)
        assert "--profile" in old and "phase10-upgrade" in old
        assert "--build" not in old and old[-1] == "phase9-agent-worker-normal"
        assert "--build" in current and current[-1] == "phase10-tool-worker-normal"
        tool_group, agent_group = harness.phase10_worker_up_commands()
        assert tool_group[-4:] == tuple(
            f"phase10-tool-worker-{scenario}"
            for scenario in ("normal", "approval", "sync", "cleanup")
        )
        assert agent_group[-4:] == tuple(
            f"phase10-agent-worker-{scenario}"
            for scenario in ("normal", "approval", "sync", "cleanup")
        )
        assert "--build" in tool_group and "--build" in agent_group
        assert upgraded.compose_command("down", upgrade=True)[-1:] == ("down",)
    finally:
        upgraded.remove_runtime_paths()


def _upgrade_harness_for_recorder() -> UpgradeHarness:
    authority = _authority_for_recorder()
    frozen = FrozenPhase9Image(
        source_ref="6318781b57692bf39f37cd428d73de115d7458e2",
        tag=f"jhin-phase9-agent-worker:6318781b5769-{authority.token}",
        image_id="sha256:" + "a" * 64,
    )
    return UpgradeHarness.from_authority(authority.with_upgrade_runtime(frozen))


def test_phase9_sigkill_reaps_exact_container_before_current_tool_first_swap() -> None:
    harness = _upgrade_harness_for_recorder()
    authority = harness.authority
    service = "phase9-agent-worker-normal"
    old_id = "phase9-normal-id"
    service_ps = authority.compose_command(
        "--profile", "phase10-upgrade", "ps", "-q", service, upgrade=True
    )
    service_all_ps = authority.compose_command(
        "--profile", "phase10-upgrade", "ps", "--all", "-q", service, upgrade=True
    )
    service_queries = 0
    old_inspections = 0
    calls: list[tuple[str, ...]] = []

    def recorder(command: tuple[str, ...], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal old_inspections, service_queries
        assert kwargs["env"] == authority.environment
        assert kwargs["cwd"] == authority.repo
        calls.append(command)
        if command == service_ps:
            service_queries += 1
            output = f"{old_id}\n" if service_queries == 1 else ""
            return subprocess.CompletedProcess(command, 0, output, "")
        if command == service_all_ps:
            service_queries += 1
            return subprocess.CompletedProcess(command, 0, "", "")
        if command == authority.docker_command("inspect", old_id):
            old_inspections += 1
            if old_inspections == 1:
                payload = [{"Id": old_id, "State": {"Running": True}}]
                return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")
            if old_inspections == 2:
                payload = [{"Id": old_id, "State": {"Running": False}}]
                return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")
            return subprocess.CompletedProcess(command, 1, "", "not found")
        if "compose" in command and "ps" in command and "-q" in command:
            selected_service = command[-1]
            container_id = f"{selected_service}-id"
            return subprocess.CompletedProcess(command, 0, f"{container_id}\n", "")
        if command[:4] == (*authority.docker_command(), "inspect"):
            container_id = command[-1]
            selected_service = container_id.removesuffix("-id")
            kind = "tool" if "-tool-" in selected_service else "agent"
            scenario = selected_service.rsplit("-", 1)[-1]
            payload = [
                {
                    "Id": container_id,
                    "Image": "sha256:" + ("b" if kind == "tool" else "c") * 64,
                    "Config": {
                        "Env": [
                            "APP_ENV=test",
                            f"TEMPORAL_NAMESPACE={harness.scenarios[scenario].namespace}",
                        ]
                    },
                }
            ]
            return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")
        return subprocess.CompletedProcess(command, 0, "", "")

    try:
        harness.stop_phase9_worker("normal", kill=True, runner=recorder)
        harness.start_phase10_workers(runner=recorder)
        post_kill_inspect = calls.index(authority.docker_command("inspect", old_id), 2)
        exact_remove = calls.index(authority.docker_command("rm", old_id))
        final_id_inspect = calls.index(
            authority.docker_command("inspect", old_id), exact_remove + 1
        )
        service_absence = calls.index(service_all_ps)
        tool_up, agent_up = harness.phase10_worker_up_commands()
        assert post_kill_inspect < exact_remove < final_id_inspect < service_absence
        assert service_absence < calls.index(tool_up) < calls.index(agent_up)
        assert old_inspections == 3 and service_queries == 2
    finally:
        authority.remove_runtime_paths()


def test_phase9_sigkill_refuses_to_remove_a_still_running_old_container() -> None:
    harness = _upgrade_harness_for_recorder()
    authority = harness.authority
    service = "phase9-agent-worker-normal"
    old_id = "phase9-normal-id"
    service_ps = authority.compose_command(
        "--profile", "phase10-upgrade", "ps", "-q", service, upgrade=True
    )
    inspections = 0
    calls: list[tuple[str, ...]] = []

    def recorder(command: tuple[str, ...], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal inspections
        del kwargs
        calls.append(command)
        if command == service_ps:
            return subprocess.CompletedProcess(command, 0, f"{old_id}\n", "")
        if command == authority.docker_command("inspect", old_id):
            inspections += 1
            payload = [{"Id": old_id, "State": {"Running": True}}]
            return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")
        return subprocess.CompletedProcess(command, 0, "", "")

    try:
        with pytest.raises(RuntimeError, match="still running after SIGKILL"):
            harness.stop_phase9_worker("normal", kill=True, runner=recorder)
        assert inspections == 2
        assert authority.docker_command("rm", old_id) not in calls
    finally:
        authority.remove_runtime_paths()


@pytest.mark.parametrize("survivor", ["identity", "service"])
def test_phase9_sigkill_fails_if_the_removed_old_container_survives(survivor: str) -> None:
    harness = _upgrade_harness_for_recorder()
    authority = harness.authority
    service = "phase9-agent-worker-normal"
    old_id = "phase9-normal-id"
    service_ps = authority.compose_command(
        "--profile", "phase10-upgrade", "ps", "-q", service, upgrade=True
    )
    service_all_ps = authority.compose_command(
        "--profile", "phase10-upgrade", "ps", "--all", "-q", service, upgrade=True
    )
    service_queries = 0
    inspections = 0

    def recorder(command: tuple[str, ...], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal inspections, service_queries
        del kwargs
        if command == service_ps:
            service_queries += 1
            output = f"{old_id}\n" if service_queries == 1 else ""
            return subprocess.CompletedProcess(command, 0, output, "")
        if command == service_all_ps:
            service_queries += 1
            output = f"{old_id}\n" if survivor == "service" else ""
            return subprocess.CompletedProcess(command, 0, output, "")
        if command == authority.docker_command("inspect", old_id):
            inspections += 1
            if inspections < 3:
                payload = [{"Id": old_id, "State": {"Running": inspections == 1}}]
                return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")
            return subprocess.CompletedProcess(
                command,
                0 if survivor == "identity" else 1,
                json.dumps([{"Id": old_id}]) if survivor == "identity" else "",
                "",
            )
        return subprocess.CompletedProcess(command, 0, "", "")

    try:
        with pytest.raises(RuntimeError, match="survived exact removal"):
            harness.stop_phase9_worker("normal", kill=True, runner=recorder)
        assert inspections == 3
        assert service_queries == (1 if survivor == "identity" else 2)
    finally:
        authority.remove_runtime_paths()


@pytest.mark.parametrize(
    ("stage", "extra_services"),
    [
        (
            "parked-phase9",
            {f"phase9-agent-worker-{name}" for name in ("normal", "approval", "sync", "cleanup")},
        ),
        ("base-only", set()),
        (
            "current-phase10",
            {
                f"phase10-{kind}-worker-{name}"
                for kind in ("tool", "agent")
                for name in ("normal", "approval", "sync", "cleanup")
            },
        ),
    ],
)
def test_upgrade_stage_topology_is_profiled_healthy_and_exact(
    stage: str,
    extra_services: set[str],
) -> None:
    harness = _upgrade_harness_for_recorder()
    authority = harness.authority
    recorder = _ScriptedRecorder(authority)
    expected = EXPECTED_ROOTFUL_SERVICES | extra_services
    command = authority.compose_command(
        "--profile",
        "phase10-upgrade",
        "ps",
        "--all",
        "--format",
        "json",
        upgrade=True,
    )
    recorder.responses[command] = (
        0,
        json.dumps(
            [
                {"Service": service, "State": "running", "Health": "healthy"}
                for service in sorted(expected)
            ]
        ),
        "",
    )
    try:
        observed = harness.assert_stage_topology(cast(Any, stage), runner=recorder)
        assert set(observed) == expected
        assert recorder.calls == [command]
    finally:
        authority.remove_runtime_paths()


def test_authority_uses_unique_exact_targets_and_all_ephemeral_ports() -> None:
    repo = Path(__file__).resolve().parents[2]
    authority = ComposeAuthority.create(
        repo=repo,
        mode="rootful",
        socket_path=Path("/var/run/docker.sock"),
        socket_gid=123,
        token="a1b2c3d4e5f6",
        source_environment={"PATH": "/usr/bin", "DOCKER_CONFIG": "/tmp/docker-auth"},
    )
    try:
        assert authority.project == "jhin-p10-a1b2c3d4e5f6"
        assert authority.project != "jhin"
        assert authority.sandbox_network == "jhin-p10-sandbox-a1b2c3d4e5f6"
        assert authority.runner_image == "jhin-phase10-sandbox-runner:a1b2c3d4e5f6"
        assert authority.sandbox_image == "jhin-phase10-sandbox:a1b2c3d4e5f6"
        assert authority.runtime_dir.parent == Path("/tmp")
        assert authority.barrier_root.parent == Path("/tmp")
        assert authority.environment["WEB_PORT"] == "127.0.0.1:0"
        assert authority.environment["API_PORT"] == "127.0.0.1:0"
        assert len(PUBLISHED_ENDPOINTS) == 14
        for variable, _service, _container_port in PUBLISHED_ENDPOINTS:
            if variable not in {"WEB_PORT", "API_PORT"}:
                assert authority.environment[variable] == "0"
        assert authority.environment["SANDBOX_NETWORK"] == authority.sandbox_network
        assert authority.environment["SANDBOX_RUNNER_IMAGE"] == authority.runner_image
        assert authority.environment["SANDBOX_DEFAULT_IMAGE"] == authority.sandbox_image
        assert authority.environment["MASTER_KEY_FILE_HOST"] == str(authority.master_key_path)
        assert authority.environment["DOCKER_CONFIG"] == "/tmp/docker-auth"
    finally:
        authority.remove_runtime_paths()


def test_authority_commands_pin_daemon_project_and_file_vector() -> None:
    repo = Path(__file__).resolve().parents[2]
    authority = ComposeAuthority.create(
        repo=repo,
        mode="rootless",
        socket_path=Path("/run/user/10001/docker.sock"),
        token="001122334455",
        source_environment={"PATH": "/usr/bin"},
    )
    try:
        assert authority.docker_command("info") == (
            authority.docker_executable,
            "--host",
            "unix:///run/user/10001/docker.sock",
            "info",
        )
        assert authority.compose_command("ps", "--all", "--format", "json") == (
            authority.docker_executable,
            "--host",
            "unix:///run/user/10001/docker.sock",
            "compose",
            "-p",
            "jhin-p10-001122334455",
            "-f",
            "compose.yaml",
            "-f",
            "compose.dev.yaml",
            "-f",
            "compose.rootless.yaml",
            "ps",
            "--all",
            "--format",
            "json",
        )
        upgrade = authority.compose_command("config", upgrade=True)
        assert upgrade[-3:] == (
            "-f",
            "tests/integration/compose.phase10-upgrade.yaml",
            "config",
        )
        assert upgrade.count("-f") == 4
    finally:
        authority.remove_runtime_paths()


def test_authority_rejects_predictable_or_reserved_identity() -> None:
    repo = Path(__file__).resolve().parents[2]
    for token in ("", "short", "AABBCCDDEEFF", "00000000/evil"):
        with pytest.raises(ValueError, match="token"):
            ComposeAuthority.create(
                repo=repo,
                mode="rootful",
                socket_path=Path("/var/run/docker.sock"),
                socket_gid=123,
                token=token,
                source_environment={},
            )


def test_socket_metadata_validation_binds_mode_and_exact_gid() -> None:
    rootful = SocketMetadata(
        path=Path("/var/run/docker.sock"),
        inode=123,
        mode=stat.S_IFSOCK | 0o660,
        uid=0,
        gid=998,
    )
    assert validate_socket_metadata(rootful, mode="rootful", expected_gid=998) == rootful
    with pytest.raises(ValueError, match="GID"):
        validate_socket_metadata(rootful, mode="rootful", expected_gid=997)
    with pytest.raises(ValueError, match=r"rootless.*UID 10001"):
        validate_socket_metadata(rootful, mode="rootless")

    rootless = SocketMetadata(
        path=Path("/run/user/10001/docker.sock"),
        inode=456,
        mode=stat.S_IFSOCK | 0o600,
        uid=10001,
        gid=10001,
    )
    assert validate_socket_metadata(rootless, mode="rootless") == rootless
    with pytest.raises(ValueError, match="root-owned"):
        validate_socket_metadata(rootless, mode="rootful", expected_gid=10001)


def test_daemon_info_validation_rejects_mode_confusion_and_weak_rootless_host() -> None:
    rootful = {"SecurityOptions": ["name=seccomp"], "Name": "docker", "CgroupVersion": "2"}
    validate_daemon_info(rootful, mode="rootful")
    with pytest.raises(ValueError, match="rootless"):
        validate_daemon_info(rootful, mode="rootless")

    rootless = {
        "SecurityOptions": ["name=rootless", "name=seccomp"],
        "Name": "rootless",
        "CgroupVersion": "2",
        "CgroupDriver": "systemd",
    }
    validate_daemon_info(rootless, mode="rootless")
    with pytest.raises(ValueError, match="rootless daemon"):
        validate_daemon_info(rootless, mode="rootful")
    with pytest.raises(ValueError, match="systemd"):
        validate_daemon_info({**rootless, "CgroupDriver": "none"}, mode="rootless")


class _Recorder:
    def __init__(self, authority: ComposeAuthority) -> None:
        self.authority = authority
        self.calls: list[tuple[str, ...]] = []
        self.ps_output = ""
        self.ports: dict[tuple[str, str], int] = {}

    def __call__(
        self,
        command: tuple[str, ...],
        *,
        env: dict[str, str],
        cwd: Path,
        timeout: float,
        check: bool,
        input_bytes: bytes | None = None,
    ) -> subprocess.CompletedProcess[str]:
        del timeout, check, input_bytes
        assert cwd == self.authority.repo
        assert env == self.authority.environment
        self.calls.append(command)
        if "phase10-key-install" in " ".join(command):
            self.authority.master_key_path.write_text("test-key-material\n", encoding="utf-8")
            self.authority.master_key_path.chmod(0o400)
        if "ps" in command:
            ps_index = command.index("ps")
            if command[ps_index : ps_index + 4] == (
                "ps",
                "--all",
                "--format",
                "json",
            ):
                output = self.ps_output
                if len(command) > ps_index + 4:
                    service = command[ps_index + 4]
                    rows = json.loads(output)
                    output = json.dumps([row for row in rows if row.get("Service") == service])
                return subprocess.CompletedProcess(command, 0, output, "")
        if "port" in command:
            index = command.index("port")
            key = (command[index + 1], command[index + 2])
            port = self.ports[key]
            return subprocess.CompletedProcess(command, 0, f"127.0.0.1:{port}\n", "")
        return subprocess.CompletedProcess(command, 0, "", "")


def _authority_for_recorder() -> ComposeAuthority:
    return ComposeAuthority.create(
        repo=Path(__file__).resolve().parents[2],
        mode="rootful",
        socket_path=Path("/var/run/docker.sock"),
        socket_gid=123,
        token="deadc0de1234",
        source_environment={"PATH": "/usr/bin"},
    )


def test_key_install_uses_selected_daemon_without_exposing_material() -> None:
    authority = _authority_for_recorder()
    recorder = _Recorder(authority)
    try:
        authority.install_master_key(runner=recorder)
        assert stat.S_IMODE(authority.master_key_path.lstat().st_mode) == 0o400
        assert len(recorder.calls) == 1
        command = recorder.calls[0]
        assert command[:3] == authority.docker_command()[:3]
        joined = " ".join(command)
        assert "phase10-key-install" in joined
        assert authority.runner_image in command
        assert "secrets.token_bytes(32)" in joined
        assert "test-key-material" not in joined
        assert "--user" in command and "0:0" in command
    finally:
        authority.remove_runtime_paths()


def test_build_order_and_readiness_use_exact_authority() -> None:
    authority = _authority_for_recorder()
    recorder = _Recorder(authority)
    recorder.ps_output = json.dumps(
        [
            {"Service": name, "State": "running", "Health": "healthy"}
            for name in sorted(EXPECTED_ROOTFUL_SERVICES)
        ]
    )
    try:
        authority.build_required_images(runner=recorder)
        authority.assert_ready(runner=recorder)
        commands = recorder.calls
        assert commands[0][-2:] == ("build", "sandbox-runner")
        assert commands[1][-4:] == ("--profile", "build", "build", "sandbox-image")
        assert "--profile" in commands[1]
        assert commands[-1][-4:] == ("ps", "--all", "--format", "json")
        assert all(command[:3] == authority.docker_command()[:3] for command in commands)
    finally:
        authority.remove_runtime_paths()


def test_bounded_readiness_retries_starting_service_until_exact_topology_is_healthy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = _authority_for_recorder()
    command = authority.compose_command("ps", "--all", "--format", "json")
    starting = [
        {
            "Service": name,
            "State": "running",
            "Health": "starting" if name == "event-worker" else "healthy",
        }
        for name in sorted(EXPECTED_ROOTFUL_SERVICES)
    ]
    healthy = [
        {"Service": name, "State": "running", "Health": "healthy"}
        for name in sorted(EXPECTED_ROOTFUL_SERVICES)
    ]
    outputs = iter((json.dumps(starting), json.dumps(healthy)))
    calls: list[tuple[str, ...]] = []
    sleeps: list[float] = []

    def runner(command_value: tuple[str, ...], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert command_value == command
        assert kwargs["env"] == authority.environment
        assert kwargs["cwd"] == authority.repo
        calls.append(command_value)
        return subprocess.CompletedProcess(command_value, 0, next(outputs), "")

    monotonic = iter((0.0, 0.0))
    monkeypatch.setattr(time, "monotonic", lambda: next(monotonic))
    monkeypatch.setattr(time, "sleep", sleeps.append)
    try:
        observed = authority.wait_ready(
            runner=runner,
            timeout_seconds=5.0,
            interval_seconds=1.0,
        )
        assert set(observed) == EXPECTED_ROOTFUL_SERVICES
        assert calls == [command, command]
        assert sleeps == [1.0]
    finally:
        authority.remove_runtime_paths()


def test_stack_diagnostics_are_bounded_and_secret_redacted(
    capsys: pytest.CaptureFixture[str],
) -> None:
    authority = _authority_for_recorder()
    recorder = _ScriptedRecorder(authority)
    runner_token = authority.environment["SANDBOX_RUNNER_TOKEN"]
    ps = authority.compose_command("ps", "--all", "--format", "json")
    logs = authority.compose_command("logs", "--no-color", "--tail", "100")
    recorder.responses[ps] = (0, f"event-worker starting {runner_token}\n", "")
    recorder.responses[logs] = (0, f"event-worker crashed {runner_token}\n", "")
    try:
        authority.emit_stack_diagnostics(runner=recorder)
        emitted = capsys.readouterr().err
        assert runner_token not in emitted
        assert emitted.count("--- live stack-ps stdout (redacted tail) ---") == 1
        assert emitted.count("--- live stack-logs stdout (redacted tail) ---") == 1
        assert emitted.count("event-worker starting") == 1
        assert emitted.count("event-worker crashed") == 1
        assert recorder.calls == [ps, logs]
    finally:
        authority.remove_runtime_paths()


def test_bounded_readiness_timeout_preserves_starting_error_and_emits_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    authority = _authority_for_recorder()
    ps = authority.compose_command("ps", "--all", "--format", "json")
    logs = authority.compose_command("logs", "--no-color", "--tail", "100")
    starting = json.dumps(
        [
            {
                "Service": name,
                "State": "running",
                "Health": "starting" if name == "event-worker" else "healthy",
            }
            for name in sorted(EXPECTED_ROOTFUL_SERVICES)
        ]
    )
    calls: list[tuple[str, ...]] = []

    def runner(command: tuple[str, ...], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        del kwargs
        calls.append(command)
        if command == ps:
            return subprocess.CompletedProcess(command, 0, starting, "")
        assert command == logs
        return subprocess.CompletedProcess(command, 0, "event-worker restart detail\n", "")

    monotonic = iter((0.0, 6.0))
    monkeypatch.setattr(time, "monotonic", lambda: next(monotonic))
    monkeypatch.setattr(
        time,
        "sleep",
        lambda _seconds: pytest.fail("expired readiness must not sleep"),
    )
    try:
        with pytest.raises(lifecycle.ComposeStartingError, match="event-worker"):
            authority.wait_ready(
                runner=runner,
                timeout_seconds=5.0,
                interval_seconds=1.0,
            )
        assert calls == [ps, ps, logs]
        assert "event-worker restart detail" in capsys.readouterr().err
    finally:
        authority.remove_runtime_paths()


@pytest.mark.parametrize(
    "payload",
    (
        "not-json",
        json.dumps(
            [
                {"Service": name, "State": "running", "Health": "healthy"}
                for name in sorted(EXPECTED_ROOTFUL_SERVICES - {"event-worker"})
            ]
        ),
        json.dumps(
            [
                {
                    "Service": name,
                    "State": "exited" if name == "event-worker" else "running",
                    "Health": "unhealthy" if name == "event-worker" else "healthy",
                }
                for name in sorted(EXPECTED_ROOTFUL_SERVICES)
            ]
        ),
        json.dumps(
            [
                {
                    "Service": name,
                    "State": "running",
                    "Health": (
                        "starting"
                        if name == "event-worker"
                        else "unhealthy"
                        if name == "tool-worker"
                        else "healthy"
                    ),
                }
                for name in sorted(EXPECTED_ROOTFUL_SERVICES)
            ]
        ),
    ),
)
def test_bounded_readiness_fails_immediately_on_nonstarting_topology_errors(
    monkeypatch: pytest.MonkeyPatch,
    payload: str,
) -> None:
    authority = _authority_for_recorder()
    ps = authority.compose_command("ps", "--all", "--format", "json")
    calls: list[tuple[str, ...]] = []

    def runner(command: tuple[str, ...], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        del kwargs
        calls.append(command)
        assert command == ps
        return subprocess.CompletedProcess(command, 0, payload, "")

    monkeypatch.setattr(
        time,
        "sleep",
        lambda _seconds: pytest.fail("nonstarting errors must not be retried"),
    )
    try:
        with pytest.raises(ComposePsError):
            authority.wait_ready(runner=runner)
        assert calls == [ps]
    finally:
        authority.remove_runtime_paths()


@pytest.mark.parametrize(
    ("mode", "service"),
    (
        ("rootful", "sandbox-runner"),
        ("rootless", "rootless-docker-transport"),
    ),
)
def test_bounded_readiness_applies_to_required_socket_boundary_services(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    service: str,
) -> None:
    authority = ComposeAuthority.create(
        repo=Path(__file__).resolve().parents[2],
        mode=mode,
        socket_path=(
            Path("/var/run/docker.sock")
            if mode == "rootful"
            else Path("/run/user/10001/docker.sock")
        ),
        socket_gid=123 if mode == "rootful" else None,
        token="deadc0de1234",
        source_environment={"PATH": "/usr/bin"},
    )
    command = authority.compose_command("ps", "--all", "--format", "json", service)
    outputs = iter(
        (
            json.dumps([{"Service": service, "State": "running", "Health": "starting"}]),
            json.dumps([{"Service": service, "State": "running", "Health": "healthy"}]),
        )
    )
    calls: list[tuple[str, ...]] = []

    def runner(command_value: tuple[str, ...], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        del kwargs
        calls.append(command_value)
        assert command_value == command
        return subprocess.CompletedProcess(command_value, 0, next(outputs), "")

    monotonic = iter((0.0, 0.0))
    monkeypatch.setattr(time, "monotonic", lambda: next(monotonic))
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    try:
        observed = authority.wait_ready(
            runner=runner,
            expected_services={service},
            timeout_seconds=5.0,
            interval_seconds=1.0,
        )
        assert set(observed) == {service}
        assert calls == [command, command]
    finally:
        authority.remove_runtime_paths()


def test_endpoint_discovery_resolves_all_fourteen_ports_without_env_drift() -> None:
    authority = _authority_for_recorder()
    recorder = _Recorder(authority)
    recorder.ports = {
        (service, str(container_port)): 49152 + index
        for index, (_variable, service, container_port) in enumerate(PUBLISHED_ENDPOINTS)
    }
    try:
        endpoints = authority.resolve_published_ports(runner=recorder)
        assert len(endpoints) == 14
        assert endpoints["API_PORT"] == 49153
        assert endpoints["TEMPORAL_UI_DEV_PORT"] == 49165
        assert authority.environment["POSTGRES_DEV_PORT"] == "0"
        assert len(recorder.calls) == 14
        assert all("port" in command for command in recorder.calls)
    finally:
        authority.remove_runtime_paths()


class _ScriptedRecorder(_Recorder):
    def __init__(self, authority: ComposeAuthority) -> None:
        super().__init__(authority)
        self.responses: dict[tuple[str, ...], tuple[int, str, str]] = {}
        self.removed_images: set[str] = set()

    def __call__(
        self,
        command: tuple[str, ...],
        *,
        env: dict[str, str],
        cwd: Path,
        timeout: float,
        check: bool,
        input_bytes: bytes | None = None,
    ) -> subprocess.CompletedProcess[str]:
        del timeout, input_bytes
        assert cwd == self.authority.repo
        assert env == self.authority.environment
        self.calls.append(command)
        response = self.responses.get(command)
        if (
            response is None
            and command[-3:-1] == ("image", "inspect")
            and command[-1] in self.removed_images
        ):
            response = (1, "", "not found")
        returncode, stdout, stderr = response if response is not None else (0, "", "")
        if returncode == 0 and command[-3:-1] == ("image", "rm"):
            self.removed_images.add(command[-1])
        if check and returncode:
            raise subprocess.CalledProcessError(returncode, command, stdout, stderr)
        return subprocess.CompletedProcess(command, returncode, stdout, stderr)


def test_clean_daemon_preflight_checks_only_exact_reserved_authorities() -> None:
    authority = _authority_for_recorder()
    recorder = _ScriptedRecorder(authority)
    network = authority.docker_command("network", "inspect", "jhin_sandbox")
    recorder.responses[network] = (1, "", "not found")
    try:
        authority.assert_no_preexisting_shared_resources(runner=recorder)
        joined = [" ".join(command) for command in recorder.calls]
        assert any("com.docker.compose.project=jhin" in command for command in joined)
        assert any("label=jhin.sandbox.job" in command for command in joined)
        assert any("label=jhin.sandbox.workspace" in command for command in joined)
        assert any(command.endswith("network inspect jhin_sandbox") for command in joined)
        assert not any("jhin*" in command for command in joined)
    finally:
        authority.remove_runtime_paths()


@pytest.mark.parametrize(
    "resource",
    ["project", "project-volume", "project-network", "job", "volume", "network"],
)
def test_clean_daemon_preflight_fails_closed_on_any_reserved_resource(resource: str) -> None:
    authority = _authority_for_recorder()
    recorder = _ScriptedRecorder(authority)
    project = authority.docker_command(
        "ps", "-aq", "--filter", "label=com.docker.compose.project=jhin"
    )
    project_volume = authority.docker_command(
        "volume", "ls", "-q", "--filter", "label=com.docker.compose.project=jhin"
    )
    project_network = authority.docker_command(
        "network", "ls", "-q", "--filter", "label=com.docker.compose.project=jhin"
    )
    job = authority.docker_command("ps", "-aq", "--filter", "label=jhin.sandbox.job")
    volume = authority.docker_command(
        "volume", "ls", "-q", "--filter", "label=jhin.sandbox.workspace"
    )
    network = authority.docker_command("network", "inspect", "jhin_sandbox")
    recorder.responses[network] = (1, "", "not found")
    if resource == "project":
        recorder.responses[project] = (0, "foreign-container\n", "")
    elif resource == "project-volume":
        recorder.responses[project_volume] = (0, "foreign-project-volume\n", "")
    elif resource == "project-network":
        recorder.responses[project_network] = (0, "foreign-project-network\n", "")
    elif resource == "job":
        recorder.responses[job] = (0, "foreign-job\n", "")
    elif resource == "volume":
        recorder.responses[volume] = (0, "foreign-volume\n", "")
    else:
        recorder.responses[network] = (0, "{}\n", "")
    try:
        with pytest.raises(RuntimeError, match="pre-existing"):
            authority.assert_no_preexisting_shared_resources(runner=recorder)
    finally:
        authority.remove_runtime_paths()


def test_cleanup_uses_exact_project_vector_labels_network_and_images() -> None:
    authority = _authority_for_recorder()
    recorder = _ScriptedRecorder(authority)
    network = authority.docker_command("network", "inspect", authority.sandbox_network)
    ordinary_network = authority.docker_command("network", "inspect", "jhin_sandbox")
    recorder.responses[network] = (1, "", "not found")
    recorder.responses[ordinary_network] = (1, "", "not found")
    authority.down_and_cleanup(runner=recorder)
    joined = [" ".join(command) for command in recorder.calls]
    assert any(
        " compose " in f" {command} "
        and f" -p {authority.project} " in f" {command} "
        and command.endswith("down -v --remove-orphans --rmi local")
        for command in joined
    )
    assert any(f"label=jhin.phase10.invocation={authority.token}" in command for command in joined)
    assert any(
        command.endswith(f"network inspect {authority.sandbox_network}") for command in joined
    )
    assert {command.rsplit(" ", 1)[-1] for command in joined if " image rm " in f" {command} "} == {
        authority.runner_image,
        authority.sandbox_image,
        *authority.compose_auto_image_tags(),
    }
    assert not authority.runtime_dir.exists()
    assert not authority.barrier_root.exists()


@pytest.mark.parametrize("upgrade", [False, True])
def test_cleanup_exhausts_every_base_and_upgrade_compose_auto_image(upgrade: bool) -> None:
    authority = _authority_for_recorder()
    if upgrade:
        frozen = FrozenPhase9Image(
            source_ref="6318781b57692bf39f37cd428d73de115d7458e2",
            tag=authority.phase9_image_tag("6318781b57692bf39f37cd428d73de115d7458e2"),
            image_id="sha256:" + "a" * 64,
        )
        authority = authority.with_upgrade_runtime(frozen)
    recorder = _ScriptedRecorder(authority)
    recorder.responses[
        authority.docker_command("network", "inspect", authority.sandbox_network)
    ] = (1, "", "not found")
    recorder.responses[authority.docker_command("network", "inspect", "jhin_sandbox")] = (
        1,
        "",
        "not found",
    )
    base_services = {
        "web",
        "api",
        "workflow-worker",
        "agent-worker",
        "tool-worker",
        "event-worker",
        "fake-provider",
        "fake-github",
        "fake-linear",
        "fake-vercel",
        "fake-supabase",
    }
    upgrade_services = {
        f"phase10-{kind}-worker-{scenario}"
        for kind in ("agent", "tool")
        for scenario in ("normal", "approval", "sync", "cleanup")
    }
    expected = {
        f"{authority.project}-{service}"
        for service in base_services | (upgrade_services if upgrade else set())
    }
    try:
        assert set(authority.compose_auto_image_tags(upgrade=upgrade)) == expected
        authority.down_and_cleanup(runner=recorder, upgrade=upgrade)
        down = authority.compose_command(
            *(("--profile", "phase10-upgrade") if upgrade else ()),
            "down",
            "-v",
            "--remove-orphans",
            "--rmi",
            "local",
            upgrade=upgrade,
        )
        assert down in recorder.calls
        for tag in expected:
            assert authority.docker_command("image", "rm", tag) in recorder.calls
            assert recorder.calls.count(authority.docker_command("image", "inspect", tag)) == 3
    finally:
        if authority.runtime_dir.exists() or authority.barrier_root.exists():
            authority.remove_runtime_paths()


@pytest.mark.parametrize("upgrade", [False, True])
def test_cleanup_reports_a_surviving_base_or_upgrade_compose_auto_image(upgrade: bool) -> None:
    authority = _authority_for_recorder()
    if upgrade:
        frozen = FrozenPhase9Image(
            source_ref="6318781b57692bf39f37cd428d73de115d7458e2",
            tag=authority.phase9_image_tag("6318781b57692bf39f37cd428d73de115d7458e2"),
            image_id="sha256:" + "a" * 64,
        )
        authority = authority.with_upgrade_runtime(frozen)
    recorder = _ScriptedRecorder(authority)
    recorder.responses[
        authority.docker_command("network", "inspect", authority.sandbox_network)
    ] = (1, "", "not found")
    recorder.responses[authority.docker_command("network", "inspect", "jhin_sandbox")] = (
        1,
        "",
        "not found",
    )
    service = "phase10-agent-worker-normal" if upgrade else "api"
    survivor = f"{authority.project}-{service}"
    inspect = authority.docker_command("image", "inspect", survivor)
    recorder.responses[inspect] = (0, "", "")
    try:
        with pytest.raises(BaseExceptionGroup, match="cleanup invariants") as captured:
            authority.down_and_cleanup(runner=recorder, upgrade=upgrade)
        assert "exact image tag remains" in " ".join(
            str(error) for error in captured.value.exceptions
        )
        assert recorder.calls.count(inspect) == 3
        assert recorder.calls.count(authority.docker_command("image", "rm", survivor)) == 2
    finally:
        if authority.runtime_dir.exists() or authority.barrier_root.exists():
            authority.remove_runtime_paths()


def test_cleanup_inventory_is_database_bound_and_uses_exact_resource_labels() -> None:
    authority = _authority_for_recorder()
    recorder = _ScriptedRecorder(authority)
    postgres = authority.compose_command("ps", "-q", "postgres")
    schema_probe = authority.compose_command(
        "exec",
        "-T",
        "postgres",
        "psql",
        "-U",
        "jhin",
        "-d",
        "jhin",
        "-At",
        "-c",
        "SELECT CASE WHEN to_regclass('public.sandbox_job') IS NULL "
        "THEN 'missing' ELSE 'present' END",
    )
    query = authority.compose_command(
        "exec",
        "-T",
        "postgres",
        "psql",
        "-U",
        "jhin",
        "-d",
        "jhin",
        "-At",
        "-F",
        "|",
        "-c",
        "SELECT id::text, run_id::text FROM sandbox_job ORDER BY id",
    )
    job_id = "018f4d52-8b93-7d41-8ac7-7f190f091110"
    run_id = "018f4d52-8b93-7d41-8ac7-7f190f091111"
    recorder.responses[postgres] = (0, "postgres-id\n", "")
    recorder.responses[schema_probe] = (0, "present\n", "")
    recorder.responses[query] = (0, f"{job_id}|{run_id}\n", "")
    try:
        inventory = authority.snapshot_sandbox_artifacts(runner=recorder)
        assert inventory == (SandboxArtifact(job_id=job_id, run_id=run_id),)
        assert authority.sandbox_artifact_filters(inventory) == (
            ("container", f"jhin.sandbox.job={job_id}"),
            ("volume", f"jhin.sandbox.workspace=run-{run_id}"),
        )
    finally:
        authority.remove_runtime_paths()


def test_cleanup_inventory_treats_an_unmigrated_database_as_empty() -> None:
    authority = _authority_for_recorder()
    recorder = _ScriptedRecorder(authority)
    postgres = authority.compose_command("ps", "-q", "postgres")
    schema_probe = authority.compose_command(
        "exec",
        "-T",
        "postgres",
        "psql",
        "-U",
        "jhin",
        "-d",
        "jhin",
        "-At",
        "-c",
        "SELECT CASE WHEN to_regclass('public.sandbox_job') IS NULL "
        "THEN 'missing' ELSE 'present' END",
    )
    recorder.responses[postgres] = (0, "postgres-id\n", "")
    recorder.responses[schema_probe] = (0, "missing\n", "")
    try:
        assert authority.snapshot_sandbox_artifacts(runner=recorder) == ()
        assert recorder.calls == [postgres, schema_probe]
    finally:
        authority.remove_runtime_paths()


@pytest.mark.parametrize(
    ("returncode", "output", "message"),
    (
        (1, "", "failed to inspect"),
        (0, "unknown\n", "malformed"),
    ),
)
def test_cleanup_inventory_fails_closed_on_indeterminate_schema_state(
    returncode: int,
    output: str,
    message: str,
) -> None:
    authority = _authority_for_recorder()
    recorder = _ScriptedRecorder(authority)
    postgres = authority.compose_command("ps", "-q", "postgres")
    schema_probe = authority.compose_command(
        "exec",
        "-T",
        "postgres",
        "psql",
        "-U",
        "jhin",
        "-d",
        "jhin",
        "-At",
        "-c",
        "SELECT CASE WHEN to_regclass('public.sandbox_job') IS NULL "
        "THEN 'missing' ELSE 'present' END",
    )
    recorder.responses[postgres] = (0, "postgres-id\n", "")
    recorder.responses[schema_probe] = (returncode, output, "probe error")
    try:
        with pytest.raises(RuntimeError, match=message):
            authority.snapshot_sandbox_artifacts(runner=recorder)
        assert recorder.calls == [postgres, schema_probe]
    finally:
        authority.remove_runtime_paths()


def test_run_event_payload_is_bound_to_exact_run_type_and_step() -> None:
    authority = _authority_for_recorder()
    recorder = _ScriptedRecorder(authority)
    run_id = "018f4d52-8b93-7d41-8ac7-7f190f091111"
    statement = (
        "SELECT payload_json::text FROM run_event "
        f"WHERE run_id = '{run_id}' "
        "AND event_type = 'agent.step.tool_manifest' "
        "AND payload_json ->> 'step' = '0' ORDER BY created_at"
    )
    command = authority.compose_command(
        "exec",
        "-T",
        "postgres",
        "psql",
        "-U",
        "jhin",
        "-d",
        "jhin",
        "-At",
        "-c",
        statement,
    )
    payload = {"step": 0, "manifest": {"count": 1, "calls": []}}
    recorder.responses[command] = (0, json.dumps(payload) + "\n", "")
    try:
        assert (
            authority.run_event_payload(
                run_id,
                event_type="agent.step.tool_manifest",
                step=0,
                runner=recorder,
            )
            == payload
        )
    finally:
        authority.remove_runtime_paths()


def test_direct_runner_jobs_are_durably_inventoried_for_parent_cleanup() -> None:
    authority = _authority_for_recorder()
    first = "phase10-security-deadc0de"
    second = "0123456789abcdef01234567"
    try:
        authority.record_direct_sandbox_job(first)
        authority.record_direct_sandbox_job(second)
        ledger = authority.runtime_dir / "direct-sandbox-jobs.json"
        assert stat.S_IMODE(ledger.lstat().st_mode) == 0o600
        assert authority.direct_sandbox_jobs() == (first, second)
        assert authority.direct_sandbox_artifact_filters() == (
            ("container", f"jhin.sandbox.job={first}"),
            ("container", f"jhin.sandbox.job={second}"),
        )
    finally:
        authority.remove_runtime_paths()


def test_cleanup_attempts_every_exact_auxiliary_before_reraising_invariant() -> None:
    authority = _authority_for_recorder()
    recorder = _ScriptedRecorder(authority)
    aux_list = authority.docker_command(
        "ps",
        "-aq",
        "--filter",
        f"label=jhin.phase10.invocation={authority.token}",
    )
    recorder.responses[aux_list] = (0, "aux-one\naux-two\n", "")
    for container in ("aux-one", "aux-two"):
        inspect = authority.docker_command(
            "inspect", "--format", "{{json .Config.Labels}}", container
        )
        recorder.responses[inspect] = (
            0,
            json.dumps({"jhin.phase10.invocation": authority.token}) + "\n",
            "",
        )
    network = authority.docker_command("network", "inspect", authority.sandbox_network)
    ordinary_network = authority.docker_command("network", "inspect", "jhin_sandbox")
    recorder.responses[network] = (1, "", "not found")
    recorder.responses[ordinary_network] = (1, "", "not found")
    try:
        with pytest.raises(ExceptionGroup, match="cleanup invariants"):
            authority.down_and_cleanup(runner=recorder)
        removals = [command for command in recorder.calls if command[-3:-1] == ("rm", "-f")]
        assert {command[-1] for command in removals} == {"aux-one", "aux-two"}
    finally:
        if authority.runtime_dir.exists() or authority.barrier_root.exists():
            authority.remove_runtime_paths()


def test_cleanup_reinspects_project_volumes_and_networks_before_exact_removal() -> None:
    authority = _authority_for_recorder()
    recorder = _ScriptedRecorder(authority)
    project_label = f"com.docker.compose.project={authority.project}"
    volume_list = authority.docker_command(
        "volume", "ls", "-q", "--filter", f"label={project_label}"
    )
    network_list = authority.docker_command(
        "network", "ls", "-q", "--filter", f"label={project_label}"
    )
    recorder.responses[volume_list] = (0, "owned-volume\n", "")
    recorder.responses[network_list] = (0, "owned-network\n", "")
    recorder.responses[
        authority.docker_command(
            "volume", "inspect", "--format", "{{json .Labels}}", "owned-volume"
        )
    ] = (0, json.dumps({"com.docker.compose.project": authority.project}), "")
    recorder.responses[
        authority.docker_command(
            "network", "inspect", "--format", "{{json .Labels}}", "owned-network"
        )
    ] = (0, json.dumps({"com.docker.compose.project": authority.project}), "")
    recorder.responses[
        authority.docker_command("network", "inspect", authority.sandbox_network)
    ] = (1, "", "not found")
    recorder.responses[authority.docker_command("network", "inspect", "jhin_sandbox")] = (
        1,
        "",
        "not found",
    )
    try:
        with pytest.raises(ExceptionGroup, match="cleanup invariants"):
            authority.down_and_cleanup(runner=recorder)
        assert authority.docker_command("volume", "rm", "owned-volume") in recorder.calls
        assert authority.docker_command("network", "rm", "owned-network") in recorder.calls
        assert recorder.calls.count(volume_list) >= 2
        assert recorder.calls.count(network_list) >= 2
    finally:
        if authority.runtime_dir.exists() or authority.barrier_root.exists():
            authority.remove_runtime_paths()


def test_cleanup_continues_after_one_exact_target_removal_times_out() -> None:
    authority = _authority_for_recorder()
    recorder = _ScriptedRecorder(authority)
    project_label = f"com.docker.compose.project={authority.project}"
    volume_list = authority.docker_command(
        "volume", "ls", "-q", "--filter", f"label={project_label}"
    )
    network_list = authority.docker_command(
        "network", "ls", "-q", "--filter", f"label={project_label}"
    )
    recorder.responses[volume_list] = (0, "first-volume\nsecond-volume\n", "")
    recorder.responses[network_list] = (0, "later-network\n", "")
    for volume in ("first-volume", "second-volume"):
        recorder.responses[
            authority.docker_command("volume", "inspect", "--format", "{{json .Labels}}", volume)
        ] = (0, json.dumps({"com.docker.compose.project": authority.project}), "")
    recorder.responses[
        authority.docker_command(
            "network", "inspect", "--format", "{{json .Labels}}", "later-network"
        )
    ] = (0, json.dumps({"com.docker.compose.project": authority.project}), "")
    recorder.responses[
        authority.docker_command("network", "inspect", authority.sandbox_network)
    ] = (1, "", "not found")
    ordinary_probe = authority.docker_command("network", "inspect", "jhin_sandbox")
    recorder.responses[ordinary_probe] = (1, "", "not found")
    first_remove = authority.docker_command("volume", "rm", "first-volume")

    def one_timeout(
        command: tuple[str, ...],
        **kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        if command == first_remove:
            recorder.calls.append(command)
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        return recorder(command, **kwargs)

    try:
        with pytest.raises(BaseExceptionGroup, match="cleanup invariants"):
            authority.down_and_cleanup(runner=one_timeout)
        assert authority.docker_command("volume", "rm", "second-volume") in recorder.calls
        assert authority.docker_command("network", "rm", "later-network") in recorder.calls
        assert authority.docker_command("image", "rm", authority.runner_image) in recorder.calls
        assert authority.docker_command("image", "rm", authority.sandbox_image) in recorder.calls
        assert ordinary_probe in recorder.calls
    finally:
        if authority.runtime_dir.exists() or authority.barrier_root.exists():
            authority.remove_runtime_paths()


def test_cleanup_continues_to_down_and_exact_sweeps_after_diagnostic_exception() -> None:
    authority = _authority_for_recorder()
    recorder = _ScriptedRecorder(authority)
    network = authority.docker_command("network", "inspect", authority.sandbox_network)
    ordinary_network = authority.docker_command("network", "inspect", "jhin_sandbox")
    recorder.responses[network] = (1, "", "not found")
    recorder.responses[ordinary_network] = (1, "", "not found")

    def diagnostic_failure(
        command: tuple[str, ...],
        **kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        if "logs" in command:
            recorder.calls.append(command)
            raise TimeoutError("diagnostic timeout")
        return recorder(command, **kwargs)

    try:
        with pytest.raises(ExceptionGroup, match="cleanup invariants"):
            authority.down_and_cleanup(runner=diagnostic_failure)
        assert any(
            command[-5:] == ("down", "-v", "--remove-orphans", "--rmi", "local")
            for command in recorder.calls
        )
        assert any(
            f"label=jhin.phase10.invocation={authority.token}" in command
            for call in recorder.calls
            for command in call
        )
    finally:
        if authority.runtime_dir.exists() or authority.barrier_root.exists():
            authority.remove_runtime_paths()


def test_cleanup_preserves_base_exception_evidence_and_still_runs_later_sweeps() -> None:
    authority = _authority_for_recorder()
    recorder = _ScriptedRecorder(authority)
    recorder.responses[
        authority.docker_command("network", "inspect", authority.sandbox_network)
    ] = (1, "", "not found")
    recorder.responses[authority.docker_command("network", "inspect", "jhin_sandbox")] = (
        1,
        "",
        "not found",
    )

    def interrupted_diagnostic(
        command: tuple[str, ...],
        **kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        if "logs" in command:
            recorder.calls.append(command)
            raise KeyboardInterrupt("diagnostic interrupted")
        return recorder(command, **kwargs)

    try:
        with pytest.raises(BaseExceptionGroup) as captured:
            authority.down_and_cleanup(runner=interrupted_diagnostic)
        assert any(isinstance(error, KeyboardInterrupt) for error in captured.value.exceptions)
        assert any(
            command[-5:] == ("down", "-v", "--remove-orphans", "--rmi", "local")
            for command in recorder.calls
        )
        assert any("network" in command and "ls" in command for command in recorder.calls)
    finally:
        if authority.runtime_dir.exists() or authority.barrier_root.exists():
            authority.remove_runtime_paths()


def test_cleanup_network_probe_exception_does_not_skip_images_or_final_preflight() -> None:
    authority = _authority_for_recorder()
    recorder = _ScriptedRecorder(authority)
    sandbox_probe = authority.docker_command("network", "inspect", authority.sandbox_network)
    ordinary_probe = authority.docker_command("network", "inspect", "jhin_sandbox")
    recorder.responses[ordinary_probe] = (1, "", "not found")

    def network_failure(
        command: tuple[str, ...],
        **kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        if command == sandbox_probe:
            recorder.calls.append(command)
            raise TimeoutError("sandbox network inspect timed out")
        return recorder(command, **kwargs)

    try:
        with pytest.raises(BaseExceptionGroup, match="cleanup invariants"):
            authority.down_and_cleanup(runner=network_failure)
        assert (
            authority.docker_command("image", "inspect", authority.runner_image) in recorder.calls
        )
        assert ordinary_probe in recorder.calls
    finally:
        if authority.runtime_dir.exists() or authority.barrier_root.exists():
            authority.remove_runtime_paths()


def test_cleanup_final_rechecks_run_after_indeterminate_initial_queries() -> None:
    authority = _authority_for_recorder()
    recorder = _ScriptedRecorder(authority)
    project_label = f"com.docker.compose.project={authority.project}"
    volume_list = authority.docker_command(
        "volume", "ls", "-q", "--filter", f"label={project_label}"
    )
    runner_inspect = authority.docker_command("image", "inspect", authority.runner_image)
    recorder.responses[
        authority.docker_command("network", "inspect", authority.sandbox_network)
    ] = (1, "", "not found")
    recorder.responses[authority.docker_command("network", "inspect", "jhin_sandbox")] = (
        1,
        "",
        "not found",
    )
    volume_queries = 0
    runner_queries = 0

    def indeterminate_once(
        command: tuple[str, ...],
        **kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        nonlocal volume_queries, runner_queries
        if command == volume_list:
            volume_queries += 1
            if volume_queries == 1:
                recorder.calls.append(command)
                raise TimeoutError("initial volume query timed out")
        if command == runner_inspect:
            runner_queries += 1
            if runner_queries == 1:
                recorder.calls.append(command)
                raise TimeoutError("initial image inspect timed out")
            recorder.responses[command] = (1, "", "not found")
        return recorder(command, **kwargs)

    try:
        with pytest.raises(BaseExceptionGroup, match="cleanup invariants"):
            authority.down_and_cleanup(runner=indeterminate_once)
        assert volume_queries == 3
        assert runner_queries == 3
        assert (
            authority.docker_command("image", "inspect", authority.sandbox_image) in recorder.calls
        )
    finally:
        if authority.runtime_dir.exists() or authority.barrier_root.exists():
            authority.remove_runtime_paths()


def test_cleanup_recovery_removes_survivors_found_after_initial_probe_timeouts() -> None:
    authority = _authority_for_recorder()
    recorder = _ScriptedRecorder(authority)
    project_label = f"com.docker.compose.project={authority.project}"
    volume_list = authority.docker_command(
        "volume", "ls", "-q", "--filter", f"label={project_label}"
    )
    volume_inspect = authority.docker_command(
        "volume", "inspect", "--format", "{{json .Labels}}", "recovered-volume"
    )
    volume_remove = authority.docker_command("volume", "rm", "recovered-volume")
    runner_inspect = authority.docker_command("image", "inspect", authority.runner_image)
    runner_remove = authority.docker_command("image", "rm", authority.runner_image)
    recorder.responses[volume_inspect] = (
        0,
        json.dumps({"com.docker.compose.project": authority.project}),
        "",
    )
    recorder.responses[
        authority.docker_command("network", "inspect", authority.sandbox_network)
    ] = (1, "", "not found")
    recorder.responses[authority.docker_command("network", "inspect", "jhin_sandbox")] = (
        1,
        "",
        "not found",
    )
    recorder.responses[authority.docker_command("image", "inspect", authority.sandbox_image)] = (
        1,
        "",
        "not found",
    )
    volume_queries = 0
    image_queries = 0
    volume_present = True
    image_present = True

    def recover_on_second_probe(
        command: tuple[str, ...],
        **kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        nonlocal volume_queries, image_queries, volume_present, image_present
        if command == volume_list:
            volume_queries += 1
            recorder.calls.append(command)
            if volume_queries == 1:
                raise TimeoutError("initial project volume query timed out")
            stdout = "recovered-volume\n" if volume_present else ""
            return subprocess.CompletedProcess(command, 0, stdout, "")
        if command == volume_remove:
            volume_present = False
        if command == runner_inspect:
            image_queries += 1
            recorder.calls.append(command)
            if image_queries == 1:
                raise TimeoutError("initial exact image inspect timed out")
            return subprocess.CompletedProcess(command, 0 if image_present else 1, "", "")
        if command == runner_remove:
            image_present = False
        return recorder(command, **kwargs)

    try:
        with pytest.raises(BaseExceptionGroup, match="cleanup invariants") as captured:
            authority.down_and_cleanup(runner=recover_on_second_probe)
        messages = " ".join(str(error) for error in captured.value.exceptions)
        assert "initial project volume query timed out" in messages
        assert "initial exact image inspect timed out" in messages
        assert volume_queries == 3 and image_queries == 3
        assert volume_remove in recorder.calls
        assert runner_remove in recorder.calls
        assert volume_present is False and image_present is False
    finally:
        if authority.runtime_dir.exists() or authority.barrier_root.exists():
            authority.remove_runtime_paths()


@pytest.mark.parametrize("resource", ["container", "volume", "network"])
def test_exact_label_recovery_exhausts_every_project_resource_class(resource: str) -> None:
    authority = _authority_for_recorder()
    recorder = _ScriptedRecorder(authority)
    project_label = f"com.docker.compose.project={authority.project}"
    identifier = f"recovered-{resource}"
    if resource == "container":
        list_command = authority.docker_command("ps", "-aq", "--filter", f"label={project_label}")
        inspect_command = authority.docker_command(
            "inspect", "--format", "{{json .Config.Labels}}", identifier
        )
        remove_command = authority.docker_command("rm", "-f", identifier)
    else:
        list_command = authority.docker_command(
            resource, "ls", "-q", "--filter", f"label={project_label}"
        )
        inspect_command = authority.docker_command(
            resource, "inspect", "--format", "{{json .Labels}}", identifier
        )
        remove_command = authority.docker_command(resource, "rm", identifier)
    recorder.responses[inspect_command] = (
        0,
        json.dumps({"com.docker.compose.project": authority.project}),
        "",
    )
    queries = 0
    present = True

    def recover(
        command: tuple[str, ...],
        **kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        nonlocal queries, present
        if command == list_command:
            queries += 1
            recorder.calls.append(command)
            if queries == 1:
                raise TimeoutError(f"initial {resource} query timed out")
            return subprocess.CompletedProcess(
                command,
                0,
                f"{identifier}\n" if present else "",
                "",
            )
        if command == remove_command:
            present = False
        return recorder(command, **kwargs)

    try:
        errors = authority._exhaust_exact_labeled_resources(
            resource=cast(Any, resource),
            label=project_label,
            description=f"project {resource} resources",
            runner=recover,
        )
        assert any(f"initial {resource} query timed out" in str(error) for error in errors)
        assert queries == 3
        assert remove_command in recorder.calls
        assert present is False
    finally:
        authority.remove_runtime_paths()


def test_cleanup_rechecks_each_removed_image_and_fails_on_exact_survivors() -> None:
    authority = _authority_for_recorder()
    recorder = _ScriptedRecorder(authority)
    recorder.responses[
        authority.docker_command("network", "inspect", authority.sandbox_network)
    ] = (1, "", "not found")
    recorder.responses[authority.docker_command("network", "inspect", "jhin_sandbox")] = (
        1,
        "",
        "not found",
    )
    runner_inspect = authority.docker_command("image", "inspect", authority.runner_image)
    sandbox_inspect = authority.docker_command("image", "inspect", authority.sandbox_image)
    recorder.responses[runner_inspect] = (0, "", "")
    recorder.responses[sandbox_inspect] = (0, "", "")
    try:
        with pytest.raises(BaseExceptionGroup, match="cleanup invariants"):
            authority.down_and_cleanup(runner=recorder)
        assert recorder.calls.count(runner_inspect) == 3
        assert recorder.calls.count(sandbox_inspect) == 3
        assert authority.docker_command("image", "rm", authority.runner_image) in recorder.calls
        assert authority.docker_command("image", "rm", authority.sandbox_image) in recorder.calls
    finally:
        if authority.runtime_dir.exists() or authority.barrier_root.exists():
            authority.remove_runtime_paths()


def test_cleanup_socket_authority_loss_makes_zero_daemon_calls_and_retains_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = replace(
        _authority_for_recorder(),
        socket_snapshot=SocketMetadata(
            path=Path("/var/run/docker.sock"),
            inode=111,
            mode=stat.S_IFSOCK | 0o660,
            uid=0,
            gid=123,
        ),
    )
    recorder = _ScriptedRecorder(authority)
    monkeypatch.setattr(
        SocketMetadata,
        "capture",
        classmethod(
            lambda cls, path: SocketMetadata(
                path=path,
                inode=222,
                mode=stat.S_IFSOCK | 0o660,
                uid=0,
                gid=123,
            )
        ),
    )
    try:
        with pytest.raises(ExceptionGroup, match=r"authority lost.*survivors.*unknown"):
            authority.down_and_cleanup(runner=recorder)
        assert recorder.calls == []
        assert authority.runtime_dir.is_dir()
        assert authority.barrier_root.is_dir()
    finally:
        authority.remove_runtime_paths()


def test_authority_lease_round_trip_is_private_and_exact() -> None:
    authority = _authority_for_recorder()
    authority = replace(
        authority,
        socket_snapshot=SocketMetadata(
            path=authority.socket_path,
            inode=123456,
            mode=stat.S_IFSOCK | 0o660,
            uid=0,
            gid=123,
        ),
    )
    authority = authority.with_published_ports(
        {
            variable: 49152 + index
            for index, (variable, _service, _container_port) in enumerate(PUBLISHED_ENDPOINTS)
        }
    )
    lease = Path("/tmp") / f"jhin-p10-test-lease-{uuid.uuid4().hex}.json"
    try:
        write_authority_lease(authority, lease)
        assert stat.S_IMODE(lease.lstat().st_mode) == 0o600
        loaded = read_authority_lease(lease, expected_repo=authority.repo)
        assert loaded == authority
        assert loaded.environment == authority.environment
        assert loaded.published_ports == authority.published_ports
        assert loaded.compose_command("ps") == authority.compose_command("ps")
    finally:
        lease.unlink(missing_ok=True)
        authority.remove_runtime_paths()


def test_initial_authority_lease_retries_short_writes_and_fsyncs_publication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = _authority_for_recorder()
    lease = Path("/tmp") / f"jhin-p10-short-write-{uuid.uuid4().hex}.json"
    real_write = os.write
    real_fsync = os.fsync
    writes: list[int] = []
    fsynced_modes: list[int] = []

    def short_write(descriptor: int, payload: bytes) -> int:
        chunk = payload[:7]
        writes.append(len(chunk))
        return real_write(descriptor, chunk)

    def recording_fsync(descriptor: int) -> None:
        fsynced_modes.append(os.fstat(descriptor).st_mode)
        real_fsync(descriptor)

    monkeypatch.setattr(os, "write", short_write)
    monkeypatch.setattr(os, "fsync", recording_fsync)
    try:
        ownership = write_authority_lease(authority, lease)
        assert ownership.path == lease
        assert ownership.authority_token == authority.token
        assert (ownership.device, ownership.inode) == (
            lease.lstat().st_dev,
            lease.lstat().st_ino,
        )
        assert len(writes) > 1
        assert sum(stat.S_ISDIR(mode) for mode in fsynced_modes) >= 1
        assert read_authority_lease(lease, expected_repo=authority.repo) == authority
        lifecycle._unlink_owned_authority_lease(ownership)
        assert not lease.exists()
    finally:
        lease.unlink(missing_ok=True)
        authority.remove_runtime_paths()


def test_initial_authority_lease_rejects_zero_write_and_removes_only_its_partial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = _authority_for_recorder()
    lease = Path("/tmp") / f"jhin-p10-zero-write-{uuid.uuid4().hex}.json"
    temporary_pattern = f".{lease.name}.*.tmp"
    before = set(Path("/tmp").glob(temporary_pattern))
    monkeypatch.setattr(os, "write", lambda descriptor, payload: 0)
    try:
        with pytest.raises(RuntimeError, match="zero progress"):
            write_authority_lease(authority, lease)
        assert not lease.exists()
        assert set(Path("/tmp").glob(temporary_pattern)) == before
    finally:
        lease.unlink(missing_ok=True)
        authority.remove_runtime_paths()


def _lease_handoff_lifecycle_case(
    *,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    lifecycle_kind: str,
) -> tuple[Path, list[ComposeAuthority], Any, Any]:
    """Build a real active signal lifecycle around fake external boundaries."""
    (tmp_path / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    lease = (
        Path("/tmp") / f"jhin-p10-handoff-{uuid.uuid4().hex}.json"
        if lifecycle_kind == "one-shot"
        else lease_path_for(tmp_path)
    )
    ports = {
        variable: 53300 + index
        for index, (variable, _service, _container_port) in enumerate(PUBLISHED_ENDPOINTS)
    }
    created: list[ComposeAuthority] = []

    def new_authority() -> ComposeAuthority:
        authority = ComposeAuthority.create(
            repo=tmp_path,
            mode="rootful",
            socket_path=Path("/var/run/docker.sock"),
            socket_gid=123,
            token=uuid.uuid4().hex[:12],
            source_environment={"PATH": os.environ.get("PATH", "/usr/bin")},
        )
        created.append(authority)
        return authority

    def fake_start(self: ComposeAuthority, *, runner: Any) -> dict[str, int]:
        del self, runner
        return ports

    def conclusive_cleanup(
        self: ComposeAuthority,
        *,
        runner: Any,
        upgrade: bool = False,
    ) -> None:
        del runner, upgrade
        self.remove_recovery_paths()

    def successful_child(command: tuple[str, ...], **kwargs: Any) -> Any:
        del kwargs
        return subprocess.CompletedProcess(command, 0, "passed\n", "")

    monkeypatch.setattr(ComposeAuthority, "start_stack", fake_start)
    monkeypatch.setattr(ComposeAuthority, "down_and_cleanup", conclusive_cleanup)

    def invoke() -> Any:
        authority = new_authority()
        if lifecycle_kind == "one-shot":
            return execute_one_shot(
                authority,
                scenario=LiveScenario(
                    nodes=("lease-handoff",),
                    expected_tests=1,
                    start_stack=True,
                ),
                lease_path=lease,
                child_runner=successful_child,
            )
        monkeypatch.setattr(
            lifecycle,
            "select_live_authority",
            lambda **kwargs: authority,
        )
        return lifecycle._persistent_up(repo=tmp_path, mode="rootful")

    def rerun_and_recover() -> None:
        result = invoke()
        if lifecycle_kind == "one-shot":
            assert isinstance(result, subprocess.CompletedProcess)
            assert result.returncode == 0
        else:
            assert isinstance(result, ComposeAuthority)
            loaded = read_authority_lease(lease, expected_repo=tmp_path)
            assert loaded.token == result.token
            lifecycle._persistent_down(repo=tmp_path)
        assert not lease.exists()
        assert not created[-1].runtime_dir.exists()
        assert not created[-1].barrier_root.exists()

    return lease, created, invoke, rerun_and_recover


def _assert_failed_handoff_is_fully_removed(
    *,
    lease: Path,
    authority: ComposeAuthority,
) -> None:
    """Reject the unreadable lease-without-runtime state from a lost handoff."""
    assert not lease.exists()
    assert not authority.runtime_dir.exists()
    assert not authority.barrier_root.exists()


@pytest.mark.parametrize("lifecycle_kind", ("one-shot", "persistent"))
def test_active_lifecycle_binds_post_link_ownership_before_pending_signal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    lifecycle_kind: str,
) -> None:
    lease, created, invoke, rerun_and_recover = _lease_handoff_lifecycle_case(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        lifecycle_kind=lifecycle_kind,
    )
    real_link = os.link
    signalled = False

    def signal_after_link(source: Any, destination: Any, **kwargs: Any) -> None:
        nonlocal signalled
        real_link(source, destination, **kwargs)
        if Path(destination) == lease and not signalled:
            signalled = True
            os.kill(os.getpid(), signal.SIGTERM)

    monkeypatch.setattr(os, "link", signal_after_link)
    try:
        with pytest.raises(SystemExit) as raised:
            invoke()
        assert raised.value.code == 128 + signal.SIGTERM
        assert signalled is True
        _assert_failed_handoff_is_fully_removed(lease=lease, authority=created[-1])
        rerun_and_recover()
    finally:
        lease.unlink(missing_ok=True)
        lifecycle.persistent_operation_lock_path(tmp_path).unlink(missing_ok=True)
        for authority in created:
            if authority.runtime_dir.exists() or authority.barrier_root.exists():
                authority.remove_runtime_paths()


@pytest.mark.parametrize("lifecycle_kind", ("one-shot", "persistent"))
def test_active_lifecycle_binds_post_replace_ownership_before_pending_signal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    lifecycle_kind: str,
) -> None:
    lease, created, invoke, rerun_and_recover = _lease_handoff_lifecycle_case(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        lifecycle_kind=lifecycle_kind,
    )
    real_replace = os.replace
    signalled = False

    def signal_after_replace(source: Any, destination: Any, **kwargs: Any) -> None:
        nonlocal signalled
        real_replace(source, destination, **kwargs)
        if Path(destination) == lease and not signalled:
            signalled = True
            os.kill(os.getpid(), signal.SIGTERM)

    monkeypatch.setattr(os, "replace", signal_after_replace)
    try:
        with pytest.raises(SystemExit) as raised:
            invoke()
        assert raised.value.code == 128 + signal.SIGTERM
        assert signalled is True
        _assert_failed_handoff_is_fully_removed(lease=lease, authority=created[-1])
        rerun_and_recover()
    finally:
        lease.unlink(missing_ok=True)
        lifecycle.persistent_operation_lock_path(tmp_path).unlink(missing_ok=True)
        for authority in created:
            if authority.runtime_dir.exists() or authority.barrier_root.exists():
                authority.remove_runtime_paths()


@pytest.mark.parametrize("lifecycle_kind", ("one-shot", "persistent"))
def test_post_replace_fsync_failure_never_loses_new_lease_ownership(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    lifecycle_kind: str,
) -> None:
    lease, created, invoke, rerun_and_recover = _lease_handoff_lifecycle_case(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        lifecycle_kind=lifecycle_kind,
    )
    real_replace = os.replace
    real_fsync_tmp = lifecycle._fsync_tmp_directory
    replaced = False
    failed = False

    def observe_replace(source: Any, destination: Any, **kwargs: Any) -> None:
        nonlocal replaced
        real_replace(source, destination, **kwargs)
        if Path(destination) == lease:
            replaced = True

    def fail_once_after_replace() -> None:
        nonlocal failed
        if replaced and not failed:
            failed = True
            raise OSError("injected post-replace directory fsync failure")
        real_fsync_tmp()

    monkeypatch.setattr(os, "replace", observe_replace)
    monkeypatch.setattr(lifecycle, "_fsync_tmp_directory", fail_once_after_replace)
    try:
        with pytest.raises(OSError, match="post-replace"):
            invoke()
        assert replaced is True
        assert failed is True
        _assert_failed_handoff_is_fully_removed(lease=lease, authority=created[-1])
        rerun_and_recover()
    finally:
        lease.unlink(missing_ok=True)
        lifecycle.persistent_operation_lock_path(tmp_path).unlink(missing_ok=True)
        for authority in created:
            if authority.runtime_dir.exists() or authority.barrier_root.exists():
                authority.remove_runtime_paths()


@pytest.mark.parametrize("lifecycle_kind", ("one-shot", "persistent"))
def test_post_replace_fsync_signal_preserves_direct_lifecycle_exit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    lifecycle_kind: str,
) -> None:
    lease, created, invoke, rerun_and_recover = _lease_handoff_lifecycle_case(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        lifecycle_kind=lifecycle_kind,
    )
    ready = threading.Event()
    stop = threading.Event()

    def unblocked_signal_target() -> None:
        previous = signal.pthread_sigmask(
            signal.SIG_UNBLOCK,
            {signal.SIGINT, signal.SIGTERM},
        )
        ready.set()
        try:
            stop.wait(timeout=10.0)
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous)

    helper = threading.Thread(target=unblocked_signal_target, daemon=True)
    helper.start()
    assert ready.wait(timeout=5.0)
    real_replace = os.replace
    real_fsync_tmp = lifecycle._fsync_tmp_directory
    replaced = False
    signalled = False

    def observe_replace(source: Any, destination: Any, **kwargs: Any) -> None:
        nonlocal replaced
        real_replace(source, destination, **kwargs)
        if Path(destination) == lease:
            replaced = True

    def signal_after_post_replace_fsync() -> None:
        nonlocal signalled
        real_fsync_tmp()
        if replaced and not signalled:
            signalled = True
            os.kill(os.getpid(), signal.SIGTERM)
            # A replay/runtime helper can receive the process signal while the
            # main thread is still returning through the refresh boundary.
            for _index in range(1000):
                pass

    monkeypatch.setattr(os, "replace", observe_replace)
    monkeypatch.setattr(
        lifecycle,
        "_fsync_tmp_directory",
        signal_after_post_replace_fsync,
    )
    try:
        with pytest.raises(SystemExit) as raised:
            invoke()
        assert raised.value.code == 128 + signal.SIGTERM
        assert replaced is True
        assert signalled is True
        _assert_failed_handoff_is_fully_removed(lease=lease, authority=created[-1])
        rerun_and_recover()
    finally:
        stop.set()
        helper.join(timeout=5.0)
        lease.unlink(missing_ok=True)
        lifecycle.persistent_operation_lock_path(tmp_path).unlink(missing_ok=True)
        for authority in created:
            if authority.runtime_dir.exists() or authority.barrier_root.exists():
                authority.remove_runtime_paths()


@pytest.mark.parametrize("handoff", ("link", "replace"))
def test_unblocked_helper_thread_cannot_split_lease_ownership_handoff(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    handoff: str,
) -> None:
    lease, created, invoke, rerun_and_recover = _lease_handoff_lifecycle_case(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        lifecycle_kind="one-shot",
    )
    ready = threading.Event()
    stop = threading.Event()

    def unblocked_signal_target() -> None:
        previous = signal.pthread_sigmask(
            signal.SIG_UNBLOCK,
            {signal.SIGINT, signal.SIGTERM},
        )
        ready.set()
        try:
            stop.wait(timeout=10.0)
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous)

    helper = threading.Thread(target=unblocked_signal_target, daemon=True)
    helper.start()
    assert ready.wait(timeout=5.0)
    signalled = False

    def signal_after_real_handoff(
        real_operation: Any,
        source: Any,
        destination: Any,
        **kwargs: Any,
    ) -> None:
        nonlocal signalled
        real_operation(source, destination, **kwargs)
        if Path(destination) == lease and not signalled:
            signalled = True
            os.kill(os.getpid(), signal.SIGTERM)
            # Force a main-thread bytecode checkpoint while the lifecycle
            # caller still has not received the callee's ownership result.
            for _index in range(1000):
                pass

    if handoff == "link":
        real_link = os.link
        monkeypatch.setattr(
            os,
            "link",
            lambda source, destination, **kwargs: signal_after_real_handoff(
                real_link,
                source,
                destination,
                **kwargs,
            ),
        )
    else:
        real_replace = os.replace
        monkeypatch.setattr(
            os,
            "replace",
            lambda source, destination, **kwargs: signal_after_real_handoff(
                real_replace,
                source,
                destination,
                **kwargs,
            ),
        )
    try:
        with pytest.raises(SystemExit) as raised:
            invoke()
        assert raised.value.code == 128 + signal.SIGTERM
        assert signalled is True
        _assert_failed_handoff_is_fully_removed(lease=lease, authority=created[-1])
        rerun_and_recover()
    finally:
        stop.set()
        helper.join(timeout=5.0)
        lease.unlink(missing_ok=True)
        lifecycle.persistent_operation_lock_path(tmp_path).unlink(missing_ok=True)
        for authority in created:
            if authority.runtime_dir.exists() or authority.barrier_root.exists():
                authority.remove_runtime_paths()


def test_one_shot_does_not_unlink_a_preexisting_unowned_lease() -> None:
    authority = _authority_for_recorder()
    lease = Path("/tmp") / f"jhin-p10-unowned-one-shot-{uuid.uuid4().hex}.json"
    sentinel = b"pre-existing recovery owner\n"
    lease.write_bytes(sentinel)
    lease.chmod(0o600)
    identity = lease.lstat().st_dev, lease.lstat().st_ino
    scenario = LiveScenario(nodes=("unowned-lease",), expected_tests=1, start_stack=False)
    try:
        with pytest.raises(FileExistsError, match="already exists"):
            execute_one_shot(authority, scenario=scenario, lease_path=lease)
        assert lease.read_bytes() == sentinel
        assert (lease.lstat().st_dev, lease.lstat().st_ino) == identity
    finally:
        lease.unlink(missing_ok=True)
        authority.remove_runtime_paths()


def test_persistent_start_does_not_unlink_a_racing_unowned_lease(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    (tmp_path / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    authority = ComposeAuthority.create(
        repo=tmp_path,
        mode="rootful",
        socket_path=Path("/var/run/docker.sock"),
        socket_gid=123,
        token=uuid.uuid4().hex[:12],
        source_environment={"PATH": os.environ.get("PATH", "/usr/bin")},
    )
    lease = lease_path_for(tmp_path)
    sentinel = b"racing recovery owner\n"

    def select_after_racing_lease(**kwargs: Any) -> ComposeAuthority:
        del kwargs
        lease.write_bytes(sentinel)
        lease.chmod(0o600)
        return authority

    monkeypatch.setattr(lifecycle, "select_live_authority", select_after_racing_lease)
    try:
        with pytest.raises(FileExistsError, match="already exists"):
            lifecycle._persistent_up(repo=tmp_path, mode="rootful")
        assert lease.read_bytes() == sentinel
    finally:
        lease.unlink(missing_ok=True)
        authority.remove_runtime_paths()


def test_authority_lease_rejects_symlink_loose_mode_and_identity_tamper() -> None:
    authority = _authority_for_recorder()
    lease = Path("/tmp") / f"jhin-p10-test-lease-{uuid.uuid4().hex}.json"
    target = Path("/tmp") / f"jhin-p10-test-target-{uuid.uuid4().hex}.json"
    try:
        target.write_text("{}", encoding="utf-8")
        lease.symlink_to(target)
        with pytest.raises(ValueError, match="symlink"):
            write_authority_lease(authority, lease)
        lease.unlink()

        write_authority_lease(authority, lease)
        lease.chmod(0o644)
        with pytest.raises(ValueError, match="mode 0600"):
            read_authority_lease(lease, expected_repo=authority.repo)
        lease.chmod(0o600)
        payload: dict[str, Any] = json.loads(lease.read_text(encoding="utf-8"))
        payload["project"] = "jhin"
        lease.write_text(json.dumps(payload), encoding="utf-8")
        lease.chmod(0o600)
        with pytest.raises(ValueError, match="identity"):
            read_authority_lease(lease, expected_repo=authority.repo)
    finally:
        lease.unlink(missing_ok=True)
        target.unlink(missing_ok=True)
        authority.remove_runtime_paths()


def test_conftest_requires_the_exact_full_service_topology() -> None:
    assert integration_config.required_services_for_mode("rootful") == EXPECTED_ROOTFUL_SERVICES
    assert integration_config.required_services_for_mode("rootless") == EXPECTED_ROOTLESS_SERVICES


def test_conftest_has_no_default_or_unleased_compose_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("JHIN_PHASE10_AUTHORITY_LEASE", raising=False)
    monkeypatch.setenv("PHASE10_SOCKET_MODE", "rootful")
    with pytest.raises(RuntimeError, match="authority lease"):
        integration_config.compose_authority()


def test_wrong_gid_scenario_never_requires_a_full_stack() -> None:
    assert integration_config.stack_readiness_required("wrong-gid") is False
    assert integration_config.stack_readiness_required("boundary") is True
    assert integration_config.stack_readiness_required(None) is True


def test_strict_selection_hook_accepts_exact_passes_without_report_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = cast(pytest.Config, SimpleNamespace())
    integration_config.pytest_configure(config)
    report = cast(
        pytest.TestReport,
        SimpleNamespace(when="call", wasxfail=False, skipped=False, passed=True),
    )
    integration_config.pytest_runtest_logreport(report)
    session = cast(
        pytest.Session,
        SimpleNamespace(config=config, exitstatus=pytest.ExitCode.OK),
    )
    monkeypatch.setenv("JHIN_PHASE10_STRICT_SELECTION", "1")
    monkeypatch.setenv("JHIN_PHASE10_EXPECTED_TESTS", "1")
    integration_config.pytest_sessionfinish(session, pytest.ExitCode.OK)
    assert session.exitstatus == pytest.ExitCode.OK


def test_strict_selection_hook_fails_on_skip_or_deselection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = cast(pytest.Config, SimpleNamespace())
    integration_config.pytest_configure(config)
    integration_config.pytest_runtest_logreport(
        cast(
            pytest.TestReport,
            SimpleNamespace(when="call", wasxfail=False, skipped=True, passed=False),
        )
    )
    integration_config.pytest_deselected([cast(pytest.Item, SimpleNamespace(config=config))])
    session = cast(
        pytest.Session,
        SimpleNamespace(config=config, exitstatus=pytest.ExitCode.OK),
    )
    monkeypatch.setenv("JHIN_PHASE10_STRICT_SELECTION", "1")
    monkeypatch.setenv("JHIN_PHASE10_EXPECTED_TESTS", "1")
    integration_config.pytest_sessionfinish(session, pytest.ExitCode.OK)
    assert session.exitstatus == pytest.ExitCode.TESTS_FAILED


def test_strict_selection_hook_counts_fixture_setup_skips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = cast(pytest.Config, SimpleNamespace())
    integration_config.pytest_configure(config)
    integration_config.pytest_runtest_logreport(
        cast(
            pytest.TestReport,
            SimpleNamespace(when="setup", wasxfail=False, skipped=True, passed=False),
        )
    )
    assert integration_config._PHASE10_COUNTS["skipped"] == 1
    session = cast(
        pytest.Session,
        SimpleNamespace(config=config, exitstatus=pytest.ExitCode.OK),
    )
    monkeypatch.setenv("JHIN_PHASE10_STRICT_SELECTION", "1")
    monkeypatch.setenv("JHIN_PHASE10_EXPECTED_TESTS", "1")
    integration_config.pytest_sessionfinish(session, pytest.ExitCode.OK)
    assert session.exitstatus == pytest.ExitCode.TESTS_FAILED


def test_child_environment_publishes_resolved_endpoints_without_compose_port_drift() -> None:
    authority = _authority_for_recorder()
    lease = Path("/tmp") / f"jhin-p10-child-{uuid.uuid4().hex}.json"
    ports = {
        variable: 49152 + index
        for index, (variable, _service, _container_port) in enumerate(PUBLISHED_ENDPOINTS)
    }
    try:
        environment = build_child_environment(
            authority,
            ports=ports,
            lease_path=lease,
            expected_tests=7,
        )
        assert environment["JHIN_API_URL"] == "http://127.0.0.1:49153"
        assert environment["JHIN_TEMPORAL_ADDRESS"] == "127.0.0.1:49164"
        assert environment["JHIN_NATS_URL"] == "nats://127.0.0.1:49162"
        assert environment["SANDBOX_RUNNER_DEV_URL"] == "http://127.0.0.1:49160"
        assert environment["JHIN_POSTGRES_PORT"] == "49161"
        assert environment["POSTGRES_DEV_PORT"] == "0"
        assert environment["JHIN_PHASE9_DB_ADMIN_DSN"].endswith(":49159/supabase_fixture")
        assert environment["JHIN_PHASE10_AUTHORITY_LEASE"] == str(lease)
        assert environment["JHIN_PHASE10_STRICT_SELECTION"] == "1"
        assert environment["JHIN_PHASE10_EXPECTED_TESTS"] == "7"
        assert environment["JHIN_TEST_COMPOSE_PROJECT"] == authority.project
        assert environment["DOCKER_HOST"] == authority.docker_host
    finally:
        authority.remove_runtime_paths()


def test_persistent_lease_path_is_stable_per_repo_and_directly_under_tmp() -> None:
    repo = Path(__file__).resolve().parents[2]
    first = lease_path_for(repo)
    second = lease_path_for(repo)
    assert first == second
    assert first.parent == Path("/tmp")
    assert first.name.startswith("jhin-p10-worktree-")
    assert lease_path_for(repo.parent) != first


def test_persistent_operation_lock_rejects_symlink_and_loose_mode_without_running(
    tmp_path: Path,
) -> None:
    (tmp_path / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    lock = lifecycle.persistent_operation_lock_path(tmp_path)
    target = Path("/tmp") / f"jhin-p10-lock-target-{uuid.uuid4().hex}"
    calls: list[tuple[str, ...]] = []

    def forbidden_runner(command: tuple[str, ...], **kwargs: Any) -> Any:
        del kwargs
        calls.append(command)
        raise AssertionError("lock rejection must happen before any command")

    try:
        target.write_text("foreign\n", encoding="utf-8")
        target.chmod(0o600)
        lock.symlink_to(target)
        with pytest.raises(OSError):
            lifecycle._persistent_compose(
                repo=tmp_path,
                arguments=("run", "--rm", "--no-deps", "api", "jhin-db-migrate"),
                runner=forbidden_runner,
            )
        assert target.read_text(encoding="utf-8") == "foreign\n"
        lock.unlink()

        lock.write_text("loose\n", encoding="utf-8")
        lock.chmod(0o644)
        with pytest.raises(RuntimeError, match="owner-only"):
            lifecycle._persistent_compose(
                repo=tmp_path,
                arguments=("run", "--rm", "--no-deps", "api", "jhin-db-migrate"),
                runner=forbidden_runner,
            )
        assert calls == []
    finally:
        lock.unlink(missing_ok=True)
        target.unlink(missing_ok=True)


def test_live_preflight_binds_socket_daemon_and_reserved_resource_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = _authority_for_recorder()
    recorder = _ScriptedRecorder(authority)
    metadata = SocketMetadata(
        path=authority.socket_path,
        inode=12345,
        mode=stat.S_IFSOCK | 0o660,
        uid=0,
        gid=123,
    )
    monkeypatch.setattr(
        SocketMetadata,
        "capture",
        classmethod(lambda cls, path: metadata),
    )
    info = authority.docker_command("info", "--format", "{{json .}}")
    recorder.responses[info] = (
        0,
        json.dumps({"SecurityOptions": ["name=seccomp"], "CgroupVersion": "2"}),
        "",
    )
    ordinary_network = authority.docker_command("network", "inspect", "jhin_sandbox")
    recorder.responses[ordinary_network] = (1, "", "not found")
    try:
        assert authority.preflight(runner=recorder) == metadata
        assert recorder.calls[0] == info
        assert any("com.docker.compose.project=jhin" in " ".join(call) for call in recorder.calls)
    finally:
        authority.remove_runtime_paths()


def test_start_stack_uses_bounded_build_key_up_migrate_readiness_and_ports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = _authority_for_recorder()
    recorder = _Recorder(authority)
    recorder.ps_output = json.dumps(
        [
            {"Service": name, "State": "running", "Health": "healthy"}
            for name in sorted(EXPECTED_ROOTFUL_SERVICES)
        ]
    )
    recorder.ports = {
        (service, str(container_port)): 50000 + index
        for index, (_variable, service, container_port) in enumerate(PUBLISHED_ENDPOINTS)
    }
    observed: list[str] = []
    monkeypatch.setattr(
        ComposeAuthority,
        "preflight",
        lambda self, runner: observed.append("preflight"),
    )
    monkeypatch.setattr(
        ComposeAuthority,
        "verify_master_key_readability",
        lambda self, runner, services=("api", "agent-worker", "tool-worker"): observed.append(
            "key-readable"
        ),
    )
    try:
        ports = authority.start_stack(runner=recorder)
        assert len(ports) == 14
        assert observed == ["preflight", "key-readable"]
        calls = recorder.calls
        build_runner = next(
            i for i, call in enumerate(calls) if call[-2:] == ("build", "sandbox-runner")
        )
        build_job = next(i for i, call in enumerate(calls) if call[-1:] == ("sandbox-image",))
        key_install = next(
            i for i, call in enumerate(calls) if "phase10-key-install" in " ".join(call)
        )
        up = next(i for i, call in enumerate(calls) if "up" in call and "--wait-timeout" in call)
        migrate = next(i for i, call in enumerate(calls) if call[-1:] == ("jhin-db-migrate",))
        first_port = next(i for i, call in enumerate(calls) if "port" in call)
        assert build_runner < build_job < key_install < up < migrate < first_port
        up_command = calls[up]
        assert up_command[-5:] == ("-d", "--build", "--wait", "--wait-timeout", "300")
        assert all(call[:3] == authority.docker_command()[:3] for call in calls)
    finally:
        authority.remove_runtime_paths()


def test_live_scenarios_use_exact_pytest_selection_without_default_addopts() -> None:
    regressions = LIVE_SCENARIOS["regressions"]
    assert regressions.expected_tests == 18
    assert build_live_pytest_command(regressions)[:7] == (
        os.fspath(Path(sys.executable)),
        "-m",
        "pytest",
        "-o",
        "addopts=",
        "-m",
        "integration",
    )
    assert build_live_pytest_command(regressions)[7:-1] == regressions.nodes
    assert all("::" not in node for node in regressions.nodes)
    assert LIVE_SCENARIOS["wrong-gid"].start_stack is False
    assert LIVE_SCENARIOS["socket-rootful"].start_stack is True


def test_make_and_ci_delegate_all_live_modes_to_the_shared_harness() -> None:
    makefile = Path("Makefile").read_text(encoding="utf-8")
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    workflow_payload = yaml.safe_load(workflow)
    for target in (
        "test-tool-worker-boundary:",
        "test-tool-worker-boundary-integration:",
        "test-tool-worker-live-upgrade:",
        "test-sandbox-socket-rootful:",
        "test-sandbox-socket-rootless:",
        "test-sandbox-socket-wrong-gid:",
    ):
        assert target in makefile
    assert "phase10_upgrade_harness" in makefile
    assert "docker compose" not in makefile
    assert "phase10-rootful-live:" in workflow
    assert "phase10-rootless-live:" in workflow
    assert "runs-on: ubuntu-24.04" in workflow
    assert "fetch-depth: 0" in workflow
    assert "SERVICE_PACKAGE=jhin-tool-worker" in workflow
    assert "docker/sandbox.Dockerfile" in workflow
    assert "PHASE10_MODE: rootful" in workflow
    assert "PHASE10_ROOTFUL_DOCKER_SOCKET: /var/run/docker.sock" in workflow
    assert "trap 'stop_rootless || true' EXIT" in workflow
    focused_recipe = makefile.split("test-tool-worker-boundary:", 1)[1].split("\n\n", 1)[0]
    assert "assert_phase10_tool_worker_compose.py" not in focused_recipe
    assert "SANDBOX_DOCKER_GID" not in focused_recipe
    python_job = workflow.split("  python:", 1)[1].split("\n  web:", 1)[0]
    assert "/var/run/docker.sock" not in python_job
    assert "SANDBOX_DOCKER_GID" not in python_job
    rootful_job = workflow.split("  phase10-rootful-live:", 1)[1].split(
        "\n  phase10-rootless-live:", 1
    )[0]
    rootless_job = workflow.split("  phase10-rootless-live:", 1)[1]
    assert "assert_phase10_tool_worker_compose.py --mode rootful" in rootful_job
    assert "assert_phase10_tool_worker_compose.py --mode rootless" in rootless_job

    rootless_steps = workflow_payload["jobs"]["phase10-rootless-live"]["steps"]
    rootless_checkout = rootless_steps[0]
    assert rootless_checkout["uses"] == "actions/checkout@v4"
    assert rootless_checkout["with"]["persist-credentials"] is False
    provision_step = next(
        step
        for step in rootless_steps
        if step.get("name") == "Provision UID-10001 rootless Docker prerequisites"
    )
    provision_script = provision_step["run"]
    apt_update = provision_script.index("sudo apt-get update")
    assert provision_script.index("https://download.docker.com/linux/ubuntu/gpg") < apt_update
    assert provision_script.index("/etc/apt/keyrings/docker.asc") < apt_update
    assert provision_script.index("/etc/apt/sources.list.d/docker.sources") < apt_update
    assert provision_script.index("URIs: https://download.docker.com/linux/ubuntu") < apt_update
    assert provision_script.index("Signed-By: /etc/apt/keyrings/docker.asc") < apt_update
    assert "dpkg-query -W -f='${Version}' docker-ce" in provision_script
    assert 'docker-ce-rootless-extras="$docker_ce_version"' in provision_script

    sync_step = next(
        step
        for step in rootless_steps
        if step.get("name") == "Sync workspace for the rootless test UID"
    )
    sync_script = sync_step["run"]
    rootless_workspace = "/tmp/jhin-phase10-rootless-workspace"
    assert 'sudo chown -R 10001:10001 "$GITHUB_WORKSPACE"' not in sync_script
    assert f"rootless_workspace={rootless_workspace}" in sync_script
    assert 'sudo install -d -o 10001 -g 10001 -m 0711 "$rootless_workspace"' in sync_script
    assert 'sudo cp -a "$GITHUB_WORKSPACE"/. "$rootless_workspace"/' in sync_script
    assert 'sudo chown -R 10001:10001 "$rootless_workspace"' in sync_script
    assert 'sudo chmod 0711 "$rootless_workspace"' in sync_script
    assert 'uv --directory "$rootless_workspace" sync --frozen --all-packages' in sync_script
    assert (
        sync_script.index('sudo cp -a "$GITHUB_WORKSPACE"/. "$rootless_workspace"/')
        < sync_script.index('sudo chown -R 10001:10001 "$rootless_workspace"')
        < sync_script.index('sudo chmod 0711 "$rootless_workspace"')
        < sync_script.index('uv --directory "$rootless_workspace" sync --frozen --all-packages')
    )
    for step_name in (
        "Validate rootless Compose contract",
        "Rootless sandbox socket",
        "Rootless tool-worker boundary",
        "Rootless Phase 9 to Phase 10 in-flight upgrade",
    ):
        selected_step = next(step for step in rootless_steps if step.get("name") == step_name)
        assert selected_step["working-directory"] == rootless_workspace

    start_step = next(
        step
        for step in rootless_steps
        if step.get("name") == "Start and validate UID-10001 rootless Docker"
    )
    start_script = start_step["run"]
    manager_set = start_script.index(
        "systemctl --user set-environment \\\n"
        "  HOME=/home/phase10rootless \\\n"
        "  XDG_CONFIG_HOME=/home/phase10rootless/.config \\\n"
        "  XDG_RUNTIME_DIR=/run/user/10001"
    )
    manager_verify = start_script.index("systemctl --user show-environment", manager_set)
    rootlesskit_preflight = start_script.index("rootlesskit true")
    installer = start_script.index("dockerd-rootless-setuptool.sh install --force")
    manager_set_env = start_script.rfind("sudo -u phase10rootless -H env", 0, manager_set)
    assert manager_set_env >= 0
    for assignment in (
        "HOME=/home/phase10rootless",
        "XDG_CONFIG_HOME=/home/phase10rootless/.config",
        "XDG_RUNTIME_DIR=/run/user/10001",
        "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/10001/bus",
    ):
        assert start_script.index(assignment, manager_set_env, manager_set)
    assert manager_set < manager_verify < rootlesskit_preflight < installer
    manager_verification = start_script[manager_verify:rootlesskit_preflight]
    for assignment in (
        "HOME=/home/phase10rootless",
        "XDG_CONFIG_HOME=/home/phase10rootless/.config",
        "XDG_RUNTIME_DIR=/run/user/10001",
    ):
        assert f"'{assignment}'" in manager_verification
    assert 'grep -Fx "$expected" <<<"$manager_environment" >/dev/null' in manager_verification
    assert "daemon.json" not in start_script
    assert "native.cgroupdriver" not in start_script
    assert "systemctl --user restart docker.service" not in start_script
    assert start_script.index("HOME=/home/phase10rootless") < rootlesskit_preflight
    assert (
        start_script.index("XDG_CONFIG_HOME=/home/phase10rootless/.config") < rootlesskit_preflight
    )
    assert rootlesskit_preflight < installer
    installer_env = start_script.rfind(
        "sudo -u phase10rootless -H env", rootlesskit_preflight, installer
    )
    assert installer_env > rootlesskit_preflight
    assert start_script.index("HOME=/home/phase10rootless", installer_env, installer)
    assert start_script.index(
        "XDG_CONFIG_HOME=/home/phase10rootless/.config", installer_env, installer
    )
    status_diagnostic = start_script.index(
        "systemctl --user --no-pager --full status docker.service", installer
    )
    journal_diagnostic = start_script.index(
        "journalctl --user --no-pager -n 100 -u docker.service", status_diagnostic
    )
    failure_branch_start = start_script.rfind(
        "if ! sudo -u phase10rootless -H env", rootlesskit_preflight, installer
    )
    failure_branch_end = start_script.index("\nfi\n", journal_diagnostic) + len("\nfi")
    failure_branch = start_script[failure_branch_start:failure_branch_end]
    assert failure_branch_start > rootlesskit_preflight
    assert installer < status_diagnostic < journal_diagnostic
    for assignment in (
        "HOME=/home/phase10rootless",
        "XDG_CONFIG_HOME=/home/phase10rootless/.config",
        "XDG_RUNTIME_DIR=/run/user/10001",
        "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/10001/bus",
    ):
        assert sum(line.strip() == f"{assignment} \\" for line in failure_branch.splitlines()) == 3
    assert "systemctl --user --no-pager --full status docker.service || true" in failure_branch
    assert "journalctl --user --no-pager -n 100 -u docker.service || true" in failure_branch
    assert failure_branch.endswith("exit 1\nfi")
    socket_owner_check = start_script.index(
        'test "$(sudo -u phase10rootless -H stat -c %u /run/user/10001/docker.sock)" = "10001"',
        installer,
    )
    security_check = start_script.index(
        "sudo -u phase10rootless -H docker --host ", socket_owner_check
    )
    cgroup_version_check = start_script.index("{{.CgroupVersion}}", security_check)
    cgroup_driver_check = start_script.index("{{.CgroupDriver}}", cgroup_version_check)
    socket_export = start_script.index(
        "PHASE10_ROOTLESS_DOCKER_SOCKET=/run/user/10001/docker.sock",
        cgroup_driver_check,
    )
    assert (
        installer
        < socket_owner_check
        < security_check
        < cgroup_version_check
        < cgroup_driver_check
        < socket_export
    )
    assert (
        'echo "ROOTLESS_SOCKET_SNAPSHOT=$(sudo -u phase10rootless -H '
        "stat -c '%d:%i:%f:%u:%g' "
        '/run/user/10001/docker.sock)" >> "$GITHUB_ENV"' in start_script
    )
    cleanup_step = next(
        step
        for step in rootless_steps
        if step.get("name") == "Stop the CI-owned rootless daemon after identity verification"
    )
    assert cleanup_step["if"] == ("${{ always() && env.ROOTLESS_SOCKET_SNAPSHOT != '' }}")
    assert 'test -n "$ROOTLESS_SOCKET_SNAPSHOT"' in cleanup_step["run"]
    assert "sudo -u phase10rootless -H test -S /run/user/10001/docker.sock" in cleanup_step["run"]
    assert (
        "test \"$(sudo -u phase10rootless -H stat -c '%d:%i:%f:%u:%g' "
        '/run/user/10001/docker.sock)" '
        '= "$ROOTLESS_SOCKET_SNAPSHOT"' in cleanup_step["run"]
    )


def test_live_authority_selection_binds_requested_socket_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = Path(__file__).resolve().parents[2]
    socket_path = Path("/var/run/docker.sock")
    metadata = SocketMetadata(
        path=socket_path,
        inode=24680,
        mode=stat.S_IFSOCK | 0o660,
        uid=0,
        gid=123,
    )
    monkeypatch.setattr(
        SocketMetadata,
        "capture",
        classmethod(lambda cls, path: metadata),
    )
    with pytest.raises(ValueError, match="GID"):
        select_live_authority(
            repo=repo,
            mode="rootful",
            source_environment={"PATH": "/usr/bin", "SANDBOX_DOCKER_GID": "124"},
        )
    authority = select_live_authority(
        repo=repo,
        mode="rootful",
        source_environment={"PATH": "/usr/bin", "SANDBOX_DOCKER_GID": "123"},
    )
    try:
        assert authority.socket_snapshot == metadata
        assert authority.socket_gid == 123
        assert authority.environment["SANDBOX_DOCKER_GID"] == "123"
    finally:
        authority.remove_runtime_paths()

    with pytest.raises(ValueError, match="PHASE10_ROOTLESS_DOCKER_SOCKET"):
        select_live_authority(
            repo=repo,
            mode="rootless",
            source_environment={"PATH": "/usr/bin"},
        )


def test_one_shot_lifecycle_publishes_lease_before_child_and_cleans_every_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = _authority_for_recorder()
    ports = {
        variable: 51000 + index
        for index, (variable, _service, _container_port) in enumerate(PUBLISHED_ENDPOINTS)
    }
    lease = Path("/tmp") / f"jhin-p10-one-shot-{uuid.uuid4().hex}.json"
    observed: list[str] = []

    def fake_start(self: ComposeAuthority, *, runner: Any) -> dict[str, int]:
        del self, runner
        observed.append("start")
        return ports

    def fake_cleanup(
        self: ComposeAuthority,
        *,
        runner: Any,
        upgrade: bool = False,
    ) -> None:
        del self, runner, upgrade
        observed.append("cleanup")

    def child_runner(
        command: tuple[str, ...],
        *,
        env: dict[str, str],
        cwd: Path,
        timeout: float,
        check: bool,
        input_bytes: bytes | None = None,
    ) -> subprocess.CompletedProcess[str]:
        del timeout, check, input_bytes
        observed.append("child")
        assert lease.is_file()
        loaded = read_authority_lease(lease, expected_repo=authority.repo)
        assert loaded.published_ports == ports
        assert env["JHIN_PHASE10_AUTHORITY_LEASE"] == str(lease)
        assert env["JHIN_PHASE10_EXPECTED_TESTS"] == "1"
        assert cwd == authority.repo
        assert command == build_live_pytest_command(scenario)
        return subprocess.CompletedProcess(command, 0, "passed\n", "")

    monkeypatch.setattr(ComposeAuthority, "start_stack", fake_start)
    monkeypatch.setattr(ComposeAuthority, "down_and_cleanup", fake_cleanup)
    scenario = LiveScenario(nodes=("tests/integration/fake.py::test_live",), expected_tests=1)
    try:
        result = execute_one_shot(
            authority,
            scenario=scenario,
            lease_path=lease,
            child_runner=child_runner,
        )
        assert result.returncode == 0
        assert observed == ["start", "child", "cleanup"]
        assert not lease.exists()
    finally:
        lease.unlink(missing_ok=True)
        authority.remove_runtime_paths()


def test_one_shot_emits_redacted_child_failure_and_preserves_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    authority = _authority_for_recorder()
    ports = {
        variable: 51200 + index
        for index, (variable, _service, _container_port) in enumerate(PUBLISHED_ENDPOINTS)
    }
    lease = Path("/tmp") / f"jhin-p10-child-failure-{uuid.uuid4().hex}.json"
    observed: list[str] = []
    sensitive_values: list[str] = []
    failures: list[subprocess.CalledProcessError] = []

    def fake_start(self: ComposeAuthority, *, runner: Any) -> dict[str, int]:
        del self, runner
        observed.append("start")
        return ports

    def fake_cleanup(
        self: ComposeAuthority,
        *,
        runner: Any,
        upgrade: bool = False,
    ) -> None:
        del self, runner, upgrade
        observed.append("cleanup")

    def failing_child(
        command: tuple[str, ...],
        *,
        env: dict[str, str],
        cwd: Path,
        timeout: float,
        check: bool,
        input_bytes: bytes | None = None,
    ) -> subprocess.CompletedProcess[str]:
        del cwd, timeout, input_bytes
        assert check is True
        observed.append("child")
        sensitive_values.extend((env["SANDBOX_RUNNER_TOKEN"], env["JHIN_PHASE9_DB_READER_DSN"]))
        failure = subprocess.CalledProcessError(
            1,
            command,
            output=f"FAILED live-node {sensitive_values[0]}\n",
            stderr=f"database detail {sensitive_values[1]}\n",
        )
        failures.append(failure)
        raise failure

    monkeypatch.setattr(ComposeAuthority, "start_stack", fake_start)
    monkeypatch.setattr(ComposeAuthority, "down_and_cleanup", fake_cleanup)
    scenario = LiveScenario(nodes=("tests/integration/fake.py::test_live",), expected_tests=1)
    try:
        with pytest.raises(subprocess.CalledProcessError) as raised:
            execute_one_shot(
                authority,
                scenario=scenario,
                lease_path=lease,
                child_runner=failing_child,
            )
        assert raised.value.returncode == 1
        emitted = capsys.readouterr().err
        assert "FAILED live-node" in emitted
        assert "database detail" in emitted
        assert sensitive_values
        assert all(value not in emitted for value in sensitive_values)
        assert failures == [raised.value]
        assert emitted.count("--- live pytest stdout (redacted tail) ---") == 1
        assert emitted.count("--- live pytest stderr (redacted tail) ---") == 1
        assert emitted.count("FAILED live-node") == 1
        assert emitted.count("database detail") == 1
        assert "--- live setup" not in emitted
        assert observed == ["start", "child", "cleanup"]
        assert not lease.exists()
    finally:
        lease.unlink(missing_ok=True)
        authority.remove_runtime_paths()


def test_one_shot_emits_redacted_setup_failure_and_preserves_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    authority = _authority_for_recorder()
    lease = Path("/tmp") / f"jhin-p10-setup-failure-{uuid.uuid4().hex}.json"
    observed: list[str] = []
    runner_token = authority.environment["SANDBOX_RUNNER_TOKEN"]
    failure = subprocess.CalledProcessError(
        1,
        ("docker", "compose", "build", "sandbox-image"),
        output=f"build output {runner_token}\n",
        stderr=f"build failure {runner_token}\n",
    )

    def failing_start(
        self: ComposeAuthority,
        *,
        runner: Any,
    ) -> dict[str, int]:
        del self, runner
        observed.append("start")
        raise failure

    def fake_cleanup(
        self: ComposeAuthority,
        *,
        runner: Any,
        upgrade: bool = False,
    ) -> None:
        del self, runner, upgrade
        observed.append("cleanup")

    monkeypatch.setattr(ComposeAuthority, "start_stack", failing_start)
    monkeypatch.setattr(ComposeAuthority, "down_and_cleanup", fake_cleanup)
    scenario = LiveScenario(nodes=("tests/integration/fake.py::test_live",), expected_tests=1)
    try:
        with pytest.raises(subprocess.CalledProcessError) as raised:
            execute_one_shot(
                authority,
                scenario=scenario,
                lease_path=lease,
            )
        assert raised.value is failure
        emitted = capsys.readouterr().err
        assert "live setup stdout" in emitted
        assert "live setup stderr" in emitted
        assert "build output" in emitted
        assert "build failure" in emitted
        assert runner_token not in emitted
        assert "<redacted>" in emitted
        assert emitted.count("--- live setup stdout (redacted tail) ---") == 1
        assert emitted.count("--- live setup stderr (redacted tail) ---") == 1
        assert emitted.count("build output") == 1
        assert emitted.count("build failure") == 1
        assert "--- live pytest" not in emitted
        assert observed == ["start", "cleanup"]
        assert not lease.exists()
    finally:
        lease.unlink(missing_ok=True)
        authority.remove_runtime_paths()


def test_one_shot_withholds_cleanup_if_owned_child_group_survives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = _authority_for_recorder()
    lease = Path("/tmp") / f"jhin-p10-one-shot-{uuid.uuid4().hex}.json"
    cleanup_calls: list[bool] = []
    monkeypatch.setattr(
        ComposeAuthority,
        "preflight",
        lambda self, *, runner: None,
    )
    monkeypatch.setattr(
        ComposeAuthority,
        "down_and_cleanup",
        lambda self, *, runner, upgrade=False: cleanup_calls.append(upgrade),
    )

    def surviving_child_group(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        raise lifecycle._OwnedProcessGroupSurvived(4242)

    scenario = LiveScenario(nodes=("signal-probe",), expected_tests=1, start_stack=False)
    try:
        with pytest.raises(RuntimeError, match=r"4242.*survived"):
            execute_one_shot(
                authority,
                scenario=scenario,
                lease_path=lease,
                child_runner=surviving_child_group,
            )
        assert cleanup_calls == []
        assert lease.is_file()
    finally:
        lease.unlink(missing_ok=True)
        authority.remove_runtime_paths()


def test_one_shot_journals_recovery_lease_before_first_mutating_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = _authority_for_recorder()
    lease = Path("/tmp") / f"jhin-p10-recovery-before-start-{uuid.uuid4().hex}.json"
    lease_seen_before_start: list[bool] = []
    cleanup_calls: list[bool] = []

    def surviving_start(self: ComposeAuthority, *, runner: Any) -> dict[str, int]:
        del self, runner
        lease_seen_before_start.append(lease.is_file())
        raise lifecycle._OwnedProcessGroupSurvived(4243)

    monkeypatch.setattr(ComposeAuthority, "start_stack", surviving_start)
    monkeypatch.setattr(
        ComposeAuthority,
        "down_and_cleanup",
        lambda self, *, runner, upgrade=False: cleanup_calls.append(upgrade),
    )
    scenario = LiveScenario(nodes=("recovery-lease",), expected_tests=1)
    try:
        with pytest.raises(RuntimeError, match=r"4243.*survived"):
            execute_one_shot(authority, scenario=scenario, lease_path=lease)
        assert lease_seen_before_start == [True]
        assert cleanup_calls == []
        loaded = read_authority_lease(lease, expected_repo=authority.repo)
        assert loaded.project == authority.project
        assert loaded.runtime_dir == authority.runtime_dir
        assert loaded.published_ports == {}
        assert authority.runtime_dir.is_dir()
        assert authority.barrier_root.is_dir()
    finally:
        lease.unlink(missing_ok=True)
        authority.remove_runtime_paths()


def test_one_shot_cleanup_stops_commands_and_withholds_lease_on_group_survivor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = _authority_for_recorder()
    lease = Path("/tmp") / f"jhin-p10-one-shot-{uuid.uuid4().hex}.json"
    runner_calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        ComposeAuthority,
        "preflight",
        lambda self, *, runner: None,
    )

    def surviving_runner(command: tuple[str, ...], **kwargs: Any) -> Any:
        del kwargs
        runner_calls.append(command)
        raise lifecycle._OwnedProcessGroupSurvived(4343)

    def fake_cleanup(
        self: ComposeAuthority,
        *,
        runner: Any,
        upgrade: bool = False,
    ) -> None:
        del self, upgrade
        errors: list[BaseException] = []
        for command in (("cleanup-first",), ("cleanup-second",)):
            try:
                runner(
                    command,
                    env=authority.environment,
                    cwd=authority.repo,
                    timeout=5.0,
                    check=False,
                    input_bytes=None,
                )
            except BaseException as error:
                errors.append(error)
        raise BaseExceptionGroup("cleanup commands failed", errors)

    def child_runner(command: tuple[str, ...], **kwargs: Any) -> Any:
        del kwargs
        return subprocess.CompletedProcess(command, 0, "passed\n", "")

    monkeypatch.setattr(ComposeAuthority, "down_and_cleanup", fake_cleanup)
    scenario = LiveScenario(nodes=("cleanup-survivor",), expected_tests=1, start_stack=False)
    try:
        with pytest.raises(BaseExceptionGroup, match="cleanup"):
            execute_one_shot(
                authority,
                scenario=scenario,
                lease_path=lease,
                runner=surviving_runner,
                child_runner=child_runner,
            )
        assert runner_calls == [("cleanup-first",)]
        assert lease.is_file()
    finally:
        lease.unlink(missing_ok=True)
        authority.remove_runtime_paths()


def test_real_cleanup_survivor_retains_lease_key_and_runtime_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = _authority_for_recorder()
    lease = Path("/tmp") / f"jhin-p10-real-cleanup-survivor-{uuid.uuid4().hex}.json"
    authority.master_key_path.write_bytes(b"k" * 32)
    os.chmod(authority.master_key_path, 0o400)
    runner_calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(ComposeAuthority, "preflight", lambda self, *, runner: None)

    def surviving_cleanup_group(command: tuple[str, ...], **kwargs: Any) -> Any:
        del kwargs
        runner_calls.append(command)
        raise lifecycle._OwnedProcessGroupSurvived(4646)

    def child_runner(command: tuple[str, ...], **kwargs: Any) -> Any:
        del kwargs
        return subprocess.CompletedProcess(command, 0, "passed\n", "")

    scenario = LiveScenario(nodes=("real-cleanup-survivor",), expected_tests=1, start_stack=False)
    try:
        with pytest.raises(BaseExceptionGroup, match="cleanup") as raised:
            execute_one_shot(
                authority,
                scenario=scenario,
                lease_path=lease,
                runner=surviving_cleanup_group,
                child_runner=child_runner,
            )
        assert lifecycle._contains_owned_process_group_survivor(raised.value)
        assert len(runner_calls) == 1
        assert lease.is_file()
        assert authority.runtime_dir.is_dir()
        assert authority.barrier_root.is_dir()
        assert authority.master_key_path.read_bytes() == b"k" * 32
        assert stat.S_IMODE(authority.master_key_path.lstat().st_mode) == 0o400
        loaded = read_authority_lease(lease, expected_repo=authority.repo)
        assert loaded.runtime_dir == authority.runtime_dir
        assert loaded.barrier_root == authority.barrier_root
        assert loaded.master_key_path == authority.master_key_path
    finally:
        lease.unlink(missing_ok=True)
        authority.remove_runtime_paths()


def test_one_shot_indeterminate_cleanup_retains_readable_recovery_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = _authority_for_recorder()
    lease = Path("/tmp") / f"jhin-p10-indeterminate-one-shot-{uuid.uuid4().hex}.json"
    authority.master_key_path.write_bytes(b"r" * 32)
    authority.master_key_path.chmod(0o400)
    monkeypatch.setattr(ComposeAuthority, "preflight", lambda self, *, runner: None)

    def successful_child(command: tuple[str, ...], **kwargs: Any) -> Any:
        del kwargs
        return subprocess.CompletedProcess(command, 0, "passed\n", "")

    def indeterminate_runner(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        raise TimeoutError("indeterminate Docker cleanup")

    scenario = LiveScenario(nodes=("indeterminate-cleanup",), expected_tests=1, start_stack=False)
    try:
        with pytest.raises(BaseExceptionGroup, match="cleanup"):
            execute_one_shot(
                authority,
                scenario=scenario,
                lease_path=lease,
                runner=indeterminate_runner,
                child_runner=successful_child,
            )
        loaded = read_authority_lease(lease, expected_repo=authority.repo)
        assert loaded.runtime_dir == authority.runtime_dir
        assert authority.runtime_dir.is_dir()
        assert authority.barrier_root.is_dir()
        assert authority.master_key_path.read_bytes() == b"r" * 32
    finally:
        lease.unlink(missing_ok=True)
        authority.remove_runtime_paths()


def test_one_shot_default_runner_owns_setup_and_cleanup_process_groups(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = _authority_for_recorder()
    lease = Path("/tmp") / f"jhin-p10-one-shot-{uuid.uuid4().hex}.json"
    ports = {
        variable: 51500 + index
        for index, (variable, _service, _container_port) in enumerate(PUBLISHED_ENDPOINTS)
    }
    process_groups: dict[str, dict[str, int]] = {}

    def record_process_group(label: str, runner: Any) -> None:
        command = (
            sys.executable,
            "-c",
            "import json,os; print(json.dumps({'pid': os.getpid(), 'pgid': os.getpgrp()}))",
        )
        result = runner(
            command,
            env=authority.environment,
            cwd=authority.repo,
            timeout=5.0,
            check=True,
            input_bytes=None,
        )
        process_groups[label] = json.loads(cast(str, result.stdout))

    def fake_start(self: ComposeAuthority, *, runner: Any) -> dict[str, int]:
        del self
        record_process_group("setup", runner)
        return ports

    def fake_cleanup(
        self: ComposeAuthority,
        *,
        runner: Any,
        upgrade: bool = False,
    ) -> None:
        del self, upgrade
        record_process_group("cleanup", runner)

    def child_runner(command: tuple[str, ...], **kwargs: Any) -> Any:
        del kwargs
        return subprocess.CompletedProcess(command, 0, "passed\n", "")

    monkeypatch.setattr(ComposeAuthority, "start_stack", fake_start)
    monkeypatch.setattr(ComposeAuthority, "down_and_cleanup", fake_cleanup)
    scenario = LiveScenario(nodes=("owned-outer-runner",), expected_tests=1)
    try:
        execute_one_shot(
            authority,
            scenario=scenario,
            lease_path=lease,
            child_runner=child_runner,
        )
        assert set(process_groups) == {"setup", "cleanup"}
        assert process_groups["setup"]["pid"] == process_groups["setup"]["pgid"]
        assert process_groups["cleanup"]["pid"] == process_groups["cleanup"]["pgid"]
        assert process_groups["setup"]["pgid"] != process_groups["cleanup"]["pgid"]
    finally:
        lease.unlink(missing_ok=True)
        authority.remove_runtime_paths()


def _spawn_signal_lifecycle_probe(
    tmp_path: Path,
    *,
    mode: str,
    stage: str = "pytest",
) -> tuple[subprocess.Popen[str], Path, Path]:
    repo = Path(__file__).resolve().parents[2]
    token = uuid.uuid4().hex[:12]
    lease = Path("/tmp") / f"jhin-p10-signal-{token}.json"
    script = r"""
import json
import os
import signal
import sys
import time
from pathlib import Path

from tests.integration import phase10_upgrade_harness as lifecycle

mode = sys.argv[1]
stage = sys.argv[2]
root = Path(sys.argv[3])
repo = Path(sys.argv[4])
lease = Path(sys.argv[5])
token = sys.argv[6]
child_pid_path = root / "child.json"
grandchild_pid_path = root / "grandchild.json"
mutation_path = root / "grandchild-mutations"
cleanup_started = root / "cleanup-started"
cleanup_state = root / "cleanup-state"
survivor_check = root / "survivor-check"

authority = lifecycle.ComposeAuthority.create(
    repo=repo,
    mode="rootful",
    socket_path=Path("/var/run/docker.sock"),
    socket_gid=123,
    token=token,
    source_environment={"PATH": os.environ.get("PATH", "/usr/bin")},
)

def observed_cleanup(self, *, runner, upgrade=False):
    del self, runner, upgrade
    child = json.loads(child_pid_path.read_text(encoding="utf-8"))
    grandchild = json.loads(grandchild_pid_path.read_text(encoding="utf-8"))

    def alive(pid):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True

    try:
        os.killpg(child["pgid"], 0)
    except ProcessLookupError:
        group_alive = False
    else:
        group_alive = True
    before = mutation_path.stat().st_size
    time.sleep(0.15)
    after = mutation_path.stat().st_size
    cleanup_state.write_text(
        json.dumps(
            {
                "harness_pid": os.getpid(),
                "harness_pgid": os.getpgrp(),
                "child_pid": child["pid"],
                "child_pgid": child["pgid"],
                "child_alive": alive(child["pid"]),
                "grandchild_pid": grandchild["pid"],
                "grandchild_pgid": grandchild["pgid"],
                "grandchild_alive": alive(grandchild["pid"]),
                "group_alive": group_alive,
                "mutation_stable": before == after,
            }
        ),
        encoding="utf-8",
    )
    cleanup_started.write_text("started", encoding="utf-8")
    if mode == "second-signal":
        time.sleep(1.0)
    survivor_check.write_text("checked", encoding="utf-8")

grandchild_code = (
    "import json,os,signal,sys,time\n"
    "from pathlib import Path\n"
    "state = Path(sys.argv[1])\n"
    "mutations = Path(sys.argv[2])\n"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "state.write_text(json.dumps({'pid': os.getpid(), 'pgid': os.getpgrp()}), "
    "encoding='utf-8')\n"
    "while True:\n"
    "    with mutations.open('a', encoding='utf-8') as stream:\n"
    "        stream.write('mutation\\n')\n"
    "        stream.flush()\n"
    "        os.fsync(stream.fileno())\n"
    "    time.sleep(0.02)\n"
)
child_code = (
    "import json,os,sys\n"
    "from pathlib import Path\n"
    "from tests.integration import phase10_upgrade_harness as nested_lifecycle\n"
    "Path(sys.argv[1]).write_text(json.dumps({'pid': os.getpid(), "
    "'pgid': os.getpgrp()}), encoding='utf-8')\n"
    f"grandchild_code = {grandchild_code!r}\n"
    "nested_lifecycle.run_command(\n"
    "    (sys.executable, '-c', grandchild_code, sys.argv[2], sys.argv[3]),\n"
    "    env=os.environ.copy(),\n"
    "    cwd=Path.cwd(),\n"
    "    timeout=300.0,\n"
    "    check=True,\n"
    ")\n"
)

def selected_preflight(self, *, runner):
    if stage in {"preflight", "preflight-timeout", "termination-transition"}:
        runner(
            (
                sys.executable,
                "-c",
                child_code,
                str(child_pid_path),
                str(grandchild_pid_path),
                str(mutation_path),
            ),
            env=self.environment,
            cwd=repo,
            timeout=0.5 if stage in {"preflight-timeout", "termination-transition"} else 300.0,
            check=True,
            input_bytes=None,
        )

lifecycle.ComposeAuthority.preflight = selected_preflight
lifecycle.ComposeAuthority.down_and_cleanup = observed_cleanup
if stage == "termination-transition":
    real_signal = lifecycle.signal.signal
    transition_injected = False

    def inject_signal_during_ignore(signum, handler):
        global transition_injected
        result = real_signal(signum, handler)
        if (
            not transition_injected
            and handler is signal.SIG_IGN
            and child_pid_path.is_file()
            and grandchild_pid_path.is_file()
        ):
            transition_injected = True
            os.kill(os.getpid(), signal.SIGTERM)
        return result

    lifecycle.signal.signal = inject_signal_during_ignore
if stage == "pytest":
    lifecycle.build_live_pytest_command = lambda scenario: (
        sys.executable,
        "-c",
        child_code,
        str(child_pid_path),
        str(grandchild_pid_path),
        str(mutation_path),
    )
else:
    lifecycle.build_live_pytest_command = lambda scenario: (
        sys.executable,
        "-c",
        "raise AssertionError('final pytest child unexpectedly started')",
    )
scenario = lifecycle.LiveScenario(
    nodes=("signal-probe",),
    expected_tests=1,
    start_stack=False,
)
try:
    lifecycle.execute_one_shot(authority, scenario=scenario, lease_path=lease)
finally:
    authority.remove_runtime_paths()
"""
    process = subprocess.Popen(
        (
            sys.executable,
            "-c",
            script,
            mode,
            stage,
            str(tmp_path),
            str(repo),
            str(lease),
            token,
        ),
        cwd=repo,
        env=os.environ.copy(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    child_pid_path = tmp_path / "child.json"
    deadline = time.monotonic() + 10.0
    grandchild_pid_path = tmp_path / "grandchild.json"
    mutation_path = tmp_path / "grandchild-mutations"
    while (
        not (child_pid_path.is_file() and grandchild_pid_path.is_file() and mutation_path.is_file())
        and process.poll() is None
        and time.monotonic() < deadline
    ):
        time.sleep(0.02)
    if not (child_pid_path.is_file() and grandchild_pid_path.is_file() and mutation_path.is_file()):
        stdout, stderr = process.communicate(timeout=5.0)
        pytest.fail(f"signal lifecycle child did not start: {stdout=} {stderr=}")
    return process, child_pid_path, lease


def _finish_signal_lifecycle_probe(
    process: subprocess.Popen[str],
    child_pid_path: Path,
    lease: Path,
) -> tuple[str, str, bool]:
    child_group: int | None = None
    try:
        stdout, stderr = process.communicate(timeout=10.0)
        return stdout, stderr, lease.exists()
    finally:
        # Always reap every recorded owned group, even if the harness already
        # exited before this test's emergency cleanup begins.
        if child_pid_path.is_file():
            child = json.loads(child_pid_path.read_text(encoding="utf-8"))
            child_group = int(child["pgid"])
            _signal_test_process_group(child_group)
        if process.poll() is None:
            _signal_test_process_group(process.pid)
            process.wait(timeout=5.0)
        _wait_for_test_process_group_exit(process.pid)
        if child_group is not None:
            _wait_for_test_process_group_exit(child_group)
        lease.unlink(missing_ok=True)


def _spawn_popen_window_probe(
    tmp_path: Path,
    *,
    signum: int,
) -> tuple[subprocess.Popen[str], Path, Path]:
    repo = Path(__file__).resolve().parents[2]
    token = uuid.uuid4().hex[:12]
    lease = Path("/tmp") / f"jhin-p10-spawn-window-{token}.json"
    script = r"""
import json
import os
import signal
import sys
import threading
import time
from pathlib import Path

from tests.integration import phase10_upgrade_harness as lifecycle

signum = int(sys.argv[1])
root = Path(sys.argv[2])
repo = Path(sys.argv[3])
lease = Path(sys.argv[4])
token = sys.argv[5]
child_state = root / "spawn-window-child.json"
grandchild_state = root / "spawn-window-grandchild.json"
mutation_path = root / "spawn-window-mutations"
cleanup_state = root / "spawn-window-cleanup.json"

authority = lifecycle.ComposeAuthority.create(
    repo=repo,
    mode="rootful",
    socket_path=Path("/var/run/docker.sock"),
    socket_gid=123,
    token=token,
    source_environment={"PATH": os.environ.get("PATH", "/usr/bin")},
)

def no_preflight(self, *, runner):
    del self, runner

def observed_cleanup(self, *, runner, upgrade=False):
    del self, runner, upgrade
    child = json.loads(child_state.read_text(encoding="utf-8"))
    grandchild = json.loads(grandchild_state.read_text(encoding="utf-8"))

    def alive(pid):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True

    try:
        os.killpg(child["pgid"], 0)
    except ProcessLookupError:
        group_alive = False
    else:
        group_alive = True
    before = mutation_path.stat().st_size
    time.sleep(0.15)
    after = mutation_path.stat().st_size
    cleanup_state.write_text(
        json.dumps(
            {
                "child_alive": alive(child["pid"]),
                "grandchild_alive": alive(grandchild["pid"]),
                "group_alive": group_alive,
                "mutation_stable": before == after,
            }
        ),
        encoding="utf-8",
    )

lifecycle.ComposeAuthority.preflight = no_preflight
lifecycle.ComposeAuthority.down_and_cleanup = observed_cleanup
grandchild_code = (
    "import json,os,signal,sys,time\n"
    "from pathlib import Path\n"
    "state = Path(sys.argv[1])\n"
    "mutations = Path(sys.argv[2])\n"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "state.write_text(json.dumps({'pid': os.getpid(), 'pgid': os.getpgrp()}), "
    "encoding='utf-8')\n"
    "while True:\n"
    "    with mutations.open('a', encoding='utf-8') as stream:\n"
    "        stream.write('mutation\\n')\n"
    "        stream.flush()\n"
    "        os.fsync(stream.fileno())\n"
    "    time.sleep(0.02)\n"
)
child_code = (
    "import os,subprocess,sys,time\n"
    f"grandchild_code = {grandchild_code!r}\n"
    "subprocess.Popen(\n"
    "    (sys.executable, '-c', grandchild_code, sys.argv[1], sys.argv[2]),\n"
    "    stdin=subprocess.DEVNULL,\n"
    "    stdout=subprocess.DEVNULL,\n"
    "    stderr=subprocess.DEVNULL,\n"
    ")\n"
    "while True:\n"
    "    time.sleep(0.02)\n"
)
lifecycle.build_live_pytest_command = lambda scenario: (
    sys.executable,
    "-c",
    child_code,
    str(grandchild_state),
    str(mutation_path),
)

real_popen = lifecycle.subprocess.Popen
triggered = False
helper_ready = threading.Event()
helper_stop = threading.Event()
signal_requested = threading.Event()
signal_sent = threading.Event()

def unblocked_signal_target():
    previous = signal.pthread_sigmask(
        signal.SIG_UNBLOCK,
        {signal.SIGINT, signal.SIGTERM},
    )
    helper_ready.set()
    try:
        if signal_requested.wait(timeout=10.0):
            os.kill(os.getpid(), signum)
            signal_sent.set()
        helper_stop.wait(timeout=10.0)
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)

helper = threading.Thread(target=unblocked_signal_target, daemon=True)
helper.start()
assert helper_ready.wait(timeout=5.0)

def signal_after_real_spawn(*args, **kwargs):
    global triggered
    process = real_popen(*args, **kwargs)
    if not triggered:
        triggered = True
        child_state.write_text(
            json.dumps({"pid": process.pid, "pgid": os.getpgid(process.pid)}),
            encoding="utf-8",
        )
        deadline = time.monotonic() + 5.0
        while (
            not (grandchild_state.is_file() and mutation_path.is_file())
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        if not (grandchild_state.is_file() and mutation_path.is_file()):
            raise RuntimeError("spawn-window grandchild did not start")
        signal_requested.set()
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            if signal_sent.is_set():
                sum(range(100))
    return process

lifecycle.subprocess.Popen = signal_after_real_spawn
scenario = lifecycle.LiveScenario(
    nodes=("spawn-window",),
    expected_tests=1,
    start_stack=False,
)
try:
    lifecycle.execute_one_shot(authority, scenario=scenario, lease_path=lease)
finally:
    helper_stop.set()
    helper.join(timeout=5.0)
    authority.remove_runtime_paths()
"""
    process = subprocess.Popen(
        (
            sys.executable,
            "-c",
            script,
            str(signum),
            str(tmp_path),
            str(repo),
            str(lease),
            token,
        ),
        cwd=repo,
        env=os.environ.copy(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    child_state = tmp_path / "spawn-window-child.json"
    deadline = time.monotonic() + 10.0
    while not child_state.is_file() and process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.02)
    if not child_state.is_file():
        stdout, stderr = process.communicate(timeout=5.0)
        pytest.fail(f"spawn-window child did not start: {stdout=} {stderr=}")
    return process, child_state, lease


def _spawn_post_communicate_signal_probe(
    tmp_path: Path,
) -> tuple[subprocess.Popen[str], Path, Path]:
    repo = Path(__file__).resolve().parents[2]
    token = uuid.uuid4().hex[:12]
    lease = Path("/tmp") / f"jhin-p10-post-communicate-{token}.json"
    script = r"""
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

from tests.integration import phase10_upgrade_harness as lifecycle

root = Path(sys.argv[1])
repo = Path(sys.argv[2])
lease = Path(sys.argv[3])
token = sys.argv[4]
descendant_state = root / "post-communicate-descendant.json"
cleanup_state = root / "post-communicate-cleanup.json"

authority = lifecycle.ComposeAuthority.create(
    repo=repo,
    mode="rootful",
    socket_path=Path("/var/run/docker.sock"),
    socket_gid=123,
    token=token,
    source_environment={"PATH": os.environ.get("PATH", "/usr/bin")},
)

def no_preflight(self, *, runner):
    del self, runner

def observed_cleanup(self, *, runner, upgrade=False):
    del self, runner, upgrade
    descendant = json.loads(descendant_state.read_text(encoding="utf-8"))
    try:
        os.kill(descendant["pid"], 0)
    except ProcessLookupError:
        descendant_alive = False
    else:
        descendant_alive = True
    try:
        os.killpg(descendant["pgid"], 0)
    except ProcessLookupError:
        group_alive = False
    else:
        group_alive = True
    cleanup_state.write_text(
        json.dumps(
            {"descendant_alive": descendant_alive, "group_alive": group_alive}
        ),
        encoding="utf-8",
    )

descendant_code = (
    "import signal,time\n"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "time.sleep(300)\n"
)
child_code = (
    "import json,os,subprocess,sys\n"
    "from pathlib import Path\n"
    f"descendant_code = {descendant_code!r}\n"
    "descendant = subprocess.Popen(\n"
    "    (sys.executable, '-c', descendant_code),\n"
    "    stdin=subprocess.DEVNULL,\n"
    "    stdout=subprocess.DEVNULL,\n"
    "    stderr=subprocess.DEVNULL,\n"
    "    close_fds=True,\n"
    ")\n"
    "Path(sys.argv[1]).write_text(json.dumps({'pid': descendant.pid, "
    "'pgid': os.getpgid(descendant.pid)}), encoding='utf-8')\n"
)

lifecycle.ComposeAuthority.preflight = no_preflight
lifecycle.ComposeAuthority.down_and_cleanup = observed_cleanup
lifecycle.build_live_pytest_command = lambda scenario: (
    sys.executable,
    "-c",
    child_code,
    str(descendant_state),
)
real_group_exists = lifecycle._owned_process_group_exists
signal_injected = False

def inject_signal_on_post_communicate_check(process_group):
    global signal_injected
    if not signal_injected and descendant_state.is_file():
        signal_injected = True
        os.kill(os.getpid(), signal.SIGTERM)
    return real_group_exists(process_group)

lifecycle._owned_process_group_exists = inject_signal_on_post_communicate_check
scenario = lifecycle.LiveScenario(
    nodes=("post-communicate",),
    expected_tests=1,
    start_stack=False,
)
try:
    lifecycle.execute_one_shot(authority, scenario=scenario, lease_path=lease)
finally:
    authority.remove_runtime_paths()
"""
    process = subprocess.Popen(
        (sys.executable, "-c", script, str(tmp_path), str(repo), str(lease), token),
        cwd=repo,
        env=os.environ.copy(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    descendant_state = tmp_path / "post-communicate-descendant.json"
    deadline = time.monotonic() + 10.0
    while not descendant_state.is_file() and process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.02)
    if not descendant_state.is_file():
        stdout, stderr = process.communicate(timeout=5.0)
        pytest.fail(f"post-communicate descendant did not start: {stdout=} {stderr=}")
    return process, descendant_state, lease


def _spawn_persistent_start_probe(
    tmp_path: Path,
    *,
    mode: str,
) -> tuple[subprocess.Popen[str], Path, Path]:
    repo = Path(__file__).resolve().parents[2]
    token = uuid.uuid4().hex[:12]
    lease = lease_path_for(repo)
    assert not lease.exists() and not lease.is_symlink()
    script = r"""
import json
import os
import sys
import time
from pathlib import Path

from tests.integration import phase10_upgrade_harness as lifecycle

mode = sys.argv[1]
root = Path(sys.argv[2])
repo = Path(sys.argv[3])
token = sys.argv[4]
child_state = root / "persistent-child.json"
grandchild_state = root / "persistent-grandchild.json"
mutation_path = root / "persistent-mutations"
cleanup_state = root / "persistent-cleanup.json"

authority = lifecycle.ComposeAuthority.create(
    repo=repo,
    mode="rootful",
    socket_path=Path("/var/run/docker.sock"),
    socket_gid=123,
    token=token,
    source_environment={"PATH": os.environ.get("PATH", "/usr/bin")},
)

grandchild_code = (
    "import json,os,signal,sys,time\n"
    "from pathlib import Path\n"
    "state = Path(sys.argv[1])\n"
    "mutations = Path(sys.argv[2])\n"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "state.write_text(json.dumps({'pid': os.getpid(), 'pgid': os.getpgrp()}), "
    "encoding='utf-8')\n"
    "while True:\n"
    "    with mutations.open('a', encoding='utf-8') as stream:\n"
    "        stream.write('mutation\\n')\n"
    "        stream.flush()\n"
    "        os.fsync(stream.fileno())\n"
    "    time.sleep(0.02)\n"
)
child_code = (
    "import json,os,sys\n"
    "from pathlib import Path\n"
    "from tests.integration import phase10_upgrade_harness as nested_lifecycle\n"
    "Path(sys.argv[1]).write_text(json.dumps({'pid': os.getpid(), "
    "'pgid': os.getpgrp()}), encoding='utf-8')\n"
    f"grandchild_code = {grandchild_code!r}\n"
    "nested_lifecycle.run_command(\n"
    "    (sys.executable, '-c', grandchild_code, sys.argv[2], sys.argv[3]),\n"
    "    env=os.environ.copy(),\n"
    "    cwd=Path.cwd(),\n"
    "    timeout=300.0,\n"
    "    check=True,\n"
    ")\n"
)

def fake_start(self, *, runner):
    runner(
        (
            sys.executable,
            "-c",
            child_code,
            str(child_state),
            str(grandchild_state),
            str(mutation_path),
        ),
        env=self.environment,
        cwd=repo,
        timeout=0.5 if mode == "timeout" else 300.0,
        check=True,
        input_bytes=None,
    )
    raise AssertionError("persistent startup command unexpectedly returned")

def observed_cleanup(self, *, runner, upgrade=False):
    del upgrade
    child = json.loads(child_state.read_text(encoding="utf-8"))
    grandchild = json.loads(grandchild_state.read_text(encoding="utf-8"))

    def alive(pid):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True

    try:
        os.killpg(child["pgid"], 0)
    except ProcessLookupError:
        group_alive = False
    else:
        group_alive = True
    before = mutation_path.stat().st_size
    time.sleep(0.15)
    after = mutation_path.stat().st_size
    cleanup_process = runner(
        (
            sys.executable,
            "-c",
            "import json,os; print(json.dumps({'pid': os.getpid(), "
            "'pgid': os.getpgrp()}))",
        ),
        env=self.environment,
        cwd=repo,
        timeout=5.0,
        check=True,
        input_bytes=None,
    )
    cleanup_state.write_text(
        json.dumps(
            {
                "child_alive": alive(child["pid"]),
                "grandchild_alive": alive(grandchild["pid"]),
                "group_alive": group_alive,
                "mutation_stable": before == after,
                "cleanup_process": json.loads(cleanup_process.stdout),
            }
        ),
        encoding="utf-8",
    )

lifecycle.select_live_authority = lambda *, repo, mode: authority
lifecycle.ComposeAuthority.start_stack = fake_start
lifecycle.ComposeAuthority.down_and_cleanup = observed_cleanup
try:
    lifecycle.main(("up", "--mode", "rootful"))
finally:
    authority.remove_runtime_paths()
"""
    process = subprocess.Popen(
        (sys.executable, "-c", script, mode, str(tmp_path), str(repo), token),
        cwd=repo,
        env=os.environ.copy(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    child_state = tmp_path / "persistent-child.json"
    grandchild_state = tmp_path / "persistent-grandchild.json"
    mutation_path = tmp_path / "persistent-mutations"
    deadline = time.monotonic() + 10.0
    while (
        not (child_state.is_file() and grandchild_state.is_file() and mutation_path.is_file())
        and process.poll() is None
        and time.monotonic() < deadline
    ):
        time.sleep(0.02)
    if not (child_state.is_file() and grandchild_state.is_file() and mutation_path.is_file()):
        stdout, stderr = process.communicate(timeout=5.0)
        pytest.fail(f"persistent startup descendants did not start: {stdout=} {stderr=}")
    return process, child_state, lease


def _spawn_authority_selection_signal_probe(
    tmp_path: Path,
    *,
    operation: str,
) -> tuple[subprocess.Popen[str], Path, Path]:
    repo = Path(__file__).resolve().parents[2]
    token = uuid.uuid4().hex[:12]
    lease = lease_path_for(repo)
    assert not lease.exists() and not lease.is_symlink()
    script = r"""
import json
import os
import signal
import sys
from pathlib import Path

from tests.integration import phase10_upgrade_harness as lifecycle

operation = sys.argv[1]
root = Path(sys.argv[2])
repo = Path(sys.argv[3])
token = sys.argv[4]
authority_state = root / "selection-authority.json"

def signal_after_selection(*, repo, mode):
    authority = lifecycle.ComposeAuthority.create(
        repo=repo,
        mode=mode,
        socket_path=Path("/var/run/docker.sock"),
        socket_gid=123,
        token=token,
        source_environment={"PATH": os.environ.get("PATH", "/usr/bin")},
    )
    authority_state.write_text(
        json.dumps(
            {
                "runtime": str(authority.runtime_dir),
                "barrier": str(authority.barrier_root),
            }
        ),
        encoding="utf-8",
    )
    os.kill(os.getpid(), signal.SIGTERM)
    return authority

lifecycle.select_live_authority = signal_after_selection
if operation == "run":
    lifecycle.main(("run", "--mode", "rootful", "--scenario", "wrong-gid"))
elif operation == "persistent-up":
    lifecycle.main(("up", "--mode", "rootful"))
else:
    raise AssertionError(f"unknown operation: {operation}")
"""
    process = subprocess.Popen(
        (sys.executable, "-c", script, operation, str(tmp_path), str(repo), token),
        cwd=repo,
        env=os.environ.copy(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    authority_state = tmp_path / "selection-authority.json"
    deadline = time.monotonic() + 10.0
    while not authority_state.is_file() and process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.02)
    if not authority_state.is_file():
        stdout, stderr = process.communicate(timeout=5.0)
        pytest.fail(f"selection authority was not allocated: {stdout=} {stderr=}")
    return process, authority_state, lease


def _spawn_persistent_command_probe(
    tmp_path: Path,
    *,
    operation: str,
    failure: str,
) -> tuple[subprocess.Popen[str], Path, Path, Path]:
    repo = Path(__file__).resolve().parents[2]
    token = uuid.uuid4().hex[:12]
    lease = lease_path_for(repo)
    assert not lease.exists() and not lease.is_symlink()
    script = r"""
import json
import os
import sys
from pathlib import Path

from tests.integration import phase10_upgrade_harness as lifecycle

operation = sys.argv[1]
failure = sys.argv[2]
root = Path(sys.argv[3])
repo = Path(sys.argv[4])
token = sys.argv[5]
child_state = root / "persistent-command-child.json"
grandchild_state = root / "persistent-command-grandchild.json"
mutation_path = root / "persistent-command-mutations"
authority_state = root / "persistent-command-authority.json"

authority = lifecycle.ComposeAuthority.create(
    repo=repo,
    mode="rootful",
    socket_path=Path("/var/run/docker.sock"),
    socket_gid=123,
    token=token,
    source_environment={"PATH": os.environ.get("PATH", "/usr/bin")},
)
lifecycle.write_authority_lease(authority, lifecycle.lease_path_for(repo))
authority_state.write_text(
    json.dumps(
        {"runtime": str(authority.runtime_dir), "barrier": str(authority.barrier_root)}
    ),
    encoding="utf-8",
)

grandchild_code = (
    "import json,os,signal,sys,time\n"
    "from pathlib import Path\n"
    "state = Path(sys.argv[1])\n"
    "mutations = Path(sys.argv[2])\n"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "state.write_text(json.dumps({'pid': os.getpid(), 'pgid': os.getpgrp()}), "
    "encoding='utf-8')\n"
    "while True:\n"
    "    with mutations.open('a', encoding='utf-8') as stream:\n"
    "        stream.write('mutation\\n')\n"
    "        stream.flush()\n"
    "        os.fsync(stream.fileno())\n"
    "    time.sleep(0.02)\n"
)
child_code = (
    "import json,os,sys\n"
    "from pathlib import Path\n"
    "from tests.integration import phase10_upgrade_harness as nested_lifecycle\n"
    "Path(sys.argv[1]).write_text(json.dumps({'pid': os.getpid(), "
    "'pgid': os.getpgrp()}), encoding='utf-8')\n"
    f"grandchild_code = {grandchild_code!r}\n"
    "nested_lifecycle.run_command(\n"
    "    (sys.executable, '-c', grandchild_code, sys.argv[2], sys.argv[3]),\n"
    "    env=os.environ.copy(),\n"
    "    cwd=Path.cwd(),\n"
    "    timeout=300.0,\n"
    "    check=True,\n"
    ")\n"
)
child_command = (
    sys.executable,
    "-c",
    child_code,
    str(child_state),
    str(grandchild_state),
    str(mutation_path),
)

def run_child(self, *, runner, upgrade=False):
    del upgrade
    runner(
        child_command,
        env=self.environment,
        cwd=repo,
        timeout=0.5 if failure == "timeout" else 300.0,
        check=True,
        input_bytes=None,
    )

def compose_child(self, *args, upgrade=False):
    del self, args, upgrade
    return child_command

lifecycle.ComposeAuthority.down_and_cleanup = run_child
lifecycle.ComposeAuthority.compose_command = compose_child
if failure == "timeout":
    lifecycle._PERSISTENT_COMPOSE_TIMEOUT_SECONDS = 0.5
if operation == "down":
    lifecycle.main(("down",))
elif operation == "compose":
    lifecycle.main(
        ("compose", "--", "run", "--rm", "--no-deps", "api", "jhin-db-migrate")
    )
else:
    raise AssertionError(f"unknown persistent operation: {operation}")
"""
    process = subprocess.Popen(
        (
            sys.executable,
            "-c",
            script,
            operation,
            failure,
            str(tmp_path),
            str(repo),
            token,
        ),
        cwd=repo,
        env=os.environ.copy(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    child_state = tmp_path / "persistent-command-child.json"
    grandchild_state = tmp_path / "persistent-command-grandchild.json"
    mutation_path = tmp_path / "persistent-command-mutations"
    deadline = time.monotonic() + 10.0
    while (
        not (child_state.is_file() and grandchild_state.is_file() and mutation_path.is_file())
        and process.poll() is None
        and time.monotonic() < deadline
    ):
        time.sleep(0.02)
    if not (child_state.is_file() and grandchild_state.is_file() and mutation_path.is_file()):
        stdout, stderr = process.communicate(timeout=5.0)
        pytest.fail(f"persistent command descendants did not start: {stdout=} {stderr=}")
    return process, child_state, mutation_path, lease


def _spawn_persistent_cleanup_handoff_probe(
    tmp_path: Path,
) -> tuple[subprocess.Popen[str], Path, Path]:
    repo = Path(__file__).resolve().parents[2]
    token = uuid.uuid4().hex[:12]
    lease = lease_path_for(repo)
    assert not lease.exists() and not lease.is_symlink()
    script = r"""
import json
import os
import signal
import sys
from pathlib import Path

from tests.integration import phase10_upgrade_harness as lifecycle

root = Path(sys.argv[1])
repo = Path(sys.argv[2])
token = sys.argv[3]
startup_failed = root / "persistent-handoff-startup-failed"
cleanup_finished = root / "persistent-handoff-cleanup-finished"
authority_state = root / "persistent-handoff-authority.json"

authority = lifecycle.ComposeAuthority.create(
    repo=repo,
    mode="rootful",
    socket_path=Path("/var/run/docker.sock"),
    socket_gid=123,
    token=token,
    source_environment={"PATH": os.environ.get("PATH", "/usr/bin")},
)
authority_state.write_text(
    json.dumps(
        {"runtime": str(authority.runtime_dir), "barrier": str(authority.barrier_root)}
    ),
    encoding="utf-8",
)

def select_authority(*, repo, mode):
    del repo, mode
    return authority

def fail_start(self, *, runner):
    del self, runner
    startup_failed.write_text("failed", encoding="utf-8")
    raise RuntimeError("synthetic persistent startup failure")

def conclusive_cleanup(self, *, runner, upgrade=False):
    del runner, upgrade
    self.remove_runtime_paths()
    cleanup_finished.write_text("finished", encoding="utf-8")

lifecycle.select_live_authority = select_authority
lifecycle.ComposeAuthority.start_stack = fail_start
lifecycle.ComposeAuthority.down_and_cleanup = conclusive_cleanup
real_signal = lifecycle.signal.signal
transition_injected = False

def inject_signal_during_cleanup_handoff(signum, handler):
    global transition_injected
    result = real_signal(signum, handler)
    if (
        not transition_injected
        and handler is signal.SIG_IGN
        and startup_failed.is_file()
    ):
        transition_injected = True
        os.kill(os.getpid(), signal.SIGTERM)
    return result

lifecycle.signal.signal = inject_signal_during_cleanup_handoff
lifecycle.main(("up", "--mode", "rootful"))
"""
    process = subprocess.Popen(
        (sys.executable, "-c", script, str(tmp_path), str(repo), token),
        cwd=repo,
        env=os.environ.copy(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    authority_state = tmp_path / "persistent-handoff-authority.json"
    deadline = time.monotonic() + 10.0
    while not authority_state.is_file() and process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.02)
    if not authority_state.is_file():
        stdout, stderr = process.communicate(timeout=5.0)
        pytest.fail(f"persistent handoff authority did not start: {stdout=} {stderr=}")
    return process, authority_state, lease


def _finish_persistent_start_probe(
    process: subprocess.Popen[str],
    child_state_path: Path,
    lease: Path,
) -> tuple[str, str]:
    child_group: int | None = None
    try:
        return process.communicate(timeout=10.0)
    finally:
        if child_state_path.is_file():
            child_group = int(json.loads(child_state_path.read_text(encoding="utf-8"))["pgid"])
            _signal_test_process_group(child_group)
        if process.poll() is None:
            _signal_test_process_group(process.pid)
            process.wait(timeout=5.0)
        _wait_for_test_process_group_exit(process.pid)
        if child_group is not None and child_group != process.pid:
            _wait_for_test_process_group_exit(child_group)
        lease.unlink(missing_ok=True)


def _signal_test_process_group(process_group: int) -> None:
    assert process_group > 1
    assert process_group != os.getpgrp()
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(process_group, signal.SIGKILL)


def _wait_for_test_process_group_exit(process_group: int) -> None:
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            os.killpg(process_group, 0)
        except ProcessLookupError:
            return
        except PermissionError:
            pass
        _signal_test_process_group(process_group)
        time.sleep(0.02)
    raise AssertionError(f"test process group {process_group} survived emergency cleanup")


def _owned_child_timeout_command(tmp_path: Path) -> tuple[str, ...]:
    child_state = tmp_path / "timeout-child.json"
    grandchild_state = tmp_path / "timeout-grandchild.json"
    mutation_path = tmp_path / "timeout-mutations"
    grandchild_code = (
        "import json,os,signal,sys,time\n"
        "from pathlib import Path\n"
        "state = Path(sys.argv[1])\n"
        "mutations = Path(sys.argv[2])\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "state.write_text(json.dumps({'pid': os.getpid(), 'pgid': os.getpgrp()}), "
        "encoding='utf-8')\n"
        "while True:\n"
        "    with mutations.open('a', encoding='utf-8') as stream:\n"
        "        stream.write('mutation\\n')\n"
        "        stream.flush()\n"
        "        os.fsync(stream.fileno())\n"
        "    time.sleep(0.02)\n"
    )
    child_code = (
        "import json,os,sys\n"
        "from pathlib import Path\n"
        "from tests.integration import phase10_upgrade_harness as nested_lifecycle\n"
        "Path(sys.argv[1]).write_text(json.dumps({'pid': os.getpid(), "
        "'pgid': os.getpgrp()}), encoding='utf-8')\n"
        "print('child-ready', flush=True)\n"
        f"grandchild_code = {grandchild_code!r}\n"
        "nested_lifecycle.run_command(\n"
        "    (sys.executable, '-c', grandchild_code, sys.argv[2], sys.argv[3]),\n"
        "    env=os.environ.copy(),\n"
        "    cwd=Path.cwd(),\n"
        "    timeout=300.0,\n"
        "    check=True,\n"
        ")\n"
    )
    return (
        sys.executable,
        "-c",
        child_code,
        str(child_state),
        str(grandchild_state),
        str(mutation_path),
    )


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _fork_phase10_operation(callback: Any, error_path: Path) -> int:
    pid = os.fork()
    if pid != 0:
        return pid
    try:
        callback()
    except BaseException as error:
        error_path.write_text(repr(error), encoding="utf-8")
        os._exit(1)
    os._exit(0)


def _wait_phase10_operation(pid: int, error_path: Path) -> None:
    waited, status = os.waitpid(pid, 0)
    assert waited == pid
    detail = error_path.read_text(encoding="utf-8") if error_path.exists() else ""
    assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0, detail


def _wait_for_path(path: Path, *, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert path.exists(), f"timed out waiting for {path}"


def test_owned_live_child_timeout_reaps_nested_process_group(tmp_path: Path) -> None:
    command = _owned_child_timeout_command(tmp_path)
    child_state_path = tmp_path / "timeout-child.json"
    grandchild_state_path = tmp_path / "timeout-grandchild.json"
    mutation_path = tmp_path / "timeout-mutations"
    child_group: int | None = None
    try:
        with pytest.raises(subprocess.TimeoutExpired) as raised:
            lifecycle.run_owned_command(
                command,
                env=os.environ.copy(),
                cwd=Path(__file__).resolve().parents[2],
                timeout=1.0,
                check=True,
            )
        assert "child-ready" in cast(str, raised.value.stdout)
        child = json.loads(child_state_path.read_text(encoding="utf-8"))
        grandchild = json.loads(grandchild_state_path.read_text(encoding="utf-8"))
        child_group = int(child["pgid"])
        assert child_group == int(child["pid"])
        assert int(grandchild["pgid"]) == child_group
        assert not _pid_exists(int(child["pid"]))
        assert not _pid_exists(int(grandchild["pid"]))
        with pytest.raises(ProcessLookupError):
            os.killpg(child_group, 0)
        before = mutation_path.stat().st_size
        time.sleep(0.15)
        assert mutation_path.stat().st_size == before
    finally:
        if child_group is None and child_state_path.is_file():
            child_group = int(json.loads(child_state_path.read_text(encoding="utf-8"))["pgid"])
        if child_group is not None:
            _signal_test_process_group(child_group)
            _wait_for_test_process_group_exit(child_group)


def test_owned_live_child_preserves_output_and_check_semantics(tmp_path: Path) -> None:
    command = (
        sys.executable,
        "-c",
        "import sys; print('stdout-value'); print('stderr-value', file=sys.stderr); "
        "raise SystemExit(7)",
    )
    result = lifecycle.run_owned_command(
        command,
        env=os.environ.copy(),
        cwd=tmp_path,
        timeout=5.0,
        check=False,
    )
    assert result.returncode == 7
    assert result.stdout == "stdout-value\n"
    assert result.stderr == "stderr-value\n"

    with pytest.raises(subprocess.CalledProcessError) as raised:
        lifecycle.run_owned_command(
            command,
            env=os.environ.copy(),
            cwd=tmp_path,
            timeout=5.0,
            check=True,
        )
    assert raised.value.returncode == 7
    assert raised.value.stdout == "stdout-value\n"
    assert raised.value.stderr == "stderr-value\n"


def test_live_child_failure_output_is_bounded_and_redacted(
    capsys: pytest.CaptureFixture[str],
) -> None:
    master_key = "phase10-sensitive-master-key"
    runner_token = "phase10-sensitive-runner-token"
    reader_dsn = "postgresql://reader:phase10-sensitive-password@127.0.0.1:5432/jhin"
    stdout = "x" * (lifecycle.LIVE_FAILURE_OUTPUT_LIMIT + 20)
    stderr = "y" * (lifecycle.LIVE_FAILURE_OUTPUT_LIMIT + 20)
    error = subprocess.CalledProcessError(
        1,
        ("pytest",),
        output=f"{stdout}\nFAILED boundary-node {master_key} {reader_dsn}\n",
        stderr=f"{stderr}\nassertion detail {runner_token}\n",
    )

    lifecycle.emit_live_child_failure_output(
        error,
        environment={
            "JHIN_MASTER_KEY": master_key,
            "JHIN_PHASE9_DB_READER_DSN": reader_dsn,
            "SANDBOX_RUNNER_TOKEN": runner_token,
        },
    )

    captured = capsys.readouterr()
    emitted = captured.out + captured.err
    assert master_key not in emitted
    assert reader_dsn not in emitted
    assert runner_token not in emitted
    assert "<redacted>" in emitted
    assert "FAILED boundary-node" in emitted
    assert "assertion detail" in emitted
    stdout_marker = "--- live pytest stdout (redacted tail) ---\n"
    stderr_marker = "--- live pytest stderr (redacted tail) ---\n"
    stdout_emitted = emitted.split(stdout_marker, 1)[1].split(stderr_marker, 1)[0]
    stderr_emitted = emitted.split(stderr_marker, 1)[1]
    assert len(stdout_emitted) <= lifecycle.LIVE_FAILURE_OUTPUT_LIMIT + 1
    assert len(stderr_emitted) <= lifecycle.LIVE_FAILURE_OUTPUT_LIMIT + 1


def test_owned_child_restores_the_callers_exact_signal_mask(tmp_path: Path) -> None:
    catchable = {signal.SIGINT, signal.SIGTERM}
    original_mask = signal.pthread_sigmask(signal.SIG_BLOCK, set())
    original_handlers = {
        signum: signal.getsignal(signum) for signum in (signal.SIGINT, signal.SIGTERM)
    }
    caller_mask = (original_mask - catchable) | {signal.SIGUSR1}
    signal.pthread_sigmask(signal.SIG_SETMASK, caller_mask)
    for signum in catchable:
        signal.signal(signum, signal.SIG_IGN)
    command = (
        sys.executable,
        "-c",
        "import json,signal\n"
        "def describe(item):\n"
        "    handler = signal.getsignal(item)\n"
        "    if handler is signal.default_int_handler:\n"
        "        return 'default_int_handler'\n"
        "    return int(handler)\n"
        "print(json.dumps({'mask': sorted(int(item) for item in "
        "signal.pthread_sigmask(signal.SIG_BLOCK, set())), 'handlers': [describe("
        "item) for item in (signal.SIGINT, signal.SIGTERM)]}))",
    )
    try:
        result = lifecycle.run_owned_command(
            command,
            env=os.environ.copy(),
            cwd=tmp_path,
            timeout=5.0,
            check=True,
        )
        restored_parent_mask = signal.pthread_sigmask(signal.SIG_BLOCK, set())
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, original_mask)
        for signum, handler in original_handlers.items():
            signal.signal(signum, handler)

    child_state = json.loads(cast(str, result.stdout))
    assert child_state == {
        "mask": sorted(int(item) for item in caller_mask),
        "handlers": ["default_int_handler", int(signal.SIG_DFL)],
    }
    assert restored_parent_mask == caller_mask


def test_owned_timeout_delivers_term_handler_before_considering_kill(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    child_state = tmp_path / "term-grace-child.json"
    term_marker = tmp_path / "term-grace-handler"
    delivered: list[int] = []
    real_signal_group = lifecycle._signal_owned_process_group

    def record_signal(process_group: int, signum: int) -> None:
        delivered.append(signum)
        real_signal_group(process_group, signum)

    monkeypatch.setattr(lifecycle, "_signal_owned_process_group", record_signal)
    child_code = (
        "import json,os,signal,sys,time\n"
        "from pathlib import Path\n"
        "state = Path(sys.argv[1])\n"
        "marker = Path(sys.argv[2])\n"
        "def terminate(signum, frame):\n"
        "    del signum, frame\n"
        "    marker.write_text('term', encoding='utf-8')\n"
        "    raise SystemExit(0)\n"
        "signal.signal(signal.SIGTERM, terminate)\n"
        "state.write_text(json.dumps({'pid': os.getpid(), 'pgid': os.getpgrp()}), "
        "encoding='utf-8')\n"
        "while True:\n"
        "    time.sleep(0.02)\n"
    )
    child_group: int | None = None
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            lifecycle.run_owned_command(
                (sys.executable, "-c", child_code, str(child_state), str(term_marker)),
                env=os.environ.copy(),
                cwd=tmp_path,
                timeout=1.0,
                check=True,
            )
        state = json.loads(child_state.read_text(encoding="utf-8"))
        child_group = int(state["pgid"])
        assert child_group == int(state["pid"])
        assert term_marker.read_text(encoding="utf-8") == "term"
        assert delivered == [signal.SIGTERM]
        with pytest.raises(ProcessLookupError):
            os.killpg(child_group, 0)
    finally:
        if child_group is None and child_state.is_file():
            child_group = int(json.loads(child_state.read_text(encoding="utf-8"))["pgid"])
        if child_group is not None:
            _signal_test_process_group(child_group)
            _wait_for_test_process_group_exit(child_group)


@pytest.mark.parametrize("signum", (signal.SIGINT, signal.SIGTERM))
def test_owned_spawn_window_defers_signal_until_group_can_be_exhausted(
    tmp_path: Path,
    signum: int,
) -> None:
    process, child_state_path, lease = _spawn_popen_window_probe(
        tmp_path,
        signum=signum,
    )
    child_group: int | None = None
    try:
        stdout, stderr = process.communicate(timeout=10.0)
        assert process.returncode == 128 + signum, (stdout, stderr)
        child = json.loads(child_state_path.read_text(encoding="utf-8"))
        child_group = int(child["pgid"])
        assert child_group == int(child["pid"])
        cleanup = json.loads((tmp_path / "spawn-window-cleanup.json").read_text(encoding="utf-8"))
        assert cleanup == {
            "child_alive": False,
            "grandchild_alive": False,
            "group_alive": False,
            "mutation_stable": True,
        }
        with pytest.raises(ProcessLookupError):
            os.killpg(child_group, 0)
        assert not lease.exists()
    finally:
        if child_group is None and child_state_path.is_file():
            child_group = int(json.loads(child_state_path.read_text(encoding="utf-8"))["pgid"])
        if child_group is not None:
            _signal_test_process_group(child_group)
            _wait_for_test_process_group_exit(child_group)
        if process.poll() is None:
            _signal_test_process_group(process.pid)
            process.wait(timeout=5.0)
        lease.unlink(missing_ok=True)


def test_owned_spawn_failure_restores_catchable_signal_mask(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catchable = {signal.SIGINT, signal.SIGTERM}
    original_mask = signal.pthread_sigmask(signal.SIG_BLOCK, set())
    observed_spawn_masks: list[set[int | signal.Signals]] = []

    def failing_popen(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        observed_spawn_masks.append(signal.pthread_sigmask(signal.SIG_BLOCK, set()))
        raise OSError("synthetic Popen failure")

    monkeypatch.setattr(subprocess, "Popen", failing_popen)
    try:
        with pytest.raises(OSError, match="synthetic Popen failure"):
            lifecycle.run_owned_command(
                (sys.executable, "-c", "raise AssertionError('not started')"),
                env=os.environ.copy(),
                cwd=Path(__file__).resolve().parents[2],
                timeout=5.0,
                check=True,
            )
        restored_mask = signal.pthread_sigmask(signal.SIG_BLOCK, set())
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, original_mask)

    assert len(observed_spawn_masks) == 1
    assert catchable <= observed_spawn_masks[0]
    assert restored_mask == original_mask


@pytest.mark.parametrize("rollback_fails", (False, True))
def test_atomic_handler_transition_rolls_back_or_keeps_signals_blocked(
    monkeypatch: pytest.MonkeyPatch,
    rollback_fails: bool,
) -> None:
    caught = (signal.SIGINT, signal.SIGTERM)
    original_mask = signal.pthread_sigmask(signal.SIG_BLOCK, set())
    original_handlers = {signum: signal.getsignal(signum) for signum in caught}
    signal.pthread_sigmask(signal.SIG_UNBLOCK, set(caught))
    real_signal = signal.signal
    target_failed = False
    rollback_failed = False

    def fail_second_target_then_optional_rollback(signum: int, handler: Any) -> Any:
        nonlocal target_failed, rollback_failed
        if signum == signal.SIGTERM and handler is signal.SIG_IGN and not target_failed:
            target_failed = True
            raise RuntimeError("synthetic second-handler transition failure")
        if (
            rollback_fails
            and target_failed
            and not rollback_failed
            and signum == signal.SIGINT
            and handler == original_handlers[signal.SIGINT]
        ):
            rollback_failed = True
            raise RuntimeError("synthetic handler rollback failure")
        return real_signal(signum, handler)

    monkeypatch.setattr(signal, "signal", fail_second_target_then_optional_rollback)
    try:
        with pytest.raises(BaseExceptionGroup, match="handler transition"):
            lifecycle._atomic_signal_handler_transition(dict.fromkeys(caught, signal.SIG_IGN))
        observed_mask = signal.pthread_sigmask(signal.SIG_BLOCK, set())
        observed_handlers = {signum: signal.getsignal(signum) for signum in caught}
        if rollback_fails:
            assert set(caught) <= observed_mask
        else:
            assert observed_mask == original_mask
            assert observed_handlers == original_handlers
    finally:
        monkeypatch.setattr(signal, "signal", real_signal)
        for signum, handler in original_handlers.items():
            real_signal(signum, handler)
        signal.pthread_sigmask(signal.SIG_SETMASK, original_mask)


def test_signal_lifecycle_prepare_rollback_failure_keeps_signals_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caught = (signal.SIGINT, signal.SIGTERM)
    original_mask = signal.pthread_sigmask(signal.SIG_BLOCK, set())
    original_handlers = {signum: signal.getsignal(signum) for signum in caught}
    signal.pthread_sigmask(signal.SIG_UNBLOCK, set(caught))
    real_signal = signal.signal
    target_failed = False
    rollback_failed = False

    def fail_install_and_rollback(signum: int, handler: Any) -> Any:
        nonlocal target_failed, rollback_failed
        if (
            signum == signal.SIGTERM
            and handler != original_handlers[signal.SIGTERM]
            and not target_failed
        ):
            target_failed = True
            raise RuntimeError("synthetic lifecycle install failure")
        if (
            signum == signal.SIGINT
            and handler == original_handlers[signal.SIGINT]
            and target_failed
            and not rollback_failed
        ):
            rollback_failed = True
            raise RuntimeError("synthetic lifecycle rollback failure")
        return real_signal(signum, handler)

    monkeypatch.setattr(signal, "signal", fail_install_and_rollback)
    try:
        with pytest.raises(BaseExceptionGroup, match="installation and restoration"):
            lifecycle._CatchableSignalLifecycle.prepare()
        assert set(caught) <= signal.pthread_sigmask(signal.SIG_BLOCK, set())
    finally:
        monkeypatch.setattr(signal, "signal", real_signal)
        for signum, handler in original_handlers.items():
            real_signal(signum, handler)
        signal.pthread_sigmask(signal.SIG_SETMASK, original_mask)


def test_prepared_signal_lifecycle_restore_failure_keeps_signals_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caught = (signal.SIGINT, signal.SIGTERM)
    original_mask = signal.pthread_sigmask(signal.SIG_BLOCK, set())
    original_handlers = {signum: signal.getsignal(signum) for signum in caught}
    signal.pthread_sigmask(signal.SIG_UNBLOCK, set(caught))
    signal_lifecycle = lifecycle._CatchableSignalLifecycle.prepare()
    real_signal = signal.signal
    failed = False

    def fail_second_restore(signum: int, handler: Any) -> Any:
        nonlocal failed
        if signum == signal.SIGTERM and handler == original_handlers[signal.SIGTERM] and not failed:
            failed = True
            raise RuntimeError("synthetic prepared lifecycle restore failure")
        return real_signal(signum, handler)

    monkeypatch.setattr(signal, "signal", fail_second_restore)
    try:
        with pytest.raises(BaseExceptionGroup, match="prepared catchable"):
            signal_lifecycle.restore()
        assert set(caught) <= signal.pthread_sigmask(signal.SIG_BLOCK, set())
    finally:
        monkeypatch.setattr(signal, "signal", real_signal)
        for signum, handler in original_handlers.items():
            real_signal(signum, handler)
        signal.pthread_sigmask(signal.SIG_SETMASK, original_mask)


def test_post_communicate_signal_exhausts_same_group_descendant_before_cleanup(
    tmp_path: Path,
) -> None:
    process, descendant_state_path, lease = _spawn_post_communicate_signal_probe(tmp_path)
    descendant_group: int | None = None
    try:
        stdout, stderr = process.communicate(timeout=10.0)
        assert process.returncode == 128 + signal.SIGTERM, (stdout, stderr)
        descendant = json.loads(descendant_state_path.read_text(encoding="utf-8"))
        descendant_group = int(descendant["pgid"])
        cleanup = json.loads(
            (tmp_path / "post-communicate-cleanup.json").read_text(encoding="utf-8")
        )
        assert cleanup == {"descendant_alive": False, "group_alive": False}
        with pytest.raises(ProcessLookupError):
            os.killpg(descendant_group, 0)
        assert not lease.exists()
    finally:
        if descendant_group is None and descendant_state_path.is_file():
            descendant_group = int(
                json.loads(descendant_state_path.read_text(encoding="utf-8"))["pgid"]
            )
        if descendant_group is not None:
            _signal_test_process_group(descendant_group)
            _wait_for_test_process_group_exit(descendant_group)
        if process.poll() is None:
            _signal_test_process_group(process.pid)
            process.wait(timeout=5.0)
        lease.unlink(missing_ok=True)


@pytest.mark.parametrize(
    ("mode", "signal_number", "expected_returncode"),
    (
        ("signal", signal.SIGTERM, 128 + signal.SIGTERM),
        ("timeout", None, 1),
    ),
)
def test_persistent_cli_start_exhausts_nested_group_before_failure_cleanup(
    tmp_path: Path,
    mode: str,
    signal_number: int | None,
    expected_returncode: int,
) -> None:
    process, child_state_path, lease = _spawn_persistent_start_probe(tmp_path, mode=mode)
    if signal_number is not None:
        os.kill(process.pid, signal_number)
    try:
        stdout, stderr = _finish_persistent_start_probe(process, child_state_path, lease)
        assert process.returncode == expected_returncode, (stdout, stderr)
        if mode == "timeout":
            assert "TimeoutExpired" in stderr
        cleanup = json.loads((tmp_path / "persistent-cleanup.json").read_text(encoding="utf-8"))
        assert cleanup["child_alive"] is False
        assert cleanup["grandchild_alive"] is False
        assert cleanup["group_alive"] is False
        assert cleanup["mutation_stable"] is True
        assert cleanup["cleanup_process"]["pid"] == cleanup["cleanup_process"]["pgid"]
        assert not lease.exists()
    finally:
        lease.unlink(missing_ok=True)


def test_persistent_start_withholds_cleanup_for_unexhausted_process_group(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    (tmp_path / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    authority = ComposeAuthority.create(
        repo=tmp_path,
        mode="rootful",
        socket_path=Path("/var/run/docker.sock"),
        socket_gid=123,
        token=uuid.uuid4().hex[:12],
        source_environment={"PATH": os.environ.get("PATH", "/usr/bin")},
    )
    cleanup_calls: list[bool] = []
    monkeypatch.setattr(lifecycle, "select_live_authority", lambda **kwargs: authority)
    monkeypatch.setattr(
        ComposeAuthority,
        "start_stack",
        lambda self, *, runner: (_ for _ in ()).throw(lifecycle._OwnedProcessGroupSurvived(4545)),
    )
    monkeypatch.setattr(
        ComposeAuthority,
        "down_and_cleanup",
        lambda self, *, runner, upgrade=False: cleanup_calls.append(upgrade),
    )
    try:
        with pytest.raises(RuntimeError, match=r"4545.*survived"):
            lifecycle._persistent_up(repo=tmp_path, mode="rootful")
        assert cleanup_calls == []
        lease = lease_path_for(tmp_path)
        assert lease.is_file()
        loaded = read_authority_lease(lease, expected_repo=tmp_path)
        assert loaded.project == authority.project
        assert loaded.runtime_dir == authority.runtime_dir
        assert loaded.published_ports == {}
    finally:
        lease_path_for(tmp_path).unlink(missing_ok=True)
        authority.remove_runtime_paths()


def test_persistent_down_unlinks_lease_after_conclusive_nested_signal_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    (tmp_path / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    authority = ComposeAuthority.create(
        repo=tmp_path,
        mode="rootful",
        socket_path=Path("/var/run/docker.sock"),
        socket_gid=123,
        token=uuid.uuid4().hex[:12],
        source_environment={"PATH": os.environ.get("PATH", "/usr/bin")},
    )
    lease = lease_path_for(tmp_path)
    write_authority_lease(authority, lease)

    def conclusive_interrupted_cleanup(
        self: ComposeAuthority,
        *,
        runner: Any,
        upgrade: bool = False,
    ) -> None:
        del runner, upgrade
        self.remove_runtime_paths()
        raise BaseExceptionGroup(
            "cleanup recorded an interrupted diagnostic",
            [lifecycle._LifecycleSignal(signal.SIGTERM)],
        )

    monkeypatch.setattr(ComposeAuthority, "down_and_cleanup", conclusive_interrupted_cleanup)
    try:
        with pytest.raises(SystemExit) as raised:
            lifecycle._persistent_down(repo=tmp_path)
        assert raised.value.code == 128 + signal.SIGTERM
        assert not lease.exists()
        assert not authority.runtime_dir.exists()
        assert not authority.barrier_root.exists()
    finally:
        lease.unlink(missing_ok=True)
        authority.remove_runtime_paths()


def test_persistent_down_indeterminate_cleanup_retains_readable_recovery_authority(
    tmp_path: Path,
) -> None:
    (tmp_path / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    authority = ComposeAuthority.create(
        repo=tmp_path,
        mode="rootful",
        socket_path=Path("/var/run/docker.sock"),
        socket_gid=123,
        token=uuid.uuid4().hex[:12],
        source_environment={"PATH": os.environ.get("PATH", "/usr/bin")},
    )
    authority.master_key_path.write_bytes(b"d" * 32)
    authority.master_key_path.chmod(0o400)
    lease = lease_path_for(tmp_path)
    write_authority_lease(authority, lease)

    def indeterminate_runner(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        raise TimeoutError("indeterminate Docker cleanup")

    try:
        with pytest.raises(BaseExceptionGroup, match="cleanup"):
            lifecycle._persistent_down(repo=tmp_path, runner=indeterminate_runner)
        loaded = read_authority_lease(lease, expected_repo=tmp_path)
        assert loaded.runtime_dir == authority.runtime_dir
        assert authority.runtime_dir.is_dir()
        assert authority.barrier_root.is_dir()
        assert authority.master_key_path.read_bytes() == b"d" * 32
    finally:
        lease.unlink(missing_ok=True)
        authority.remove_runtime_paths()


@pytest.mark.parametrize("failed_path_name", ("barrier_root", "runtime_dir"))
def test_persistent_down_rereads_and_retries_partial_local_path_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failed_path_name: str,
) -> None:
    (tmp_path / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    authority = ComposeAuthority.create(
        repo=tmp_path,
        mode="rootful",
        socket_path=Path("/var/run/docker.sock"),
        socket_gid=123,
        token=uuid.uuid4().hex[:12],
        source_environment={"PATH": os.environ.get("PATH", "/usr/bin")},
    )
    lease = lease_path_for(tmp_path)
    write_authority_lease(authority, lease)
    failed_path = cast(Path, getattr(authority, failed_path_name))
    real_rmtree = shutil.rmtree
    injected = False

    def fail_one_local_path(path: Any, *args: Any, **kwargs: Any) -> None:
        nonlocal injected
        if Path(path) == failed_path and not injected:
            injected = True
            raise OSError(f"injected {failed_path_name} removal failure")
        real_rmtree(path, *args, **kwargs)

    def local_cleanup_only(
        self: ComposeAuthority,
        *,
        runner: Any,
        upgrade: bool = False,
    ) -> None:
        del runner, upgrade
        self.remove_recovery_paths()

    monkeypatch.setattr(shutil, "rmtree", fail_one_local_path)
    monkeypatch.setattr(ComposeAuthority, "down_and_cleanup", local_cleanup_only)
    try:
        with pytest.raises(BaseExceptionGroup, match="runtime paths"):
            lifecycle._persistent_down(repo=tmp_path)
        assert injected is True
        assert lease.is_file()
        assert failed_path.exists()
        with pytest.raises(FileNotFoundError):
            read_authority_lease(lease, expected_repo=tmp_path)

        lifecycle._persistent_down(repo=tmp_path)
        assert not lease.exists()
        assert not authority.barrier_root.exists()
        assert not authority.runtime_dir.exists()
    finally:
        lease.unlink(missing_ok=True)
        lifecycle.persistent_operation_lock_path(tmp_path).unlink(missing_ok=True)
        if authority.runtime_dir.exists() or authority.barrier_root.exists():
            authority.remove_runtime_paths()


def test_persistent_down_never_unlinks_a_replaced_unowned_lease(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    (tmp_path / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    authority = ComposeAuthority.create(
        repo=tmp_path,
        mode="rootful",
        socket_path=Path("/var/run/docker.sock"),
        socket_gid=123,
        token=uuid.uuid4().hex[:12],
        source_environment={"PATH": os.environ.get("PATH", "/usr/bin")},
    )
    lease = lease_path_for(tmp_path)
    write_authority_lease(authority, lease)
    replacement = b"replacement recovery owner\n"

    def replace_during_cleanup(
        self: ComposeAuthority,
        *,
        runner: Any,
        upgrade: bool = False,
    ) -> None:
        del runner, upgrade
        self.remove_runtime_paths()
        lease.unlink()
        lease.write_bytes(replacement)
        lease.chmod(0o600)

    monkeypatch.setattr(ComposeAuthority, "down_and_cleanup", replace_during_cleanup)
    try:
        with pytest.raises(BaseExceptionGroup, match="handoff"):
            lifecycle._persistent_down(repo=tmp_path)
        assert lease.read_bytes() == replacement
    finally:
        lease.unlink(missing_ok=True)
        lease.with_suffix(".lock").unlink(missing_ok=True)
        authority.remove_runtime_paths()


def test_persistent_compose_and_down_are_serialized_across_processes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    (tmp_path / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    authority = ComposeAuthority.create(
        repo=tmp_path,
        mode="rootful",
        socket_path=Path("/var/run/docker.sock"),
        socket_gid=123,
        token=uuid.uuid4().hex[:12],
        source_environment={"PATH": os.environ.get("PATH", "/usr/bin")},
    )
    lease = lease_path_for(tmp_path)
    write_authority_lease(authority, lease)
    compose_started = tmp_path / "compose-started"
    compose_release = tmp_path / "compose-release"
    down_entered = tmp_path / "down-entered"
    compose_error = tmp_path / "compose-error"
    down_error = tmp_path / "down-error"
    children: set[int] = set()

    def compose_operation() -> None:
        def blocking_runner(command: tuple[str, ...], **kwargs: Any) -> Any:
            del kwargs
            compose_started.touch()
            _wait_for_path(compose_release)
            return subprocess.CompletedProcess(command, 0, "", "")

        lifecycle._persistent_compose(
            repo=tmp_path,
            arguments=("run", "--rm", "--no-deps", "api", "jhin-db-migrate"),
            runner=blocking_runner,
        )

    def down_operation() -> None:
        def conclusive_cleanup(
            self: ComposeAuthority,
            *,
            runner: Any,
            upgrade: bool = False,
        ) -> None:
            del runner, upgrade
            down_entered.touch()
            self.remove_runtime_paths()

        monkeypatch.setattr(ComposeAuthority, "down_and_cleanup", conclusive_cleanup)
        lifecycle._persistent_down(repo=tmp_path)

    try:
        compose_pid = _fork_phase10_operation(compose_operation, compose_error)
        children.add(compose_pid)
        _wait_for_path(compose_started)
        down_pid = _fork_phase10_operation(down_operation, down_error)
        children.add(down_pid)
        time.sleep(0.25)
        assert not down_entered.exists()
        compose_release.touch()
        _wait_phase10_operation(compose_pid, compose_error)
        children.remove(compose_pid)
        _wait_phase10_operation(down_pid, down_error)
        children.remove(down_pid)
        assert down_entered.is_file()
        assert not lease.exists()
    finally:
        for pid in children:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
            with contextlib.suppress(ChildProcessError):
                os.waitpid(pid, 0)
        compose_release.touch(exist_ok=True)
        lease.unlink(missing_ok=True)
        lease.with_suffix(".lock").unlink(missing_ok=True)
        authority.remove_runtime_paths()


def test_provisional_persistent_up_and_down_are_serialized_across_processes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    (tmp_path / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    lease = lease_path_for(tmp_path)
    up_started = tmp_path / "up-started.json"
    up_release = tmp_path / "up-release"
    down_entered = tmp_path / "provisional-down-entered"
    up_error = tmp_path / "up-error"
    down_error = tmp_path / "provisional-down-error"
    children: set[int] = set()
    ports = {
        variable: 54000 + index
        for index, (variable, _service, _container_port) in enumerate(PUBLISHED_ENDPOINTS)
    }

    def up_operation() -> None:
        authority = ComposeAuthority.create(
            repo=tmp_path,
            mode="rootful",
            socket_path=Path("/var/run/docker.sock"),
            socket_gid=123,
            token=uuid.uuid4().hex[:12],
            source_environment={"PATH": os.environ.get("PATH", "/usr/bin")},
        )

        def select_authority(**kwargs: Any) -> ComposeAuthority:
            del kwargs
            return authority

        def blocking_start(self: ComposeAuthority, *, runner: Any) -> dict[str, int]:
            del runner
            up_started.write_text(
                json.dumps({"runtime": str(self.runtime_dir), "barrier": str(self.barrier_root)}),
                encoding="utf-8",
            )
            _wait_for_path(up_release)
            return ports

        def local_cleanup(
            self: ComposeAuthority,
            *,
            runner: Any,
            upgrade: bool = False,
        ) -> None:
            del runner, upgrade
            self.remove_runtime_paths()

        monkeypatch.setattr(lifecycle, "select_live_authority", select_authority)
        monkeypatch.setattr(ComposeAuthority, "start_stack", blocking_start)
        monkeypatch.setattr(ComposeAuthority, "down_and_cleanup", local_cleanup)
        lifecycle._persistent_up(repo=tmp_path, mode="rootful")

    def down_operation() -> None:
        def conclusive_cleanup(
            self: ComposeAuthority,
            *,
            runner: Any,
            upgrade: bool = False,
        ) -> None:
            del runner, upgrade
            down_entered.touch()
            self.remove_runtime_paths()

        monkeypatch.setattr(ComposeAuthority, "down_and_cleanup", conclusive_cleanup)
        lifecycle._persistent_down(repo=tmp_path)

    paths: tuple[Path, ...] = ()
    try:
        up_pid = _fork_phase10_operation(up_operation, up_error)
        children.add(up_pid)
        _wait_for_path(up_started)
        state = json.loads(up_started.read_text(encoding="utf-8"))
        paths = (Path(state["runtime"]), Path(state["barrier"]))
        down_pid = _fork_phase10_operation(down_operation, down_error)
        children.add(down_pid)
        time.sleep(0.25)
        assert not down_entered.exists()
        up_release.touch()
        _wait_phase10_operation(up_pid, up_error)
        children.remove(up_pid)
        _wait_phase10_operation(down_pid, down_error)
        children.remove(down_pid)
        assert down_entered.is_file()
        assert not lease.exists()
        assert all(not path.exists() for path in paths)
    finally:
        for pid in children:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
            with contextlib.suppress(ChildProcessError):
                os.waitpid(pid, 0)
        up_release.touch(exist_ok=True)
        lease.unlink(missing_ok=True)
        lease.with_suffix(".lock").unlink(missing_ok=True)
        for path in paths:
            if path.exists():
                os.chmod(path, 0o700, follow_symlinks=False)
                shutil.rmtree(path)


@pytest.mark.parametrize("operation", ("run", "persistent-up"))
def test_signal_after_authority_selection_removes_unpublished_runtime_paths(
    tmp_path: Path,
    operation: str,
) -> None:
    process, authority_state_path, lease = _spawn_authority_selection_signal_probe(
        tmp_path,
        operation=operation,
    )
    state = json.loads(authority_state_path.read_text(encoding="utf-8"))
    paths = (Path(state["runtime"]), Path(state["barrier"]))
    try:
        stdout, stderr = process.communicate(timeout=10.0)
        assert process.returncode == 128 + signal.SIGTERM, (stdout, stderr)
        assert all(not path.exists() for path in paths)
        assert not lease.exists()
    finally:
        if process.poll() is None:
            _signal_test_process_group(process.pid)
            process.wait(timeout=5.0)
        for path in paths:
            if path.exists():
                os.chmod(path, 0o700, follow_symlinks=False)
                shutil.rmtree(path)
        lease.unlink(missing_ok=True)


@pytest.mark.parametrize("operation", ("down", "compose"))
@pytest.mark.parametrize("failure", ("signal", "timeout"))
def test_failed_persistent_command_exhausts_descendants_and_retains_lease(
    tmp_path: Path,
    operation: str,
    failure: str,
) -> None:
    process, child_state_path, mutation_path, lease = _spawn_persistent_command_probe(
        tmp_path,
        operation=operation,
        failure=failure,
    )
    child = json.loads(child_state_path.read_text(encoding="utf-8"))
    child_group = int(child["pgid"])
    try:
        if failure == "signal":
            os.kill(process.pid, signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=10.0)
        expected = 128 + signal.SIGTERM if failure == "signal" else 1
        assert process.returncode == expected, (stdout, stderr)
        if failure == "timeout":
            assert "TimeoutExpired" in stderr
        grandchild = json.loads(
            (tmp_path / "persistent-command-grandchild.json").read_text(encoding="utf-8")
        )
        assert not _pid_exists(int(child["pid"]))
        assert not _pid_exists(int(grandchild["pid"]))
        with pytest.raises(ProcessLookupError):
            os.killpg(child_group, 0)
        before = mutation_path.stat().st_size
        time.sleep(0.15)
        assert mutation_path.stat().st_size == before
        assert lease.is_file()
        loaded = read_authority_lease(lease, expected_repo=Path(__file__).resolve().parents[2])
        assert loaded.runtime_dir.is_dir()
        assert loaded.barrier_root.is_dir()
    finally:
        _signal_test_process_group(child_group)
        if process.poll() is None:
            _signal_test_process_group(process.pid)
            process.wait(timeout=5.0)
        _wait_for_test_process_group_exit(process.pid)
        if child_group != process.pid:
            _wait_for_test_process_group_exit(child_group)
        authority_state_path = tmp_path / "persistent-command-authority.json"
        if authority_state_path.is_file():
            state = json.loads(authority_state_path.read_text(encoding="utf-8"))
            for key in ("runtime", "barrier"):
                path = Path(state[key])
                if path.exists():
                    os.chmod(path, 0o700, follow_symlinks=False)
                    shutil.rmtree(path)
        lease.unlink(missing_ok=True)


def test_signal_during_persistent_cleanup_handoff_cannot_skip_cleanup(
    tmp_path: Path,
) -> None:
    process, authority_state_path, lease = _spawn_persistent_cleanup_handoff_probe(tmp_path)
    state = json.loads(authority_state_path.read_text(encoding="utf-8"))
    paths = (Path(state["runtime"]), Path(state["barrier"]))
    try:
        stdout, stderr = process.communicate(timeout=10.0)
        assert process.returncode == 1, (stdout, stderr)
        assert "synthetic persistent startup failure" in stderr
        assert (tmp_path / "persistent-handoff-cleanup-finished").is_file()
        assert all(not path.exists() for path in paths)
        assert not lease.exists()
    finally:
        if process.poll() is None:
            _signal_test_process_group(process.pid)
            process.wait(timeout=5.0)
        for path in paths:
            if path.exists():
                os.chmod(path, 0o700, follow_symlinks=False)
                shutil.rmtree(path)
        lease.unlink(missing_ok=True)


def test_signal_waits_for_real_pytest_child_exit_before_cleanup(tmp_path: Path) -> None:
    process, child_pid_path, lease = _spawn_signal_lifecycle_probe(tmp_path, mode="first-signal")
    os.kill(process.pid, signal.SIGTERM)
    stdout, stderr, lease_survived = _finish_signal_lifecycle_probe(process, child_pid_path, lease)
    assert process.returncode == 128 + signal.SIGTERM, (stdout, stderr)
    assert lease_survived is False
    cleanup_state = json.loads((tmp_path / "cleanup-state").read_text(encoding="utf-8"))
    assert cleanup_state["child_pgid"] == cleanup_state["child_pid"]
    assert cleanup_state["grandchild_pgid"] == cleanup_state["child_pgid"]
    assert cleanup_state["child_pgid"] != cleanup_state["harness_pgid"]
    assert cleanup_state["child_alive"] is False
    assert cleanup_state["grandchild_alive"] is False
    assert cleanup_state["group_alive"] is False
    assert cleanup_state["mutation_stable"] is True
    assert (tmp_path / "survivor-check").read_text(encoding="utf-8") == "checked"


def test_signal_waits_for_real_preflight_descendants_before_cleanup(tmp_path: Path) -> None:
    process, child_pid_path, lease = _spawn_signal_lifecycle_probe(
        tmp_path,
        mode="first-signal",
        stage="preflight",
    )
    os.kill(process.pid, signal.SIGTERM)
    stdout, stderr, lease_survived = _finish_signal_lifecycle_probe(
        process,
        child_pid_path,
        lease,
    )
    assert process.returncode == 128 + signal.SIGTERM, (stdout, stderr)
    assert lease_survived is False
    cleanup_state = json.loads((tmp_path / "cleanup-state").read_text(encoding="utf-8"))
    assert cleanup_state["child_pgid"] == cleanup_state["child_pid"]
    assert cleanup_state["grandchild_pgid"] == cleanup_state["child_pgid"]
    assert cleanup_state["child_pgid"] != cleanup_state["harness_pgid"]
    assert cleanup_state["child_alive"] is False
    assert cleanup_state["grandchild_alive"] is False
    assert cleanup_state["group_alive"] is False
    assert cleanup_state["mutation_stable"] is True
    assert (tmp_path / "survivor-check").read_text(encoding="utf-8") == "checked"


def test_preflight_timeout_reaps_real_descendants_before_cleanup(tmp_path: Path) -> None:
    process, child_pid_path, lease = _spawn_signal_lifecycle_probe(
        tmp_path,
        mode="first-signal",
        stage="preflight-timeout",
    )
    stdout, stderr, lease_survived = _finish_signal_lifecycle_probe(
        process,
        child_pid_path,
        lease,
    )
    assert process.returncode == 1, (stdout, stderr)
    assert "TimeoutExpired" in stderr
    assert "final pytest child unexpectedly started" not in stderr
    assert lease_survived is False
    cleanup_state = json.loads((tmp_path / "cleanup-state").read_text(encoding="utf-8"))
    assert cleanup_state["child_pgid"] == cleanup_state["child_pid"]
    assert cleanup_state["grandchild_pgid"] == cleanup_state["child_pgid"]
    assert cleanup_state["child_pgid"] != cleanup_state["harness_pgid"]
    assert cleanup_state["child_alive"] is False
    assert cleanup_state["grandchild_alive"] is False
    assert cleanup_state["group_alive"] is False
    assert cleanup_state["mutation_stable"] is True
    assert (tmp_path / "survivor-check").read_text(encoding="utf-8") == "checked"


def test_concurrent_signal_during_timeout_handoff_cannot_escape_group_reaping(
    tmp_path: Path,
) -> None:
    process, child_pid_path, lease = _spawn_signal_lifecycle_probe(
        tmp_path,
        mode="first-signal",
        stage="termination-transition",
    )
    stdout, stderr, lease_survived = _finish_signal_lifecycle_probe(
        process,
        child_pid_path,
        lease,
    )
    assert process.returncode == 1, (stdout, stderr)
    assert "TimeoutExpired" in stderr
    assert lease_survived is False
    cleanup_state = json.loads((tmp_path / "cleanup-state").read_text(encoding="utf-8"))
    assert cleanup_state["child_alive"] is False
    assert cleanup_state["grandchild_alive"] is False
    assert cleanup_state["group_alive"] is False
    assert cleanup_state["mutation_stable"] is True
    assert (tmp_path / "survivor-check").read_text(encoding="utf-8") == "checked"


def test_second_signal_cannot_interrupt_final_survivor_checks(tmp_path: Path) -> None:
    process, child_pid_path, lease = _spawn_signal_lifecycle_probe(
        tmp_path,
        mode="second-signal",
    )
    os.kill(process.pid, signal.SIGTERM)
    cleanup_started = tmp_path / "cleanup-started"
    deadline = time.monotonic() + 10.0
    while not cleanup_started.is_file() and process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.02)
    assert cleanup_started.is_file()
    os.kill(process.pid, signal.SIGINT)
    stdout, stderr, lease_survived = _finish_signal_lifecycle_probe(process, child_pid_path, lease)
    assert process.returncode == 128 + signal.SIGTERM, (stdout, stderr)
    assert lease_survived is False
    cleanup_state = json.loads((tmp_path / "cleanup-state").read_text(encoding="utf-8"))
    assert cleanup_state["child_alive"] is False
    assert cleanup_state["grandchild_alive"] is False
    assert cleanup_state["group_alive"] is False
    assert cleanup_state["mutation_stable"] is True
    assert (tmp_path / "survivor-check").read_text(encoding="utf-8") == "checked"


def test_outer_signal_exhausts_child_journaled_barrier_before_lease_removal(
    tmp_path: Path,
) -> None:
    repo = Path(__file__).resolve().parents[2]
    token = uuid.uuid4().hex[:12]
    lease = Path("/tmp") / f"jhin-p10-child-barrier-signal-{token}.json"
    script = r"""
import json
import os
import sys
from pathlib import Path

from tests.integration import phase10_upgrade_harness as lifecycle

root = Path(sys.argv[1])
repo = Path(sys.argv[2])
lease = Path(sys.argv[3])
token = sys.argv[4]
authority_state = root / "journal-authority.json"
child_state = root / "journal-child.json"
cleanup_state = root / "journal-cleanup.json"
authority = lifecycle.ComposeAuthority.create(
    repo=repo,
    mode="rootful",
    socket_path=Path("/var/run/docker.sock"),
    socket_gid=123,
    token=token,
    source_environment={"PATH": os.environ.get("PATH", "/usr/bin")},
)
authority_state.write_text(
    json.dumps(
        {"runtime": str(authority.runtime_dir), "barrier": str(authority.barrier_root)}
    ),
    encoding="utf-8",
)

child_code = r'''import json,os,time
from pathlib import Path
from tests.integration.phase10_upgrade_harness import create_barrier_root
barrier = create_barrier_root("phase10.tool.after_claim.before_effect.v1")
Path(os.environ["PHASE10_CHILD_STATE"]).write_text(
    json.dumps({"pid": os.getpid(), "pgid": os.getpgrp(), "barrier": str(barrier.root)}),
    encoding="utf-8",
)
while True:
    time.sleep(1)
'''
child_command = (sys.executable, "-c", child_code)

def no_preflight(self, *, runner):
    del self, runner

def selected_command(scenario):
    del scenario
    return child_command

def local_cleanup(self, *, runner, upgrade=False):
    del runner, upgrade
    barrier = Path(json.loads(child_state.read_text(encoding="utf-8"))["barrier"])
    self.remove_recovery_paths()
    cleanup_state.write_text(
        json.dumps({"barrier_exists": barrier.exists()}),
        encoding="utf-8",
    )

lifecycle.ComposeAuthority.preflight = no_preflight
lifecycle.ComposeAuthority.down_and_cleanup = local_cleanup
lifecycle.build_live_pytest_command = selected_command
environment = authority.environment
environment["PHASE10_CHILD_STATE"] = str(child_state)
authority = lifecycle.replace(
    authority,
    _environment_items=tuple(sorted(environment.items())),
)
scenario = lifecycle.LiveScenario(
    nodes=("journaled-child-barrier",), expected_tests=1, start_stack=False
)
lifecycle.execute_one_shot(authority, scenario=scenario, lease_path=lease)
"""
    process = subprocess.Popen(
        (sys.executable, "-c", script, str(tmp_path), str(repo), str(lease), token),
        cwd=repo,
        env=os.environ.copy(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    authority_state = tmp_path / "journal-authority.json"
    child_state = tmp_path / "journal-child.json"
    _wait_for_path(child_state)
    child = json.loads(child_state.read_text(encoding="utf-8"))
    child_group = int(child["pgid"])
    child_barrier = Path(child["barrier"])
    authority_paths: tuple[Path, ...] = ()
    if authority_state.is_file():
        state = json.loads(authority_state.read_text(encoding="utf-8"))
        authority_paths = (Path(state["runtime"]), Path(state["barrier"]))
    try:
        os.kill(process.pid, signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=10.0)
        assert process.returncode == 128 + signal.SIGTERM, (stdout, stderr)
        assert not _pid_exists(int(child["pid"]))
        with pytest.raises(ProcessLookupError):
            os.killpg(child_group, 0)
        assert not child_barrier.exists()
        assert json.loads((tmp_path / "journal-cleanup.json").read_text(encoding="utf-8")) == {
            "barrier_exists": False
        }
        assert not lease.exists()
        assert all(not path.exists() for path in authority_paths)
    finally:
        _signal_test_process_group(child_group)
        if process.poll() is None:
            _signal_test_process_group(process.pid)
            process.wait(timeout=5.0)
        if child_barrier.exists():
            os.chmod(child_barrier, 0o700, follow_symlinks=False)
            shutil.rmtree(child_barrier)
        for path in authority_paths:
            if path.exists():
                os.chmod(path, 0o700, follow_symlinks=False)
                shutil.rmtree(path)
        lease.unlink(missing_ok=True)


def test_one_shot_upgrade_build_uses_the_injected_selected_daemon_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = _authority_for_recorder()
    ports = {
        variable: 52000 + index
        for index, (variable, _service, _container_port) in enumerate(PUBLISHED_ENDPOINTS)
    }
    lease = Path("/tmp") / f"jhin-p10-one-shot-{uuid.uuid4().hex}.json"
    build_runners: list[Any] = []
    child_commands: list[tuple[str, ...]] = []
    frozen = FrozenPhase9Image(
        source_ref="6318781b57692bf39f37cd428d73de115d7458e2",
        tag=authority.phase9_image_tag("6318781b57692bf39f37cd428d73de115d7458e2"),
        image_id="sha256:" + "a" * 64,
    )

    monkeypatch.setattr(
        ComposeAuthority,
        "start_stack",
        lambda self, *, runner: ports,
    )
    monkeypatch.setattr(
        ComposeAuthority,
        "down_and_cleanup",
        lambda self, *, runner, upgrade=False: None,
    )

    def fake_build(
        self: ComposeAuthority,
        source_ref: str,
        *,
        runner: Any = lifecycle.run_command,
    ) -> FrozenPhase9Image:
        del self, source_ref
        build_runners.append(runner)
        return frozen

    monkeypatch.setattr(ComposeAuthority, "build_phase9_agent_image", fake_build)
    monkeypatch.setattr(
        ComposeAuthority,
        "with_upgrade_runtime",
        lambda self, prepared: self,
    )

    def selected_runner(
        command: tuple[str, ...],
        **kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        del kwargs
        return subprocess.CompletedProcess(command, 0, "", "")

    def selected_child_runner(
        command: tuple[str, ...],
        **kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        del kwargs
        child_commands.append(command)
        return subprocess.CompletedProcess(command, 0, "passed\n", "")

    scenario = LiveScenario(
        nodes=("tests/integration/fake.py::test_live",),
        expected_tests=1,
        upgrade=True,
    )
    try:
        execute_one_shot(
            authority,
            scenario=scenario,
            lease_path=lease,
            runner=selected_runner,
            child_runner=selected_child_runner,
        )
        assert build_runners == [selected_runner]
        assert child_commands == [build_live_pytest_command(scenario)]
    finally:
        lease.unlink(missing_ok=True)
        authority.remove_runtime_paths()


def test_upgrade_runtime_binding_is_signal_atomic_before_cleanup_ownership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = _authority_for_recorder()
    ports = {
        variable: 52500 + index
        for index, (variable, _service, _container_port) in enumerate(PUBLISHED_ENDPOINTS)
    }
    lease = Path("/tmp") / f"jhin-p10-upgrade-signal-{uuid.uuid4().hex}.json"
    source_ref = "6318781b57692bf39f37cd428d73de115d7458e2"
    frozen = FrozenPhase9Image(
        source_ref=source_ref,
        tag=authority.phase9_image_tag(source_ref),
        image_id="sha256:" + "a" * 64,
    )
    extra_barrier = Path("/tmp") / f"jhin-p10-upgrade-signal-barrier-{uuid.uuid4().hex}"

    monkeypatch.setattr(ComposeAuthority, "start_stack", lambda self, *, runner: ports)
    monkeypatch.setattr(lifecycle, "read_phase9_source_ref", lambda *args, **kwargs: source_ref)
    monkeypatch.setattr(
        ComposeAuthority,
        "build_phase9_agent_image",
        lambda self, selected_ref, *, runner: frozen,
    )
    monkeypatch.setattr(
        ComposeAuthority,
        "remove_phase9_agent_image",
        lambda self, selected_ref, *, runner: None,
    )

    def signal_before_return(
        self: ComposeAuthority,
        prepared: FrozenPhase9Image,
    ) -> ComposeAuthority:
        del prepared
        extra_barrier.mkdir(mode=0o700)
        environment = self.environment
        environment["PHASE10_UPGRADE_BARRIER_NORMAL_HOST"] = str(extra_barrier)
        selected = replace(self, _environment_items=tuple(sorted(environment.items())))
        os.kill(os.getpid(), signal.SIGTERM)
        return selected

    def local_cleanup(
        self: ComposeAuthority,
        *,
        runner: Any,
        upgrade: bool = False,
    ) -> None:
        del runner, upgrade
        self.remove_runtime_paths()

    monkeypatch.setattr(ComposeAuthority, "with_upgrade_runtime", signal_before_return)
    monkeypatch.setattr(ComposeAuthority, "down_and_cleanup", local_cleanup)
    scenario = LiveScenario(nodes=("upgrade-signal-atomic",), expected_tests=1, upgrade=True)
    try:
        with pytest.raises(SystemExit) as raised:
            execute_one_shot(authority, scenario=scenario, lease_path=lease)
        assert raised.value.code == 128 + signal.SIGTERM
        assert not extra_barrier.exists()
        assert not lease.exists()
    finally:
        lease.unlink(missing_ok=True)
        if extra_barrier.exists():
            shutil.rmtree(extra_barrier)
        authority.remove_runtime_paths()


def test_one_shot_upgrade_runtime_failure_exhausts_the_exact_frozen_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = _authority_for_recorder()
    ports = {
        variable: 53000 + index
        for index, (variable, _service, _container_port) in enumerate(PUBLISHED_ENDPOINTS)
    }
    lease = Path("/tmp") / f"jhin-p10-one-shot-{uuid.uuid4().hex}.json"
    frozen = FrozenPhase9Image(
        source_ref="6318781b57692bf39f37cd428d73de115d7458e2",
        tag=authority.phase9_image_tag("6318781b57692bf39f37cd428d73de115d7458e2"),
        image_id="sha256:" + "a" * 64,
    )
    image_present = True
    image_calls: list[tuple[str, ...]] = []

    monkeypatch.setattr(
        ComposeAuthority,
        "start_stack",
        lambda self, *, runner: ports,
    )
    monkeypatch.setattr(
        ComposeAuthority,
        "down_and_cleanup",
        lambda self, *, runner, upgrade=False: None,
    )
    monkeypatch.setattr(
        ComposeAuthority,
        "build_phase9_agent_image",
        lambda self, source_ref, *, runner: frozen,
    )
    monkeypatch.setattr(
        ComposeAuthority,
        "with_upgrade_runtime",
        lambda self, prepared: (_ for _ in ()).throw(RuntimeError("barrier setup failed")),
    )

    def selected_runner(
        command: tuple[str, ...],
        **kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        nonlocal image_present
        del kwargs
        if "image" in command:
            image_calls.append(command)
        if command[-3:] == ("image", "inspect", frozen.tag):
            return subprocess.CompletedProcess(command, 0 if image_present else 1, "", "")
        if command[-3:] == ("image", "rm", frozen.tag):
            image_present = False
        return subprocess.CompletedProcess(command, 0, "", "")

    scenario = LiveScenario(
        nodes=("tests/integration/fake.py::test_live",),
        expected_tests=1,
        upgrade=True,
    )
    try:
        with pytest.raises(RuntimeError, match="barrier setup failed"):
            execute_one_shot(
                authority,
                scenario=scenario,
                lease_path=lease,
                runner=selected_runner,
            )
        assert image_calls == [
            authority.docker_command("image", "inspect", frozen.tag),
            authority.docker_command("image", "rm", frozen.tag),
            authority.docker_command("image", "inspect", frozen.tag),
        ]
        assert image_present is False
    finally:
        lease.unlink(missing_ok=True)
        authority.remove_runtime_paths()


def test_service_inspection_is_project_scoped_and_selected_daemon_only() -> None:
    authority = _authority_for_recorder()
    recorder = _ScriptedRecorder(authority)
    ps = authority.compose_command("ps", "-q", "sandbox-runner")
    inspect = authority.docker_command("inspect", "runner-container")
    recorder.responses[ps] = (0, "runner-container\n", "")
    recorder.responses[inspect] = (
        0,
        json.dumps([{"Id": "runner-container", "Config": {"User": "10001:10001"}}]),
        "",
    )
    try:
        observed = authority.inspect_service("sandbox-runner", runner=recorder)
        assert observed["Id"] == "runner-container"
        assert recorder.calls == [ps, inspect]
        assert all(call[:3] == authority.docker_command()[:3] for call in recorder.calls)
    finally:
        authority.remove_runtime_paths()


def test_temporal_history_run_id_is_loaded_from_the_isolated_database() -> None:
    authority = _authority_for_recorder()
    recorder = _ScriptedRecorder(authority)
    domain_run_id = "018f4d52-8b93-7d41-8ac7-7f190f091110"
    temporal_run_id = "018f4d52-8b93-7d41-8ac7-7f190f091111"
    query = authority.compose_command(
        "exec",
        "-T",
        "postgres",
        "psql",
        "-U",
        "jhin",
        "-d",
        "jhin",
        "-At",
        "-c",
        (f"SELECT temporal_run_id FROM agent_run WHERE id = '{domain_run_id}'"),
    )
    recorder.responses[query] = (0, f"{temporal_run_id}\n", "")
    try:
        assert authority.temporal_run_id(domain_run_id, runner=recorder) == temporal_run_id
    finally:
        authority.remove_runtime_paths()


def test_private_health_and_engine_version_probes_use_exact_service_paths() -> None:
    authority = _authority_for_recorder()
    recorder = _ScriptedRecorder(authority)
    health_url = "http://sandbox-runner:8085/health"
    version_url = "http://rootless-docker-transport:2375/version"
    health = authority.service_http_json_command("tool-worker", health_url)
    version = authority.service_http_json_command("sandbox-runner", version_url)
    recorder.responses[health] = (0, '{"docker":true,"status":"ok"}\n', "")
    recorder.responses[version] = (
        0,
        '{"ApiVersion":"1.45","Version":"26.1.0"}\n',
        "",
    )
    try:
        assert authority.service_http_json("tool-worker", health_url, runner=recorder) == {
            "docker": True,
            "status": "ok",
        }
        assert authority.service_http_json("sandbox-runner", version_url, runner=recorder) == {
            "ApiVersion": "1.45",
            "Version": "26.1.0",
        }
    finally:
        authority.remove_runtime_paths()


def test_upgrade_master_key_probe_uses_profiled_exact_service_vector() -> None:
    authority = _authority_for_recorder()
    try:
        command = authority.master_key_readability_command(
            "phase9-agent-worker-normal", upgrade=True
        )
        assert "--profile" in command and "phase10-upgrade" in command
        assert "tests/integration/compose.phase10-upgrade.yaml" in command
        assert "phase9-agent-worker-normal" in command
        assert command[-3:-1] == ("python", "-c")
    finally:
        authority.remove_runtime_paths()


def test_upgrade_agent_runner_probe_is_profiled_and_cannot_target_other_services() -> None:
    authority = _authority_for_recorder()
    try:
        command = authority.upgrade_agent_runner_probe_command("phase10-agent-worker-normal")
        assert "--profile" in command and "phase10-upgrade" in command
        assert "tests/integration/compose.phase10-upgrade.yaml" in command
        assert "http://sandbox-runner:8085/health" in command
        assert "phase10-agent-worker-normal" in command
        with pytest.raises(ValueError, match="agent service"):
            authority.upgrade_agent_runner_probe_command("phase10-tool-worker-normal")
    finally:
        authority.remove_runtime_paths()


def test_wrong_gid_probe_builds_only_runner_and_never_changes_selected_gid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = _authority_for_recorder()
    failure = authority.with_rootful_socket_gid(1)
    recorder = _ScriptedRecorder(failure)
    up = failure.compose_command(
        "up",
        "-d",
        "--no-deps",
        "--force-recreate",
        "--build",
        "--wait",
        "--wait-timeout",
        "60",
        "sandbox-runner",
    )
    recorder.responses[up] = (
        1,
        "",
        "Docker socket group does not match SANDBOX_DOCKER_GID",
    )
    monkeypatch.setattr(ComposeAuthority, "preflight", lambda self, runner: None)
    try:
        result = authority.run_wrong_gid_probe(runner=recorder)
        assert result.returncode == 1
        joined = [" ".join(call) for call in recorder.calls]
        assert any(command.endswith("build sandbox-runner") for command in joined)
        assert not any(" sandbox-image" in command for command in joined)
        assert not any(command.endswith(" up") for command in joined)
        assert authority.socket_gid == 123
        assert failure.environment["SANDBOX_DOCKER_GID"] == "1"
    finally:
        authority.remove_runtime_paths()


def test_worker_recreation_uses_exact_barrier_mount_and_bounded_wait() -> None:
    authority = _authority_for_recorder()
    barrier = create_barrier_root("phase10.tool.after_claim.before_effect.v1")
    identity = "018f4d52-8b93-7d41-8ac7-7f190f091111"
    try:
        configured = authority.worker_environment(barrier=barrier, identity=identity)
        assert configured["APP_ENV"] == "test"
        assert configured["JHIN_TEST_CRASH_BARRIER_HOST_DIR"] == str(barrier.root)
        assert configured["JHIN_TEST_CRASH_BARRIER_DIR"] == "/run/jhin/test-barriers"
        assert configured["JHIN_TEST_CRASH_BARRIER_NAME"] == barrier.failpoint
        assert configured["JHIN_TEST_CRASH_BARRIER_MATCH"] == identity
        unconfigured = authority.worker_environment()
        assert unconfigured["JHIN_TEST_CRASH_BARRIER_HOST_DIR"] == str(authority.barrier_root)
        assert unconfigured["JHIN_TEST_CRASH_BARRIER_DIR"] == ""
        assert unconfigured["JHIN_TEST_CRASH_BARRIER_NAME"] == ""
        assert unconfigured["JHIN_TEST_CRASH_BARRIER_MATCH"] == ""
        command = authority.worker_recreate_command("tool-worker")
        assert command[-9:] == (
            "up",
            "-d",
            "--no-deps",
            "--force-recreate",
            "--build",
            "--wait",
            "--wait-timeout",
            "300",
            "tool-worker",
        )
        pair_command = authority.worker_recreate_command("tool-worker", "agent-worker")
        assert pair_command[-10:] == (
            "up",
            "-d",
            "--no-deps",
            "--force-recreate",
            "--build",
            "--wait",
            "--wait-timeout",
            "300",
            "tool-worker",
            "agent-worker",
        )
    finally:
        barrier.cleanup()
        authority.remove_runtime_paths()


def test_worker_recreation_reasserts_the_whole_live_topology(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = _authority_for_recorder()
    recorder = _Recorder(authority)
    recorder.ps_output = json.dumps(
        [
            {"Service": service, "State": "running", "Health": "healthy"}
            for service in sorted(EXPECTED_ROOTFUL_SERVICES)
        ]
    )
    environment = authority.worker_environment()
    monkeypatch.setattr(
        ComposeAuthority,
        "inspect_service",
        lambda self, service, runner: {
            "Config": {
                "Env": [
                    f"{key}={environment[key]}"
                    for key in (
                        "APP_ENV",
                        "JHIN_TEST_CRASH_BARRIER_DIR",
                        "JHIN_TEST_CRASH_BARRIER_NAME",
                        "JHIN_TEST_CRASH_BARRIER_MATCH",
                    )
                ]
            }
        },
    )
    try:
        authority.recreate_worker("agent-worker", runner=recorder)
        ps_calls = [call for call in recorder.calls if "ps" in call]
        assert len(ps_calls) == 1
        assert ps_calls[0][-4:] == ("ps", "--all", "--format", "json")
    finally:
        authority.remove_runtime_paths()


def test_worker_pair_recreation_is_combined_before_whole_topology_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = _authority_for_recorder()
    recorder = _Recorder(authority)
    recorder.ps_output = json.dumps(
        [
            {"Service": service, "State": "running", "Health": "healthy"}
            for service in sorted(EXPECTED_ROOTFUL_SERVICES)
        ]
    )
    barrier = create_barrier_root("phase10.agent.before_manifest_bind.v1")
    identity = "018f4d52-8b93-7d41-8ac7-7f190f091111"
    environment = authority.worker_environment(barrier=barrier, identity=identity)
    recorder.authority = replace(
        authority,
        _environment_items=tuple(sorted(environment.items())),
    )
    inspected: list[str] = []

    def inspect_service(
        self: ComposeAuthority,
        service: str,
        *,
        runner: Any,
    ) -> dict[str, Any]:
        inspected.append(service)
        return {
            "Config": {
                "Env": [
                    f"{key}={environment[key]}"
                    for key in (
                        "APP_ENV",
                        "JHIN_TEST_CRASH_BARRIER_DIR",
                        "JHIN_TEST_CRASH_BARRIER_NAME",
                        "JHIN_TEST_CRASH_BARRIER_MATCH",
                    )
                ]
            }
        }

    monkeypatch.setattr(ComposeAuthority, "inspect_service", inspect_service)
    try:
        authority.recreate_workers(
            ("tool-worker", "agent-worker"),
            barrier=barrier,
            identity=identity,
            runner=recorder,
        )
        assert recorder.calls[0][-2:] == ("tool-worker", "agent-worker")
        assert inspected == ["tool-worker", "agent-worker"]
        ps_calls = [call for call in recorder.calls if "ps" in call]
        assert len(ps_calls) == 1
    finally:
        barrier.cleanup()
        authority.remove_runtime_paths()


def test_persistent_compose_passthrough_is_a_typed_nonpublishing_allowlist() -> None:
    allowed = (
        ("--profile", "build", "build", "sandbox-image"),
        ("run", "--rm", "--no-deps", "api", "jhin-db-migrate"),
        ("run", "--rm", "--no-deps", "api", "jhin-seed-dev"),
    )
    for allowed_command in allowed:
        assert lifecycle.validate_persistent_compose_arguments(allowed_command) == allowed_command
    for rejected_command in (
        ("-p", "foreign", "ps"),
        ("-f", "foreign.yaml", "ps"),
        ("--env-file", ".env", "ps"),
        ("push",),
        ("publish",),
        ("up", "-d"),
    ):
        with pytest.raises(ValueError, match="allowlist"):
            lifecycle.validate_persistent_compose_arguments(rejected_command)


def test_shared_compose_helper_rejects_authority_selectors_and_publication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allowed = (
        ("restart", "agent-worker"),
        ("ps",),
        ("ps", "--all"),
        ("ps", "--all", "--format", "json"),
        ("run", "--rm", "--no-deps", "api", "jhin-seed-dev"),
        ("run", "--rm", "--no-deps", "api", "jhin-db-migrate"),
        ("exec", "-T", "postgres", "psql", "-U", "jhin", "-c", "SELECT 1"),
    )
    for allowed_command in allowed:
        assert integration_config.validate_compose_arguments(allowed_command) == allowed_command
    for rejected_command in (
        ("ps", "-p", "foreign"),
        ("ps", "--project-name=foreign"),
        ("ps", "-f", "foreign.yaml"),
        ("ps", "--file=foreign.yaml"),
        ("ps", "--env-file", ".env"),
        ("ps", "--project-directory", "/tmp/foreign"),
        ("ps", "--host", "unix:///tmp/foreign.sock"),
        ("ps", "--context=foreign"),
        ("push",),
        ("publish",),
        ("build", "--push"),
    ):
        with pytest.raises(ValueError, match="not an allowed leased Compose operation"):
            integration_config.validate_compose_arguments(rejected_command)
    runner_called = False

    def forbidden_runner(*args: Any, **kwargs: Any) -> Any:
        nonlocal runner_called
        del args, kwargs
        runner_called = True
        raise AssertionError("unsafe Compose arguments reached the runner")

    monkeypatch.setattr(integration_config, "run_command", forbidden_runner)
    with pytest.raises(ValueError, match="not an allowed leased Compose operation"):
        integration_config.compose("push")
    assert runner_called is False


def test_shared_compose_helper_rechecks_socket_before_the_allowed_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = replace(
        _authority_for_recorder(),
        socket_snapshot=SocketMetadata(
            path=Path("/var/run/docker.sock"),
            inode=111,
            mode=stat.S_IFSOCK | 0o660,
            uid=0,
            gid=123,
        ),
    )
    monkeypatch.setattr(integration_config, "compose_authority", lambda: authority)
    monkeypatch.setattr(
        SocketMetadata,
        "capture",
        classmethod(
            lambda cls, path: SocketMetadata(
                path=path,
                inode=222,
                mode=stat.S_IFSOCK | 0o660,
                uid=0,
                gid=123,
            )
        ),
    )
    runner_calls: list[tuple[str, ...]] = []

    def forbidden_runner(command: tuple[str, ...], **kwargs: Any) -> Any:
        del kwargs
        runner_calls.append(command)
        raise AssertionError("changed socket reached Compose")

    monkeypatch.setattr(integration_config, "run_command", forbidden_runner)
    try:
        with pytest.raises(RuntimeError, match="socket metadata changed"):
            integration_config.compose("ps")
        assert runner_calls == []
    finally:
        authority.remove_runtime_paths()


def test_frozen_phase6_regression_uses_the_leased_selected_daemon() -> None:
    source = Path("tests/integration/test_phase6_exit.py").read_text(encoding="utf-8")
    assert "subprocess.run" not in source
    assert "compose_authority" in source
    assert "docker_command" in source


def test_counting_provider_is_nonlogging_readonly_and_data_network_only() -> None:
    authority = _authority_for_recorder()
    try:
        command = authority.counting_provider_command(
            image_id="sha256:" + "a" * 64,
            marker="phase10-model-deadc0de",
        )
        joined = " ".join(command)
        assert command[:3] == authority.docker_command()[:3]
        assert "--network" in command
        assert f"{authority.project}_data" in command
        assert "--user 10001:10001" in joined
        assert "--read-only" in command
        assert "--cap-drop ALL" in joined
        assert "--publish" not in command and "-p" not in command
        assert "build_completion" in joined
        assert "log_message" in joined
        assert "Authorization" not in joined
        assert f"jhin.phase10.invocation={authority.token}" in command
    finally:
        authority.remove_runtime_paths()


def test_service_once_uses_exact_vector_and_explicit_container_environment() -> None:
    authority = _authority_for_recorder()
    try:
        command = authority.service_once_command(
            "agent-worker",
            environment={
                "APP_ENV": "production",
                "JHIN_TEST_CRASH_BARRIER_NAME": TOOL_AFTER_CLAIM,
            },
        )
        run_index = command.index("run")
        assert command[run_index : run_index + 3] == ("run", "--rm", "--no-deps")
        assert command[-1] == "agent-worker"
        assert command.count("-e") == 2
        assert "APP_ENV=production" in command
        assert f"JHIN_TEST_CRASH_BARRIER_NAME={TOOL_AFTER_CLAIM}" in command
    finally:
        authority.remove_runtime_paths()


def test_inspectable_job_request_is_nonnetworked_and_exactly_label_addressable() -> None:
    authority = _authority_for_recorder()
    job_id = "deadc0dedeadc0dedeadc0de"
    try:
        assert authority.blocking_sandbox_job_request(job_id) == {
            "job_id": job_id,
            "command": ["python3", "-c", "import time;time.sleep(300)"],
            "network_policy": "none",
            "timeout_seconds": 300,
        }
        assert authority.sandbox_job_label(job_id) == f"jhin.sandbox.job={job_id}"
    finally:
        authority.remove_runtime_paths()


def test_inspectable_job_uses_a_runner_valid_hex_identifier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = replace(
        _authority_for_recorder(),
        _published_port_items=(("SANDBOX_RUNNER_DEV_PORT", 49152),),
    )
    captured: dict[str, Any] = {}

    class RequestCaptured(Exception):
        pass

    def capture_request(request: Any, *, timeout: float) -> Any:
        captured["request"] = request
        captured["timeout"] = timeout
        raise RequestCaptured

    monkeypatch.setattr(secrets, "token_hex", lambda _length: "deadc0dedeadc0dedeadc0de")
    monkeypatch.setattr(urllib.request, "urlopen", capture_request)
    try:
        with pytest.raises(RequestCaptured):
            authority.start_inspectable_sandbox_job()
        request = captured["request"]
        assert json.loads(request.data) == authority.blocking_sandbox_job_request(
            "deadc0dedeadc0dedeadc0de"
        )
        assert captured["timeout"] == 5.0
    finally:
        authority.remove_runtime_paths()


_TOOL_QUEUE = "jhin-tool-queue"
_AGENT_QUEUE = "jhin-agent-queue"
_TOOL_ACTIVITIES = {
    "resolve_advertised_tools",
    "execute_bound_tool",
    "resolve_bound_tool_approval",
    "sync_external_tool",
    "cleanup_run_workspace",
}
_AGENT_ACTIVITIES = {
    "resolve_snapshot",
    "reason_agent_step",
    "commit_agent_step",
    "commit_approval_projection",
    "finalize_run_projection",
    "prepare_triggered_task",
}


def _csrf(client: httpx.AsyncClient) -> dict[str, str]:
    token = client.cookies.get("jhin_csrf")
    assert token
    return {"x-csrf-token": token}


async def _post(
    client: httpx.AsyncClient,
    path: str,
    body: dict[str, Any],
    *,
    expect: int = 201,
) -> dict[str, Any]:
    response = await client.post(path, json=body, headers=_csrf(client))
    assert response.status_code == expect, f"{path}: {response.status_code} {response.text}"
    payload: dict[str, Any] = response.json()
    return payload


async def _put(
    client: httpx.AsyncClient,
    path: str,
    body: dict[str, Any],
) -> dict[str, Any]:
    response = await client.put(path, json=body, headers=_csrf(client))
    assert response.status_code == 200, f"{path}: {response.status_code} {response.text}"
    payload: dict[str, Any] = response.json()
    return payload


@asynccontextmanager
async def _live_owner() -> AsyncIterator[tuple[httpx.AsyncClient, str]]:
    async with httpx.AsyncClient(base_url=integration_config.API_URL, timeout=30.0) as client:
        credentials = {"email": DEV_OWNER_EMAIL, "password": DEV_OWNER_PASSWORD}
        login = await client.post("/api/v1/auth/login", json=credentials)
        if login.status_code != 200:
            integration_config.compose("run", "--rm", "--no-deps", "api", "jhin-seed-dev")
            login = await client.post("/api/v1/auth/login", json=credentials)
        assert login.status_code == 200, login.text
        workspace = await _post(
            client,
            "/api/v1/workspaces",
            {"name": f"Phase 10 boundary {uuid.uuid4().hex[:10]}"},
        )
        yield client, str(workspace["id"])


async def _github_agent(
    client: httpx.AsyncClient,
    workspace_id: str,
    *,
    tag: str,
    preset: str,
    provider_url: str = "http://fake-provider:8080/v1",
) -> tuple[dict[str, Any], dict[str, Any]]:
    connection = (
        await _post(
            client,
            f"/api/v1/workspaces/{workspace_id}/connections",
            {
                "connector_type": "github",
                "name": f"P10 GitHub {tag}",
                "auth_type": "pat",
                "credentials": {"token": "fake-github-pat"},
                "config": {"base_url": "http://fake-github:8080"},
            },
        )
    )["connection"]
    provider = await _post(
        client,
        f"/api/v1/workspaces/{workspace_id}/model-providers",
        {
            "type": "openai_compatible",
            "display_name": f"P10 provider {tag}",
            "base_url": provider_url,
        },
    )
    profile = await _post(
        client,
        f"/api/v1/workspaces/{workspace_id}/model-profiles",
        {
            "provider_id": provider["id"],
            "model_name": "fake-mini",
            "display_name": f"P10 profile {tag}",
        },
    )
    agent = await _post(
        client,
        f"/api/v1/workspaces/{workspace_id}/agents",
        {
            "name": f"P10 agent {tag}",
            "system_prompt": "Use each explicitly requested tool exactly once.",
            "model_profile_id": profile["id"],
        },
    )
    await _put(
        client,
        f"/api/v1/workspaces/{workspace_id}/agents/{agent['id']}/policy",
        {"preset": preset},
    )
    await _post(
        client,
        f"/api/v1/workspaces/{workspace_id}/agents/{agent['id']}/grants",
        {
            "capability": "github.issue.comment",
            "scope": {"connection_id": connection["id"], "repository": "octo/alpha"},
            "effect": "allow",
        },
    )
    return connection, agent


def _comment_marker(connection_id: str, marker: str) -> str:
    arguments = json.dumps(
        {
            "connection_id": connection_id,
            "repository": "octo/alpha",
            "number": 1,
            "body": marker,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"[[tool:github.issue.comment {arguments}]]"


async def _assign(
    client: httpx.AsyncClient,
    workspace_id: str,
    agent_id: str,
    *,
    marker: str,
    description: str,
) -> dict[str, Any]:
    return await _post(
        client,
        f"/api/v1/workspaces/{workspace_id}/agents/{agent_id}/assign-task",
        {"title": f"Phase 10 {marker}", "description": description},
    )


async def _task(
    client: httpx.AsyncClient,
    workspace_id: str,
    task_id: str,
) -> dict[str, Any]:
    response = await client.get(f"/api/v1/workspaces/{workspace_id}/tasks/{task_id}")
    assert response.status_code == 200, response.text
    payload: dict[str, Any] = response.json()
    return payload


async def _wait_run_started(
    client: httpx.AsyncClient,
    workspace_id: str,
    task_id: str,
    *,
    deadline_seconds: float = 60.0,
) -> tuple[dict[str, Any], str]:
    deadline = time.monotonic() + deadline_seconds
    detail: dict[str, Any] = {}
    while time.monotonic() < deadline:
        detail = await _task(client, workspace_id, task_id)
        if len(detail.get("runs", [])) == 1:
            run_id = str(detail["runs"][0]["id"])
            assert str(uuid.UUID(run_id)) == run_id
            return detail, run_id
        assert detail.get("runs", []) == []
        await asyncio.sleep(0.1)
    pytest.fail(f"task {task_id} did not start one run in {deadline_seconds}s: {detail}")


async def _wait_task(
    client: httpx.AsyncClient,
    workspace_id: str,
    task_id: str,
    *,
    deadline_seconds: float = 180.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + deadline_seconds
    detail: dict[str, Any] = {}
    while time.monotonic() < deadline:
        detail = await _task(client, workspace_id, task_id)
        if detail["task"]["state"] in {"completed", "failed", "cancelled"}:
            return detail
        await asyncio.sleep(0.25)
    pytest.fail(f"task {task_id} did not close in {deadline_seconds}s: {detail}")


async def _calls(
    client: httpx.AsyncClient,
    workspace_id: str,
    run_id: str,
) -> list[dict[str, Any]]:
    response = await client.get(f"/api/v1/workspaces/{workspace_id}/runs/{run_id}/tool-calls")
    assert response.status_code == 200, response.text
    payload: list[dict[str, Any]] = response.json()
    return payload


async def _timeline(
    client: httpx.AsyncClient,
    workspace_id: str,
    run_id: str,
) -> list[dict[str, Any]]:
    response = await client.get(f"/api/v1/workspaces/{workspace_id}/runs/{run_id}/timeline")
    assert response.status_code == 200, response.text
    payload: list[dict[str, Any]] = response.json()
    return payload


async def _fake_github_state() -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(f"{integration_config.FAKE_GITHUB_URL}/_state")
    assert response.status_code == 200, response.text
    payload: dict[str, Any] = response.json()
    return payload


async def _comment_count(marker: str) -> int:
    state = await _fake_github_state()
    comments = state["repos"]["octo/alpha"]["issues"]["1"]["comments"]
    return sum(1 for comment in comments if comment["body"] == marker)


async def _described_temporal_run_id(workflow_id: str) -> str:
    client = await TemporalClient.connect(integration_config.TEMPORAL_ADDRESS)
    description = await client.get_workflow_handle(workflow_id).describe()
    temporal_run_id = description.run_id
    assert uuid.UUID(temporal_run_id), temporal_run_id
    return temporal_run_id


async def _history(workflow_id: str, temporal_run_id: str) -> Any:
    assert uuid.UUID(temporal_run_id), temporal_run_id
    client = await TemporalClient.connect(integration_config.TEMPORAL_ADDRESS)
    handle = client.get_workflow_handle(workflow_id, run_id=temporal_run_id)
    return await handle.fetch_history()


def _assert_queue_ownership(
    history: Any,
    *,
    expected: list[tuple[str, str]],
) -> None:
    pairs = activity_schedule_pairs(history)
    assert pairs == expected
    for name, queue in pairs:
        if name in _TOOL_ACTIVITIES:
            assert queue == _TOOL_QUEUE
        if name in _AGENT_ACTIVITIES:
            assert queue == _AGENT_QUEUE


def _one_tool_history() -> list[tuple[str, str]]:
    return [
        ("resolve_snapshot", _AGENT_QUEUE),
        ("resolve_advertised_tools", _TOOL_QUEUE),
        ("reason_agent_step", _AGENT_QUEUE),
        ("execute_bound_tool", _TOOL_QUEUE),
        ("commit_agent_step", _AGENT_QUEUE),
        ("resolve_advertised_tools", _TOOL_QUEUE),
        ("reason_agent_step", _AGENT_QUEUE),
        ("commit_agent_step", _AGENT_QUEUE),
        ("cleanup_run_workspace", _TOOL_QUEUE),
        ("finalize_run_projection", _AGENT_QUEUE),
    ]


@pytest.mark.integration
async def test_all_effect_classes_cross_tool_queue_once() -> None:
    authority = integration_config.compose_authority()
    async with _live_owner() as (client, workspace_id):
        ordinary_marker = f"p10-ordinary-{uuid.uuid4().hex}"
        ordinary_connection, ordinary_agent = await _github_agent(
            client,
            workspace_id,
            tag=uuid.uuid4().hex[:8],
            preset="autonomous",
        )
        ordinary = await _assign(
            client,
            workspace_id,
            ordinary_agent["id"],
            marker=ordinary_marker,
            description=_comment_marker(ordinary_connection["id"], ordinary_marker),
        )
        ordinary_detail = await _wait_task(client, workspace_id, ordinary["id"])
        assert ordinary_detail["task"]["state"] == "completed", ordinary_detail
        ordinary_run = str(ordinary_detail["runs"][0]["id"])
        assert await _comment_count(ordinary_marker) == 1
        _assert_queue_ownership(
            await _history(
                str(ordinary["temporal_workflow_id"]),
                authority.temporal_run_id(ordinary_run),
            ),
            expected=_one_tool_history(),
        )

        approval_marker = f"p10-approval-{uuid.uuid4().hex}"
        approval_connection, approval_agent = await _github_agent(
            client,
            workspace_id,
            tag=uuid.uuid4().hex[:8],
            preset="restricted",
        )
        approval_task = await _assign(
            client,
            workspace_id,
            approval_agent["id"],
            marker=approval_marker,
            description=_comment_marker(approval_connection["id"], approval_marker),
        )
        approval_id = ""
        approval_call_id = ""
        deadline = time.monotonic() + 120.0
        while time.monotonic() < deadline:
            detail = await _task(client, workspace_id, approval_task["id"])
            pending = await client.get(
                f"/api/v1/workspaces/{workspace_id}/approvals",
                params={"status": "pending", "limit": 100},
            )
            assert pending.status_code == 200, pending.text
            if detail["runs"]:
                approval_run = str(detail["runs"][0]["id"])
                rows = await _calls(client, workspace_id, approval_run)
                parked = [row for row in rows if row["status"] == "pending_approval"]
                matches = [
                    row
                    for row in pending.json()["items"]
                    if str(row.get("task_id")) == str(approval_task["id"])
                ]
                if len(parked) == len(matches) == 1:
                    approval_call_id = str(parked[0]["id"])
                    approval_id = str(matches[0]["id"])
                    break
            await asyncio.sleep(0.25)
        assert approval_id and approval_call_id
        authority.recreate_worker("tool-worker")
        decision = await client.post(
            f"/api/v1/workspaces/{workspace_id}/approvals/{approval_id}/approve",
            headers=_csrf(client),
        )
        assert decision.status_code == 200, decision.text
        approval_detail = await _wait_task(client, workspace_id, approval_task["id"])
        approval_run = str(approval_detail["runs"][0]["id"])
        approval_calls = await _calls(client, workspace_id, approval_run)
        assert [str(row["id"]) for row in approval_calls] == [approval_call_id]
        assert approval_calls[0]["status"] == "completed"
        assert await _comment_count(approval_marker) == 1
        _assert_queue_ownership(
            await _history(
                str(approval_task["temporal_workflow_id"]),
                authority.temporal_run_id(approval_run),
            ),
            expected=[
                *_one_tool_history()[:5],
                ("resolve_bound_tool_approval", _TOOL_QUEUE),
                ("commit_approval_projection", _AGENT_QUEUE),
                *_one_tool_history()[5:],
            ],
        )

        from . import test_phase7_exit as phase7

        sync_agent = await phase7._make_agent(client, workspace_id, uuid.uuid4().hex[:8])
        linear, _secret = await phase7._linear_connection(
            client, workspace_id, uuid.uuid4().hex[:8]
        )
        sync_name = f"P10 sync {uuid.uuid4().hex[:8]}"
        trigger = await phase7._make_trigger(
            client,
            workspace_id,
            name=sync_name,
            connection_id=linear["id"],
            agent_id=sync_agent["id"],
            comment_back=True,
        )
        issue = await phase7._new_issue(sync_name, "Reply briefly without using tools.")
        await phase7._settle_trigger_cache()
        await phase7._transition(issue, "Todo")
        invocations = await phase7._wait_invocations(client, workspace_id, trigger["id"], minimum=1)
        assert len(invocations) == 1
        invocation = invocations[0]
        deadline = time.monotonic() + 60.0
        while invocation["task_id"] is None and time.monotonic() < deadline:
            await asyncio.sleep(0.25)
            invocation = (await phase7._invocations(client, workspace_id, trigger["id"]))[0]
        assert invocation["task_id"]
        sync_detail = await phase7._wait_task_finished(
            client, workspace_id, str(invocation["task_id"])
        )
        assert sync_detail["task"]["state"] == "completed", sync_detail
        deadline = time.monotonic() + 30.0
        matching_comments: list[dict[str, Any]] = []
        while time.monotonic() < deadline:
            linear_state = await phase7._fake_state()
            matching_comments = [
                comment
                for comment in linear_state["comments"][issue]
                if sync_name in comment["body"]
            ]
            if matching_comments:
                break
            await asyncio.sleep(0.25)
        assert len(matching_comments) == 1
        _assert_queue_ownership(
            await _history(
                str(invocation["workflow_id"]),
                await _described_temporal_run_id(str(invocation["workflow_id"])),
            ),
            expected=[
                ("prepare_triggered_task", _AGENT_QUEUE),
                ("sync_external_tool", _TOOL_QUEUE),
            ],
        )

        cleanup_tag = uuid.uuid4().hex[:8]
        cleanup_provider = await _post(
            client,
            f"/api/v1/workspaces/{workspace_id}/model-providers",
            {
                "type": "openai_compatible",
                "display_name": f"P10 cleanup provider {cleanup_tag}",
                "base_url": "http://fake-provider:8080/v1",
            },
        )
        cleanup_profile = await _post(
            client,
            f"/api/v1/workspaces/{workspace_id}/model-profiles",
            {
                "provider_id": cleanup_provider["id"],
                "model_name": "fake-mini",
                "display_name": f"P10 cleanup profile {cleanup_tag}",
            },
        )
        cleanup_agent = await _post(
            client,
            f"/api/v1/workspaces/{workspace_id}/agents",
            {
                "name": f"P10 cleanup agent {cleanup_tag}",
                "system_prompt": "Use the requested tool exactly once.",
                "model_profile_id": cleanup_profile["id"],
            },
        )
        cli = (
            await _post(
                client,
                f"/api/v1/workspaces/{workspace_id}/connections",
                {
                    "connector_type": "cli",
                    "name": f"P10 cleanup CLI {cleanup_tag}",
                    "auth_type": "none",
                    "credentials": {},
                    "config": {"default_network": "none"},
                },
            )
        )["connection"]
        await _post(
            client,
            f"/api/v1/workspaces/{workspace_id}/agents/{cleanup_agent['id']}/grants",
            {
                "capability": "cli.command.execute",
                "scope": {"connection_id": cli["id"], "command": "python3 *"},
                "effect": "allow",
            },
        )
        cli_arguments = json.dumps(
            {
                "connection_id": cli["id"],
                "command": "python3 -c \"print('phase10-cleanup')\"",
                "network": "none",
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        cleanup_task = await _assign(
            client,
            workspace_id,
            cleanup_agent["id"],
            marker=cleanup_tag,
            description=f"[[tool:cli.command.execute {cli_arguments}]]",
        )
        cleanup_detail = await _wait_task(client, workspace_id, cleanup_task["id"])
        cleanup_run = str(cleanup_detail["runs"][0]["id"])
        cleanup_calls = await _calls(client, workspace_id, cleanup_run)
        assert len(cleanup_calls) == 1
        assert cleanup_calls[0]["status"] == "completed"
        assert cleanup_calls[0]["sanitized_output_json"]["exit_code"] == 0
        cleanup_history = await _history(
            str(cleanup_task["temporal_workflow_id"]),
            authority.temporal_run_id(cleanup_run),
        )
        assert (
            activity_schedule_pairs(cleanup_history).count(("cleanup_run_workspace", _TOOL_QUEUE))
            == 1
        )
        assert activity_start_count(cleanup_history, "cleanup_run_workspace") == 1
        volume = authority._run(
            authority.docker_command("volume", "inspect", f"jhin-sandbox-ws-run-{cleanup_run}"),
            runner=integration_config.run_command,
            timeout=30.0,
            check=False,
        )
        assert volume.returncode == 1
        cleanup_timeline = await _timeline(client, workspace_id, cleanup_run)
        assert len([row for row in cleanup_timeline if row["event_type"] == "sandbox.job"]) == 1
        assert ordinary_run


@pytest.mark.integration
async def test_advertised_tools_filter_before_reasoning() -> None:
    authority = integration_config.compose_authority()
    marker = f"phase10-model-{uuid.uuid4().hex[:16]}"
    provider = authority.start_counting_provider(marker=marker)
    try:
        async with _live_owner() as (client, workspace_id):
            _connection, agent = await _github_agent(
                client,
                workspace_id,
                tag=uuid.uuid4().hex[:8],
                preset="autonomous",
                provider_url=provider.internal_base_url,
            )
            assigned = await _assign(
                client,
                workspace_id,
                agent["id"],
                marker=marker,
                description=f"Complete this no-tool task and retain marker {marker}.",
            )
            detail = await _wait_task(client, workspace_id, assigned["id"])
            assert detail["task"]["state"] == "completed", detail
            assert provider.count() == 1
            assert provider.advertised_tools() == ("github.issue.comment",)
            assert await _calls(client, workspace_id, str(detail["runs"][0]["id"])) == []
    finally:
        provider.close()


@pytest.mark.integration
async def test_tool_queue_loss_blocks_effect_and_live_networks_are_isolated() -> None:
    authority = integration_config.compose_authority()
    async with _live_owner() as (client, workspace_id):
        marker = f"p10-queue-loss-{uuid.uuid4().hex}"
        connection, agent = await _github_agent(
            client,
            workspace_id,
            tag=uuid.uuid4().hex[:8],
            preset="autonomous",
        )
        authority.stop_service("tool-worker")
        tool_worker_stopped = True
        try:
            assigned = await _assign(
                client,
                workspace_id,
                agent["id"],
                marker=marker,
                description=_comment_marker(connection["id"], marker),
            )
            _started, run_id = await _wait_run_started(
                client,
                workspace_id,
                str(assigned["id"]),
            )
            temporal_run_id = authority.temporal_run_id(run_id)
            deadline = time.monotonic() + 30.0
            while time.monotonic() < deadline:
                history = await _history(str(assigned["temporal_workflow_id"]), temporal_run_id)
                if ("resolve_advertised_tools", _TOOL_QUEUE) in activity_schedule_pairs(history):
                    break
                await asyncio.sleep(0.25)
            else:
                pytest.fail("workflow never scheduled tool-queue schema resolution")
            assert await _comment_count(marker) == 0
            authority.recreate_worker("tool-worker")
            tool_worker_stopped = False
            detail = await _wait_task(client, workspace_id, assigned["id"])
            assert detail["task"]["state"] == "completed", detail
            assert await _comment_count(marker) == 1
            _assert_queue_ownership(
                await _history(str(assigned["temporal_workflow_id"]), temporal_run_id),
                expected=_one_tool_history(),
            )
            runner = authority.inspect_service("sandbox-runner")
            assert runner["Config"]["User"] == "10001:10001"
            assert runner["HostConfig"]["Privileged"] is False
            expected_groups = [] if authority.mode == "rootless" else [str(authority.socket_gid)]
            assert runner["HostConfig"].get("GroupAdd", []) == expected_groups
            assert authority.service_dns_probe("agent-worker", "sandbox-runner") != 0
            assert authority.service_dns_probe("tool-worker", "sandbox-runner") == 0
            assert authority.service_http_json(
                "tool-worker",
                "http://sandbox-runner:8085/health",
            ) == {
                "docker": True,
                "status": "ok",
            }
        finally:
            if tool_worker_stopped:
                authority.recreate_worker("tool-worker")


@pytest.mark.integration
def test_live_sandbox_job_security_contract() -> None:
    authority = integration_config.compose_authority()
    job = authority.start_inspectable_sandbox_job()
    try:
        container = job.container
        assert container["Config"]["User"] == "1000:1000"
        assert container["Config"]["Labels"]["jhin.sandbox.job"] == job.job_id
        host = container["HostConfig"]
        assert host["Privileged"] is False
        assert host["CapDrop"] == ["ALL"]
        assert host["ReadonlyRootfs"] is True
        assert host.get("GroupAdd", []) == []
        assert host["NetworkMode"] == "none"
        assert all("docker.sock" not in str(mount) for mount in container.get("Mounts", []))
        assert not any("docker.sock" in bind for bind in host.get("Binds", []) or [])
    finally:
        cancelled = authority.cancel_sandbox_job(job)
    assert cancelled["status"] == "cancelled"


@pytest.mark.integration
@pytest.mark.parametrize("service", ["agent-worker", "tool-worker"])
def test_worker_image_rejects_live_barrier_controls_in_production(service: str) -> None:
    authority = integration_config.compose_authority()
    result = authority.run_service_once(
        service,
        environment={
            "APP_ENV": "production",
            "JHIN_TEST_CRASH_BARRIER_DIR": "/run/jhin/test-barriers",
            "JHIN_TEST_CRASH_BARRIER_NAME": TOOL_AFTER_CLAIM,
            "JHIN_TEST_CRASH_BARRIER_MATCH": "018f4d52-8b93-7d41-8ac7-7f190f091111",
        },
        timeout=10.0,
    )
    assert result.returncode != 0
    assert "test crash barriers are forbidden in production" in str(result.stderr)


@pytest.mark.integration
@pytest.mark.parametrize(
    ("failpoint", "expected_model_calls", "events_at_barrier"),
    [
        (AGENT_BEFORE_BIND, 2, (0, 0)),
        (PHASE9_AFTER_MANIFEST, 1, (1, 1)),
    ],
)
async def test_agent_crash_matrix_retries_without_tool_effect_duplication(
    failpoint: str,
    expected_model_calls: int,
    events_at_barrier: tuple[int, int],
) -> None:
    authority = integration_config.compose_authority()
    model_marker = f"phase10-model-{uuid.uuid4().hex[:16]}"
    effect_marker = f"p10-agent-crash-{uuid.uuid4().hex}"
    provider = authority.start_counting_provider(marker=model_marker)
    barrier = create_barrier_root(failpoint)
    try:
        async with _live_owner() as (client, workspace_id):
            connection, agent = await _github_agent(
                client,
                workspace_id,
                tag=uuid.uuid4().hex[:8],
                preset="autonomous",
                provider_url=provider.internal_base_url,
            )
            authority.stop_service("tool-worker")
            assigned = await _assign(
                client,
                workspace_id,
                agent["id"],
                marker=model_marker,
                description=(f"{model_marker} " + _comment_marker(connection["id"], effect_marker)),
            )
            _detail, run_id = await _wait_run_started(client, workspace_id, str(assigned["id"]))
            temporal_run_id = authority.temporal_run_id(run_id)
            authority.stop_service("agent-worker")
            authority.recreate_workers(
                ("tool-worker", "agent-worker"),
                barrier=barrier,
                identity=run_id,
            )
            assert barrier.wait_arrival(timeout=120.0) == run_id
            timeline = await _timeline(client, workspace_id, run_id)
            event_types = [row["event_type"] for row in timeline]
            assert event_types.count("agent.step.tool_manifest") == events_at_barrier[0]
            assert event_types.count("agent.step.reasoning") == events_at_barrier[1]
            assert provider.count() == 1
            assert await _comment_count(effect_marker) == 0
            authority.kill_service("agent-worker")
            barrier.release(run_id)
            authority.recreate_worker("agent-worker")
            final = await _wait_task(client, workspace_id, assigned["id"], deadline_seconds=780.0)
            assert final["task"]["state"] == "completed", final
            final_timeline = await _timeline(client, workspace_id, run_id)
            final_types = [row["event_type"] for row in final_timeline]
            assert final_types.count("agent.step.tool_manifest") == 1
            assert final_types.count("agent.step.reasoning") == 1
            assert final_types.count("run.completed") == 1
            assert provider.count() == expected_model_calls
            assert await _comment_count(effect_marker) == 1
            calls = await _calls(client, workspace_id, run_id)
            assert len(calls) == 1 and calls[0]["status"] == "completed"
            history = await _history(str(assigned["temporal_workflow_id"]), temporal_run_id)
            assert activity_start_count(history, "reason_agent_step") == 2
            _assert_queue_ownership(history, expected=_one_tool_history())
    finally:
        with contextlib.suppress(Exception):
            authority.recreate_workers(("tool-worker", "agent-worker"))
        barrier.cleanup()
        provider.close()


@pytest.mark.integration
@pytest.mark.parametrize(
    ("failpoint", "expected_effects", "expected_status"),
    [
        (TOOL_BEFORE_CLAIM, 1, "completed"),
        (TOOL_AFTER_CLAIM, 0, "execution_unknown"),
        (TOOL_AFTER_EFFECT, 1, "execution_unknown"),
    ],
)
async def test_tool_crash_matrix_preserves_claim_and_ambiguity_contract(
    failpoint: str,
    expected_effects: int,
    expected_status: str,
) -> None:
    authority = integration_config.compose_authority()
    barrier = create_barrier_root(failpoint)
    marker = f"p10-tool-crash-{uuid.uuid4().hex}"
    try:
        async with _live_owner() as (client, workspace_id):
            connection, agent = await _github_agent(
                client,
                workspace_id,
                tag=uuid.uuid4().hex[:8],
                preset="autonomous",
            )
            authority.stop_service("tool-worker")
            assigned = await _assign(
                client,
                workspace_id,
                agent["id"],
                marker=marker,
                description=_comment_marker(connection["id"], marker),
            )
            _detail, run_id = await _wait_run_started(client, workspace_id, str(assigned["id"]))
            temporal_run_id = authority.temporal_run_id(run_id)
            invocation_id = str(stable_tool_invocation_id(uuid.UUID(run_id), 0, 0))
            authority.recreate_worker("tool-worker", barrier=barrier, identity=invocation_id)
            assert barrier.wait_arrival(timeout=120.0) == invocation_id
            calls = await _calls(client, workspace_id, run_id)
            if failpoint == TOOL_BEFORE_CLAIM:
                assert calls == []
                assert await _comment_count(marker) == 0
            elif failpoint == TOOL_AFTER_CLAIM:
                assert len(calls) == 1 and calls[0]["status"] == "executing"
                assert str(calls[0]["id"]) == invocation_id
                assert await _comment_count(marker) == 0
            else:
                assert len(calls) == 1 and calls[0]["status"] == "executing"
                assert str(calls[0]["id"]) == invocation_id
                assert await _comment_count(marker) == 1
            authority.kill_service("tool-worker")
            barrier.release(invocation_id)
            authority.recreate_worker("tool-worker")
            final = await _wait_task(client, workspace_id, assigned["id"], deadline_seconds=780.0)
            terminal_calls = await _calls(client, workspace_id, run_id)
            assert len(terminal_calls) == 1
            assert str(terminal_calls[0]["id"]) == invocation_id
            assert terminal_calls[0]["status"] == expected_status
            assert await _comment_count(marker) == expected_effects
            history = await _history(str(assigned["temporal_workflow_id"]), temporal_run_id)
            assert activity_start_count(history, "execute_bound_tool") == 2
            if expected_status == "completed":
                assert final["task"]["state"] == "completed", final
                _assert_queue_ownership(history, expected=_one_tool_history())
            else:
                assert final["task"]["state"] == "failed", final
                assert activity_schedule_pairs(history) == [
                    *_one_tool_history()[:5],
                    ("cleanup_run_workspace", _TOOL_QUEUE),
                    ("finalize_run_projection", _AGENT_QUEUE),
                ]
    finally:
        with contextlib.suppress(Exception):
            authority.recreate_worker("tool-worker")
        barrier.cleanup()
