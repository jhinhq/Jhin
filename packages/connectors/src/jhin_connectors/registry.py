"""Registry of installed connectors (plan 11, 36.5).

Contributing a connector means adding a package directory under
``jhin_connectors/`` and one entry to :data:`DEFAULT_CONNECTORS` — no other
service changes (see docs/architecture/connectors.md).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

from jhin_connectors.base import Connector, ConnectorError
from jhin_tools import ToolCatalog, build_builtin_catalog


class ConnectorRegistry:
    """Installed connectors keyed by connector type."""

    def __init__(self) -> None:
        self._connectors: dict[str, Connector] = {}

    def register(self, connector: Connector) -> None:
        if connector.type in self._connectors:
            raise ConnectorError(f"connector already registered: {connector.type}")
        self._connectors[connector.type] = connector

    def get(self, connector_type: str) -> Connector | None:
        return self._connectors.get(connector_type)

    def types(self) -> tuple[str, ...]:
        return tuple(self._connectors)

    def __iter__(self) -> Iterator[Connector]:
        return iter(self._connectors.values())

    def __len__(self) -> int:
        return len(self._connectors)


def _github() -> Connector:
    from jhin_connectors.github.connector import GitHubConnector

    return GitHubConnector()


def _cli() -> Connector:
    from jhin_connectors.cli.connector import CliConnector

    return CliConnector()


def _linear() -> Connector:
    from jhin_connectors.linear.connector import LinearConnector

    return LinearConnector()


def _vercel() -> Connector:
    from jhin_connectors.vercel.connector import VercelConnector

    return VercelConnector()


# One factory per shipped connector. Factories keep import cost lazy and are
# the single line a contributor adds for a new connector (plan 36.5).
DEFAULT_CONNECTORS: tuple[Callable[[], Connector], ...] = (_github, _cli, _linear, _vercel)


def default_registry() -> ConnectorRegistry:
    """A fresh registry holding every shipped connector."""
    registry = ConnectorRegistry()
    for factory in DEFAULT_CONNECTORS:
        registry.register(factory())
    return registry


def build_default_catalog(registry: ConnectorRegistry | None = None) -> ToolCatalog:
    """The full tool catalog: Phase 4 system built-ins plus every tool of
    every installed connector. The catalog enforces unique tool names and
    refuses self-modification capabilities exactly as for built-ins."""
    catalog = build_builtin_catalog()
    for connector in registry if registry is not None else default_registry():
        for definition, executor in connector.tools():
            catalog.register(definition, executor)
    return catalog
