"""Closed, catalog-authoritative tool telemetry labels."""

from __future__ import annotations

import ast
import asyncio
import importlib
import importlib.util
import inspect
import tomllib
from collections.abc import Callable
from dataclasses import FrozenInstanceError, fields, is_dataclass
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import BaseModel, ConfigDict

from jhin_observability import SPAN_ATTRIBUTE_VALUES
from jhin_policy import RiskLevel, ToolDefinition
from jhin_tools import ToolCatalog, ToolExecutionContext

REPO_ROOT = Path(__file__).resolve().parents[3]


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Output(BaseModel):
    ok: bool = True


async def _execute(_context: ToolExecutionContext, _payload: BaseModel) -> BaseModel:
    return _Output()


def _telemetry_module() -> Any:
    spec = importlib.util.find_spec("jhin_tools.telemetry")
    assert spec is not None, "jhin_tools.telemetry must own pure tool label normalization"
    return importlib.import_module("jhin_tools.telemetry")


def _mapper() -> Callable[[ToolCatalog, object, object], object]:
    mapper = getattr(_telemetry_module(), "describe_tool_telemetry", None)
    assert callable(mapper)
    return cast(Callable[[ToolCatalog, object, object], object], mapper)


def _status_authority() -> Callable[[object], object]:
    authority = getattr(_telemetry_module(), "_tool_status_authority", None)
    assert callable(authority)
    return cast(Callable[[object], object], authority)


def _normalizer() -> Callable[[ToolCatalog, object], tuple[str, str]]:
    def normalize(catalog: ToolCatalog, tool_name: object) -> tuple[str, str]:
        description = cast(Any, _mapper()(catalog, tool_name, "executed"))
        assert type(description).__name__ == "ToolTelemetryDescription"
        return cast(str, description.tool_family), cast(str, description.risk)

    return normalize


def test_tool_telemetry_module_is_pure_private_and_cycle_free() -> None:
    module = _telemetry_module()
    root = importlib.import_module("jhin_tools")

    assert not hasattr(root, "describe_tool_telemetry")
    assert module.__name__ == "jhin_tools.telemetry"
    assert module.__file__ is not None
    tree = ast.parse(Path(module.__file__).read_text())
    imports: list[tuple[int, str | None]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend((0, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.append((node.level, node.module))

    assert (1, "builtin") in imports
    assert any(module_name == "jhin_observability" for _level, module_name in imports)
    assert all(
        module_name != "jhin_tools"
        and not (
            module_name
            and module_name.startswith(
                (
                    "jhin_db",
                    "jhin_tool_worker",
                    "opentelemetry",
                    "sqlalchemy",
                )
            )
        )
        and not (level == 1 and module_name is None)
        for level, module_name in imports
    )
    assert not {
        "runtime",
        "metrics",
        "tracer",
        "session_factory",
        "logger",
    } & set(vars(module))
    assert not {
        "JhinMetrics",
        "safe_span",
        "set_span_attributes",
        "noop_metrics",
        "noop_tracer",
        "get_runtime",
        "init_observability",
        "shutdown",
        "select",
        "logging",
    } & {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    assert not {
        "counter",
        "histogram",
        "add",
        "record",
        "start_as_current_span",
    } & {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    assert "SPAN_ATTRIBUTE_VALUES" in {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    }

    allowed_imports = {
        (0, "__future__", "annotations"),
        (0, "dataclasses", "dataclass"),
        (0, "jhin_observability", "SPAN_ATTRIBUTE_VALUES"),
        (0, "jhin_policy", "RiskLevel"),
        (0, "jhin_policy", "ToolDefinition"),
        (1, "builtin", "ToolCatalog"),
    }
    resolved_imports: set[tuple[int, str | None, str]] = set()
    for node in tree.body:
        assert not isinstance(node, ast.Import), "pure mapper uses a closed direct-import set"
        if not isinstance(node, ast.ImportFrom):
            continue
        for alias in node.names:
            assert alias.asname is None, "aliases cannot hide package mapper authorities"
            resolved_imports.add((node.level, node.module, alias.name))
    assert resolved_imports == allowed_imports

    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    for call in calls:
        if isinstance(call.func, ast.Name):
            assert call.func.id in {
                "ToolTelemetryDescription",
                "_tool_status_authority",
                "dataclass",
                "type",
            }
        else:
            assert isinstance(call.func, ast.Attribute)
            assert isinstance(call.func.value, ast.Name)
            assert (call.func.value.id, call.func.attr) == ("catalog", "get")


def test_tools_declares_the_mapper_registry_dependency_and_lock_edge_exactly_once() -> None:
    project = tomllib.loads((REPO_ROOT / "packages/tools/pyproject.toml").read_text())
    dependencies = project["project"]["dependencies"]
    assert sum(item == "jhin-observability" for item in dependencies) == 1
    assert project["tool"]["uv"]["sources"]["jhin-observability"] == {"workspace": True}

    lock = tomllib.loads((REPO_ROOT / "uv.lock").read_text())
    package = next(item for item in lock["package"] if item["name"] == "jhin-tools")
    assert [item["name"] for item in package["dependencies"]].count("jhin-observability") == 1
    requires_dist = package["metadata"]["requires-dist"]
    assert sum(item["name"] == "jhin-observability" for item in requires_dist) == 1


@pytest.mark.parametrize(
    (
        "gateway_status",
        "expected_row_status",
        "outcome",
        "failure_class",
        "terminal_countable",
    ),
    [
        ("executed", "completed", "completed", None, True),
        ("failed", "failed", "failed", "internal", True),
        ("denied", "denied", "denied", "policy", True),
        ("rejected", "rejected", "rejected", "policy", True),
        (
            "execution_unknown",
            "execution_unknown",
            "execution_unknown",
            "execution_unknown",
            True,
        ),
        ("needs_approval", "pending_approval", "accepted", None, False),
        ("future_status", None, "other", None, False),
    ],
)
def test_description_is_frozen_and_maps_the_exact_closed_outcome_table(
    gateway_status: str,
    expected_row_status: str | None,
    outcome: str,
    failure_class: str | None,
    terminal_countable: bool,
) -> None:
    catalog = ToolCatalog()
    _register(catalog, name="system.read", risk=RiskLevel.READ)

    description = cast(Any, _mapper()(catalog, "system.read", gateway_status))

    assert is_dataclass(description)
    assert type(description).__name__ == "ToolTelemetryDescription"
    assert tuple(field.name for field in fields(type(description))) == (
        "tool_family",
        "risk",
        "expected_row_status",
        "outcome",
        "failure_class",
        "terminal_countable",
    )
    assert not hasattr(description, "tool_name")
    assert description.tool_family == "system"
    assert description.risk == "read"
    assert description.expected_row_status == expected_row_status
    assert description.outcome == outcome
    assert description.failure_class == failure_class
    assert description.terminal_countable is terminal_countable
    assert description.tool_family in SPAN_ATTRIBUTE_VALUES["jhin.tool_family"]
    assert description.risk in SPAN_ATTRIBUTE_VALUES["jhin.risk"]
    assert description.outcome in SPAN_ATTRIBUTE_VALUES["jhin.outcome"]
    with pytest.raises((FrozenInstanceError, AttributeError)):
        description.outcome = "completed"


@pytest.mark.parametrize(
    (
        "gateway_status",
        "expected_row_status",
        "outcome",
        "failure_class",
        "terminal_countable",
        "expected_approval_status",
    ),
    [
        ("executed", "completed", "completed", None, True, "approved"),
        ("failed", "failed", "failed", "internal", True, "approved"),
        ("denied", "denied", "denied", "policy", True, "approved"),
        ("rejected", "rejected", "rejected", "policy", True, "rejected"),
        (
            "execution_unknown",
            "execution_unknown",
            "execution_unknown",
            "execution_unknown",
            True,
            "approved",
        ),
        ("needs_approval", "pending_approval", "accepted", None, False, "pending"),
        ("future_status", None, "other", None, False, None),
    ],
)
def test_pure_status_authority_maps_row_telemetry_and_approval_without_catalog(
    gateway_status: str,
    expected_row_status: str | None,
    outcome: str,
    failure_class: str | None,
    terminal_countable: bool,
    expected_approval_status: str | None,
) -> None:
    assert _status_authority()(gateway_status) == (
        expected_row_status,
        expected_approval_status,
        outcome,
        failure_class,
        terminal_countable,
    )


def test_mapper_uses_the_package_status_authority_return_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _telemetry_module()
    sentinel = ("completed", "approved", "denied", "policy", True)
    calls: list[object] = []

    def sentinel_status_authority(gateway_status: object) -> object:
        calls.append(gateway_status)
        return sentinel

    monkeypatch.setattr(
        module,
        "_tool_status_authority",
        sentinel_status_authority,
        raising=False,
    )
    catalog = ToolCatalog()
    _register(catalog, name="system.read", risk=RiskLevel.READ)

    description = cast(Any, _mapper()(catalog, "system.read", "future_status"))

    assert calls == ["future_status"]
    assert description.expected_row_status == "completed"
    assert description.outcome == "denied"
    assert description.failure_class == "policy"
    assert description.terminal_countable is True


def test_mapper_has_the_exact_public_signature_and_no_hidden_authority_input() -> None:
    signature = inspect.signature(_mapper())

    assert tuple(signature.parameters) == ("catalog", "tool_name", "gateway_status")
    assert all(
        parameter.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
        for parameter in signature.parameters.values()
    )


@pytest.mark.parametrize(
    "gateway_status",
    [None, False, 7],
)
def test_non_exact_gateway_status_is_nonterminal_other_without_rendering(
    gateway_status: object,
) -> None:
    catalog = ToolCatalog()
    _register(catalog, name="system.read", risk=RiskLevel.READ)

    description = cast(Any, _mapper()(catalog, "system.read", gateway_status))

    assert description.outcome == "other"
    assert description.expected_row_status is None
    assert description.failure_class is None
    assert description.terminal_countable is False


def _register(catalog: ToolCatalog, *, name: str, risk: RiskLevel) -> None:
    catalog.register(
        ToolDefinition(
            name=name,
            description="telemetry authority fixture",
            risk=risk,
            input_model=_Input,
            output_model=_Output,
            required_capability=name,
        ),
        _execute,
    )


@pytest.mark.parametrize(
    ("name", "risk", "expected"),
    [
        ("system.read", RiskLevel.READ, ("system", "read")),
        ("organization.write", RiskLevel.WRITE, ("organization", "write")),
        ("github.elevate", RiskLevel.ELEVATED, ("github", "elevated")),
        ("linear.destroy", RiskLevel.DESTRUCTIVE, ("linear", "destructive")),
        ("vercel.read", RiskLevel.READ, ("vercel", "read")),
        ("supabase.write", RiskLevel.WRITE, ("supabase", "write")),
        ("cli.elevate", RiskLevel.ELEVATED, ("cli", "elevated")),
        ("future.read", RiskLevel.READ, ("other", "read")),
    ],
)
def test_normalize_tool_labels_uses_exact_current_catalog_authority(
    name: str,
    risk: RiskLevel,
    expected: tuple[str, str],
) -> None:
    catalog = ToolCatalog()
    _register(catalog, name=name, risk=risk)

    assert _normalizer()(catalog, name) == expected


@pytest.mark.parametrize(
    ("name", "risk", "families", "risks", "expected"),
    [
        (
            "future.read",
            RiskLevel.READ,
            frozenset({"future", "other"}),
            frozenset({"read", "other"}),
            ("future", "read"),
        ),
        (
            "system.read",
            RiskLevel.READ,
            frozenset({"other"}),
            frozenset({"other"}),
            ("other", "other"),
        ),
    ],
)
def test_mapper_uses_the_imported_closed_registry_as_live_runtime_authority(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    risk: RiskLevel,
    families: frozenset[str],
    risks: frozenset[str],
    expected: tuple[str, str],
) -> None:
    module = _telemetry_module()
    registry = dict(SPAN_ATTRIBUTE_VALUES)
    registry["jhin.tool_family"] = families
    registry["jhin.risk"] = risks
    monkeypatch.setattr(module, "SPAN_ATTRIBUTE_VALUES", registry)
    catalog = ToolCatalog()
    _register(catalog, name=name, risk=risk)

    assert _normalizer()(catalog, name) == expected


@pytest.mark.parametrize(
    "name",
    [
        "system",
        "systemish.read",
        "system.read.extra",
        "organizationish.write",
        "githubish.issue.get",
        "linear.issue.get",
        "vercel.deploy",
        "supabase.query",
        "cli.run",
        "SYSTEM.read",
        " system.read",
        "system.read ",
        "",
    ],
)
def test_unknown_removed_and_known_looking_names_are_closed(name: str) -> None:
    catalog = ToolCatalog()
    _register(catalog, name="system.read", risk=RiskLevel.READ)

    assert _normalizer()(catalog, name) == ("other", "other")


class _HostileValue:
    def __init__(self) -> None:
        self.str_calls = 0
        self.repr_calls = 0

    def __str__(self) -> str:
        self.str_calls += 1
        raise AssertionError("tool telemetry must not stringify authority")

    def __repr__(self) -> str:
        self.repr_calls += 1
        raise AssertionError("tool telemetry must not render authority")


class _StringSubclass(str):
    pass


@pytest.mark.parametrize("gateway_status", [_StringSubclass("executed"), _HostileValue()])
def test_hostile_gateway_status_is_other_without_rendering(gateway_status: object) -> None:
    catalog = ToolCatalog()
    _register(catalog, name="system.read", risk=RiskLevel.READ)

    description = cast(Any, _mapper()(catalog, "system.read", gateway_status))

    assert description.outcome == "other"
    assert description.expected_row_status is None
    assert description.failure_class is None
    assert description.terminal_countable is False
    if isinstance(gateway_status, _HostileValue):
        assert gateway_status.str_calls == 0
        assert gateway_status.repr_calls == 0


@pytest.mark.parametrize(
    "name",
    [
        None,
        False,
        7,
        _StringSubclass("system.read"),
        _HostileValue(),
    ],
)
def test_non_exact_string_names_are_other_without_rendering(name: object) -> None:
    catalog = ToolCatalog()
    _register(catalog, name="system.read", risk=RiskLevel.READ)

    assert _normalizer()(catalog, name) == ("other", "other")
    if isinstance(name, _HostileValue):
        assert name.str_calls == 0
        assert name.repr_calls == 0


class _HostileCatalog:
    def __init__(self, failure: BaseException | None = None) -> None:
        self.failure = failure
        self.lookups: list[object] = []
        self.raised_traceback: object | None = None

    def get(self, name: object) -> object:
        self.lookups.append(name)
        if self.failure is not None:
            try:
                raise self.failure
            except BaseException as error:
                self.raised_traceback = error.__traceback__
                raise
        return object(), object()


class _CatalogDiagnostic(Exception):
    pass


@pytest.mark.parametrize(
    "failure",
    [
        RuntimeError("lookup"),
        ValueError("lookup"),
        KeyError("lookup"),
        AttributeError("lookup"),
        _CatalogDiagnostic("lookup"),
    ],
)
def test_lookup_failure_is_closed_without_leaking_the_name(failure: Exception) -> None:
    catalog = _HostileCatalog(failure)

    assert _normalizer()(cast(ToolCatalog, catalog), "system.read") == ("other", "other")
    assert catalog.lookups == ["system.read"]


@pytest.mark.parametrize(
    "fatal",
    [asyncio.CancelledError("lookup"), KeyboardInterrupt(), SystemExit(19)],
)
def test_lookup_never_swallows_fatal_base_exceptions(fatal: BaseException) -> None:
    catalog = _HostileCatalog(fatal)

    with pytest.raises(type(fatal)) as raised:
        _normalizer()(cast(ToolCatalog, catalog), "system.read")

    assert raised.value is fatal
    assert catalog.lookups == ["system.read"]
    assert catalog.raised_traceback is not None
    tail = raised.value.__traceback__
    while tail is not None and tail.tb_next is not None:
        tail = tail.tb_next
    assert tail is catalog.raised_traceback


def test_normalizer_uses_one_exact_public_catalog_lookup_without_toctou() -> None:
    backing = ToolCatalog()
    _register(backing, name="github.issue.get", risk=RiskLevel.ELEVATED)
    entry = backing.get("github.issue.get")
    assert entry is not None
    calls = 0

    class _OneShotCatalog:
        def get(self, name: object) -> object:
            nonlocal calls
            calls += 1
            assert name == "github.issue.get"
            if calls > 1:
                raise RuntimeError("catalog entry disappeared after the authoritative lookup")
            return entry

    assert _normalizer()(cast(ToolCatalog, _OneShotCatalog()), "github.issue.get") == (
        "github",
        "elevated",
    )
    assert calls == 1


def test_spoofed_catalog_entry_is_closed_without_rendering() -> None:
    hostile = _HostileValue()
    catalog = _HostileCatalog()
    catalog.get = cast(Any, lambda _name: (hostile, hostile))

    assert _normalizer()(cast(ToolCatalog, catalog), "system.read") == ("other", "other")
    assert hostile.str_calls == 0
    assert hostile.repr_calls == 0


class _DefinitionClassSpoof:
    def __init__(self) -> None:
        self.name_calls = 0
        self.risk_calls = 0

    @property
    def __class__(self) -> type[ToolDefinition]:
        return ToolDefinition

    @property
    def name(self) -> object:
        self.name_calls += 1
        raise AssertionError("spoofed definitions are not authority")

    @property
    def risk(self) -> object:
        self.risk_calls += 1
        raise AssertionError("spoofed definitions are not authority")


class _RiskClassSpoof:
    def __init__(self) -> None:
        self.value_calls = 0

    @property
    def __class__(self) -> type[RiskLevel]:
        return RiskLevel

    @property
    def value(self) -> object:
        self.value_calls += 1
        raise AssertionError("spoofed risk values are not authority")


class _TupleSubclass(tuple[object, ...]):
    def __new__(cls) -> _TupleSubclass:
        return super().__new__(cls, (object(), object()))

    def __iter__(self) -> Any:
        raise AssertionError("tuple-subclass iteration is not authority")

    def __getitem__(self, index: object) -> object:
        raise AssertionError(f"tuple-subclass lookup is not authority: {type(index).__name__}")


def test_definition_and_risk_require_exact_authority_types_before_property_access() -> None:
    definition_spoof = _DefinitionClassSpoof()
    hostile_catalog = _HostileCatalog()
    hostile_catalog.get = cast(Any, lambda _name: (definition_spoof, _execute))

    assert _normalizer()(cast(ToolCatalog, hostile_catalog), "system.read") == (
        "other",
        "other",
    )
    assert definition_spoof.name_calls == 0
    assert definition_spoof.risk_calls == 0

    catalog = ToolCatalog()
    _register(catalog, name="system.read", risk=RiskLevel.READ)
    entry = catalog.get("system.read")
    assert entry is not None
    definition, executor = entry
    risk_spoof = _RiskClassSpoof()
    poisoned = definition.model_copy(update={"risk": risk_spoof})
    hostile_catalog.get = cast(Any, lambda _name: (poisoned, executor))

    assert _normalizer()(cast(ToolCatalog, hostile_catalog), "system.read") == (
        "other",
        "other",
    )
    assert risk_spoof.value_calls == 0


def test_exact_definition_name_type_is_required() -> None:
    catalog = ToolCatalog()
    _register(catalog, name="system.read", risk=RiskLevel.READ)
    entry = catalog.get("system.read")
    assert entry is not None
    definition, executor = entry
    poisoned = definition.model_copy(update={"name": _StringSubclass("system.read")})
    hostile = _HostileCatalog()
    hostile.get = cast(Any, lambda _name: (poisoned, executor))

    assert _normalizer()(cast(ToolCatalog, hostile), "system.read") == ("other", "other")


def test_catalog_return_container_requires_exact_public_tuple_shape() -> None:
    catalog = _HostileCatalog()
    catalog.get = cast(Any, lambda _name: _TupleSubclass())

    assert _normalizer()(cast(ToolCatalog, catalog), "system.read") == ("other", "other")


class _MalformedCatalogShape:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def _explode(self, operation: str) -> object:
        self.calls.append(operation)
        raise _CatalogDiagnostic(f"catalog shape {operation}")

    def __len__(self) -> int:
        return cast(int, self._explode("len"))

    def __iter__(self) -> Any:
        return self._explode("iter")

    def __getitem__(self, _index: object) -> object:
        return self._explode("getitem")

    def __str__(self) -> str:
        return cast(str, self._explode("str"))

    def __repr__(self) -> str:
        return cast(str, self._explode("repr"))


@pytest.mark.parametrize(
    "shape",
    ["empty-tuple", "short-tuple", "long-tuple", "list", "generator", "hostile"],
)
def test_every_malformed_catalog_return_shape_is_closed_without_iteration_or_rendering(
    shape: str,
) -> None:
    backing = ToolCatalog()
    _register(backing, name="system.read", risk=RiskLevel.READ)
    entry = backing.get("system.read")
    assert entry is not None
    hostile = _MalformedCatalogShape()
    returned: object
    if shape == "empty-tuple":
        returned = ()
    elif shape == "short-tuple":
        returned = (entry[0],)
    elif shape == "long-tuple":
        returned = (entry[0], entry[1], object())
    elif shape == "list":
        returned = [entry[0], entry[1]]
    elif shape == "generator":
        returned = (item for item in entry)
    else:
        returned = hostile
    catalog = _HostileCatalog()
    catalog.get = cast(Any, lambda _name: returned)

    assert _normalizer()(cast(ToolCatalog, catalog), "system.read") == ("other", "other")
    assert hostile.calls == []


@pytest.mark.parametrize(
    "invalid_name",
    [None, False, 7, _StringSubclass("system.read"), _HostileValue()],
)
def test_invalid_tool_name_is_rejected_before_any_catalog_access(
    invalid_name: object,
) -> None:
    catalog = _HostileCatalog(AssertionError("catalog access is forbidden"))

    description = cast(Any, _mapper()(cast(ToolCatalog, catalog), invalid_name, "executed"))

    assert catalog.lookups == []
    assert description.tool_family == "other"
    assert description.risk == "other"
    assert description.expected_row_status == "completed"
    assert description.outcome == "completed"
    assert description.failure_class is None
    assert description.terminal_countable is True
    if isinstance(invalid_name, _HostileValue):
        assert invalid_name.str_calls == 0
        assert invalid_name.repr_calls == 0


def test_definition_name_must_exactly_match_the_lookup_key() -> None:
    catalog = ToolCatalog()
    _register(catalog, name="system.read", risk=RiskLevel.READ)
    entry = catalog.get("system.read")
    assert entry is not None
    hostile = _HostileCatalog()
    hostile.get = cast(Any, lambda _name: entry)

    assert _normalizer()(cast(ToolCatalog, hostile), "system.write") == ("other", "other")
