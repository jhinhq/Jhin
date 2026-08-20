"""Fail-closed AST audit for application logging and console output."""

from __future__ import annotations

import ast
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from jhin_observability.events import CONTEXT_FIELD_RULES, EVENT_FIELD_RULES

REPO_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class AuditFailure:
    path: Path
    line: int
    code: Literal[
        "dynamic_event",
        "unregistered_event",
        "positional_text",
        "unregistered_field",
        "direct_print",
        "direct_stream_write",
        "foreign_logging",
        "unresolved_logger_receiver",
    ]


AUDIT_EXCLUDED_PARTS = frozenset({"tests", "testing", "alembic", "__pycache__"})
AUDIT_EXCLUDED_FILES = frozenset({"seed.py", "migrate.py"})


def application_python_paths(root: Path) -> tuple[Path, ...]:
    source_roots = (root / "apps/api/src", root / "packages", root / "services")
    return tuple(
        sorted(
            path
            for source_root in source_roots
            for path in source_root.rglob("*.py")
            if not set(path.parts) & AUDIT_EXCLUDED_PARTS and path.name not in AUDIT_EXCLUDED_FILES
        )
    )


LOGGER_METHODS = frozenset(
    {
        "debug",
        "info",
        "warning",
        "warn",
        "error",
        "exception",
        "critical",
        "fatal",
        "log",
    }
)


@dataclass(frozen=True)
class LoggingCall:
    path: Path
    line: int
    column: int
    method: str
    receiver: Literal["logger", "foreign_logging", "unresolved_logger_receiver"]


def _import_aliases(tree: ast.AST) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                aliases[item.asname or item.name.split(".")[0]] = item.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            for item in node.names:
                aliases[item.asname or item.name] = f"{node.module}.{item.name}"
    return aliases


def _qualified_name(node: ast.expr, aliases: Mapping[str, str]) -> str:
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        prefix = _qualified_name(node.value, aliases)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


LOGGER_FACTORY_KINDS: dict[str, Literal["logger", "foreign_logging"]] = {
    "structlog.get_logger": "logger",
    "structlog.stdlib.get_logger": "logger",
    "jhin_observability.get_logger": "logger",
    "jhin_observability.logging.get_logger": "logger",
    "logging.getLogger": "foreign_logging",
}


def _logger_bindings(
    tree: ast.AST, aliases: Mapping[str, str]
) -> dict[str, Literal["logger", "foreign_logging"]]:
    bindings: dict[str, Literal["logger", "foreign_logging"]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        value = node.value
        if not isinstance(value, ast.Call):
            continue
        kind = LOGGER_FACTORY_KINDS.get(_qualified_name(value.func, aliases))
        if kind is None:
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        bindings.update((target.id, kind) for target in targets if isinstance(target, ast.Name))
    return bindings


def _enclosing_function(
    node: ast.AST, parents: Mapping[ast.AST, ast.AST]
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    parent = parents.get(node)
    while parent is not None:
        if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return parent
        parent = parents.get(parent)
    return None


def _has_parameter(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    name: str,
    annotation: str,
) -> bool:
    return any(
        argument.arg == name
        and argument.annotation is not None
        and ast.unparse(argument.annotation) == annotation
        for argument in (
            *function.args.posonlyargs,
            *function.args.args,
            *function.args.kwonlyargs,
        )
    )


def _assigns_container_lookup(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
) -> bool:
    return any(
        isinstance(candidate, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "container"
            for target in candidate.targets
        )
        and isinstance(candidate.value, ast.Call)
        and ast.unparse(candidate.value.func) == "self.docker.containers.container"
        for candidate in ast.walk(function)
    )


def _is_within_await(
    node: ast.AST,
    parents: Mapping[ast.AST, ast.AST],
) -> bool:
    parent = parents.get(node)
    while parent is not None and not isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
        if isinstance(parent, ast.Await):
            return True
        parent = parents.get(parent)
    return False


def _assigns_validated_docker_client(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    aliases: Mapping[str, str],
) -> bool:
    for candidate in ast.walk(function):
        if not isinstance(candidate, ast.Assign) or not isinstance(candidate.value, ast.Call):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "client" for target in candidate.targets
        ):
            continue
        if _qualified_name(candidate.value.func, aliases) != "aiodocker.Docker":
            continue
        if candidate.value.args:
            continue
        if len(candidate.value.keywords) != 1:
            continue
        keyword = candidate.value.keywords[0]
        if (
            keyword.arg == "url"
            and isinstance(keyword.value, ast.Name)
            and keyword.value.id == "validated_url"
        ):
            return True
    return False


def _is_reviewed_non_logger_call(
    path: Path,
    node: ast.Call,
    parents: Mapping[ast.AST, ast.AST],
    aliases: Mapping[str, str],
) -> bool:
    """Recognize only audited APIs whose method names resemble loggers."""
    if not isinstance(node.func, ast.Attribute):
        return False
    method = node.func.attr
    function = _enclosing_function(node, parents)
    if function is None:
        return False
    relative = path.as_posix()

    if isinstance(node.func.value, ast.Name):
        receiver = node.func.value.id
        if (
            relative.endswith("services/sandbox_runner/src/jhin_sandbox_runner/jobs.py")
            and receiver == "container"
            and method == "log"
        ):
            return (
                function.name == "_collect_logs"
                and _has_parameter(function, name="container", annotation="Any")
            ) or (function.name == "current_logs" and _assigns_container_lookup(function))
        if (
            relative.endswith("packages/connectors/src/jhin_connectors/supabase/database_tools.py")
            and function.name == "consume_result"
            and receiver == "completed"
            and method == "exception"
        ):
            return _has_parameter(
                function,
                name="completed",
                annotation="asyncio.Future[Any]",
            )

    return (
        relative.endswith("services/sandbox_runner/src/jhin_sandbox_runner/jobs.py")
        and function.name == "start"
        and isinstance(node.func.value, ast.Attribute)
        and isinstance(node.func.value.value, ast.Name)
        and node.func.value.value.id == "client"
        and node.func.value.attr == "system"
        and method == "info"
        and not node.args
        and not node.keywords
        and _is_within_await(node, parents)
        and _assigns_validated_docker_client(function, aliases)
    )


def _is_closed_poller_print(
    path: Path,
    node: ast.Call,
    parents: Mapping[ast.AST, ast.AST],
) -> bool:
    if not path.as_posix().endswith("packages/workflows/src/jhin_workflows/poller_health.py"):
        return False
    function = _enclosing_function(node, parents)
    if function is None or node.keywords or len(node.args) != 1:
        return False
    argument = node.args[0]
    if function.name == "run":
        return isinstance(argument, ast.Name) and argument.id == "_UNAVAILABLE_OUTPUT"
    if function.name != "main" or not isinstance(argument, ast.IfExp):
        return False
    return (
        isinstance(argument.body, ast.Name)
        and argument.body.id == "_READY_OUTPUT"
        and isinstance(argument.orelse, ast.Name)
        and argument.orelse.id == "_UNAVAILABLE_OUTPUT"
    )


def collect_logging_method_calls(paths: Sequence[Path]) -> tuple[LoggingCall, ...]:
    calls: list[LoggingCall] = []
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        parents = {
            child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)
        }
        aliases = _import_aliases(tree)
        bindings = _logger_bindings(tree, aliases)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            qualified_call = _qualified_name(node.func, aliases)
            method = qualified_call.rsplit(".", 1)[-1]
            if method not in LOGGER_METHODS:
                continue
            if isinstance(node.func, ast.Name):
                kind = (
                    "foreign_logging"
                    if qualified_call.startswith("logging.")
                    else "unresolved_logger_receiver"
                )
                calls.append(
                    LoggingCall(
                        path,
                        node.lineno,
                        node.col_offset,
                        method,
                        cast(
                            Literal["logger", "foreign_logging", "unresolved_logger_receiver"],
                            kind,
                        ),
                    )
                )
                continue
            assert isinstance(node.func, ast.Attribute)
            if _is_reviewed_non_logger_call(path, node, parents, aliases):
                continue
            receiver_node = node.func.value
            receiver_name = _qualified_name(receiver_node, aliases)
            if receiver_name == "temporalio.activity" and method == "info":
                continue
            direct_kind = (
                LOGGER_FACTORY_KINDS.get(_qualified_name(receiver_node.func, aliases))
                if isinstance(receiver_node, ast.Call)
                else None
            )
            if isinstance(receiver_node, ast.Name) and receiver_node.id in bindings:
                kind = bindings[receiver_node.id]
            elif receiver_name == "temporalio.activity.logger":
                kind = "logger"
            elif direct_kind is not None:
                kind = direct_kind
            elif receiver_name == "logging":
                kind = "foreign_logging"
            else:
                kind = "unresolved_logger_receiver"
            calls.append(
                LoggingCall(
                    path,
                    node.lineno,
                    node.col_offset,
                    method,
                    cast(
                        Literal["logger", "foreign_logging", "unresolved_logger_receiver"],
                        kind,
                    ),
                )
            )
    return tuple(
        sorted(
            calls,
            key=lambda item: (item.path.as_posix(), item.line, item.column, item.method),
        )
    )


def audit_paths(paths: Sequence[Path]) -> list[AuditFailure]:
    calls = collect_logging_method_calls(paths)
    failures: list[AuditFailure] = []
    call_nodes: dict[tuple[Path, int, int, str], ast.Call] = {}
    for path in sorted(paths):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        parents = {
            child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)
        }
        aliases = _import_aliases(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            qualified = _qualified_name(node.func, aliases)
            if qualified == "print" or qualified.startswith("traceback.print_"):
                if qualified == "print" and _is_closed_poller_print(path, node, parents):
                    continue
                failures.append(AuditFailure(path, node.lineno, "direct_print"))
                continue
            if qualified in {"sys.stdout.write", "sys.stderr.write"}:
                failures.append(AuditFailure(path, node.lineno, "direct_stream_write"))
                continue
            method = qualified.rsplit(".", 1)[-1]
            if method not in LOGGER_METHODS:
                continue
            call_nodes[(path, node.lineno, node.col_offset, method)] = node

    for found in calls:
        node = call_nodes[(found.path, found.line, found.column, found.method)]
        if found.receiver != "logger":
            failures.append(AuditFailure(found.path, found.line, found.receiver))
            continue
        if len(node.args) != 1:
            failures.append(AuditFailure(found.path, found.line, "positional_text"))
            continue
        event_arg = node.args[0]
        if not isinstance(event_arg, ast.Constant) or not isinstance(event_arg.value, str):
            failures.append(AuditFailure(found.path, found.line, "dynamic_event"))
            continue
        event = event_arg.value
        if event not in EVENT_FIELD_RULES:
            failures.append(AuditFailure(found.path, found.line, "unregistered_event"))
            continue
        allowed = set(CONTEXT_FIELD_RULES) | set(EVENT_FIELD_RULES[event]) | {"exc_info"}
        for keyword in node.keywords:
            if keyword.arg is None or keyword.arg not in allowed:
                failures.append(AuditFailure(found.path, found.line, "unregistered_field"))
    return sorted(
        failures,
        key=lambda item: (item.path.as_posix(), item.line, item.code),
    )


def main() -> int:
    failures = audit_paths(application_python_paths(REPO_ROOT))
    for failure in failures:
        relative = failure.path.relative_to(REPO_ROOT)
        sys.stderr.write(f"{relative}:{failure.line}: {failure.code}\n")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
