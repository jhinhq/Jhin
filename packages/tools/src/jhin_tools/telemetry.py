"""Pure, catalog-authoritative labels for tool telemetry."""

from __future__ import annotations

from dataclasses import dataclass

from jhin_observability import SPAN_ATTRIBUTE_VALUES
from jhin_policy import RiskLevel, ToolDefinition

from .builtin import ToolCatalog

_TOOL_ROW_COMPLETED = "completed"
_TOOL_ROW_FAILED = "failed"
_TOOL_ROW_DENIED = "denied"
_TOOL_ROW_REJECTED = "rejected"
_TOOL_ROW_EXECUTION_UNKNOWN = "execution_unknown"
_TOOL_ROW_PENDING_APPROVAL = "pending_approval"

_TOOL_OUTCOME_COMPLETED = "completed"
_TOOL_OUTCOME_ACCEPTED = "accepted"
_TOOL_OUTCOME_FAILED = "failed"
_TOOL_OUTCOME_DENIED = "denied"
_TOOL_OUTCOME_REJECTED = "rejected"
_TOOL_OUTCOME_EXECUTION_UNKNOWN = "execution_unknown"
_TOOL_OUTCOME_OTHER = "other"

_TOOL_FAILURE_INTERNAL = "internal"
_TOOL_FAILURE_POLICY = "policy"
_TOOL_FAILURE_EXECUTION_UNKNOWN = "execution_unknown"


@dataclass(frozen=True)
class ToolTelemetryDescription:
    tool_family: str
    risk: str
    expected_row_status: str | None
    outcome: str
    failure_class: str | None
    terminal_countable: bool


def _tool_status_authority(
    gateway_status: object,
) -> tuple[str | None, str | None, str, str | None, bool]:
    if type(gateway_status) is not str:
        return None, None, _TOOL_OUTCOME_OTHER, None, False
    if gateway_status == "executed":
        return _TOOL_ROW_COMPLETED, "approved", _TOOL_OUTCOME_COMPLETED, None, True
    if gateway_status == "failed":
        return (
            _TOOL_ROW_FAILED,
            "approved",
            _TOOL_OUTCOME_FAILED,
            _TOOL_FAILURE_INTERNAL,
            True,
        )
    if gateway_status == "denied":
        return (
            _TOOL_ROW_DENIED,
            "approved",
            _TOOL_OUTCOME_DENIED,
            _TOOL_FAILURE_POLICY,
            True,
        )
    if gateway_status == "rejected":
        return (
            _TOOL_ROW_REJECTED,
            "rejected",
            _TOOL_OUTCOME_REJECTED,
            _TOOL_FAILURE_POLICY,
            True,
        )
    if gateway_status == "execution_unknown":
        return (
            _TOOL_ROW_EXECUTION_UNKNOWN,
            "approved",
            _TOOL_OUTCOME_EXECUTION_UNKNOWN,
            _TOOL_FAILURE_EXECUTION_UNKNOWN,
            True,
        )
    if gateway_status == "needs_approval":
        return (
            _TOOL_ROW_PENDING_APPROVAL,
            "pending",
            _TOOL_OUTCOME_ACCEPTED,
            None,
            False,
        )
    return None, None, _TOOL_OUTCOME_OTHER, None, False


def describe_tool_telemetry(
    catalog: ToolCatalog,
    tool_name: object,
    gateway_status: object,
) -> ToolTelemetryDescription:
    (
        expected_row_status,
        _expected_approval_status,
        outcome,
        failure_class,
        terminal_countable,
    ) = _tool_status_authority(gateway_status)

    tool_family = "other"
    risk = "other"
    if type(tool_name) is str:
        try:
            entry = catalog.get(tool_name)
            if type(entry) is tuple:
                definition, _executor = entry
                if (
                    type(definition) is ToolDefinition
                    and type(definition.name) is str
                    and definition.name == tool_name
                    and type(definition.risk) is RiskLevel
                ):
                    candidate = ""
                    for character in tool_name:
                        if character == ".":
                            break
                        candidate += character
                    else:
                        candidate = ""
                    if candidate in SPAN_ATTRIBUTE_VALUES["jhin.tool_family"]:
                        tool_family = candidate
                    candidate_risk = definition.risk.value
                    if candidate_risk in SPAN_ATTRIBUTE_VALUES["jhin.risk"]:
                        risk = candidate_risk
        except Exception:
            tool_family = "other"
            risk = "other"

    return ToolTelemetryDescription(
        tool_family=tool_family,
        risk=risk,
        expected_row_status=expected_row_status,
        outcome=outcome,
        failure_class=failure_class,
        terminal_countable=terminal_countable,
    )


__all__ = ["ToolTelemetryDescription", "describe_tool_telemetry"]
