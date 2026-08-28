"""jhin-policy: capability registry, risk levels, and the pure policy
evaluator (plan sections 12 and 42).

This package performs no I/O. The tool gateway (jhin-tools) loads grants and
rules from the database and calls :func:`evaluate`.
"""

from jhin_policy.agent_defaults import (
    ask_person_grant_specs,
    default_agent_grant_specs,
    memory_grant_specs,
)
from jhin_policy.approvals import (
    PRESET_RULES,
    ApprovalPreset,
    matching_preset,
    rules_for_preset,
)
from jhin_policy.ask_person import ASK_PERSON_CAPABILITY
from jhin_policy.capabilities import (
    FORBIDDEN_CAPABILITY_PREFIXES,
    CapabilityRegistry,
    RegistryError,
    ToolDefinition,
    capability_matches,
    is_forbidden_capability,
    is_valid_capability,
)
from jhin_policy.delegation import (
    DelegationFacts,
    DelegationSettings,
    delegation_settings,
    evaluate_delegation,
)
from jhin_policy.evaluator import (
    DecisionType,
    Grant,
    GrantEffect,
    PolicyDecision,
    PolicyRule,
    evaluate,
    scope_matches,
)
from jhin_policy.memory import (
    MEMORY_CAPABILITIES,
    MEMORY_PROPOSE_CAPABILITY,
    MEMORY_READ_CAPABILITY,
)
from jhin_policy.reviews import (
    REVIEW_REQUEST_CAPABILITY,
    ReviewCondition,
    ReviewConditionKind,
    ReviewContext,
    ReviewDecision,
    ReviewerCandidates,
    ReviewerResolution,
    ReviewerSelector,
    ReviewPolicySpec,
    ReviewRequirement,
    evaluate_review_policies,
    policy_spec_from_row,
    resolve_reviewer,
)
from jhin_policy.risk import RiskLevel, RuleAction
from jhin_policy.skills import (
    SKILLS_CAPABILITIES,
    SKILLS_MANAGE_CAPABILITY,
    SKILLS_READ_CAPABILITY,
)
from jhin_policy.work_requests import (
    DIRECTORY_READ_CAPABILITY,
    WORK_REQUEST_CAPABILITY,
    WORK_RESPOND_CAPABILITY,
    CoordinationSettings,
    WorkRequestDecision,
    WorkRequestFacts,
    collaboration_grant_specs,
    coordination_settings,
    evaluate_work_request,
)

__all__ = [
    "ASK_PERSON_CAPABILITY",
    "DIRECTORY_READ_CAPABILITY",
    "FORBIDDEN_CAPABILITY_PREFIXES",
    "MEMORY_CAPABILITIES",
    "MEMORY_PROPOSE_CAPABILITY",
    "MEMORY_READ_CAPABILITY",
    "PRESET_RULES",
    "REVIEW_REQUEST_CAPABILITY",
    "SKILLS_CAPABILITIES",
    "SKILLS_MANAGE_CAPABILITY",
    "SKILLS_READ_CAPABILITY",
    "WORK_REQUEST_CAPABILITY",
    "WORK_RESPOND_CAPABILITY",
    "ApprovalPreset",
    "CapabilityRegistry",
    "CoordinationSettings",
    "DecisionType",
    "DelegationFacts",
    "DelegationSettings",
    "Grant",
    "GrantEffect",
    "PolicyDecision",
    "PolicyRule",
    "RegistryError",
    "ReviewCondition",
    "ReviewConditionKind",
    "ReviewContext",
    "ReviewDecision",
    "ReviewPolicySpec",
    "ReviewRequirement",
    "ReviewerCandidates",
    "ReviewerResolution",
    "ReviewerSelector",
    "RiskLevel",
    "RuleAction",
    "ToolDefinition",
    "WorkRequestDecision",
    "WorkRequestFacts",
    "ask_person_grant_specs",
    "capability_matches",
    "collaboration_grant_specs",
    "coordination_settings",
    "default_agent_grant_specs",
    "delegation_settings",
    "evaluate",
    "evaluate_delegation",
    "evaluate_review_policies",
    "evaluate_work_request",
    "is_forbidden_capability",
    "is_valid_capability",
    "matching_preset",
    "memory_grant_specs",
    "policy_spec_from_row",
    "resolve_reviewer",
    "rules_for_preset",
    "scope_matches",
]
