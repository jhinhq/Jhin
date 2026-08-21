"""Dependency-light metrics facade used before the validated Task 3 registry."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from jhin_observability.registry import MetricName


@dataclass(frozen=True)
class Observation:
    value: int | float
    attributes: Mapping[str, str]


class BoundCounter(Protocol):
    def add(self, amount: int | float, **labels: str) -> None:
        """Record through the validated metric-label boundary."""


class BoundHistogram(Protocol):
    def record(self, amount: int | float, **labels: str) -> None:
        """Record through the validated metric-label boundary."""


class _NoopCounter:
    def add(self, amount: int | float, **labels: str) -> None:
        return None


class _NoopHistogram:
    def record(self, amount: int | float, **labels: str) -> None:
        return None


@dataclass(frozen=True)
class JhinMetrics:
    _counter_getter: Callable[[MetricName], BoundCounter]
    _histogram_getter: Callable[[MetricName], BoundHistogram]
    _observable_setter: Callable[[MetricName, Sequence[Observation]], None]
    is_noop: bool = False

    def counter(self, name: MetricName) -> BoundCounter:
        return self._counter_getter(name)

    def histogram(self, name: MetricName) -> BoundHistogram:
        return self._histogram_getter(name)

    def set_observable(self, name: MetricName, observations: Sequence[Observation]) -> None:
        self._observable_setter(name, observations)


_NOOP_COUNTER = _NoopCounter()
_NOOP_HISTOGRAM = _NoopHistogram()
_NOOP_METRICS = JhinMetrics(
    _counter_getter=lambda _name: _NOOP_COUNTER,
    _histogram_getter=lambda _name: _NOOP_HISTOGRAM,
    _observable_setter=lambda _name, _observations: None,
    is_noop=True,
)


def noop_metrics() -> JhinMetrics:
    return _NOOP_METRICS


__all__ = [
    "BoundCounter",
    "BoundHistogram",
    "JhinMetrics",
    "MetricName",
    "Observation",
    "noop_metrics",
]
