"""Trust tier to risk floor.

The catalog knows where a server came from. It does not know what the server's
tools do. That is the whole reason this mapping is so short and so blunt: a
provenance signal may raise the bar for approval, and it may never lower it,
and it may never claim a tool is read-only or destructive on evidence it does
not have.

So the floor is only ever ``write`` or ``elevated``. ``read`` would let a
crawled server run unattended on the strength of a crawl; ``destructive``
would assert knowledge of behaviour nobody observed. The one modifier —
an endpoint upstream could not reach raises the floor a step — moves in a
single direction by construction.
"""

from __future__ import annotations

from jhin_catalog_sync.types import TrustTier
from jhin_policy import RiskLevel

# Most trusted first. This is also the gallery's primary sort key, which is
# why it is a rank and not just an ordering of the literals.
TRUST_RANK: dict[str, int] = {
    "curated": 0,
    "registry_verified": 1,
    "smithery_verified": 2,
    "reviewed": 3,
    "indexed": 4,
}

DEFAULT_RISK_BY_TRUST: dict[TrustTier, RiskLevel] = {
    "curated": RiskLevel.WRITE,
    "registry_verified": RiskLevel.WRITE,
    "smithery_verified": RiskLevel.ELEVATED,
    "reviewed": RiskLevel.ELEVATED,
    "indexed": RiskLevel.ELEVATED,
}

RISK_RANK: dict[RiskLevel, int] = {
    RiskLevel.READ: 0,
    RiskLevel.WRITE: 1,
    RiskLevel.ELEVATED: 2,
    RiskLevel.DESTRUCTIVE: 3,
}

# The rank of the floor an unverified endpoint cannot sit below.
_UNVERIFIED_FLOOR: RiskLevel = RiskLevel.ELEVATED

#: The tiers the sync may never take upstream's word for. ``curated`` means "a
#: person at Jhin reviewed this entry", and the only entries that can be true
#: of are the built-ins compiled into ``jhin_connectors``. ``reviewed`` is the
#: same kind of word — "this skill came from a library the Jhin team looked
#: at" — and only the consumer's own allowlist plumbing
#: (``wire.to_row``'s ``marketplace_reviewed`` election) may say it. Upstream
#: declaring either would buy a crawled entry the reassuring badge and the top
#: of the trust sort on nothing but its own say-so.
UNASSERTABLE_TIERS: frozenset[str] = frozenset({"curated", "reviewed"})

#: The original single-tier constant, kept so existing callers and tests keep
#: reading; :data:`UNASSERTABLE_TIERS` is the set the gate actually checks.
UNASSERTABLE_TIER: TrustTier = "curated"

#: What an entry claiming an unassertable tier is demoted to. The entry is
#: kept and stays searchable; only the claim is dropped.
DEMOTED_TIER: TrustTier = "indexed"


def syncable_tier(tier: TrustTier) -> TrustTier:
    """The trust tier a synced row is allowed to carry.

    Every tier outside :data:`UNASSERTABLE_TIERS` passes through. This is the
    trust-tier twin of the reserved-slug gate: what upstream asserts about its
    own provenance is bounded by what the sync is permitted to say.
    """
    return DEMOTED_TIER if tier in UNASSERTABLE_TIERS else tier


def trust_rank(tier: TrustTier) -> int:
    """0..4, most trusted first. An unknown tier sorts last."""
    return TRUST_RANK.get(tier, TRUST_RANK["indexed"])


def risk_rank(level: RiskLevel) -> int:
    """0..3, least dangerous first, so "never lower a floor" is a comparison."""
    return RISK_RANK[level]


def default_risk(tier: TrustTier, *, url_unverified: bool) -> RiskLevel:
    """The risk floor a connection made from an entry of this tier starts at.

    ``curated`` is the one tier an unreachable endpoint does not move: a
    person already looked at the entry, and a URL upstream happened not to
    reach is not evidence against that review.
    """
    base = DEFAULT_RISK_BY_TRUST.get(tier, RiskLevel.ELEVATED)
    if url_unverified and tier != "curated" and risk_rank(base) < risk_rank(_UNVERIFIED_FLOOR):
        return _UNVERIFIED_FLOOR
    return base


__all__ = [
    "DEFAULT_RISK_BY_TRUST",
    "DEMOTED_TIER",
    "RISK_RANK",
    "TRUST_RANK",
    "UNASSERTABLE_TIER",
    "UNASSERTABLE_TIERS",
    "default_risk",
    "risk_rank",
    "syncable_tier",
    "trust_rank",
]
