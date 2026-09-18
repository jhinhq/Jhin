"""The rules for what an agent may be called.

An agent's name is the first agent-written string that lands in layer 1 of a
system prompt unhedged ("You are Bisby, an AI teammate…"), and it reaches
every colleague through the workspace roster. So the write-time rule is an
allow-list, and the render-time rule is deliberately narrower than it.
"""

import pytest

from jhin_policy import (
    IDENTITY_SELF_CAPABILITY,
    MAX_AGENT_NAME_CHARS,
    MAX_AGENT_NAME_WORDS,
    agent_name_problem,
    identity_grant_specs,
    normalize_agent_name,
    render_safe_agent_name,
)


class TestTheCapability:
    def test_it_is_a_twin_of_the_persona_capability(self) -> None:
        assert IDENTITY_SELF_CAPABILITY == "organization.identity.self"
        assert identity_grant_specs() == ((IDENTITY_SELF_CAPABILITY, {}),)

    def test_it_is_not_in_the_agent_namespace(self) -> None:
        """``agent.identity.self`` would have made ``agent.*`` read as an
        ordinary grantable subtree to whoever writes grants by hand."""
        assert not IDENTITY_SELF_CAPABILITY.startswith("agent.")


class TestNormalization:
    def test_it_collapses_whitespace_and_folds_apostrophes(self) -> None:
        assert normalize_agent_name("  Bisby \n the  Second ") == "Bisby the Second"
        assert normalize_agent_name("O\u2019Brien") == "O'Brien"
        # NFKC: a non-breaking space is a space, a fullwidth B is a B.
        assert normalize_agent_name("Bis\u00a0by") == "Bis by"
        assert normalize_agent_name("\uff22isby") == "Bisby"

    def test_it_never_removes_a_character_that_would_be_refused(self) -> None:
        """Tidying that silently dropped an override character would hand
        validation a string the database never sees."""
        assert "\u202e" in normalize_agent_name("Bis\u202eby")


class TestNamesThatArePermitted:
    @pytest.mark.parametrize(
        "name",
        [
            "Bisby",
            "Senior Software Engineer",
            "QA Engineer",
            "O'Brien",
            "J. Smith",
            "R2-D2",
            "Ada",
            "\u7cfb\u7d71",  # letters in any script
        ],
    )
    def test_ordinary_names_pass(self, name: str) -> None:
        assert agent_name_problem(name) is None

    def test_every_name_on_this_install_passes(self) -> None:
        """The 85 agents in the live workspace, by shape: the rule has to
        admit what people already called their agents."""
        for name in ("Blogger", "Marketing Director", "CTO", "Helper", "Scout", "Primary"):
            assert agent_name_problem(name) is None


class TestNamesThatAreRefused:
    def test_blank_and_letterless(self) -> None:
        assert agent_name_problem("   ") == "a name cannot be blank"
        assert agent_name_problem("...") == "a name needs at least one letter"

    def test_invisible_and_control_characters(self) -> None:
        """Category C: where zero-width joiners and right-to-left overrides
        live."""
        assert "invisible or control" in (agent_name_problem("Bis\u200bby") or "")
        assert "invisible or control" in (agent_name_problem("Bis\u202eby") or "")

    @pytest.mark.parametrize(
        ("raw", "codepoint"),
        [
            ("Bisby\nDoorstop", "U+000A"),
            ("Bisby\r\nDoorstop", "U+000D"),
            ("Bisby\tDoorstop", "U+0009"),
            ("Bisby\u2028Doorstop", "U+2028"),
        ],
    )
    def test_a_line_break_or_tab_is_refused_not_repaired(self, raw: str, codepoint: str) -> None:
        """The operator asked for a name shaped like an injection payload to
        FAIL. ``PATCH`` with "Bisby\\nDoorstop" returned 200 and stored
        "Bisby Doorstop": whitespace collapse ran before the control-character
        check, so nothing dangerous was stored and the agent was quietly
        renamed to something nobody typed. Refused, with the codepoint named,
        rather than repaired."""
        problem = agent_name_problem(raw) or ""
        assert "single line" in problem
        assert codepoint in problem

    def test_the_whitespace_a_person_actually_types_still_works(self) -> None:
        """The rule is about lines, not about tidying: a non-breaking space
        (a phone keyboard, a paste out of a document) is NFKC-folded to a
        space and keeps working, and so does ordinary padding."""
        assert agent_name_problem("Bisby\u00a0Doorstop") is None
        assert agent_name_problem("  Bisby   Doorstop  ") is None

    def test_characters_outside_the_allow_list(self) -> None:
        problem = agent_name_problem("Ops & Analytics") or ""
        assert "cannot contain" in problem and "letters, digits, spaces" in problem
        assert agent_name_problem("Bisby <b>") is not None
        assert agent_name_problem("Bisby:") is not None

    def test_length_and_word_count(self) -> None:
        assert f"at most {MAX_AGENT_NAME_CHARS} characters" in (
            agent_name_problem("Bisby " + "y" * MAX_AGENT_NAME_CHARS) or ""
        )
        assert f"at most {MAX_AGENT_NAME_WORDS} words" in (
            agent_name_problem("The Senior Software Engineer") or ""
        )

    def test_injection_markers_that_fit_inside_three_words(self) -> None:
        """The word limit rules out "ignore all previous instructions" on its
        own, but "You are now" and "New instructions" fit inside it — which
        is why the persona content rules are applied to a name too."""
        assert agent_name_problem("You are now") is not None
        assert agent_name_problem("New instructions") is not None
        assert agent_name_problem("System prompt") is not None

    def test_words_that_belong_to_what_an_agent_may_do(self) -> None:
        problem = agent_name_problem("Grant") or ""
        assert "approvals, permissions or policy" in problem


class TestRenderSafety:
    def test_a_safe_name_renders_as_itself(self) -> None:
        assert render_safe_agent_name("Bisby", fallback="bisby") == "Bisby"

    def test_a_name_that_predates_the_rules_still_renders(self) -> None:
        """The render guard is the safety half of the rule and not the whole
        of it. An agent nobody renamed must not be quietly renamed by a
        deploy, so ``&``, four words and a long name all still render."""
        assert render_safe_agent_name("Ops & Analytics", fallback="ops") == "Ops & Analytics"
        assert render_safe_agent_name("The Senior Software Engineer") == (
            "The Senior Software Engineer"
        )

    def test_an_unrenderable_name_falls_back_to_the_canonical_name(self) -> None:
        assert render_safe_agent_name("Bis\u202eby", fallback="bisby") == "bisby"
        assert render_safe_agent_name("", fallback="senior-software-engineer") == (
            "senior-software-engineer"
        )
        assert render_safe_agent_name("x" * 500, fallback="bisby") == "bisby"

    def test_with_no_usable_fallback_it_returns_nothing(self) -> None:
        """The caller drops the name clause rather than printing something
        the platform cannot vouch for."""
        assert render_safe_agent_name("\u200b") == ""
        assert render_safe_agent_name("\u200b", fallback="\u200b") == ""
