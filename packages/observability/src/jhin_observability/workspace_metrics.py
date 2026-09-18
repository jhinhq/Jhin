"""Workspace instruments use the service runtime; standalone tools remain usable."""

from jhin_observability.bootstrap import get_runtime
from jhin_observability.config import ObservabilityNotInitializedError
from jhin_observability.metrics import JhinMetrics, noop_metrics


def workspace_metrics() -> JhinMetrics:
    try:
        return get_runtime().metrics
    except ObservabilityNotInitializedError:
        return noop_metrics()
