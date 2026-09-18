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
from jhin_observability.events import filter_log_event, library_text_allowed
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
            # A record from outside this codebase. ``event`` holds the
            # library's formatted message rather than one of our registered
            # event names, so the name becomes ``stdlib.message`` and the
            # text moves to ``message``, which is a registered field on that
            # event. Moving it rather than dropping it is the whole point:
            # dropping it left every Temporal, httpx and uvicorn warning in
            # production saying nothing at all.
            #
            # It only moves for a logger on the text allow-list. That check is
            # made again downstream in ``filter_log_event`` — this one keeps a
            # denied library's sentence from travelling through the redaction
            # processors and the extra processors at all, so the string a
            # ``sqlalchemy.engine.Engine`` record was formatted from is
            # dropped at the first opportunity rather than the last.
            if library_text_allowed(event_dict.get("logger")):
                event_dict["message"] = event_dict.get("event")
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


#: Loggers pinned to WARNING, whatever the service level is.
#:
#: ``sqlalchemy.engine`` logs every statement and every bound parameter at
#: INFO. ``create_async_engine(..., echo=False)`` does not stop that: ``echo``
#: only decides whether SQLAlchemy attaches a level of its own, and with none
#: attached the effective level is inherited from the root — which this module
#: sets to the service level, and which ``_route_named_loggers_through_root``
#: resets to NOTSET on every existing logger. So a service running at INFO was
#: writing its own SQL, and its parameters, to stdout: hundreds of records per
#: tool-worker run, one of them the row that carried a token.
#:
#: This is the half of the fix that stops the record being *emitted*, which is
#: also what stops the volume. The allow-list in ``events.py`` is the half that
#: stops the text being *written* — it still applies, and still denies
#: ``sqlalchemy``, so an engine warning at WARNING keeps its name and loses its
#: statement.
_PINNED_LEVELS: tuple[tuple[str, int], ...] = (("sqlalchemy", logging.WARNING),)


def _route_named_loggers_through_root() -> None:
    """Remove pre-existing formatter bypasses while preserving logger identity."""
    for candidate in logging.root.manager.loggerDict.values():
        if not isinstance(candidate, logging.Logger):
            continue
        candidate.handlers.clear()
        candidate.setLevel(logging.NOTSET)
        candidate.propagate = True
    for name, level in _PINNED_LEVELS:
        # After the reset, and by name rather than over the existing loggers,
        # so a logger SQLAlchemy has not created yet still inherits the pin.
        logging.getLogger(name).setLevel(level)


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
        cache_logger_on_first_use=False,
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


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.stdlib.get_logger(name)
