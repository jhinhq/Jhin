"""Jhin trigger engine — pure, connector-agnostic (plan sections 10 and 52).

This package holds no I/O and no connector knowledge: filters are data
(a JSON DSL, never code), evaluation is a pure function over a normalized
event payload, and idempotency keys are deterministic hashes. Connector
specifics (like Linear's ``updatedFrom``) are resolved upstream during
normalization into the ``changed_from`` convention this DSL understands.
"""

from jhin_triggers.filters import (
    FilterError,
    evaluate_filter,
    validate_filter,
)
from jhin_triggers.idempotency import (
    build_idempotency_key,
    transition_fingerprint,
    workflow_id_for_key,
)

__all__ = [
    "FilterError",
    "build_idempotency_key",
    "evaluate_filter",
    "transition_fingerprint",
    "validate_filter",
    "workflow_id_for_key",
]
