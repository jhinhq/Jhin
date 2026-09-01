"""The runner names its own Docker-authority refusal before the process dies."""

from __future__ import annotations

import ast
import json
import logging
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
import structlog

import jhin_sandbox_runner.docker_socket as docker_socket_module
import jhin_sandbox_runner.jobs as jobs_module
import jhin_sandbox_runner.main as main_module
from jhin_observability import ObservabilityRuntime, configure_json_logging
from jhin_observability.events import EVENT_FIELD_ENUM_VALUES
from jhin_sandbox_runner.docker_socket import (
    DOCKER_SOCKET_MODES,
    ROOTLESS_TRANSPORT_URL,
    DockerSocketConfigurationError,
)
from jhin_sandbox_runner.settings import Settings

REFUSAL_EVENT = "sandbox_runner.docker_authority_refused"
# The sentence tests/integration/test_phase10_sandbox_socket_modes.py greps for
# in the live container logs of a runner started with a wrong SANDBOX_DOCKER_GID.
WRONG_GID_REFUSAL = "Docker socket group does not match SANDBOX_DOCKER_GID"


@pytest.fixture
def restore_logging_globals() -> Iterator[None]:
    """``configure_json_logging`` installs process-wide handlers and rebinds
    every named logger, so a test that renders real log lines has to put the
    interpreter back the way it found it."""
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level
    original_named = {
        candidate: (list(candidate.handlers), candidate.level, candidate.propagate)
        for candidate in logging.root.manager.loggerDict.values()
        if isinstance(candidate, logging.Logger)
    }
    original_structlog_config = dict(structlog.get_config())
    try:
        yield
    finally:
        installed_handlers = [
            handler for handler in root.handlers if handler not in original_handlers
        ]
        root.handlers[:] = original_handlers
        root.setLevel(original_level)
        for named, (handlers, level, propagate) in original_named.items():
            named.handlers[:] = handlers
            named.setLevel(level)
            named.propagate = propagate
        for handler in installed_handlers:
            handler.close()
        structlog.configure(**original_structlog_config)


class _RefusingManager:
    def __init__(self, refusal: BaseException) -> None:
        self.refusal = refusal
        self.close_count = 0

    async def start(self) -> None:
        raise self.refusal

    async def close(self) -> None:
        self.close_count += 1


def _settings() -> Settings:
    return Settings(
        app_env="test",
        sandbox_runner_token="token",
        sandbox_docker_mode="rootless",
        sandbox_docker_transport_url=ROOTLESS_TRANSPORT_URL,
    )


def _runtime() -> ObservabilityRuntime:
    return cast(
        ObservabilityRuntime,
        SimpleNamespace(
            tracer=object(),
            metrics=object(),
            shutdown=lambda timeout_millis: None,
        ),
    )


async def _refuse_startup(
    message: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> tuple[_RefusingManager, DockerSocketConfigurationError, str]:
    manager = _RefusingManager(DockerSocketConfigurationError(message))
    monkeypatch.setattr(main_module, "JobManager", lambda _settings: manager)
    app = main_module.create_app(_settings(), runtime=_runtime())
    configure_json_logging(service="sandbox-runner", environment="test", level="INFO")
    with pytest.raises(DockerSocketConfigurationError) as raised:
        async with app.router.lifespan_context(app):
            pytest.fail("startup must not be reached")
    return manager, raised.value, capsys.readouterr().out


def _records(rendered: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in rendered.splitlines() if line]


async def test_wrong_gid_refusal_is_logged_as_its_own_event_and_stays_fatal(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    restore_logging_globals: None,
) -> None:
    manager, caught, rendered = await _refuse_startup(WRONG_GID_REFUSAL, monkeypatch, capsys)

    assert caught is manager.refusal
    assert manager.close_count == 1
    records = _records(rendered)
    assert [record["event"] for record in records] == [REFUSAL_EVENT]
    assert records[0]["reason"] == WRONG_GID_REFUSAL
    assert records[0]["level"] == "error"
    assert records[0]["service"] == "sandbox-runner"
    assert WRONG_GID_REFUSAL in rendered


async def test_unregistered_refusal_text_is_dropped_rather_than_logged(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    restore_logging_globals: None,
) -> None:
    socket_path = "/run/jhin/docker.sock"
    _manager, _caught, rendered = await _refuse_startup(
        f"cannot inspect {socket_path}", monkeypatch, capsys
    )

    records = _records(rendered)
    assert [record["event"] for record in records] == [REFUSAL_EVENT]
    assert "reason" not in records[0]
    assert socket_path not in rendered


def _refusal_sentences(path: Path) -> set[str]:
    """Every sentence one module can hand to ``DockerSocketConfigurationError``."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    sentences: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id != "DockerSocketConfigurationError":
            continue
        assert len(node.args) == 1
        message = node.args[0]
        if isinstance(message, ast.Constant) and isinstance(message.value, str):
            sentences.add(message.value)
            continue
        assert isinstance(message, ast.JoinedStr)
        # A refusal may interpolate the configured mode and nothing else: any
        # other value could carry a path, a GID, or an identity into the log.
        for part in message.values:
            if isinstance(part, ast.FormattedValue):
                assert ast.unparse(part.value) == "self._settings.sandbox_docker_mode"
        sentences.update(
            "".join(
                part.value if isinstance(part, ast.Constant) else mode for part in message.values
            )
            for mode in DOCKER_SOCKET_MODES
        )
    return sentences


def test_registered_reasons_are_exactly_what_the_runner_can_refuse_with() -> None:
    sources = (
        Path(cast(str, docker_socket_module.__file__)),
        Path(cast(str, jobs_module.__file__)),
    )
    raised = set[str]().union(*(_refusal_sentences(path) for path in sources))

    assert WRONG_GID_REFUSAL in raised
    assert raised == EVENT_FIELD_ENUM_VALUES[(REFUSAL_EVENT, "reason")]
