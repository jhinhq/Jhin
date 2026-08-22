"""Package-local isolation for process-global observability state."""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from typing import Any, cast

import pytest
import structlog


@pytest.fixture(autouse=True)
def restore_observability_globals() -> Iterator[None]:
    """Restore logging, structlog, and bootstrap globals after every test."""
    root = logging.getLogger()
    handlers = list(root.handlers)
    level = root.level
    disabled = root.disabled
    structlog_config = cast(
        dict[str, Any],
        {
            key: list(value) if key == "processors" else value
            for key, value in structlog.get_config().items()
        },
    )
    try:
        yield
    finally:
        try:
            from jhin_observability.bootstrap import _reset_observability_for_test
        except ImportError:
            pass
        else:
            _reset_observability_for_test()
        root.handlers.clear()
        root.handlers.extend(handlers)
        root.setLevel(level)
        root.disabled = disabled
        structlog.configure(**structlog_config)
        task_threads = [
            thread
            for thread in threading.enumerate()
            if thread.name.startswith("jhin-otel-")
            or thread.name == "OtelPeriodicExportingMetricReader"
        ]
        for thread in task_threads:
            thread.join(timeout=1.0)
        assert [thread.name for thread in task_threads if thread.is_alive()] == []
