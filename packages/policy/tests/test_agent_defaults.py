"""The grant set every new agent starts with.

These are platform defaults, not a relaxation of deny-by-default: the gateway
still re-decides every call against the agent's live grants.
"""

from jhin_policy import (
    ASK_PERSON_CAPABILITY,
    MEMORY_PROPOSE_CAPABILITY,
    MEMORY_READ_CAPABILITY,
    PERSONA_SELF_CAPABILITY,
    ask_person_grant_specs,
    collaboration_grant_specs,
    default_agent_grant_specs,
    memory_grant_specs,
    persona_grant_specs,
)


def test_a_new_agent_can_remember_and_can_ask() -> None:
    """An agent that cannot remember is a colleague with amnesia, and one
    that cannot ask has to guess."""
    capabilities = [capability for capability, _scope in default_agent_grant_specs()]
    assert MEMORY_READ_CAPABILITY in capabilities
    assert MEMORY_PROPOSE_CAPABILITY in capabilities
    assert ASK_PERSON_CAPABILITY in capabilities


def test_a_new_agent_can_choose_a_persona() -> None:
    """How an agent sounds is its own to choose; what it may do is not.
    The default is exactly the self-facing capability — dressing a
    colleague stays under ``organization.manage_agents``."""
    capabilities = [capability for capability, _scope in default_agent_grant_specs()]
    assert PERSONA_SELF_CAPABILITY in capabilities
    assert PERSONA_SELF_CAPABILITY == "organization.persona.self"
    assert persona_grant_specs() == ((PERSONA_SELF_CAPABILITY, {}),)


def test_the_defaults_are_the_collaboration_baseline_plus_memory_asking_and_persona() -> None:
    assert default_agent_grant_specs() == (
        *collaboration_grant_specs(),
        *memory_grant_specs(),
        *ask_person_grant_specs(),
        *persona_grant_specs(),
    )


def test_the_default_set_is_exactly_these_capabilities() -> None:
    """Pinned by name so a new default is a deliberate change here, not a
    side effect of editing one of the spec functions."""
    assert [capability for capability, _scope in default_agent_grant_specs()] == [
        "organization.directory.read",
        "organization.work.request",
        "organization.work.respond",
        "memory.read",
        "memory.propose",
        "organization.ask_person",
        "organization.persona.self",
    ]


def test_the_collaboration_baseline_is_unchanged() -> None:
    """Docs and other call sites still reference it by name and contents."""
    assert collaboration_grant_specs() == (
        ("organization.directory.read", {}),
        ("organization.work.request", {"targets": "any"}),
        ("organization.work.respond", {}),
    )


def test_no_default_grant_carries_a_scope_that_would_widen_it() -> None:
    """Memory, asking, and personas are granted unscoped because the
    *tool* is where they are bounded — memory policy for one, the ask
    validator and its per-run budget for another, the content rules and
    the approval on a new card for the third. A scope key here would be a
    second, quieter authority model."""
    scoped = {
        capability: scope
        for capability, scope in (
            *memory_grant_specs(),
            *ask_person_grant_specs(),
            *persona_grant_specs(),
        )
        if scope
    }
    assert scoped == {}


def test_nothing_higher_authority_is_granted_by_default() -> None:
    """Delegation transfers ownership; connectors, sandbox, and agent
    management all reach outside the workspace or change who may do what.
    None of them is a platform default."""
    capabilities = {capability for capability, _scope in default_agent_grant_specs()}
    for forbidden in (
        "organization.delegate",
        "organization.create_agent",
        "connector.http.request",
        "sandbox.exec",
        "skills.manage",
        "organization.manage_agents",
    ):
        assert forbidden not in capabilities


def test_the_specs_are_distinct() -> None:
    """A duplicate would write two identical grant rows at every creation
    site; ``agent_capability_grant`` has no unique constraint to catch it."""
    capabilities = [capability for capability, _scope in default_agent_grant_specs()]
    assert len(capabilities) == len(set(capabilities))
