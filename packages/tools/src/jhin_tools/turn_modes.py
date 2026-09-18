"""Read-only turn modes narrow both discovery and execution, never permissions."""

from typing import Any

from jhin_policy import RiskLevel, ToolDefinition


def mode_denial(definition: ToolDefinition, metadata: dict[str, Any]) -> tuple[str, str] | None:
    if metadata.get("stop_requested_at"):
        return "turn_stopping", "This turn is stopping; no new actions can start."
    mode = metadata.get("execution_mode", "act")
    # Asking the person clarifies a plan without changing their workspace.
    if (
        mode in ("ask", "plan")
        and definition.risk != RiskLevel.READ
        and definition.name != "organization.ask_person"
    ):
        return (
            "turn_mode_read_only",
            f"{str(mode).title()} mode allows read-only tools. Start an Act turn to make changes.",
        )
    return None
