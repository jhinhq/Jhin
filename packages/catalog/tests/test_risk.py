"""Trust tier to risk floor.

The catalog knows where a server came from. It does not know what the server's
tools do. That is the whole reason this mapping is so short and so blunt: a
provenance signal may raise the bar for approval, and it may never lower it,
and it may never claim a tool is read-only or destructive on evidence it does
not have.
"""

from __future__ import annotations

import pytest

from jhin_catalog_sync.risk import (
    DEFAULT_RISK_BY_TRUST,
    TRUST_RANK,
    UNASSERTABLE_TIERS,
    default_risk,
    risk_rank,
    syncable_tier,
    trust_rank,
)
from jhin_catalog_sync.types import TrustTier
from jhin_policy import RiskLevel, RuleAction
from jhin_policy.risk import DEFAULT_ACTION_BY_RISK

TIERS: tuple[TrustTier, ...] = (
    "curated",
    "registry_verified",
    "smithery_verified",
    "reviewed",
    "indexed",
)


def test_trust_rank_orders_the_five_tiers_most_trusted_first() -> None:
    assert TRUST_RANK == {
        "curated": 0,
        "registry_verified": 1,
        "smithery_verified": 2,
        "reviewed": 3,
        "indexed": 4,
    }
    assert [trust_rank(tier) for tier in TIERS] == [0, 1, 2, 3, 4]


@pytest.mark.parametrize(
    ("tier", "risk", "action"),
    [
        ("curated", RiskLevel.WRITE, RuleAction.AUTO),
        ("registry_verified", RiskLevel.WRITE, RuleAction.AUTO),
        ("smithery_verified", RiskLevel.ELEVATED, RuleAction.APPROVAL),
        ("reviewed", RiskLevel.ELEVATED, RuleAction.APPROVAL),
        ("indexed", RiskLevel.ELEVATED, RuleAction.APPROVAL),
    ],
)
def test_a_verified_endpoint_gets_the_tier_default_and_the_stated_action(
    tier: str, risk: RiskLevel, action: RuleAction
) -> None:
    """The table in the spec, read end to end: tier, floor, and the approval
    behaviour the existing policy engine derives from that floor."""
    assert DEFAULT_RISK_BY_TRUST[tier] is risk  # type: ignore[index]
    assert default_risk(tier, url_unverified=False) is risk  # type: ignore[arg-type]
    assert DEFAULT_ACTION_BY_RISK[risk] is action


@pytest.mark.parametrize("tier", ["registry_verified", "smithery_verified", "reviewed", "indexed"])
def test_an_unverified_endpoint_is_raised_to_elevated(tier: str) -> None:
    assert default_risk(tier, url_unverified=True) is RiskLevel.ELEVATED  # type: ignore[arg-type]


def test_curated_is_the_one_tier_an_unverified_endpoint_does_not_move() -> None:
    """A hand-reviewed entry whose URL upstream could not reach is still a
    hand-reviewed entry; a person already looked at it."""
    assert default_risk("curated", url_unverified=True) is RiskLevel.WRITE
    assert default_risk("curated", url_unverified=False) is RiskLevel.WRITE


@pytest.mark.parametrize("tier", TIERS)
@pytest.mark.parametrize("unverified", [True, False])
def test_the_floor_is_never_read_and_never_destructive(tier: str, unverified: bool) -> None:
    """READ would let a crawled server run unattended on the strength of a
    crawl; DESTRUCTIVE would claim knowledge of behaviour nobody observed."""
    resolved = default_risk(tier, url_unverified=unverified)  # type: ignore[arg-type]
    assert resolved is not RiskLevel.READ
    assert resolved is not RiskLevel.DESTRUCTIVE
    assert resolved in (RiskLevel.WRITE, RiskLevel.ELEVATED)


def test_the_modifier_only_ever_raises() -> None:
    for tier in TIERS:
        verified = default_risk(tier, url_unverified=False)
        unverified = default_risk(tier, url_unverified=True)
        assert risk_rank(unverified) >= risk_rank(verified)


def test_risk_rank_orders_the_four_levels() -> None:
    # Four *risk* levels: the trust vocabulary grew to five, this one did not.
    assert [
        risk_rank(RiskLevel.READ),
        risk_rank(RiskLevel.WRITE),
        risk_rank(RiskLevel.ELEVATED),
        risk_rank(RiskLevel.DESTRUCTIVE),
    ] == [0, 1, 2, 3]
    assert risk_rank(RiskLevel.DESTRUCTIVE) > risk_rank(RiskLevel.ELEVATED)


# --- the tiers the sync may not assert ---------------------------------------


def test_the_unassertable_set_is_exactly_the_two_jhin_words() -> None:
    """``curated`` and ``reviewed`` both mean "somebody at Jhin looked", so
    neither is upstream's to claim."""
    assert frozenset({"curated", "reviewed"}) == UNASSERTABLE_TIERS


@pytest.mark.parametrize("tier", sorted(UNASSERTABLE_TIERS))
def test_syncable_tier_demotes_an_asserted_jhin_word(tier: TrustTier) -> None:
    """``curated`` means a person at Jhin reviewed the entry, and ``reviewed``
    means the skill's library is on Jhin's allowlist. Upstream saying either
    about itself is the trust-tier twin of slug theft."""
    assert syncable_tier(tier) == "indexed"


@pytest.mark.parametrize("tier", ("registry_verified", "smithery_verified", "indexed"))
def test_syncable_tier_passes_every_other_tier_through(tier: TrustTier) -> None:
    assert syncable_tier(tier) == tier


def test_demoting_the_claim_restores_the_unverified_bump() -> None:
    """The claim was not just a badge: ``default_risk`` exempts ``curated``
    from the unverified bump, so asserting it bought a crawled server with an
    endpoint nobody could reach a *lower* floor than telling the truth."""
    assert default_risk("curated", url_unverified=True) is RiskLevel.WRITE
    assert default_risk(syncable_tier("curated"), url_unverified=True) is RiskLevel.ELEVATED
