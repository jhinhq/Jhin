"""Tool risk levels and their default policy actions (plan 12.2).

Defaults apply only when an agent's explicit approval-policy rules do not
match a call. They implement the plan's baseline: read and write tools may
run automatically *once granted*; elevated and destructive tools require a
human decision unless the agent's policy explicitly says otherwise.
"""

from __future__ import annotations

from enum import StrEnum


class RiskLevel(StrEnum):
    READ = "read"
    WRITE = "write"
    ELEVATED = "elevated"
    DESTRUCTIVE = "destructive"


class RuleAction(StrEnum):
    """What an approval-policy rule (or risk default) says about a call."""

    AUTO = "auto"
    APPROVAL = "approval"
    FORBID = "forbid"


# Plan 12.2 defaults, used when no explicit rule matches.
DEFAULT_ACTION_BY_RISK: dict[RiskLevel, RuleAction] = {
    RiskLevel.READ: RuleAction.AUTO,
    RiskLevel.WRITE: RuleAction.AUTO,
    RiskLevel.ELEVATED: RuleAction.APPROVAL,
    RiskLevel.DESTRUCTIVE: RuleAction.APPROVAL,
}
