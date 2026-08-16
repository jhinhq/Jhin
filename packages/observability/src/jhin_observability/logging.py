"""Structured JSON logging shared by all Jhin Python services.

Both structlog loggers and stdlib loggers (uvicorn, temporalio, nats, alembic)
are rendered through the same JSON pipeline so every log line the stack emits
is machine-parseable.
"""

from __future__ import annotations

import logging
import sys

import structlog
from structlog.typing import EventDict, WrappedLogger


def configure_logging(
    service: str,
    level: str = "INFO",
    extra_processors: list[structlog.typing.Processor] | None = None,
) -> None:
    """Route all logging (structlog + stdlib) to JSON lines on stdout.

    ``extra_processors`` run on every record (structlog and stdlib) before
    rendering — services that handle credentials pass the secret redaction
    processor here (plan 13.5) without this package depending on it.
    """

    def add_service(logger: WrappedLogger, method_name: str, event_dict: EventDict) -> EventDict:
        event_dict.setdefault("service", service)
        return event_dict

    shared_processors: list[structlog.typing.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        add_service,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        *(extra_processors or []),
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
            structlog.processors.dict_tracebacks,
            structlog.processors.JSONRenderer(),
        ],
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.stdlib.get_logger(name)
