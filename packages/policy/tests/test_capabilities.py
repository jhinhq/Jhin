"""Registry and capability-name behavior (plan 12.1, 12.3, 21.10-12)."""

import pytest
from pydantic import BaseModel

from jhin_policy import (
    CapabilityRegistry,
    RegistryError,
    RiskLevel,
    ToolDefinition,
    capability_matches,
    is_valid_capability,
)


class _In(BaseModel):
    text: str


class _Out(BaseModel):
    text: str


def _tool(name: str, capability: str | None = None) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description="test tool",
        risk=RiskLevel.READ,
        input_model=_In,
        output_model=_Out,
        required_capability=capability or name,
    )


class TestCapabilityNames:
    def test_valid_names(self) -> None:
        assert is_valid_capability("system.echo")
        assert is_valid_capability("github.pull_request.create")
        assert is_valid_capability("cli")

    def test_invalid_names(self) -> None:
        assert not is_valid_capability("")
        assert not is_valid_capability("System.Echo")
        assert not is_valid_capability("system..echo")
        assert not is_valid_capability("system.echo ")
        assert not is_valid_capability(".system")

    def test_exact_match(self) -> None:
        assert capability_matches("system.echo", "system.echo")
        assert not capability_matches("system.echo", "system.time")

    def test_subtree_wildcard(self) -> None:
        assert capability_matches("system.*", "system.echo")
        assert capability_matches("system.*", "system.note.append")
        assert not capability_matches("system.*", "systemx.echo")
        assert not capability_matches("system.*", "system")

    def test_global_wildcard(self) -> None:
        assert capability_matches("*", "anything.at.all")

    def test_no_mid_pattern_wildcards(self) -> None:
        assert not capability_matches("system.*.append", "system.note.append")


class TestRegistry:
    def test_register_and_lookup(self) -> None:
        registry = CapabilityRegistry()
        tool = _tool("system.echo")
        registry.register(tool)
        assert registry.get("system.echo") is tool
        assert registry.get("system.unknown") is None
        assert len(registry) == 1
        assert registry.names() == ("system.echo",)

    def test_duplicate_registration_fails(self) -> None:
        registry = CapabilityRegistry()
        registry.register(_tool("system.echo"))
        with pytest.raises(RegistryError, match="already registered"):
            registry.register(_tool("system.echo"))

    def test_self_modification_capabilities_rejected(self) -> None:
        """Plan 21.10-12: no tool may grant permissions, alter policy, or
        touch secrets. The registry refuses such registrations outright."""
        registry = CapabilityRegistry()
        for forbidden in (
            "agent.permission.grant",
            "agent.grant.create",
            "agent.policy.update",
            "workspace.member.invite",
            "capability.grant",
            "policy.update",
            "approval.approve",
            "secret.read",
            "auth.session.create",
        ):
            with pytest.raises(RegistryError, match="self-modification"):
                registry.register(_tool(forbidden))
        assert len(registry) == 0

    def test_invalid_tool_name_rejected(self) -> None:
        with pytest.raises(ValueError, match="not a valid dotted capability"):
            _tool("Not A Capability")
