"""Unified fail-closed JSON-v1 logging for structlog and stdlib records."""

from __future__ import annotations

import logging
import sys
import traceback
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import structlog
from structlog.typing import EventDict, Processor, WrappedLogger

from jhin_observability.errors import SafeErrorCode
from jhin_observability.events import filter_log_event
from jhin_observability.redaction import (
    LOG_SCHEMA_VERSION,
    MAX_TRACEBACK_FRAMES,
    structural_redaction_processor,
)


def _add_contract_fields(
    *, service: str, environment: str
) -> Callable[[WrappedLogger, str, EventDict], EventDict]:
    def add_contract_fields(
        _logger: WrappedLogger,
        _method_name: str,
        event_dict: EventDict,
    ) -> EventDict:
        event_dict["schema_version"] = LOG_SCHEMA_VERSION
        event_dict["service"] = service
        event_dict["environment"] = environment
        if event_dict.get("_from_structlog") is False:
            event_dict["event"] = "stdlib.message"
            event_dict.pop("positional_args", None)
        return event_dict

    return add_contract_fields


def _add_current_trace_ids(
    _logger: WrappedLogger,
    _method_name: str,
    event_dict: EventDict,
) -> EventDict:
    """Reserve the trace hook without introducing an OTel dependency in Task 1."""
    return event_dict


def _normalize_exception(
    *, max_frames: int
) -> Callable[[WrappedLogger, str, EventDict], EventDict]:
    def normalize_exception(
        _logger: WrappedLogger,
        _method_name: str,
        event_dict: EventDict,
    ) -> EventDict:
        raw_exc_info = event_dict.pop("exc_info", None)
        event_dict.pop("exception", None)
        if not raw_exc_info:
            return event_dict

        exc_type: type[BaseException] | None = None
        exc_traceback: Any = None
        if raw_exc_info is True:
            current = sys.exc_info()
            exc_type, _exc_value, exc_traceback = current
        elif isinstance(raw_exc_info, tuple) and len(raw_exc_info) == 3:
            possible_type, _possible_value, exc_traceback = raw_exc_info
            if isinstance(possible_type, type) and issubclass(possible_type, BaseException):
                exc_type = possible_type
        elif isinstance(raw_exc_info, BaseException):
            exc_type = type(raw_exc_info)
            exc_traceback = raw_exc_info.__traceback__

        error_type = exc_type.__name__ if exc_type is not None else "Error"
        code = event_dict.get("error_code")
        safe_codes = {candidate.value for candidate in SafeErrorCode}
        safe_code = code if isinstance(code, str) and code in safe_codes else "internal_error"
        frames = (
            [
                {
                    "file": Path(frame.filename).name,
                    "function": frame.name,
                    "line": frame.lineno,
                }
                for frame in traceback.extract_tb(exc_traceback)[-max_frames:]
            ]
            if exc_traceback is not None
            else []
        )
        event_dict["error"] = {
            "type": error_type,
            "code": safe_code,
            "traceback": frames,
        }
        return event_dict

    return normalize_exception


def _filter_log_event_processor(
    _logger: WrappedLogger,
    _method_name: str,
    event_dict: EventDict,
) -> EventDict:
    return filter_log_event(event_dict)


def _route_named_loggers_through_root() -> None:
    """Remove pre-existing formatter bypasses while preserving logger identity."""
    for candidate in logging.root.manager.loggerDict.values():
        if not isinstance(candidate, logging.Logger):
            continue
        candidate.handlers.clear()
        candidate.setLevel(logging.NOTSET)
        candidate.propagate = True


def configure_json_logging(
    service: str,
    environment: str,
    level: str = "INFO",
    extra_processors: Sequence[Processor] = (),
) -> None:
    """Route structlog and foreign stdlib records to bounded JSON lines."""
    shared_processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        _add_contract_fields(service=service, environment=environment),
        _add_current_trace_ids,
        structlog.processors.TimeStamper(fmt="iso", utc=True, key="timestamp"),
    ]

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            _normalize_exception(max_frames=MAX_TRACEBACK_FRAMES),
            structural_redaction_processor,
            *extra_processors,
            structural_redaction_processor,
            _filter_log_event_processor,
            structlog.processors.JSONRenderer(sort_keys=True),
        ],
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())
    _route_named_loggers_through_root()


configure_logging = configure_json_logging


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.stdlib.get_logger(name)
